"""Notification channels of the calu schema (PLAN.md §7).

PostgreSQL announces three kinds of change with `pg_notify`:

| Channel | Sent when | Payload |
|---|---|---|
| `config_changed` | a row of `config`, `rules` or `checker_types` changed | `{"table": "<name>"}` |
| `escalation_resolved` | an operator resolved an escalation | `{"id", "session_id", "state"}` |
| `trust_changed` | a trusted commit or file content was added or removed | `{"table": "<name>"}` |

Trust has a channel of its own rather than using `config_changed`, because a session records a
trusted commit after a commit of its own: announcing that on the configuration channel would
make every session of the database reload all of its settings.

A connection that waits for notifications cannot run queries at the same time, so a session
uses one connection for its queries and a second one for listening.

Notifications are a hint, never the only path: PostgreSQL does not keep them for a listener
that is not connected. A `Notifier` with a `ConnectionSource` therefore reopens a lost listening
connection and tells its `on_listening` callbacks each time `LISTEN` is in force again, so that
the caller re-reads what it may have missed.
"""

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import anyio
import psycopg
from psycopg import sql

from calu.db.connection import (
    RECONNECT_FIRST_S,
    RECONNECT_MAX_S,
    REFUSED_ATTEMPTS,
    ConnectionSource,
    LoginRefused,
)

log = logging.getLogger(__name__)

CONFIG_CHANGED = "config_changed"
"""Channel of configuration, rule and checker definition changes."""

ESCALATION_RESOLVED = "escalation_resolved"
"""Channel of escalations resolved by an operator."""


async def listen(conn: psycopg.AsyncConnection, channels: Iterable[str]) -> None:
    """Subscribe a connection to notification channels.

    After this call, `conn.notifies()` yields the notifications of these channels. The
    connection must be in autocommit mode (`calu.db.connect`), because `LISTEN` takes effect
    only when its transaction commits.

    Args:
        conn: An open connection in autocommit mode.
        channels: Channel names, e.g. `[CONFIG_CHANGED]`. Names are quoted as identifiers, so
            they are case-sensitive and match the names used by `pg_notify`.

    Raises:
        psycopg.Error: On database errors.
    """
    for channel in channels:
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))


class Dirty:
    """A flag that a notification sets and a worker waits for and then clears.

    Several notifications before the worker wakes up count as one: the worker reads the current
    state of the database anyway. A notification that arrives between `wait` returning and the
    worker's read is lost, which is harmless for the same reason.

    The flag has two strengths. `set` says "something changed": the worker must act. `hint`
    says "something may have changed" (a reconnect, a subscription that just took effect): the
    worker should look and act only if it finds a difference. `wait` reports which one woke it.
    """

    def __init__(self) -> None:
        """Create a flag that is not set."""
        self._event = anyio.Event()
        self._certain = False

    def set(self) -> None:
        """Set the flag as "something changed"; a waiting worker wakes up."""
        self._certain = True
        self._event.set()

    def hint(self) -> None:
        """Set the flag as "something may have changed"; a waiting worker wakes up.

        A `set` before the worker wakes up outranks the hint.
        """
        self._event.set()

    def is_set(self) -> bool:
        """Tell whether the flag is set.

        Returns:
            True if `set` or `hint` was called since the last `wait`.
        """
        return self._event.is_set()

    async def wait(self) -> bool:
        """Wait until the flag is set, then clear it.

        Returns:
            True if `set` was called since the last `wait`, False if only `hint` was.
        """
        await self._event.wait()
        certain, self._certain = self._certain, False
        self._event = anyio.Event()
        return certain


