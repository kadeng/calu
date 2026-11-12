"""A scripted stand-in for Claude Code, for tests and front-end development.

Each user message starts the next scripted turn; when the script is exhausted, the turn answers
"Echo: <message>". A turn is a list of actions, executed in order:

    {"text": "..."}                                   Claude writes text
    {"thinking": "..."}                               Claude's thinking (assistant.thinking)
    {"text": "...", "stream": true}                   ... preceded by word-wise assistant.delta
    {"tool": "Bash", "input": {...}, "result": "..."} a tool call; it passes the ToolGate first
                                                      (PASS runs it: there is no auto mode here)
    {"tool": "Bash", "writes": {"a.txt": "text"}}     ... and the call really writes those files
                                                      (relative to the workspace; "" deletes),
                                                      so that features which watch the file
                                                      system have something to see; the call is
                                                      then reviewed like a command of the real
                                                      runner (`review_write_set`)
    {"tool": "Bash", "run": ["git", "commit", ...]}   ... and the call really runs that program
                                                      in the workspace, after its "writes"
                                                      (no shell; its exit code is ignored)
    {"tool": "Edit", "input": {...}, "fails": true}   ... the approved call fails: it writes
                                                      nothing and reports an error result
    {"sleep": 0.5}                                    pause (lets tests inject messages)
    {"rate_limit": {"type": "five_hour", "utilization": 0.4}}
                                                      Claude Code reports a usage limit
                                                      (`usage.update`)
    {"background_tasks": 2}                           the turn ends with 2 background tasks
                                                      running; like Claude Code (D158), the
                                                      runner then starts the next scripted turn
                                                      by itself, as if they had finished
    {"agent": {"description": "...", "background": false, "actions": [...]}}
                                                      a subagent runs the nested actions

Example script (JSON): {"turns": [[{"text": "Hi"}, {"tool": "Bash", "input": {"command": "ls"},
"result": "a.py"}]]}

Messages sent with priority "next" during a turn are taken in after the current action (a
`UserInput` event); "later" messages start their own turn after the current one.
"""

import itertools
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
from pydantic import BaseModel, Field

from calu.checkers.model import Step
from calu.checkers.result_checker import SubagentResult
from calu.claude.runner import (
    NOT_FROM_USER,
    EventSink,
    Fork,
    Priority,
    SideQuestionFailed,
    ToolGate,
    Verdict,
)
from calu.context.foreign import user_message_source
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    ContextUsage,
    Event,
    RateLimit,
    ToolResult,
    ToolUse,
    TurnResult,
    UsageUpdate,
    UserInput,
)

log = logging.getLogger(__name__)

FOLLOW_UP_DELAY_S = 0.2
"""Pause before the scripted follow-up turn of `{"background_tasks": N}`, standing in for the
time the background tasks take; the session is idle during it, as with Claude Code."""


