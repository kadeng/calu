"""Notification channels of the calu schema (PLAN.md §7).

PostgreSQL announces two kinds of change with `pg_notify`:

| Channel | Sent when | Payload |
|---|---|---|
| `config_changed` | a row of `config`, `rules` or `checker_types` changed | `{"table": "<name>"}` |
| `escalation_resolved` | an operator resolved an escalation | `{"id", "session_id", "state"}` |

A connection that waits for notifications cannot run queries at the same time, so a session
uses one connection for its queries and a second one for listening.
"""

from collections.abc import Iterable

import psycopg
from psycopg import sql

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
