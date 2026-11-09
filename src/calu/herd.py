"""Watching and unblocking sessions through the database (PLAN.md §8, REQUIREMENTS 12, 13).

These queries need no running session: they work on the database alone, from any machine that
can reach it. `resolve` calls `calu_resolve_escalation`, which notifies the waiting session, so
the session continues within a second.

`calu sessions`, `calu escalations` and `calu resolve` use the `admin` credential for now. A
separate login for the role `calu_operator` (reads everything, resolves escalations, changes
nothing) is still open (PLAN.md Q4).
"""

from dataclasses import dataclass
from datetime import datetime

from calu.db import Db


@dataclass(frozen=True)
class SessionRow:
    """One session as the herd commands show it."""

    id: str
    hostname: str
    os_user: str
    directory: str
    state: str
    state_since: datetime
    started_at: datetime
    ended_at: datetime | None
    claude_session_id: str | None
    open_escalations: int

    def line(self) -> str:
        """Return the session as one terminal line.

        Returns:
            Id, state, host and directory, with the number of open escalations if there are any.
        """
        waiting = f" ({self.open_escalations} open)" if self.open_escalations else ""
        return (
            f"{self.id}  {self.state}{waiting}  {self.hostname}:{self.directory}"
            f"  since {self.state_since:%Y-%m-%d %H:%M:%S}"
        )


@dataclass(frozen=True)
class EscalationRow:
    """One escalation as the herd commands show it."""

    id: int
    session_id: str
    state: str
    reason: str
    created_at: datetime
    rendered: str
    hostname: str
    directory: str

    def line(self) -> str:
        """Return the escalation as one terminal line.

        Returns:
            Id, session, host and directory, age and reason.
        """
        return (
            f"{self.id}  session {self.session_id}  {self.hostname}:{self.directory}"
            f"  {self.created_at:%Y-%m-%d %H:%M:%S}\n    {' '.join(self.reason.split())[:300]}"
        )


async def sessions(db: Db, active_only: bool = True) -> list[SessionRow]:
    """List the sessions of all hosts, newest first.

    Args:
        db: A connection with read access to `sessions` (owner or `calu_operator`).
        active_only: Only sessions that have not ended.

    Returns:
        The sessions, newest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT s.id::text, h.hostname, s.os_user, s.directory, s.state, s.state_since,"
        " s.started_at, s.ended_at, s.claude_session_id,"
        " (SELECT count(*) FROM escalations e"
        "  WHERE e.session_id = s.id AND e.state = 'open') AS open_escalations"
        " FROM sessions s JOIN hosts h ON h.id = s.host_id"
        " WHERE (NOT %s::boolean OR s.ended_at IS NULL) ORDER BY s.started_at DESC",
        (active_only,),
    )
    return [SessionRow(*row) for row in rows]


async def escalations(db: Db, session_id: str | None = None) -> list[EscalationRow]:
    """List the open escalations, oldest first.

    Args:
        db: A connection with read access to `escalations`.
        session_id: Only escalations of this session, or None for all.

    Returns:
        The open escalations, oldest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT e.id, e.session_id::text, e.state, e.reason, e.created_at,"
        " coalesce(e.context->>'rendered', ''), h.hostname, s.directory"
        " FROM escalations e JOIN sessions s ON s.id = e.session_id"
        " JOIN hosts h ON h.id = s.host_id"
        " WHERE e.state = 'open' AND (%s::uuid IS NULL OR e.session_id = %s::uuid)"
        " ORDER BY e.created_at",
        (session_id, session_id),
    )
    return [EscalationRow(*row) for row in rows]


async def resolve(db: Db, escalation_id: int, decision: str, note: str = "") -> bool:
    """Allow or deny an open escalation and notify the waiting session.

    Args:
        db: A connection whose role may call `calu_resolve_escalation` (the database owner or
            `calu_operator`).
        escalation_id: Id of the `escalations` row.
        decision: "allow" or "deny".
        note: Text for Claude and for the record.

    Returns:
        True if the escalation was open and is now resolved; False if it was already resolved,
        cancelled or does not exist.

    Raises:
        ValueError: If `decision` is neither "allow" nor "deny".
        psycopg.Error: On database errors, including a role that may not resolve.
    """
    if decision not in ("allow", "deny"):
        raise ValueError(f"decision must be allow or deny, not {decision!r}")
    state = "allowed" if decision == "allow" else "denied"
    return bool(
        await db.scalar(
            "SELECT calu_resolve_escalation(%s, %s, %s)", (escalation_id, state, note or None)
        )
    )
