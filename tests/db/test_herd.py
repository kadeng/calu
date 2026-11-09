"""Herd management on the database alone: list sessions, list and resolve escalations."""

import pytest

from calu import herd
from calu.db import Db, connect
from calu.setup import setup
from calu.store import DatabaseSessionStore, SessionIdentity, StepRow

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
