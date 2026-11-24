"""The rows of `calu serve` processes and their server tokens (agent/records/PLAN.md D242).

    calu serve (user login)                      a client (`calu token`, user login)
    ───────────────────────                      ───────────────────────────────────
    record_start    ──► servers row              mint ──► server_tokens row (hash only)
    beat            ──► last_seen, every 10 s    tokens, revoke
    end_gone_peers  ──► dead servers' rows ended running ──► the servers that run
    token_label     ──► "is this hash valid?"
    file_request    ──► session_requests row
    session_details ──► names and state times
    record_end      ──► ended, tokens revoked

A server token lets a client of the server's relay (`calu.relay`) attach to every session of
that one server and ask it for new sessions. Only its SHA-256 is stored; the token is printed
once by `calu token mint --server`. It is valid until it is revoked or its server stops
running.

A server runs while its row has not ended and is not stale (agent/records/PLAN.md D244, the
rules of sessions in D237):

    ended_at set                                    ──► not running (it said so)
    no `last_seen` stamp for `herd.STALE_AFTER_S`   ──► stale: not running
    same process table as the reader, no such pid   ──► gone: not running
                                                        (`herd.process_gone`)

The database applies the first two in its functions (migration 0040), so that the session
login, which cannot read `servers`, gets the same answer; the third needs a look at the
process table, which only a process on the server's machine can take.
"""

import os
from dataclasses import dataclass, replace
from datetime import datetime

