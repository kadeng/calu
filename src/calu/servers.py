"""The rows of `calu serve` processes and their server tokens (agent/records/PLAN.md D242).

    calu serve (session login)                operator (`calu token`)
    ──────────────────────────                ───────────────────────
    record_start ──► servers row              mint ──► server_tokens row (hash only)
    token_label  ──► "is this hash valid?"    tokens, revoke
    file_request ──► session_requests row     running ──► the servers that have not ended
    record_end   ──► ended, tokens revoked

A server token lets a client of the server's relay (`calu.relay`) attach to every session of
that one server and ask it for new sessions. Only its SHA-256 is stored; the token is printed
once by `calu token mint --server`. It is valid until it is revoked or its server ends.
"""

import os
from dataclasses import dataclass
from datetime import datetime

from calu import machine
from calu.db import Db
from calu.session_requests import SessionRequest
from calu.tokens import new_token, token_hash


@dataclass(frozen=True)
class ServerRow:
    """One `calu serve` process as the database records it."""

    id: str
    hostname: str
    os_user: str
    pid: int
    machine: str | None
    relay_address: str
    """`ws://127.0.0.1:<port>` on the server's machine; "" for a server without a relay."""
    started_at: datetime
    ended_at: datetime | None

    def line(self) -> str:
        """Return the server as one terminal line.

        Returns:
            Id, user and host, pid, relay address and start time.
        """
        relay = self.relay_address or "no relay"
        ended = f"  ended {self.ended_at:%Y-%m-%d %H:%M:%S}" if self.ended_at else ""
        return (
            f"{self.id}  {self.os_user}@{self.hostname}  pid {self.pid}  {relay}"
            f"  since {self.started_at:%Y-%m-%d %H:%M:%S}{ended}"
        )


@dataclass(frozen=True)
class ServerTokenRow:
    """One server token as `calu token list` shows it (never the token itself)."""

    id: int
    server_id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool
    relay_address: str = ""

    def line(self) -> str:
        """Return the token as one terminal line.

        Returns:
            Id, state, server, relay address, when it was last used and its label.
        """
        used = f"{self.last_used_at:%Y-%m-%d %H:%M:%S}" if self.last_used_at else "never used"
        state = "revoked" if self.revoked else "valid"
        label = f"  {self.label}" if self.label else ""
        return (
            f"{self.id}  {state}  server {self.server_id}  {self.relay_address or '-'}  "
            f"{used}{label}"
        )


class StartRefused(Exception):
    """A `relay.start` the server did not carry out; the message is the reason for the client."""


@dataclass(frozen=True)
class Hosted:
    """One session the server runs, as the relay reaches it."""

    calu_session_id: str
    address: str
    """The session's own address, `ws://127.0.0.1:<port>`."""
    token: str
    """The session's own client token: made by `calu.run` for the session and kept in this
    process's memory; under `calu serve` it is never printed."""
    directory: str
    started_at: datetime


# ------------------------------------------------------------------ the server's side (session)


async def record_start(db: Db, hostname: str, os_user: str, relay_address: str) -> str:
    """Record this `calu serve` process in `servers`.

    Side effect: inserts the row, with this process's pid and process table
    (`calu.machine.here`).

    Args:
        db: A connection of the session login (or the owner).
        hostname: The host the server answers requests for.
        os_user: The OS user it answers requests for.
        relay_address: The relay's address, or "" without a relay.

    Returns:
        The id of the new row.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0038
            (`psycopg.errors.UndefinedFunction`).
    """
    row = await db.one(
        "SELECT calu_server_started(%s, %s, %s, %s, %s)::text",
        (hostname, os_user, os.getpid(), machine.here().text(), relay_address),
    )
    assert row is not None  # an INSERT ... RETURNING returns its row
    return str(row[0])


