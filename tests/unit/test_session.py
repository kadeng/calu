"""The session orchestrator: turns, checked tool calls, escalations, state and store records."""

from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from calu.checkers import CheckDecision, CodeMark, Decision, Route, Step
from calu.checkers.checker import Checker, CheckOutcome
from calu.claude import FakeRunner
from calu.claude.runner import Fork, HumanAnswer
from calu.clock import ClockJump
from calu.config import (
    EMPTY_CHAT_SUMMARY,
    AutoEscalate,
    ChangesConfig,
    ClaudeConfig,
    Config,
    ContextConfig,
    HonestyConfig,
)
from calu.honesty import blocking_marks
from calu.protocol.messages import (
    AgentsUpdate,
    AutoEscalateChanged,
    ChatSendParams,
    CheckResult,
    CommentAddParams,
    EditDiff,
    EscalationRequest,
    EscalationResolved,
    HonestyUpdate,
    Log,
    SessionState,
    ToolResult,
    TurnResult,
    UserInput,
)
from calu.session import (
    Session,
    SessionError,
    _Decided,  # the shape `_decide` returns, built directly for one test
    combine_decisions,
)
from calu.store.config import CheckerDefinition
from calu.testing import fake_models, fixed_checker, marking_checker
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
    # PASS: the checkers have no objection, and Claude Code's auto mode decides as well (D21).
    assert checks == {"Read:PASS", "Bash:PASS", "Edit:PASS", "Bash:DENY"}
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
        "pass",
        "pass",
        "pass",
        "deny",
    ]
    # What the allow and deny lists found. The read and the edit of a workspace file are
    # allow-listed; the edit is checked anyway, because the fast path is only for Bash.
    assert [s.decision.rules_outcome for s in steps if s.decision] == [
        "allow",
        "unlisted",
        "allow",
        "unlisted",
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
        assert store.contexts[int(first.escalation_id)]["rendered"] == first.rendered
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
    # The decision of a client is recorded as the resolution it is, with who decided (D28).
    rows = [store.escalations[i] for i in sorted(store.escalations)]
    assert [r.state for r in rows] == ["allowed", "denied"]
    assert [r.resolved_by for r in rows] == ["client:unknown", "client:unknown"]
    assert rows[1].note == "not today"


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
    # A route carries a list of checkers now (D81); its own comes first.
    assert session.checkers[Route.COMMAND][0].instructions.startswith("#")
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


async def test_comments_are_injected_and_stored(tmp_path: Path):
    session, rec, store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        with pytest.raises(SessionError):
            await session.add_comment(
                CommentAddParams(
                    kind="file", path="a.py", start_line=1, end_line=1, after_text="x", text="t"
                )
            )
        await session.start(tg)
        comment = await session.add_comment(
            CommentAddParams(
                kind="diff",
                path="a.py",
                side="after",
                start_line=2,
                end_line=2,
                before_text="a\nb\n",
                after_text="a\nB\n",
                ref="HEAD",
                text="Why upper case?",
            )
        )
        await rec.wait_for(TurnResult)
        await session.stop()
        assert comment.before is not None and comment.before.text == "b"
        (_, stored, turn) = store.comments[0]
        assert stored == comment and turn == 1
    injected = rec.of(UserInput)[0].text
    assert injected.startswith("[User comment on a diff against HEAD of `a.py`]")


async def test_the_snapshot_shows_the_open_escalation_and_the_queue(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        await session.send_chat(ChatSendParams(text="and this too"))
        snapshot = session.snapshot()
        assert snapshot.state == "escalated"
        assert snapshot.workspace == str(tmp_path)
        assert snapshot.calu_session_id == "memory-session"
        assert [e.escalation_id for e in snapshot.escalations] == [request.escalation_id]
        assert [m.text for m in snapshot.queue] == ["and this too"]
        await session.decide_escalation(request.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        await session.stop()
    assert session.initialize_result().workspace == str(tmp_path)
    assert session.snapshot().escalations == []


async def test_an_escalation_nobody_answers_stops_the_session(tmp_path: Path):
    config = Config(escalation_timeout_s=0.3)
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "never"}]],
        config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        # Claude's session id is recorded at the start, so the session stays resumable (D27).
        assert store.claude_session_id == "fake-session"
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        with anyio.fail_after(10):
            while session.state != "stopped":
                await anyio.sleep(0.02)
        await session.stop()
        timed_out = store.escalations[int(request.escalation_id)]
        assert timed_out.state == "cancelled" and "No decision within" in timed_out.note
    (result,) = rec.of(ToolResult)
    assert result.is_error and "No decision within" in result.content
    assert "calu run --resume fake-session" in result.content
    assert [e.message for e in rec.of(Log) if "No decision" in e.message]
    assert store.ended == "stopped"


