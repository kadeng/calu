"""An in-memory editor for the tests of `calu ide`: it talks to `calu.ide.bridge.serve`."""

import json
import math
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio

from calu.credentials import CredentialStore
from calu.ide.bridge import serve


class Failed(Exception):
    """The bridge answered a request with a JSON-RPC error."""

    def __init__(self, error: dict[str, Any]) -> None:
        """Keep the error object.

        Args:
            error: `{code, message, data?}`.
        """
        super().__init__(f"{error.get('message')} (code {error.get('code')})")
        self.code = int(error.get("code", 0))
        self.message = str(error.get("message", ""))
        self.data = error.get("data")


class Editor:
    """Stands for the editor extension: sends requests, collects notifications in order."""

    def __init__(self) -> None:
        """Create the editor with empty queues."""
        self._to_bridge, self._lines = anyio.create_memory_object_stream[str](math.inf)
        self._answers: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.wire: list[dict[str, Any]] = []
        """Every message the bridge wrote, in the order it wrote them."""
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def read_line(self) -> str:
        """Give the bridge its next line; "" once the editor closed its end."""
        try:
            return await self._lines.receive()
        except anyio.EndOfStream:
            return ""

    async def write_line(self, line: str) -> None:
        """Take one line the bridge wrote.

        Args:
            line: The JSON message.
        """
        message = json.loads(line)
        assert "\n" not in line and message["jsonrpc"] == "2.0"
        self.wire.append(message)
        if "method" in message:
            self.notifications.append((message["method"], message["params"]))
        else:
            self._answers[message["id"]] = message

    def close(self) -> None:
        """Close the editor's end of the pipe."""
        self._to_bridge.close()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        """Send a request and wait for its result.

        Args:
            method: The method.
            params: Its params.
            timeout: Seconds before the wait fails.

        Raises:
            Failed: If the bridge answered with an error.
        """
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        await self._to_bridge.send(json.dumps(message))
        with anyio.fail_after(timeout):
            while request_id not in self._answers:
                await anyio.sleep(0.01)
        answer = self._answers.pop(request_id)
        if "error" in answer:
            raise Failed(answer["error"])
        return answer["result"]

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one request of the session protocol through `session.call`.

        Args:
            method: The session's method.
            params: Its params.
        """
        return await self.request("session.call", {"method": method, "params": params or {}})

    def events(self, method: str | None = None) -> list[dict[str, Any]]:
        """Return the forwarded session events, optionally of one method.

        Args:
            method: The session event's method, or None for all.
        """
        return [
            params
            for name, params in self.notifications
            if name == "session.event" and method in (None, params["method"])
        ]

    def links(self) -> list[str]:
        """Return the states of the `ide.link` notifications, in order."""
        return [params["state"] for name, params in self.notifications if name == "ide.link"]

    async def wait_event(self, method: str, count: int = 1, timeout: float = 15) -> list[dict]:
        """Wait until `count` session events of one method were forwarded.

        Args:
            method: The session event's method.
            count: How many are awaited.
            timeout: Seconds before the wait fails.
        """
        with anyio.fail_after(timeout):
            while len(self.events(method)) < count:
                await anyio.sleep(0.01)
        return [event["params"] for event in self.events(method)]

    async def wait_link(self, state: str, timeout: float = 15) -> None:
        """Wait until an `ide.link` notification with this state arrived.

        Args:
            state: "attached", "reconnecting" or "lost".
            timeout: Seconds before the wait fails.
        """
        with anyio.fail_after(timeout):
            while state not in self.links():
                await anyio.sleep(0.01)


@asynccontextmanager
async def bridged(
    workspace: Path, store: CredentialStore, **options: Any
) -> AsyncGenerator[Editor]:
    """Run the bridge for one editor for the duration of the block.

    Args:
        workspace: The editor's workspace folder.
        store: The credential store the bridge reads.
        **options: Further arguments of `calu.ide.bridge.Bridge`.

    Yields:
        The editor.
    """
    editor = Editor()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: serve(editor.read_line, editor.write_line, workspace, store, **options)
        )
        try:
            yield editor
        finally:
            editor.close()
