import psycopg
import pytest

from calu.comments import Comment, Excerpt
from calu.context.turns import StepRecord, TurnRecord
from calu.db import Db, connect
from calu.protocol.messages import AgentInfo
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


async def test_the_address_and_the_agent_tree_are_recorded(db_url: str) -> None:
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_address("ws://127.0.0.1:8765")
        cur = await conn.execute(
            "SELECT address, pid, os_user FROM sessions WHERE id = %s", (store.session_id,)
        )
        assert await cur.fetchone() == ("ws://127.0.0.1:8765", 4321, "u")

        # The main agent starts "reviewer", which starts "helper".
        await store.add_agent("tool-1", None)
        await store.add_agent("tool-2", "tool-1")
        await store.update_agents(
            [
                AgentInfo(
                    agent_id="tool-1",
                    sdk_agent_id="sdk-1",
                    description="reviewer",
                    subagent_type="general-purpose",
                    background=True,
                ),
                AgentInfo(agent_id="tool-2", description="helper", status="completed"),
            ]
        )
        await store.add_agent("tool-2", None)  # a later call keeps the first parent
        cur = await conn.execute(
            "SELECT agent_id, parent_agent_id, sdk_agent_id, description, subagent_type,"
            " background, status, ended_at IS NULL FROM agents WHERE session_id = %s"
            " ORDER BY agent_id",
            (store.session_id,),
        )
        assert await cur.fetchall() == [
            ("tool-1", None, "sdk-1", "reviewer", "general-purpose", True, "running", True),
            ("tool-2", "tool-1", None, "helper", None, False, "completed", False),
        ]
    finally:
        await conn.close()


