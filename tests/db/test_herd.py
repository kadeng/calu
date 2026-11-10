"""Herd management on the database alone: list sessions, list and resolve escalations."""

import pytest

from calu import herd
from calu.checkers import Decision
from calu.db import Db, connect
from calu.evals import cases_from_escalations
from calu.setup import setup
from calu.store import CheckerRun, DatabaseSessionStore, SessionIdentity, StepRow
from calu.tokens import token_hash

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(hostname="box", os_user="u", directory="/w", pid=7, os="Linux")


async def test_sessions_escalations_and_resolution(db_url: str) -> None:
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("escalated")
        step = await store.add_step(StepRow(seq=1, tool_name="Bash", tool_input={"command": "x"}))
        first = await store.open_escalation("needs a decision", {"rendered": "`rm -rf /`"}, step)
        second = await store.open_escalation("another one", {}, step)

        rows = await herd.sessions(db)
        assert len(rows) == 1
        assert rows[0].id == session_id and rows[0].state == "escalated"
        assert rows[0].open_escalations == 2
        assert "box:/w" in rows[0].line() and "escalated (2 open)" in rows[0].line()

        open_rows = await herd.escalations(db)
        assert [r.id for r in open_rows] == [first, second]
        assert open_rows[0].rendered == "`rm -rf /`"
        assert "needs a decision" in open_rows[0].line()
        # Filtering by session gives the same escalations. Compared by id, not by row: a row
        # carries how long the session has been silent (D56), which the database measures
        # afresh on every read.
        assert [r.id for r in await herd.escalations(db, session_id)] == [r.id for r in open_rows]

        assert await herd.resolve(db, first, "allow", "go ahead") is True
        assert await herd.resolve(db, first, "deny") is False  # already resolved
        assert [r.id for r in await herd.escalations(db)] == [second]
        resolved = await store.escalation(first)
        assert resolved.allowed and resolved.note == "go ahead"

        with pytest.raises(ValueError):
            await herd.resolve(db, second, "maybe")

        await store.end("stopped")
        assert await herd.sessions(db) == []
        assert len(await herd.sessions(db, active_only=False)) == 1
    finally:
        await owner.close()


async def test_resolved_escalations_become_evaluation_cases(db_url: str) -> None:
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        # A step the command checker escalated and a human then allowed becomes a case.
        checked = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "deploy"})
        )
        await store.add_checker_run(
            checked,
            CheckerRun(
                checker_name="command",
                checker_version=1,
                model="test:model",
                decision="escalate",
            ),
        )
        first = await store.open_escalation(
            "why", {"tool_input": {"command": "deploy"}, "files": {}}, checked
        )
        await herd.resolve(db, first, "allow", "go ahead")
        # A step the rules escalated (no checker run) says nothing about the checkers.
        unchecked = await store.add_step(
            StepRow(seq=2, tool_name="Bash", tool_input={"command": "sudo ls"})
        )
        second = await store.open_escalation(
            "deny rule", {"tool_input": {"command": "sudo ls"}}, unchecked
        )
        await herd.resolve(db, second, "deny")
        # An open escalation has no human decision yet.
        await store.open_escalation("open", {"tool_input": {"command": "x"}}, checked)

        # A step the checker **approved** and a human then denied, because an
        # auto-escalation category was on (D42): the label for a false negative, which is the
        # one the evals could not see before.
        approved = await store.add_step(
            StepRow(seq=3, tool_name="Write", tool_input={"file_path": "a.py", "content": "x"})
        )
        await store.add_checker_run(
            approved,
            CheckerRun(
                checker_name="edit",
                checker_version=1,
                model="test:model",
                decision="pass",
            ),
        )
        third = await store.open_escalation(
            "every edit waits for a human",
            {"tool_input": {"file_path": "a.py", "content": "x"}, "files": {}},
            approved,
        )
        await herd.resolve(db, third, "deny", "not like that")

        cases = await cases_from_escalations(db)
        # Newest first, and the rules escalation stays out: no checker ran on it.
        assert [c.name for c in cases] == [f"escalation-{third}", f"escalation-{first}"]
        assert cases[0].checker == "edit"
        # The checker said PASS here, so ALLOW is scored as a critical miss.
        assert cases[0].expected == [Decision.DENY, Decision.ESCALATE]
        assert cases[1].checker == "command"
        assert cases[1].expected == [Decision.ALLOW, Decision.ESCALATE]
        assert cases[1].source == "escalations"
    finally:
        await owner.close()


