"""What the scripted runner can play beyond plain turns: timing, interrupts, subagents beside
the turn, calls at the same time, transcript uuids (see `calu.claude.fake_runner`)."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.claude import FakeRunner, FakeScript
from calu.claude.fake_runner import INTERRUPTED_TOOL_RESULT, LAUNCH_ACKNOWLEDGEMENT
from calu.protocol.messages import (
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    Event,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from tests.unit.test_sdk_runner import AllowAll

pytestmark = pytest.mark.anyio


class Played:
    """A running fake runner and the events it reported."""

    def __init__(self, runner: FakeRunner) -> None:
        self.runner = runner
        self.seen: list[Event] = []

    async def sink(self, event: Event) -> None:
        self.seen.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.seen if isinstance(event, kind)]

    async def until(self, kind: type, count: int = 1) -> None:
        with anyio.fail_after(5):
            while len(self.of(kind)) < count:
                await anyio.sleep(0.005)


async def play(tmp_path: Path, turns: list[list[dict[str, Any]]], tg, **attributes) -> Played:
    """Start a runner on `turns` in the task group and send it the first message."""
    runner = FakeRunner(FakeScript(turns=turns), tmp_path)
    for name, value in attributes.items():
        setattr(runner, name, value)
    played = Played(runner)
    tg.start_soon(runner.run, AllowAll(), played.sink)
    await runner.wait_ready()
    await runner.send("go", "later")
    return played


async def test_an_interrupt_ends_a_slow_text_after_the_words_said(tmp_path: Path):
    text = "one two three four five six seven eight"
    async with anyio.create_task_group() as tg:
        played = await play(
            tmp_path, [[{"text": text, "stream": True, "delay": 0.05}, {"text": "never"}]], tg
        )
        await played.until(AssistantDelta, 2)
        await played.runner.interrupt()
        await played.until(TurnResult)
        await played.runner.stop()
    (said,) = played.of(AssistantText)
    assert said.text and text.startswith(said.text) and said.text != text
    assert played.of(TurnResult)[0].subtype == "interrupted"


async def test_an_interrupt_ends_a_long_tool_call_with_an_error_and_writes_nothing(tmp_path: Path):
    call = {"tool": "Bash", "input": {"command": "make"}, "duration": 30, "writes": {"a": "x"}}
    async with anyio.create_task_group() as tg:
        played = await play(tmp_path, [[call, {"text": "never"}]], tg)
        await played.until(ToolUse)
        await anyio.sleep(0.05)
        await played.runner.interrupt()
        await played.until(TurnResult)
        await played.runner.stop()
    (result,) = played.of(ToolResult)
    assert (result.is_error, result.content) == (True, INTERRUPTED_TOOL_RESULT)
    assert not (tmp_path / "a").exists() and played.of(AssistantText) == []


async def test_a_tool_call_with_a_duration_that_passes_runs_as_scripted(tmp_path: Path):
    call = {"tool": "Bash", "duration": 0.01, "writes": {"a": "x"}, "result": "built"}
    async with anyio.create_task_group() as tg:
        played = await play(tmp_path, [[call]], tg)
        await played.until(TurnResult)
        await played.runner.stop()
    assert played.of(ToolResult)[0].content == "built" and (tmp_path / "a").read_text() == "x"


async def test_a_failing_subagent_ends_failed_with_an_error_result(tmp_path: Path):
    agent = {"description": "broken", "fails": True, "result": "It crashed.", "actions": []}
    async with anyio.create_task_group() as tg:
        played = await play(tmp_path, [[{"agent": agent}]], tg)
        await played.until(TurnResult)
        await played.runner.stop()
    assert played.of(AgentsUpdate)[-1].agents[0].status == "failed"
    assert [(r.is_error, r.content) for r in played.of(ToolResult)] == [(True, "It crashed.")]


async def test_a_detached_subagent_outlives_its_turn_and_starts_the_next_one(tmp_path: Path):
    agent = {
        "description": "watcher",
        "background": True,
        "detach": True,
        "actions": [{"sleep": 0.1}, {"text": "watched"}],
    }
    turns = [[{"agent": agent}, {"text": "launched"}], [{"text": "it finished"}]]
    async with anyio.create_task_group() as tg:
        played = await play(tmp_path, turns, tg)
        await played.until(TurnResult)
        first = played.of(TurnResult)[0]
        assert first.background_tasks == 1
        assert played.of(ToolResult)[0].content == LAUNCH_ACKNOWLEDGEMENT
        assert played.of(AgentsUpdate)[-1].agents[0].status == "running"
        await played.until(TurnResult, 2)
        await played.runner.stop()
    assert played.of(AgentsUpdate)[-1].agents[0].status == "completed"
    assert [t.text for t in played.of(AssistantText)] == ["launched", "watched", "it finished"]
    assert played.of(TurnResult)[1].background_tasks == 0
    # Nobody typed the follow-up turn: the only user message is the first one.
    assert [u.text for u in played.of(UserInput)] == ["go"]


async def test_parallel_branches_run_at_the_same_time(tmp_path: Path):
    slow = [{"sleep": 0.1}, {"text": "slow"}]
    fast = [{"text": "fast"}]
    async with anyio.create_task_group() as tg:
        played = await play(tmp_path, [[{"parallel": [slow, fast]}, {"text": "after"}]], tg)
        await played.until(TurnResult)
        await played.runner.stop()
    assert [t.text for t in played.of(AssistantText)] == ["fast", "slow", "after"]


async def test_transcript_uuids_are_given_only_when_asked_for(tmp_path: Path):
    async with anyio.create_task_group() as tg:
        plain = await play(tmp_path, [[{"text": "hi"}]], tg)
        marked = await play(tmp_path, [[{"text": "hi"}]], tg, transcript_uuids=True)
        await plain.until(TurnResult)
        await marked.until(TurnResult)
        await plain.runner.stop()
        await marked.runner.stop()
    assert [e.uuid for e in plain.of(UserInput) + plain.of(AssistantText)] == ["", ""]
    uuids = [e.uuid for e in marked.of(UserInput) + marked.of(AssistantText)]
    assert all(uuids) and len(set(uuids)) == 2


async def test_an_interrupt_ends_the_wait_for_a_humans_decision(tmp_path: Path):
    from calu.protocol.messages import ChatSendParams, EscalationRequest, EscalationResolved
    from tests.unit.helpers import make_session

    call = {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "writes": {"a": "x"}}
    session, rec, _store = make_session(tmp_path, [[call, {"text": "never"}]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(EscalationRequest)
        await session.interrupt()
        await rec.wait_for(TurnResult)
        assert [e.decision for e in rec.of(EscalationResolved)] == ["cancelled"]
        (result,) = rec.of(ToolResult)
        assert (result.is_error, result.content) == (True, INTERRUPTED_TOOL_RESULT)
        assert rec.of(TurnResult)[0].subtype == "interrupted" and not (tmp_path / "a").exists()
        await session.stop()
        tg.cancel_scope.cancel()
