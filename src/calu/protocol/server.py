"""The calu protocol server: JSON-RPC 2.0 over WebSocket, for several clients at once.

    client ──{"id":1,"method":"chat.send",...}──► Client._handle ──► Session
    client ◄──{"id":1,"result":{}}───────────────┘
    client ◄──{"method":"tool.use","params":{...,"seq":42}}── Bus (after events.subscribe)

One `SessionServer` serves one session. Each WebSocket connection gets a `Client`, which

1. accepts only `initialize` until the client has shown a valid token (`calu.tokens`),
2. handles the requests of that connection one after another, so a client may send
   `chat.send` right after `session.start` without waiting,
3. finishes the requests it has read when the client disconnects (only their responses are
   dropped), so the session is never left halfway through a start or a stop, and
4. after `events.subscribe` pushes the session's events to the client as notifications.

Control requests (`CONTROL_METHODS`) are handled next to a running request, so that
`escalation.decide`, `interrupt` and `shutdown` work while a slow request (`session.start`) is
still running, and a side question (`side.ask`) does not hold up the client's other requests
(a client has one side question answered at a time; a second one meanwhile is refused):
the loop that reads the connection never waits for a request, it hands the ordinary ones to
one worker task per client and starts a task for each control request.
Requests of different clients are independent; the session itself serializes what must not
overlap.

Error codes (JSON-RPC 2.0): -32700 parse error, -32600 invalid request, -32601 unknown method,
-32602 invalid params, -32000 request failed (`SessionError`, `ValueError`), -32603 internal,
and -32001 not authenticated (no valid `initialize` yet, or the token was revoked since).

The server closes a connection itself in two cases, each with its own WebSocket close code
(`calu.protocol.messages`):

    CLOSE_TOKEN_REVOKED (4001)  the client's token was revoked, or it was minted for a
                                session that has ended. The token is refused from now on,
                                so connecting again with it does not help.
    CLOSE_FELL_BEHIND (4002)    the client's event queue overflowed (`calu.bus`). The client
                                connects again and subscribes after the last sequence number
                                it received.

Token revocation (PLAN.md D30) and open connections: a token is checked against the database
at `initialize`, again before every request that is not in `READ_ONLY_METHODS`, every
`REVALIDATE_S` seconds for every open connection (`SessionServer.sweep`), when the session
ends and when a new one has started. A connection whose token was revoked is closed. See
`SessionServer.observe` for what counts as revoked.

While the database gives no answer (an error of any kind, or no answer within
`TOKEN_CHECK_S`), the last known answer stands: an authenticated connection keeps working, the
printed token still attaches unless it was seen revoked, and a minted token cannot attach.
"""

import json
import logging
import secrets
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import psycopg
import psycopg.errors
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed

from calu.bus import Bus, Subscription
from calu.protocol.messages import (
    CLOSE_FELL_BEHIND,
    CLOSE_TOKEN_REVOKED,
    REQUESTS,
    AutoEscalate,
    AutoEscalateSetParams,
    ChatSendParams,
    CommentAddParams,
    CommentAddResult,
    EscalationContextParams,
    EscalationDecideParams,
    Event,
    EventsSubscribeParams,
    EventsSubscribeResult,
    HonestyClearParams,
    InitializeParams,
    ModelSetParams,
    SessionHistoryParams,
    SessionListParams,
    SessionListResult,
    SessionStartParams,
    SessionState,
    SideAskParams,
    SideAskResult,
)
from calu.session import Session, SessionError
from calu.tokens import token_hash

log = logging.getLogger(__name__)

CONTROL_METHODS = frozenset(
    {"escalation.decide", "interrupt", "shutdown", "state.get", "side.ask", "usage.get"}
)
"""Requests that are handled next to a running request of the same client."""

NO_AUTH_METHODS = frozenset({"initialize"})
"""Requests a client may send before it has shown a valid token."""

UNAUTHENTICATED = -32001
"""JSON-RPC error code for a request before a successful `initialize`, and for a request of a
client whose token was revoked after it."""

READ_ONLY_METHODS = frozenset(
    {
        "initialize",
        "state.get",
        "events.subscribe",
        "session.list",
        "session.history",
        "escalation.context",
        "checkers.get",
        "context.get",
        "usage.get",
    }
)
"""Requests that change nothing in the session. Before **every other** request the client's
token is checked against the database again, so a revoked token cannot decide an escalation,
send a message or stop the process, even in the seconds before `SessionServer.sweep` closes its
connection. A request that is added to the protocol is checked unless it is listed here."""

REVALIDATE_S = 15.0
"""Seconds between two checks of the tokens of all open connections (`SessionServer.sweep`).
One check is one small database call per distinct token."""

TOKEN_CHECK_S = 2.0
"""Seconds a request or an `initialize` waits for the database's answer about a token. After
that the last known answer is used (`SessionServer.still_valid`, `SessionServer.authenticate`);
the question stays asked, and its answer is applied when it arrives."""

END_CHECK_S = 5.0
"""Upper bound in seconds for the token checks at the end of a session
(`SessionServer.session_ended`). The session waits that long before it goes on ending; a token
that the database did not confirm in that time does not outlive the session row."""

REQUEST_BACKLOG = 256
"""Ordinary requests of one client that may wait for the one that is running. Past that the
read loop waits (and with it the client's control requests), so a client cannot make the
session buffer requests without bound."""

ENDED_WITH_SESSION = "the client token belonged to a session that has ended: attach again"
"""Close reason for a minted token whose connection outlived its session row."""

