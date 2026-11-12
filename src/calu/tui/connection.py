"""The connection of the full-screen client to a session, and the loop that keeps it.

    Connector ──► Connection ──(method, params)──► Link ──► SessionModel.apply ──► notify(Change)

`Connection` is what the client needs from a protocol client: requests, the last sequence
number, and why the connection closed. `calu.protocol.client.ProtocolClient` fits it as it is;
`calu.tui.fake.FakeConnection` fits it for tests. A `Connector` opens a connection that
continues after a sequence number; `protocol_connector` wraps `calu.protocol.client.connect`.

`Link.run` is the reconnect loop of `calu.attach.attach` with the model in place of the
printer: on the first attach it takes the replayed events and a snapshot; when the session
closes the connection because this client fell behind, it connects again after the last event
it applied, at most `calu.attach.MAX_RECONNECTS` times in a row without an event in between;
when the session can no longer replay the missed events, it forgets the session data, applies
the events it did get and takes the rest from a snapshot.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import anyio

from calu.attach import MAX_RECONNECTS
from calu.protocol.client import OnEvent, RequestFailed, connect, first_set
from calu.tui.capabilities import Capabilities, capabilities
from calu.tui.model import Change, Notice, SessionModel

CLIENT_NAME = "calu attach (full screen)"
"""How this client names itself in `initialize`, for the session's log."""


