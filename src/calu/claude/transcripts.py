"""Conversations that Claude Code keeps itself, read for calu (PLAN.md D209).

Claude Code stores every conversation of a directory as a transcript below its configuration
directory, whether calu started it or `claude` was run directly. calu reads them through the
Agent SDK (`list_sessions`, `get_session_messages`) for two purposes:

- `calu list-sessions` also lists the conversations of the directory that calu has no record
  of, so that one can be resumed or forked under calu;
- when such a conversation is resumed, the checkers are told what the transcript says the
  user asked for (`unwitnessed_turn`).

A transcript is a file in the user's home directory that calu did not write and cannot
verify. What is read from it is therefore handed to the checkers as a report about an earlier
conversation, never as words of the user that calu witnessed.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import get_session_messages, list_sessions

from calu.context.foreign import CROSS_SESSION_MARKER
from calu.context.turns import TurnRecord

log = logging.getLogger(__name__)

MAX_INPUTS = 30
"""How many of the user's messages of a transcript are reported to the checkers, the latest
ones; an older conversation is mentioned by its count only."""

MAX_INPUT_CHARS = 2000
"""Longest reported message; a longer one is cut and marked."""

NOT_TYPED = ("<", "[Request interrupted", "Caveat:")
"""Beginnings of user entries that Claude Code writes itself: slash commands and their output,
task notifications, reminders, the note of an interruption."""


@dataclass(frozen=True)
class Transcript:
    """One conversation Claude Code has stored for a directory."""

    session_id: str
    """The Claude session id, which `--resume` and `--fork` take."""
    last_activity: datetime
    """When the transcript was last written."""
    first_input: str
    """The first message of the user, as Claude Code reports it; "" if it reports none."""


def transcripts(directory: Path, limit: int | None = None) -> list[Transcript]:
    """List the conversations Claude Code has stored for one directory, the latest first.

    Reads files below the Claude configuration directory of the user this process runs as.

    Args:
        directory: The project directory.
        limit: At most this many; None lists all.

    Returns:
        The conversations; empty if there are none or the directory cannot be read (the
        reason is logged).
    """
    try:
        found = list_sessions(directory=str(directory), limit=limit, include_worktrees=False)
    except (OSError, ValueError) as exc:
        log.warning("listing Claude Code's conversations of %s failed: %s", directory, exc)
        return []
    return [
        Transcript(
            session_id=info.session_id,
            last_activity=datetime.fromtimestamp(info.last_modified / 1000, UTC),
            first_input=(info.first_prompt or "").strip(),
        )
        for info in found
    ]


def _typed_text(message: Any) -> str:
    """Return the text of a user entry if a person typed it, else "".

    Args:
        message: The entry's `message` value of a transcript.

    Returns:
        The text; "" for a tool result, for an entry Claude Code wrote itself (`NOT_TYPED`)
        and for a message of another Claude session.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return ""
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        return ""
    text = text.strip()
    if not text or text.startswith(NOT_TYPED) or CROSS_SESSION_MARKER.search(text):
        return ""
    return text


def user_inputs(session_id: str, directory: Path) -> list[str]:
    """Read what the user typed in a conversation, according to its transcript.

    Args:
        session_id: The Claude session id.
        directory: The project directory the conversation belongs to.

    Returns:
        The messages, oldest first; empty if there is no such transcript, it cannot be read
        (the reason is logged), or it holds no typed message.
    """
    try:
        messages = get_session_messages(session_id, directory=str(directory))
    except (OSError, ValueError) as exc:
        log.warning("reading the transcript of %s failed: %s", session_id, exc)
        return []
    return [
        text for entry in messages if entry.type == "user" and (text := _typed_text(entry.message))
    ]


def unwitnessed_turn(inputs: list[str]) -> TurnRecord | None:
    """Describe an earlier conversation that ran without calu as one turn for the checkers.

    The turn has no user input of its own: the messages are quoted inside a narration that
    says where they come from, so a checker reads them as a report and not as the user's
    words in this session.

    Args:
        inputs: What `user_inputs` returned.

    Returns:
        The turn, with the index 1; None if `inputs` is empty.
    """
    if not inputs:
        return None
    shown = inputs[-MAX_INPUTS:]
    lines = [
        "This conversation was started in Claude Code without calu and is continued here. "
        "calu saw none of it: no step of it was checked. According to Claude Code's own "
        f"transcript, which calu cannot verify, the user wrote {len(inputs)} message(s) in it"
        + (f"; the last {len(shown)} were:" if len(shown) < len(inputs) else ":"),
        "",
    ]
    for text in shown:
        if len(text) > MAX_INPUT_CHARS:
            text = text[:MAX_INPUT_CHARS] + " [...]"
        lines.extend("    " + line for line in text.splitlines())
        lines.append("")
    return TurnRecord(
        index=1,
        answer_narration="\n".join(lines).rstrip(),
        narrated=True,
        ended_at=datetime.now(UTC),
    )