async def test_waiting_without_a_limit_is_possible(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]],
        Config(escalation_timeout_s=0),
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        await anyio.sleep(0.3)
        assert session.state == "escalated"  # still waiting
        await session.decide_escalation(request.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert not result.is_error


async def test_the_agent_tree_of_a_session_is_recorded(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {
                    "agent": {
                        "description": "reviewer",
                        "background": True,
                        "actions": [{"text": "reviewing"}],
                    }
                }
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="review it"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (agent_id,) = store.agent_parents
    assert store.agent_parents[agent_id] is None  # started by the main agent
    assert store.agents[agent_id].description == "reviewer"
    assert store.agents[agent_id].background
    assert store.agents[agent_id].status == "completed"


async def test_a_backward_clock_jump_escalates_the_next_step_and_then_clears(tmp_path: Path):
    """D32: attribution rests on the clock, so a human sees a backward jump before work goes on."""
    session, rec, _store = make_session(tmp_path, [])
    await session.clock_jumped(ClockJump(backward=True, seconds=42.0, at=1_700_000_000.0))
    logs = [event for event in rec.of(Log) if "clock" in event.message]
    assert logs and logs[-1].level == "error"
    assert "backwards by 42.0 s" in logs[-1].message

    step = Step(tool_name="Read", tool_input={"file_path": "a.txt"}, cwd=str(tmp_path))
    first = await session._decide(step, None)
    assert first.decision.decision is Decision.ESCALATE
    assert first.source == "clock"
    assert "moved" in first.decision.reason and "which step changed which file" in (
        first.decision.reason
    )
    # The answer to that escalation is the answer to the jump, so the next step is normal again.
    second = await session._decide(step, None)
    assert second.decision.decision is not Decision.ESCALATE or second.source != "clock"


async def test_a_forward_clock_jump_is_only_reported(tmp_path: Path):
    session, rec, _store = make_session(tmp_path, [])
    await session.clock_jumped(ClockJump(backward=False, seconds=400.0, at=1_700_000_000.0))
    logs = [event for event in rec.of(Log) if "clock" in event.message]
    assert logs and logs[-1].level == "warning" and "not an escalation" in logs[-1].message
    step = Step(tool_name="Read", tool_input={"file_path": "a.txt"}, cwd=str(tmp_path))
    decided = await session._decide(step, None)
    assert decided.source != "clock"


async def test_the_step_times_of_a_session_are_recorded_for_the_checkers(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    session, _rec, _store = make_session(
        tmp_path,
        [
            [
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a.txt"},
                {"tool": "Read", "input": {"file_path": "a.txt"}, "result": "old"},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="look around"))
        with anyio.fail_after(10):
            while len(session.step_times()) < 2:
                await anyio.sleep(0.01)
        times = session.step_times()
        assert [step.seq for step in times] == [1, 2]
        assert [step.tool_name for step in times] == ["Bash", "Read"]
        # Every window is closed once the result arrived, and the checker's summary is attached.
        with anyio.fail_after(10):
            while not all(step.finished_at for step in session.step_times()):
                await anyio.sleep(0.01)
        assert all(step.finished_at >= step.started_at for step in session.step_times())  # type: ignore[operator]
        assert any(step.summary for step in session.step_times())
        await session.stop()
        tg.cancel_scope.cancel()


async def test_the_write_set_of_a_command_is_measured_when_snapshots_are_on(tmp_path: Path):
    """D31: a command that writes more than it said leaves a trace in the record and the log."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    config = Config(changes=ChangesConfig(snapshot=True))
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "format a.txt"},
                    "writes": {"a.txt": "two\n", "sneaky.txt": "oops\n"},
                    "result": "formatted",
                }
            ]
        ],
        config=config,
    )
    snapshots = tmp_path.parent / f"{tmp_path.name}-snapshots"
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="format it"))
        with anyio.fail_after(20):
            while not store.write_sets:
                await anyio.sleep(0.01)
        recorded = next(iter(store.write_sets.values()))
        assert set(recorded["paths"]) == {"a.txt", "sneaky.txt"}
        assert recorded["before"] and recorded["after"] != recorded["before"]
        assert not recorded["overlapping"]
        # The fake checker's summary names neither file, so both are undeclared.
        assert set(recorded["undeclared"]) == {"a.txt", "sneaky.txt"}
        warnings = [
            event for event in rec.of(Log) if "its summary did not mention" in event.message
        ]
        assert warnings and warnings[0].level == "warning"
        assert list(snapshots.iterdir())  # the repository of this session exists while it runs
        await session.stop()
        tg.cancel_scope.cancel()
    # The trees are only the source of a revert of a recent step, so they go with the session.
    assert not list(snapshots.iterdir())


async def test_without_snapshots_nothing_is_measured(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, _rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "x"}, "writes": {"a.txt": "two\n"}}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        with anyio.fail_after(20):
            while session.state != "idle":
                await anyio.sleep(0.01)
        assert store.write_sets == {}
        assert (tmp_path / "a.txt").read_text() == "two\n"  # the step did run
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_refused_write_set_is_reverted_and_claude_is_told(tmp_path: Path):
    """D31: the step checker judged the command, this judges what it actually wrote."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    config = Config(changes=ChangesConfig(snapshot=True, review=True))
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "generate"},
                    # The written content carries the fake checker's deny marker, not the command
                    # line, so the step passes the gate and the review refuses the change.
                    "writes": {"a.txt": "two # calu:deny\n", "b.txt": "also mine\n"},
                    "result": "generated",
                }
            ]
        ],
        config=config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(TurnResult)
        notes = [event.text for event in rec.of(UserInput) if "undone" in event.text]
        assert notes, [event.text for event in rec.of(UserInput)]
        assert "a.txt" in notes[0] and "calu:deny" in notes[0]
        assert "Do not simply repeat the command" in notes[0]
        # The files are back as they were, and the log says so.
        assert (tmp_path / "a.txt").read_text() == "one\n"
        assert not (tmp_path / "b.txt").exists()
        assert [e for e in rec.of(Log) if "reverted" in e.message and "NOT" not in e.message]
        # The review leaves the same audit trail as the check before the command.
        runs = [run for row in store.steps.values() for run in row.checker_runs]
        assert [run.checker_name for run in runs] == ["command", "changeset"]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_an_accepted_write_set_keeps_the_change_and_passes_a_remark_on(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    config = Config(changes=ChangesConfig(snapshot=True, review=True))
    session, rec, _store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "generate"},
                    "writes": {"a.txt": "two # calu:comment\n"},
                    "result": "generated",
                }
            ]
        ],
        config=config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(TurnResult)
        assert (tmp_path / "a.txt").read_text() == "two # calu:comment\n"
        remarks = [event.text for event in rec.of(UserInput) if "remarked" in event.text]
        assert remarks and "Fake checker remark" in remarks[0]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_an_escalated_write_set_waits_for_the_human_and_reverts_on_a_denial(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    config = Config(changes=ChangesConfig(snapshot=True, review=True))
    session, rec, _store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "generate"},
                    "writes": {"a.txt": "two # calu:escalate\n"},
                    "result": "generated",
                }
            ]
        ],
        config=config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "Escalated by the rule-based fake checker" in request.reason
        # The change is still in place while the human thinks: the command has run.
        assert (tmp_path / "a.txt").read_text() == "two # calu:escalate\n"
        await session.decide_escalation(request.escalation_id, "deny", "put that back")
        await rec.wait_for(TurnResult)
        assert (tmp_path / "a.txt").read_text() == "one\n"
        notes = [event.text for event in rec.of(UserInput) if "undone" in event.text]
        assert notes and "put that back" in notes[0]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_without_the_review_a_write_set_is_only_recorded(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    config = Config(changes=ChangesConfig(snapshot=True))  # measuring on, reviewing off
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "generate"},
                    "writes": {"a.txt": "two # calu:deny\n"},
                    "result": "generated",
                }
            ]
        ],
        config=config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(TurnResult)
        assert (tmp_path / "a.txt").read_text() == "two # calu:deny\n"  # nothing was undone
        assert store.write_sets  # but it was measured
        assert not [event for event in rec.of(UserInput) if "undone" in event.text]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_question_of_the_agent_is_answered_by_the_human(tmp_path: Path):
    """D35: AskUserQuestion is escalated without a checker, and the answer is the result."""
    questions = [
        {
            "question": "Should this project indent with tabs or with spaces?",
            "header": "Indentation",
            "options": [
                {"label": "Tabs", "description": "tab characters"},
                {"label": "Spaces", "description": "space characters"},
            ],
            "multiSelect": False,
        }
    ]
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "AskUserQuestion", "input": {"questions": questions}, "result": "unused"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="set up the project"))
        request = (await rec.wait_for(EscalationRequest))[0]
        # The question and its options reach the client, so it can show the real choices.
        assert request.questions == questions
        assert "tabs or with spaces" in request.reason
        assert "Tabs, Spaces" in request.reason
        # No checker was asked: only the human knows the answer.
        assert [c.route for c in rec.of(CheckResult)] == [Route.QUESTION]
        assert [c.decision for c in rec.of(CheckResult)] == [Decision.ESCALATE]

        await session.decide_escalation(request.escalation_id, "deny", None, answer="Spaces")
        await rec.wait_for(TurnResult)
        # Claude receives the answer as the result of the call, which did not run.
        results = rec.of(ToolResult)
        assert results and "The user answered your question: Spaces" in results[0].content
        assert "was not run" in results[0].content
        # The row says what happened: answered, not allowed and not denied.
        row = store.escalations[int(request.escalation_id)]
        assert row.state == "answered" and row.note == "Spaces"
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_fork_point_reaches_the_runner_and_the_uuids_are_recorded(tmp_path: Path):
    """D36: a fork tells the runner where to branch, and turns keep the uuids it needs."""
    session, _rec, _store = make_session(tmp_path, [[{"text": "done"}]])
    fork = Fork(resume_at="entry-of-turn-1", drops_turn="prompt-of-turn-2")
    async with anyio.create_task_group() as tg:
        await session.start(tg, resume_id="claude-1", fork=fork)
        runner = session.runner
        assert isinstance(runner, FakeRunner)
        assert runner.fork == fork  # the runner would pass it to resume_session_at/drops_turn
        await session.stop()
        tg.cancel_scope.cancel()

    # The uuids come from the events Claude Code replays; the fake runner reports none, so a
    # turn recorded from it has none either. Feed them by hand to check they are kept.
    session.context.user_input("fix it", uuid="prompt-uuid")
    session.context.assistant_entry("assistant-uuid")
    turn = session.context.end_turn("done")
    assert turn is not None
    assert turn.prompt_uuid == "prompt-uuid"
    assert turn.last_entry_uuid == "assistant-uuid"
    # A second prompt in the same turn does not move the fork point.
    session.context.user_input("and this", uuid="later-uuid")
    assert session.context.current is not None
    assert session.context.current.prompt_uuid == "later-uuid"  # a new turn, its own prompt


