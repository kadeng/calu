"""A client of the calu protocol: connects to a session over WebSocket (ported from sitl).

    async with connect("ws://127.0.0.1:8765", token, "my-client", on_event) as client:
        await client.request("session.start", {})
        await client.request("chat.send", {"text": "fix the failing test"})

`request` sends a JSON-RPC request and waits for its response; events arrive at the `on_event`
callback as `(method, params)` in the order the session sent them. The client sends `initialize`
itself (with the token) and subscribes to the events, so a caller only sends the requests it
needs. `params["seq"]` numbers the events; `last_seq` holds the highest one received, which a
reconnecting client passes as `after_seq`.

Used by `calu attach` and by the end-to-end tests.
"""

import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

OnEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Called with `(method, params)` for every event the session sends."""


class RequestFailed(Exception):
    """The session answered a request with a JSON-RPC error."""

    def __init__(self, error: dict[str, Any]) -> None:
        """Create the exception from a JSON-RPC error object.

        Args:
            error: The error object with "code", "message" and optional "data"; kept as `error`.
        """
        super().__init__(f"{error.get('message')} (code {error.get('code')})")
        self.error = error

    @property
    def code(self) -> int:
        """The JSON-RPC error code.

        Returns:
            The code, or 0 if the error object has none.
        """
        return int(self.error.get("code", 0))


class ProtocolClient:
    """Client side of the protocol on one WebSocket connection.

    Use `connect`, which performs `initialize` and runs the reader task; calling the
    constructor directly needs `_read` to run in a task, otherwise requests never complete.
    """

    def __init__(self, connection: ClientConnection, on_event: OnEvent) -> None:
        """Wrap an open connection.

        Args:
            connection: The open WebSocket connection.
            on_event: Called with `(method, params)` for every event.
        """
        self.connection = connection
        self.on_event = on_event
        self.last_seq = 0
        """Highest sequence number received so far."""
        self.closed = anyio.Event()
        self.initialize: dict[str, Any] = {}
        """The result of the `initialize` request, once `connect` has sent it."""
        self.complete = True
        """Whether the replayed events continued without a gap (`events.subscribe`)."""
        self._next_id = 1
        self._pending: dict[int, tuple[anyio.Event, list[dict[str, Any]]]] = {}

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and wait for its response.

        Args:
            method: The request method, e.g. "chat.send".
            params: Its params; None sends `{}`.

        Returns:
            The "result" of the response.

        Raises:
            RequestFailed: If the session answered with an error.
            ConnectionError: If the connection closed before the response arrived.
        """
        request_id = self._next_id
        self._next_id += 1
        done = anyio.Event()
        box: list[dict[str, Any]] = []
        self._pending[request_id] = (done, box)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            await self.connection.send(json.dumps(message, ensure_ascii=False))
        except ConnectionClosed as exc:
            self._pending.pop(request_id, None)
            raise ConnectionError(f"the session closed the connection: {exc}") from exc
        async with anyio.create_task_group() as tg:

            async def closed() -> None:
                """End the wait if the connection closes."""
                await self.closed.wait()
                done.set()

            tg.start_soon(closed)
            await done.wait()
            tg.cancel_scope.cancel()
        self._pending.pop(request_id, None)
        if not box:
            raise ConnectionError("the connection closed before the response arrived")
        response = box[0]
        if "error" in response:
            raise RequestFailed(response["error"])
        return response.get("result")

    async def _read(self) -> None:
        """Read messages until the connection closes, dispatching responses and events."""
        try:
            async for message in self.connection:
                text = message if isinstance(message, str) else message.decode()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("ignoring a message that is not JSON: %.80s", text)
                    continue
                if isinstance(data, dict):
                    await self._dispatch(data)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()
            for done, _ in list(self._pending.values()):
                done.set()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        """Deliver one received message to the waiting request or to `on_event`.

        Args:
            message: The decoded message.
        """
        request_id = message.get("id")
        if request_id is not None:
            waiting = self._pending.get(int(request_id))
            if waiting is None:
                log.warning("response for an unknown request %r", request_id)
                return
            done, box = waiting
            box.append(message)
            done.set()
            return
        method = message.get("method")
        if not method:
            log.warning("ignoring a message without method or id")
            return
        params = message.get("params") or {}
        if isinstance(params, dict):
            self.last_seq = max(self.last_seq, int(params.get("seq") or 0))
            await self.on_event(str(method), params)


def _single(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """Return the one exception of a (possibly nested) group, or the group itself.

    Args:
        group: The exception group raised by a task group.

    Returns:
        The single leaf exception if the group holds exactly one, else `group` unchanged.
    """
    if len(group.exceptions) != 1:
        return group
    inner = group.exceptions[0]
    return _single(inner) if isinstance(inner, BaseExceptionGroup) else inner


@asynccontextmanager
async def connect(
    address: str,
    token: str,
    client: str,
    on_event: OnEvent,
    client_version: str = "",
    after_seq: int = 0,
) -> AsyncGenerator[ProtocolClient]:
    """Connect to a session, authenticate and subscribe to its events.

    On entry the client sends `initialize` with the token and `events.subscribe` with
    `after_seq`; the replayed events of the subscription reach `on_event` before this returns.
    On exit the connection is closed.

    Args:
        address: The WebSocket address that `calu run` printed, e.g. "ws://127.0.0.1:8765".
        token: The client token that `calu run` printed.
        client: Name of this client, for the session's log.
        on_event: Called with `(method, params)` for every event.
        client_version: Version of this client.
        after_seq: Sequence number the client has already seen; 0 for a new client.

    Yields:
        The connected client; `client.initialize` holds the `initialize` result.

    Raises:
        OSError: If the address cannot be reached.
        RequestFailed: If the token is refused.
        ConnectionError: If the session closes the connection during the handshake.
    """
    async with ws_connect(address) as connection:
        protocol = ProtocolClient(connection, on_event)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(protocol._read)
                try:
                    protocol.initialize = await protocol.request(
                        "initialize",
                        {"client": client, "client_version": client_version, "token": token},
                    )
                    replay = await protocol.request("events.subscribe", {"after_seq": after_seq})
                    for event in replay.get("events", []):
                        await protocol._dispatch(
                            {"method": event["method"], "params": event["params"]}
                        )
                    protocol.complete = bool(replay.get("complete", True))
                    yield protocol
                finally:
                    tg.cancel_scope.cancel()
        except BaseExceptionGroup as group:
            # The reader runs in a task group, which wraps every error in a group; a caller
            # should see the error it caused (a refused token, its own exception), not a group.
            error = _single(group)
            raise error from None
