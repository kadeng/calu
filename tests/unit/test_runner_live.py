"""The SDK runner against the real `claude` CLI: whose words a user-role message is (`live`).

The other runner tests build the SDK's messages by hand. What only the real CLI can show is the
shape in which it replays a prompt, a queued message and a message injected into a running turn,
and what it puts into the user role by itself. calu's rule is fixed (`SdkRunner._user_source`):
a text counts as the user's words only when the session sent it for a human; this test checks
that the real stream lets that rule recognise all three, and nothing else.

Needs a logged-in `claude`, `bwrap` and `socat` (the sandbox is on, as in a real session). The
checkers are the rule-based fakes, so no checker API key is needed.
"""

import shutil
from pathlib import Path

import anyio
import pytest

from calu.claude.runner import NOT_FROM_USER
from calu.claude.sdk_runner import SdkRunner
from calu.config import Config
from calu.protocol.messages import ChatSendParams, ToolResult, ToolUse, TurnResult, UserInput
from calu.session import Session
from calu.store.memory import MemorySessionStore
from calu.testing import fake_models
from tests.unit.helpers import Recorder, default_settings

pytestmark = [pytest.mark.anyio, pytest.mark.live]

MARKER = "calu-live-tool-output-7391"
PROMPT = (
    f"Run exactly one Bash command, `sleep 12; echo {MARKER}`, as a single foreground Bash "
    "call. When it has finished, reply with the single word DONE and nothing else."
)
INJECTED = "One more thing for this same answer: put the word PINEAPPLE after DONE."
QUEUED = "Now reply with the single word SECOND and nothing else. Do not use any tool."
NOTE = "calu-live-note-5528: the review of this command's writes has nothing to report."


async def test_only_what_a_human_sent_is_recorded_as_user_input(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("no `claude` on PATH")
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = Config.model_validate(
        {"claude": {"model": "haiku", "stream_partial": False}, "context": {"narrate": False}}
    )
    rec = Recorder()
    session = Session(
        workspace,
        default_settings(workspace, config),
        MemorySessionStore(),
        lambda: SdkRunner(workspace, config.claude),
        rec,
        checker_models=fake_models(),
        snapshot_root=tmp_path / "snapshots",
    )

    async def note_after_command(step: object) -> str:
        """Stand for a write-set review that has a remark: calu's own text for the agent.

        Args:
            step: The command that ran (not read).

        Returns:
            The fixed note.
        """
        return NOTE

    session.review_write_set = note_after_command  # type: ignore[method-assign]

    async with anyio.create_task_group() as tg:
        await session.start(tg)
        try:
            await session.send_chat(ChatSendParams(text=PROMPT))
            await rec.wait_for(ToolUse, timeout=120)  # the command is running now
            await session.send_chat(ChatSendParams(text=INJECTED, delivery="inject"))
            await session.send_chat(ChatSendParams(text=QUEUED, delivery="queued"))
            await rec.wait_for(TurnResult, count=2, timeout=300)
        finally:
            await session.stop()

    inputs = rec.of(UserInput)
    seen = [(event.source, event.text) for event in inputs]
    human = [event.text.strip() for event in inputs if event.source == "user"]
    assert sorted(human) == sorted([PROMPT, INJECTED, QUEUED]), seen
    # The prompt and the queued message each start a turn, and Claude Code replays both with
    # the transcript uuid a fork needs (PLAN.md D36).
    by_text = {event.text.strip(): event for event in inputs}
    assert by_text[PROMPT].uuid and by_text[QUEUED].uuid, seen
    # calu's own note arrives in the user role as well, and is not the user's.
    notes = [event for event in inputs if NOTE in event.text]
    assert notes and all(event.source == NOT_FROM_USER for event in notes), seen
    # The command's output is a tool result, never a user input.
    assert any(MARKER in event.content for event in rec.of(ToolResult))
    assert not any(MARKER in event.text for event in inputs if event.text.strip() != PROMPT), seen

    recorded = [text for turn in session.context.turns for text in turn.user_inputs]
    assert sorted(text.strip() for text in recorded) == sorted([PROMPT, INJECTED, QUEUED])
    assert not any(NOTE in text for text in recorded)
