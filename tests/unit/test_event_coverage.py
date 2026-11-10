"""Every event reaches a person, or is named here as one that deliberately does not.

An event nobody shows is worse than no event: the session records it, a client receives it, and
the human watching sees nothing. `auto_escalate.changed` was exactly that for a day -- the
session announced a change of what waits for a human and the terminal said nothing about it.

These tests are structural: they read the dispatch of the terminal renderer and of the ACP
adapter and insist that every event in `EVENTS` is either handled there or listed below. Adding
an event therefore forces a decision instead of allowing silence.
"""

import inspect

from calu import run
from calu import session as session_module
from calu.acp import adapter
from calu.protocol import server as server_module
from calu.protocol.messages import EVENTS

SILENT_IN_THE_TERMINAL = {
    "AssistantDelta",  # the token stream; the completed block follows
    "ToolFiles",  # the files a step touched, as links
    "EditDiff",  # the diff of an edit
    "ChatQueue",  # the queue of waiting messages
    "AgentsUpdate",  # the subagent list
}
"""Events a line-based terminal deliberately does not print (`calu.run.format_event`)."""

NOT_CARRIED_BY_ACP = {
    "tool.files",  # ACP carries a tool call's locations with the call itself
    "chat.queue",  # ACP has no shape for a queue of waiting messages
    "agents.update",  # ACP has no subagents; the web interface shows them (PLAN.md §6.1)
}
"""Events the ACP adapter deliberately drops, because the protocol has nowhere to put them."""


def test_every_event_is_printed_or_deliberately_silent() -> None:
    """The terminal of `calu run` and `calu attach` share one renderer."""
    source = inspect.getsource(run.format_event)
    unhandled = {
        event.__name__
        for event in EVENTS
        if f"{event.__name__}(" not in source and event.__name__ not in SILENT_IN_THE_TERMINAL
    }
    assert unhandled == set(), (
        f"{', '.join(sorted(unhandled))} would reach a client and show nothing; "
        "render it in `format_event` or name it in SILENT_IN_THE_TERMINAL"
    )
    # The list itself must not go stale either.
    names = {event.__name__ for event in EVENTS}
    assert SILENT_IN_THE_TERMINAL <= names, (
        f"SILENT_IN_THE_TERMINAL names events that no longer exist: "
        f"{', '.join(sorted(SILENT_IN_THE_TERMINAL - names))}"
    )


def test_every_event_reaches_an_editor_or_is_named_as_one_that_cannot() -> None:
    """The ACP adapter translates the stream for an editor (PLAN.md D24)."""
    source = inspect.getsource(adapter.Adapter.on_event)
    unhandled = {
        event.method
        for event in EVENTS
        if f'"{event.method}"' not in source and event.method not in NOT_CARRIED_BY_ACP
    }
    assert unhandled == set(), (
        f"{', '.join(sorted(unhandled))} is not translated for an editor; "
        "handle it in `Adapter.on_event` or name it in NOT_CARRIED_BY_ACP"
    )
    methods = {event.method for event in EVENTS}
    assert NOT_CARRIED_BY_ACP <= methods, (
        f"NOT_CARRIED_BY_ACP names events that no longer exist: "
        f"{', '.join(sorted(NOT_CARRIED_BY_ACP - methods))}"
    )


def test_a_mode_change_is_printed_with_what_waits() -> None:
    """The line has to name the categories, not only that something changed."""
    from calu.config import AutoEscalate
    from calu.protocol.messages import AutoEscalateChanged

    line = run.format_event(AutoEscalateChanged(settings=AutoEscalate(edits=True)))
    assert line is not None
    assert "waits for a human" in line and "edits" in line
    # All off is worth a line too: it says the checkers decide again.
    off = run.format_event(AutoEscalateChanged(settings=AutoEscalate()))
    assert off is not None and "checkers" in off


def test_every_advertised_request_has_a_handler() -> None:
    """The JSON schema advertises these methods, so each one has to do something.

    A request model with no handler is worse than a missing one: `calu protocol` exports it, a
    client written against the schema calls it, and the session answers "unhandled method".
    """
    from calu.protocol.messages import REQUESTS

    dispatch = ""
    for member in vars(server_module).values():
        if inspect.isclass(member):
            for name in ("_dispatch", "handle", "_handle"):
                found = getattr(member, name, None)
                if found is not None and callable(found):
                    dispatch += inspect.getsource(found)
    assert dispatch, "no dispatch found in calu.protocol.server"
    unhandled = [method for method in REQUESTS if f'"{method}"' not in dispatch]
    assert unhandled == [], f"advertised but not implemented: {', '.join(unhandled)}"


def test_every_route_is_handled_by_the_session() -> None:
    """A route the session does not act on would send a step nowhere."""
    from calu.checkers.policy import Route

    source = inspect.getsource(session_module)
    unused = [route.value for route in Route if f"Route.{route.name}" not in source]
    assert unused == [], f"routes the session never acts on: {', '.join(unused)}"
