"""The tool gate under failure: outcomes of an escalation, deadlines, bookkeeping errors, the hold.

Each test names the review finding it closes (sg1 ... ga2); the commit findings (sg0, cc3) are in
`test_changeset.py` and the snapshot finding (wa1) in `test_changes.py`.
"""

from pathlib import Path
from typing import Any

import anyio
import pytest

import calu.session as session_module
from calu.changes import SnapshotError
from calu.checkers import Step
from calu.claude.runner import GateResult, HumanAnswer, Verdict
from calu.claude.sdk_runner import HOOK_TIMEOUT_S
from calu.config import ChangesConfig, Config
from calu.protocol.messages import (
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Log,
    ToolResult,
    TurnResult,
    UserInput,
)
from calu.session import SessionError
from calu.testing import fake_models
from tests.unit.helpers import make_session
from tests.unit.test_result_checker import agent_turn

pytestmark = pytest.mark.anyio


def bash(command: str, tool_use_id: str, workspace: Path, agent_id: str | None = None) -> Step:
    """Build a Bash step as the runner would pass it to the gate."""
    return Step(
        tool_name="Bash",
        tool_input={"command": command},
        tool_use_id=tool_use_id,
        agent_id=agent_id,
        cwd=str(workspace),
    )


async def wait_until(condition: Any, timeout: float = 5) -> None:
    """Wait until `condition()` is true."""
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.01)


def write_turn(content: str) -> list[list[dict[str, Any]]]:
    """A turn whose one command rewrites a.txt; the content carries the fake checker's marker."""
    return [
        [
            {
                "tool": "Bash",
                "input": {"command": "generate"},
                "writes": {"a.txt": content},
                "result": "generated",
            }
        ]
    ]


REVIEWED = Config(changes=ChangesConfig(snapshot=True, review=True))


# ------------------------------------------------------------------ sg1


async def test_a_write_set_nobody_could_be_asked_about_is_not_reverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """sg1: a failed INSERT is not a human refusing the change."""
    monkeypatch.setattr(session_module, "OPEN_ESCALATION_RETRY_S", (0.0, 0.0), raising=False)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, rec, store = make_session(tmp_path, write_turn("two # calu:escalate\n"), REVIEWED)

    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database gone")

    store.open_escalation = broken  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(TurnResult)
        assert (tmp_path / "a.txt").read_text() == "two # calu:escalate\n"
        texts = [event.text for event in rec.of(UserInput)]
        assert not [text for text in texts if "Do not simply repeat" in text], texts
        notes = [text for text in texts if "not reviewed by a human" in text]
        assert notes and "a.txt" in notes[0] and "database" in notes[0]
        assert "has **not** undone it" in notes[0] and "refused" not in notes[0]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_stopping_the_session_does_not_revert_a_write_set_under_review(tmp_path: Path):
    """sg1: the operator who stops a session to look at a change must still find the change."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, rec, _store = make_session(tmp_path, write_turn("two # calu:escalate\n"), REVIEWED)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(EscalationRequest)
        await session.stop()
        tg.cancel_scope.cancel()
    assert (tmp_path / "a.txt").read_text() == "two # calu:escalate\n"
    assert not [e for e in rec.of(Log) if "reverted" in e.message]


async def test_a_subagent_result_nobody_decided_is_not_called_a_decision_of_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """sg1: the note to the main agent must not attribute a decision that no user made."""
    monkeypatch.setattr(session_module, "OPEN_ESCALATION_RETRY_S", (0.0, 0.0), raising=False)
    session, rec, store = make_session(tmp_path, agent_turn("All done. calu:escalate-result"))

    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database gone")

    store.open_escalation = broken  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    texts = [event.text for event in rec.of(UserInput)]
    assert not [text for text in texts if "Decision of the user" in text], texts
    assert [text for text in texts if "No human decided" in text], texts


async def test_opening_an_escalation_is_retried_before_the_step_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """sg1: one database hiccup must not turn an escalation into a denial."""
    monkeypatch.setattr(session_module, "OPEN_ESCALATION_RETRY_S", (0.0, 0.0), raising=False)
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "done"}]],
    )
    original = store.open_escalation
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection reset")
        return await original(*args, **kwargs)

    store.open_escalation = flaky  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        await session.decide_escalation(request.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.content == "done" and not result.is_error


# ------------------------------------------------------------------ sg2


async def test_a_cancelled_wait_for_a_human_closes_its_row(tmp_path: Path):
    """sg2: the SDK cancels the permission callback on an interrupt; the row must not stay open."""
    session, rec, store = make_session(tmp_path, [])
    step = bash("deploy", "t1", tmp_path)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        async with anyio.create_task_group() as callback:
            callback.start_soon(session.ask, step)
            await rec.wait_for(EscalationRequest)
            callback.cancel_scope.cancel()
        (request,) = rec.of(EscalationRequest)
        row = store.escalations[int(request.escalation_id)]
        assert row.state == "cancelled" and "interrupted" in row.note
        (resolved,) = rec.of(EscalationResolved)
        assert resolved.decision == "cancelled"
        assert session.snapshot().escalations == []
        await session.stop()
        tg.cancel_scope.cancel()


# ------------------------------------------------------------------ sg3


async def test_the_checking_count_is_zero_after_a_write_set_review(tmp_path: Path):
    """sg3: the review used to lower the count once more than it raised it."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, rec, _store = make_session(tmp_path, write_turn("two\n"), REVIEWED)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="generate it"))
        await rec.wait_for(TurnResult)
        assert session._checking == 0
        async with session._checking_scope():
            assert session._derived_state() == "checking"
        assert session._checking == 0
        await session.stop()
        tg.cancel_scope.cancel()


