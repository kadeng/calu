"""Switching Claude Code's permission mode while a session runs (agent/records/PLAN.md D247).

The session refuses the modes calu does not offer, records every change with who asked, and
passes the mode Claude Code reports to every client. The scripted runner stands in for Claude
Code: it reports its mode at the start and after each change, as Claude Code's init and status
messages do, and records the mode every tool call ran in.
"""

from pathlib import Path

import anyio
import pytest

from calu.claude import FakeRunner
from calu.config import ClaudeConfig, Config
from calu.protocol.messages import (
    REFUSED_PERMISSION_MODES,
    ChatSendParams,
    PermissionModeChanged,
    TurnResult,
)
from calu.session import SessionError
from tests.unit.helpers import make_session

pytestmark = pytest.mark.anyio


def runner_of(session: object) -> FakeRunner:
    """Return the session's scripted runner.

    Args:
        session: A session built by `make_session` and started.

    Returns:
        Its `FakeRunner`.
    """
    runner = getattr(session, "runner", None)
    assert isinstance(runner, FakeRunner)
    return runner


async def test_a_switch_between_turns_is_applied_recorded_and_announced(tmp_path: Path) -> None:
    """The mode reaches Claude Code, the audit log and every client, and later starts keep it."""
    session, rec, store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        # The start is reported, so a client attached before it learns the mode as well.
        assert [e.mode for e in rec.of(PermissionModeChanged)] == ["auto"]
        assert session.snapshot().permission_mode == "auto"

        await session.set_permission_mode("acceptEdits", client="vscode on the laptop")
        await anyio.sleep(0.05)  # Claude Code's own report arrives after the answer
        assert runner_of(session).mode_changes == ["acceptEdits"]
        changes = [e for e in rec.of(PermissionModeChanged)]
        assert [(e.mode, e.client) for e in changes] == [
            ("auto", None),
            ("acceptEdits", "vscode on the laptop"),
        ], "Claude Code's confirmation of the same mode is not passed on a second time"
        assert store.setting_changes == [
            ("permission_mode", {"mode": "auto"}, {"mode": "acceptEdits"}, "vscode on the laptop")
        ]
        assert session.snapshot().permission_mode == "acceptEdits"
        assert session.config.claude.permission_mode == "acceptEdits"

        # Asking for the mode it already runs in changes nothing and records nothing.
        await session.set_permission_mode("acceptEdits", client="tui")
        assert runner_of(session).mode_changes == ["acceptEdits"]
        assert len(store.setting_changes) == 1
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_switch_during_a_turn_applies_from_the_next_tool_call(tmp_path: Path) -> None:
    """A call already checked keeps its mode; the next call of the same turn has the new one."""
    session, rec, _store = make_session(
        tmp_path,
        [
            [
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
                {"sleep": 0.3},
                {"tool": "Bash", "input": {"command": "ls -l"}, "result": "a"},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="list it"))
        with anyio.fail_after(5):
            while not runner_of(session).tool_modes:
                await anyio.sleep(0.01)
        await session.set_permission_mode("default", client="web")
        await rec.wait_for(TurnResult)
        assert runner_of(session).tool_modes == [("Bash", "auto"), ("Bash", "default")]
        assert rec.of(PermissionModeChanged)[-1].mode == "default"
        await session.stop()
        tg.cancel_scope.cancel()


@pytest.mark.parametrize("mode", sorted(REFUSED_PERMISSION_MODES))
async def test_the_modes_calu_does_not_offer_are_refused_with_the_reason(
    tmp_path: Path, mode: str
) -> None:
    """plan, dontAsk and bypassPermissions never reach Claude Code, and the error says why."""
    session, _rec, store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        with pytest.raises(SessionError) as refused:
            await session.set_permission_mode(mode, client="tui")
        assert REFUSED_PERMISSION_MODES[mode] in str(refused.value)
        assert "default, acceptEdits, auto" in str(refused.value)
        assert runner_of(session).mode_changes == []
        assert store.setting_changes == []
        await session.stop()
        tg.cancel_scope.cancel()


async def test_require_auto_mode_keeps_the_session_in_auto(tmp_path: Path) -> None:
    """With `claude.require_auto_mode` only "auto" may be chosen; the error names the setting."""
    config = Config(claude=ClaudeConfig(require_auto_mode=True))
    session, _rec, store = make_session(tmp_path, [], config=config)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        with pytest.raises(SessionError, match=r"claude\.require_auto_mode"):
            await session.set_permission_mode("default", client="tui")
        assert runner_of(session).mode_changes == [] and store.setting_changes == []
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_mode_claude_code_refuses_is_recorded_as_not_applied(tmp_path: Path) -> None:
    """The first audit entry is written before the change, so a refusal adds a second one."""
    session, rec, store = make_session(tmp_path, [])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        runner_of(session).permission_mode_error = "Claude Code did not change the mode: gone"
        with pytest.raises(SessionError, match="gone"):
            await session.set_permission_mode("default", client="tui")
        assert [change[1:] for change in store.setting_changes] == [
            ({"mode": "auto"}, {"mode": "default"}, "tui"),
            ({"mode": "default"}, {"mode": "auto"}, "session: Claude Code refused the change"),
        ]
        assert session.permission_mode == "auto"
        assert [e.mode for e in rec.of(PermissionModeChanged)] == ["auto"]
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_mode_claude_code_changes_by_itself_reaches_the_clients(tmp_path: Path) -> None:
    """A tool call that enters plan mode is shown as Claude Code reports it, with no client."""
    session, rec, _store = make_session(tmp_path, [[{"permission_mode": "plan"}, {"text": "ok"}]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="plan it"))
        await rec.wait_for(TurnResult)
        assert [(e.mode, e.client) for e in rec.of(PermissionModeChanged)] == [
            ("auto", None),
            ("plan", None),
        ]
        assert session.snapshot().permission_mode == "plan"
        # The switch leads out of it again.
        await session.set_permission_mode("default", client="tui")
        assert session.permission_mode == "default"
        await session.stop()
        tg.cancel_scope.cancel()


async def test_no_switch_without_a_running_session(tmp_path: Path) -> None:
    """A stopped session has no Claude Code to switch; nothing is recorded."""
    session, _rec, store = make_session(tmp_path, [])
    with pytest.raises(SessionError):
        await session.set_permission_mode("default", client="tui")
    assert store.setting_changes == []
