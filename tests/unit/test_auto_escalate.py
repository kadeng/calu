"""Auto-escalation mode: categories of steps that always wait for the human."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.config import Config
from calu.protocol.messages import (
    AutoEscalate,
    ChatSendParams,
    EscalationRequest,
    ToolResult,
    TurnResult,
)
from calu.session import SessionError
from tests.unit.helpers import Recorder, make_session
from tests.unit.test_result_checker import agent_turn

pytestmark = pytest.mark.anyio


async def run_turn(
    tmp_path: Path, turns: Sequence[Sequence[dict[str, Any]]], auto: AutoEscalate
) -> Recorder:
    """Run one turn and allow every escalation it raises."""
    session, rec, _ = make_session(tmp_path, turns, Config(auto_escalate=auto))
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        with anyio.fail_after(10):
            while not rec.of(TurnResult):
                for esc in rec.of(EscalationRequest):
                    try:
                        await session.decide_escalation(esc.escalation_id, "allow", None)
                    except SessionError:
                        pass
                await anyio.sleep(0.02)
        await session.stop()
    return rec


async def test_commands_auto_escalate_with_the_checker_opinion(tmp_path: Path):
    rec = await run_turn(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "ls"}, "result": "a"}]],
        AutoEscalate(commands=True),
    )
    (esc,) = rec.of(EscalationRequest)
    assert "Auto-escalation for commands" in esc.reason and "PASS" in esc.reason
    assert rec.of(ToolResult)[0].content == "a"


async def test_only_the_enabled_category_escalates(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    turn = [
        {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
        {
            "tool": "Edit",
            "input": {"file_path": "a.txt", "old_string": "x", "new_string": "y"},
            "result": "ok",
        },
        {"tool": "Read", "input": {"file_path": "a.txt"}, "result": "y"},
    ]
    rec = await run_turn(tmp_path, [turn], AutoEscalate(edits=True))
    (esc,) = rec.of(EscalationRequest)
    assert esc.tool_name == "Edit" and "Auto-escalation for edits" in esc.reason


async def test_subagent_results_auto_escalate(tmp_path: Path):
    rec = await run_turn(tmp_path, agent_turn("All good."), AutoEscalate(subagents=True))
    (esc,) = rec.of(EscalationRequest)
    assert esc.tool_name == "SubagentResult"
    assert "Auto-escalation for subagent results" in esc.reason


async def test_auto_escalation_is_off_by_default(tmp_path: Path):
    rec = await run_turn(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "ls"}, "result": "a"}]],
        AutoEscalate(),
    )
    assert not rec.of(EscalationRequest)


async def test_a_checker_deny_stays_a_deny(tmp_path: Path):
    rec = await run_turn(
        tmp_path,
        [[{"tool": "Bash", "input": {"command": "rm x # calu:deny"}, "result": "never"}]],
        AutoEscalate(commands=True),
    )
    assert not rec.of(EscalationRequest)
    assert rec.of(ToolResult)[0].is_error