class Notifier:
    """Dispatches the notifications of one listening connection to handlers.

    The connection is used for nothing else while `run` is active (see the module docstring).
    Handlers are called in the order they were registered, with the parsed JSON payload; they
    must return at once, because the next notification waits for them. A handler that raises is
    logged, and the other handlers of the channel still run.

    With a `source`, `run` survives the loss of the connection: it reports the loss to the
    `on_lost` callbacks, reopens the connection with pauses from `RECONNECT_FIRST_S` doubling up
    to `RECONNECT_MAX_S`, subscribes again and calls the `on_listening` callbacks. Notifications
    sent while it was not listening are lost; `on_listening` is where a caller makes up for them.
    If the server refuses the login `REFUSED_ATTEMPTS` times in a row for a reason that waiting
    does not change (`calu.db.connection.LoginRefused`), `run` tells the `on_refused` callbacks
    and returns.
    """

    def __init__(
        self, conn: psycopg.AsyncConnection, source: ConnectionSource | None = None
    ) -> None:
        """Create a notifier on a connection that is not used for anything else.

        Args:
            conn: An open connection in autocommit mode (`calu.db.connect`). With `source` the
                notifier owns it and its replacements (`aclose`).
            source: Opens the replacement of a lost connection, or None: `run` then raises
                when the connection breaks.
        """
        self._conn = conn
        self._source = source
        self._handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self._listening: list[Callable[[], None]] = []
        self._lost: list[Callable[[BaseException], None]] = []
        self._refused: list[Callable[[LoginRefused], None]] = []
        self.listening = False
        """True while `LISTEN` is in force on a connection that has not failed."""

    def on(self, channel: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        """Register a handler for one channel.

        Args:
            channel: Channel name, e.g. `ESCALATION_RESOLVED`.
            handler: Called with the parsed payload ({} if the payload is not a JSON object).
        """
        self._handlers.setdefault(channel, []).append(handler)

    def on_listening(self, callback: Callable[[], None]) -> None:
        """Register a callback for each time the subscriptions are in force.

        That is once shortly after `run` starts and once after every reconnect. A change made
        before that moment was not announced to this notifier, so a caller that keeps a copy of
        database state re-reads it here.

        Args:
            callback: Called without arguments; it must return at once.
        """
        self._listening.append(callback)

    def on_lost(self, callback: Callable[[BaseException], None]) -> None:
        """Register a callback for the loss of the listening connection.

        Args:
            callback: Called with the error, once per outage; it must return at once. Only a
                notifier with a `source` calls it (without one, `run` raises the error).
        """
        self._lost.append(callback)

    def on_refused(self, callback: Callable[[LoginRefused], None]) -> None:
        """Register a callback for the moment the notifier gives up on a refused login.

        Args:
            callback: Called once, with the last refusal, after the server refused
                `REFUSED_ATTEMPTS` reconnect attempts in a row; `run` returns after it. It
                must return at once.
        """
        self._refused.append(callback)

    async def aclose(self) -> None:
        """Close the current listening connection; errors while closing are ignored."""
        try:
            await self._conn.close()
        except psycopg.Error, OSError:
            log.debug("closing the listening connection failed", exc_info=True)

    async def run(self) -> None:
        """Subscribe to the registered channels and dispatch notifications until cancelled.

        With a `source` this returns in one case only: the server refused the login
        `REFUSED_ATTEMPTS` times in a row for a reason that waiting does not change. The
        `on_refused` callbacks were called then, and nothing listens any more.

        Raises:
            psycopg.Error: Without a `source`: if `LISTEN` fails or the connection breaks. With
                a `source` the notifier reconnects instead and this never raises.
            OSError: Without a `source`: if the network fails below psycopg.
        """
        pause = RECONNECT_FIRST_S
        reported = False
        refused = 0

        def subscribed() -> None:
            """Note that `LISTEN` is in force and tell the `on_listening` callbacks."""
            nonlocal pause, reported, refused
            if reported:
                log.warning("the notification connection is back")
            pause, reported, refused = RECONNECT_FIRST_S, False, 0
            self.listening = True
            self._call(self._listening)

        while True:
            try:
                await self._serve(subscribed)
            except (psycopg.Error, OSError) as exc:
                self.listening = False
                if self._source is None:
                    raise
                if not reported:
                    log.warning("the notification connection is lost: %s", exc)
                    self._call(self._lost, exc)
                    reported = True
                await self.aclose()
                refused = refused + 1 if isinstance(exc, LoginRefused) else 0
                if isinstance(exc, LoginRefused) and refused >= REFUSED_ATTEMPTS:
                    log.error("the notification connection cannot be reopened: %s", exc)
                    self._call(self._refused, exc)
                    return
                await anyio.sleep(pause)
                pause = min(pause * 2, RECONNECT_MAX_S)

    async def _serve(self, subscribed: Callable[[], None]) -> None:
        """Subscribe on one connection and dispatch its notifications until it fails.

        Side effects: with a `source`, replaces a closed connection by a new one first.

        Args:
            subscribed: Called once `LISTEN` is in force.

        Raises:
            psycopg.OperationalError: If the connection cannot be opened, breaks or is closed.
            psycopg.Error: If `LISTEN` fails.
            OSError: If the network fails below psycopg.
        """
        if self._conn.closed and self._source is not None:
            self._conn = await self._source.open()
        await listen(self._conn, self._handlers)
        subscribed()
        async for note in self._conn.notifies():
            self._dispatch(note)
        raise psycopg.OperationalError("the notification connection was closed")

    def _dispatch(self, note: psycopg.Notify) -> None:
        """Pass one notification to the handlers of its channel.

        Args:
            note: The notification; a payload that is not a JSON object is passed as {}.
        """
        try:
            payload = json.loads(note.payload) if note.payload else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for handler in self._handlers.get(note.channel, ()):
            try:
                handler(payload)
            except Exception:
                log.exception("a handler of %s failed", note.channel)

    @staticmethod
    def _call(callbacks: Iterable[Callable[..., None]], *args: object) -> None:
        """Call every callback; one that raises is logged and the others still run.

        Args:
            callbacks: The callbacks.
            *args: Passed to each.
        """
        for callback in callbacks:
            try:
                callback(*args)
            except Exception:
                log.exception("a notifier callback failed")
