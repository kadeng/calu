"""Database connections and schema migrations (PLAN.md §7, D13).

`Db` wraps one connection so that several tasks of a session can use it one after another.
Given a `ConnectionSource`, it also replaces a connection that was lost (a restarted server, a
dropped socket): statements then wait until the database answers again instead of failing.

    statement ──► lock ──► connection alive? ──yes──► run it (shielded, bounded by `timeout`)
                               │ no
                               └► reconnect with backoff, run the `on_connect` hooks, then run it

A failed reconnect is one of two kinds. The server did not answer, or answered that it cannot
take connections right now: waiting may help, so the wrapper tries again without a limit. The
server answered and refused this login (the password no longer matches, the role may not log
in, the database does not exist): waiting does not help, so after `REFUSED_ATTEMPTS` such
answers in a row the wrapper gives up with `LoginRefused` (see `ConnectionSource.open`).

Migrations are the files `calu/db/migrations/NNNN_name.sql`, applied in the order of their
number. Each runs in its own transaction and is recorded in `schema_migrations`. A
transaction-level advisory lock keeps two processes from migrating at the same time.
"""

import logging
import os
import re
import select
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

import anyio
import anyio.to_thread
import psycopg
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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


REFUSED_ATTEMPTS = 3
"""Reconnect attempts in a row that the server may refuse before `Db` and `Notifier` give up.
More than one, because a server that is being reconfigured can refuse a login for a moment."""

REFUSAL_PROBE_S = 5.0
"""Upper bound for asking the server why it refused a login (`ConnectionSource.refusal`)."""

LAST_ATTEMPT_S = 2.0
"""How long one reconnect attempt may still take once `Db.limit_reconnect` has run out."""

REFUSED_CLASSES = ("28", "3D")
"""SQLSTATE classes of a refused login that waiting does not change: 28 "invalid authorization
specification" (wrong password, role missing or without LOGIN, no pg_hba.conf entry) and 3D
"invalid catalog name" (the database does not exist)."""

REFUSED_STATES = ("42501",)
"""Further SQLSTATEs of that kind: 42501 "insufficient privilege" (CONNECT was revoked)."""

_ERROR_CODE = re.compile(rb'\tErrorResponse\t.*? C "([0-9A-Z]{5})"')
"""The SQLSTATE field of a server error in libpq's protocol trace."""


class LoginRefused(psycopg.OperationalError):
    """The server answered a login and refused it for a reason that waiting does not change.

    Raised by `ConnectionSource.open`. `Db` and `Notifier` stop reconnecting after
    `REFUSED_ATTEMPTS` of these in a row; every other connection error is retried.
    """

    def __init__(self, message: str, code: str) -> None:
        """Create the error.

        Args:
            message: The server's message, as libpq reports it.
            code: The SQLSTATE the server answered with, e.g. "28P01".
        """
        super().__init__(message)
        self.code = code
        """The SQLSTATE the server answered the login with."""


def refuses_for_good(code: str | None) -> bool:
    """Tell whether a login refused with this SQLSTATE stays refused however long one waits.

    Args:
        code: The SQLSTATE of the server's answer, or None if the server gave none.

    Returns:
        True for the classes in `REFUSED_CLASSES` and the states in `REFUSED_STATES`. False
        for None and for everything else, among them 57P03 (the server is starting up or
        shutting down) and 53300 (too many connections), which pass.
    """
    return code is not None and (code[:2] in REFUSED_CLASSES or code in REFUSED_STATES)


