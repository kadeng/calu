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
still running. Requests of different clients are independent; the session itself serializes
what must not overlap.

Error codes (JSON-RPC 2.0): -32700 parse error, -32600 invalid request, -32601 unknown method,
-32602 invalid params, -32000 request failed (`SessionError`, `ValueError`), -32603 internal,
and -32001 not authenticated (no valid `initialize` yet).
"""

import json
import logging
from typing import Any

import anyio
from anyio.abc import TaskGroup
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from calu.bus import Bus, Subscription
from calu.protocol.messages import (
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
    InitializeParams,
    SessionHistoryParams,
    SessionListParams,
    SessionListResult,
    SessionStartParams,
)
from calu.session import Session, SessionError
from calu.tokens import matches

log = logging.getLogger(__name__)

CONTROL_METHODS = frozenset({"escalation.decide", "interrupt", "shutdown", "state.get"})
"""Requests that are handled next to a running request of the same client."""

NO_AUTH_METHODS = frozenset({"initialize"})
"""Requests a client may send before it has shown a valid token."""

UNAUTHENTICATED = -32001
"""JSON-RPC error code for a request before a successful `initialize`."""

GRACE_S = 0.1
"""How long `listen` keeps the connections open after a `shutdown` request, so that the events
and responses already written reach the clients."""


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

    `token_hash` is the SHA-256 of the token that `calu run` printed; a client must send the
    token in `initialize`. `shutdown` is set when a client asks the process to stop, so that
    `calu run` can end the session.
    """

    def __init__(
        self,
        session: Session,
        bus: Bus,
        token_hash: str,
        task_group: TaskGroup,
    ) -> None:
        """Create the server; `listen` starts it.

        Args:
            session: The session that requests are dispatched to.
            bus: The bus whose events the clients receive; the session's event sink must be
                `bus.publish`.
            token_hash: SHA-256 of the client token (`calu.tokens.token_hash`).
            task_group: The task group that runs the Claude session; `session.start` adds the
                session's own task to it, so it must outlive every Claude session.
        """
        self.session = session
        self.bus = bus
        self.token_hash = token_hash
        self.task_group = task_group
        self.shutdown = anyio.Event()
        self.ready = anyio.Event()
        """Set by `listen` once the socket is bound and `address` holds the URL."""
        self.address = ""
        """The WebSocket address clients connect to, set by `listen`."""

    async def listen(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Accept clients until `shutdown` is set or the caller is cancelled.

        Side effects: sets `address` to the WebSocket URL once the socket is bound (with the
        port the operating system chose, if `port` was 0) and then sets `ready`.

        Args:
            host: Interface to bind; the default accepts only local clients (PLAN.md §6).
            port: TCP port, or 0 to let the operating system choose a free one.

        Raises:
            OSError: If the address cannot be bound.
        """
        async with await serve(self._handle, host, port) as server:
            bound = server.sockets[0].getsockname()
            self.address = f"ws://{bound[0]}:{bound[1]}"
            self.ready.set()
            log.info("protocol server listening on %s", self.address)
            await self.shutdown.wait()
            await anyio.sleep(GRACE_S)  # let the other clients' last events go out

    async def _handle(self, connection: ServerConnection) -> None:
        """Serve one WebSocket connection until it closes. Never raises.

        Args:
            connection: The accepted connection.
        """
        client = Client(self, connection)
        try:
            await client.serve()
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("a client connection failed")
        finally:
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
        self._lock = anyio.Lock()
        self._subscription: Subscription | None = None
        self._last_sent = 0  # so a replayed event is not sent twice
        self._tasks: TaskGroup | None = None

    def close(self) -> None:
        """End the event subscription of this client, if it has one."""
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def serve(self) -> None:
        """Handle the client's messages until the connection closes.

        Control requests (`CONTROL_METHODS`) run next to a running request; the others run one
        after another, in the order the client sent them. A client that disconnects ends this
        method without an error.
        """
        async with anyio.create_task_group() as tg:
            self._tasks = tg
            try:
                async for message in self.connection:
                    text = message if isinstance(message, str) else message.decode()
                    if not text.strip():
                        continue
                    if _method_of(text) in CONTROL_METHODS:
                        tg.start_soon(self._handle_message, text)
                    else:
                        await self._handle_message(text)
            except ConnectionClosed:
                log.info("client %r disconnected", self.name or "unknown")
            tg.cancel_scope.cancel()

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
            RpcError: For an unknown method (-32601), invalid params (-32602) or a request
                before a successful `initialize` (-32001).
            SessionError: If the session rejects the request.
            ValueError: If the request content is invalid (e.g. a selection outside the
                document).
        """
        model = REQUESTS.get(method)
        if model is None:
            raise RpcError(-32601, f"unknown method {method!r}")
        if not self.authenticated and method not in NO_AUTH_METHODS:
            raise RpcError(UNAUTHENTICATED, "send initialize with a valid token first")
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
                await session.decide_escalation(params.escalation_id, params.decision, params.note)
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
                session.config.auto_escalate = AutoEscalate(**params.model_dump())
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
                operator minted for this session.
            psycopg.Error: If the database cannot be asked about a minted token. A database
                outage is not reported as a wrong token, and the printed token still works,
                because it is checked first and without the database.
        """
        if not await self._token_ok(params.token):
            log.warning("client %r sent an invalid token", params.client)
            raise RpcError(UNAUTHENTICATED, "invalid client token")
        self.authenticated = True
        self.name = params.client
        log.info("client %r connected", self.name)
        return self.server.session.initialize_result().model_copy(
            update={"last_seq": self.server.bus.last_seq}
        )

    async def _token_ok(self, token: str) -> bool:
        """Check a client's token against this process and then against the database.

        Args:
            token: The token from `initialize`.

        Returns:
            True if it is the token this process printed, or a non-revoked token an operator
            minted for this session (D30).

        Raises:
            psycopg.Error: If the database cannot be asked.
        """
        if matches(token, self.server.token_hash):
            return True
        return await self.server.session.accepts_token(token)

    async def _subscribe(self, params: EventsSubscribeParams) -> EventsSubscribeResult:
        """Start pushing events to this client and return the ones it missed.

        A second `events.subscribe` replaces the first subscription, so a client can resubscribe
        after a gap without reconnecting.

        Args:
            params: The sequence number the client has already seen.

        Returns:
            The missed events, whether they continue without a gap, and the current sequence
            number.
        """
        self.close()
        events, complete = self.server.bus.replay(params.after_seq)
        self._last_sent = params.after_seq
        self._subscription = self.server.bus.subscribe()
        assert self._tasks is not None, "serve() must run before events.subscribe"
        self._tasks.start_soon(self._push)
        return EventsSubscribeResult(
            events=events, complete=complete, last_seq=self.server.bus.last_seq
        )

    async def _push(self) -> None:
        """Send the subscribed events to the client until the subscription ends."""
        subscription = self._subscription
        if subscription is None:
            return
        try:
            async for event in subscription.events():
                if event.seq > self._last_sent:
                    self._last_sent = event.seq
                    await self.send(encode_event(event))
            if subscription.overflowed:
                await self._error(
                    None, -32002, "the client fell too far behind; subscribe again after state.get"
                )
        except ConnectionClosed:
            pass


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
