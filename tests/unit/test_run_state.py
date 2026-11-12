"""The per-run state of a session: what a start resets and what it carries over."""

from dataclasses import fields
from typing import Any, cast

import anyio
import pytest

from calu.run_state import PerCall, RunState

pytestmark = pytest.mark.anyio

CARRIED_OVER = {
    "escalations",
    "escalation_closed",
    "asking",
    "opening",
    "queue",
    "clock_alarm",
    "store_failures",
    "stopping",
}
"""The fields that `RunState.following` keeps; every other field starts from its default."""


async def test_no_run_is_running_before_the_first_start():
    assert RunState.before_first_run().run_done.is_set()


async def test_a_new_run_starts_from_the_defaults_except_what_is_carried_over():
    async with anyio.create_task_group() as tg:
        old = RunState.before_first_run()
        old.run_error = "boom"
        old.session_id = "s1"
        old.busy = True
        old.checking = 2
        next(old.step_seq)
        old.call("t1").running = True
        old.claude_outputs[None] = []
        old.asking["t1"] = 1.0
        old.opening = 1
        old.clock_alarm = "the clock jumped"
        old.store_failures = 3
        old.stopping = True

        new = old.following(tg)

        assert new.tg is tg and not new.run_done.is_set()
        fresh = RunState()
        for item in fields(RunState):
            if item.name in CARRIED_OVER:
                assert getattr(new, item.name) is getattr(old, item.name), item.name
            elif item.name not in ("tg", "run_done", "step_seq"):
                assert getattr(new, item.name) == getattr(fresh, item.name), item.name
        assert next(new.step_seq) == 1
        assert old.calls and old.session_id == "s1"  # the previous state is not changed


def test_a_record_is_removed_with_its_last_field():
    state = RunState()
    assert state.peek("t1") == PerCall() and not state.calls  # reading creates nothing
    state.call("t1").running = True
    state.call("t1").step_id = 7
    assert state.any_running()
    state.peek("t1").running = False
    state.release("t1")
    assert "t1" in state.calls and not state.any_running()
    state.peek("t1").step_id = None
    state.release("t1")
    state.release("unknown")
    assert not state.calls


def test_every_field_of_a_call_record_has_an_empty_default():
    values = cast(dict[str, Any], vars(PerCall()))
    assert not any(values.values()) and PerCall().is_empty()
