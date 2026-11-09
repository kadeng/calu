"""The event bus of one session process: fans events out to every client and keeps them for replay.

    Session ──emit──► Bus.publish ──┬──► client 1 (WebSocket)
                                    ├──► client 2
                                    └──► ring buffer (the last `capacity` events)

Every event gets a sequence number, counted from 1 per calu process (`Event.seq`). A client that
connects takes the current state (`state.get`) and then subscribes with the sequence number it
has reached; the bus hands it the events after that number from the ring buffer and sends the
following ones as they happen. A client that was away longer than the buffer reaches is told so
(`EventsSubscribeResult.complete` is False) and should take a fresh snapshot.

Token streams are not written to the database (PLAN.md D3), so the buffer is the only place
where an event lives after it was sent; it is lost when the process ends. What a client needs
after a restart (steps, turns, escalations) is in the database.

A slow client must not slow the session down: each client has a bounded queue, and a client
whose queue is full is disconnected (`Subscription.overflowed`) instead of blocking `publish`.
"""

import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from itertools import count

import anyio

from calu.protocol.messages import Event, RecordedEvent

log = logging.getLogger(__name__)

CAPACITY = 5_000
"""Events kept for replay; roughly the last few minutes of a busy session."""
CLIENT_QUEUE = 1_000
"""Events a client may fall behind before it is disconnected."""


@dataclass
class Subscription:
    """One client's view of the stream. Iterate `events()` and stop when it ends.

    `overflowed` is True if the client fell more than `CLIENT_QUEUE` events behind; the stream
    then ends and the client must reconnect and take a fresh snapshot.
    """

    bus: Bus
    queue: deque[Event] = field(default_factory=deque)
    overflowed: bool = False
    closed: bool = False
    _ready: anyio.Event = field(default_factory=anyio.Event)

    def _push(self, event: Event) -> None:
        """Add an event to the client's queue, or mark the subscription as overflowed.

        Args:
            event: The event to deliver.
        """
        if self.overflowed or self.closed:
            return
        if len(self.queue) >= CLIENT_QUEUE:
            self.overflowed = True
            self.queue.clear()
        else:
            self.queue.append(event)
        self._ready.set()

    def close(self) -> None:
        """End the subscription; `events()` returns after the events already queued."""
        self.closed = True
        self.bus.unsubscribe(self)
        self._ready.set()

    async def events(self) -> AsyncIterator[Event]:
        """Yield the events for this client until it is closed or overflowed.

        Yields:
            Each event in the order the bus published it.
        """
        while True:
            while self.queue:
                yield self.queue.popleft()
            if self.closed or self.overflowed:
                return
            await self._ready.wait()
            self._ready = anyio.Event()


class Bus:
    """Publishes the events of one session to its clients and keeps the recent ones."""

    def __init__(self, capacity: int = CAPACITY) -> None:
        """Create an empty bus with no subscribers.

        Args:
            capacity: How many events are kept for replay.
        """
        self._seq = count(1)
        self.last_seq = 0
        self._recent: deque[tuple[int, RecordedEvent]] = deque(maxlen=capacity)
        self._clients: list[Subscription] = []

    async def publish(self, event: Event) -> None:
        """Number `event`, keep it for replay and hand it to every client. Never raises.

        This is the session's event sink (`Session(emit=bus.publish)`). It does not wait for any
        client: an event for a client that has fallen behind is dropped and the client's
        subscription is marked as overflowed.

        Args:
            event: The event; its `seq` is set to the next sequence number.
        """
        event.seq = self.last_seq = next(self._seq)
        self._recent.append(
            (event.seq, RecordedEvent(method=event.method, params=event.model_dump(mode="json")))
        )
        for client in list(self._clients):
            client._push(event)

    def subscribe(self) -> Subscription:
        """Add a client to the fan-out.

        Returns:
            Its subscription; the caller iterates `events()` and calls `close()` at the end.
        """
        subscription = Subscription(bus=self)
        self._clients.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a client from the fan-out; unknown subscriptions are ignored.

        Args:
            subscription: The subscription to remove.
        """
        if subscription in self._clients:
            self._clients.remove(subscription)

    @property
    def clients(self) -> int:
        """Number of subscribed clients.

        Returns:
            How many subscriptions the bus fans out to.
        """
        return len(self._clients)

    def replay(self, after_seq: int) -> tuple[list[RecordedEvent], bool]:
        """Return the kept events after `after_seq`, oldest first.

        Args:
            after_seq: The sequence number the client has already seen; 0 for everything the
                buffer holds.

        Returns:
            The events, and whether they continue without a gap after `after_seq`: False if
            earlier events were already dropped from the buffer, so the client cannot rebuild
            its view from them alone and should take a fresh snapshot. A client that asks with
            0 on a long-running session therefore gets False, which is correct: the buffer no
            longer holds the start of the session.
        """
        events = [recorded for seq, recorded in self._recent if seq > after_seq]
        complete = not self._recent or after_seq + 1 >= self._recent[0][0]
        return events, complete
