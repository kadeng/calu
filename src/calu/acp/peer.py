"""JSON-RPC 2.0 over newline-delimited JSON, as ACP uses it on stdin and stdout.

    peer = Peer(read_line, write_line, handle)
    async with anyio.create_task_group() as tg:
        tg.start_soon(peer.serve)          # reads until end of input
        await peer.notify("session/update", {...})
        answer = await peer.request("session/request_permission", {...})

The transport is one JSON object per line (the ACP libraries call it `ndJsonStream`), so this
module is deliberately framing-only: it does not know any ACP method. `read_line` returns one
line without its newline and "" at end of input; `write_line` writes one line. Both are
callables rather than streams, so tests drive the peer through in-memory queues and the CLI
passes stdin and stdout.

Each inbound request is handled in its own task, because a handler may wait a long time (a
prompt turn runs until Claude is done) while `session/cancel` and the answers to the agent's own
requests must still be read.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

log = logging.getLogger(__name__)

ReadLine = Callable[[], Awaitable[str]]
"""Returns the next line without its newline, or "" at end of input."""

WriteLine = Callable[[str], Awaitable[None]]
"""Writes one line (the implementation appends the newline and flushes)."""

Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""Called with `(method, params)` for every inbound request and notification.

Its return value becomes the JSON-RPC result of a request (None becomes `{}`); for a
notification it is ignored. It may raise `RpcError` to answer with a JSON-RPC error.
"""


class RpcError(Exception):
    """An error to answer a request with (a JSON-RPC error object)."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        """Create the error.

        Args:
            code: JSON-RPC error code (see the codes in `calu.acp.messages`).
            message: One sentence for the client's user.
            data: Optional extra information for the client.
        """
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data

    def body(self) -> dict[str, Any]:
        """Render the error as a JSON-RPC error object.

        Returns:
            The object for the "error" member of a response.
        """
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            body["data"] = self.data
        return body


class Peer:
    """One JSON-RPC peer on a pair of line callables.

    It serves inbound requests through `handle` and lets the caller send notifications and
    requests of its own. `serve` must be running for `request` to complete, because the reader
    task delivers the response.
    """

    def __init__(self, read_line: ReadLine, write_line: WriteLine, handle: Handler) -> None:
        """Create the peer.

        Args:
            read_line: Reads the next line; "" means end of input.
            write_line: Writes one line.
            handle: Handles inbound requests and notifications.
        """
        self._read_line = read_line
        self._write_line = write_line
        self._handle = handle
        self._write_lock = anyio.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[anyio.Event, list[dict[str, Any]]]] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._handling = 0
        self._idle = anyio.Event()
        self._idle.set()
        self.closed = anyio.Event()
        """Set when the input ended, so callers can stop."""
        self.after_response: Callable[[str], Awaitable[None]] | None = None
        """Awaited with the method name after the successful response to an inbound request
        was written. For work that must follow the response on the wire, such as a request to
        the client that refers to an id the response has just handed out."""

    async def idle(self) -> None:
        """Wait until no inbound request is being handled and every response is written.

        A request that arrives later is not waited for. Used before the process ends, so that
        the answers to the requests that were just finished still reach the client.
        """
        await self._idle.wait()

    async def serve(self) -> None:
        """Read and dispatch messages until end of input.

        Handlers run in a task group that is cancelled when the input ends, so a client that
        disconnects does not leave a prompt turn waiting forever. Side effect: sets `closed`.
        """
        async with anyio.create_task_group() as tasks:
            self._tasks = tasks
            while True:
                line = await self._read_line()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                tasks.start_soon(self._dispatch, stripped)
            self.closed.set()
            tasks.cancel_scope.cancel()
        self._tasks = None

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no answer is expected).

        Args:
            method: The method name, e.g. "session/update".
            params: Its params.
        """
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a request and wait for its answer.

        Args:
            method: The method name, e.g. "session/request_permission".
            params: Its params.

        Returns:
            The "result" of the response.

        Raises:
            RpcError: If the client answered with an error.
            ConnectionError: If the input ended before the answer arrived.
        """
        request_id = self._next_id
        self._next_id += 1
        done = anyio.Event()
        box: list[dict[str, Any]] = []
        self._pending[request_id] = (done, box)
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            async with anyio.create_task_group() as tasks:

                async def ended() -> None:
                    """End the wait when the input ends."""
                    await self.closed.wait()
                    done.set()

                tasks.start_soon(ended)
                await done.wait()
                tasks.cancel_scope.cancel()
        finally:
            self._pending.pop(request_id, None)
        if not box:
            raise ConnectionError(f"the client did not answer {method}")
        response = box[0]
        if "error" in response:
            error = response["error"] or {}
            raise RpcError(
                int(error.get("code", 0)), str(error.get("message", "")), error.get("data")
            )
        return response.get("result")

    async def _send(self, message: dict[str, Any]) -> None:
        """Write one message as a line.

        Args:
            message: The JSON-RPC message.
        """
        line = json.dumps(message, ensure_ascii=False)
        async with self._write_lock:
            await self._write_line(line)

    async def _dispatch(self, line: str) -> None:
        """Handle one received line: a response to our request, or an inbound message.

        Never raises: a failing handler becomes a JSON-RPC error response, and a malformed line
        a parse error, so that one bad message does not end the connection.

        Args:
            line: The received line, without its newline.
        """
        if not self._handling:
            self._idle = anyio.Event()
        self._handling += 1
        try:
            await self._dispatch_line(line)
        finally:
            self._handling -= 1
            if not self._handling:
                self._idle.set()

    async def _dispatch_line(self, line: str) -> None:
        """Handle one received line (see `_dispatch`, which counts the lines in work).

        Args:
            line: The received line, without its newline.
        """
        try:
            message = json.loads(line)
        except ValueError:
            log.warning("ignoring a line that is not JSON (%d characters)", len(line))
            await self._send(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
            )
            return
        if not isinstance(message, dict):
            log.warning("ignoring a JSON value that is not an object")
            return
        method = message.get("method")
        if method is None:
            self._resolve(message)
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        request_id = message.get("id")
        try:
            result = await self._handle(str(method), params)
        except RpcError as error:
            if request_id is not None:
                await self._send({"jsonrpc": "2.0", "id": request_id, "error": error.body()})
            return
        except Exception:
            log.exception("the handler of %s failed", method)
            if request_id is not None:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": f"{method} failed"},
                    }
                )
            return
        if request_id is not None:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result or {}})
            if self.after_response is not None:
                try:
                    await self.after_response(str(method))
                except Exception:
                    log.exception("the work after the response to %s failed", method)

    def _resolve(self, message: dict[str, Any]) -> None:
        """Hand a response to the `request` call that waits for it.

        Args:
            message: The received response (it has an "id" and no "method").
        """
        try:
            request_id = int(message.get("id"))  # type: ignore[arg-type]
        except TypeError, ValueError:
            log.warning("ignoring a response without a usable id")
            return
        waiting = self._pending.get(request_id)
        if waiting is None:
            log.warning("ignoring a response to the unknown request %s", request_id)
            return
        done, box = waiting
        box.append(message)
        done.set()
