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
        assert await herd.escalations(db, session_id) == open_rows

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

        cases = await cases_from_escalations(db)
        assert [c.name for c in cases] == [f"escalation-{first}"]
        assert cases[0].checker == "command"
        assert cases[0].expected == [Decision.ALLOW, Decision.ESCALATE]
        assert cases[0].source == "escalations"
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
