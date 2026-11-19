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


async def test_both_clients_follow_a_cut_tunnel_to_the_relay(tmp_path, monkeypatch) -> None:
    """One forward of the relay port (D242): the client comes back after the tunnel is cut."""
    from tests.unit.test_relay import SESSION_ID, TOKEN, relayed

    monkeypatch.setattr(connection_module, "REDIAL_DELAYS_S", (0.1,))
    model = SessionModel()
    async with relayed(tmp_path) as it, anyio.create_task_group() as tg:
        tunnel = Tunnel(int(it.relay.address.rsplit(":", 1)[1]))
        await tunnel.open(tg)
        address = f"{tunnel.address}/session/{SESSION_ID}"
        link = Link(
            protocol_connector(address, TOKEN), model, lambda change: None, anyio.current_time
        )
        tg.start_soon(link.run)
        await wait_until(lambda: model.connection.status == "attached")
        tunnel.cut()
        await wait_until(lambda: model.connection.status == "reconnecting")
        await it.run.server.bus.publish(Log(level="info", message="while the forward was down"))
        await tunnel.open(tg)
        await wait_until(lambda: model.connection.status == "attached")
        await wait_until(
            lambda: any(line.message == "while the forward was down" for line in model.logs)
        )
        link.detach()
        await wait_until(lambda: model.connection.status == "detached")
        tunnel.cut()


async def test_a_local_session_whose_process_is_gone_ends_the_client_at_once(
    tmp_path, monkeypatch, capsys
) -> None:
    """The owner's rule: a local session that is gone ends the client; a remote one is retried."""
    monkeypatch.setattr(connection_module, "REDIAL_DELAYS_S", (60.0,))
    monkeypatch.setattr(attach_module, "REDIAL_DELAYS_S", (60.0,))
    model = SessionModel()
    gone = {"now": False}
    codes: list[int] = []
    async with anyio.create_task_group() as tg, tunnelled(tmp_path, tg) as (run, tunnel):
        link = Link(
            protocol_connector(tunnel.address, run.token),  # type: ignore[attr-defined]
            model,
            lambda change: None,
            anyio.current_time,
            gone=lambda: gone["now"],
        )
        tg.start_soon(link.run)
        await wait_until(lambda: model.connection.status == "attached")
        gone["now"] = True
        tunnel.cut()
        # No 60-second wait: the link ends at once and says why.
        await wait_until(lambda: model.connection.status == "detached", timeout=5)
        assert link.closed_reason == attach_module.ENDED_GONE
        assert not link.lost

        typed_quit = anyio.Event()

        async def read_lines(send):
            await typed_quit.wait()

        monkeypatch.setattr(attach_module, "read_lines", read_lines)
        await tunnel.open(tg)
        bus = run.server.bus  # type: ignore[attr-defined]

        async def attached() -> None:
            codes.append(
                await attach(tunnel.address, run.token, gone=lambda: True)  # type: ignore[attr-defined]
            )

        tg.start_soon(attached)
        await wait_until(lambda: bus.clients >= 1)
        tunnel.cut()
        await wait_until(lambda: bool(codes), timeout=5)
        typed_quit.set()
    assert codes == [0]
    assert f"[detached] {attach_module.ENDED_GONE}" in capsys.readouterr().out


async def test_a_remote_session_is_still_tried_again(tmp_path, monkeypatch) -> None:
    """Without the check (an address given on the command line) the client keeps trying."""
    monkeypatch.setattr(connection_module, "REDIAL_DELAYS_S", (0.1,))
    model = SessionModel()
    async with anyio.create_task_group() as tg, tunnelled(tmp_path, tg) as (run, tunnel):
        link = Link(
            protocol_connector(tunnel.address, run.token),  # type: ignore[attr-defined]
            model,
            lambda change: None,
            anyio.current_time,
            gone=lambda: False,
        )
        tg.start_soon(link.run)
        await wait_until(lambda: model.connection.status == "attached")
        tunnel.cut()
        await wait_until(lambda: model.connection.attempt >= 2)
        assert model.connection.status == "reconnecting"
        link.detach()
        await wait_until(lambda: model.connection.status == "detached")


async def test_the_line_client_lists_and_switches_through_the_relay(
    tmp_path, monkeypatch, capsys
) -> None:
    from calu.protocol.client import RelayTarget
    from tests.unit.test_relay import SESSION_ID, TOKEN, relayed

    async def read_lines(send):
        await send("/running")
        await send("/switch 5e55")

    monkeypatch.setattr(attach_module, "read_lines", read_lines)
    async with relayed(tmp_path) as it:
        relay = RelayTarget(it.relay.address, TOKEN, "box", "dev")
        switched: list[str] = []
        with anyio.fail_after(10):
            code = await attach(
                relay.session_address(SESSION_ID), TOKEN, relay=relay, switch_to=switched
            )
    out = capsys.readouterr().out
    assert code == 0
    assert switched == [SESSION_ID]
    assert f"{SESSION_ID}  " in out and "[running] 1 session(s)" in out
