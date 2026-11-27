"""The supervision level "unchecked": no checker runs, calu's rules still apply (requirement 53)."""

from pathlib import Path

import anyio
import pytest

from calu.config import AutoEscalate, ChangesConfig, Config
from calu.gate import UNCHECKED_REASON
from calu.protocol.messages import (
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    TurnResult,
    UserInput,
)
from calu.session import RESULT_UNCHECKED
from tests.unit.helpers import make_session
from tests.unit.test_result_checker import agent_turn, result_checks
from tests.unit.test_rules import run_one

pytestmark = pytest.mark.anyio

UNCHECKED = Config(auto_escalate=AutoEscalate(unchecked=True))


async def test_a_command_the_checker_would_deny_passes_without_a_checker(tmp_path: Path) -> None:
    """The fake command checker denies "calu:deny"; with the level unchecked it is not asked."""
    session, rec, store = make_session(
        tmp_path, [[{"tool": "Bash", "input": {"command": "python3 run.py  # calu:deny"}}]], UNCHECKED
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    result = rec.of(CheckResult)[0]
    assert result.decision == "PASS" and result.reason == UNCHECKED_REASON
    assert not rec.of(EscalationRequest)
    (step,) = store.steps.values()
    assert not step.checker_runs
    assert step.decision is not None and step.decision.decision == "pass"


async def test_an_edit_the_checker_would_escalate_passes(tmp_path: Path) -> None:
    """The edit checker is a checker like the others: it does not run either."""
    rec = await run_one(
        tmp_path,
        "Write",
        {"file_path": str(tmp_path / "a.py"), "content": "x = 1  # calu:escalate\n"},
        UNCHECKED,
    )
    result = rec.of(CheckResult)[0]
    assert result.route == "edit" and result.decision == "PASS"
    assert not rec.of(EscalationRequest)


async def test_a_deny_rule_still_asks_a_person(tmp_path: Path) -> None:
    """The owner: keep all rules. A match of the deny list escalates as before."""
    rec = await run_one(tmp_path, "Bash", {"command": "sudo rm -rf /"}, UNCHECKED)
    assert rec.of(EscalationRequest)


async def test_a_category_that_always_asks_still_asks(tmp_path: Path) -> None:
    """`commands` with `unchecked`: every command waits for a person, with no checker's opinion."""
    config = Config(auto_escalate=AutoEscalate(commands=True, unchecked=True))
    rec = await run_one(tmp_path, "Bash", {"command": "python3 run.py"}, config)
    reason = rec.of(EscalationRequest)[0].reason
    assert "Auto-escalation for commands is on." in reason
    assert "No checker ran" in reason


async def test_a_subagent_result_is_accepted_without_the_result_checker(tmp_path: Path) -> None:
    """The fake result checker would escalate this result; nobody reads it."""
    session, rec, store = make_session(
        tmp_path, agent_turn("Rewrite everything calu:escalate-result"), UNCHECKED
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert not rec.of(EscalationRequest)
    # The first check is the task's, passed without the subagent checker; the second the result's.
    task, check = result_checks(rec)
    assert task.decision == "PASS" and task.reason == UNCHECKED_REASON
    assert check.decision == "PASS" and check.step_summary == RESULT_UNCHECKED
    assert not [u for u in rec.of(UserInput) if "result checker" in u.text]
    reviewed = next(s for s in store.steps.values() if s.row.tool_name == "SubagentResult")
    assert not reviewed.checker_runs


async def test_the_level_set_in_a_running_session_takes_effect_at_the_next_step(
    tmp_path: Path,
) -> None:
    """A client switches the level with `auto_escalate.set`; the change is in the audit log."""
    session, rec, store = make_session(
        tmp_path, [[{"tool": "Bash", "input": {"command": "python3 run.py  # calu:deny"}}]]
    )
    await session.set_auto_escalate(AutoEscalate(unchecked=True), client="vscode")
    assert store.setting_changes[-1][2] == AutoEscalate(unchecked=True).model_dump()
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert rec.of(CheckResult)[0].reason == UNCHECKED_REASON


def test_what_a_command_wrote_is_not_reviewed(tmp_path: Path) -> None:
    """The write-set review is a check by the changeset checkers; the measuring stays."""
    config = Config(
        auto_escalate=AutoEscalate(unchecked=True),
        changes=ChangesConfig(snapshot=True, review=True),
    )
    session, _rec, _store = make_session(tmp_path, [], config)
    assert session._changes_config().review is False
    assert session._changes_config().snapshot is True
    session.config.auto_escalate = AutoEscalate()
    assert session._changes_config().review is True