from calu import herd, machine
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
    projects_directory: str = ""
    """The resolved directory it starts sessions in or below (D243); "" if not recorded."""
    silent_for_s: float = 0.0
    """Seconds since the server's last heartbeat (`servers.last_seen`), measured by the
    database."""
    gone: bool = False
    """Whether the reader found the server's process gone (`herd.process_gone`); False when
    that cannot be judged from the reader's side."""

    @property
    def stale(self) -> bool:
        """Whether the server's row has not ended but its process is not running any more.

        Returns:
            True if the row has not ended and the process is gone or has been silent for
            longer than `herd.STALE_AFTER_S`.
        """
        return self.ended_at is None and (self.gone or self.silent_for_s > herd.STALE_AFTER_S)

    @property
    def live(self) -> bool:
        """Whether the server counts as running (module docstring).

        Returns:
            True if the row has not ended and is not stale.
        """
        return self.ended_at is None and not self.stale

    def line(self) -> str:
        """Return the server as one terminal line.

        Returns:
            Id, user and host, pid, relay address, projects directory, start time, and the end
            or a mark for a server that stopped running without ending its row.
        """
        relay = self.relay_address or "no relay"
        ended = f"  ended {self.ended_at:%Y-%m-%d %H:%M:%S}" if self.ended_at else ""
        if self.stale:
            ended = (
                "  GONE (its process is not running)"
                if self.gone
                else f"  SILENT for {self.silent_for_s / 60:.0f} min"
            )
        projects = f"  projects {self.projects_directory}" if self.projects_directory else ""
        return (
            f"{self.id}  {self.os_user}@{self.hostname}  pid {self.pid}  {relay}{projects}"
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
    server_ended: bool = False
    """Whether the server's row has ended (its tokens are then revoked as well)."""
    server_silent_for_s: float = 0.0
    """Seconds since the server's last heartbeat, measured by the database."""
    server_gone: bool = False
    """Whether the reader found the server's process gone (`herd.process_gone`)."""

    @property
    def state(self) -> str:
        """Say whether the relay accepts the token, and if not, why (D244 (3)).

        Returns:
            "revoked"; "server ended"; "server gone" (its process is not running, the row
            not ended); "server silent for N min" (no heartbeat for `herd.STALE_AFTER_S`:
            refused until the server beats again); otherwise "valid". The words match
            `ServerRow.line`.
        """
        if self.revoked:
            return "revoked"
        if self.server_ended:
            return "server ended"
        if self.server_gone:
            return "server gone"
        if self.server_silent_for_s > herd.STALE_AFTER_S:
            return f"server silent for {self.server_silent_for_s / 60:.0f} min"
        return "valid"

    def line(self) -> str:
        """Return the token as one terminal line.

        Returns:
            Id, state (`state`), server, relay address, when it was last used and its label.
        """
        used = f"{self.last_used_at:%Y-%m-%d %H:%M:%S}" if self.last_used_at else "never used"
        state = self.state
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


async def record_start(
    db: Db, hostname: str, os_user: str, relay_address: str, projects_directory: str = ""
) -> str:
    """Record this `calu serve` process in `servers`.

    Side effect: inserts the row, with this process's pid and process table
    (`calu.machine.here`).

    Args:
        db: A connection of the user login (or the owner).
        hostname: The host the server answers requests for.
        os_user: The OS user it answers requests for.
        relay_address: The relay's address, or "" without a relay.
        projects_directory: The resolved directory it serves (D243); "" for none recorded.

    Returns:
        The id of the new row.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0039
            (`psycopg.errors.UndefinedFunction`).
    """
    row = await db.one(
        "SELECT calu_server_started(%s, %s, %s, %s, %s, %s)::text",
        (
            hostname,
            os_user,
            os.getpid(),
            machine.here().text(),
            relay_address,
            projects_directory,
        ),
    )
    assert row is not None  # an INSERT ... RETURNING returns its row
    return str(row[0])


async def record_end(db: Db, server_id: str) -> bool:
    """End a server's row and revoke every token of it.

    Args:
        db: A connection of the user login (or the owner).
        server_id: The row's id.

    Returns:
        True if the row was open until now.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one("SELECT calu_server_ended(%s::uuid)", (server_id,))
    return bool(row and row[0])


async def beat(db: Db, server_id: str) -> bool:
    """Stamp the server's heartbeat (`servers.last_seen`), every `herd.HEARTBEAT_S` seconds.

    A row that is not stamped for `herd.STALE_AFTER_S` counts as not running: it claims no
    request, keeps no other server from the requests in its directory, and its tokens are
    refused (migration 0040).

    Args:
        db: A connection of the user login (or the owner).
        server_id: The server's row id.

    Returns:
        True if the row is open; False if it has ended (another server found this one gone,
        or an operator ended it).

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040
            (`psycopg.errors.UndefinedFunction`).
    """
    row = await db.one("SELECT calu_server_beat(%s::uuid)", (server_id,))
    return bool(row and row[0])


async def end_gone_peers(db: Db, server_id: str) -> list[str]:
    """End the rows of the other servers of this host and user whose process is gone.

    The database lists the open rows of the same host, OS user and process table
    (`calu_server_peers`); for each, this process looks up the pid (`herd.process_gone`), and
    a row whose process is gone is ended with its tokens revoked (`calu_server_gone`, which
    checks the same process table again). A request in such a server's directory is then
    claimed by another server at once, without waiting until the row is stale.

    Args:
        db: A connection of the user login (or the owner).
        server_id: This server's row id.

    Returns:
        The ids of the rows that were ended.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040.
    """
    this = machine.here()
    ended = []
    for other, pid, recorded in await db.all(
        "SELECT id::text, pid, machine FROM calu_server_peers(%s::uuid)", (server_id,)
    ):
        if herd.process_gone(pid, recorded, this):
            row = await db.one(
                "SELECT calu_server_gone(%s::uuid, %s::uuid)", (server_id, str(other))
            )
            if row and row[0]:
                ended.append(str(other))
    return ended


async def session_details(
    db: Db, server_id: str, sha: str, session_ids: list[str]
) -> dict[str, tuple[str, datetime]]:
    """Read the names and state times of this server's sessions, for `relay.sessions`.

    A login without row-level access to other sessions' rows (`calu_session` before
    migration 0043) could not read them, so the
    database answers through `calu_server_sessions` for the holder of a valid token of a
    running server, and only about the sessions of that server's process.

    Args:
        db: A connection of the user login (or the owner).
        server_id: The server's row id.
        sha: SHA-256 of the client's server token.
        session_ids: The session row ids to describe.

    Returns:
        Session id -> (name, "" for none; when the session entered its current state). A
        session the database does not describe (another process's, ended, or an invalid
        token) is missing.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040.
    """
    if not session_ids:
        return {}
    rows = await db.all(
        "SELECT id::text, name, state_since FROM calu_server_sessions(%s::uuid, %s, %s::uuid[])",
        (server_id, sha, session_ids),
    )
    return {str(session_id): (str(name), since) for session_id, name, since in rows}


async def token_label(db: Db, server_id: str, sha: str) -> str | None:
    """Ask whether a hash belongs to a valid token of a server that has not ended.

    Side effect: stamps the token's `last_used_at`.

    Args:
        db: A connection of the user login (or the owner).
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
        db: A connection of the user login (or the owner).
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


COLUMNS = """id::text, hostname, os_user, pid, machine, relay_address, started_at, ended_at,
              projects_directory, extract(epoch FROM (now() - last_seen))::float8"""
"""The columns of a `ServerRow`, in its order; the database measures the silence."""


