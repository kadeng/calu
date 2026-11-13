"""A client of the calu protocol: connects to a session over WebSocket (ported from sitl).

    async with connect("ws://127.0.0.1:8765", token, "my-client", on_event) as client:
        await client.request("session.start", {})
        await client.request("chat.send", {"text": "fix the failing test"})

`request` sends a JSON-RPC request and waits for its response; events arrive at the `on_event`
callback as `(method, params)` in the order the session sent them. The client sends `initialize`
itself (with the token) and subscribes to the events, so a caller only sends the requests it
needs. `params["seq"]` numbers the events; `last_seq` holds the highest one received, which a
reconnecting client passes as `after_seq`.

When the connection ends, `closed` is set and `close_code` and `close_reason` say why. The
session closes a connection itself with `CLOSE_FELL_BEHIND` (the client did not read its events
fast enough; connect again with `after_seq=last_seq`) and with `CLOSE_TOKEN_REVOKED`
(`calu.protocol.messages`). `fell_behind` tells the first case apart, which is the one where
connecting again is the right answer.

Used by `calu attach`, `calu acp` and by the end-to-end tests.
"""

import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from calu import asuser, localsocket
from calu.protocol.messages import CLOSE_FELL_BEHIND

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
        """Set when the connection has ended, whoever ended it."""
        self.close_code: int | None = None
        """WebSocket close code once `closed` is set; None if the connection ended without a
        close frame (the process died, the network dropped)."""
        self.close_reason = ""
        """The reason text the other side gave with the close, once `closed` is set."""
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
        """Read messages until the connection closes, dispatching responses and events.

        Side effects at the end: records `close_code` and `close_reason`, sets `closed` and
        wakes every waiting `request`.
        """
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
            self.close_code = self.connection.close_code
            self.close_reason = self.connection.close_reason or ""
            self.closed.set()
            for done, _ in list(self._pending.values()):
                done.set()

    @property
    def fell_behind(self) -> bool:
        """Whether the session closed this connection because the client fell behind.

        Returns:
            True once `closed` is set with the close code `CLOSE_FELL_BEHIND`. The session then
            still runs and the token still works, so the caller connects again with
            `after_seq=last_seq`.
        """
        return self.closed.is_set() and self.close_code == CLOSE_FELL_BEHIND

    def describe_close(self) -> str:
        """Say why the connection ended, for a message to the user.

        Returns:
            The reason the session gave, or "no reason given" for a connection that ended
            without one (the calu process ended, or the network dropped).
        """
        return self.close_reason or "no reason given"

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


def single_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """Return the one exception of a (possibly nested) group, or the group itself.

    Args:
        group: The exception group raised by a task group.

    Returns:
        The single leaf exception if the group holds exactly one, else `group` unchanged.
    """
    if len(group.exceptions) != 1:
        return group
    inner = group.exceptions[0]
    return single_error(inner) if isinstance(inner, BaseExceptionGroup) else inner


async def first_set(*events: anyio.Event) -> None:
    """Wait until at least one of the events is set.

    Args:
        *events: The events to wait for.
    """
    async with anyio.create_task_group() as tg:

        async def wait(event: anyio.Event) -> None:
            """End the wait for all events when this one is set.

            Args:
                event: The event to wait for.
            """
            await event.wait()
            tg.cancel_scope.cancel()

        for event in events:
            tg.start_soon(wait, event)


UNIX_PREFIX = "unix:"
"""How a Unix socket address is written: `unix:/run/user/1000/calu/<id>.sock` (PLAN.md §6)."""


@asynccontextmanager
async def _open(address: str) -> AsyncGenerator[ClientConnection]:
    """Open a connection to a TCP or a Unix socket address.

    Args:
        address: `ws://host:port` or `unix:<path>`.

    Yields:
        The open connection.

    Raises:
        OSError: If the address cannot be reached.
    """
    if address.startswith(UNIX_PREFIX):
        path = Path(address[len(UNIX_PREFIX) :])
        if localsocket.ROOT in path.parents:
            # A session's root-owned socket (D215): checked to be root's alone before
            # anything is sent, and opened with root's file access.
            opened = await anyio.to_thread.run_sync(asuser.as_root, localsocket.connected, path)
            connecting = unix_connect(None, "ws://localhost/", sock=opened)
        else:
            connecting = unix_connect(str(path), "ws://localhost/")
        async with connecting as connection:
            yield connection
        return
    async with ws_connect(address) as connection:
        yield connection


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
        address: The address that `calu run` printed: `ws://127.0.0.1:8765`, or `unix:<path>`
            for a session that serves a Unix socket (PLAN.md §6).
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
    async with _open(address) as connection:
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
            error = single_error(group)
            raise error from None