async def test_client_tokens_are_minted_checked_and_revoked(db_url: str) -> None:
    """D30: a session accepts every non-revoked token of its own session, and only those."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_address("ws://127.0.0.1:8765")
        printed = token_hash("printed-by-calu-run")
        await store.add_client_token(printed, "ws://127.0.0.1:8765")

        token_id, token = await herd.mint_token(db, session_id, "zed on the laptop")
        assert await store.client_token_ok(token_hash(token)) is True
        assert await store.client_token_ok(printed) is True
        assert await store.client_token_ok(token_hash("guessed")) is False

        rows = await herd.list_tokens(db, session_id)
        assert [row.id for row in rows] == [token_id, token_id - 1]
        minted = rows[0]
        assert minted.label == "zed on the laptop"
        assert minted.address == "ws://127.0.0.1:8765"
        assert minted.last_used_at is not None  # the check above stamped it
        assert not minted.revoked
        assert "valid" in minted.line() and "zed on the laptop" in minted.line()
        # The token itself is nowhere in what an operator can read.
        assert token not in minted.line()

        assert await herd.revoke_token(db, token_id) is True
        assert await herd.revoke_token(db, token_id) is False  # already revoked
        assert await store.client_token_ok(token_hash(token)) is False
        assert await store.client_token_ok(printed) is True  # the others keep working
        assert (await herd.list_tokens(db, session_id))[0].revoked is True

        # A token of another session does not open this one.
        other = DatabaseSessionStore(db)
        other_session = await other.start(IDENTITY)
        _, foreign = await herd.mint_token(db, other_session, "elsewhere")
        assert await store.client_token_ok(token_hash(foreign)) is False
        assert await other.client_token_ok(token_hash(foreign)) is True
        assert len(await herd.list_tokens(db)) == 3

        with pytest.raises(ValueError):
            await herd.mint_token(db, "00000000-0000-0000-0000-000000000000")
    finally:
        await owner.close()


async def test_a_session_whose_process_is_gone_is_visible_as_such(db_url: str) -> None:
    """A killed process cannot set its own state, so the herd needs a heartbeat (D56)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("running")

        # A fresh session is not stale, and the silence is measured by the database.
        rows = await herd.sessions(db)
        assert [row.id for row in rows] == [session_id]
        assert rows[0].stale is False
        assert rows[0].last_seen is not None
        assert rows[0].silent_for_s < 10
        assert "SILENT" not in rows[0].line()

        # The heartbeat moves `last_seen`.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
            (session_id,),
        )
        await store.heartbeat()
        assert (await herd.sessions(db))[0].stale is False

        # Silence for longer than `STALE_AFTER_S` marks the session, and the line says so.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '20 minutes' WHERE id = %s::uuid",
            (session_id,),
        )
        gone = (await herd.sessions(db))[0]
        assert gone.stale is True
        assert gone.silent_for_s > herd.STALE_AFTER_S
        assert "SILENT for 20 min" in gone.line()

        # A session that ended is never stale: it said so itself.
        await store.end("stopped")
        ended = (await herd.sessions(db, active_only=False))[0]
        assert ended.stale is False
        assert "SILENT" not in ended.line()
    finally:
        await owner.close()


async def test_an_escalation_of_a_gone_session_says_so(db_url: str) -> None:
    """Answering it is recorded and reaches nobody, so the list has to say which ones (D56)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("escalated")
        escalation = await store.open_escalation("why", {"rendered": "$ rm -rf build"})

        # While the session beats, the escalation is an ordinary one.
        live = (await herd.escalations(db))[0]
        assert live.unanswerable is False
        assert "the session is gone" not in live.line()
        rendered = herd.render_escalation(live, await herd.escalation_context(db, escalation))
        assert "process is gone" not in rendered

        # Once the process stops saying it is there, every view says so.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '10 minutes' WHERE id = %s::uuid",
            (session_id,),
        )
        gone = (await herd.escalations(db))[0]
        assert gone.unanswerable is True
        assert "the session is gone: answering changes nothing" in gone.line()
        one = await herd.escalation(db, escalation)
        assert one is not None and one.unanswerable is True
        rendered = herd.render_escalation(one, await herd.escalation_context(db, escalation))
        assert "the session's process is gone" in rendered
        assert "will not run" in rendered

        # A session that ended counts the same way: nobody is waiting either.
        await db.execute(
            "UPDATE sessions SET last_seen = now(), ended_at = now() WHERE id = %s::uuid",
            (session_id,),
        )
        ended = await herd.escalation(db, escalation)
        assert ended is not None and ended.unanswerable is True
    finally:
        await owner.close()


async def test_an_escalation_nobody_waits_for_can_be_cancelled(db_url: str) -> None:
    """Cancelling records that nothing was decided, which is the truth (PLAN.md D57)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        escalation = await store.open_escalation("why", {"rendered": "$ deploy"})
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
            (session_id,),
        )

        # The hint says what to do with it, instead of offering a decision.
        one = await herd.escalation(db, escalation)
        assert one is not None
        rendered = herd.render_escalation(one, await herd.escalation_context(db, escalation))
        assert f"calu resolve {escalation} cancel" in rendered
        assert "allow|deny" not in rendered

        assert await herd.resolve(db, escalation, "cancel", "the session is gone") is True
        row = await herd.escalation(db, escalation)
        assert row is not None
        assert row.state == "cancelled", "a cancellation is not a decision"
        assert row.note == "the session is gone"
        # It is out of the queue a human works through.
        assert await herd.escalations(db) == []
        # And cancelling it twice changes nothing.
        assert await herd.resolve(db, escalation, "cancel") is False

        # An unknown verb is still refused.
        with pytest.raises(ValueError, match="allow, deny, answer or cancel"):
            await herd.resolve(db, escalation, "maybe")
    finally:
        await owner.close()
