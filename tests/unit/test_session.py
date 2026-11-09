"""The session orchestrator: turns, checked tool calls, escalations, state and store records."""

from pathlib import Path

import anyio
import pytest

from calu.checkers import Route
from calu.config import ClaudeConfig, Config, ContextConfig
from calu.protocol.messages import (
    AgentsUpdate,
    ChatSendParams,
    CheckResult,
    EditDiff,
    EscalationRequest,
    EscalationResolved,
    SessionState,
    ToolResult,
    TurnResult,
    UserInput,
)
from calu.session import Session, SessionError
from tests.unit.helpers import default_settings, make_session

pytestmark = pytest.mark.anyio


async def test_chat_turn_with_checked_tool_calls(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {"text": "Looking."},
                {"tool": "Read", "input": {"file_path": "a.txt"}, "result": "old"},
                {"tool": "Bash", "input": {"command": "make"}, "result": "a.txt"},
                {
                    "tool": "Edit",
                    "input": {"file_path": "a.txt", "old_string": "old", "new_string": "new"},
                    "result": "ok",
                },
                {"tool": "Bash", "input": {"command": "make clean # calu:deny"}, "result": "never"},
                {"text": "Done."},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="Please work"))
        await rec.wait_for(TurnResult)
        await session.stop()

    checks = {c.tool_name + ":" + c.decision for c in rec.of(CheckResult)}
    assert checks == {"Read:ALLOW", "Bash:ALLOW", "Edit:ALLOW", "Bash:DENY"}
    diff = rec.of(EditDiff)[0]
    assert (diff.path, diff.before, diff.after) == ("a.txt", "old\n", "new\n")
    denied = [r for r in rec.of(ToolResult) if r.is_error]
    assert len(denied) == 1 and "calu:deny" in denied[0].content
    turn = session.context.turns[0]
    assert turn.user_inputs == ["Please work"]
    assert [s.tool_name for s in turn.steps] == ["Read", "Bash", "Edit", "Bash"]
    assert turn.answer == "Done."
    states = [e.state for e in rec.of(SessionState)]
    assert states[:3] == ["starting", "idle", "running"] and states[-1] == "stopped"

    # The store holds one step row per tool call, with its decision and its result.
    assert store.identity is not None and store.identity.directory == str(tmp_path)
    assert store.claude_session_id == "fake-session"
    assert store.ended == "stopped"
    steps = [store.steps[i] for i in sorted(store.steps)]
    assert [s.row.tool_name for s in steps] == ["Read", "Bash", "Edit", "Bash"]
    assert [s.row.seq for s in steps] == [1, 2, 3, 4]
    assert all(s.row.turn == 1 for s in steps)
    assert [s.decision.decision for s in steps if s.decision] == [
        "allow",
        "allow",
        "allow",
        "deny",
    ]
    assert steps[0].result_summary == "old"
    assert steps[3].result_summary is not None and steps[3].result_summary.startswith("error:")
    assert store.turns[1].answer == "Done."
    # Reading a file inside the workspace is on the read allowlist: no model call.
    assert [len(s.checker_runs) for s in steps] == [0, 1, 1, 1]
    assert steps[2].checker_runs[0].checker_name == "edit"
    assert steps[2].checker_runs[0].checker_version == 1
    assert steps[2].checker_runs[0].model == "rule-based-checker"


async def test_escalation_waits_for_human(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "git push # calu:escalate"},
                    "result": "pushed",
                },
                {
                    "tool": "Bash",
                    "input": {"command": "deploy # calu:escalate"},
                    "result": "deployed",
                },
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        first = (await rec.wait_for(EscalationRequest))[0]
        assert session.state == "escalated"
        assert "calu:escalate" in first.reason or "fake checker" in first.reason
        assert "git push" in first.rendered
        assert store.escalation_context[int(first.escalation_id)]["rendered"] == first.rendered
        await anyio.sleep(0.05)
        assert not rec.of(ToolResult)  # still blocked
        await session.decide_escalation(first.escalation_id, "allow", None)
        second = (await rec.wait_for(EscalationRequest, 2))[1]
        await session.decide_escalation(second.escalation_id, "deny", "not today")
        await rec.wait_for(TurnResult)
        await session.stop()

    results = rec.of(ToolResult)
    assert [r.is_error for r in results] == [False, True]
    assert "not today" in results[1].content
    with pytest.raises(SessionError):
        await session.decide_escalation("999", "allow", None)
    # A client decision cancels the row and records what was decided.
    rows = [store.escalations[i] for i in sorted(store.escalations)]
    assert [r.state for r in rows] == ["cancelled", "cancelled"]
    assert rows[0].note == "allowed by a client of the session process"
    assert "denied by a client" in rows[1].note and "not today" in rows[1].note


