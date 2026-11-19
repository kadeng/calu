"""The connection of `calu ide` to one calu session, kept across reconnects.

    session ──events──► SessionLink._on_event ──► queue ──► forwarder ──► editor
                          │ while connecting first:            (starts when `release` is
                          └► `first["events"]` (the replay)     called: after the response
                                                                 to `ide.attach` is written)

    the connection ends:
      token revoked (4001), or the token is refused ─► `ide.link lost`
      anything else ─► `ide.link reconnecting`, connect again with `after_seq`:
                       at once after 4002 (the bridge fell behind), else with a growing wait;
                       connected ─► `ide.link attached`
                       not connected within `reconnect_wait_s` ─► `ide.link lost`
                       `MAX_RECONNECTS` connections in a row without a new event ─► lost

The editor gets every event once and in the session's order: an event whose `seq` is not
above the last one forwarded is dropped (a replay after a reconnect may repeat events), and
one task alone writes the notifications.
"""

import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio

from calu import __version__
from calu.acp.serve import MAX_RECONNECTS, RECONNECT_WAIT_S, Link
from calu.protocol.client import RequestFailed, connect
from calu.protocol.messages import CLOSE_TOKEN_REVOKED

log = logging.getLogger(__name__)

Notify = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Sends one notification to the editor: `(method, params)`."""

FIRST_WAIT_S = 0.25
"""Seconds before the second attempt to connect again; doubled after each attempt."""

LONGEST_WAIT_S = 2.0
"""The longest wait between two attempts to connect again."""


@dataclass(frozen=True)
class Endpoint:
    """Where the bridge connects, and with which token."""

    address: str
    """`ws://host:port` or `unix:<path>`."""
    token: str
    """A client token of the session; "" for a socket that asks for none."""
    token_id: int | None = None
    """The id of a token the bridge minted, to be revoked at the detach; None otherwise."""
    session_id: str = ""
    """The id of the session's row; "" if the bridge was given an address only."""
    password: str | None = None
    """The password of the credential the token was minted with, kept in memory only for
    revoking that token at the detach; None if none was needed."""