def _login_answer(conninfo: str, timeout: float) -> str | None:
    """Try one login and return the SQLSTATE of the server's refusal; blocks the thread.

    psycopg reports a failed login as text only, in the language of the server, so the code is
    read from libpq's protocol trace of one more attempt. The trace is written to an anonymous
    memory file that no other process can open, and is discarded.

    Args:
        conninfo: The libpq connection string.
        timeout: Upper bound in seconds for the attempt.

    Returns:
        The SQLSTATE of the error the server answered with, or None if the login worked, the
        server did not answer in time, or it could not be reached at all.
    """
    trace = os.memfd_create("calu-login-trace")
    conn = None
    try:
        conn = pq.PGconn.connect_start(conninfo.encode())
        conn.trace(trace)
        deadline = time.monotonic() + timeout
        status = pq.PollingStatus.WRITING
        while status in (pq.PollingStatus.READING, pq.PollingStatus.WRITING):
            left = deadline - time.monotonic()
            if left <= 0 or conn.status == pq.ConnStatus.BAD:
                break
            reading = status == pq.PollingStatus.READING
            select.select(
                [conn.socket] if reading else [], [] if reading else [conn.socket], [], left
            )
            status = conn.connect_poll()
        conn.untrace()
        os.lseek(trace, 0, os.SEEK_SET)
        found = _ERROR_CODE.search(os.read(trace, 1 << 20))
        return found.group(1).decode() if found else None
    except psycopg.Error, OSError, ValueError:
        return None
    finally:
        if conn is not None:
            conn.finish()
        os.close(trace)


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
        """Open a new connection (see `connect`), and say when a failure cannot heal.

        Side effects: after a failed attempt, one more login attempt that only reads the
        server's answer (`refusal`).

        Returns:
            The open connection in autocommit mode; the caller closes it.

        Raises:
            LoginRefused: If the server answered and refused the login for a reason that
                waiting does not change (`refuses_for_good`).
            psycopg.OperationalError: If the server cannot be reached, or refuses for a reason
                that may pass. `LoginRefused` is a subclass, so a caller that does not care
                about the difference catches this one.
        """
        try:
            return await connect(self._conninfo)
        except psycopg.OperationalError as exc:
            code = await self.refusal()
            if refuses_for_good(code):
                assert code is not None
                raise LoginRefused(" ".join(str(exc).split()), code) from exc
            raise

    async def refusal(self) -> str | None:
        """Ask the server how it answers a login with this connection string.

        Side effects: one login attempt, in a worker thread, bounded by `REFUSAL_PROBE_S`.

        Returns:
            The SQLSTATE of the server's refusal, or None if the login worked or the server
            did not answer.
        """
        try:
            params = {**LIVENESS, **conninfo_to_dict(self._conninfo)}
        except psycopg.ProgrammingError:
            return None
        return await anyio.to_thread.run_sync(
            _login_answer, make_conninfo("", **params), REFUSAL_PROBE_S, abandon_on_cancel=True
        )

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
    - If the server refuses the login `REFUSED_ATTEMPTS` times in a row for a reason that
      waiting does not change, the wrapper gives up: the `on_refused` callbacks run, the
      waiting statement and every later one raise `LoginRefused`.
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
        self._on_lost: list[Callable[[str], None]] = []
        self._on_restored: list[Callable[[], None]] = []
        self._on_refused: list[Callable[[LoginRefused], None]] = []
        self._give_up_at: float | None = None  # on the clock of `anyio.current_time`
        self._gone: psycopg.OperationalError | None = None  # why reconnecting was given up
        self._attempt: anyio.CancelScope | None = None  # the reconnect attempt in flight
        self._wake = anyio.Event()  # ends the pause between two attempts early
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

    def on_lost(self, callback: Callable[[str], None]) -> None:
        """Register a callback for the moment the wrapper starts to replace its connection.

        Args:
            callback: Called once per replacement, before the first attempt, with the reason
                in plain words: the connection was lost, or a statement did not finish in time
                and the connection may be unusable. It must return at once and must not use
                this wrapper.
        """
        self._on_lost.append(callback)

    def on_restored(self, callback: Callable[[], None]) -> None:
        """Register a callback for the moment a replacement connection is in use.

        Args:
            callback: Called once per replacement, after the `on_connect` hooks succeeded. It
                must return at once and must not use this wrapper.
        """
        self._on_restored.append(callback)

    def on_refused(self, callback: Callable[[LoginRefused], None]) -> None:
        """Register a callback for the moment the wrapper gives up on a refused login.

        Args:
            callback: Called once, with the last refusal, after the server refused
                `REFUSED_ATTEMPTS` reconnect attempts in a row. From then on every statement
                raises `LoginRefused`. It must return at once and must not use this wrapper.
        """
        self._on_refused.append(callback)

    def limit_reconnect(self, seconds: float) -> None:
        """Stop waiting for a lost connection `seconds` from now.

        For the end of a process: its last writes (the session's end) are worth a short wait,
        but a shutdown must not wait for a database that stays away. A statement that is
        waiting between two attempts tries again at once. When the limit has run out, the
        attempt in flight (or the one attempt of a statement that comes later) gets
        `LAST_ATTEMPT_S` more and then the statement raises `psycopg.OperationalError`; every
        later statement raises at once. A waiting statement therefore returns or raises at most
        `seconds + LAST_ATTEMPT_S` after this call.

        Must be called from within the event loop.

        Args:
            seconds: How long statements may still wait, counted from this call.
        """
        self._give_up_at = anyio.current_time() + max(seconds, 0.0)
        if self._attempt is not None:
            self._attempt.deadline = min(self._attempt.deadline, self._give_up_at + LAST_ATTEMPT_S)
        self._wake.set()

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
            LoginRefused: If the server refused the login `REFUSED_ATTEMPTS` times in a row
                for a reason that waiting does not change, now or before an earlier statement.
            psycopg.OperationalError: If the connection is lost and `limit_reconnect` has run
                out.
        """
        if self._source is None or (not self._suspect and alive(self._conn)):
            return
        if self._gone is not None:
            error = _again(self._gone)
            raise error
        reason = (
            "a statement did not finish in time, so the connection is replaced"
            if self._suspect
            else "the database connection is lost"
        )
        log.warning("%s; reconnecting", reason)
        self.waiting = True
        _call_all(self._on_lost, reason)
        refused = 0
        try:
            pause = RECONNECT_FIRST_S
            while True:
                failure = await self._try_replace()
                if failure is None:
                    break
                log.warning("reconnecting to the database failed: %s", failure)
                refused = refused + 1 if isinstance(failure, LoginRefused) else 0
                if isinstance(failure, LoginRefused) and refused >= REFUSED_ATTEMPTS:
                    self._gone = failure
                    _call_all(self._on_refused, failure)
                    error = _again(failure)
                    raise error from failure
                wait = pause
                if self._give_up_at is not None:
                    left = self._give_up_at - anyio.current_time()
                    if left <= 0:
                        self._gone = psycopg.OperationalError(
                            f"the database connection is lost: {failure}"
                        )
                        error = _again(self._gone)
                        raise error from failure
                    wait = min(pause, left)
                self._wake = anyio.Event()
                with anyio.move_on_after(wait):
                    await self._wake.wait()
                pause = min(pause * 2, RECONNECT_MAX_S)
        finally:
            self.waiting = False
        log.warning("the database connection is back")
        _call_all(self._on_restored)

    async def _try_replace(self) -> Exception | None:
        """Make one attempt to replace the connection, bounded once `limit_reconnect` was set.

        Returns:
            None if `_conn` is a new, bound connection; else why the attempt failed: the
            `psycopg.Error` or `OSError` of `_replace`, or a `TimeoutError` if the attempt was
            cut off by the limit.
        """
        with anyio.CancelScope() as scope:
            self._attempt = scope
            if self._give_up_at is not None:
                scope.deadline = max(self._give_up_at, anyio.current_time()) + LAST_ATTEMPT_S
            try:
                await self._replace()
            except (psycopg.Error, OSError) as exc:
                return exc
            finally:
                self._attempt = None
            return None
        return TimeoutError("the attempt to reconnect did not finish before the process ends")

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


def _again(error: psycopg.OperationalError) -> psycopg.OperationalError:
    """Return a new error like `error`, so that one instance is not raised again and again.

    Args:
        error: Why reconnecting was given up.

    Returns:
        A `LoginRefused` with the same message and code, or an `OperationalError` with the
        same message.
    """
    if isinstance(error, LoginRefused):
        return LoginRefused(str(error), error.code)
    return psycopg.OperationalError(str(error))


def _call_all(callbacks: Sequence[Callable[..., None]], *args: object) -> None:
    """Call every callback; one that raises is logged and the others still run.

    Args:
        callbacks: The callbacks.
        *args: Passed to each.
    """
    for callback in callbacks:
        try:
            callback(*args)
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
