"""A client on another machine: `calu attach` through a forwarded port, and the tunnel breaking.

A user with a headless server reaches a session through `ssh -L <port>:127.0.0.1:<port>`.
`Tunnel` stands in for that forward: a TCP relay on a port of its own that can be cut (every
connection ends without a WebSocket close frame, as when `ssh` dies or the network goes away)
and opened again on the same port. Both terminal clients must then try again by themselves
and continue after the last event they showed.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import anyio
import anyio.abc
import pytest

import calu.attach as attach_module
from calu.attach import attach
from calu.protocol.messages import Log
from calu.tui import connection as connection_module
from calu.tui.connection import Link, protocol_connector
from calu.tui.model import SessionModel
from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


class Tunnel:
    """A TCP relay from a port of its own to the session's port, like `ssh -L`."""

    def __init__(self, target_port: int) -> None:
        """Create the relay; `open` starts it.

        Args:
            target_port: The session's port on 127.0.0.1.
        """
        self.target_port = target_port
        self.port = 0
        self._scope: anyio.CancelScope | None = None

    @property
    def address(self) -> str:
        """The WebSocket address a client on the other end uses."""
        return f"ws://127.0.0.1:{self.port}"

    async def _relay(self, client: anyio.abc.SocketStream) -> None:
        """Copy both directions between one client and the session until either side ends.

        Args:
            client: The accepted connection.
        """
        async with client, await anyio.connect_tcp("127.0.0.1", self.target_port) as server:

            async def pump(source: anyio.abc.SocketStream, sink: anyio.abc.SocketStream) -> None:
                with suppress(anyio.EndOfStream, anyio.BrokenResourceError, OSError):
                    async for chunk in source:
                        await sink.send(chunk)
                tg.cancel_scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.start_soon(pump, client, server)
                tg.start_soon(pump, server, client)

    async def open(self, tg: anyio.abc.TaskGroup) -> None:
        """Listen (again) on the tunnel's port and relay every connection.

        Args:
            tg: The task group the relay runs in.
        """
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=self.port)
        self.port = listener.extra(anyio.abc.SocketAttribute.local_port)
        scope = anyio.CancelScope()
        self._scope = scope

        async def run() -> None:
            with scope:
                async with listener:
                    await listener.serve(self._relay)

        tg.start_soon(run)

    def cut(self) -> None:
        """End the listener and every relayed connection at once, without a close frame."""
        if self._scope is not None:
            self._scope.cancel()
            self._scope = None


@asynccontextmanager
async def tunnelled(tmp_path, tg: anyio.abc.TaskGroup) -> AsyncGenerator[tuple[object, Tunnel]]:
    """Run a session and a tunnel to it; yield both."""
    async with served(tmp_path) as run:
        tunnel = Tunnel(int(run.address.rsplit(":", 1)[1]))
        await tunnel.open(tg)
        try:
            yield run, tunnel
        finally:
            tunnel.cut()


async def wait_until(condition, timeout: float = 10) -> None:
    """Wait until `condition()` is true."""
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.02)


async def test_the_line_client_comes_back_after_the_tunnel_was_cut(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(attach_module, "REDIAL_DELAYS_S", (0.1,))
    typed_quit = anyio.Event()

    async def read_lines(send):
        await typed_quit.wait()

    monkeypatch.setattr(attach_module, "read_lines", read_lines)
    shown: list[str] = []

    def out() -> str:
        shown.append(capsys.readouterr().out)
        return "".join(shown)

    codes: list[int] = []
    async with anyio.create_task_group() as tg, tunnelled(tmp_path, tg) as (run, tunnel):
        bus = run.server.bus  # type: ignore[attr-defined]

        async def attached() -> None:
            codes.append(await attach(tunnel.address, run.token))  # type: ignore[attr-defined]

        tg.start_soon(attached)
        await wait_until(lambda: "[attached]" in out())
        tunnel.cut()
        await wait_until(lambda: bus.clients == 0)
        await wait_until(lambda: "nothing listens at the address" in out())
        await bus.publish(Log(level="info", message="written while the tunnel was down"))
        await tunnel.open(tg)
        await wait_until(lambda: "written while the tunnel was down" in out())
        typed_quit.set()
        await wait_until(lambda: bool(codes))
        tunnel.cut()
    text = out()
    assert "[reconnecting] connection lost: the connection broke; try 1" in text
    assert "[attached again] continuing after the last event shown" in text
    assert codes == [0]


async def test_the_full_screen_client_comes_back_after_the_tunnel_was_cut(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(connection_module, "REDIAL_DELAYS_S", (0.1,))
    model = SessionModel()
    statuses: list[str] = []

    def notify(change) -> None:
        if not statuses or statuses[-1] != model.connection.status:
            statuses.append(model.connection.status)

    async with anyio.create_task_group() as tg, tunnelled(tmp_path, tg) as (run, tunnel):
        bus = run.server.bus  # type: ignore[attr-defined]
        link = Link(
            protocol_connector(tunnel.address, run.token),  # type: ignore[attr-defined]
            model,
            notify,
            anyio.current_time,
        )
        tg.start_soon(link.run)
        await wait_until(lambda: model.connection.status == "attached")
        tunnel.cut()
        await wait_until(lambda: model.connection.status == "reconnecting")
        await bus.publish(Log(level="info", message="written while the tunnel was down"))
        await wait_until(lambda: model.connection.attempt >= 2)
        assert model.connection.reason.startswith("nothing listens at the address")
        await tunnel.open(tg)
        await wait_until(lambda: model.connection.status == "attached")
        await wait_until(
            lambda: any(line.message == "written while the tunnel was down" for line in model.logs)
        )
        link.detach()
        await wait_until(lambda: model.connection.status == "detached")
        assert not link.lost and link.closed_reason == ""
        tunnel.cut()
    assert [status for status in statuses if status != "connecting"] == [
        "attached",
        "reconnecting",
        "attached",
        "detached",
    ]