# ------------------------------------------------------------------ sg4


def test_the_gate_answers_before_the_hook_times_out():
    """sg4: narrator plus checker deadline used to equal the hook timeout exactly."""
    assert session_module.NARRATOR_DEADLINE_S + session_module.CHECKER_DEADLINE_S < (
        session_module.GATE_BUDGET_S
    )
    assert session_module.HOLD_BUDGET_S + session_module.GATE_BUDGET_S <= HOOK_TIMEOUT_S - 120


async def test_a_check_that_does_not_finish_in_its_budget_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """sg4: one budget for narration, diff and checkers; past it a human is asked."""
    monkeypatch.setattr(session_module, "GATE_BUDGET_S", 0.2, raising=False)
    session, rec, _store = make_session(tmp_path, [])
    (checker,) = session.checkers[session_module.Route.COMMAND]

    async def hang(*args: object, **kwargs: object) -> None:
        await anyio.sleep_forever()

    checker.run = hang  # type: ignore[method-assign]
    with anyio.fail_after(5):
        result = await session.pre_tool_use(bash("deploy", "t1", tmp_path))
    assert result.verdict is Verdict.ASK
    assert "did not finish" in result.reason
    (check,) = rec.of(CheckResult)
    assert check.decision == "ESCALATE"
    assert session._checking == 0


# ------------------------------------------------------------------ ci4


async def test_a_bookkeeping_error_after_a_pass_asks_a_human(tmp_path: Path):
    """ci4: "any internal error yields ASK" has to hold after the decision too."""
    session, _rec, _store = make_session(tmp_path, [])

    def broken(record: object) -> None:
        raise RuntimeError("context is broken")

    session.context.step = broken  # type: ignore[method-assign]
    result = await session.pre_tool_use(bash("ls", "t1", tmp_path))
    assert result.verdict is Verdict.ASK
    assert "internal error" in result.reason and "context is broken" in result.reason
    assert "t1" not in session._running_tools
    # The human who is asked reads the same reason.
    assert session._pending_reasons["t1"][0] == result.reason


async def test_a_bookkeeping_error_never_weakens_a_denial(tmp_path: Path):
    """ci4: the verdict is fixed before the bookkeeping runs."""
    session, _rec, _store = make_session(tmp_path, [])

    def broken(record: object) -> None:
        raise RuntimeError("context is broken")

    session.context.step = broken  # type: ignore[method-assign]
    result = await session.pre_tool_use(bash("rm x # calu:deny", "t1", tmp_path))
    assert result.verdict is Verdict.DENY and "calu:deny" in result.reason


