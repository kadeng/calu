import psycopg
import pytest

from calu.comments import Comment, Excerpt
from calu.context.turns import StepRecord, TurnRecord
from calu.db import Db, connect
from calu.setup import setup
from calu.store import (
    CheckerRun,
    DatabaseSessionStore,
    SessionIdentity,
    StepDecision,
    StepRow,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(
    hostname="box",
    os_user="u",
    directory="/w",
    pid=4321,
    os="Linux",
    git_commit="abc123",
    model="opus",
)


async def session_store(db_url: str) -> tuple[DatabaseSessionStore, psycopg.AsyncConnection]:
    """Set up a database and return a store on a `calu_session` connection."""
    owner = await connect(db_url)
    try:
        _, session_url = await setup(owner, db_url, "test:model")
    finally:
        await owner.close()
    conn = await connect(session_url)
    return DatabaseSessionStore(Db(conn)), conn


async def test_session_row_records_identity_states_and_end(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        session_id = await store.start(IDENTITY)
        await store.set_claude_session("claude-1", "sonnet")
        await store.set_state("running")
        cur = await conn.execute(
            "SELECT s.state, s.os_user, s.directory, s.pid, s.git_commit, s.model,"
            " s.claude_session_id, h.hostname, h.os, s.started_by"
            " FROM sessions s JOIN hosts h ON h.id = s.host_id WHERE s.id = %s",
            (session_id,),
        )
        assert await cur.fetchone() == (
            "running",
            "u",
            "/w",
            4321,
            "abc123",
            "sonnet",
            "claude-1",
            "box",
            "Linux",
            "calu_session",
        )
        await store.end("failed")
        cur = await conn.execute(
            "SELECT state, ended_at IS NOT NULL FROM sessions WHERE id = %s", (session_id,)
        )
        assert await cur.fetchone() == ("failed", True)
    finally:
        await conn.close()


async def test_step_checker_runs_and_result_are_recorded(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        step_id = await store.add_step(
            StepRow(
                seq=1,
                tool_name="Bash",
                tool_input={"command": "ls"},
                turn=1,
                agent_id="a1",
                tool_use_id="t1",
                git_commit="abc123",
            )
        )
        await store.add_checker_run(
            step_id,
            CheckerRun(
                checker_name="command",
                checker_version=1,
                model="test:model",
                decision="allow",
                reason="harmless",
                input_tokens=10,
                output_tokens=3,
                duration_ms=42,
            ),
        )
        await store.decide_step(
            step_id,
            StepDecision(
                decision="allow",
                reason="harmless",
                summary="Lists the directory.",
                narration="Claude said it would look around.",
                rules_outcome="unlisted",
            ),
        )
        await store.finish_step(step_id, "The directory has three files.")
        cur = await conn.execute(
            "SELECT seq, turn, agent_id, tool_use_id, tool_name, tool_input, decision, reason,"
            " summary, narration, rules_outcome, result_summary,"
            " decided_at IS NOT NULL, finished_at IS NOT NULL FROM steps WHERE id = %s",
            (step_id,),
        )
        assert await cur.fetchone() == (
            1,
            1,
            "a1",
            "t1",
            "Bash",
            {"command": "ls"},
            "allow",
            "harmless",
            "Lists the directory.",
            "Claude said it would look around.",
            "unlisted",
            "The directory has three files.",
            True,
            True,
        )
        cur = await conn.execute(
            "SELECT checker_name, checker_version, model, decision, input_tokens, duration_ms"
            " FROM checker_runs WHERE step_id = %s",
            (step_id,),
        )
        assert await cur.fetchone() == ("command", 1, "test:model", "allow", 10, 42)
    finally:
        await conn.close()


async def test_turns_and_summary_survive_a_resume(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-1")
        first = TurnRecord(
            index=1,
            user_inputs=["fix the typo"],
            steps=[StepRecord(tool_name="Edit", decision="allow", summary="Fixes a typo.")],
            answer="done",
            answer_narration="Claude said it was done.",
            narrated=True,
        )
        await store.save_turn(first)
        await store.save_turn(TurnRecord(index=2, user_inputs=["and the next one"]))
        await store.save_turn(first.model_copy(update={"answer": "really done"}))
        await store.save_summary("# Chat summary\n\nEarlier turns.\n", 1)
        await store.mark_folded(1)
        cur = await conn.execute(
            "SELECT number, folded, answer FROM turns WHERE session_id = %s ORDER BY number",
            (store.session_id,),
        )
        assert await cur.fetchall() == [(1, True, "really done"), (2, False, "")]

        resumed = DatabaseSessionStore(Db(conn))
        await resumed.start(IDENTITY)
        turns, summary = await resumed.load_previous("claude-1")
        assert [t.index for t in turns] == [2]  # turn 1 is folded into the summary
        assert summary == "# Chat summary\n\nEarlier turns.\n"
        assert await resumed.load_previous("claude-unknown") == ([], "")
    finally:
        await conn.close()


async def test_turn_records_round_trip(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-2")
        turn = TurnRecord(
            index=7,
            user_inputs=["a", "b"],
            steps=[
                StepRecord(
                    tool_name="Bash",
                    agent_id="a1",
                    decision="escalate",
                    summary="Removes a directory.",
                    comment="Be careful.",
                    narration="Claude said it would clean up.",
                )
            ],
            answer="verbatim answer",
            answer_narration="Claude reported success.",
            narrated=True,
        )
        await store.save_turn(turn)
        other = DatabaseSessionStore(Db(conn))
        await other.start(IDENTITY)
        turns, _ = await other.load_previous("claude-2")
    finally:
        await conn.close()
    assert len(turns) == 1
    assert turns[0].model_dump(exclude={"started_at", "ended_at"}) == turn.model_dump(
        exclude={"started_at", "ended_at"}
    )


async def test_the_session_waits_for_an_operator_and_may_only_cancel(db_url: str) -> None:
    owner = await connect(db_url)
    _, session_url = await setup(owner, db_url, "test:model")
    conn = await connect(session_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        await store.start(IDENTITY)
        step_id = await store.add_step(StepRow(seq=1, tool_name="Bash", tool_input={}))
        first = await store.open_escalation("why", {"rendered": "rm -rf /"}, step_id)
        assert (await store.escalation(first)).open
        # The session role cannot decide; only an operator's function call can.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(f"SELECT calu_resolve_escalation({first}, 'allowed', 'x')".encode())
        cur = await owner.execute(
            "SELECT calu_resolve_escalation(%s, 'allowed', 'go ahead')", (first,)
        )
        assert await cur.fetchone() == (True,)
        resolved = await store.escalation(first)
        assert resolved.allowed and resolved.note == "go ahead"
        assert resolved.resolved_by == "calu_owner"

        second = await store.open_escalation("why again", {}, step_id)
        await store.close_escalation(second, "the session was stopped")
        cancelled = await store.escalation(second)
        assert cancelled.state == "cancelled" and not cancelled.allowed
        assert cancelled.note == "the session was stopped"
        await store.close_escalation(first, "too late")  # already resolved: unchanged
        assert (await store.escalation(first)).note == "go ahead"
        assert (await store.escalation(999_999)).state == "cancelled"
    finally:
        await conn.close()
        await owner.close()


async def test_a_client_of_the_session_decides_with_its_token(db_url: str) -> None:
    owner = await connect(db_url)
    _, session_url = await setup(owner, db_url, "test:model")
    conn = await connect(session_url)
    store = DatabaseSessionStore(Db(conn))
    try:
        await store.start(IDENTITY)
        step = await store.add_step(StepRow(seq=1, tool_name="Bash", tool_input={}))
        before_token = await store.open_escalation("why", {}, step)
        # Without a client token the session cannot decide: nothing claims to be its client.
        assert await store.decide_escalation(before_token, True, "go", "test") is False
        assert (await store.escalation(before_token)).open

        await store.add_client_token("a" * 64, "ws://127.0.0.1:1")
        assert await store.decide_escalation(before_token, True, "go ahead", "attach") is True
        decided = await store.escalation(before_token)
        assert decided.allowed and decided.note == "go ahead"
        assert decided.resolved_by == "client:attach"
        # Only the first decision counts.
        assert await store.decide_escalation(before_token, False, "no", "attach") is False

        denied = await store.open_escalation("another", {}, step)
        assert await store.decide_escalation(denied, False, "not today", "ide") is True
        row = await store.escalation(denied)
        assert row.state == "denied" and row.resolved_by == "client:ide"

        # The role cannot read the token hashes back: they are a capability, not data.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(b"SELECT token_sha256 FROM client_tokens")
        # An escalation of another session is refused, even with this session's token.
        other = DatabaseSessionStore(Db(conn))
        await other.start(IDENTITY)
        elsewhere = await other.open_escalation("not ours", {}, None)
        assert await store.decide_escalation(elsewhere, True, "", "attach") is False
        assert (await store.escalation(elsewhere)).open
    finally:
        await conn.close()
        await owner.close()


async def test_comments_and_recent_sessions(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-9")
        await store.save_turn(TurnRecord(index=1, user_inputs=["the first request", "more"]))
        comment = Comment(
            kind="diff",
            path="src/a.py",
            before=Excerpt(start_line=2, end_line=2, text="old"),
            after=Excerpt(start_line=2, end_line=2, text="new"),
            text="Why upper case?",
            ref="HEAD",
        )
        comment_id = await store.add_comment(comment, turn=1)
        cur = await conn.execute(
            "SELECT kind, path, start_line, end_line, ref, comment, content FROM comments"
            " WHERE id = %s",
            (comment_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[:6] == ("diff", "src/a.py", 2, 2, "HEAD", "Why upper case?")
        assert Comment.model_validate(row[6]) == comment

        sessions = await store.recent_sessions("/w")
        assert len(sessions) == 1
        assert sessions[0].calu_session_id == store.session_id
        assert sessions[0].claude_session_id == "claude-9"
        assert sessions[0].first_input == "the first request"
        assert sessions[0].state == "starting" and sessions[0].ended_at is None
        assert await store.recent_sessions("/elsewhere") == []
    finally:
        await conn.close()


async def test_the_history_of_a_session_comes_back_from_the_database(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-h")
        first = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "ls"}, turn=1)
        )
        await store.decide_step(
            first, StepDecision(decision="pass", summary="Lists the directory.")
        )
        await store.finish_step(first, "a.py")
        second = await store.add_step(
            StepRow(seq=2, tool_name="Bash", tool_input={"command": "deploy"}, turn=1)
        )
        await store.decide_step(
            second, StepDecision(decision="escalate", reason="needs a human", summary="Deploys.")
        )
        await store.save_turn(
            TurnRecord(
                index=1,
                user_inputs=["have a look"],
                steps=[StepRecord(tool_name="Bash", decision="pass", summary="Lists.")],
                answer="done",
            )
        )
        await store.save_summary("# Chat summary\n\nEarlier work.\n", 1)

        history = await store.load_history()
        assert history.calu_session_id == store.session_id
        assert history.claude_session_id == "claude-h"
        assert history.chat_summary == "# Chat summary\n\nEarlier work.\n"
        assert [t.index for t in history.turns] == [1]
        assert history.turns[0].user_inputs == ["have a look"]
        assert [(s.seq, s.tool_name, s.decision) for s in history.steps] == [
            (1, "Bash", "pass"),
            (2, "Bash", "escalate"),
        ]
        assert history.steps[0].tool_input == {"command": "ls"}
        assert history.steps[0].result_summary == "a.py"
        assert history.steps[1].reason == "needs a human"
        assert history.steps[0].created_at is not None

        # The newest steps win when the limit cuts, and they stay oldest first.
        cut = await store.load_history(limit=1)
        assert [s.seq for s in cut.steps] == [2]

        # Another session reads it by Claude's session id; an unknown id gives empty fields.
        other = DatabaseSessionStore(Db(conn))
        await other.start(IDENTITY)
        by_id = await other.load_history("claude-h")
        assert by_id.calu_session_id == store.session_id
        assert [s.seq for s in by_id.steps] == [1, 2]
        empty = await other.load_history("claude-unknown")
        assert empty.turns == [] and empty.steps == [] and empty.chat_summary == ""
        assert empty.claude_session_id == "claude-unknown"
    finally:
        await conn.close()


async def test_the_stored_context_of_an_escalation_comes_back_with_its_checker_runs(
    db_url: str,
) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(seq=1, tool_name="Edit", tool_input={"file_path": "a.py"})
        )
        await store.add_checker_run(
            step,
            CheckerRun(
                checker_name="edit",
                checker_version=3,
                model="test:model",
                decision="escalate",
                reason="the contract does not match the code",
                input_tokens=120,
                duration_ms=900,
            ),
        )
        escalation = await store.open_escalation(
            "the contract does not match the code",
            {
                "rendered": "```diff\n-old\n+new\n```",
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py"},
                "agent_id": None,
                "files": {"a.py": "old\n"},
                "recent": "## Turn 1\n\n**User:**\n\n    fix it",
                "chat_summary": "# Chat summary\n\nEarlier work.\n",
            },
            step,
        )
        found = await store.escalation_context(escalation)
        assert found is not None
        assert found.state == "open" and found.escalation_id == str(escalation)
        assert found.reason == "the contract does not match the code"
        assert found.rendered.startswith("```diff")
        assert found.files == {"a.py": "old\n"}
        assert "fix it" in found.recent and "Earlier work." in found.chat_summary
        assert [(r.checker_name, r.checker_version, r.decision) for r in found.checker_runs] == [
            ("edit", 3, "escalate")
        ]
        assert found.checker_runs[0].input_tokens == 120
        assert found.checker_runs[0].duration_ms == 900

        await store.close_escalation(escalation, "the session was stopped")
        closed = await store.escalation_context(escalation)
        assert closed is not None and closed.state == "cancelled"
        assert closed.note == "the session was stopped"
        assert closed.resolved_by == "calu_session"
        assert await store.escalation_context(999_999) is None
    finally:
        await conn.close()