async def test_a_confirmation_the_harness_asks_for_escalates_even_after_a_pass(tmp_path: Path):
    """D38: auto mode can ask, not only allow or deny, and that reaches calu as an escalation."""
    session, rec, store = make_session(tmp_path, [])
    step = Step(
        tool_name="Bash", tool_input={"command": "deploy"}, tool_use_id="t1", cwd=str(tmp_path)
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        answers: list[HumanAnswer] = []

        async def ask() -> None:
            """Stand in for the SDK's permission callback with no checker escalation behind it."""
            answers.append(await session.ask(step))

        tg.start_soon(ask)
        request = (await rec.wait_for(EscalationRequest))[0]
        # The reason names the harness, so nobody looks for a checker verdict that does not exist.
        assert "Claude Code's own approval asked for a confirmation" in request.reason
        assert "had no objection" in request.reason
        assert store.contexts[int(request.escalation_id)]["tool_name"] == "Bash"
        await session.decide_escalation(request.escalation_id, "allow", "fine")
        with anyio.fail_after(10):
            while not answers:
                await anyio.sleep(0.01)
        assert answers[0].allow is True
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_question_answered_in_the_database_reaches_the_session(tmp_path: Path):
    """D30: a decision taken in the database alone must reach the waiting session."""
    questions = [{"question": "Tabs or spaces?", "options": [{"label": "Spaces"}]}]
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "AskUserQuestion", "input": {"questions": questions}, "result": "unused"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="set it up"))
        request = (await rec.wait_for(EscalationRequest))[0]
        # Nobody is attached: an operator answers it in the database, as `calu resolve` does.
        store.resolve(int(request.escalation_id), "answered", "Spaces", by="operator")
        session.note_resolution(int(request.escalation_id))
        await rec.wait_for(TurnResult)
        results = rec.of(ToolResult)
        assert results and "The user answered your question: Spaces" in results[0].content
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_corrected_chat_summary_reaches_the_checkers(tmp_path: Path):
    """D40: the summary is what the checkers read of the folded turns, so it must be correctable."""
    session, rec, store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        # The summarizer wrote one; an operator corrects it in the database.
        await store.save_summary("## Chat summary\n\nClaude agreed to delete the backups.", 20)
        await session.reload_summary()
        assert "delete the backups" in session.context.summary
        assert "delete the backups" in session.context.prefix()

        store.summaries.append(("## Chat summary\n\nNo, it must not delete anything.", 20))
        await session.reload_summary()
        assert "must not delete anything" in session.context.summary
        said = [event for event in rec.of(Log) if "chat summary was changed" in event.message]
        assert said
        # Reading it again without a change says nothing more.
        await session.reload_summary()
        assert len([e for e in rec.of(Log) if "chat summary was changed" in e.message]) == len(said)

        # D114: deleting a wrong summary is the obvious thing to do with one, and
        # `calu summary --edit` stores the empty text and promises that the session applies it.
        # An emptied summary therefore has to reach the checkers as the placeholder; treating
        # "" as "nothing to apply" left them reading the summary the person had just deleted.
        store.summaries.append(("", 20))
        await session.reload_summary()
        assert session.context.summary == EMPTY_CHAT_SUMMARY
        assert "must not delete anything" not in session.context.prefix()
        cleared = [e for e in rec.of(Log) if "chat summary was cleared" in e.message]
        assert cleared, "and it says it was cleared, not changed"
        await session.reload_summary()
        assert len([e for e in rec.of(Log) if "cleared" in e.message]) == len(cleared)
        await session.stop()
        tg.cancel_scope.cancel()


