"""Notification channels of the calu schema (PLAN.md §7).

PostgreSQL announces three kinds of change with `pg_notify`:

| Channel | Sent when | Payload |
|---|---|---|
| `config_changed` | a row of `config`, `rules` or `checker_types` changed | `{"table": "<name>"}` |
| `escalation_resolved` | an operator resolved an escalation | `{"id", "session_id", "state"}` |
| `honesty_changed` | a mark of the honesty register was opened or closed | `{"session_id"}` |

The register has a channel of its own rather than using `config_changed`, because a mark is
written on an **edit** and a configuration change is rare: announcing marks on the
configuration channel would make every session of the database reload all of its settings on
every edit of every session (PLAN.md D92). The payload names the session that wrote it, so a
session can skip its own.

A connection that waits for notifications cannot run queries at the same time, so a session
uses one connection for its queries and a second one for listening.
"""

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import anyio
import psycopg
from psycopg import sql

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
    """

    def __init__(self) -> None:
        """Create a flag that is not set."""
        self._event = anyio.Event()

    def set(self) -> None:
        """Set the flag; a waiting worker wakes up."""
        self._event.set()

    def is_set(self) -> bool:
        """Tell whether the flag is set.

        Returns:
            True if `set` was called since the last `wait`.
        """
        return self._event.is_set()

    async def wait(self) -> None:
        """Wait until the flag is set, then clear it."""
        await self._event.wait()
        self._event = anyio.Event()


class Notifier:
    """Dispatches the notifications of one listening connection to handlers.

    The connection is used for nothing else while `run` is active (see the module docstring).
    Handlers are called in the order they were registered, with the parsed JSON payload; they
    must return at once, because the next notification waits for them. A handler that raises is
    logged, and the other handlers of the channel still run.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        """Create a notifier on a connection that is not used for anything else.

        Args:
            conn: An open connection in autocommit mode (`calu.db.connect`).
        """
        self._conn = conn
        self._handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}

    def on(self, channel: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        """Register a handler for one channel.

        Args:
            channel: Channel name, e.g. `ESCALATION_RESOLVED`.
            handler: Called with the parsed payload ({} if the payload is not a JSON object).
        """
        self._handlers.setdefault(channel, []).append(handler)

    async def run(self) -> None:
        """Subscribe to the registered channels and dispatch notifications until cancelled.

        Raises:
            psycopg.Error: If `LISTEN` fails or the connection breaks.
        """
        await listen(self._conn, self._handlers)
        async for note in self._conn.notifies():
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
