"""The calu protocol server: JSON-RPC 2.0 over WebSocket, for several clients at once.

    client ──{"id":1,"method":"chat.send",...}──► Client._handle ──► Session
    client ◄──{"id":1,"result":{}}───────────────┘
    client ◄──{"method":"tool.use","params":{...,"seq":42}}── Bus (after events.subscribe)

One `SessionServer` serves one session. Each WebSocket connection gets a `Client`, which

1. accepts only `initialize` until the client has shown a valid token (`calu.tokens`),
2. handles the requests of that connection one after another, so a client may send
   `chat.send` right after `session.start` without waiting, and
3. after `events.subscribe` pushes the session's events to the client as notifications.

Control requests (`CONTROL_METHODS`) are handled next to a running request, so that
`escalation.decide`, `interrupt` and `shutdown` work while a slow request (`session.start`) is
still running: the loop that reads the connection never waits for a request, it hands the
ordinary ones to one worker task per client and starts a task for each control request.
Requests of different clients are independent; the session itself serializes what must not
overlap.

Error codes (JSON-RPC 2.0): -32700 parse error, -32600 invalid request, -32601 unknown method,
-32602 invalid params, -32000 request failed (`SessionError`, `ValueError`), -32603 internal,
and -32001 not authenticated (no valid `initialize` yet, or the token was revoked since).

The server closes a connection itself in two cases, each with its own WebSocket close code
(`calu.protocol.messages`):

    CLOSE_TOKEN_REVOKED (4001)  the client's token was revoked. The token is refused from
                                now on, so connecting again with it does not help.
    CLOSE_FELL_BEHIND (4002)    the client's event queue overflowed (`calu.bus`). The client
                                connects again and subscribes after the last sequence number
                                it received.

Token revocation (PLAN.md D30) and open connections: a token is checked against the database
at `initialize`, again before every request in `REVALIDATED_METHODS`, and every
`REVALIDATE_S` seconds for every open connection (`SessionServer.sweep`). A connection whose
token was revoked is closed. See `SessionServer.observe` for what counts as revoked.
"""

import json
import logging
import secrets
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import psycopg
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream
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
    SessionHistoryParams,
    SessionListParams,
    SessionListResult,
    SessionStartParams,
)
from calu.session import Session, SessionError
from calu.tokens import token_hash

log = logging.getLogger(__name__)

CONTROL_METHODS = frozenset({"escalation.decide", "interrupt", "shutdown", "state.get"})
"""Requests that are handled next to a running request of the same client."""

NO_AUTH_METHODS = frozenset({"initialize"})
"""Requests a client may send before it has shown a valid token."""

UNAUTHENTICATED = -32001
"""JSON-RPC error code for a request before a successful `initialize`, and for a request of a
client whose token was revoked after it."""

REVALIDATED_METHODS = frozenset(
    {
        "session.start",
        "session.stop",
        "chat.send",
        "comment.add",
        "escalation.decide",
        "auto_escalate.set",
        "honesty.clear",
        "interrupt",
        "shutdown",
    }
)
"""Requests that change the session. Before each of them the client's token is checked against
the database again, so a revoked token cannot decide an escalation, send a message or stop the
process, even in the seconds before `SessionServer.sweep` closes its connection."""

REVALIDATE_S = 15.0
"""Seconds between two checks of the tokens of all open connections (`SessionServer.sweep`).
One check is one small database call per distinct token."""

REQUEST_BACKLOG = 256
"""Ordinary requests of one client that may wait for the one that is running. Past that the
read loop waits (and with it the client's control requests), so a client cannot make the
session buffer requests without bound."""

GRACE_S = 0.1
"""How long `listen` keeps the connections open after a `shutdown` request, so that the events
and responses already written reach the clients."""