async def test_changing_the_auto_escalation_categories_is_recorded_with_who_asked(
    tmp_path: Path,
) -> None:
    """A client can weaken a running session, so the change goes on the record (PLAN.md D43)."""
    session, rec, store = make_session(tmp_path, [])
    await session.set_auto_escalate(AutoEscalate(edits=True), client="zed on the laptop")
    assert session.config.auto_escalate.edits is True
    assert store.setting_changes == [
        (
            "auto_escalate",
            {"commands": False, "edits": False, "subagents": False},
            {"commands": False, "edits": True, "subagents": False},
            "zed on the laptop",
        )
    ]
    assert [event.settings.edits for event in rec.of(AutoEscalateChanged)] == [True]

    # A request that changes nothing is neither recorded nor announced.
    await session.set_auto_escalate(AutoEscalate(edits=True), client="zed on the laptop")
    assert len(store.setting_changes) == 1
    assert len(rec.of(AutoEscalateChanged)) == 1

    # Switching it off again is recorded as well -- that is the direction that matters.
    await session.set_auto_escalate(AutoEscalate(), client="whoever")
    assert store.setting_changes[-1][1:] == (
        {"commands": False, "edits": True, "subagents": False},
        {"commands": False, "edits": False, "subagents": False},
        "whoever",
    )


async def test_a_reloaded_configuration_records_the_categories_it_changed(tmp_path: Path) -> None:
    """A reload is the other way the categories change, and it names itself as the actor."""
    session, rec, store = make_session(tmp_path, [])
    before = session.config.auto_escalate
    session.apply_settings(
        default_settings(tmp_path, Config(auto_escalate=AutoEscalate(commands=True)))
    )
    await session.announce_auto_escalate(before)
    assert store.setting_changes[-1][0] == "auto_escalate"
    assert store.setting_changes[-1][3] == "session: reloaded settings"
    assert [event.settings.commands for event in rec.of(AutoEscalateChanged)] == [True]

    # A reload that leaves them alone records nothing.
    await session.announce_auto_escalate(session.config.auto_escalate)
    assert len(store.setting_changes) == 1


async def test_the_session_beats_until_it_is_cancelled(tmp_path: Path) -> None:
    """A herd tells a live session from a dead one by this alone (PLAN.md D56)."""
    session, _rec, store = make_session(tmp_path, [])
    with anyio.move_on_after(1):
        async with anyio.create_task_group() as tg:
            tg.start_soon(session.beat, 0.02)
            with anyio.fail_after(5):
                while store.heartbeats < 3:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
    assert store.heartbeats >= 3

    # An interval of 0 switches it off, and the call returns at once.
    before = store.heartbeats
    with anyio.fail_after(5):
        await session.beat(0)
    assert store.heartbeats == before

    # The first beat waits one interval: `calu run` starts the loop before the session row
    # exists, and beating at once made every session report a failed heartbeat.
    fresh, _rec, store = make_session(tmp_path, [])
    with anyio.move_on_after(1):
        async with anyio.create_task_group() as tg:
            tg.start_soon(fresh.beat, 5.0)
            await anyio.sleep(0.05)
            assert store.heartbeats == 0, "the heartbeat ran before the session row existed"
            tg.cancel_scope.cancel()


