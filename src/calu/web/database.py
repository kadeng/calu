"""The database connection of `calu web`: one connection that is replaced when it dies.

`calu.db.Db` wraps one connection for the life of its process. That fits `calu run`, which wipes
its credentials after connecting and reports a lost database as an error of the session.
`calu web` runs for days against a database on another machine and keeps its credential in
memory, so it can reconnect; `ReopeningDb` does that:

    statement arrives
      -> the connection is closed, or the last statement timed out   -> open a new one
      -> the connection was idle for `PROBE_AFTER_S` or longer       -> `SELECT 1`; open a new
                                                                        one if that fails
      -> run the statement on the connection

No statement is ever run twice. A statement that fails because the connection died while it ran
raises to its caller (the web interface answers 503), and the next statement gets a new
connection. Whether a failed write took effect is unknown in that case, so repeating it is the
operator's decision and not this class's.
"""

import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

import anyio
import psycopg

from calu.db.connection import STATEMENT_TIMEOUT_S, Db

log = logging.getLogger(__name__)

PROBE_AFTER_S = 1.0
"""Idle time after which a connection is tested before it is used again.

The statements of one request follow each other within milliseconds, and the pages poll every
two to four seconds, so in practice the first statement of a request is preceded by a probe and
the others are not.
"""

Opener = Callable[[], Awaitable[psycopg.AsyncConnection]]
"""Opens a new connection in autocommit mode, as `calu.db.connect` does."""


class ReopeningDb(Db):
    """A `Db` that replaces its connection when the database closed it or stopped answering.

    The interface is that of `Db`. The differences:

    - A connection that is closed, or that a statement timed out on, is replaced before the
      next statement runs.
    - A connection that was idle for `probe_after` seconds is tested with `SELECT 1` first, so
      a database restart between two requests costs no request.
    - `close` closes the current connection; `Db` leaves that to its caller.

    Invariant: a statement runs at most once, whatever happens to the connection.
    """

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        opener: Opener,
        timeout: float = STATEMENT_TIMEOUT_S,
        probe_after: float = PROBE_AFTER_S,
    ) -> None:
        """Wrap an open connection and remember how to open the next one.

        Args:
            conn: An open connection in autocommit mode (see `calu.db.connect`).
            opener: Opens a replacement connection; it may raise `psycopg.OperationalError`.
            timeout: Upper bound in seconds for one statement, one `exclusive` block, one probe
                and one reconnect.
            probe_after: Idle seconds after which the connection is tested before use.
        """
        super().__init__(conn, timeout)
        self._opener = opener
        self._probe_after = probe_after
        self._used_at = time.monotonic()
        self._unusable = False
        """True after a timeout: an unread result may still be on the connection."""

    async def _ready(self) -> None:
        """Make sure `_conn` is usable, replacing it if it is not. Call with the lock held.

        Raises:
            psycopg.OperationalError: If the connection is dead and a new one cannot be opened.
            TimeoutError: If the probe and the reconnect together take longer than `timeout`.
        """
        with anyio.move_on_after(self.timeout, shield=True):
            dead = self._unusable or self._conn.closed
            if not dead and time.monotonic() - self._used_at >= self._probe_after:
                try:
                    await self._conn.execute(b"SELECT 1")
                except psycopg.Error, OSError:
                    dead = True
            if dead:
                with suppress(psycopg.Error, OSError):
                    await self._conn.close()
                self._conn = await self._opener()
                self._unusable = False
                log.warning("the database connection was lost and has been opened again")
            self._used_at = time.monotonic()
            return
        self._unusable = True
        raise TimeoutError(f"the database did not answer within {self.timeout} s")

    async def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        """Run a statement and discard any rows it returns, on a connection that is usable.

        Args:
            statement: The SQL statement, with `%s` placeholders.
            params: Its parameters.

        Raises:
            psycopg.Error: On database errors, including a database that cannot be reached.
            TimeoutError: If the statement takes longer than `timeout`.
        """
        async with self._lock:
            await self._ready()
            with anyio.move_on_after(self.timeout, shield=True):
                await self._conn.execute(statement.encode(), params)
                self._used_at = time.monotonic()
                return
            self._unusable = True
        raise TimeoutError(_slow(statement, self.timeout))

    async def one(self, statement: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        """Run a query and return its first row, on a connection that is usable.

        Args:
            statement: The query.
            params: Its parameters.

        Returns:
            The first row, or None if the query returned none.

        Raises:
            psycopg.Error: On database errors, including a database that cannot be reached.
            TimeoutError: If the query takes longer than `timeout`.
        """
        async with self._lock:
            await self._ready()
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                row = await cur.fetchone()
                self._used_at = time.monotonic()
                return row
            self._unusable = True
        raise TimeoutError(_slow(statement, self.timeout))

    async def all(self, statement: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a query and return all its rows, on a connection that is usable.

        Args:
            statement: The query.
            params: Its parameters.

        Returns:
            The rows in the order the query produced them.

        Raises:
            psycopg.Error: On database errors, including a database that cannot be reached.
            TimeoutError: If the query takes longer than `timeout`.
        """
        async with self._lock:
            await self._ready()
            with anyio.move_on_after(self.timeout, shield=True):
                cur = await self._conn.execute(statement.encode(), params)
                rows = await cur.fetchall()
                self._used_at = time.monotonic()
                return rows
            self._unusable = True
        raise TimeoutError(_slow(statement, self.timeout))

    @asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[psycopg.AsyncConnection]:
        """Hold the lock and hand out a usable raw connection, for several statements in a row.

        The connection is tested or replaced before the block starts, never inside it: a
        transaction that loses its connection fails as a whole.

        Yields:
            The connection; no other task may use it until the block ends.

        Raises:
            psycopg.OperationalError: If the database cannot be reached.
            TimeoutError: If the block takes longer than `timeout`.
        """
        async with self._lock:
            await self._ready()
            with anyio.move_on_after(self.timeout, shield=True) as scope:
                yield self._conn
            self._used_at = time.monotonic()
            if scope.cancelled_caught:
                self._unusable = True
                raise TimeoutError(_slow("an exclusive block", self.timeout))

    async def close(self) -> None:
        """Close the current connection. Statements after this open a new one."""
        with suppress(psycopg.Error, OSError):
            await self._conn.close()


def _slow(statement: str, timeout: float) -> str:
    """Return the message of a statement that took too long.

    Args:
        statement: The SQL statement (shortened in the message).
        timeout: The bound in seconds.

    Returns:
        The message for the `TimeoutError`.
    """
    return f"the database did not answer within {timeout} s: {' '.join(statement.split())[:80]}"
