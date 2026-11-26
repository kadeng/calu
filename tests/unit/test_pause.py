"""The user's pause (`pause.set`, `calu.pause`): approved checked calls wait while it lasts.

The owner, 2026-11-26: a pause button next to the stop button gives time to review; it holds
the next edit, command or subagent start of every agent, and ends when clicked again or after
10 minutes.
"""

from pathlib import Path

import anyio
import pytest

import calu.session as session_module
from calu.checkers import Step
from calu.claude.runner import GateResult, Verdict
from calu.config import Config
from calu.protocol.messages import Log, PauseChanged, StepHeld
from calu.session import PAUSE_HOOK_MARGIN_S
from tests.unit.helpers import make_session
from tests.unit.test_gate import bash, wait_until

pytestmark = pytest.mark.anyio


async def test_a_paused_session_holds_an_approved_call_of_any_agent_until_resumed(
    tmp_path: Path,
):
    session, rec, _store = make_session(tmp_path, [])
    results: list[GateResult] = []

    async def gate(step: Step) -> None:
        results.append(await session.pre_tool_use(step))

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        assert session.snapshot().pause.paused is False
        info = await session.set_pause(True, client="pane")
        assert info.paused and info.until is not None
        assert session.snapshot().pause == info
        (changed,) = rec.of(PauseChanged)
        assert changed.paused and changed.client == "pane"

        tg.start_soon(gate, bash("ls", "t1", tmp_path))
        tg.start_soon(gate, bash("pwd", "t2", tmp_path, agent_id="agent-a"))
        await wait_until(lambda: len([e for e in rec.of(StepHeld) if e.held]) == 2)
        await anyio.sleep(0.1)
        assert not results, "an approved call ran during the pause"
        held = [e for e in rec.of(StepHeld) if e.held]
        assert {e.reason for e in held} == {"the user's pause"}
        assert [e for e in rec.of(Log) if "paused" in e.message and "waits" in e.message]

        # A call that needs no check is not held, and a denial is answered at once.
        read = Step(
            tool_name="TodoWrite", tool_input={"todos": []}, tool_use_id="t3", cwd=str(tmp_path)
        )
        with anyio.fail_after(2):
            assert (await session.pre_tool_use(read)).verdict is Verdict.PASS
            denied = await session.pre_tool_use(bash("rm -rf x # calu:deny", "t4", tmp_path))
        assert denied.verdict is Verdict.DENY

        await session.set_pause(False, client="pane")
        await wait_until(lambda: len(results) == 2)
        assert {r.verdict for r in results} == {Verdict.PASS}
        assert [(e.paused, e.client) for e in rec.of(PauseChanged)][-1] == (False, "pane")
        assert session.snapshot().pause.paused is False
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_pause_ends_by_itself_after_its_time(tmp_path: Path):
    session, rec, _store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.set_pause(True, minutes=0.005)  # 0.3 s
        with anyio.fail_after(3):
            result = await session.pre_tool_use(bash("ls", "t1", tmp_path))
        assert result.verdict is Verdict.PASS
        assert rec.of(PauseChanged)[-1].paused is False
        assert rec.of(PauseChanged)[-1].client == ""  # the time ran out, nobody resumed
        assert session.snapshot().pause.paused is False
        await session.stop()
        tg.cancel_scope.cancel()


async def test_the_length_is_bounded_by_the_setting(tmp_path: Path):
    session, _rec, _store = make_session(tmp_path, [], Config(pause_timeout_s=60))
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.set_pause(True, minutes=30)
        assert 0 < session.pause.remaining() <= 60
        await session.set_pause(True)  # the default is the setting
        assert 59 < session.pause.remaining() <= 60
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_held_call_goes_on_before_the_hook_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The wait runs inside the PreToolUse hook: it never outlasts the hook's time."""
    hook = session_module.LOCK_HOOK_MARGIN_S + PAUSE_HOOK_MARGIN_S + 0.5
    monkeypatch.setattr(session_module, "HOOK_TIMEOUT_S", hook)
    session, _rec, _store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.set_pause(True)
        with anyio.fail_after(5):
            result = await session.pre_tool_use(bash("ls", "t1", tmp_path))
        assert result.verdict is Verdict.PASS
        assert session.pause.remaining() > 0  # the pause itself goes on
        await session.stop()
        tg.cancel_scope.cancel()
