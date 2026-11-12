"""The connection of the full-screen client to a session, and the loop that keeps it.

    Connector ──► Connection ──(method, params)──► Link ──► SessionModel.apply ──► notify(Change)

`Connection` is what the client needs from a protocol client: requests, the last sequence
number, and why the connection closed. `calu.protocol.client.ProtocolClient` fits it as it is;
`calu.tui.fake.FakeConnection` fits it for tests. A `Connector` opens a connection that
continues after a sequence number; `protocol_connector` wraps `calu.protocol.client.connect`.

`Link.run` is the reconnect loop of `calu.attach.attach` with the model in place of the
printer: on the first attach it takes the stored history of the session (`session.history`),
the replayed events below it and a snapshot; when the session
closes the connection because this client fell behind, it connects again after the last event
it applied, at most `calu.attach.MAX_RECONNECTS` times in a row without an event in between;
when the session can no longer replay the missed events, it forgets the session data, loads
the stored history again, applies the events it did get and takes the rest from a snapshot.

While the client has handed the terminal to another program (`f4`), it reads nothing, and the
session may close the connection because its keepalive got no answer. The app announces such
a stay (`Link.expect_drop`); a connection that closes during it or shortly after is opened
again in the same way, unless the token was revoked.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import anyio

from calu.attach import MAX_RECONNECTS
from calu.protocol.client import OnEvent, RequestFailed, connect, first_set
from calu.protocol.messages import CLOSE_TOKEN_REVOKED
from calu.tui.capabilities import Capabilities, capabilities
from calu.tui.model import Change, Notice, SessionModel

HISTORY_STEPS = 500
"""How many stored steps (the newest) the client asks for when it attaches."""

HISTORY_TIMEOUT_S = 8.0
"""How long the client waits for the stored history before it shows the session without it."""

DROP_GRACE_S = 15.0
"""After the client took the terminal back from a program it had handed it to, a closed
connection is still put down to that stay for this many seconds."""

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
    close_code: int | None
    """The WebSocket close code once `closed` is set; None without a close frame."""

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
        history: bool = True,
    ) -> None:
        """Create the link; `run` starts it.

        Args:
            connector: Opens a connection to the session.
            model: The model that receives the events.
            notify: Called with every change of the model; it must return quickly and must
                not send requests, because the connection reads no response while it runs.
            clock: The client clock in seconds (monotonic).
            after_seq: Sequence number the first subscription continues after.
            history: Whether the stored history of the session is loaded above the replayed
                events on the first attach (not with an `after_seq` above 0) and after a
                rebuild.
        """
        self.connector = connector
        self.model = model
        self.history = history
        model.clock = clock
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
        self._reload = False
        self._held: list[tuple[str, dict[str, Any]]] | None = None
        """The events replayed by a reconnect, until it is known whether a gap precedes them."""
        self._drop_expected = False
        self._drop_until: float | None = None

    def expect_drop(self) -> None:
        """Announce that the client stops reading for a while (it hands the terminal over).

        Until `drop_over` and `DROP_GRACE_S` seconds beyond it, or until the next snapshot
        after a reconnect, a connection the session closes is opened again as after falling
        behind. A revoked token (`CLOSE_TOKEN_REVOKED`) still ends the link.
        """
        self._drop_expected = True
        self._drop_until = None

    def drop_over(self) -> None:
        """Say that the client reads again; the announcement of `expect_drop` now runs out."""
        if self._drop_expected:
            self._drop_until = self.clock() + DROP_GRACE_S

    def _excused(self, connection: Connection) -> bool:
        """Tell whether a closed connection is put down to an announced stay.

        Args:
            connection: The connection that closed.

        Returns:
            True if `expect_drop` is in force and the token was not revoked.
        """
        if not self._drop_expected or connection.close_code == CLOSE_TOKEN_REVOKED:
            return False
        if self._drop_until is not None and self.clock() > self._drop_until:
            self._drop_expected = False
            return False
        return True

    def reload(self) -> None:
        """Ask `run` to forget the session data and build the view again.

        Used after the client started the session with another conversation: the stored
        history of the conversation that runs now is loaded, then a snapshot.
        """
        self._reload = True
        self._stale.set()

    def detach(self) -> None:
        """Ask `run` to close the connection and return. The session keeps running."""
        self.stop.set()

    async def _on_event(self, method: str, params: dict[str, Any]) -> None:
        """Apply one event to the model and report the change.

        A replayed event gets no time. A change of the session state that involves a start
        marks the snapshot as stale, because model and effort are not part of any event. While
        a connection replays, and until the stored history is loaded, the events are held back
        (`_release`), so that they are applied below what precedes them.

        Args:
            method: The notification method.
            params: Its params.
        """
        if self._held is not None:
            self._held.append((method, params))
            return
        change = Change()
        before = self.model.state
        change.merge(self.model.apply(method, params, None if self._replaying else self.clock()))
        after = self.model.state
        restarted = "starting" in {before, after} or before in {"stopped", "failed"}
        if not self._replaying and before != after and restarted:
            self._stale.set()
        if change:
            self.notify(change)

    async def _history(self, connection: Connection) -> dict[str, Any] | None:
        """Ask the session for its stored conversation (`session.history`).

        Args:
            connection: The open connection.

        Returns:
            The result, or None if the session refused, answered with something else than an
            object, or took longer than `HISTORY_TIMEOUT_S`.
        """
        result: Any = None
        with anyio.move_on_after(HISTORY_TIMEOUT_S):
            try:
                result = await connection.request("session.history", {"limit": HISTORY_STEPS})
            except RequestFailed:
                return None
        return result if isinstance(result, dict) else None

    async def _release(self, connection: Connection, first: bool, reload: bool = False) -> None:
        """Apply the events a connection replayed, below what the session has stored.

        On the first attach, and after a reconnect whose replay has a gap, the model starts
        empty: the stored history of the session comes first (`SessionModel.load_history`,
        without what the replayed events cover), then the replayed events. A line of the client
        says where a gap is: after a rebuild, and when the replay does not reach back to the
        start of the session and the session has no stored history for the part before.

        Args:
            connection: The open connection.
            first: Whether this is the first attach of this link.
            reload: The model was emptied on purpose (`reload`): load the stored history
                whatever sequence number the link started after, and name no gap.
        """
        rebuild = not first and not connection.complete
        if rebuild:
            self.model.reset()
            self.notify(Change(rebuilt=True))
        if first or rebuild:
            stored = Change()
            wanted = self.history and (rebuild or reload or self.after_seq == 0)
            raw = await self._history(connection) if wanted else None
            held = self._held or []
            if raw is not None:
                ids = [str(params.get("tool_use_id") or "") for _, params in held]
                inputs = [
                    str(params.get("text") or "")
                    for method, params in held
                    if method == "user.input"
                ]
                stored = self.model.load_history(raw, [i for i in ids if i], inputs)
            gap = not reload and bool(held) and int(held[0][1].get("seq") or 0) > 1
            if rebuild:
                stored.merge(
                    self.model.note(
                        "the session could not replay every event this client missed: "
                        "the view was rebuilt from the session's current state"
                    )
                )
            elif gap and not stored.blocks_added and self.after_seq == 0:
                stored.merge(
                    self.model.note(
                        "above: events the session no longer holds for replay "
                        "(/history shows the stored conversation)"
                    )
                )
            if stored:
                self.notify(stored)
        held, self._held = self._held or [], None
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
        client falling behind or an announced stay away (`closed_reason` is then set). The
        connection state of the model says which. A connection the session closes while the
        attach itself still asks (`state.get`, the stored history) counts like one closed
        afterwards.
        """
        after_seq = self.after_seq
        reconnects = 0
        first = True
        while not self.stop.is_set():
            self._replaying = True
            self._held = []
            connection: Connection | None = None
            try:
                async with self.connector(self._on_event, after_seq) as connection:
                    self._replaying = False
                    self.connection = connection
                    await self._release(connection, first)
                    await self._snapshot(connection)
                    first = False
                    if self._drop_until is not None:
                        self._drop_expected = False
                    self.notify(self.model.set_connection("attached"))
                    while not (self.stop.is_set() or connection.closed.is_set()):
                        await first_set(self.stop, connection.closed, self._stale)
                        if self._stale.is_set():
                            self._stale = anyio.Event()
                            if not (self.stop.is_set() or connection.closed.is_set()):
                                if self._reload:
                                    self._reload = False
                                    self._held = []
                                    self.model.reset()
                                    self.notify(Change(rebuilt=True))
                                    await self._release(connection, first=True, reload=True)
                                await self._snapshot(connection)
                    if connection.last_seq > after_seq:
                        reconnects = 0
                    after_seq = connection.last_seq
                    again = connection.fell_behind or self._excused(connection)
                    reason = connection.describe_close()
            except (OSError, RequestFailed) as error:  # ConnectionError is an OSError
                # The session may have closed the connection while a request of the attach
                # was awaited (`state.get`, `session.history`): what it said still counts.
                again = connection is not None and (
                    connection.fell_behind
                    or (connection.closed.is_set() and self._excused(connection))
                )
                if connection is not None and self._held is None:
                    after_seq = max(after_seq, connection.last_seq)  # these were applied
                reason = str(error)
                if connection is not None and connection.closed.is_set():
                    reason = connection.describe_close() or reason
                if first and not again:
                    self.error = error
                    self.connection = None
                    self.notify(self.model.set_connection("detached", reason=str(error)))
                    return
            self.connection = None
            if self.stop.is_set():
                self.notify(self.model.set_connection("detached", reason="detached by you"))
                return
            if again and reconnects < MAX_RECONNECTS:
                reconnects += 1
                self.notify(self.model.set_connection("reconnecting", reconnects, MAX_RECONNECTS))
                continue
            self.closed_reason = reason
            change = self.model.set_connection("detached", reason=reason)
            change.notices.append(Notice(f"the session closed the connection: {reason}", "error"))
            self.notify(change)
            return
