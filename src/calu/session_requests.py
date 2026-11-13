"""Requests for a session: the rows a `calu serve` process answers (PLAN.md D208).

    requester (operator login)                      calu serve (session login)
    file ──► session_requests row, state pending
                                                    claim ──► claimed
                                                    start the session
                                                    finish ──► started (session id) | failed
    wait_for: reads the row until it is finished
    withdraw: pending ──► withdrawn, when no server claimed it in time

The states and who may change them are enforced by the database (migration 0032). This module
only reads and writes the rows; it starts nothing.
"""

import time
from dataclasses import dataclass
from typing import Literal

import anyio

from calu.db import Db

Kind = Literal["new", "resume", "fork"]
State = Literal["pending", "claimed", "started", "failed", "withdrawn"]

CLAIM_WAIT_S = 10.0
"""How long a requester waits for a server to claim its request before it withdraws it. A
running server claims within moments: it is notified, and looks every `calu.serve.POLL_S`."""

START_WAIT_S = 180.0
"""How long a requester waits for a claimed request to be finished: the server connects to
the database twice, loads the settings and starts Claude Code."""

POLL_S = 0.25
"""Time between two reads of a request a requester waits for."""

COLUMNS = (
    "id, hostname, os_user, directory, kind, claude_session_id, fork_before_turn, prompt, "
    "model, effort, project, label, state, session_id::text, message"
)


class NoServer(Exception):
    """No `calu serve` claimed the request in time; it was withdrawn."""


@dataclass(frozen=True)
class SessionRequest:
    """One request for a session.

    `kind`: "new" starts a new conversation; "resume" continues `claude_session_id` (with
    `fork_before_turn`: a branch of it that starts before that turn); "fork" continues a copy
    of `claude_session_id`. `id`, `state`, `session_id` and `message` are filled by the
    database and the server: a request that is being filed needs none of them.
    """

    hostname: str
    os_user: str
    directory: str
    kind: Kind = "new"
    claude_session_id: str | None = None
    fork_before_turn: int | None = None
    prompt: str | None = None
    model: str | None = None
    effort: str | None = None
    project: str | None = None
    label: str = ""
    id: int = 0
    state: State = "pending"
    session_id: str | None = None
    message: str = ""


def _request(row: tuple) -> SessionRequest:
    """Build a request from a row read with `COLUMNS`.

    Args:
        row: The row.

    Returns:
        The request.
    """
    (
        request_id,
        hostname,
        os_user,
        directory,
        kind,
        claude_session_id,
        fork_before_turn,
        prompt,
        model,
        effort,
        project,
        label,
        state,
        session_id,
        message,
    ) = row
    return SessionRequest(
        hostname=hostname,
        os_user=os_user,
        directory=directory,
        kind=kind,
        claude_session_id=claude_session_id,
        fork_before_turn=fork_before_turn,
        prompt=prompt,
        model=model,
        effort=effort,
        project=project,
        label=label,
        id=request_id,
        state=state,
        session_id=session_id,
        message=message,
    )


