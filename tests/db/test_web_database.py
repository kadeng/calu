"""`ReopeningDb`: the connection of `calu web` is replaced when the database closed it."""

import anyio
import psycopg
import pytest

from calu.db import connect

pytest.importorskip("starlette", reason="the web interface's packages are missing: uv sync")

from calu.web.database import ReopeningDb

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def end_other_connections(db_url: str) -> None:
    """Close every other connection to the test database, as a database restart does."""
    killer = await connect(db_url)
    try:
        await killer.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    finally:
        await killer.close()


async def test_a_connection_the_database_closed_is_replaced_before_the_next_statement(
    db_url: str,
) -> None:
    opened = 0

    async def reopen() -> psycopg.AsyncConnection:
        """Open the next connection and count it."""
        nonlocal opened
        opened += 1
        return await connect(db_url)

    db = ReopeningDb(await connect(db_url), reopen, probe_after=0.0)
    try:
        assert await db.scalar("SELECT 1") == 1
        assert opened == 0, "a healthy connection is kept"

        await end_other_connections(db_url)
        # Idle long enough to be probed (here: always), so the restart costs no statement.
        assert await db.scalar("SELECT 2") == 2
        assert opened == 1

        await end_other_connections(db_url)
        await db.execute("CREATE TABLE written (n integer)")
        await end_other_connections(db_url)
        assert await db.all("SELECT count(*) FROM written") == [(0,)]
        await end_other_connections(db_url)
        async with db.exclusive() as conn, conn.transaction():
            await conn.execute(b"INSERT INTO written VALUES (1)")
        assert await db.one("SELECT n FROM written") == (1,)
        assert opened == 4
    finally:
        await db.close()


async def test_a_statement_is_never_run_twice(db_url: str) -> None:
    """A connection that dies under a statement fails that statement; the next one works.

    Without a probe (the connection was used a moment ago) the dead connection is found by the
    statement itself. Whether a write took effect is then unknown, so it is not repeated.
    """
    db = ReopeningDb(await connect(db_url), lambda: connect(db_url), probe_after=3600.0)
    try:
        await db.execute("CREATE TABLE written (n integer)")
        await end_other_connections(db_url)
        with pytest.raises(psycopg.OperationalError):
            await db.execute("INSERT INTO written VALUES (1)")
        assert await db.all("SELECT n FROM written") == [], "not repeated on the new connection"
        await db.execute("INSERT INTO written VALUES (2)")
        assert await db.all("SELECT n FROM written") == [(2,)]
    finally:
        await db.close()


async def test_a_database_that_stays_down_raises_until_it_is_back(db_url: str) -> None:
    reachable = False

    async def reopen() -> psycopg.AsyncConnection:
        """Open the next connection, unless the test says the database is down."""
        if not reachable:
            raise psycopg.OperationalError("connection refused")
        return await connect(db_url)

    db = ReopeningDb(await connect(db_url), reopen, probe_after=0.0)
    try:
        await end_other_connections(db_url)
        for _ in range(2):
            with pytest.raises(psycopg.OperationalError):
                await db.one("SELECT 1")
        reachable = True
        assert await db.one("SELECT 1") == (1,)
    finally:
        await db.close()


async def test_a_connection_a_statement_timed_out_on_is_not_used_again(db_url: str) -> None:
    """`Db` says the connection is unusable after a timeout; here it is replaced."""
    opened = 0

    async def reopen() -> psycopg.AsyncConnection:
        """Open the next connection and count it."""
        nonlocal opened
        opened += 1
        return await connect(db_url)

    db = ReopeningDb(await connect(db_url), reopen, timeout=0.3, probe_after=3600.0)
    try:
        with pytest.raises(TimeoutError):
            await db.one("SELECT pg_sleep(5)")
        with anyio.fail_after(5):
            assert await db.one("SELECT 1") == (1,)
        assert opened == 1
        with pytest.raises(TimeoutError):
            async with db.exclusive() as conn:
                await conn.execute(b"SELECT pg_sleep(5)")
        with anyio.fail_after(5):
            assert await db.scalar("SELECT 3") == 3
        assert opened == 2
    finally:
        await db.close()
