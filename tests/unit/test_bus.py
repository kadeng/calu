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
