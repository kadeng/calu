"""The last usage limits Claude Code reported, stored per Claude account (migration 0041, D250)."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from calu.db import Db, connect
from calu.protocol.messages import RateLimit
from calu.setup import setup
from calu.store import DatabaseSessionStore, SessionIdentity

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_a_session_stores_and_reads_the_limits_of_its_own_account(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    owner = await connect(db_url)
    first, second, third = [await connect(session_url) for _ in range(3)]
    try:
        one = DatabaseSessionStore(Db(first))
        two = DatabaseSessionStore(Db(second))
        other = DatabaseSessionStore(Db(third))
        await one.start(SessionIdentity(hostname="h1", os_user="ada", directory="/a", pid=1))
        await two.start(SessionIdentity(hostname="h1", os_user="ada", directory="/b", pid=2))
        await other.start(SessionIdentity(hostname="h2", os_user="bob", directory="/c", pid=3))
        now = datetime.now(UTC)
        week = RateLimit(type="seven_day", status="allowed", utilization=0.3, reported_at=now)

        key = await one.note_usage_limit("ada@example.org (Example)", week)
        assert key == "claude:ada@example.org (Example)"
        # A later session of the same account reads it, with its time and marked as stored.
        (read,) = await two.usage_limits("ada@example.org (Example)")
        assert read.type == "seven_day" and read.utilization == pytest.approx(0.3)
        assert read.stored and read.reported_at == now
        assert await other.usage_limits("bob@example.org") == []

        # An older report does not replace a newer one.
        stale = week.model_copy(update={"utilization": 0.1, "reported_at": now - timedelta(1)})
        await two.note_usage_limit("ada@example.org (Example)", stale)
        (read,) = await one.usage_limits("ada@example.org (Example)")
        assert read.utilization == pytest.approx(0.3)

        # Without Claude Code's name of the login: per OS user and host, from the session's
        # own row; another OS user has another key.
        hour = RateLimit(type="five_hour", utilization=0.5, reported_at=now)
        assert await one.note_usage_limit(None, hour) == "os:ada@h1"
        assert [limit.type for limit in await two.usage_limits(None)] == ["five_hour"]
        assert await other.usage_limits(None) == []
        assert await other.note_usage_limit("", hour) == "os:bob@h2"

        # The session login has no right on the table itself; operators read it.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await third.execute("SELECT * FROM usage_limits")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await third.execute(
                "INSERT INTO usage_limits (account, limit_type, status, reported_at)"
                " VALUES ('os:ada@h1', 'x', 'allowed', now())"
            )
        cur = await owner.execute("SELECT account, limit_type FROM usage_limits ORDER BY 1, 2")
        assert await cur.fetchall() == [
            ("claude:ada@example.org (Example)", "seven_day"),
            ("os:ada@h1", "five_hour"),
            ("os:bob@h2", "five_hour"),
        ]

        # A connection that proved no session stores and reads nothing.
        blind = await connect(session_url)
        try:
            cur = await blind.execute(
                "SELECT calu_note_usage_limit('x@example.org', 'five_hour', 'allowed', 0.9,"
                " NULL, now())"
            )
            assert await cur.fetchone() == (None,)
            cur = await blind.execute("SELECT count(*) FROM calu_usage_limits('x@example.org')")
            assert await cur.fetchone() == (0,)
        finally:
            await blind.close()
    finally:
        for c in (first, second, third, owner):
            await c.close()
