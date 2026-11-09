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