async def file(db: Db, request: SessionRequest) -> int:
    """Write a request; the servers are notified by the database.

    Args:
        db: A connection of the operator or the owner.
        request: What to start. `id`, `state`, `session_id` and `message` are ignored.

    Returns:
        The id of the new row.

    Raises:
        psycopg.Error: On database errors, among them a combination of `kind`,
            `claude_session_id` and `fork_before_turn` that the table refuses.
    """
    return int(
        await db.scalar(
            """INSERT INTO session_requests
                   (hostname, os_user, directory, kind, claude_session_id, fork_before_turn,
                    prompt, model, effort, project, label)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                request.hostname,
                request.os_user,
                request.directory,
                request.kind,
                request.claude_session_id,
                request.fork_before_turn,
                request.prompt,
                request.model,
                request.effort,
                request.project,
                request.label,
            ),
        )
    )


async def read(db: Db, request_id: int) -> SessionRequest | None:
    """Read one request.

    Args:
        db: A connection of the operator or the owner.
        request_id: The row's id.

    Returns:
        The request, or None if there is no such row.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(f"SELECT {COLUMNS} FROM session_requests WHERE id = %s", (request_id,))
    return None if row is None else _request(row)


async def claim(db: Db, hostname: str, os_user: str, pid: int) -> SessionRequest | None:
    """Take the oldest pending request for this host and user, if there is a recent one.

    Each request is claimed at most once, also when several servers ask at the same moment.

    Args:
        db: A connection with the session login.
        hostname: This machine's host name.
        os_user: The OS user the server starts sessions for.
        pid: The server's process id, recorded with the claim.

    Returns:
        The request, now in the state "claimed", or None if nothing is pending.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        f"SELECT {COLUMNS} FROM calu_claim_session_request(%s, %s, %s)", (hostname, os_user, pid)
    )
    return None if row is None else _request(row)


async def finish(
    db: Db,
    request_id: int,
    outcome: Literal["started", "failed"],
    session_id: str | None = None,
    message: str = "",
) -> bool:
    """Write the outcome of a claimed request.

    Args:
        db: A connection with the session login.
        request_id: The row's id.
        outcome: "started" or "failed".
        session_id: The id of the `sessions` row of the started session, or None.
        message: Why it failed, in words for the requester.

    Returns:
        False if the request was not in the state "claimed" (it was finished before).

    Raises:
        psycopg.Error: On database errors.
    """
    return bool(
        await db.scalar(
            "SELECT calu_finish_session_request(%s, %s, %s::uuid, %s)",
            (request_id, outcome, session_id, message),
        )
    )


async def withdraw(db: Db, request_id: int) -> bool:
    """Take back a request that no server has claimed.

    Args:
        db: A connection of the operator or the owner.
        request_id: The row's id.

    Returns:
        False if the request is not pending any more (a server claimed it meanwhile).

    Raises:
        psycopg.Error: On database errors.
    """
    return bool(await db.scalar("SELECT calu_withdraw_session_request(%s)", (request_id,)))


async def wait_for(
    db: Db,
    request_id: int,
    claim_wait_s: float | None = None,
    start_wait_s: float | None = None,
) -> SessionRequest:
    """Wait until a request is finished, and withdraw it when no server takes it.

        pending for claim_wait_s ──► withdraw ──► NoServer
        claimed for start_wait_s ──► TimeoutError (the request stays with the server)
        started | failed         ──► returned

    Args:
        db: A connection of the operator or the owner.
        request_id: The row's id.
        claim_wait_s: How long to wait for a server to claim the request; None uses
            `CLAIM_WAIT_S`.
        start_wait_s: How long to wait, after the claim, for the outcome; None uses
            `START_WAIT_S`.

    Returns:
        The request in the state "started" or "failed".

    Raises:
        NoServer: If no server claimed the request in time; it was withdrawn.
        TimeoutError: If a server claimed the request and did not finish it in time.
        LookupError: If the row does not exist (any more).
        psycopg.Error: On database errors.
    """
    claim_wait_s = CLAIM_WAIT_S if claim_wait_s is None else claim_wait_s
    start_wait_s = START_WAIT_S if start_wait_s is None else start_wait_s
    filed = time.monotonic()
    claimed: float | None = None
    while True:
        request = await read(db, request_id)
        if request is None:
            raise LookupError(f"session request {request_id} does not exist")
        if request.state in ("started", "failed"):
            return request
        if request.state == "withdrawn":
            raise NoServer
        now = time.monotonic()
        if request.state == "pending":
            if now - filed > claim_wait_s and await withdraw(db, request_id):
                raise NoServer
        else:
            claimed = claimed or now
            if now - claimed > start_wait_s:
                raise TimeoutError(
                    f"the server claimed session request {request_id} and did not answer "
                    f"within {start_wait_s:.0f} s"
                )
        await anyio.sleep(POLL_S)