class FakeScript(BaseModel):
    """The script: a list of turns, each a list of action dicts (see module docstring)."""

    turns: list[list[dict[str, Any]]] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> FakeScript:
        """Read a script from a JSON file.

        Args:
            path: The JSON file, e.g. `{"turns": [[{"text": "Hi"}]]}`.

        Returns:
            The script. Actions are not validated until they run.

        Raises:
            OSError: If the file cannot be read.
            pydantic.ValidationError: If the file is not valid JSON or does not match the model.
        """
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class FakeRunner:
    """`ClaudeRunner` that replays a `FakeScript`. See the module docstring."""

    def __init__(self, script: FakeScript, workspace: Path) -> None:
        """Create a runner that replays `script` from its first turn.

        Args:
            script: The scripted turns.
            workspace: Reported as the working directory (`Step.cwd`) of tool calls.
        """
        self.script = script
        self.workspace = workspace
        self.session_id: str | None = "fake-session"
        self._turns = iter(script.turns)
        self._ids = itertools.count(1)
        self._ready = anyio.Event()
        self._send, self._receive = anyio.create_memory_object_stream[str](100)
        self._next_queue: list[str] = []
        self._cancel: anyio.CancelScope | None = None
        self._interrupted = False
        self.agents: dict[str, AgentInfo] = {}
        self.sink_failures = 0
        """How often the sink raised on an event; each time is logged (`run`)."""
        self.fork: Fork | None = None
        """The fork point `run` was called with, for tests (PLAN.md D36)."""
        self._follow_up = False
        self.side_questions: list[tuple[str, list[tuple[str, str]]]] = []
        """Every side question asked, with the history it came with, for tests."""
        self.side_error: str | None = None
        """If set, `side_question` fails with this message."""
        self.context: ContextUsage | None = ContextUsage(
            tokens=42_000, max_tokens=200_000, percent=21.0, model="fake-model"
        )
        """What `context_usage` returns."""
        self.model: str | None = None
        """The model `set_model` was last called with, for tests."""
        self.model_changes = 0
        """How often `set_model` was called."""
        self._rate_limits: dict[str, RateLimit] = {}

    async def run(
        self,
        gate: ToolGate,
        sink: EventSink,
        resume_id: str | None = None,
        fork: Fork | None = None,
    ) -> None:
        """Run one scripted turn per received message until `stop` is called.

        Each message is first reported as a `UserInput` event. Like the real runner, this one
        keeps the sink contract (`calu.claude.runner.EventSink`): a sink that raises is logged
        at level "error", and the script goes on with the next event.

        Args:
            gate: Asked before every scripted tool call.
            sink: Receives the events of the turns.
            resume_id: If given, becomes `session_id` (no history is restored).
            fork: Where a real runner would branch off (PLAN.md D36). The fake runner has no
                transcript, so it only records it as `self.fork` for tests to assert on.

        Raises:
            ValueError: If the script contains an unknown action.
        """
        self.fork = fork
        if resume_id:
            self.session_id = resume_id
        deliver = sink

        async def guarded(event: Event) -> None:
            """Hand one event to the caller's sink; an exception it raises is logged.

            Args:
                event: The event.
            """
            try:
                await deliver(event)
            except Exception:
                self.sink_failures += 1
                log.exception("the event sink raised on a %s event", event.method)

        sink = guarded
        with anyio.CancelScope() as scope:
            self._cancel = scope
            self._ready.set()
            async for text in self._receive:
                await sink(UserInput(text=text, source=user_message_source(text)))
                await self._turn(text, gate, sink)
                while self._follow_up:
                    # Like Claude Code (D158): the turn has ended and the session is idle; when
                    # the background tasks "finish", the next turn starts without a message.
                    self._follow_up = False
                    await anyio.sleep(FOLLOW_UP_DELAY_S)
                    await self._turn("background tasks finished", gate, sink)

    async def wait_ready(self) -> None:
        """Return once `run` has started."""
        await self._ready.wait()

    async def send(self, text: str, priority: Priority) -> None:
        """Queue a user message.

        A "next" message during a turn is reported after the current action of the main agent;
        otherwise the message starts its own turn after the current one.

        Args:
            text: The message text.
            priority: "next" or "later".
        """
        if priority == "next" and self._in_turn:
            self._next_queue.append(text)
        else:
            await self._send.send(text)

    async def interrupt(self) -> None:
        """Skip the remaining actions of the current turn; its result becomes "interrupted"."""
        self._interrupted = True

    async def stop(self) -> None:
        """Cancel `run`; does nothing if `run` has not started."""
        if self._cancel is not None:
            self._cancel.cancel()

    async def side_question(self, question: str, history: Sequence[tuple[str, str]] = ()) -> str:
        """Answer a side question with a fixed text and record it in `side_questions`.

        Args:
            question: The question.
            history: Earlier side questions with their answers.

        Returns:
            `Side answer to: <question>`.

        Raises:
            SideQuestionFailed: If `side_error` is set, with that message.
        """
        self.side_questions.append((question, list(history)))
        if self.side_error is not None:
            raise SideQuestionFailed(self.side_error)
        return f"Side answer to: {question}"

    async def context_usage(self) -> ContextUsage | None:
        """Return the scripted context usage.

        Returns:
            `self.context`.
        """
        return self.context

    async def set_model(self, model: str | None) -> None:
        """Record a change of model in `model` and `model_changes`.

        Args:
            model: The new model; None for the default.
        """
        self.model = model
        self.model_changes += 1

    # ------------------------------------------------------------------ script execution

    _in_turn = False

    async def _turn(self, text: str, gate: ToolGate, sink: EventSink) -> None:
        """Run the next scripted turn (or an echo turn) and report a `TurnResult`.

        The result text is the last "text" action of the turn.

        Args:
            text: The user message that started the turn.
            gate: Asked before every tool call.
            sink: Receives the events.

        Raises:
            ValueError: If the turn contains an unknown action.
        """
        actions = next(self._turns, None)
        if actions is None:
            actions = [{"text": f"Echo: {text}"}]
        self._in_turn, self._interrupted = True, False
        try:
            await self._actions(actions, None, gate, sink)
        finally:
            self._in_turn = False
        background = sum(int(a.get("background_tasks", 0)) for a in actions)
        await sink(
            TurnResult(
                session_id=self.session_id,
                subtype="interrupted" if self._interrupted else "success",
                result=next((a["text"] for a in reversed(actions) if "text" in a), ""),
                num_turns=1,
                background_tasks=0 if self._interrupted else background,
            )
        )
        # Claude Code continues by itself once its background tasks finished (D158); `run`
        # starts that turn after a pause, so that the session is idle in between.
        self._follow_up = bool(background) and not self._interrupted

    async def _actions(
        self, actions: list[dict[str, Any]], agent: str | None, gate: ToolGate, sink: EventSink
    ) -> None:
        """Run `actions` in order until the turn is interrupted.

        After each main-agent action, queued "next" messages are reported as `UserInput`.

        Args:
            actions: The action dicts.
            agent: Protocol id of the subagent that runs them, or None for the main agent.
            gate: Asked before every tool call.
            sink: Receives the events.

        Raises:
            ValueError: If an action is unknown.
        """
        for action in actions:
            if self._interrupted:
                return
            await self._action(action, agent, gate, sink)
            while self._next_queue and agent is None:
                await sink(UserInput(text=self._next_queue.pop(0)))

    async def _action(
        self, action: dict[str, Any], agent: str | None, gate: ToolGate, sink: EventSink
    ) -> None:
        """Run one action: text, sleep, tool call or subagent (see module docstring).

        A subagent action reports the Agent tool call, `AgentsUpdate` events before and after
        the nested actions, and the Agent tool result.

        Args:
            action: The action dict.
            agent: Protocol id of the subagent that runs it, or None for the main agent.
            gate: Asked before every tool call.
            sink: Receives the events.

        Raises:
            ValueError: If the action has none of the known keys.
        """
        if "thinking" in action:
            await sink(AssistantThinking(agent_id=agent, text=str(action["thinking"])))
        elif "text" in action:
            message_id = f"fake-msg-{next(self._ids)}"
            if action.get("stream"):
                for word in str(action["text"]).split(" "):
                    await sink(
                        AssistantDelta(agent_id=agent, message_id=message_id, text=word + " ")
                    )
                    await anyio.lowlevel.checkpoint()
            await sink(AssistantText(agent_id=agent, text=action["text"], message_id=message_id))
        elif "sleep" in action:
            await anyio.sleep(float(action["sleep"]))
        elif "rate_limit" in action:
            limit = RateLimit.model_validate(action["rate_limit"])
            self._rate_limits[limit.type] = limit
            await sink(UsageUpdate(rate_limits=list(self._rate_limits.values())))
        elif "background_tasks" in action:
            pass  # reported with the turn's result (`_turn`)
        elif "tool" in action:
            await self._tool(action, agent, gate, sink)
        elif "agent" in action:
            spec = action["agent"]
            tool_use_id = f"fake-tool-{next(self._ids)}"
            tool_input = {
                "description": spec.get("description", ""),
                "prompt": spec.get("prompt", ""),
                "subagent_type": spec.get("subagent_type", "general-purpose"),
                "run_in_background": bool(spec.get("background", False)),
            }
            await sink(
                ToolUse(agent_id=agent, tool_use_id=tool_use_id, name="Agent", input=tool_input)
            )
            info = AgentInfo(
                agent_id=tool_use_id,
                sdk_agent_id=f"fake-agent-{tool_use_id}",
                description=tool_input["description"],
                subagent_type=tool_input["subagent_type"],
                background=tool_input["run_in_background"],
            )
            self.agents[tool_use_id] = info
            await sink(AgentsUpdate(agents=list(self.agents.values())))
            await self._actions(list(spec.get("actions", [])), tool_use_id, gate, sink)
            info.status = "completed"
            await sink(AgentsUpdate(agents=list(self.agents.values())))
            result_text = str(spec.get("result", "Subagent finished."))
            note = await gate.review_subagent_result(
                SubagentResult(
                    agent_id=tool_use_id,
                    description=tool_input["description"],
                    prompt=tool_input["prompt"],
                    result=result_text,
                    background=tool_input["run_in_background"],
                )
            )
            await sink(ToolResult(agent_id=agent, tool_use_id=tool_use_id, content=result_text))
            if note:
                # Stands for the injected priority-"next" note: calu's text, not the user's.
                await sink(UserInput(text=note, source=NOT_FROM_USER))
        else:
            raise ValueError(f"unknown fake action: {action!r}")

    async def _tool(
        self, action: dict[str, Any], agent: str | None, gate: ToolGate, sink: EventSink
    ) -> None:
        """Run one scripted tool call through the gate and report `ToolUse` and `ToolResult`.

        An allowed call reports the scripted "result"; a denied call reports the denial
        message as an error result, and an allowed call with "fails" reports its "result" as an
        error result without writing anything.

        Side effect: an action with "writes" writes (or deletes) those files in the workspace,
        and an action with "run" runs that program in the workspace, after the gate approved
        the call, which is how a scripted command stands in for one that
        edits files (`sed -i`, a formatter, a script).

        Args:
            action: The action dict with "tool", optional "input", "writes", "run", "fails"
                and "result".
            agent: Protocol id of the calling subagent, or None for the main agent.
            gate: Decides about the call; `ask` is awaited for an ASK verdict.
            sink: Receives the events.
        """
        tool_use_id = f"fake-tool-{next(self._ids)}"
        name, tool_input = str(action["tool"]), dict(action.get("input", {}))
        await sink(ToolUse(agent_id=agent, tool_use_id=tool_use_id, name=name, input=tool_input))
        step = Step(
            tool_name=name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            agent_id=agent,
            cwd=str(self.workspace),
        )
        gate_result = await gate.pre_tool_use(step)
        if gate_result.verdict is Verdict.ASK:
            answer = await gate.ask(step)
            allowed, message = answer.allow, answer.message or "The user denied this step."
        else:
            # The fake runner has no auto mode of its own, so PASS runs the step (PLAN.md D21).
            allowed = gate_result.verdict in (Verdict.PASS, Verdict.ALLOW)
            message = gate_result.reason
        note = ""
        if allowed and action.get("fails"):
            content, is_error = str(action.get("result", "The tool call failed.")), True
        elif allowed:
            _write_files(self.workspace, action.get("writes") or {})
            if action.get("run"):
                await anyio.run_process(
                    [str(word) for word in action["run"]], cwd=self.workspace, check=False
                )
            content, is_error = str(action.get("result", "")), False
            await gate.tool_succeeded(step)  # as the PostToolUse hook does for edits
            note = await gate.review_write_set(step)  # as the PostToolUse hook does for Bash
        else:
            content, is_error = message, True
        await sink(
            ToolResult(agent_id=agent, tool_use_id=tool_use_id, is_error=is_error, content=content)
        )
        if note:
            # Stands for the injected priority-"next" note: calu's text, not the user's.
            await sink(UserInput(text=note, source=NOT_FROM_USER))


def _write_files(workspace: Path, writes: dict[str, str]) -> None:
    """Write or delete the files of a scripted call's "writes" mapping.

    Args:
        workspace: The workspace root; every path is relative to it.
        writes: Path -> content; an empty content deletes the file.
    """
    for relative, content in writes.items():
        path = workspace / relative
        if content == "":
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