GRACE_S = 0.1
"""How long `listen` keeps the connections open after a `shutdown` request, so that the events
and responses already written reach the clients."""


FIRST_CHECK_S = 1.0
"""Seconds after the start of `listen` until the first `SessionServer.sweep`. It confirms the
printed token soon after `calu run` recorded it, and records it if that did not happen
(`SessionServer.observe`)."""

MAX_SOCKET_PATH = 100
"""Bytes a Unix socket path may have. The kernel's limit is 108 including the terminating zero
(`sun_path`), and a few bytes of margin keep the message friendlier than `AF_UNIX path too
long`."""


def check_socket_path(socket_path: Path) -> None:
    """Refuse a Unix socket path the kernel cannot bind.

    Args:
        socket_path: The path to check.

    Raises:
        OSError: If the path is longer than `MAX_SOCKET_PATH` bytes. The message names the
            length and suggests `$XDG_RUNTIME_DIR`, because the usual cause is a socket under a
            deeply nested directory.
    """
    length = len(str(socket_path).encode())
    if length > MAX_SOCKET_PATH:
        raise OSError(
            f"the socket path is {length} bytes, and a Unix socket allows about "
            f"{MAX_SOCKET_PATH}: put it somewhere shorter, for example under $XDG_RUNTIME_DIR "
            f"({socket_path})"
        )


def _prepare_socket(socket_path: Path) -> None:
    """Make the directory of a Unix socket and clear a stale socket file.

    A directory that has to be created gets mode 0700; one that already exists is not touched,
    because it may be a shared directory such as `/tmp` whose mode is neither calu's business
    nor changeable. Runs in a worker thread, because it touches the file system.

    Args:
        socket_path: Where the server will bind.

    Raises:
        OSError: If the path is too long for a Unix socket, the directory cannot be created, or
            the stale socket cannot be removed.
    """
    check_socket_path(socket_path)
    if not socket_path.parent.exists():
        # A directory calu makes is calu's own, so it gets 0700. One that already exists is left
        # alone: it may be `/tmp`, whose mode cannot be changed and is none of calu's business.
        # What protects the socket either way is its own mode, set once it is bound.
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.parent.chmod(0o700)
    if socket_path.exists():
        log.info("replacing the stale socket %s", socket_path)
        socket_path.unlink()


def _remove_socket(socket_path: Path) -> None:
    """Remove a Unix socket file, ignoring one that is already gone.

    Args:
        socket_path: The socket to remove.
    """
    socket_path.unlink(missing_ok=True)


def encode_event(event: Event) -> str:
    """Return the JSON-RPC notification text for `event`.

    Args:
        event: The event; its `method` becomes the notification method, its fields the params
            (including the sequence number `seq`).

    Returns:
        One JSON object; non-ASCII characters are not escaped.
    """
    return json.dumps(
        {"jsonrpc": "2.0", "method": event.method, "params": event.model_dump(mode="json")},
        ensure_ascii=False,
    )


class RpcError(Exception):
    """A request error that is answered with the given JSON-RPC error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        """Create the error.

        Args:
            code: JSON-RPC error code (see the module docstring).
            message: Error message for the client.
            data: Optional JSON-serializable details, sent as the error's "data".
        """
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class TokenCheckUnavailable(Exception):
    """The database gave no answer about a client token: an error, or none in time."""


class _Check:
    """One question to the database about one token hash, shared by everyone who waits for it."""

    def __init__(self) -> None:
        """Create a check that has not been answered."""
        self.started = anyio.current_time()
        self.done = anyio.Event()
        self.valid: bool | None = None
        """The database's answer; None while it is open and when there was none."""