async def test_a_failing_heartbeat_is_reported_and_the_loop_goes_on(tmp_path: Path) -> None:
    """Losing the stamp is not a reason to stop a session that is otherwise fine."""
    session, rec, store = make_session(tmp_path, [])
    calls = {"n": 0}

    async def failing() -> None:
        """Fail the first two heartbeats, then work.

        Raises:
            RuntimeError: For the first two calls.
        """
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("the database went away")
        store.heartbeats += 1

    store.heartbeat = failing  # type: ignore[method-assign]
    with anyio.move_on_after(2):
        async with anyio.create_task_group() as tg:
            tg.start_soon(session.beat, 0.02)
            with anyio.fail_after(5):
                while store.heartbeats < 1:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
    assert calls["n"] >= 3, "the loop stopped at the first failure"
    said = [event.message for event in rec.of(Log)]
    assert any("heartbeat failed" in message for message in said)


async def test_a_database_that_keeps_refusing_stops_the_session(tmp_path: Path) -> None:
    """A session nobody can watch or unblock gives the machine back (PLAN.md D59)."""
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "ls"}, "result": "ok"}]],
        Config(store_failures_before_stop=3),
    )

    async def refuse(*_args: object, **_kwargs: object) -> None:
        """Fail every write.

        Raises:
            RuntimeError: Always, as a database that is gone does.
        """
        raise RuntimeError("the database went away")

    store.add_checker_run = refuse  # type: ignore[method-assign]
    store.save_turn = refuse  # type: ignore[method-assign]
    store.set_state = refuse  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="look"))
        with anyio.fail_after(15):
            while session.state != "stopped":
                await anyio.sleep(0.02)
        await session.stop()

    said = [event.message for event in rec.of(Log)]
    assert any("failed:" in message for message in said), said[:4]
    stopping = [m for m in said if "database writes failed in a row" in m]
    assert stopping, f"the session stopped without saying why: {said[-3:]}"
    assert "watch or unblock" in stopping[0]
    assert "--resume" in stopping[0] or "calu sessions" in stopping[0]


async def test_a_single_failure_does_not_end_an_overnight_run(tmp_path: Path) -> None:
    """A network hiccup must not stop a session: the count resets on the next write."""
    session, rec, store = make_session(tmp_path, [], Config(store_failures_before_stop=3))
    calls = {"n": 0}
    real = store.set_state

    async def flaky(state: object) -> None:
        """Fail every other call.

        Args:
            state: The new state.

        Raises:
            RuntimeError: On every second call.
        """
        calls["n"] += 1
        if calls["n"] % 2:
            raise RuntimeError("a hiccup")
        await real(state)  # type: ignore[arg-type]

    store.set_state = flaky  # type: ignore[method-assign]
    for _ in range(8):
        await session._store_call("setting the state", store.set_state("idle"))
    # Eight attempts, four failures, never two in a row: the limit of three is never reached,
    # so nothing says the session is giving up and the counter is back at zero.
    assert calls["n"] == 8
    assert not [m for m in (e.message for e in rec.of(Log)) if "in a row" in m]
    assert session._store_failures == 0
    # Each failure was still reported, because a silent failure is worse than a noisy one.
    assert len([m for m in (e.message for e in rec.of(Log)) if "a hiccup" in m]) == 4


async def test_a_harness_question_leads_with_what_claude_code_asked(tmp_path: Path) -> None:
    """A capability request has no useful tool name or input, so its sentence comes first (D60).

    Without this the human reads "SandboxNetworkAccess" and a paragraph about checkers having
    no objection, and has to guess which domain or path is meant.
    """
    session, rec, store = make_session(tmp_path, [])
    step = Step(
        tool_name="SandboxNetworkAccess",
        tool_input={},
        tool_use_id="call-1",
        cwd=str(tmp_path),
        asked_about="Allow network connection to example.net?",
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        answers: list[object] = []

        async def ask() -> None:
            """Wait for the human, as the permission callback does."""
            answers.append(await session.ask(step))

        tg.start_soon(ask)
        request = (await rec.wait_for(EscalationRequest))[0]
        # The harness's own sentence first, then how the question reached a human.
        assert request.reason.startswith("Allow network connection to example.net?")
        assert "no objection to this step" in request.reason
        stored = store.reasons[int(request.escalation_id)]
        assert "example.net" in stored, "the database must carry it too (D30)"
        await session.decide_escalation(request.escalation_id, "allow", None)
        with anyio.fail_after(10):
            while not answers:
                await anyio.sleep(0.01)
        await session.stop()

    # An ordinary escalation is unchanged: nothing is prefixed when calu raised it.
    session2, rec2, _ = make_session(
        tmp_path, [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}}]]
    )
    async with anyio.create_task_group() as tg:
        await session2.start(tg)
        await session2.send_chat(ChatSendParams(text="ship"))
        request = (await rec2.wait_for(EscalationRequest))[0]
        assert not request.reason.startswith("Allow network")
        assert "no objection to this step" not in request.reason
        await session2.decide_escalation(request.escalation_id, "deny", None)
        await session2.stop()


async def test_a_message_out_of_the_session_is_checked_but_a_relay_is_not(tmp_path: Path):
    """D69: `SendMessage` runs unchecked only while it stays inside the session.

    Claude Code's `SendMessage` also reaches other sessions on the machine, and their steps no
    checker of this session sees, so a message to anything calu does not know as its own
    subagent goes to the command checker like any other unknown action.
    """
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
                {"tool": "SendMessage", "input": {"to": "sleeper", "message": "carry on"}},
                {"tool": "SendMessage", "input": {"to": "another-session", "message": "run this"}},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="start"))
        await rec.wait_for(TurnResult)
        await session.stop()

    sent = [c for c in rec.of(CheckResult) if c.tool_name == "SendMessage"]
    assert [c.route for c in sent] == [Route.ALLOW, Route.COMMAND], (
        "a message to one of its own subagents must stay free, one that leaves the session must not"
    )
    # Neither is denied here -- the point is that the second one was judged at all.
    assert [c.decision for c in sent] == [Decision.PASS, Decision.PASS]