async def test_the_write_set_of_a_step_is_recorded(db_url: str) -> None:
    """D31: the two snapshot trees and the paths a command wrote, with the undeclared ones."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        store = DatabaseSessionStore(Db(conn))
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "format"})
        )
        await store.record_changes(
            step, "tree-before", "tree-after", ["a.py", "sneaky.txt"], ["sneaky.txt"]
        )
        cur = await conn.execute(
            "SELECT snapshot_before, snapshot_after, wrote FROM steps WHERE id = %s", (step,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "tree-before" and row[1] == "tree-after"
        assert row[2] == {
            "paths": ["a.py", "sneaky.txt"],
            "undeclared": ["sneaky.txt"],
            "overlapping": False,
        }
        # A second call replaces the record, and overlapping windows are marked as such.
        await store.record_changes(step, "t1", "t2", ["a.py"], [], overlapping=True)
        cur = await conn.execute("SELECT wrote FROM steps WHERE id = %s", (step,))
        again = await cur.fetchone()
        assert again is not None and again[0]["overlapping"] is True
    finally:
        await conn.close()


async def test_a_second_fold_after_a_resume_keeps_every_turn_exactly_once(db_url: str) -> None:
    """The invariant the checkers' context rests on, across two sessions.

    A turn must reach the checkers either folded into the summary or verbatim, never both and
    never neither: seeing it twice inflates the prompt the fold exists to bound (D63 measures
    what that costs), and seeing it nowhere loses the conversation. The interesting case is the
    second fold, because `load_previous` filters on `folded` of the session it loads from while
    `mark_folded` writes the flag on the session that folds.
    """
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-9")
        for i in (1, 2, 3):
            await store.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))
        await store.save_summary("covers 1-2", 2)
        await store.mark_folded(2)

        resumed = DatabaseSessionStore(Db(conn))
        await resumed.start(IDENTITY)
        await resumed.set_claude_session("claude-9")
        turns, summary = await resumed.load_previous("claude-9")
        assert [t.index for t in turns] == [3]
        assert summary == "covers 1-2"

        # The resumed session continues the numbering and folds again, this time over a turn it
        # restored (3) and one of its own (4).
        for i in (4, 5):
            await resumed.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))
        await resumed.save_summary("covers 1-4", 4)
        await resumed.mark_folded(4)

        again = DatabaseSessionStore(Db(conn))
        await again.start(IDENTITY)
        await again.set_claude_session("claude-9")
        later, summary = await again.load_previous("claude-9")
        assert summary == "covers 1-4"
        # Turn 3 is in the summary now, so it must not come back verbatim as well; turn 5 is
        # not in the summary, so it must.
        assert [t.index for t in later] == [5], "a folded turn came back verbatim"
        covered = 4
        assert covered + len(later) == 5, "every turn is either folded or verbatim, exactly once"
    finally:
        await conn.close()


async def test_a_second_resume_keeps_the_summary_and_the_turns_between(db_url: str) -> None:
    """D65: the conversation is read from every session of it, not from the newest one.

    A resume saves only the turns it runs itself, and writes a `chat_summaries` row only when
    it folds. Reading one session therefore lost everything older as soon as a resume ended
    without a fold: the newest session had turns and no summary, so the checkers started from
    nothing and the turns recorded by the session before it were never read again.
    """
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-7")
        for i in (1, 2, 3):
            await store.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))
        await store.save_summary("covers 1-2", 2)
        await store.mark_folded(2)

        second = DatabaseSessionStore(Db(conn))
        await second.start(IDENTITY)
        await second.set_claude_session("claude-7")
        turns, summary = await second.load_previous("claude-7")
        assert ([t.index for t in turns], summary) == ([3], "covers 1-2")
        # It runs two turns and never folds, which is the normal case: the default is to fold
        # every 30 turns.
        for i in (4, 5):
            await second.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))

        third = DatabaseSessionStore(Db(conn))
        await third.start(IDENTITY)
        await third.set_claude_session("claude-7")
        turns, summary = await third.load_previous("claude-7")
        assert summary == "covers 1-2", "the summary of the fold two sessions ago was lost"
        assert [t.index for t in turns] == [3, 4, 5], "turn 3 was recorded by the first session"
        assert [t.user_inputs for t in turns] == [["turn 3"], ["turn 4"], ["turn 5"]]

        # A later fold, by whichever session, bounds what comes back verbatim: turn 3 stays
        # unmarked in the session that recorded it, so the number decides, not the flag.
        await third.save_summary("covers 1-4", 4)
        await third.mark_folded(4)
        fourth = DatabaseSessionStore(Db(conn))
        await fourth.start(IDENTITY)
        await fourth.set_claude_session("claude-7")
        turns, summary = await fourth.load_previous("claude-7")
        assert summary == "covers 1-4"
        assert [t.index for t in turns] == [5], "a folded turn came back verbatim as well"
    finally:
        await conn.close()


async def test_a_replay_covers_the_conversation_but_keeps_one_session_of_steps(
    db_url: str,
) -> None:
    """D65: `session/load` must replay the whole conversation, which a resume splits.

    The steps deliberately stay those of the session being loaded: a step's `seq` starts again
    at 1 in every session, and `seq` is how a client identifies a step, so merging them would
    hand it two different steps under one number.
    """
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-8")
        for i in (1, 2):
            await store.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))
        first_step = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "first"})
        )
        await store.decide_step(first_step, StepDecision(decision="allow", reason="r"))
        await store.save_summary("covers 1", 1)
        await store.mark_folded(1)

        resumed = DatabaseSessionStore(Db(conn))
        await resumed.start(IDENTITY)
        await resumed.set_claude_session("claude-8")
        await resumed.save_turn(TurnRecord(index=3, user_inputs=["turn 3"]))
        await resumed.add_step(StepRow(seq=1, tool_name="Bash", tool_input={"command": "again"}))

        history = await resumed.load_history("claude-8")
        # The replay is for a person, so it shows the folded turn as well, and the summary of
        # the fold that the other session wrote.
        assert [t.index for t in history.turns] == [1, 2, 3]
        assert history.chat_summary == "covers 1"
        # One step per seq, from the session that is being loaded.
        assert [s.tool_input["command"] for s in history.steps] == ["again"]
        assert [s.seq for s in history.steps] == [1]
    finally:
        await conn.close()


async def test_a_corrected_summary_survives_the_next_resume(db_url: str) -> None:
    """D40 through D65: the row the session reads is the row an operator corrects.

    A resuming session adopts the summary it inherited into its own row. Without that, an
    operator's correction was stored for the running session with `covered_turns = 0` and lost
    against the row it was copied from as soon as the session was resumed again.
    """
    store, conn = await session_store(db_url)
    try:
        await store.start(IDENTITY)
        await store.set_claude_session("claude-6")
        for i in (1, 2):
            await store.save_turn(TurnRecord(index=i, user_inputs=[f"turn {i}"]))
        await store.save_summary("the summarizer's version", 1)
        await store.mark_folded(1)

        second = DatabaseSessionStore(Db(conn))
        await second.start(IDENTITY)
        await second.set_claude_session("claude-6")
        _, summary = await second.load_previous("claude-6")
        assert summary == "the summarizer's version"
        # Adopted, so the session's own read finds it: before, this was "" and an operator
        # looking at the running session saw no summary at all.
        assert await second.load_summary() == "the summarizer's version"

        # The operator corrects it, as `calu summary --edit` does.
        await conn.execute(
            "UPDATE chat_summaries SET summary = %s WHERE session_id = %s",
            ("the corrected version", second.session_id),
        )
        assert await second.load_summary() == "the corrected version"

        third = DatabaseSessionStore(Db(conn))
        await third.start(IDENTITY)
        await third.set_claude_session("claude-6")
        _, summary = await third.load_previous("claude-6")
        assert summary == "the corrected version", "the correction was lost on the next resume"
    finally:
        await conn.close()