class Connection(Protocol):
    """What the client uses of an open protocol connection."""

    last_seq: int
    """Highest sequence number received so far."""
    complete: bool
    """Whether the replayed events continued without a gap."""
    closed: anyio.Event
    """Set when the connection has ended."""

    @property
    def fell_behind(self) -> bool:
        """Whether the session closed the connection because the client fell behind.

        Returns:
            True if connecting again after `last_seq` is the right answer.
        """
        ...

    def describe_close(self) -> str:
        """Say why the connection ended.

        Returns:
            The reason for a message to the user.
        """
        ...

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and wait for its result.

        Args:
            method: The request method.
            params: Its params.

        Returns:
            The result of the response.

        Raises:
            RequestFailed: If the session answered with an error.
            ConnectionError: If the connection closed before the response arrived.
        """
        ...


Connector = Callable[[OnEvent, int], AbstractAsyncContextManager[Connection]]
"""Opens a connection: `connector(on_event, after_seq)`. The replayed events after `after_seq`
reach `on_event` before the context manager yields the connection."""


def protocol_connector(address: str, token: str) -> Connector:
    """Return the connector for a real session.

    Args:
        address: The address `calu run` printed (`ws://host:port` or `unix:<path>`).
        token: The client token.

    Returns:
        A connector that calls `calu.protocol.client.connect`; entering its context manager
        raises `OSError` for an unreachable address and `RequestFailed` for a refused token.
    """

    def open_connection(
        on_event: OnEvent, after_seq: int
    ) -> AbstractAsyncContextManager[Connection]:
        """Open one connection.

        Args:
            on_event: Called for every event.
            after_seq: Sequence number to continue after.

        Returns:
            The context manager of `connect`.
        """
        return connect(address, token, CLIENT_NAME, on_event, after_seq=after_seq)

    return open_connection


class Link:
    """Keeps one model in step with one session until the user detaches.

    `connection` is the open connection or None; `Actions` sends its requests through it.
    `error` is set when the first connection could not be opened, `closed_reason` when the
    session ended the connection for good.
    """

    def __init__(
        self,
        connector: Connector,
        model: SessionModel,
        notify: Callable[[Change], None],
        clock: Callable[[], float],
        after_seq: int = 0,
    ) -> None:
        """Create the link; `run` starts it.

        Args:
            connector: Opens a connection to the session.
            model: The model that receives the events.
            notify: Called with every change of the model; it must return quickly and must
                not send requests, because the connection reads no response while it runs.
            clock: The client clock in seconds (monotonic).
            after_seq: Sequence number the first subscription continues after.
        """
        self.connector = connector
        self.model = model
        self.notify = notify
        self.clock = clock
        self.after_seq = after_seq
        self.connection: Connection | None = None
        self.capabilities = Capabilities()
        self.error: BaseException | None = None
        self.closed_reason = ""
        self.stop = anyio.Event()
        self._stale = anyio.Event()
        self._replaying = False
        self._seen_event = False
        self._held: list[tuple[str, dict[str, Any]]] | None = None
        """The events replayed by a reconnect, until it is known whether a gap precedes them."""

    def detach(self) -> None:
        """Ask `run` to close the connection and return. The session keeps running."""
        self.stop.set()

    async def _on_event(self, method: str, params: dict[str, Any]) -> None:
        """Apply one event to the model and report the change.

        A replayed event gets no time. A change of the session state that involves a start
        marks the snapshot as stale, because model and effort are not part of any event. While
        a reconnect replays, the events are held back until `run` knows whether the replay
        continues without a gap (`_release`).

        Args:
            method: The notification method.
            params: Its params.
        """
        if self._held is not None:
            self._held.append((method, params))
            return
        change = Change()
        if not self._seen_event:
            self._seen_event = True
            if self._replaying and self.after_seq == 0 and int(params.get("seq") or 0) > 1:
                change.merge(
                    self.model.note(
                        "above: events the session no longer holds for replay "
                        "(/history shows the stored conversation)"
                    )
                )
        before = self.model.state
        change.merge(self.model.apply(method, params, None if self._replaying else self.clock()))
        after = self.model.state
        restarted = "starting" in {before, after} or before in {"stopped", "failed"}
        if not self._replaying and before != after and restarted:
            self._stale.set()
        if change:
            self.notify(change)

    async def _release(self, rebuild: bool) -> None:
        """Apply the events a reconnect replayed, after a reset if a gap precedes them.

        Args:
            rebuild: The session could not replay every missed event: forget the session data
                first and say so in the transcript; the replayed events follow that line.
        """
        held, self._held = self._held or [], None
        if rebuild:
            self.model.reset()
            change = Change(rebuilt=True)
            change.merge(
                self.model.note(
                    "the session could not replay every event this client missed: "
                    "the view was rebuilt from the session's current state"
                )
            )
            self.notify(change)
        self._replaying = True
        for method, params in held:
            await self._on_event(method, params)
        self._replaying = False

    async def _snapshot(self, connection: Connection) -> None:
        """Ask for `state.get` and load it into the model.

        Args:
            connection: The open connection.

        Raises:
            RequestFailed: If the session refused the request.
            ConnectionError: If the connection closed before the answer arrived.
        """
        snapshot = await connection.request("state.get")
        change = Change()
        if isinstance(snapshot, dict):
            self.capabilities = capabilities(snapshot)
            change.merge(self.model.load_snapshot(snapshot, self.clock()))
        self.notify(change)

    async def run(self) -> None:
        """Connect, keep the model current, and reconnect after falling behind.

        Returns when the user detached (`detach`), when the first connection failed (`error`
        is then set), or when the session closed the connection for another reason than this
        client falling behind (`closed_reason` is then set). The connection state of the model
        says which.
        """
        after_seq = self.after_seq
        reconnects = 0
        first = True
        while not self.stop.is_set():
            self._replaying = True
            self._held = None if first else []
            try:
                async with self.connector(self._on_event, after_seq) as connection:
                    self._replaying = False
                    self.connection = connection
                    if not first:
                        await self._release(rebuild=not connection.complete)
                    await self._snapshot(connection)
                    first = False
                    self.notify(self.model.set_connection("attached"))
                    while not (self.stop.is_set() or connection.closed.is_set()):
                        await first_set(self.stop, connection.closed, self._stale)
                        if self._stale.is_set():
                            self._stale = anyio.Event()
                            if not (self.stop.is_set() or connection.closed.is_set()):
                                await self._snapshot(connection)
                    if connection.last_seq > after_seq:
                        reconnects = 0
                    after_seq = connection.last_seq
                    fell_behind = connection.fell_behind
                    reason = connection.describe_close()
            except (OSError, RequestFailed) as error:  # ConnectionError is an OSError
                if first:
                    self.error = error
                    self.connection = None
                    self.notify(self.model.set_connection("detached", reason=str(error)))
                    return
                fell_behind, reason = False, str(error)
            self.connection = None
            if self.stop.is_set():
                self.notify(self.model.set_connection("detached", reason="detached by you"))
                return
            if fell_behind and reconnects < MAX_RECONNECTS:
                reconnects += 1
                self.notify(self.model.set_connection("reconnecting", reconnects, MAX_RECONNECTS))
                continue
            self.closed_reason = reason
            change = self.model.set_connection("detached", reason=reason)
            change.notices.append(Notice(f"the session closed the connection: {reason}", "error"))
            self.notify(change)
            return