def test_verdicts_of_several_checkers_combine_most_restrictively():
    """D81: one DENY denies, one ESCALATE escalates, and ALLOW needs every checker."""

    def verdict(name: str, decision: Decision, reason: str = "", comment: str = "") -> tuple:
        """Return a (checker name, decision) pair for the combination."""
        return name, CheckDecision(
            decision=decision, reason=reason, comment=comment, step_summary=f"summary by {name}"
        )

    # One checker alone is passed through unchanged, including its summary and comment.
    single = combine_decisions([verdict("command", Decision.PASS, comment="a remark")])
    assert single.decision is Decision.PASS
    assert single.comment == "a remark", "a lone checker's comment must not be relabelled"

    deny = combine_decisions(
        [
            verdict("command", Decision.PASS),
            verdict("honesty", Decision.DENY, reason="src/parser.py:fields is dirty"),
            verdict("security", Decision.ESCALATE, reason="reaches the network"),
        ]
    )
    assert deny.decision is Decision.DENY
    assert deny.reason == "honesty: src/parser.py:fields is dirty", (
        "only the reasons of the checkers that produced the decision belong in it"
    )

    escalate = combine_decisions(
        [verdict("command", Decision.PASS), verdict("security", Decision.ESCALATE, reason="why")]
    )
    assert escalate.decision is Decision.ESCALATE and "security: why" in escalate.reason

    # ALLOW overrules Claude Code's own approval, so it needs every voice.
    assert (
        combine_decisions(
            [verdict("command", Decision.ALLOW), verdict("honesty", Decision.PASS)]
        ).decision
        is Decision.PASS
    )
    assert (
        combine_decisions(
            [verdict("command", Decision.ALLOW), verdict("honesty", Decision.ALLOW)]
        ).decision
        is Decision.ALLOW
    )

    # Every comment is kept and named; the summary comes from the first checker that wrote one.
    passed = combine_decisions(
        [
            verdict("command", Decision.PASS, comment="name the units"),
            verdict("honesty", Decision.PASS, comment="the docstring is vague"),
        ]
    )
    assert passed.decision is Decision.PASS
    assert "command: name the units" in passed.comment
    assert "honesty: the docstring is vague" in passed.comment
    assert passed.step_summary == "summary by command"

    with pytest.raises(ValueError, match="no checker"):
        combine_decisions([])


async def test_a_second_checker_on_a_route_runs_and_is_recorded(tmp_path: Path):
    """D81: a checker row added to a route is asked about every step of that route.

    This is what lets an honesty, security or over-engineering checker exist beside the command
    checker without a code change (§13.5): the row decides, not the session.
    """
    settings = default_settings(tmp_path)
    extra = CheckerDefinition(
        name="honesty",
        route="command",
        instructions="# Honesty checker\n\nJudge whether the code tells the truth.",
        model="test:model",
        request_limit=5,
        output_tokens_limit=24_000,
        max_tokens=8_000,
        version=1,
    )
    settings = replace(settings, checkers={**settings.checkers, "honesty": extra})
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy the parser"}, "result": "ok"}]],
        settings=settings,
        checker_models={
            **fake_models(),
            # The command checker passes this step; the second checker denies it.
            "honesty": fixed_checker("DENY", "src/parser.py:fields is dirty: fix the docstring"),
        },
    )
    assert [c.name for c in session.checkers[Route.COMMAND]] == ["command", "honesty"]

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()

    # The step was denied, although the route's own checker had no objection.
    (check,) = [e for e in rec.of(CheckResult) if e.tool_name == "Bash"]
    assert check.decision is Decision.DENY
    assert "honesty: src/parser.py:fields is dirty" in check.reason, (
        "a denial must name the checker it came from"
    )
    # Both checkers are recorded, so `calu checker stats` can price and judge each of them.
    runs = [run for step in store.steps.values() for run in step.checker_runs]
    assert sorted(run.checker_name for run in runs) == ["command", "honesty"]
    assert {run.decision for run in runs} == {"pass", "deny"}  # rows keep them lower case