async def test_an_error_while_recording_the_step_asks_a_human(tmp_path: Path):
    """ci4: the code before the decision is covered as well."""
    session, _rec, _store = make_session(tmp_path, [])

    async def broken() -> str | None:
        raise RuntimeError("git is broken")

    session._git_head = broken  # type: ignore[method-assign]
    result = await session.pre_tool_use(bash("ls", "t1", tmp_path))
    assert result.verdict is Verdict.ASK and "internal error" in result.reason


# ------------------------------------------------------------------ pc1


async def test_a_client_decision_the_database_refuses_does_not_release_the_step(tmp_path: Path):
    """pc1: an operator's denial that is already in the database wins over a later client allow."""
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        store.resolve(int(request.escalation_id), "denied", "too risky")  # no notification yet
        with pytest.raises(SessionError, match="already resolved"):
            await session.decide_escalation(request.escalation_id, "allow", None, "editor")
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.is_error and "too risky" in result.content
    (row,) = store.escalations.values()
    assert row.state == "denied" and row.resolved_by == "operator"


async def test_a_client_decision_that_cannot_be_stored_leaves_the_escalation_open(tmp_path: Path):
    """pc1: the database is the record of escalated decisions, so it accepts the decision first."""
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"}]],
    )
    original = store.decide_escalation

    async def broken(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("database gone")

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        store.decide_escalation = broken  # type: ignore[method-assign]
        with pytest.raises(SessionError, match="database gone"):
            await session.decide_escalation(request.escalation_id, "allow", None)
        await anyio.sleep(0.05)
        assert not rec.of(ToolResult) and session.state == "escalated"
        assert store.escalations[int(request.escalation_id)].open
        store.decide_escalation = original  # type: ignore[method-assign]
        await session.decide_escalation(request.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.content == "deployed"


async def test_a_client_that_disconnects_after_deciding_does_not_split_the_decision(
    tmp_path: Path,
):
    """pc1: the request task is cancelled when the connection ends; row and step stay in step."""
    session, rec, store = make_session(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"}]],
    )
    original = store.decide_escalation
    entered = anyio.Event()

    async def slow(*args: Any, **kwargs: Any) -> bool:
        entered.set()
        await anyio.sleep(0.2)
        return await original(*args, **kwargs)

    store.decide_escalation = slow  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="ship it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        async with anyio.create_task_group() as connection:
            connection.start_soon(session.decide_escalation, request.escalation_id, "allow", None)
            await entered.wait()
            connection.cancel_scope.cancel()  # the client closes right after sending
        await rec.wait_for(TurnResult)
        await session.stop()
    (result,) = rec.of(ToolResult)
    assert result.content == "deployed"
    (row,) = store.escalations.values()
    assert row.state == "allowed"


# ------------------------------------------------------------------ ga2


async def test_a_new_tool_call_waits_while_an_escalation_is_open(tmp_path: Path):
    """ga2: another agent's step is not judged, let alone approved, until the human answered."""
    session, rec, _store = make_session(tmp_path, [])
    first = bash("git push # calu:escalate", "t1", tmp_path, agent_id="agent-a")
    second = bash("ls", "t2", tmp_path, agent_id="agent-b")
    results: list[GateResult] = []
    answers: list[HumanAnswer] = []

    async def gate(step: Step) -> None:
        results.append(await session.pre_tool_use(step))

    async def ask(step: Step) -> None:
        answers.append(await session.ask(step))

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        assert (await session.pre_tool_use(first)).verdict is Verdict.ASK
        tg.start_soon(ask, first)
        request = (await rec.wait_for(EscalationRequest))[0]

        tg.start_soon(gate, second)
        await anyio.sleep(0.2)
        assert not results, "the second step was judged while the escalation was open"
        assert len(rec.of(CheckResult)) == 1
        assert [
            e for e in rec.of(Log) if "waits" in e.message and request.escalation_id in e.message
        ]

        # A call that needs no check is not held: reading is not probing.
        read = Step(
            tool_name="TodoWrite", tool_input={"todos": []}, tool_use_id="t3", cwd=str(tmp_path)
        )
        with anyio.fail_after(2):
            assert (await session.pre_tool_use(read)).verdict is Verdict.PASS

        await session.decide_escalation(request.escalation_id, "deny", "no")
        await wait_until(lambda: results)
        assert results[0].verdict is Verdict.PASS
        assert answers and not answers[0].allow
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_held_call_that_outlasts_the_hold_is_checked_but_not_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """ga2: the hook cannot wait for ever, so past the hold an approval becomes a question."""
    monkeypatch.setattr(session_module, "HOLD_BUDGET_S", 0.1, raising=False)
    session, rec, _store = make_session(tmp_path, [])
    first = bash("git push # calu:escalate", "t1", tmp_path, agent_id="agent-a")
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.pre_tool_use(first)
        tg.start_soon(session.ask, first)
        request = (await rec.wait_for(EscalationRequest))[0]

        with anyio.fail_after(5):
            passed = await session.pre_tool_use(bash("ls", "t2", tmp_path, agent_id="agent-b"))
            denied = await session.pre_tool_use(
                bash("rm x # calu:deny", "t3", tmp_path, agent_id="agent-b")
            )
        assert passed.verdict is Verdict.ASK
        assert request.escalation_id in passed.reason and "PASS" in passed.reason
        assert denied.verdict is Verdict.DENY  # a denial is never turned into a question
        await session.stop()
        tg.cancel_scope.cancel()


async def test_stopping_the_session_ends_the_hold(tmp_path: Path):
    """ga2: the wait ends on stop as well as on a decision."""
    session, rec, _store = make_session(tmp_path, [])
    first = bash("git push # calu:escalate", "t1", tmp_path)
    results: list[GateResult] = []

    async def gate(step: Step) -> None:
        results.append(await session.pre_tool_use(step))

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.pre_tool_use(first)
        tg.start_soon(session.ask, first)
        await rec.wait_for(EscalationRequest)
        tg.start_soon(gate, bash("ls", "t2", tmp_path))
        await anyio.sleep(0.1)
        assert not results
        with anyio.fail_after(10):
            await session.stop()
        await wait_until(lambda: results)
        tg.cancel_scope.cancel()


async def test_the_reviews_after_a_step_are_not_held_by_an_open_escalation(tmp_path: Path):
    """ga2: a post-hoc review opens escalations itself and must never wait for another one."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, rec, _store = make_session(tmp_path, write_turn("two # calu:escalate\n"), REVIEWED)
    blocker = bash("git push # calu:escalate", "t-block", tmp_path, agent_id="agent-b")
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.pre_tool_use(blocker)
        tg.start_soon(session.ask, blocker)
        first = (await rec.wait_for(EscalationRequest))[0]
        # The scripted command is held behind the open escalation, runs once it is decided, and
        # its write-set review then escalates while nothing else is open.
        await session.send_chat(ChatSendParams(text="generate it"))
        await anyio.sleep(0.1)
        assert not rec.of(ToolResult)
        await session.decide_escalation(first.escalation_id, "deny", None)
        second = (await rec.wait_for(EscalationRequest, 2))[1]
        assert "fake checker" in second.reason
        await session.decide_escalation(second.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        assert (tmp_path / "a.txt").read_text() == "two # calu:escalate\n"
        await session.stop()
        tg.cancel_scope.cancel()


# ------------------------------------------------------------------ wa1 (session side)


async def test_a_write_set_that_cannot_be_measured_is_escalated_not_skipped(tmp_path: Path):
    """wa1: with the review on, "no patch" must reach a human instead of counting as reviewed."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    session, rec, _store = make_session(
        tmp_path, write_turn("two\n"), REVIEWED, checker_models=fake_models()
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        assert session._snapshots is not None

        async def broken(before: str, after: str) -> None:
            raise SnapshotError("the patch leaves out 1 changed path(s): payload.py")

        session._snapshots.changes = broken  # type: ignore[method-assign,assignment]
        await session.send_chat(ChatSendParams(text="generate it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "could not be measured" in request.reason and "payload.py" in request.reason
        await session.decide_escalation(request.escalation_id, "allow", None)
        await rec.wait_for(TurnResult)
        await session.stop()
        tg.cancel_scope.cancel()
