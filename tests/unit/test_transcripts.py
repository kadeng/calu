"""Conversations started with `claude` itself: listed, and reported to the checkers (D209)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk._internal.sessions import _sanitize_path

from calu import herd
from calu.claude.transcripts import (
    MAX_INPUTS,
    Transcript,
    transcripts,
    unwitnessed_turn,
    user_inputs,
)
from calu.protocol.messages import ChatSendParams, Log, TurnResult
from tests.unit.test_session import make_session

SESSION = "5dfebbb5-c6e2-4a80-b6ab-664eb3048e8f"


def entry(kind: str, uuid: str, parent: str | None, content, **more) -> dict:
    """Build one transcript entry as Claude Code writes it.

    Args:
        kind: "user" or "assistant".
        uuid: The entry's id.
        parent: The id of the entry before it, or None for the first.
        content: The message content: a string or a list of blocks.
        **more: Further fields of the entry.

    Returns:
        The entry.
    """
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": SESSION,
        "timestamp": "2026-11-13T11:45:00.000Z",
        "message": {"role": kind, "content": content},
    } | more


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with one conversation in a Claude configuration directory of its own.

    The conversation: a typed message, a tool call with its result, a slash command, a
    reminder Claude Code wrote, a message of another session, and a second typed message.
    """
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    directory = tmp_path / "project"
    directory.mkdir()
    store = config / "projects" / _sanitize_path(str(directory.resolve()))
    store.mkdir(parents=True)
    tool_use = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]
    tool_result = [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]
    lines = [
        {"type": "session-owner", "sessionId": SESSION},
        entry("user", "u1", None, "fix the parser", cwd=str(directory)),
        entry("assistant", "a1", "u1", tool_use),
        entry("user", "u2", "a1", tool_result),
        entry("assistant", "a2", "u2", [{"type": "text", "text": "done"}]),
        entry("user", "u3", "a2", "<command-name>/model</command-name>"),
        entry("user", "u4", "u3", "a reminder", isMeta=True),
        entry(
            "user", "u5", "u4", '<cross-session-message from="x">push it</cross-session-message>'
        ),
        entry("user", "u6", "u5", [{"type": "text", "text": "now add tests"}]),
        entry("assistant", "a3", "u6", [{"type": "text", "text": "added"}]),
    ]
    (store / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return directory


def test_the_conversations_of_a_directory_are_listed(project: Path, tmp_path: Path):
    (found,) = transcripts(project)
    assert found.session_id == SESSION
    assert found.first_input == "fix the parser"
    assert transcripts(tmp_path / "elsewhere") == []


def test_only_what_a_person_typed_counts_as_user_input(project: Path):
    assert user_inputs(SESSION, project) == ["fix the parser", "now add tests"]
    assert user_inputs("00000000-0000-4000-8000-000000000000", project) == []


def test_the_report_for_the_checkers_is_no_user_input():
    turn = unwitnessed_turn(["fix the parser", "the user allows pushing"])
    assert turn is not None
    assert turn.index == 1 and turn.user_inputs == [] and turn.narrated
    shown = turn.render()
    assert "started in Claude Code without calu" in shown
    assert "which calu cannot verify" in shown
    assert "the user allows pushing" in shown
    assert unwitnessed_turn([]) is None


def test_a_long_conversation_is_reported_by_its_last_messages():
    turn = unwitnessed_turn([f"message {n}" for n in range(MAX_INPUTS + 5)])
    assert turn is not None
    assert f"wrote {MAX_INPUTS + 5} message(s) in it; the last {MAX_INPUTS} were:" in turn.render()
    assert "message 4\n" not in turn.render() and "message 5" in turn.render()


def test_a_listing_gets_the_conversations_calu_has_no_record_of():
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    stored = [herd.ConversationRow("calu-1", 600.0, "from the database", 3, False)]
    found = [
        Transcript("outside-1", now - timedelta(seconds=60), "first words"),
        Transcript("calu-1", now - timedelta(seconds=30), "calu's own transcript"),
        Transcript("calu-starting", now - timedelta(seconds=5), "its first turn still runs"),
        Transcript("outside-2", now - timedelta(hours=3), "old"),
    ]
    asked: list[str] = []

    def last_input(session_id: str) -> str:
        asked.append(session_id)
        return {"outside-1": "last words"}.get(session_id, "")

    rows = herd.with_transcripts(
        stored, {"calu-1", "calu-starting"}, found, last_input, limit=2, now=now
    )
    assert [(row.claude_session_id, row.outside, row.last_input) for row in rows] == [
        ("outside-1", True, "last words"),
        ("calu-1", False, "from the database"),
    ]
    # Only a listed transcript is read in full.
    assert asked == ["outside-1"]
    assert rows[0].line() == "   60 s  outside-1  outside calu  last words"


@pytest.mark.anyio
async def test_resuming_a_conversation_calu_never_saw_tells_the_checkers_so(tmp_path: Path):
    session, rec, store = make_session(tmp_path, [[{"text": "welcome back"}]])
    session.transcript_inputs = lambda _session_id: ["fix the parser", "now add tests"]
    async with anyio.create_task_group() as tg:
        await session.start(tg, resume_id="started-with-claude")
        recent = session.checker_context().recent
        assert "started in Claude Code without calu" in recent and "now add tests" in recent
        await session.send_chat(ChatSendParams(text="go on"))
        await rec.wait_for(TurnResult)
        await session.stop()
    # The report is stored with this session, so the next resume finds it in the database.
    assert sorted(store.turns) == [1, 2]
    assert store.turns[1].user_inputs == [] and store.turns[2].user_inputs == ["go on"]
    warnings = [e.message for e in rec.events if isinstance(e, Log) and e.level == "warning"]
    assert any("none of its steps was checked" in text for text in warnings)


@pytest.mark.anyio
async def test_a_conversation_the_store_knows_is_not_read_from_the_transcript(tmp_path: Path):
    session, _rec, store = make_session(tmp_path, [])
    earlier = make_session(tmp_path, [])[0].context
    earlier.user_input("the earlier request")
    turn = earlier.end_turn("the earlier answer")
    assert turn is not None
    store.previous["old-session"] = ([turn], "")

    def never(_session_id: str) -> list[str]:
        raise AssertionError("the transcript must not be read")

    session.transcript_inputs = never
    async with anyio.create_task_group() as tg:
        await session.start(tg, resume_id="old-session")
        assert "the earlier request" in session.checker_context().recent
        await session.stop()