async def test_the_honesty_register_blocks_running_dirty_code_but_not_editing_it(tmp_path: Path):
    """§13 end to end: a checker marks code dirty, a command that runs it is denied.

    The checker's marks travel as fields of its verdict and calu applies them (D82). The
    enforcement is deterministic and needs no model call, and the way out stays open: editing
    the dishonest code is never blocked (D83).
    """
    # The marked file has to exist: a command only counts as running it when the file is
    # really there, or every word of a command would look like a path (D89).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text("def fields():\n    return []\n", encoding="utf-8")
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="enforce")))
    extra = CheckerDefinition(
        name="honesty",
        route="edit",
        instructions="# Honesty checker",
        model="test:model",
        request_limit=5,
        output_tokens_limit=24_000,
        max_tokens=8_000,
        version=1,
    )
    settings = replace(settings, checkers={**settings.checkers, "honesty": extra})
    session, rec, store = make_session(
        tmp_path,
        [
            [
                # The edit leaves the code dishonest, and the honesty checker says so.
                {"tool": "Write", "input": {"file_path": "src/parser.py"}, "result": "ok"},
                # Running it is denied; editing it again is not.
                {"tool": "Bash", "input": {"command": "python src/parser.py"}, "result": "ok"},
                {"tool": "Write", "input": {"file_path": "src/parser.py"}, "result": "ok"},
            ]
        ],
        settings=settings,
        checker_models={
            **fake_models(),
            "honesty": marking_checker(
                dirty=[("src/parser.py", "fields", "the docstring promises a sorted list")]
            ),
        },
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()

    # The mark was recorded from the verdict.
    (open_mark,) = [m for m in store.honesty_marks if m.open]
    assert open_mark.label() == "src/parser.py:fields"

    checks = {c.tool_name: c for c in rec.of(CheckResult)}
    # The command that would run the dirty file is denied, with the reason Claude needs.
    assert checks["Bash"].decision is Decision.DENY
    assert "recorded as dishonest" in checks["Bash"].reason
    assert "src/parser.py:fields" in checks["Bash"].reason
    # The edit of the same file is not blocked: that is how the dishonesty gets fixed.
    assert checks["Write"].decision is not Decision.DENY


async def test_tracking_honesty_records_without_blocking(tmp_path: Path):
    """D83: `track` is how a user judges the checker before trusting it to block."""
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="track")))
    extra = CheckerDefinition(
        name="honesty",
        route="edit",
        instructions="# Honesty checker",
        model="test:model",
        request_limit=5,
        output_tokens_limit=24_000,
        max_tokens=8_000,
        version=1,
    )
    settings = replace(settings, checkers={**settings.checkers, "honesty": extra})
    session, rec, store = make_session(
        tmp_path,
        [
            [
                {"tool": "Write", "input": {"file_path": "src/parser.py"}, "result": "ok"},
                {"tool": "Bash", "input": {"command": "python src/parser.py"}, "result": "ok"},
            ]
        ],
        settings=settings,
        checker_models={
            **fake_models(),
            "honesty": marking_checker(dirty=[("src/parser.py", "", "the name lies")]),
        },
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()

    assert [m.label() for m in store.honesty_marks if m.open] == ["src/parser.py"]
    checks = {c.tool_name: c for c in rec.of(CheckResult)}
    assert checks["Bash"].decision is not Decision.DENY, "track records, it does not block"


async def test_a_deny_rule_outranks_the_honesty_register(tmp_path: Path):
    """§13.4: a rule the user wrote is a stronger statement than honesty debt.

    A step can both match a deny rule and touch code the register calls dishonest. It must be
    reported with the rule, because that is what the user asked for explicitly; being refused
    for dishonesty would hide the more important reason and send Claude to fix the wrong thing.
    """
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="enforce")))
    extra = CheckerDefinition(
        name="honesty",
        route="edit",
        instructions="# Honesty checker",
        model="test:model",
        request_limit=5,
        output_tokens_limit=24_000,
        max_tokens=8_000,
        version=1,
    )
    settings = replace(settings, checkers={**settings.checkers, "honesty": extra})
    session, rec, _store = make_session(
        tmp_path,
        [
            [
                {"tool": "Write", "input": {"file_path": "src/parser.py"}, "result": "ok"},
                # `sudo` is on the shipped deny list, and the command also names the dirty file.
                {
                    "tool": "Bash",
                    "input": {"command": "sudo python src/parser.py"},
                    "result": "no",
                },
            ]
        ],
        settings=settings,
        checker_models={
            **fake_models(),
            "honesty": marking_checker(dirty=[("src/parser.py", "fields", "the docstring lies")]),
        },
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        # The rule escalated it: a human decides, and the reason names the rule rather than
        # the register.
        assert "deny rule" in request.reason
        assert "dishonest" not in request.reason
        await session.decide_escalation(request.escalation_id, "deny", None)
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_a_cleared_mark_reaches_a_running_session(tmp_path: Path):
    """D84: the override has to work on the session that is refusing, not only the next one.

    `calu honesty clear` and the web interface close a mark for a person who disagrees with it.
    A running session keeps the register in memory, so without re-reading it the session would
    go on refusing the step and the override would be useless exactly when it is needed.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text("x = 1\n", encoding="utf-8")
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="enforce")))
    session, rec, store = make_session(tmp_path, [], settings=settings)
    # As if a checker had recorded it earlier.
    await store.apply_honesty_marks(
        [CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies")], [], None
    )
    session.honesty_marks = await store.open_honesty_marks()
    step = Step(tool_name="Bash", tool_input={"command": "python src/parser.py"}, cwd=str(tmp_path))
    assert blocking_marks(step, session.honesty_marks, tmp_path), "the step is refused now"

    # The person clears it in the database; the session hears about it with the configuration.
    mark_id = session.honesty_marks[0].id
    await store.apply_honesty_marks(
        [], [CodeMark(path="src/parser.py", symbol="fields", reason="cleared by hand")], None
    )
    await session.reload_register()

    assert session.honesty_marks == [], "the running session dropped the cleared mark"
    assert not blocking_marks(step, session.honesty_marks, tmp_path)
    # Clients are told, so an editor or `calu attach` stops showing it as blocked.
    update = rec.of(HonestyUpdate)[-1]
    assert update.marks == [] and update.mode == "enforce"
    assert mark_id not in {mark.id for mark in update.marks}

    # Nothing is emitted when the register did not change, so a settings reload is quiet.
    before = len(rec.of(HonestyUpdate))
    await session.reload_register()
    assert len(rec.of(HonestyUpdate)) == before


async def test_a_failed_mark_write_still_leaves_the_session_with_the_marks_that_landed(
    tmp_path: Path,
):
    """D111: the register has to be read back even when a write of the same step failed.

    Two checkers of one route both record marks. The first write lands; the second fails. The
    early return that used to follow left `self.honesty_marks` as it was, so the enforcement
    layer did not know about the mark the first checker had just opened and let the very step
    the register forbids through -- until some later step happened to produce marks and reload
    it. The session now reads the register back anyway and says that marks of this step may be
    missing.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text("x = 1\n", encoding="utf-8")
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="enforce")))
    session, rec, store = make_session(tmp_path, [], settings=settings)

    def checker(name: str) -> Checker:
        """A checker object; only its name is used here."""
        return Checker(name=name, instructions="", model="test", workspace=tmp_path)

    def outcome(mark: CodeMark) -> CheckOutcome:
        """A verdict that records one dirty spot."""
        return CheckOutcome(
            decision=CheckDecision(
                decision=Decision.PASS, reason="", step_summary="edited it", marks_dirty=[mark]
            )
        )

    first = CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies")
    second = CodeMark(path="src/parser.py", symbol="rows", reason="the name says nothing")
    decided = _Decided(
        route=Route.EDIT,
        decision=CheckDecision(decision=Decision.PASS, reason="", step_summary="edited it"),
        source="checker",
        runs=[(checker("honesty"), outcome(first)), (checker("security"), outcome(second))],
    )

    real = store.apply_honesty_marks
    calls = 0

    async def failing(*args, **kwargs):
        """Let the first write through and fail the second."""
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("the database went away")
        return await real(*args, **kwargs)

    store.apply_honesty_marks = failing  # type: ignore[method-assign]
    step = Step(tool_name="Edit", tool_input={"file_path": "src/parser.py"}, cwd=str(tmp_path))
    await session._apply_honesty(decided, 1, step)
    store.apply_honesty_marks = real  # type: ignore[method-assign]

    assert calls == 2, "it stops writing after the failure"
    assert [mark.symbol for mark in session.honesty_marks] == ["fields"], (
        "the mark that landed has to reach the running session, or it enforces nothing"
    )
    run = Step(tool_name="Bash", tool_input={"command": "python src/parser.py"}, cwd=str(tmp_path))
    assert blocking_marks(run, session.honesty_marks, tmp_path), "and it blocks the step now"

    said = [entry.message for entry in rec.of(Log)]
    assert any("recording honesty marks failed" in message for message in said), said
    assert any("the register may be missing marks of this step" in m for m in said), said
    # The clean-mark line must not be printed after a failure: the clean marks of the checkers
    # that were never reached were not offered to the register at all.
    assert not any("matched nothing recorded" in message for message in said), said