FIRST_CHECK_S = 1.0
"""Seconds after the start of `listen` until the first `SessionServer.sweep`. It confirms the
printed token soon after `calu run` recorded it, which is what makes a later revocation of that
token recognizable (`SessionServer.observe`)."""

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
        """
        self.session = session
        self.bus = bus
        self.token_hash = token_hash
        self.task_group = task_group
        self.revalidate_s = revalidate_s
        self.shutdown = anyio.Event()
        self.ready = anyio.Event()
        """Set by `listen` once the socket is bound and `address` holds the URL."""
        self.address = ""
        """The WebSocket address clients connect to, set by `listen`."""
        self._clients: set[Client] = set()
        self._confirmed: dict[str, str] = {}
        """Token hash -> id of the session row for which the database last called it valid."""
        self._revoked: set[str] = set()
        """Hashes of tokens that were seen revoked; they are refused for the rest of the
        process, also while the database is unreachable."""

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

    async def observe(self, sha: str) -> bool:
        """Ask the database about one token hash and remember what the answer means.

            database says            | meaning                         | effect
            -------------------------+---------------------------------+------------------------
            valid                    | a token of the current session  | remembered as confirmed
            not valid, and it was    | an operator revoked it          | remembered as revoked,
            confirmed for this same  | (`calu token revoke`)           | for the whole process
            row, which still runs    |                                 |
            not valid, otherwise     | unknown: the session ended and  | nothing; callers decide
                                     | revoked its own tokens (D108),  |
                                     | a later `session.start` made a  |
                                     | new row, or the token never was |
                                     | this session's                  |

        The middle row is the only way to tell an operator's revocation from the session's own
        clean-up, because the session role can ask "is this hash valid" and nothing else (D28).

        The token this process printed is recorded once by `calu run`, for the first session
        row. When a later `session.start` in the same process made a new row, this method
        records the printed token's hash for that row as well (`add_client_token`), so that it
        shows in `calu token list` and can be revoked there; without that row it would be
        accepted from memory with nothing an operator could revoke.

        Side effects: one database call (`calu_client_token_ok` stamps `last_used_at`), and the
        insert described above.

        Args:
            sha: SHA-256 of a client token.

        Returns:
            True if the database called the token valid for the current session row.

        Raises:
            psycopg.Error: If the database cannot be asked. Nothing is remembered then.
        """
        if sha in self._revoked:
            return False
        store = self.session.store
        row, running = self._session_row()
        try:
            valid = await store.client_token_ok(sha)
        except RuntimeError:
            # The database store refuses to be asked before the first `session.start`: there
            # is no session row yet, so there is no token row either.
            return False
        same_row, still_running = self._session_row()
        if valid and row is not None:
            self._confirmed[sha] = row
        if valid:
            return True
        if row is None or not (running and still_running and same_row == row):
            return False  # the session ended meanwhile and revoked its tokens itself
        confirmed_for = self._confirmed.get(sha)
        if confirmed_for == row:
            log.warning("a client token of this session was revoked")
            self._revoked.add(sha)
        elif confirmed_for is not None and self._is_printed(sha):
            try:
                await store.add_client_token(sha, self.address)
                self._confirmed[sha] = row
            except Exception:
                log.exception("recording the printed client token for the new session failed")
        return False

    def recorded(self, sha: str | None) -> str | None:
        """Return a token hash if the database holds it as a valid token of the current session.

        A decision is recorded in the database under the deciding client's own token
        (`Session.decide_escalation`). A client that was accepted without such a row (the
        printed token during a database outage, or a connection that outlived its session row)
        has none to record under, so the store's remembered token is used for it instead.

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

    async def authenticate(self, token: str) -> str | None:
        """Decide whether a client may attach with `token` (`initialize`).

        The database is asked first, for every token. The printed token is also accepted when
        the database does not call it valid, unless it was seen revoked: that covers a database
        outage (attaching with the printed token keeps working, D30), a session that has not
        started yet, and a session row that holds no row for it. A minted token needs the
        database's yes.

        Args:
            token: The token from `initialize`.

        Returns:
            The token's SHA-256 if the client may attach, else None.

        Raises:
            psycopg.Error: If the database cannot be asked about a token that is not the
                printed one. A database outage is not reported as a wrong token.
        """
        sha = token_hash(token)
        printed = self._is_printed(sha)
        try:
            if await self.observe(sha):
                return sha
        except psycopg.Error:
            if not printed:
                raise
            log.warning("the database cannot be asked; accepting the printed token from memory")
        if printed and sha not in self._revoked:
            return sha
        return None

    async def still_valid(self, sha: str) -> bool:
        """Tell whether a token that was accepted earlier may keep its connection.

        A connection is cut only for a token that was seen revoked (`observe`). While the
        database is unreachable the last known answer stands, so a database outage does not
        lock the people out who have to decide the open escalations.

        Args:
            sha: SHA-256 of the token, as `authenticate` returned it.

        Returns:
            False if the token is revoked; True otherwise.
        """
        try:
            await self.observe(sha)
        except psycopg.Error as error:
            log.warning("the database cannot be asked about a client token: %s", error)
        return sha not in self._revoked

    async def sweep(self) -> None:
        """Check the token of every open connection once and close the revoked ones.

        The printed token is checked as well when no client uses it, so that its revocation is
        noticed while it is still distinguishable from the session's own clean-up (`observe`).

        Side effects: one database call per distinct token; closes the connections of revoked
        tokens with `CLOSE_TOKEN_REVOKED`.
        """
        clients = [client for client in self._clients if client.token_sha256 is not None]
        hashes = {client.token_sha256 for client in clients} | {self.token_hash}
        for sha in hashes:
            if sha is not None:
                await self.still_valid(sha)
        for client in clients:
            if client.token_sha256 in self._revoked:
                await client.revoke()

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

    def close(self) -> None:
        """End the event subscription of this client, if it has one."""
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def revoke(self) -> None:
        """End this client because its token was revoked. Never raises.

        Side effects: ends the event subscription, forgets the authentication (a request that
        is still read is answered with -32001) and closes the connection with
        `CLOSE_TOKEN_REVOKED`.
        """
        log.warning("closing the connection of client %r: its token was revoked", self.name)
        self.authenticated = False
        self.close()
        await self.connection.close(CLOSE_TOKEN_REVOKED, "the client token was revoked")

    async def serve(self) -> None:
        """Handle the client's messages until the connection closes.

        Control requests (`CONTROL_METHODS`) run next to a running request; the others run one
        after another, in the order the client sent them. The loop that reads the connection
        waits for neither, so a control request is read and started while an ordinary request
        is still running. (Only when `REQUEST_BACKLOG` ordinary requests wait does the loop
        stop reading until one is done.) A client that disconnects ends this method without an
        error, and requests that are still running or waiting are dropped.
        """
        queue_in, queue_out = anyio.create_memory_object_stream[str](REQUEST_BACKLOG)
        async with anyio.create_task_group() as tg, queue_in, queue_out:
            self._tasks = tg
            tg.start_soon(self._work, queue_out)
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
                        tg.start_soon(self._answer, text)
                    else:
                        await queue_in.send(text)
            except ConnectionClosed:
                log.info("client %r disconnected", self.name or "unknown")
            tg.cancel_scope.cancel()

    async def _work(self, requests: MemoryObjectReceiveStream[str]) -> None:
        """Handle the ordinary requests of this client one after another, in order.

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
                # teardown of the process would otherwise abort and lose. Shielded, because
                # the closing connection ends the loop that reads from this client, which
                # cancels this task.
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
        if method in REVALIDATED_METHODS and not await self._token_still_valid():
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
            psycopg.Error: If the database cannot be asked about a minted token. A database
                outage is not reported as a wrong token, and the printed token still works
                then, unless it was seen revoked before the outage.
        """
        sha = await self.server.authenticate(params.token)
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
        """Check this client's token again; start closing the connection if it was revoked.

        Side effects: one database call. For a revoked token the authentication is forgotten
        at once and the connection is closed in a task of its own, after the error response of
        the request that noticed it.

        Returns:
            False if the token was revoked since `initialize`; True otherwise.
        """
        if self.token_sha256 is None:
            return False
        if await self.server.still_valid(self.token_sha256):
            return True
        self.authenticated = False
        if self._tasks is not None:
            self._tasks.start_soon(self._revoke_soon)
        return False

    async def _revoke_soon(self) -> None:
        """Close the connection of a revoked token once the pending error response is out."""
        await anyio.sleep(GRACE_S)
        await self.revoke()

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
        """
        self.close()
        events, complete = self.server.bus.replay(params.after_seq)
        # Everything up to here is in the replay (or was asked not to be sent), so the push
        # task of an earlier subscription must not send its leftover queue a second time.
        self._last_sent = self.server.bus.last_seq
        self._subscription = self.server.bus.subscribe()
        assert self._tasks is not None, "serve() must run before events.subscribe"
        self._tasks.start_soon(self._push)
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