class SessionServer:
    """Serves one session to any number of WebSocket clients.

    `token_hash` is the SHA-256 of the token that `calu run` printed; a client must send that
    token, or one an operator minted for the session, in `initialize`. `shutdown` is set when a
    client asks the process to stop, so that `calu run` can end the session.
    """

    def __init__(
        self,
        session: Session,
        bus: Bus,
        token_hash: str,
        task_group: TaskGroup,
        revalidate_s: float = REVALIDATE_S,
        check_s: float = TOKEN_CHECK_S,
    ) -> None:
        """Create the server; `listen` starts it.

        Args:
            session: The session that requests are dispatched to.
            bus: The bus whose events the clients receive; the session's event sink must be
                `bus.publish`.
            token_hash: SHA-256 of the client token (`calu.tokens.token_hash`).
            task_group: The task group that runs the Claude session; `session.start` adds the
                session's own task to it, so it must outlive every Claude session.
            revalidate_s: Seconds between two checks of the tokens of the open connections.
            check_s: Seconds a request waits for the database's answer about its token.

        Side effects: registers `on_event` with the bus, so the server learns of the end and
        the start of a session at the moment the session reports it.
        """
        self.session = session
        self.bus = bus
        self.token_hash = token_hash
        self.task_group = task_group
        self.revalidate_s = revalidate_s
        self.check_s = check_s
        self.shutdown = anyio.Event()
        self.ready = anyio.Event()
        """Set by `listen` once the socket is bound and `address` holds the URL."""
        self.address = ""
        """The WebSocket address clients connect to, set by `listen`."""
        self._clients: set[Client] = set()
        self._confirmed: dict[str, str] = {}
        """Token hash -> id of the session row for which the database last called it valid."""
        self._revoked: dict[str, str] = {}
        """Hashes of tokens that were seen revoked, each with the reason its clients are told.
        They are refused for the rest of the process, also while the database is unreachable."""
        self._valid_at_end: dict[str, str] = {}
        """Token hash -> id of the last session row at whose end the token was still valid."""
        self._last_ended: str | None = None
        """Id of the session row whose end was checked last (`session_ended`)."""
        self._swept_row: str | None = None
        """Id of the session row for which a check of all tokens was last started on its
        start (`on_event`)."""
        self._checks: dict[str, _Check] = {}
        """The questions to the database that are not answered yet, by token hash."""
        self._asking = anyio.Lock()
        """Serializes the questions, so that two of them cannot record the same token twice."""
        bus.watch(self.on_event)

    async def listen(
        self, host: str = "127.0.0.1", port: int = 0, socket_path: Path | None = None
    ) -> None:
        """Accept clients until `shutdown` is set or the caller is cancelled.

        With `socket_path` the server binds a **Unix socket** instead of a TCP port (PLAN.md
        §6): the file system then decides who may connect, instead of every process on the
        machine being able to reach a loopback port. Its directory is created with mode 0700 and
        the socket itself with 0600, so only this user can reach it; a stale socket from a
        process that died is replaced.

        Side effects: sets `address` once the socket is bound (the port the operating system
        chose if `port` was 0, or `unix:<path>`) and then sets `ready`; with `socket_path`,
        creates the directory and the socket file and removes the socket when it stops.

        Args:
            host: Interface to bind; the default accepts only local clients (PLAN.md §6).
            port: TCP port, or 0 to let the operating system choose a free one.
            socket_path: Path of a Unix socket to bind instead; None binds TCP.

        Raises:
            OSError: If the address cannot be bound.
        """
        if socket_path is not None:
            await self._listen_unix(socket_path)
            return
        async with await serve(self._handle, host, port) as server:
            bound = server.sockets[0].getsockname()
            self.address = f"ws://{bound[0]}:{bound[1]}"
            self.ready.set()
            log.info("protocol server listening on %s", self.address)
            await self._until_shutdown()

    async def _until_shutdown(self) -> None:
        """Check the clients' tokens on a schedule until `shutdown` is set, then wait `GRACE_S`.

        The wait at the end lets the other clients' last events go out.
        """
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._revalidate)
            await self.shutdown.wait()
            tg.cancel_scope.cancel()
        await anyio.sleep(GRACE_S)

    async def _revalidate(self) -> None:
        """Run `sweep` after `FIRST_CHECK_S` and then every `revalidate_s` seconds. Never raises.

        Runs until it is cancelled.
        """
        await anyio.sleep(min(FIRST_CHECK_S, self.revalidate_s))
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("checking the client tokens failed")
            await anyio.sleep(self.revalidate_s)

    def _session_row(self) -> tuple[str | None, bool]:
        """Say which session row the store writes to and whether that session still runs.

        Returns:
            The id of the store's current session row (None before the first `session.start`)
            and whether the session is neither stopped nor failed. A session revokes its own
            tokens when it ends (D108), and it reports "stopped" or "failed" before it does so.
        """
        row = self.session.store.session_id
        return row, self.session.state not in ("stopped", "failed")

    async def on_event(self, event: Event) -> None:
        """React to the end and to the start of a session; the bus awaits this for every event.

        - `session.state` "stopped" or "failed": `session_ended` checks the tokens, before the
          session revokes them itself (D108).
        - `session.state` "idle" under a session row that was not checked yet: a `sweep`
          starts as a task, so every connection that stayed open from the session before is
          checked against the new session row at once and not only on the next schedule.

        Args:
            event: The event the session just emitted.
        """
        if not isinstance(event, SessionState):
            return
        if event.state in ("stopped", "failed"):
            await self.session_ended()
        elif event.state == "idle":
            row = self.session.store.session_id
            if row is not None and row != self._swept_row:
                self._swept_row = row
                self.task_group.start_soon(self._sweep_quietly)

    async def _sweep_quietly(self) -> None:
        """Run `sweep` and log what it raises. Never raises."""
        try:
            await self.sweep()
        except Exception:
            log.exception("checking the client tokens failed")

    async def session_ended(self) -> None:
        """Note which tokens are still valid at the end of the current session row.

        The session reports its final state before it revokes its own tokens (D108). A token
        that the database calls valid at this moment was not revoked by an operator, so it is
        noted as valid at the end of this row; `observe` lets only those tokens outlive the
        row. The printed token is asked about first, then the tokens of the open connections.
        A printed token that has no row for this session is given one here, which tells "never
        recorded" apart from "revoked" (`_record_printed`).

        Side effects: one or two database calls per token, at most `END_CHECK_S` seconds in
        all; `_last_ended` names the row afterwards. A token that could not be asked about is
        not noted, so it does not outlive the row. Never raises.
        """
        row = self.session.store.session_id
        if row is None:
            return
        hashes = [self.token_hash]
        hashes += sorted(
            {c.token_sha256 for c in self._clients if c.token_sha256 is not None}
            - {self.token_hash}
        )
        try:
            with anyio.move_on_after(END_CHECK_S):
                async with self._asking:
                    for sha in hashes:
                        if sha in self._revoked:
                            continue
                        # The rule of `_judge`: a token last confirmed for an earlier row
                        # outlives that row only if it was valid when the row ended.
                        home = self._confirmed.get(sha)
                        carried = home is not None and home != row
                        if (carried and self._valid_at_end.get(sha) != home) or (
                            home is None and self._last_ended is not None
                        ):
                            self._revoke(sha)
                            continue
                        await self._note_valid_at_end(sha, row)
        except Exception:
            log.exception("checking the client tokens at the end of the session failed")
        finally:
            self._last_ended = row

    async def _note_valid_at_end(self, sha: str, row: str) -> None:
        """Ask about one token at the end of session row `row` and note a yes.

        Args:
            sha: SHA-256 of a client token.
            row: Id of the session row that is ending.

        Raises:
            Exception: Whatever the store raises when the database cannot be asked.
        """
        store = self.session.store
        try:
            valid = await store.client_token_ok(sha)
            if not valid and self._is_printed(sha) and self._confirmed.get(sha) != row:
                valid = await self._record_printed(sha)
        except RuntimeError:
            return  # the store has no session row
        if valid and store.session_id == row:
            self._confirmed[sha] = row
            self._valid_at_end[sha] = row

    async def _record_printed(self, sha: str) -> bool:
        """Give the printed token a row for the store's current session row.

        `calu run` records the printed token for the session row it starts. For a later
        `session.start` in the same process, and when that first recording did not happen,
        the server records it, so that the token shows in `calu token list` and can be revoked
        there. A session row holds one row per token: if the insert is refused because the row
        exists, the database is asked once more, and a row that exists without being valid is
        a revoked one.

        Args:
            sha: SHA-256 of the printed token.

        Returns:
            True if the token now has a valid row for the current session row; False if it has
            a row there that is not valid.

        Raises:
            Exception: Whatever the store raises when the database cannot be asked.
        """
        store = self.session.store
        try:
            await store.add_client_token(sha, self.address)
        except psycopg.errors.UniqueViolation:
            return await store.client_token_ok(sha)
        return True

    async def observe(self, sha: str) -> bool:
        """Ask the database about one token hash and remember what the answer means.

        A token is good for the session row it was recorded for. In the table, "its row" is
        the session row for which the database last called the token valid, and "valid at the
        end" means that `session_ended` got a yes for it when that row ended.

            database says, session is   | token                    | effect
            ----------------------------+--------------------------+---------------------------
            valid                       | any                      | remembered as confirmed
            not valid, running its row  | any                      | revoked: an operator
                                        |                          | revoked it
            not valid, its row ended    | valid at the end         | kept: the session revoked
                                        |                          | it itself (D108), and the
                                        |                          | client may start the next
                                        | not valid at the end     | revoked
            not valid, running a later  | printed, valid at the    | recorded for the new row
            row                         | end of its row           | (`_record_printed`)
                                        | minted, or not valid at  | revoked: a minted token
                                        | the end of its row       | belongs to one session row
            not valid, never confirmed  | printed, no session      | recorded for the row, or
                                        | ended yet                | revoked if it has a row
                                        | printed, a session ended | revoked
                                        | any other                | nothing: not a token of
                                        |                          | this session

        "Revoked" is remembered for the whole process. Nothing is concluded from an answer
        during which the session row or the running state changed, nor for a row that has
        ended before `session_ended` is through with it; the next check decides.

        The session role can ask "is this hash valid" and nothing else (D28), so these rules
        are how an operator's revocation is told from the session's own clean-up. Where the
        two cannot be told apart, the token counts as revoked.

        Side effects: one database call (`calu_client_token_ok` stamps `last_used_at`), and the
        insert described above. Calls are serialized.

        Args:
            sha: SHA-256 of a client token.

        Returns:
            True if the database called the token valid for the current session row.

        Raises:
            Exception: Whatever the store raises when the database cannot be asked
                (`psycopg.Error`, `TimeoutError`, `OSError`). Nothing is remembered then.
        """
        async with self._asking:
            if sha in self._revoked:
                return False
            store = self.session.store
            row, running = self._session_row()
            try:
                valid = await store.client_token_ok(sha)
            except RuntimeError:
                # The database store refuses to be asked before the first `session.start`:
                # there is no session row yet, so there is no token row either.
                return False
            if valid:
                if row is not None:
                    self._confirmed[sha] = row
                return True
            if row is None or self._session_row() != (row, running):
                return False
            return await self._judge(sha, row, running)

    async def _judge(self, sha: str, row: str, running: bool) -> bool:
        """Apply the table of `observe` to a token the database did not call valid.

        Args:
            sha: SHA-256 of the token.
            row: Id of the session row the database was asked about.
            running: Whether that session was running, before and after the question.

        Returns:
            True if the printed token was recorded for `row` and is valid there now.

        Raises:
            Exception: Whatever the store raises when the printed token cannot be recorded.
        """
        printed = self._is_printed(sha)
        home = self._confirmed.get(sha)
        if home is None and not printed:
            return False
        if home == row:
            if running or (self._last_ended == row and self._valid_at_end.get(sha) != row):
                self._revoke(sha)
            return False
        if home is None:
            if self._last_ended is not None:
                self._revoke(sha)
                return False
        elif self._valid_at_end.get(sha) != home:
            self._revoke(sha)
            return False
        if not running:
            return False
        if not printed:
            self._revoke(sha, ENDED_WITH_SESSION)
            return False
        if not await self._record_printed(sha):
            self._revoke(sha)
            return False
        if self.session.store.session_id == row:
            self._confirmed[sha] = row
            return True
        return False

    def _revoke(self, sha: str, why: str = "the client token was revoked") -> None:
        """Remember a token as revoked for the rest of the process.

        Args:
            sha: SHA-256 of the token.
            why: The reason, for the log and for the clients whose connection is closed.
        """
        kind = "printed" if self._is_printed(sha) else "minted"
        log.warning("a %s client token is refused from now on: %s", kind, why)
        self._revoked[sha] = why

    def recorded(self, sha: str | None) -> str | None:
        """Return a token hash if the database holds it as a valid token of the current session.

        A decision is recorded in the database under the deciding client's own token
        (`Session.decide_escalation`). A client that was accepted without such a row (the
        printed token during a database outage) has none to record under, so the store's
        remembered token is used for it instead.

        Args:
            sha: SHA-256 of a client's token, or None.

        Returns:
            `sha` if the last `observe` confirmed it for the store's current session row, else
            None.
        """
        if sha is None or self._confirmed.get(sha) != self.session.store.session_id:
            return None
        return sha

    def _is_printed(self, sha: str) -> bool:
        """Tell whether a hash is the one of the token this process printed.

        Args:
            sha: SHA-256 of a client token.

        Returns:
            True if it equals `token_hash`; the comparison takes the same time either way.
        """
        return secrets.compare_digest(sha, self.token_hash)

    async def ask(self, sha: str) -> bool:
        """Get the database's answer about a token, waiting at most `check_s` seconds for it.

        The question runs as a task of the server's task group (`observe`), so it is neither
        cut short when the caller stops waiting nor cancelled with the caller's connection.
        Callers that ask about the same token while a question is open share it, and none of
        them waits past `check_s` seconds after the question was asked: while the database
        stays away, a request is not held up again and again. When the answer arrives it is
        applied, and the connections of a token that turned out revoked are closed.

        Args:
            sha: SHA-256 of a client token.

        Returns:
            What `observe` returned: True if the token is valid for the current session row.

        Raises:
            TokenCheckUnavailable: If there is no answer: the database call failed, however it
                failed, or it did not finish in time.
        """
        check = self._checks.get(sha)
        if check is None:
            check = self._checks[sha] = _Check()
            try:
                self.task_group.start_soon(self._run_check, sha, check)
            except RuntimeError as error:  # the task group has ended: the process is stopping
                del self._checks[sha]
                raise TokenCheckUnavailable("the session is ending") from error
        remaining = check.started + self.check_s - anyio.current_time()
        if remaining > 0:
            with anyio.move_on_after(remaining):
                await check.done.wait()
        if check.valid is None:
            raise TokenCheckUnavailable("the database gives no answer about the client token")
        return check.valid

    async def _run_check(self, sha: str, check: _Check) -> None:
        """Run one `observe` for `ask` and apply a revocation it found. Never raises.

        Side effects: sets `check.valid` unless the database gave no answer, sets `check.done`
        in every case (also when this task is cancelled), and closes the connections of a
        token that is revoked, `GRACE_S` after the answer, so that the error response of the
        request that asked goes out first.

        Args:
            sha: SHA-256 of the token.
            check: The shared record of this question.
        """
        try:
            check.valid = await self.observe(sha)
        except (psycopg.Error, TimeoutError, OSError) as error:
            log.warning("the database cannot be asked about a client token: %s", error)
        except Exception:
            log.exception("asking the database about a client token failed")
        finally:
            self._checks.pop(sha, None)
            check.done.set()
        if sha in self._revoked:
            await anyio.sleep(GRACE_S)
            await self._close_revoked()

    async def _close_revoked(self) -> None:
        """Close every open connection whose token is remembered as revoked. Never raises."""
        for client in list(self._clients):
            why = self._revoked.get(client.token_sha256 or "")
            if why is not None:
                try:
                    await client.revoke(why)
                except Exception:
                    log.exception("closing the connection of a revoked token failed")

    async def authenticate(self, token: str) -> str | None:
        """Decide whether a client may attach with `token` (`initialize`).

        The database is asked first, for every token (`ask`). A minted token needs the
        database's yes. The printed token is also accepted without it, unless it is remembered
        as revoked (`observe`): that covers a database that gives no answer (attaching with the
        printed token keeps working, D30), a session that has not started yet, and the time
        between two sessions of this process.

        Args:
            token: The token from `initialize`.

        Returns:
            The token's SHA-256 if the client may attach, else None.

        Raises:
            TokenCheckUnavailable: If the database gives no answer about a token that is not
                the printed one. A database outage is not reported as a wrong token.
        """
        sha = token_hash(token)
        printed = self._is_printed(sha)
        try:
            if await self.ask(sha):
                return sha
        except TokenCheckUnavailable:
            if not printed:
                raise
            log.warning("the database cannot be asked; accepting the printed token from memory")
        if printed and sha not in self._revoked:
            return sha
        return None

    async def still_valid(self, sha: str) -> bool:
        """Tell whether a token that was accepted earlier may keep its connection.

        A connection is cut only for a token that is remembered as revoked (`observe`). While
        the database gives no answer (an error of any kind, or none within `check_s` seconds)
        the last known answer stands, so a database outage does not lock the people out who
        have to interrupt Claude or decide the open escalations.

        Args:
            sha: SHA-256 of the token, as `authenticate` returned it.

        Returns:
            False if the token is revoked; True otherwise.
        """
        try:
            await self.ask(sha)
        except TokenCheckUnavailable:
            pass
        return sha not in self._revoked

    async def sweep(self) -> None:
        """Check the token of every open connection once and close the revoked ones.

        The printed token is checked as well when no client uses it, so that it is recorded
        for a new session row soon and its revocation is noticed.

        Side effects: one database call per distinct token, each waited for at most `check_s`
        seconds; closes the connections of revoked tokens with `CLOSE_TOKEN_REVOKED`.
        """
        clients = [client for client in self._clients if client.token_sha256 is not None]
        hashes = {client.token_sha256 for client in clients} | {self.token_hash}
        for sha in hashes:
            if sha is not None:
                await self.still_valid(sha)
        await self._close_revoked()

    async def _listen_unix(self, socket_path: Path) -> None:
        """Accept clients on a Unix socket until `shutdown` or cancellation.

        Side effects: creates the socket's directory (0700), replaces a stale socket file, sets
        `address` to `unix:<path>` and `ready`, and removes the socket when it stops.

        Args:
            socket_path: Where to bind.

        Raises:
            OSError: If the directory or the socket cannot be created.
        """
        await anyio.to_thread.run_sync(_prepare_socket, socket_path)
        try:
            async with await unix_serve(self._handle, str(socket_path)):
                await anyio.to_thread.run_sync(socket_path.chmod, 0o600)
                self.address = f"unix:{socket_path}"
                self.ready.set()
                log.info("protocol server listening on %s", self.address)
                await self._until_shutdown()
        finally:
            # Shielded: this also runs when the task group is cancelled, and a cancelled
            # worker-thread call would leave the socket file behind.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(_remove_socket, socket_path)

    async def _handle(self, connection: ServerConnection) -> None:
        """Serve one WebSocket connection until it closes. Never raises.

        Args:
            connection: The accepted connection.
        """
        client = Client(self, connection)
        self._clients.add(client)
        try:
            await client.serve()
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("a client connection failed")
        finally:
            self._clients.discard(client)
            client.close()