async def test_a_checker_cannot_mark_a_file_outside_the_project(tmp_path: Path):
    """D112: the filter has to be in the session's path, not only available to it.

    A mark is a field of the checker's structured answer, so `/etc/passwd` is as easy for a
    model to write as `src/parser.py`. Such a mark would sit in the register until a person
    cleared it by hand.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text("x = 1\n", encoding="utf-8")
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="track")))
    extra = CheckerDefinition(
        name="honesty",
        route="edit",
        instructions="# Honesty checker",
        model="test:model",
        request_limit=5,
        output_tokens_limit=24_000,
        max_tokens=8_000,
        version=1,
    )
    settings = replace(settings, checkers={**settings.checkers, "honesty": extra})
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Write", "input": {"file_path": "src/parser.py"}, "result": "ok"}]],
        settings=settings,
        checker_models={
            **fake_models(),
            "honesty": marking_checker(
                dirty=[
                    ("/etc/passwd", "", "the file lies about itself"),
                    ("src/parser.py", "fields", "the docstring lies"),
                ]
            ),
        },
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()

    assert [m.label() for m in store.honesty_marks if m.open] == ["src/parser.py:fields"], (
        "only the mark inside the project may be recorded"
    )
    said = [entry.message for entry in rec.of(Log)]
    assert any("honesty: ignored" in message and "/etc/passwd" in message for message in said), (
        f"a dropped mark has to be visible, not silent: {said}"
    )


async def test_a_mode_switch_and_a_refreshed_reason_both_reach_the_clients(tmp_path: Path):
    """D113: what a client shows is the marks *and* the mode, so both have to be announced.

    The comparison was over mark ids alone. Switching `honesty.mode` to "off" -- which is what
    a person does to unblock a session -- therefore reached no client at all: an editor kept
    showing the marks as blocking. A reason a checker had refreshed went the same way, because
    the mark keeps its id.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text("x = 1\n", encoding="utf-8")
    settings = default_settings(tmp_path, Config(honesty=HonestyConfig(mode="enforce")))
    session, rec, store = make_session(tmp_path, [], settings=settings)
    await store.apply_honesty_marks(
        [CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies")], [], None
    )
    await session.reload_register()
    first = rec.of(HonestyUpdate)[-1]
    assert [info.reason for info in first.marks] == ["the docstring lies"]
    assert first.mode == "enforce"

    # Same mark, new reason: the id is unchanged, so an id comparison would stay silent.
    await store.apply_honesty_marks(
        [CodeMark(path="src/parser.py", symbol="fields", reason="and the name says nothing")],
        [],
        None,
    )
    await session.reload_register()
    assert [info.reason for info in rec.of(HonestyUpdate)[-1].marks] == [
        "and the name says nothing"
    ], "a refreshed reason is what Claude and the reader see; it has to be sent"

    # The mode alone changes: nothing about the marks did.
    session.config = session.config.model_copy(
        update={"honesty": session.config.honesty.model_copy(update={"mode": "off"})}
    )
    before = len(rec.of(HonestyUpdate))
    await session.reload_register()
    after = rec.of(HonestyUpdate)
    assert len(after) == before + 1, "switching the mode off has to reach the clients"
    assert after[-1].mode == "off"

    # And it is sent once, not on every reload.
    await session.reload_register()
    assert len(rec.of(HonestyUpdate)) == before + 1