async def record_end(db: Db, server_id: str) -> bool:
    """End a server's row and revoke every token of it.

    Args:
        db: A connection of the session login (or the owner).
        server_id: The row's id.

    Returns:
        True if the row was open until now.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one("SELECT calu_server_ended(%s::uuid)", (server_id,))
    return bool(row and row[0])


async def token_label(db: Db, server_id: str, sha: str) -> str | None:
    """Ask whether a hash belongs to a valid token of a server that has not ended.

    Side effect: stamps the token's `last_used_at`.

    Args:
        db: A connection of the session login (or the owner).
        server_id: The server's row id.
        sha: SHA-256 of the token a client sent (`calu.tokens.token_hash`).

    Returns:
        The token's label ("" for none) if it is valid; None otherwise.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one("SELECT calu_server_token_ok(%s::uuid, %s)", (server_id, sha))
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def file_request(db: Db, server_id: str, sha: str, wanted: SessionRequest) -> int:
    """File a session request for the holder of a valid server token (D242 (4)).

    The request names the server's own host and OS user, whatever `wanted` says, so that the
    server it is meant for claims it; `requested_by` is "relay".

    Side effect: inserts a `session_requests` row; the trigger notifies the servers.

    Args:
        db: A connection of the session login (or the owner).
        server_id: The server's row id.
        sha: SHA-256 of the client's server token.
        wanted: What to start: directory, kind, ids, prompt, model, effort, project, label.

    Returns:
        The id of the new request.

    Raises:
        psycopg.errors.InsufficientPrivilege: If the token is not valid for that server.
        psycopg.Error: On other database errors (a check of the table refuses a request whose
            kind and ids do not fit together).
    """
    row = await db.one(
        "SELECT calu_relay_session_request(%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            server_id,
            sha,
            wanted.directory,
            wanted.kind,
            wanted.claude_session_id,
            wanted.fork_before_turn,
            wanted.prompt,
            wanted.model,
            wanted.effort,
            wanted.project,
            wanted.label,
        ),
    )
    assert row is not None
    return int(row[0])


# ------------------------------------------------------------------- the operator's side


async def running(
    db: Db, hostname: str | None = None, os_user: str | None = None
) -> list[ServerRow]:
    """List the servers that have not ended, newest first.

    Args:
        db: A connection of `calu_operator`, `calu_admin` or the owner.
        hostname: Only servers of this host; None for every host.
        os_user: Only servers of this OS user; None for every user.

    Returns:
        The rows. A server whose process died without ending its row is listed too; its pid
        and machine tell (`calu.herd.process_gone`).

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0038.
    """
    rows = await db.all(
        """SELECT id::text, hostname, os_user, pid, machine, relay_address, started_at, ended_at
             FROM servers
            WHERE ended_at IS NULL
              AND (%s::text IS NULL OR hostname = %s) AND (%s::text IS NULL OR os_user = %s)
            ORDER BY started_at DESC""",
        (hostname, hostname, os_user, os_user),
    )
    return [ServerRow(*row) for row in rows]


async def server(db: Db, server_id: str) -> ServerRow | None:
    """Read one server by its id or the beginning of it.

    Args:
        db: A connection of `calu_operator`, `calu_admin` or the owner.
        server_id: The id, or at least its first 4 characters.

    Returns:
        The row, or None if no server or several start with `server_id`.

    Raises:
        psycopg.Error: On database errors.
    """
    if len(server_id) < 4:
        return None
    rows = await db.all(
        """SELECT id::text, hostname, os_user, pid, machine, relay_address, started_at, ended_at
             FROM servers WHERE id::text LIKE %s ORDER BY started_at DESC LIMIT 2""",
        (server_id.replace("%", "").replace("_", "") + "%",),
    )
    if len(rows) != 1:
        return None
    return ServerRow(*rows[0])


async def mint(db: Db, server_id: str, label: str = "") -> tuple[int, str]:
    """Create a token for a server's relay.

    Only the token's hash is stored, so the returned token is the only copy.

    Args:
        db: A connection of `calu_operator`, `calu_admin` or the owner.
        server_id: The server's row id.
        label: What the token is for, e.g. "laptop".

    Returns:
        The id of the new row and the token.

    Raises:
        ValueError: If no server that still runs has that id.
        psycopg.Error: On database errors.
    """
    token = new_token()
    row = await db.one(
        """INSERT INTO server_tokens (server_id, token_sha256, label)
           SELECT s.id, %s, %s FROM servers s WHERE s.id = %s::uuid AND s.ended_at IS NULL
           RETURNING id""",
        (token_hash(token), label, server_id),
    )
    if row is None:
        raise ValueError(f"no running server with id {server_id}")
    return int(row[0]), token


async def tokens(db: Db) -> list[ServerTokenRow]:
    """Read every server token, newest first.

    Args:
        db: A connection of `calu_operator`, `calu_admin` or the owner.

    Returns:
        The rows; the tokens themselves are not stored and never returned.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        """SELECT t.id, t.server_id::text, t.label, t.created_at, t.last_used_at, t.revoked,
                  s.relay_address
             FROM server_tokens t JOIN servers s ON s.id = t.server_id
            ORDER BY t.id DESC"""
    )
    return [ServerTokenRow(*row) for row in rows]


async def revoke(db: Db, token_id: int) -> bool:
    """Revoke a server token; the relay closes its connections within seconds.

    Args:
        db: A connection of `calu_operator`, `calu_admin` or the owner.
        token_id: Id from `calu token list`.

    Returns:
        True if the token existed and was not revoked yet.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "UPDATE server_tokens SET revoked = true WHERE id = %s AND NOT revoked RETURNING id",
        (token_id,),
    )
    return row is not None
