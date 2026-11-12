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
from calu.protocol.client import (
    ProtocolClient,
    RequestFailed,
    connect,
    first_set,
    single_error,
)

log = logging.getLogger(__name__)


MAX_RECONNECTS = 3
"""How often the adapter connects again after falling behind without having received an event
in between; after that it gives up, as for any other lost connection."""

DRAIN_S = 2.0
"""Seconds the adapter gives the answers to the editor's pending requests to be written before
it ends after the session connection was lost."""


RECONNECT_WAIT_S = 15.0
"""Seconds a request of the adapter waits for the connection to come back while `bridge` is
connecting again, before it fails."""


class Link:
    """The calu session as the adapter sees it, across reconnects.

    The adapter keeps one `Link` for its whole life; `bridge` gives it the current connection
    with `connected` and takes it away with `disconnected`. A request that is made while
    `bridge` is connecting again waits for the new connection, so that a prompt typed in that
    moment is not refused; once `bridge` has given up (`lost`), requests fail at once.
    """

    def __init__(self) -> None:
        """Create a link with no connection yet."""
        self.client: ProtocolClient | None = None
        self._lost = False
        self._ready = anyio.Event()

    def connected(self, client: ProtocolClient) -> None:
        """Use a new connection for the requests from now on.

        Args:
            client: The connected protocol client.
        """
        self.client = client
        self._ready.set()

    def disconnected(self) -> None:
        """Forget the connection; requests wait until `connected` or `lost` is called."""
        self.client = None
        if self._ready.is_set():
            self._ready = anyio.Event()

    def lost(self) -> None:
        """Give up on the session: waiting and later requests fail with `ConnectionError`."""
        self.client = None
        self._lost = True
        self._ready.set()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request to the calu session over the current connection.

        Args:
            method: A calu request method, e.g. "chat.send".
            params: Its params.

        Returns:
            The result of the request.

        Raises:
            ConnectionError: If the connection to the session is lost, did not come back within
                `RECONNECT_WAIT_S`, or closed before the answer arrived. In the last case the
                session may or may not have acted on the request.
            calu.protocol.client.RequestFailed: If the session answered with an error.
        """
        if self.client is None and not self._lost:
            with anyio.move_on_after(RECONNECT_WAIT_S):
                await self._ready.wait()
        if self.client is None:
            raise ConnectionError("not connected to the calu session")
        return await self.client.request(method, params)


async def serve_acp(address: str, token: str, name: str = "calu") -> int:
    """Attach to a calu session and serve it to an ACP client on stdin and stdout.

    Runs until the client closes its end of the pipe (end of input on stdin) or the session
    closes the WebSocket (see `bridge`). Side effects: sends the session's events to the client
    as `session/update` notifications, and the client's prompts and decisions to the session.

    Args:
        address: WebSocket address of the session, as `calu run` printed it.
        token: Client token of the session, as `calu run` printed it.
        name: Name of the agent as the editor shows it.

    Returns:
        0 when the client disconnected, 1 when the connection to the session ended first.

    Raises:
        OSError: If the address cannot be reached.
        calu.protocol.client.RequestFailed: If the session refuses the token.
    """
    _log_to_stderr()
    link = Link()
    adapter = Adapter(link, Path("."), name=name, version=__version__)
    peer = Peer(_read_line, _write_line, adapter.handle)
    return await bridge(address, token, link, adapter, peer)


async def bridge(address: str, token: str, link: Link, adapter: Adapter, peer: Peer) -> int:
    """Connect an adapter to its calu session and run the ACP peer until one side ends.

        editor ends its input            -> return 0
        session closes the connection:
          because the adapter fell behind -> connect again, replay from the last event seen
          for any other reason            -> tell the editor, answer the waiting prompts,
                                             return 1 (the editor can then restart the agent)

    The adapter exists before the connection is opened and receives every event from the first
    one on, including the events replayed while connecting. Nothing is sent to the editor
    before `session/new`, but an escalation that is open at that point is remembered and asked
    then. The peer starts once the first connection stands and the workspace is known.

    After a reconnect the adapter takes a fresh `state.get`, which brings the open escalations
    with it; if the session could no longer replay every missed event, prompts that wait on a
    session that is no longer working are answered (`Adapter.sync`).

    Args:
        address: WebSocket address of the session.
        token: Client token of the session.
        link: The link the adapter was created with; it gets each connection from here.
        adapter: The adapter, not yet attached to the peer.
        peer: The ACP peer whose handler is `adapter.handle`; not yet running.

    Returns:
        0 when the editor disconnected, 1 when the connection to the session ended first.

    Raises:
        OSError: If the address cannot be reached at the first connect.
        calu.protocol.client.RequestFailed: If the session refuses the token at the first
            connect.
    """
    after_seq = 0
    reconnects = 0
    serving = False
    code = 1
    try:
        async with anyio.create_task_group() as tasks:
            adapter.attach(peer, tasks)
            while True:
                try:
                    async with connect(
                        address,
                        token,
                        "calu acp",
                        adapter.on_event,
                        client_version=__version__,
                        after_seq=after_seq,
                    ) as client:
                        link.connected(client)
                        snapshot = await client.request("state.get")
                        adapter.workspace = Path(str(snapshot.get("workspace") or "."))
                        adapter.sync(snapshot, complete=client.complete or not serving)
                        if not serving:
                            serving = True
                            tasks.start_soon(peer.serve)
                            log.info("serving ACP for the calu session in %s", adapter.workspace)
                        await first_set(peer.closed, client.closed)
                        if client.last_seq > after_seq:
                            reconnects = 0
                        after_seq = client.last_seq
                        reason = client.describe_close()
                        again = client.fell_behind and reconnects < MAX_RECONNECTS
                except (OSError, RequestFailed) as error:
                    if not serving:
                        raise
                    reason, again = f"connecting again failed: {error}", False
                finally:
                    link.disconnected()
                if peer.closed.is_set():
                    code = 0
                    break
                if again:
                    reconnects += 1
                    log.warning("the session closed the connection (%s); connecting again", reason)
                    continue
                log.warning("the connection to the calu session ended: %s", reason)
                link.lost()
                await adapter.connection_lost(reason)
                with anyio.move_on_after(DRAIN_S):
                    await peer.idle()
                code = 1
                break
            tasks.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        # The task group wraps every error in a group; the caller handles the error itself
        # (a refused token, an unreachable address), as this function's contract says.
        error = single_error(group)
        raise error from None
    return code


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
