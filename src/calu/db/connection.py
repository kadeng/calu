"""Database connections and schema migrations (PLAN.md §7, D13).

`Db` wraps one connection so that several tasks of a session can use it one after another.
Given a `ConnectionSource`, it also replaces a connection that was lost (a restarted server, a
dropped socket): statements then wait until the database answers again instead of failing.

    statement ──► lock ──► connection alive? ──yes──► run it (shielded, bounded by `timeout`)
                               │ no
                               └► reconnect with backoff, run the `on_connect` hooks, then run it

Migrations are the files `calu/db/migrations/NNNN_name.sql`, applied in the order of their
number. Each runs in its own transaction and is recorded in `schema_migrations`. A
transaction-level advisory lock keeps two processes from migrating at the same time.
"""

import logging
import re
import select
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

import anyio
import psycopg
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict

log = logging.getLogger(__name__)

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


LIVENESS: dict[str, Any] = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 60000,
}
"""libpq parameters that make a dead peer noticed within about a minute.

Without them an idle connection whose peer vanished without a TCP reset (NAT or firewall
timeout, suspended machine, cut VPN) reports nothing for about two hours, and a connection
attempt to an unreachable host can block for minutes. A parameter that the connection string
sets itself wins over the value here. `tcp_user_timeout` is in milliseconds."""

RECONNECT_FIRST_S = 0.5
"""Pause after the first failed attempt to reopen a lost connection; doubled per attempt."""

RECONNECT_MAX_S = 30.0
"""Upper bound of the pause between two attempts to reopen a lost connection."""


async def connect(url: str | bytes | bytearray) -> psycopg.AsyncConnection:
    """Open an async connection in autocommit mode, with the `LIVENESS` parameters.

    Args:
        url: A libpq connection string or URL, e.g. "postgresql://user:pw@host:5432/calu".

    Returns:
        The open connection; the caller closes it.

    Raises:
        psycopg.OperationalError: If the server cannot be reached within the connect timeout or
            rejects the login.
    """
    conninfo = url if isinstance(url, str) else bytes(url).decode()
    try:
        params = {**LIVENESS, **conninfo_to_dict(conninfo)}
    except psycopg.ProgrammingError as exc:
        raise psycopg.OperationalError(f"invalid connection string: {exc}") from exc
    return await psycopg.AsyncConnection.connect(autocommit=True, **params)


class ConnectionSource:
    """Keeps a connection string inside the calu process, so a lost connection can be reopened.

    This object is the one place where a session keeps what it needs to log in again
    (REQUIREMENTS 16; this replaces PLAN.md D19): the string lives in this process's memory only.
    It is never written to the environment, to a command line or to a file, so the Claude Code
    process that calu starts does not inherit it, and `repr` does not show it.
    """

    def __init__(self, url: str | bytes | bytearray) -> None:
        """Copy the connection string.

        Args:
            url: A libpq connection string or URL. A `bytearray` is copied, so the caller may
                wipe its own buffer afterwards.
        """
        self._conninfo = url if isinstance(url, str) else bytes(url).decode()

    async def open(self) -> psycopg.AsyncConnection:
        """Open a new connection (see `connect`).

        Returns:
            The open connection in autocommit mode; the caller closes it.

        Raises:
            psycopg.OperationalError: If the server cannot be reached or rejects the login.
        """
        return await connect(self._conninfo)

    def __repr__(self) -> str:
        """Return a representation that does not contain the connection string.

        Returns:
            A fixed text.
        """
        return "ConnectionSource(<hidden>)"


