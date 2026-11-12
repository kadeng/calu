"""The event bus: sequence numbers, fan-out to several clients, replay and overflow."""

import anyio
import pytest

from calu import bus as bus_module
from calu.bus import Bus
from calu.protocol.messages import Log, SessionState

pytestmark = pytest.mark.anyio


def log(message: str) -> Log:
    """Return a `log` event with `message`."""
    return Log(message=message)


async def test_events_are_numbered_from_one_and_kept_for_replay():
    bus = Bus()
    assert bus.last_seq == 0
    await bus.publish(log("one"))
    state = SessionState(state="idle")
    await bus.publish(state)
    assert state.seq == 2 and bus.last_seq == 2
    events, complete = bus.replay(0)
    assert complete and [e.method for e in events] == ["log", "session.state"]
    assert events[0].params["seq"] == 1
    events, complete = bus.replay(1)
    assert complete and [e.params["state"] for e in events] == ["idle"]
    assert bus.replay(2) == ([], True)


async def test_every_client_sees_every_event_in_order():
    bus = Bus()
    first, second = bus.subscribe(), bus.subscribe()
    assert bus.clients == 2
    await bus.publish(log("a"))
    await bus.publish(log("b"))
    first.close()
    second.close()
    assert [e.seq for e in [m async for m in first.events()]] == [1, 2]
    assert [str(getattr(e, "message", "")) for e in [m async for m in second.events()]] == [
        "a",
        "b",
    ]
    assert bus.clients == 0


async def test_a_subscription_waits_for_the_next_event():
    bus = Bus()
    client = bus.subscribe()
    received: list[int] = []

    async def read() -> None:
        """Collect the sequence numbers the client receives."""
        async for event in client.events():
            received.append(event.seq)

    async with anyio.create_task_group() as tg:
        tg.start_soon(read)
        await anyio.sleep(0.05)
        assert received == []
        await bus.publish(log("now"))
        with anyio.fail_after(2):
            while not received:
                await anyio.sleep(0.01)
        client.close()
    assert received == [1]


async def test_a_client_that_falls_behind_is_cut_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bus_module, "CLIENT_QUEUE", 3)
    bus = Bus()
    client = bus.subscribe()
    for i in range(5):
        await bus.publish(log(str(i)))
    assert client.overflowed
    assert [e async for e in client.events()] == []
    # It receives nothing any more, so it must not count as an attached client either:
    # `calu run` keeps an otherwise finished run alive for as long as one is attached.
    assert bus.clients == 0
    assert bus.no_clients.is_set()
    assert bus.last_seq == 5  # publishing never fails because of a slow client


async def test_replay_reports_a_gap_in_the_buffer():
    bus = Bus(capacity=3)
    for i in range(5):
        await bus.publish(log(str(i)))
    events, complete = bus.replay(2)
    assert complete and [e.params["seq"] for e in events] == [3, 4, 5]
    events, complete = bus.replay(0)
    assert not complete and [e.params["seq"] for e in events] == [3, 4, 5]
    assert not bus.replay(1)[1]


async def test_the_bus_reports_when_the_last_client_detaches():
    bus = Bus()
    assert bus.no_clients.is_set()
    first, second = bus.subscribe(), bus.subscribe()
    assert not bus.no_clients.is_set()
    first.close()
    assert not bus.no_clients.is_set()  # one client is still attached
    second.close()
    assert bus.no_clients.is_set()


async def test_a_waiter_is_woken_even_when_a_client_joined_while_it_waited():
    """`calu run` awaits the event object it captured, which a later subscribe must not orphan.

    It ends a finished run when the last client detaches (`run._wait_for_clients`), and it awaits
    `bus.no_clients` -- the object as it was when the wait began. A second client joining after
    that must not leave the waiter behind, or the session process never exits.
    """
    bus = Bus()
    first = bus.subscribe()
    waiting = bus.no_clients  # captured, as the caller does
    woken = anyio.Event()

    async def wait() -> None:
        """Wait for the last client to leave, then report it."""
        await waiting.wait()
        woken.set()

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tg:
            tg.start_soon(wait)
            await anyio.sleep(0.05)
            second = bus.subscribe()  # joins while the waiter waits
            first.close()
            assert not woken.is_set(), "one client is still attached"
            second.close()
            with anyio.fail_after(1):
                await woken.wait()


async def test_a_sequence_number_beyond_the_stream_is_not_called_complete():
    """A client of an earlier process asks with a number this one has never reached.

    Sequence numbers count from 1 per calu process, so a client that reached 500 before a
    restart asks for everything after 500 and the new process has nothing after it. Answering
    "complete, no events" would leave the client believing it is up to date while it has seen
    none of the new session.
    """
    bus = Bus()
    for i in range(3):
        await bus.publish(log(str(i)))

    # Up to date: nothing to send, and nothing missing.
    assert bus.replay(3) == ([], True)
    # Beyond the stream: the position cannot come from this process.
    events, complete = bus.replay(500)
    assert events == []
    assert not complete, "the client has seen nothing of this session and must resubscribe"
