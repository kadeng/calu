"""Entry point of `calu acp`: ACP on stdin and stdout, calu's protocol on a WebSocket.

    calu acp ws://127.0.0.1:8765 --token <token>

The editor spawns this process (that is how ACP works: the client launches the agent as a
subprocess) and it attaches to the session that `calu run` already started. Several editors may
spawn their own adapter for the same session; the session's bus fans the events out.

**stdout carries protocol lines only.** Everything else — logs, warnings, errors — goes to
stderr, because one stray line of text on stdout breaks the JSON-RPC stream.
"""

import logging
import sys
from pathlib import Path
from typing import Any

import anyio

from calu import __version__
from calu.acp.adapter import Adapter
from calu.acp.peer import Peer
from calu.protocol.client import connect

log = logging.getLogger(__name__)


async def serve_acp(address: str, token: str, name: str = "calu") -> int:
    """Attach to a calu session and serve it to an ACP client on stdin and stdout.

    Runs until the client closes its end of the pipe (end of input on stdin) or the session
    closes the WebSocket. Side effects: sends the session's events to the client as
    `session/update` notifications, and the client's prompts and decisions to the session.

    Args:
        address: WebSocket address of the session, as `calu run` printed it.
        token: Client token of the session, as `calu run` printed it.
        name: Name of the agent as the editor shows it.

    Returns:
        0 when the client disconnected, 1 if the session could not be reached.

    Raises:
        OSError: If the address cannot be reached.
        calu.protocol.client.RequestFailed: If the session refuses the token.
    """
    _log_to_stderr()
    holder: list[Adapter] = []

    async def on_event(method: str, params: dict[str, Any]) -> None:
        """Pass one session event to the adapter, once it exists.

        Args:
            method: The event method.
            params: The event's params.
        """
        if holder:
            await holder[0].on_event(method, params)

    async with connect(address, token, "calu acp", on_event, client_version=__version__) as client:
        snapshot = await client.request("state.get")
        workspace = Path(str(snapshot.get("workspace") or "."))
        adapter = Adapter(client, workspace, name=name, version=__version__)
        adapter.state = str(snapshot.get("state", "starting"))
        holder.append(adapter)
        peer = Peer(_read_line, _write_line, adapter.handle)
        log.info("serving ACP for the calu session in %s", workspace)
        async with anyio.create_task_group() as tasks:
            adapter.attach(peer, tasks)
            tasks.start_soon(peer.serve)
            await peer.closed.wait()
            tasks.cancel_scope.cancel()
    return 0


def _log_to_stderr() -> None:
    """Send log records to stderr, so that stdout stays a clean protocol stream.

    Side effect: adds a handler to the root logger if it has none.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO, format="calu acp: %(levelname)s %(message)s"
        )


_stdin = anyio.wrap_file(sys.stdin)
_stdout = anyio.wrap_file(sys.stdout)


async def _read_line() -> str:
    """Read one line from stdin.

    Returns:
        The line including its newline, or "" at end of input.
    """
    return await _stdin.readline()


async def _write_line(line: str) -> None:
    """Write one protocol line to stdout and flush it.

    Args:
        line: The JSON message, without a newline.
    """
    await _stdout.write(line + "\n")
    await _stdout.flush()