def alive(conn: psycopg.AsyncConnection) -> bool:
    """Tell whether an idle connection is still usable, without sending anything.

    A server that shuts down or drops the connection closes the socket, which makes it readable.
    Reading what is pending then shows the broken state before a statement is sent, so the
    caller can reconnect first and no statement is lost to a connection that was already dead.
    The connection must be idle (no statement in flight).

    Args:
        conn: The connection.

    Returns:
        False if the connection is closed, broken, or its peer has closed it; True otherwise,
        including when the socket cannot be examined (the next statement then decides).
    """
    if conn.closed:
        return False
    try:
        pgconn = conn.pgconn
        poller = select.poll()
        poller.register(pgconn.socket, select.POLLIN)
        # A server that shuts down sends a last message and then closes: the first read takes
        # the message, the next one meets the end of the stream, which is what marks the
        # connection as bad. The bound only guards against a peer that keeps sending.
        for _ in range(16):
            if not poller.poll(0):
                break
            pgconn.consume_input()
        return pgconn.status == pq.ConnStatus.OK
    except psycopg.Error:
        return False
    except OSError, ValueError:
        return True


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
    connection, so every statement goes through this wrapper.

    Each method takes the lock for the whole statement, including reading its rows. Work that
    spans several statements (a transaction, an insert whose id the next statement needs) runs
    inside `exclusive`.

    Statements are **shielded from cancellation**: a task that is cancelled while its statement
    is in flight would leave an unread result on the connection, and every later statement of
    every other task would fail. A shielded statement delays the cancellation by at most
    `timeout` seconds; a statement that takes longer raises `TimeoutError`.

    **Lost connections.** Without a `source` the wrapper never reconnects: after a lost
    connection every statement raises `psycopg.OperationalError`. With a `source`:

    - Before each statement the wrapper checks that the connection is alive (`alive`). If it is
      not, or the previous statement timed out, the wrapper opens a new connection, with pauses
      from `RECONNECT_FIRST_S` doubling up to `RECONNECT_MAX_S`, and awaits the `on_connect`
      hooks on it. The statement **waits** for that, without a time limit and cancellable. A
      session's tool calls therefore wait while the database is away (fail closed) instead of
      failing.
    - A statement during which the connection breaks raises `psycopg.OperationalError` and is
      **not** repeated, because the wrapper cannot know whether the server ran it. The next
      statement reconnects first.
    - `limit_reconnect` bounds the waiting, for the end of a process.
    """

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        timeout: float = STATEMENT_TIMEOUT_S,
        source: ConnectionSource | None = None,
    ) -> None:
        """Wrap an open connection.

        Args:
            conn: An open connection in autocommit mode (see `connect`). Without `source` the
                wrapper does not close it; with `source` the wrapper owns it (`aclose`).
            timeout: Upper bound in seconds for one statement (or one `exclusive` block).
            source: Opens the replacement of a lost connection, or None for no reconnecting.
        """
        self._conn = conn
        self._lock = anyio.Lock()
        self.timeout = timeout
        self._source = source
        self._suspect = False  # the last statement timed out: the connection may be unusable
        self._on_connect: list[Callable[[psycopg.AsyncConnection], Awaitable[None]]] = []
        self._on_lost: list[Callable[[], None]] = []
        self._on_restored: list[Callable[[], None]] = []
        self._give_up_at: float | None = None
        self._gone = False
        self.waiting = False
        """True while a statement waits for the connection to be reopened."""

    def on_connect(self, hook: Callable[[psycopg.AsyncConnection], Awaitable[None]]) -> None:
        """Register what has to be run on every replacement connection before it is used.

        A session registers the statement that binds the connection to its session row here
        (`DatabaseSessionStore`), because connection settings do not survive the connection.

        Args:
            hook: Awaited with the new raw connection. A `psycopg.Error` from it counts as a
                failed attempt: the connection is closed and the wrapper tries again.
        """
        self._on_connect.append(hook)

    def on_lost(self, callback: Callable[[], None]) -> None:
        """Register a callback for the moment a lost connection is noticed.

        Args:
            callback: Called once per outage, before the first reconnect attempt. It must
                return at once and must not use this wrapper.
        """
        self._on_lost.append(callback)

    def on_restored(self, callback: Callable[[], None]) -> None:
        """Register a callback for the moment a replacement connection is in use.

        Args:
            callback: Called once per outage, after the `on_connect` hooks succeeded. It must
                return at once and must not use this wrapper.
        """
        self._on_restored.append(callback)

    def limit_reconnect(self, seconds: float) -> None:
        """Stop waiting for a lost connection `seconds` from now.

        For the end of a process: its last writes (the session's end) are worth a short wait,
        but a shutdown must not wait for a database that stays away. After the limit a
        statement that finds the connection lost makes one more attempt and then raises
        `psycopg.OperationalError`; once an attempt has failed after the limit, every later
        statement raises at once.

        Args:
            seconds: How long statements may still wait, counted from this call.
        """
        self._give_up_at = time.monotonic() + max(seconds, 0.0)

    async def aclose(self) -> None:
        """Close the current connection; errors while closing are ignored."""
        try:
            await self._conn.close()
        except psycopg.Error, OSError:
            log.debug("closing the database connection failed", exc_info=True)

    async def _ensure(self) -> None:
        """Make sure `_conn` is usable before a statement; the caller holds the lock.

        Side effects: with a `source`, replaces a lost or suspect connection (see the class
        docstring), which can take as long as the database is away.

        Raises:
            psycopg.OperationalError: If the connection is lost and `limit_reconnect` has run
                out.
        """
        if self._source is None or (not self._suspect and alive(self._conn)):
            return
        if self._gone:
            raise psycopg.OperationalError("the database connection is lost")
        log.warning("the database connection is lost; reconnecting")
        self.waiting = True
        _call_all(self._on_lost)
        try:
            pause = RECONNECT_FIRST_S
            while True:
                try:
                    await self._replace()
                    break
                except (psycopg.Error, OSError) as exc:
                    log.warning("reconnecting to the database failed: %s", exc)
                    wait = pause
                    if self._give_up_at is not None:
                        left = self._give_up_at - time.monotonic()
                        if left <= 0:
                            self._gone = True
                            raise psycopg.OperationalError(
                                f"the database connection is lost: {exc}"
                            ) from exc
                        wait = min(pause, left)
                    await anyio.sleep(wait)
                    pause = min(pause * 2, RECONNECT_MAX_S)
        finally:
            self.waiting = False
        log.warning("the database connection is back")
        _call_all(self._on_restored)

    async def _replace(self) -> None:
        """Open a new connection, run the `on_connect` hooks on it and make it `_conn`.

        Side effects: closes the previous connection.

        Raises:
            psycopg.Error: If the connection cannot be opened or a hook fails; `_conn` is then
                unchanged.
            OSError: If the network refuses the attempt below psycopg.
        """
        assert self._source is not None
        conn = await self._source.open()
        try:
            for hook in self._on_connect:
                await hook(conn)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await conn.close()
            raise
        old, self._conn = self._conn, conn
        self._suspect = False
        try:
            await old.close()
        except psycopg.Error, OSError:
            log.debug("closing the lost connection failed", exc_info=True)

    def _timed_out(self, what: str) -> TimeoutError:
        """Note that a statement was abandoned mid-flight and build its error.

        Side effects: marks the connection as suspect, so that a wrapper with a `source`
        replaces it before the next statement (an abandoned statement can leave an unread
        result behind).

        Args:
            what: The statement, for the message.

        Returns:
            The error to raise.
        """
        self._suspect = True
        return TimeoutError(_slow(what, self.timeout))

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
            await self._ensure()
            with anyio.move_on_after(self.timeout, shield=True):
                await self._conn.execute(statement.encode(), params)
                return
            raise self._timed_out(statement)

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
            await self._ensure()
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                return await cur.fetchone()
            raise self._timed_out(statement)

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
            await self._ensure()
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                return await cur.fetchall()
            raise self._timed_out(statement)

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
        it pass their SQL as `bytes`. A lost connection is replaced before the block starts
        (see the class docstring), never inside it.

        Yields:
            The connection; no other task may use it until the block ends.

        Raises:
            psycopg.OperationalError: If the connection is lost and cannot be reopened in time.
            TimeoutError: If the block takes longer than `timeout`.
        """
        async with self._lock:
            await self._ensure()
            with anyio.move_on_after(self.timeout, shield=True) as scope:
                yield self._conn
            if scope.cancelled_caught:
                raise self._timed_out("an exclusive block")


def _call_all(callbacks: Sequence[Callable[[], None]]) -> None:
    """Call every callback; one that raises is logged and the others still run.

    Args:
        callbacks: The callbacks.
    """
    for callback in callbacks:
        try:
            callback()
        except Exception:
            log.exception("a connection callback failed")


def _slow(statement: str, timeout: float) -> str:
    """Return the message of a statement that took too long.

    Args:
        statement: The SQL statement (shortened in the message).
        timeout: The bound in seconds.

    Returns:
        The message for the `TimeoutError`.
    """
    return f"the database did not answer within {timeout} s: {' '.join(statement.split())[:80]}"