class Client:
    """One connected client: its authentication, its requests and its event stream."""

    def __init__(self, server: SessionServer, connection: ServerConnection) -> None:
        """Wrap an accepted connection.

        Args:
            server: The server this client belongs to.
            connection: The WebSocket connection.
        """
        self.server = server
        self.connection = connection
        self.name = ""
        self.authenticated = False
        self.token_sha256: str | None = None
        """SHA-256 of the token this client authenticated with; None before `initialize`."""
        self._lock = anyio.Lock()
        self._subscription: Subscription | None = None
        self._last_sent = 0  # so a replayed event is not sent twice
        self._tasks: TaskGroup | None = None
        """Runs the tasks that push events; None before `serve` and after the disconnect."""
        self._side_asking = False
        """True while a side question of this client is being answered (one at a time)."""

    def close(self) -> None:
        """End the event subscription of this client, if it has one."""
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def revoke(self, why: str = "the client token was revoked") -> None:
        """End this client because its token was revoked.

        Side effects: ends the event subscription, forgets the authentication (a request that
        is still read or still waits is answered with -32001 and not served) and closes the
        connection with `CLOSE_TOKEN_REVOKED`. A request that already runs is finished.

        Args:
            why: The close reason the client is given, at most 123 bytes.
        """
        log.warning("closing the connection of client %r: its token was revoked", self.name)
        self.authenticated = False
        self.close()
        await self.connection.close(CLOSE_TOKEN_REVOKED, why)

    async def serve(self) -> None:
        """Handle the client's messages until the connection closes and its requests are done.

        Control requests (`CONTROL_METHODS`) run next to a running request; the others run one
        after another, in the order the client sent them. The loop that reads the connection
        waits for neither, so a control request is read and started while an ordinary request
        is still running. (Only when `REQUEST_BACKLOG` ordinary requests wait does the loop
        stop reading until one is done.)

        A client that disconnects ends this method without an error, but not at once:

            read loop ends ──► event subscription ends, push tasks are cancelled
                           ──► the requests already read are served to their end, in order
                           ──► serve returns

        So a `session.start` or `session.stop` whose client went away is completed by the
        session exactly as if the client had stayed; only the responses are dropped. Requests
        are cut short only when this method itself is cancelled, which is the end of the
        process.
        """
        queue_in, queue_out = anyio.create_memory_object_stream[str](REQUEST_BACKLOG)
        async with queue_out, anyio.create_task_group() as requests:
            requests.start_soon(self._work, queue_out)
            try:
                async with anyio.create_task_group() as pushes, queue_in:
                    self._tasks = pushes
                    try:
                        await self._read(requests, queue_in)
                    finally:
                        self._tasks = None
                        self.close()
                        pushes.cancel_scope.cancel()
            except Exception:
                # The connection is of no use any more, but what the client asked for before
                # is still served: leaving this block normally lets `requests` finish.
                log.exception("reading from client %r failed", self.name or "unknown")

    async def _read(self, requests: TaskGroup, queue: MemoryObjectSendStream[str]) -> None:
        """Read the client's messages until it disconnects and hand each one on.

        Args:
            requests: The task group that control requests are started in. It is not cancelled
                when the client disconnects.
            queue: Where the ordinary requests go, for `_work`.
        """
        try:
            async for message in self.connection:
                text = message if isinstance(message, str) else message.decode()
                if not text.strip():
                    continue
                if not self.authenticated:
                    # In place, so that a client may send `initialize` and its first
                    # requests in one go and still be authenticated when they are read.
                    await self._answer(text)
                elif _method_of(text) in CONTROL_METHODS:
                    requests.start_soon(self._answer, text)
                else:
                    await queue.send(text)
        except ConnectionClosed:
            pass
        log.info("client %r disconnected", self.name or "unknown")

    async def _work(self, requests: MemoryObjectReceiveStream[str]) -> None:
        """Handle the ordinary requests of this client one after another, in order.

        Returns when the read loop has closed the sending end and every request that was read
        is served.

        Args:
            requests: The received messages, in the order the client sent them.
        """
        async for text in requests:
            await self._answer(text)

    async def _answer(self, text: str) -> None:
        """Handle one message; a client that disconnected meanwhile is not an error.

        Args:
            text: One received message.
        """
        try:
            await self._handle_message(text)
        except ConnectionClosed:
            pass

    async def send(self, text: str) -> None:
        """Send one JSON message; concurrent calls are serialized.

        Args:
            text: A complete JSON object.

        Raises:
            websockets.exceptions.ConnectionClosed: If the client disconnected.
        """
        async with self._lock:
            await self.connection.send(text)

    async def _handle_message(self, text: str) -> None:
        """Parse and dispatch one message and send the response or error response.

        Every failure of the request becomes a JSON-RPC error response (the `RpcError`s of the
        dispatch are caught here); only errors of the connection itself and cancellation
        propagate. Notifications (no "id") get no success response.

        Args:
            text: One received message.

        Raises:
            websockets.exceptions.ConnectionClosed: If the client disconnected while the
                response was being sent.
        """
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            await self._error(None, -32700, f"parse error: {exc}")
            return
        if not isinstance(message, dict) or "method" not in message:
            await self._error(None, -32600, "invalid request")
            return
        request_id = message.get("id")
        method = str(message["method"])
        try:
            result = await self._dispatch(method, message.get("params") or {})
            if request_id is not None:
                payload = (
                    result.model_dump(mode="json") if isinstance(result, BaseModel) else result
                )
                await self.send(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": request_id, "result": payload}, ensure_ascii=False
                    )
                )
            if method == "shutdown":
                self.server.shutdown.set()
                # Then close this connection: the close flushes the response above, which the
                # teardown of the process would otherwise abort and lose. Shielded, so that
                # the teardown, which cancels this task, does not cut the close short.
                with anyio.move_on_after(GRACE_S, shield=True):
                    await self.connection.close()
        except RpcError as exc:
            await self._error(request_id, exc.code, exc.message, exc.data)
        except (SessionError, ValueError) as exc:
            await self._error(request_id, -32000, str(exc))
        except ConnectionClosed:
            raise
        except Exception as exc:  # the client must get an answer
            log.exception("request %r failed", method)
            await self._error(request_id, -32603, f"internal error: {exc}")

    async def _error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        """Send a JSON-RPC error response.

        Args:
            request_id: Id of the failed request, or None if it is unknown.
            code: JSON-RPC error code.
            message: Error message.
            data: Optional details; omitted from the response if None.

        Raises:
            websockets.exceptions.ConnectionClosed: If the client disconnected.
        """
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}))

    async def _dispatch(self, method: str, raw: Any) -> Any:
        """Validate the params of a request and serve it.

        Args:
            method: The request method.
            raw: The request params as decoded from JSON.

        Returns:
            The result: a pydantic model or a JSON-serializable dict.

        Raises:
            RpcError: For an unknown method (-32601), invalid params (-32602), a request
                before a successful `initialize` or a request that changes the session from a
                client whose token was revoked (both -32001).
            SessionError: If the session rejects the request.
            ValueError: If the request content is invalid (e.g. a selection outside the
                document).
        """
        model = REQUESTS.get(method)
        if model is None:
            raise RpcError(-32601, f"unknown method {method!r}")
        if not self.authenticated and method not in NO_AUTH_METHODS:
            raise RpcError(UNAUTHENTICATED, "send initialize with a valid token first")
        if method not in READ_ONLY_METHODS and not await self._token_still_valid():
            raise RpcError(UNAUTHENTICATED, "the client token was revoked")
        try:
            params = model.model_validate(raw)
        except ValidationError as exc:
            raise RpcError(-32602, "invalid params", json.loads(exc.json())) from exc
        session = self.server.session
        match method:
            case "initialize":
                assert isinstance(params, InitializeParams)
                return await self._initialize(params)
            case "state.get":
                return self.server.session.snapshot().model_copy(
                    update={"last_seq": self.server.bus.last_seq}
                )
            case "events.subscribe":
                assert isinstance(params, EventsSubscribeParams)
                return await self._subscribe(params)
            case "session.start":
                assert isinstance(params, SessionStartParams)
                await session.start(
                    self.server.task_group, params.resume_id, params.model, params.effort
                )
                return {"claude_session_id": session.session_id}
            case "session.stop":
                await session.stop()
                return {}
            case "session.list":
                assert isinstance(params, SessionListParams)
                return SessionListResult(sessions=await session.recent_sessions(params.limit))
            case "session.history":
                assert isinstance(params, SessionHistoryParams)
                return await session.history(params.claude_session_id, params.limit)
            case "chat.send":
                assert isinstance(params, ChatSendParams)
                await session.send_chat(params)
                return {}
            case "comment.add":
                assert isinstance(params, CommentAddParams)
                return CommentAddResult(comment=await session.add_comment(params))
            case "escalation.decide":
                assert isinstance(params, EscalationDecideParams)
                await session.decide_escalation(
                    params.escalation_id,
                    params.decision,
                    params.note,
                    client=self.name,
                    answer=params.answer,
                    token_sha256=self.server.recorded(self.token_sha256),
                )
                return {}
            case "escalation.context":
                assert isinstance(params, EscalationContextParams)
                found = await session.escalation_context(params.escalation_id)
                if found is None:
                    raise RpcError(-32000, f"no escalation {params.escalation_id!r}")
                return found
            case "checkers.get":
                return session.checker_definitions()
            case "auto_escalate.set":
                assert isinstance(params, AutoEscalateSetParams)
                await session.set_auto_escalate(
                    AutoEscalate(**params.model_dump()), client=self.name or "unknown"
                )
                return {}
            case "honesty.clear":
                assert isinstance(params, HonestyClearParams)
                await session.clear_mark(params.mark_id, client=self.name or "unknown")
                return {}
            case "interrupt":
                await session.interrupt()
                return {}
            case "context.get":
                return session.checker_context()
            case "side.ask":
                assert isinstance(params, SideAskParams)
                # A side question is a model call of its own and is handled next to the
                # client's other requests; one client gets one at a time.
                if self._side_asking:
                    raise RpcError(-32000, "a side question of this client is still being answered")
                history = [(entry.question, entry.answer) for entry in params.history]
                self._side_asking = True
                try:
                    return SideAskResult(answer=await session.ask_side(params.question, history))
                finally:
                    self._side_asking = False
            case "usage.get":
                return await session.usage()
            case "model.set":
                assert isinstance(params, ModelSetParams)
                await session.set_model(params.model, client=self.name or "unknown")
                return {}
            case "shutdown":
                return {}
        raise RpcError(-32601, f"unhandled method {method!r}")

    async def _initialize(self, params: InitializeParams) -> Any:
        """Check the client's token and answer `initialize`.

        Args:
            params: The client's name, version and token.

        Returns:
            The `InitializeResult` with the current sequence number.

        Raises:
            RpcError: -32001 if the token is neither the one this process printed nor a token an
                operator minted for this session, or if it was revoked.
            RpcError: -32603 if the database gives no answer about a minted token. A database
                outage is not reported as a wrong token, and the printed token still works
                then, unless it was seen revoked before the outage.
        """
        try:
            sha = await self.server.authenticate(params.token)
        except TokenCheckUnavailable as error:
            log.warning("client %r cannot attach: %s", params.client, error)
            raise RpcError(-32603, f"internal error: {error}; try again") from error
        if sha is None:
            log.warning("client %r sent an invalid token", params.client)
            raise RpcError(UNAUTHENTICATED, "invalid client token")
        self.authenticated = True
        self.token_sha256 = sha
        self.name = params.client
        log.info("client %r connected", self.name)
        return self.server.session.initialize_result().model_copy(
            update={"last_seq": self.server.bus.last_seq}
        )

    async def _token_still_valid(self) -> bool:
        """Check this client's token again; forget the authentication if it was revoked.

        Side effects: one database call, waited for at most `SessionServer.check_s` seconds.
        The server closes the connections of a revoked token shortly after
        (`SessionServer.ask`), once the error response of the request that noticed it is out.

        Returns:
            False if the token was revoked since `initialize`; True otherwise.
        """
        if self.token_sha256 is None:
            return False
        if await self.server.still_valid(self.token_sha256):
            return True
        self.authenticated = False
        return False

    async def _subscribe(self, params: EventsSubscribeParams) -> EventsSubscribeResult:
        """Start pushing events to this client and return the ones it missed.

        A second `events.subscribe` replaces the first subscription, so a client can resubscribe
        after a gap without reconnecting. Events the first subscription still held are not
        pushed any more: they are part of the returned replay or older than `after_seq`.

        Args:
            params: The sequence number the client has already seen.

        Returns:
            The missed events, whether they continue without a gap, and the current sequence
            number.

        Raises:
            RpcError: -32000 if the client has disconnected since it sent the request.
        """
        self.close()
        tasks = self._tasks
        if tasks is None:
            # The client is gone and this request is one of those still served for it; a
            # subscription would count as an attached client that nobody reads.
            raise RpcError(-32000, "the client disconnected")
        events, complete = self.server.bus.replay(params.after_seq)
        # Everything up to here is in the replay (or was asked not to be sent), so the push
        # task of an earlier subscription must not send its leftover queue a second time.
        self._last_sent = self.server.bus.last_seq
        self._subscription = self.server.bus.subscribe()
        tasks.start_soon(self._push)
        return EventsSubscribeResult(
            events=events, complete=complete, last_seq=self.server.bus.last_seq
        )

    async def _push(self) -> None:
        """Send the subscribed events to the client until the subscription ends.

        A client that fell too far behind (`Subscription.overflowed`) is disconnected with the
        close code `CLOSE_FELL_BEHIND`: it has missed events, and a connection that stays open
        without events would look attached while it shows nothing new. The client connects
        again and replays from the last sequence number it received.
        """
        subscription = self._subscription
        if subscription is None:
            return
        try:
            async for event in subscription.events():
                if event.seq > self._last_sent:
                    self._last_sent = event.seq
                    await self.send(encode_event(event))
        except ConnectionClosed:
            return
        if subscription.overflowed:
            log.warning("client %r fell too far behind; closing its connection", self.name)
            if self._subscription is subscription:
                self.close()
            await self.connection.close(
                CLOSE_FELL_BEHIND,
                "fell too far behind; connect again and subscribe after the last seq",
            )


def _method_of(text: str) -> str | None:
    """Return the "method" of a JSON-RPC message, or None if it cannot be parsed.

    Args:
        text: One received message.

    Returns:
        The "method" value, or None if the message is not a JSON object or has no method.
    """
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return None
    return message.get("method") if isinstance(message, dict) else None