def _judged(rows: list[tuple], this: machine.Machine) -> list[ServerRow]:
    """Build the rows and mark those whose process this machine knows to be gone.

    Args:
        rows: Rows of `COLUMNS`.
        this: The reader's process table (`calu.machine.here`).

    Returns:
        The rows; `gone` is set for an open row of this process table without its process.
    """
    built = [ServerRow(*row) for row in rows]
    return [
        replace(row, gone=True)
        if row.ended_at is None and herd.process_gone(row.pid, row.machine, this)
        else row
        for row in built
    ]


async def running(
    db: Db, hostname: str | None = None, os_user: str | None = None
) -> list[ServerRow]:
    """List the servers that run, newest first.

    A server runs while its row has not ended, it stamped `last_seen` within
    `herd.STALE_AFTER_S`, and its process is not known to be gone (`herd.process_gone`:
    the same process table as this process and no such pid). A server that died without
    ending its row is left out.

    Args:
        db: A connection of the user login or the owner.
        hostname: Only servers of this host; None for every host.
        os_user: Only servers of this OS user; None for every user.

    Returns:
        The rows.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040
            (`psycopg.errors.UndefinedColumn`).
    """
    rows = await db.all(
        f"""SELECT {COLUMNS}
             FROM servers
            WHERE ended_at IS NULL AND last_seen > now() - make_interval(secs => %s)
              AND (%s::text IS NULL OR hostname = %s) AND (%s::text IS NULL OR os_user = %s)
            ORDER BY started_at DESC""",
        (herd.STALE_AFTER_S, hostname, hostname, os_user, os_user),
    )
    return [row for row in _judged(rows, machine.here()) if row.live]


async def server(db: Db, server_id: str) -> ServerRow | None:
    """Read one server by its id or the beginning of it, whether it runs or not.

    Args:
        db: A connection of the user login or the owner.
        server_id: The id, or at least its first 4 characters.

    Returns:
        The row (`ServerRow.live` says whether it runs), or None if no server or several
        start with `server_id`.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040.
    """
    if len(server_id) < 4:
        return None
    rows = await db.all(
        f"""SELECT {COLUMNS}
             FROM servers WHERE id::text LIKE %s ORDER BY started_at DESC LIMIT 2""",
        (server_id.replace("%", "").replace("_", "") + "%",),
    )
    if len(rows) != 1:
        return None
    return _judged(rows, machine.here())[0]


async def mint(db: Db, server_id: str, label: str = "") -> tuple[int, str]:
    """Create a token for a server's relay.

    Only the token's hash is stored, so the returned token is the only copy.

    Args:
        db: A connection of the user login or the owner.
        server_id: The server's row id.
        label: What the token is for, e.g. "laptop".

    Returns:
        The id of the new row and the token.

    Raises:
        ValueError: If no server that still runs has that id: its row has ended or is stale
            (the caller checks `ServerRow.gone`, which needs a look at the process table).
        psycopg.Error: On database errors.
    """
    token = new_token()
    row = await db.one(
        """INSERT INTO server_tokens (server_id, token_sha256, label)
           SELECT s.id, %s, %s FROM servers s
            WHERE s.id = %s::uuid AND s.ended_at IS NULL
              AND s.last_seen > now() - make_interval(secs => %s)
           RETURNING id""",
        (token_hash(token), label, server_id, herd.STALE_AFTER_S),
    )
    if row is None:
        raise ValueError(f"no running server with id {server_id}")
    return int(row[0]), token


async def tokens(db: Db) -> list[ServerTokenRow]:
    """Read every server token, newest first.

    Args:
        db: A connection of the user login or the owner.

    Returns:
        The rows, each with its server's state (`ServerTokenRow.state`): a token of a server
        that died without ending its row is refused, and its row says so. The tokens
        themselves are not stored and never returned.

    Raises:
        psycopg.Error: On database errors, among them a schema older than migration 0040
            (`psycopg.errors.UndefinedColumn`).
    """
    rows = await db.all(
        """SELECT t.id, t.server_id::text, t.label, t.created_at, t.last_used_at, t.revoked,
                  s.relay_address, s.ended_at IS NOT NULL,
                  extract(epoch FROM (now() - s.last_seen))::float8, s.pid, s.machine
             FROM server_tokens t JOIN servers s ON s.id = t.server_id
            ORDER BY t.id DESC"""
    )
    this = machine.here()
    return [
        ServerTokenRow(
            *row[:9],
            server_gone=not row[7] and herd.process_gone(row[9], row[10], this),
        )
        for row in rows
    ]


async def revoke(db: Db, token_id: int) -> bool:
    """Revoke a server token; the relay closes its connections within seconds.

    Args:
        db: A connection of the user login or the owner.
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
