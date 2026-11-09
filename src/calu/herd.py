"""Watching and unblocking sessions through the database (PLAN.md §8, REQUIREMENTS 12, 13).

These queries need no running session: they work on the database alone, from any machine that
can reach it. `resolve` calls `calu_resolve_escalation`, which notifies the waiting session, so
the session continues within a second.

`calu sessions`, `calu escalations`, `calu resolve` and `calu token` use the `admin` credential
for now. A separate login for the role `calu_operator` (reads everything, resolves escalations,
mints and revokes client tokens, changes nothing else) is still open (PLAN.md Q4).

`calu token` is the other half of D30: a session accepts every non-revoked token of its own
session, so a token minted here lets a client (an editor's ACP adapter, a local command, a
remote interface) attach without the terminal that started the session, and revoking it closes
that access again.
"""

from dataclasses import dataclass
from datetime import datetime

from calu.db import Db
from calu.tokens import new_token, token_hash


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
            Id, state, host and directory, the number of open escalations if there are any, and
            Claude's session id, which `calu run --resume` takes (D27).
        """
        waiting = f" ({self.open_escalations} open)" if self.open_escalations else ""
        resume = f"  resume {self.claude_session_id}" if self.claude_session_id else ""
        return (
            f"{self.id}  {self.state}{waiting}  {self.hostname}:{self.directory}"
            f"  since {self.state_since:%Y-%m-%d %H:%M:%S}{resume}"
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


@dataclass(frozen=True)
class TokenRow:
    """One client token as `calu token list` shows it (never the token itself)."""

    id: int
    session_id: str
    label: str
    address: str
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool

    def line(self) -> str:
        """Return the token as one terminal line.

        Returns:
            Id, state, session, address, label and when it was last used.
        """
        used = f"{self.last_used_at:%Y-%m-%d %H:%M:%S}" if self.last_used_at else "never used"
        state = "revoked" if self.revoked else "valid"
        label = f"  {self.label}" if self.label else ""
        return (
            f"{self.id}  {state}  session {self.session_id}  {self.address or '-'}  {used}{label}"
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


async def mint_token(db: Db, session_id: str, label: str = "") -> tuple[int, str]:
    """Create a client token for a session (PLAN.md D30).

    Only the token's hash is stored, so the returned token is the only copy: the caller prints
    it once. The session accepts it at its next `initialize`, without being restarted.

    Args:
        db: The connection (owner or `calu_operator`).
        session_id: Id of the `sessions` row (`calu sessions` prints it).
        label: What the token is for, e.g. "zed on my laptop"; shown by `calu token list`.

    Returns:
        The id of the new row and the token.

    Raises:
        ValueError: If no session has that id.
        psycopg.Error: On database errors.
    """
    token = new_token()
    # INSERT ... SELECT inserts nothing for an unknown session, so the missing row is the error.
    row = await db.one(
        """INSERT INTO client_tokens (session_id, token_sha256, label, address)
           SELECT s.id, %s, %s, coalesce(s.address, '') FROM sessions s WHERE s.id = %s::uuid
           RETURNING id""",
        (token_hash(token), label, session_id),
    )
    if row is None:
        raise ValueError(f"no session with id {session_id}")
    return int(row[0]), token


async def list_tokens(db: Db, session_id: str | None = None) -> list[TokenRow]:
    """Read the client tokens of one session or of all sessions, newest first.

    Args:
        db: The connection.
        session_id: Which session; None lists the tokens of every session.

    Returns:
        The rows, newest first. The tokens themselves are not stored and never returned.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        """SELECT id, session_id::text, label, address, created_at, last_used_at, revoked
             FROM client_tokens
            WHERE %s::uuid IS NULL OR session_id = %s::uuid
            ORDER BY id DESC""",
        (session_id, session_id),
    )
    return [
        TokenRow(
            id=int(row[0]),
            session_id=str(row[1]),
            label=str(row[2]),
            address=str(row[3]),
            created_at=row[4],
            last_used_at=row[5],
            revoked=bool(row[6]),
        )
        for row in rows
    ]


async def revoke_token(db: Db, token_id: int) -> bool:
    """Revoke a client token, so that no client can attach with it any more.

    A client that is already connected keeps its connection: the token is checked at
    `initialize`, not per request. Stopping such a client needs `session.stop` or the end of the
    calu process.

    Args:
        db: The connection (owner or `calu_operator`).
        token_id: Id from `calu token list`.

    Returns:
        True if the token existed and was not revoked yet.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "UPDATE client_tokens SET revoked = true WHERE id = %s AND NOT revoked RETURNING id",
        (token_id,),
    )
    return row is not None
