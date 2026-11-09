"""The session orchestrator: turns, checked tool calls, escalations, state and store records."""

from pathlib import Path

import anyio
import pytest

from calu.checkers import Decision, Route, Step
from calu.claude import FakeRunner
from calu.claude.runner import Fork, HumanAnswer
from calu.clock import ClockJump
from calu.config import ChangesConfig, ClaudeConfig, Config, ContextConfig
from calu.protocol.messages import (
    AgentsUpdate,
    ChatSendParams,
    CheckResult,
    CommentAddParams,
    EditDiff,
    EscalationRequest,
    EscalationResolved,
    Log,
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