class SessionLink:
    """One attached session: the connection, its reconnects and the forwarding of events."""

    def __init__(
        self, endpoint: Endpoint, notify: Notify, reconnect_wait_s: float = RECONNECT_WAIT_S
    ) -> None:
        """Create the link; `run` connects.

        Args:
            endpoint: Where to connect.
            notify: Sends a notification to the editor.
            reconnect_wait_s: How long the link tries to connect again after a loss.
        """
        self.endpoint = endpoint
        self.session_id = endpoint.session_id
        self.reconnect_wait_s = reconnect_wait_s
        self.started = anyio.Event()
        """Set when the first connection stands (`first` is filled) or failed (`error`)."""
        self.ended = anyio.Event()
        """Set when `run` has returned."""
        self.first: dict[str, Any] = {}
        """`initialize`, `snapshot`, `events` and `complete` of the first connection."""
        self.error: Exception | None = None
        """Why the first connection failed; None if it stands."""
        self.is_lost = False
        """Whether the link gave up; the editor was told with `ide.link lost`."""
        self._notify = notify
        self._link = Link()
        self._scope = anyio.CancelScope()
        self._released = anyio.Event()
        self._replayed: list[dict[str, Any]] | None = []
        self._last_seq = 0
        self._send, self._receive = anyio.create_memory_object_stream[tuple[str, dict[str, Any]]](
            math.inf
        )

    async def run(self, after_seq: int = 0) -> None:
        """Connect, forward the session's events and reconnect until `stop` or the loss.

        Side effects: sets `started` and, at the end, `ended`; sends `session.event` and
        `ide.link` notifications once `release` was called. Never raises for a connection
        that fails: the first failure is stored in `error`, a later one ends in `ide.link
        lost`.

        Args:
            after_seq: The sequence number the editor has already seen; 0 for all the
                events the session still holds.
        """
        self._last_seq = max(0, after_seq)
        try:
            with self._scope:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._forward)
                    await self._connections()
                    if self.error is not None:
                        tasks.cancel_scope.cancel()
                    # After a loss the forwarder still has to deliver what is queued, the
                    # `lost` notification last; `stop` ends it.
        finally:
            self._link.lost()
            self.started.set()
            self.ended.set()

    def release(self) -> None:
        """Let the queued notifications flow: the response to the attach request is written."""
        self._released.set()

    def stop(self) -> None:
        """End `run`: close the connection and stop forwarding. Queued events are dropped."""
        self._scope.cancel()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one request of the session protocol to the session.

        While the link is connecting again the request waits for the connection
        (`calu.acp.serve.Link.request`).

        Args:
            method: The request method, e.g. "chat.send".
            params: Its params.

        Returns:
            The session's result.

        Raises:
            ConnectionError: If the link is lost, did not come back in time, or the
                connection closed before the answer arrived.
            calu.protocol.client.RequestFailed: If the session answered with an error.
        """
        return await self._link.request(method, params)

    async def _connections(self) -> None:
        """Hold one connection after the other until the link is lost.

        Returns after the first connection failed (`error` is set) or after `ide.link lost`
        was queued.
        """
        endpoint = self.endpoint
        first = True
        without_progress = 0
        lost_at: float | None = None
        wait_s = 0.0
        while True:
            before = self._last_seq
            final = False
            at_once = False
            stood = False
            try:
                async with connect(
                    endpoint.address,
                    endpoint.token,
                    "calu ide",
                    self._on_event,
                    client_version=__version__,
                    after_seq=self._last_seq,
                ) as client:
                    if first:
                        snapshot = await client.request("state.get")
                        replayed, self._replayed = self._replayed or [], None
                        self.session_id = str(
                            client.initialize.get("calu_session_id") or self.session_id
                        )
                        self.first = {
                            "initialize": client.initialize,
                            "snapshot": snapshot,
                            "events": sorted(replayed, key=lambda e: e["params"].get("seq") or 0),
                            "complete": client.complete,
                        }
                    self._queue_link(
                        "attached",
                        "attached to the session"
                        if first
                        else "connected to the session again"
                        if client.complete
                        else "connected to the session again; it no longer holds every "
                        "missed event, so read its state again (state.get)",
                        complete=client.complete,
                    )
                    self._link.connected(client)
                    if first:
                        first = False
                        self.started.set()
                    lost_at, wait_s, stood = None, 0.0, True
                    await client.closed.wait()
                    reason = client.describe_close()
                    final = client.close_code == CLOSE_TOKEN_REVOKED
                    at_once = client.fell_behind
            except RequestFailed as error:
                reason, final = f"the session refused the connection: {error}", True
                if first:
                    self.error = error
                    return
            except (OSError, ConnectionError, TimeoutError) as error:
                reason = _connect_failure(error)
                if first:
                    self.error = error
                    return
            finally:
                self._link.disconnected()
            if self._last_seq > before:
                without_progress = 0
            elif stood:
                # A connection that stood and brought no event: counted, so that a session
                # that closes every new connection at once does not keep the link busy.
                without_progress += 1
            now = time.monotonic()
            lost_at = now if lost_at is None else lost_at
            if final or without_progress > MAX_RECONNECTS or now - lost_at > self.reconnect_wait_s:
                log.warning("the connection to the calu session ended: %s", reason)
                self.is_lost = True
                self._link.lost()
                # The clients put "connection lost" / "reconnecting" in front: the message is
                # the reason alone.
                self._queue_link("lost", reason)
                return
            log.info("the session connection closed (%s); connecting again", reason)
            self._queue_link("reconnecting", reason)
            if not at_once:
                await anyio.sleep(wait_s)
                wait_s = min(LONGEST_WAIT_S, max(FIRST_WAIT_S, wait_s * 2))

    def _queue_link(self, state: str, message: str, complete: bool | None = None) -> None:
        """Queue an `ide.link` notification behind the events already queued.

        Args:
            state: "attached", "reconnecting" or "lost".
            message: One sentence for the editor's user.
            complete: For "attached": whether the replay had no gap; None leaves it out.
        """
        params: dict[str, Any] = {"state": state, "message": message}
        if self.session_id:
            params["session_id"] = self.session_id
        if complete is not None:
            params["complete"] = complete
        self._send.send_nowait(("ide.link", params))

    async def _on_event(self, method: str, params: dict[str, Any]) -> None:
        """Take one event of the session: into the first replay, or into the queue.

        Args:
            method: The event's method.
            params: Its params, `seq` included.
        """
        seq = int(params.get("seq") or 0)
        if seq:
            if seq <= self._last_seq:
                return
            self._last_seq = seq
        event = {"method": method, "params": params}
        if self._replayed is not None:
            self._replayed.append(event)
        else:
            self._send.send_nowait(("session.event", event))

    async def _forward(self) -> None:
        """Send the queued notifications to the editor, in order, once released.

        Runs until cancelled. A notification that cannot be written (the editor closed its
        end) is logged and dropped.
        """
        await self._released.wait()
        async for method, params in self._receive:
            try:
                await self._notify(method, params)
            except Exception:
                log.info("a notification to the editor could not be written", exc_info=True)


def _connect_failure(error: BaseException) -> str:
    """Say in words why a connection to the session could not be made.

    Args:
        error: The exception of the connection attempt.

    Returns:
        A sentence for a person: a refused connection means that nothing listens at the
        session's address (its server or the session stopped); other errors are named by
        their text without the error number.

    Example:
        `_connect_failure(ConnectionRefusedError(111, "Connection refused"))` returns
        "nothing answers at the session's address: the session or its calu server stopped".
    """
    if isinstance(error, ConnectionRefusedError):
        return "nothing answers at the session's address: the session or its calu server stopped"
    if isinstance(error, TimeoutError):
        return "the session did not answer in time"
    text = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    return f"connecting failed: {text or type(error).__name__}"
