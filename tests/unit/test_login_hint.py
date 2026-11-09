"""Hint after Claude Code's "Please run /login" error (Bedrock setting, one-time `claude` run)."""

from pathlib import Path

import anyio
import pytest

from calu.config import ClaudeConfig, Config
from calu.protocol.messages import ChatSendParams, Log, TurnResult
from tests.unit.helpers import make_session

pytestmark = pytest.mark.anyio

NOT_LOGGED_IN = [{"text": "Not logged in · Please run /login"}]


async def run_turns(tmp_path: Path, turns: int, config: Config | None = None) -> list[Log]:
    """Run `turns` turns in which Claude reports a missing sign-in; return the hints."""
    session, rec, _ = make_session(tmp_path, [NOT_LOGGED_IN] * turns, config)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        for i in range(turns):
            await session.send_chat(ChatSendParams(text=f"hello {i}"))
            await rec.wait_for(TurnResult, i + 1)
        await session.stop()
    return [e for e in rec.of(Log) if "not signed in" in e.message]


async def test_login_error_suggests_the_bedrock_setting_once(tmp_path: Path):
    hints = await run_turns(tmp_path, 2)
    assert len(hints) == 1
    assert hints[0].level == "warning"
    assert "claude.use_bedrock" in hints[0].message
    assert "run `claude` once" in hints[0].message


async def test_with_bedrock_on_the_hint_points_to_the_aws_credentials(tmp_path: Path):
    config = Config(claude=ClaudeConfig(use_bedrock=True))
    (hint,) = await run_turns(tmp_path, 1, config)
    assert "AWS credentials" in hint.message
    assert "claude.use_bedrock = true" not in hint.message
