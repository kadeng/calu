"""Database connections and schema migrations (PLAN.md §7, D13).

`Db` wraps one connection so that several tasks of a session can use it one after another.

Migrations are the files `calu/db/migrations/NNNN_name.sql`, applied in the order of their
number. Each runs in its own transaction and is recorded in `schema_migrations`. A
transaction-level advisory lock keeps two processes from migrating at the same time.
"""

import re
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

import anyio
import psycopg

MIGRATION_LOCK = 0x6361_6C75  # "calu"
"""Key of the advisory lock held while migrating."""

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    sql: str


def migrations() -> list[Migration]:
    """Return the migrations shipped with calu, sorted by version.

    Returns:
        The migrations; file names that do not match `NNNN_name.sql` are ignored.

    Raises:
        ValueError: If two files have the same version number.
    """
    found: dict[int, Migration] = {}
    for entry in resources.files("calu.db").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {entry.name}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


async def connect(url: str | bytes | bytearray) -> psycopg.AsyncConnection:
    """Open an async connection in autocommit mode.

    Args:
        url: A libpq connection string or URL, e.g. "postgresql://user:pw@host:5432/calu".

    Returns:
        The open connection; the caller closes it.

    Raises:
        psycopg.OperationalError: If the server cannot be reached or rejects the login.
    """
    conninfo = url if isinstance(url, str) else bytes(url).decode()
    return await psycopg.AsyncConnection.connect(conninfo, autocommit=True)


async def applied_versions(conn: psycopg.AsyncConnection) -> set[int]:
    """Return the versions recorded in `schema_migrations`.

    Args:
        conn: An open connection.

    Returns:
        The applied versions; empty if the table does not exist yet.

    Raises:
        psycopg.Error: On database errors.
    """
    cur = await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
    row = await cur.fetchone()
    if not row or not row[0]:
        return set()
    cur = await conn.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in await cur.fetchall()}


async def migrate(conn: psycopg.AsyncConnection) -> list[Migration]:
    """Apply all migrations that are not applied yet.

    The connection must belong to the database owner (it creates tables, functions and the
    roles `calu_session` and `calu_operator`). Each migration runs in one transaction together
    with its `schema_migrations` row, so a failed migration leaves nothing behind.

    Args:
        conn: An open connection in autocommit mode (see `connect`).

    Returns:
        The migrations applied by this call, in order; empty if the schema was up to date.

    Raises:
        psycopg.Error: If a migration fails; earlier migrations of this call stay applied.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version integer PRIMARY KEY,"
        " name text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied: list[Migration] = []
    for migration in migrations():
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            if migration.version in await applied_versions(conn):
                continue
            await conn.execute(migration.sql.encode())
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
        applied.append(migration)
    return applied


STATEMENT_TIMEOUT_S = 30.0
"""Upper bound for one statement of `Db` (the shield below must not last longer)."""


class Db:
    """One connection that several tasks share, serialized with a lock.

    psycopg allows only one statement at a time per connection: a second `execute` while the
    first one is still running fails with "another command is already in progress". A session
    runs checks, narrations and subagent steps at the same time and writes them all through one
    connection (which `calu run` opens before it wipes the credentials, PLAN.md D8), so every
    statement goes through this wrapper.

    Each method takes the lock for the whole statement, including reading its rows. Work that
    spans several statements (a transaction, an insert whose id the next statement needs) runs
    inside `exclusive`.

    Statements are **shielded from cancellation**: a task that is cancelled while its statement
    is in flight would leave an unread result on the connection, and every later statement of
    every other task would fail. A shielded statement delays the cancellation by at most
    `timeout` seconds; a statement that takes longer raises `TimeoutError` (the connection is
    then unusable, and the session reports the error).
    """

    def __init__(self, conn: psycopg.AsyncConnection, timeout: float = STATEMENT_TIMEOUT_S) -> None:
        """Wrap an open connection; the wrapper does not close it.

        Args:
            conn: An open connection in autocommit mode (see `connect`).
            timeout: Upper bound in seconds for one statement (or one `exclusive` block).
        """
        self._conn = conn
        self._lock = anyio.Lock()
        self.timeout = timeout

    async def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        """Run a statement and discard any rows it returns.

        Args:
            statement: The SQL statement, with `%s` placeholders.
            params: Its parameters.

        Raises:
            psycopg.Error: On database errors.
            TimeoutError: If the statement takes longer than `timeout`.
        """
        async with self._lock:
            with anyio.move_on_after(self.timeout, shield=True):
                await self._conn.execute(statement.encode(), params)
                return
        raise TimeoutError(_slow(statement, self.timeout))

    async def one(self, statement: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        """Run a query and return its first row.

        Args:
            statement: The query.
            params: Its parameters.

        Returns:
            The first row, or None if the query returned none.

        Raises:
            psycopg.Error: On database errors.
            TimeoutError: If the query takes longer than `timeout`.
        """
        async with self._lock:
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                return await cur.fetchone()
        raise TimeoutError(_slow(statement, self.timeout))

    async def all(self, statement: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a query and return all its rows.

        Args:
            statement: The query.
            params: Its parameters.

        Returns:
            The rows in the order the query produced them.

        Raises:
            psycopg.Error: On database errors.
            TimeoutError: If the query takes longer than `timeout`.
        """
        async with self._lock:
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                return await cur.fetchall()
        raise TimeoutError(_slow(statement, self.timeout))

    async def scalar(self, statement: str, params: Sequence[Any] = ()) -> Any:
        """Run a statement with one returned column and return that value.

        Args:
            statement: The statement, e.g. an INSERT with RETURNING id.
            params: Its parameters.

        Returns:
            The single value of the single returned row.

        Raises:
            psycopg.Error: On database errors.
            TimeoutError: If the statement takes longer than `timeout`.
            RuntimeError: If the statement returned no row.
        """
        row = await self.one(statement, params)
        if row is None:
            raise RuntimeError(f"no row returned by {' '.join(statement.split()[:3])}")
        return row[0]

    @asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[psycopg.AsyncConnection]:
        """Hold the lock and hand out the raw connection, for several statements in a row.

        The block is shielded from cancellation and bounded by `timeout`, like a single
        statement, so a cancelled task cannot leave the connection unusable. Statements inside
        it pass their SQL as `bytes`.

        Yields:
            The connection; no other task may use it until the block ends.

        Raises:
            TimeoutError: If the block takes longer than `timeout`.
        """
        async with self._lock:
            with anyio.move_on_after(self.timeout, shield=True) as scope:
                yield self._conn
            if scope.cancelled_caught:
                raise TimeoutError(_slow("an exclusive block", self.timeout))


def _slow(statement: str, timeout: float) -> str:
    """Return the message of a statement that took too long.

    Args:
        statement: The SQL statement (shortened in the message).
        timeout: The bound in seconds.

    Returns:
        The message for the `TimeoutError`.
    """
    return f"the database did not answer within {timeout} s: {' '.join(statement.split())[:80]}"