async def test_escalation_resolved_in_the_database_continues_the_session(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        escalation_id = int(request.escalation_id)
        store.resolve(escalation_id, "allowed", "go ahead")
        session.note_resolution(escalation_id)
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.content == "deployed" and not result.is_error
    (resolved,) = rec.of(EscalationResolved)
    assert resolved.decision == "allow" and resolved.note == "go ahead"


async def test_a_denial_in_the_database_is_noticed_by_the_poll(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        store.resolve(int(request.escalation_id), "denied", "too risky")  # no note_resolution
        await rec.wait_for(TurnResult, timeout=10)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.is_error and "too risky" in result.content


async def test_inject_is_taken_in_during_turn(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {"tool": "Bash", "input": {"command": "sleep"}, "result": ""},
                {"sleep": 0.3},
                {"text": "after sleep"},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(ToolResult)
        await session.send_chat(ChatSendParams(text="side note", delivery="inject"))
        await rec.wait_for(TurnResult)
        await session.stop()
    inputs = [e.text for e in rec.of(UserInput)]
    assert inputs == ["go", "side note"]
    assert session.context.turns[0].user_inputs == ["go", "side note"]


async def test_message_to_background_subagent_is_relayed(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {
                    "agent": {
                        "description": "sleeper",
                        "background": True,
                        "actions": [{"sleep": 0.3}, {"text": "sub done"}],
                    }
                },
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="start"))
        update = (await rec.wait_for(AgentsUpdate))[0]
        agent_id = update.agents[0].agent_id
        await session.send_chat(ChatSendParams(text="psst", to=agent_id, delivery="inject"))
        await rec.wait_for(TurnResult)
        with pytest.raises(SessionError, match="running background"):
            await session.send_chat(ChatSendParams(text="late", to=agent_id))
        with pytest.raises(SessionError, match="unknown subagent"):
            await session.send_chat(ChatSendParams(text="x", to="nope"))
        await session.stop()
    relayed = rec.of(UserInput)[1].text
    assert "SendMessage" in relayed and "psst" in relayed and "fake-agent-" in relayed


async def test_turns_are_folded_into_the_chat_summary(tmp_path: Path):
    config = Config(context=ContextConfig(fold_every=3, fold_count=2))
    session, rec, store = make_session(tmp_path, [], config)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        for i in range(3):
            await session.send_chat(ChatSendParams(text=f"message {i}"))
            await rec.wait_for(TurnResult, i + 1)
        with anyio.fail_after(5):
            while len(session.context.turns) != 1:
                await anyio.sleep(0.01)
        await session.stop()
    summary = session.context.summary
    assert "Fake summary covering 2 more turns." in summary
    assert "## User inputs (verbatim)" in summary
    assert "message 0" in summary and "message 1" in summary and "message 2" not in summary
    assert "message 2" in session.context.recent()
    assert store.summaries[-1] == (summary, 2)
    assert store.folded_up_to == 2
    assert sorted(store.turns) == [1, 2, 3]


async def test_a_resumed_session_restores_turns_and_summary(tmp_path: Path):
    session, rec, store = make_session(tmp_path, [[{"text": "welcome back"}]])
    earlier = make_session(tmp_path, [])[0].context
    earlier.user_input("the earlier request")
    turn = earlier.end_turn("the earlier answer")
    assert turn is not None
    store.previous["old-session"] = ([turn], "# Chat summary\n\nEarlier work.\n")
    async with anyio.create_task_group() as tg:
        await session.start(tg, resume_id="old-session")
        assert "Earlier work." in session.checker_context().chat_summary
        assert "the earlier request" in session.checker_context().recent
        await session.send_chat(ChatSendParams(text="go on"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert [t.index for t in session.context.turns] == [1, 2]


async def test_requests_need_a_running_session(tmp_path: Path):
    session, _, _ = make_session(tmp_path, [])
    with pytest.raises(SessionError):
        await session.send_chat(ChatSendParams(text="hi"))
    with pytest.raises(SessionError):
        await session.interrupt()


async def test_changed_settings_take_effect_from_the_next_step(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy"}, "result": "ok"}]],
    )
    strict = default_settings(
        tmp_path,
        Config(claude=ClaudeConfig(model="haiku")),
        rules=None,
    )
    session.apply_settings(strict)
    assert session.config.claude.model == "opus"  # the Claude settings of the run are kept
    assert session.checkers[Route.COMMAND].instructions.startswith("#")
    session.apply_settings(default_settings(tmp_path, Config(context=ContextConfig(narrate=False))))
    assert session.narrator is None
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert all(not s.narration for s in session.context.turns[0].steps)


async def test_a_failing_store_denies_escalations_but_keeps_the_session(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "never"}]],
    )

    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database gone")

    store.open_escalation = broken  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.is_error and "database" in result.content
    assert not rec.of(EscalationRequest)


async def test_the_session_can_be_started_again_after_stopping(tmp_path: Path):
    session, rec, store = make_session(tmp_path, [[{"text": "one"}], [{"text": "two"}]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(TurnResult)
        await session.stop()
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="second"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    assert isinstance(session, Session)
    assert store.states.count("starting") == 2
    assert store.ended == "stopped"
