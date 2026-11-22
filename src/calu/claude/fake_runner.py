"""A scripted stand-in for Claude Code, for tests and front-end development.

Each user message starts the next scripted turn; when the script is exhausted, the turn answers
"Echo: <message>". A turn is a list of actions, executed in order:

    {"text": "..."}                                   Claude writes text
    {"thinking": "..."}                               Claude's thinking (assistant.thinking)
    {"thinking": "...", "message": "m1"}              ... with the message id of the name "m1":
                                                      a second thinking of the same name is a
                                                      rewrite of the first, as Claude Code
                                                      sends a summary it revised
    {"thinking": "...", "stream": true}               ... preceded by word-wise assistant.delta
                                                      events with `thinking` true
    {"text": "...", "stream": true}                   ... preceded by word-wise assistant.delta
    {"text": "...", "stream": true, "delay": 0.05}    ... with a pause after each word; an
                                                      interrupt ends the text after the words
                                                      streamed so far
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
    {"tool": "Bash", "cwd": "sub/dir", ...}           ... the call's working directory
                                                      (`Step.cwd`): relative to the workspace,
                                                      or absolute; "writes" and "run" then
                                                      happen there (default: the workspace)
    {"tool": "Edit", "input": {...}, "fails": true}   ... the approved call fails: it writes
                                                      nothing and reports an error result
    {"tool": "Bash", "input": {...}, "duration": 2}   ... the approved call takes that many
                                                      seconds; an interrupt during them ends it
                                                      with an error result and writes nothing
                                                      (a call that waits for a human's decision
                                                      at an interrupt stops waiting, with the
                                                      same error result)
    {"sleep": 0.5}                                    pause (lets tests inject messages); an
                                                      interrupt ends it early
    {"rate_limit": {"type": "five_hour", "utilization": 0.4}}
                                                      Claude Code reports a usage limit
                                                      (`usage.update`)
    {"background_tasks": 2}                           the turn ends with 2 background tasks
                                                      running; like Claude Code (D158), the
                                                      runner then starts the next scripted turn
                                                      by itself, as if they had finished
    {"agent": {"description": "...", "background": false, "actions": [...]}}
                                                      a subagent runs the nested actions
    {"agent": {..., "fails": true}}                   ... and ends with the status "failed" and
                                                      an error result
    {"agent": {..., "result": "", "actions": [..., {"tool": "SubagentHandback", "input":
        {"message": "..."}}]}}                        ... a subagent that hands its report back
                                                      as Claude Code's background subagents do:
                                                      the gate reviews the report, its
                                                      `main_note` goes to the main agent, and
                                                      the final result is empty
    {"agent": {..., "background": true, "detach": true}}
                                                      ... as Claude Code runs a background
                                                      subagent: the Agent call returns at once
                                                      with a launch acknowledgement, the nested
                                                      actions run beside the turn, a turn that
                                                      ends before them reports them as running
                                                      background tasks, and when the last one
                                                      finishes after its turn the next scripted
                                                      turn starts by itself
    {"permission_mode": "plan"}                       Claude Code reports another permission
                                                      mode by itself (as after an EnterPlanMode
                                                      call): `permission_mode` changes and a
                                                      `permission_mode.changed` event is sent
    {"parallel": [[...], [...]]}                      the action lists run at the same time (as
                                                      several tool calls or subagents of one
                                                      assistant message do)

Example script (JSON): {"turns": [[{"text": "Hi"}, {"tool": "Bash", "input": {"command": "ls"},
"result": "a.py"}]]}

`FakeScript.permission_mode` is the mode the runner starts in (default "auto", reported as a
`permission_mode.changed` event when `run` starts, as the real runner reports the init message);
`set_permission_mode` changes it and reports it the same way, also during a turn, and every
scripted tool call records the mode it ran in (`tool_modes`). The hook's answer depends on
the mode at the moment it goes out, as with the SDK runner (`hook_answers`; D250): in auto mode a
PASS gets no decision and the scripted classifier approves the step; in another mode a PASS
runs on the checkers' word. `permission_mode_error` makes the next `set_permission_mode` fail
as a refused mode would.

Messages sent with priority "next" during a turn are taken in after the current action (a
`UserInput` event); "later" messages start their own turn after the current one.
"""

import itertools
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
from anyio.abc import TaskGroup
from pydantic import BaseModel, Field

from calu.checkers.model import Step, SubagentResult
from calu.claude.runner import (
    NOT_FROM_USER,
    EventSink,
    Fork,
    PermissionModeChangeFailed,
    Priority,
    SideQuestionFailed,
    ToolGate,
    UsageReport,
    hook_decision,
)
from calu.context.foreign import user_message_source
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    ChatImage,
    ContextUsage,
    Event,
    PermissionModeChanged,
    RateLimit,
    ToolResult,
    ToolUse,
    TurnResult,
    UsageUpdate,
    UserInput,
)

log = logging.getLogger(__name__)

INTERRUPTED_TOOL_RESULT = "[Request interrupted by user for tool use]"
"""The error result of a tool call that an interrupt ended, as Claude Code words it."""

LAUNCH_ACKNOWLEDGEMENT = "Async agent launched successfully. Its result arrives when it finishes."
"""The result of the Agent call that starts a detached background subagent."""

FOLLOW_UP = "background tasks finished"
"""The text the follow-up turn of finished background tasks is started with. No `UserInput`
is reported for it: nobody typed it."""

FOLLOW_UP_DELAY_S = 0.2
"""Pause before the scripted follow-up turn of `{"background_tasks": N}`, standing in for the
time the background tasks take; the session is idle during it, as with Claude Code."""


class FakeScript(BaseModel):
    """The script: a list of turns, each a list of action dicts (see module docstring)."""

    turns: list[list[dict[str, Any]]] = Field(default_factory=list)
    permission_mode: str = "auto"
    """The permission mode the runner starts in, as calu's default start gives it."""

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
        # Tool and message ids are unique across runners, as Claude Code's are: ids that
        # started again at `fake-tool-1` in every session made a fork's or a second session's
        # call look like an earlier one to a client that matches calls by their id.
        self._id_prefix = uuid.uuid4().hex[:8]
        self._ready = anyio.Event()
        # Each queued message travels with its own images, so two sends of the same text keep
        # their images apart and nothing stays behind once the message is reported.
        # A text of None is the runner's own signal that a detached subagent finished.
        self._send, self._receive = anyio.create_memory_object_stream[
            tuple[str | None, list[ChatImage]]
        ](100)
        self._next_queue: list[tuple[str, list[ChatImage]]] = []
        self._cancel: anyio.CancelScope | None = None
        self._interrupted = False
        self._interrupt_seen = anyio.Event()
        self.agents: dict[str, AgentInfo] = {}
        self.sink_failures = 0
        """How often the sink raised on an event; each time is logged (`run`)."""
        self.fork: Fork | None = None
        """The fork point `run` was called with, for tests (agent/records/PLAN.md D36)."""
        self._follow_up = False
        self.side_questions: list[tuple[str, list[tuple[str, str]]]] = []
        """Every side question asked, with the history it came with, for tests."""
        self.side_error: str | None = None
        """If set, `side_question` fails with this message."""
        self.context: ContextUsage | None = ContextUsage(
            tokens=42_000, max_tokens=200_000, percent=21.0, model="fake-model"
        )
        """What `context_usage` returns."""
        self.usage_text = "Total cost:            $0.0000\nUsage:                 0 input, 0 output"
        """The `/usage` text `usage_report` returns ("" makes it report a problem)."""
        self.account_limits: list[RateLimit] = []
        """The plan's limits `usage_report` returns."""
        self.account_problem = ""
        """What `usage_report` says about `get_usage` ("" for an answer of the known shape)."""
        self.usage_reports = 0
        """How often `usage_report` was called."""
        self.model: str | None = None
        """The model `set_model` was last called with, for tests."""
        self.model_changes = 0
        """How often `set_model` was called."""
        self._rate_limits: dict[str, RateLimit] = {}
        self.transcript_uuids = False
        """If True, every user message and every text of the main agent carries a transcript
        uuid, as with the real runner, so that the session records fork points (D36)."""
        self._tasks: TaskGroup | None = None
        self._asks: set[anyio.CancelScope] = set()
        """The scopes of the tool calls that wait for a human; an interrupt cancels them."""
        self._detached = 0
        """How many detached background subagents are running."""
        self.permission_mode: str | None = script.permission_mode
        """The main agent's permission mode (`ClaudeRunner.permission_mode`)."""
        self.mode_changes: list[str] = []
        """Every mode `set_permission_mode` was called with, in order, for tests."""
        self.permission_mode_error: str | None = None
        """If set, the next `set_permission_mode` fails with this message and changes nothing."""
        self.tool_modes: list[tuple[str, str | None]] = []
        """(tool name, permission mode) of every scripted tool call, at its check."""
        self.hook_answers: list[tuple[str, str | None]] = []
        """(tool name, `permissionDecision`) the hook gave each scripted tool call, None for
        no decision (`calu.claude.runner.hook_decision`), for tests."""
        self._sink: EventSink | None = None

    async def run(
        self,
        gate: ToolGate,
        sink: EventSink,
        resume_id: str | None = None,
        fork: Fork | None = None,
        session_id: str | None = None,
    ) -> None:
        """Run one scripted turn per received message until `stop` is called.

        Each message is first reported as a `UserInput` event. Like the real runner, this one
        keeps the sink contract (`calu.claude.runner.EventSink`): a sink that raises is logged
        at level "error", and the script goes on with the next event.

        Args:
            gate: Asked before every scripted tool call.
            sink: Receives the events of the turns.
            resume_id: If given, becomes `session_id` (no history is restored).
            fork: Where a real runner would branch off (agent/records/PLAN.md D36). The fake runner
                has no transcript, so it only records it as `self.fork` for tests to assert on.
            session_id: The id a new or forked conversation gets, as with the real runner: it
                becomes `self.session_id` unless this is a plain resume.

        Raises:
            ValueError: If the script contains an unknown action.
        """
        self.fork = fork
        if resume_id:
            self.session_id = resume_id
        if session_id and (resume_id is None or fork is not None):
            self.session_id = session_id
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
        self._sink = sink
        try:
            async with anyio.create_task_group() as tasks:
                self._cancel = tasks.cancel_scope
                self._tasks = tasks
                if self.permission_mode is not None:
                    await sink(PermissionModeChanged(mode=self.permission_mode))
                self._ready.set()
                async for text, images in self._receive:
                    if text is not None:
                        await sink(
                            UserInput(
                                text=text,
                                source=user_message_source(text),
                                images=images,
                                uuid=self._uuid(),
                            )
                        )
                    elif self._detached:
                        continue  # another detached subagent still runs: its end starts the turn
                    await self._turn(text if text is not None else FOLLOW_UP, gate, sink)
                    while self._follow_up:
                        # Like Claude Code (D158): the turn has ended and the session is idle; when
                        # the background tasks "finish", the next turn starts without a message.
                        self._follow_up = False
                        await anyio.sleep(FOLLOW_UP_DELAY_S)
                        await self._turn(FOLLOW_UP, gate, sink)
        finally:
            self._sink = None

    async def wait_ready(self) -> None:
        """Return once `run` has started."""
        await self._ready.wait()

    async def send(self, text: str, priority: Priority, images: Sequence[ChatImage] = ()) -> None:
        """Queue a user message.

        A "next" message during a turn is reported after the current action of the main agent;
        otherwise the message starts its own turn after the current one.

        Args:
            text: The message text.
            priority: "next" or "later".
            images: Images the user attached; reported with the message's `UserInput`.
        """
        if priority == "next" and self._in_turn:
            self._next_queue.append((text, list(images)))
        else:
            await self._send.send((text, list(images)))

    async def interrupt(self) -> None:
        """Skip the remaining actions of the current turn; its result becomes "interrupted".

        A tool call that waits for a human's decision stops waiting, and a slow text or tool
        call ends early (see the module docstring).
        """
        self._interrupted = True
        self._interrupt_seen.set()
        for waiting in list(self._asks):
            waiting.cancel()

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

    async def usage_report(self) -> UsageReport:
        """Return the scripted `/usage` text and account limits; counts the call.

        Returns:
            `usage_text` and `account_limits`; with an empty `usage_text` the problem
            "Claude Code returned no /usage text"; `account_problem` as the test set it.
        """
        self.usage_reports += 1
        return UsageReport(
            text=self.usage_text,
            problem="" if self.usage_text else "Claude Code returned no /usage text",
            account_limits=tuple(self.account_limits),
            account_problem=self.account_problem,
        )

    async def set_model(self, model: str | None) -> None:
        """Record a change of model in `model` and `model_changes`.

        Args:
            model: The new model; None for the default.
        """
        self.model = model
        self.model_changes += 1

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the scripted permission mode and report it, as Claude Code's status message.

        Side effects: appends `mode` to `mode_changes`, sets `permission_mode` and sends a
        `PermissionModeChanged` event through the sink of `run` (after this call has
        returned its control answer, as Claude Code does; a mode equal to the current one
        sends it too). The next scripted tool call runs in the new mode.

        Args:
            mode: The new mode.

        Raises:
            PermissionModeChangeFailed: If `run` has not started, or `permission_mode_error`
                is set (it is cleared then); nothing changes.
        """
        if self._sink is None:
            raise PermissionModeChangeFailed("no Claude session is running")
        if self.permission_mode_error is not None:
            message, self.permission_mode_error = self.permission_mode_error, None
            raise PermissionModeChangeFailed(message)
        self.mode_changes.append(mode)
        self.permission_mode = mode
        sink = self._sink
        assert self._tasks is not None

        async def report() -> None:
            """Send the status report after the caller got its answer."""
            await anyio.lowlevel.checkpoint()
            await sink(PermissionModeChanged(mode=mode))

        self._tasks.start_soon(report)

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
        self._interrupt_seen = anyio.Event()
        try:
            await self._actions(actions, None, gate, sink)
        finally:
            self._in_turn = False
        scripted = sum(int(a.get("background_tasks", 0)) for a in actions)
        background = scripted + self._detached
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
        self._follow_up = bool(scripted) and not self._interrupted

    def _uuid(self) -> str:
        """Make the transcript uuid of one message.

        Returns:
            A new uuid with `transcript_uuids`, else "".
        """
        return str(uuid.uuid4()) if self.transcript_uuids else ""

    async def _pause(self, seconds: float) -> None:
        """Wait, but no longer than until the turn is interrupted.

        Args:
            seconds: The longest wait.
        """
        if self._interrupted:
            return
        with anyio.move_on_after(seconds):
            await self._interrupt_seen.wait()

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
                queued, images = self._next_queue.pop(0)
                await sink(UserInput(text=queued, images=images))

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
            await self._thinking(action, agent, sink)
        elif "text" in action:
            message_id = f"fake-msg-{self._id_prefix}-{next(self._ids)}"
            text = str(action["text"])
            if action.get("stream"):
                delay = float(action.get("delay", 0))
                said: list[str] = []
                for word in text.split(" "):
                    if delay and self._interrupted:
                        text = " ".join(said)  # what an interrupted answer keeps
                        break
                    said.append(word)
                    await sink(
                        AssistantDelta(agent_id=agent, message_id=message_id, text=word + " ")
                    )
                    if delay:
                        await self._pause(delay)
                    else:
                        await anyio.lowlevel.checkpoint()
            await sink(
                AssistantText(
                    agent_id=agent,
                    text=text,
                    message_id=message_id,
                    uuid=self._uuid() if agent is None else "",
                )
            )
        elif "sleep" in action:
            await self._pause(float(action["sleep"]))
        elif "permission_mode" in action:
            self.permission_mode = str(action["permission_mode"])
            await sink(PermissionModeChanged(mode=self.permission_mode))
        elif "rate_limit" in action:
            limit = RateLimit.model_validate(action["rate_limit"])
            if limit.reported_at is None:
                limit = limit.model_copy(update={"reported_at": datetime.now(UTC)})
            self._rate_limits[limit.type] = limit
            await sink(UsageUpdate(rate_limits=list(self._rate_limits.values())))
        elif "background_tasks" in action:
            pass  # reported with the turn's result (`_turn`)
        elif "tool" in action:
            await self._tool(action, agent, gate, sink)
        elif "parallel" in action:
            async with anyio.create_task_group() as branches:
                for branch in action["parallel"]:
                    branches.start_soon(self._actions, list(branch), agent, gate, sink)
        elif "agent" in action:
            spec = action["agent"]
            tool_use_id = f"fake-tool-{self._id_prefix}-{next(self._ids)}"
            tool_input = {
                "description": spec.get("description", ""),
                "prompt": spec.get("prompt", ""),
                "subagent_type": spec.get("subagent_type", "general-purpose"),
                "run_in_background": bool(spec.get("background", False)),
            }
            await sink(
                ToolUse(agent_id=agent, tool_use_id=tool_use_id, name="Agent", input=tool_input)
            )
            # As in Claude Code, the Agent call passes the PreToolUse hook first: a refused
            # task starts no subagent.
            allowed, message = await self._gated(
                Step(
                    tool_name="Agent",
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                    agent_id=agent,
                    cwd=str(self.workspace),
                ),
                gate,
                sink,
            )
            if not allowed:
                await sink(
                    ToolResult(
                        agent_id=agent, tool_use_id=tool_use_id, is_error=True, content=message
                    )
                )
                return
            info = AgentInfo(
                agent_id=tool_use_id,
                sdk_agent_id=f"fake-agent-{tool_use_id}",
                description=tool_input["description"],
                subagent_type=tool_input["subagent_type"],
                background=tool_input["run_in_background"],
            )
            self.agents[tool_use_id] = info
            await sink(AgentsUpdate(agents=list(self.agents.values())))
            result_text = str(spec.get("result", "Subagent finished."))
            failed = bool(spec.get("fails"))

            async def work() -> str:
                """Run the subagent to its end: its actions, its status and the review.

                Returns:
                    The note of the result's review for the main agent; "" if there is none.
                """
                await self._actions(list(spec.get("actions", [])), tool_use_id, gate, sink)
                info.status = "failed" if failed else "completed"
                await sink(AgentsUpdate(agents=list(self.agents.values())))
                return await gate.review_subagent_result(
                    SubagentResult(
                        agent_id=tool_use_id,
                        description=tool_input["description"],
                        prompt=tool_input["prompt"],
                        result=result_text,
                        background=tool_input["run_in_background"],
                    )
                )

            if spec.get("detach") and self._tasks is not None:

                async def beside() -> None:
                    """Run the subagent beside the turns; its end may start the next turn."""
                    try:
                        note = await work()
                        if note:
                            await sink(UserInput(text=note, source=NOT_FROM_USER))
                    finally:
                        self._detached -= 1
                    if not self._in_turn and not self._detached:
                        await anyio.sleep(FOLLOW_UP_DELAY_S)
                        await self._send.send((None, []))

                self._detached += 1
                self._tasks.start_soon(beside)
                await sink(
                    ToolResult(
                        agent_id=agent, tool_use_id=tool_use_id, content=LAUNCH_ACKNOWLEDGEMENT
                    )
                )
                return
            note = await work()
            await sink(
                ToolResult(
                    agent_id=agent, tool_use_id=tool_use_id, is_error=failed, content=result_text
                )
            )
            if note:
                # Stands for the injected priority-"next" note: calu's text, not the user's.
                await sink(UserInput(text=note, source=NOT_FROM_USER))
        else:
            raise ValueError(f"unknown fake action: {action!r}")

    async def _thinking(self, action: dict[str, Any], agent: str | None, sink: EventSink) -> None:
        """Report a thinking action: its fragments if it streams, then the completed text.

        Args:
            action: The action dict, with `thinking` and optionally `message` (a name: the
                same name gives the same message id within this runner) and `stream`.
            agent: Protocol id of the subagent that thinks, or None for the main agent.
            sink: Receives the events.
        """
        text = str(action["thinking"])
        name = action.get("message")
        if name is not None:
            message_id: str | None = f"fake-msg-{self._id_prefix}-{name}"
        elif action.get("stream"):
            message_id = f"fake-msg-{self._id_prefix}-{next(self._ids)}"
        else:
            message_id = None
        if action.get("stream") and message_id is not None:
            for word in text.split(" "):
                await sink(
                    AssistantDelta(
                        agent_id=agent, message_id=message_id, text=word + " ", thinking=True
                    )
                )
                await anyio.lowlevel.checkpoint()
        await sink(AssistantThinking(agent_id=agent, text=text, message_id=message_id))

    async def _tool(
        self, action: dict[str, Any], agent: str | None, gate: ToolGate, sink: EventSink
    ) -> None:
        """Run one scripted tool call through the gate and report `ToolUse` and `ToolResult`.

        An allowed call reports the scripted "result"; a denied call reports the denial
        message as an error result, and an allowed call with "fails" reports its "result" as an
        error result without writing anything.

        Side effect: an action with "writes" writes (or deletes) those files in the call's
        working directory, and an action with "run" runs that program there, after the gate
        approved the call, which is how a scripted command stands in for one that
        edits files (`sed -i`, a formatter, a script).

        Args:
            action: The action dict with "tool", optional "input", "cwd" (the call's working
                directory, relative to the workspace or absolute; default the workspace),
                "writes", "run", "fails" and "result".
            agent: Protocol id of the calling subagent, or None for the main agent.
            gate: Decides about the call; `ask` is awaited for an ASK verdict.
            sink: Receives the events.
        """
        tool_use_id = f"fake-tool-{self._id_prefix}-{next(self._ids)}"
        name, tool_input = str(action["tool"]), dict(action.get("input", {}))
        await sink(ToolUse(agent_id=agent, tool_use_id=tool_use_id, name=name, input=tool_input))
        self.tool_modes.append((name, self.permission_mode))
        cwd = self.workspace / str(action.get("cwd", ""))
        step = Step(
            tool_name=name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            agent_id=agent,
            cwd=str(cwd),
        )
        allowed, message = await self._gated(step, gate, sink)
        note = ""
        if allowed and action.get("fails"):
            content, is_error = str(action.get("result", "The tool call failed.")), True
        elif allowed and action.get("duration") and await self._cut_short(action["duration"]):
            content, is_error = INTERRUPTED_TOOL_RESULT, True
        elif allowed:
            _write_files(cwd, action.get("writes") or {})
            if action.get("run"):
                await anyio.run_process([str(word) for word in action["run"]], cwd=cwd, check=False)
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

    async def _gated(self, step: Step, gate: ToolGate, sink: EventSink) -> tuple[bool, str]:
        """Ask the gate about one call, as the SDK runner's hook and `can_use_tool` do.

        Side effects: may send the gate's note for the main agent; records the hook's answer
        in `hook_answers`; waits for `gate.ask` on an ASK verdict (an interrupt cancels it).

        Args:
            step: The call.
            gate: Decides about the call.
            sink: Receives the note for the main agent.

        Returns:
            (whether the call runs, the message for a refused call).
        """
        gate_result = await gate.pre_tool_use(step)
        if gate_result.main_note:
            # As the SDK runner's hook: the note for the main agent goes out before the answer.
            await sink(UserInput(text=gate_result.main_note, source=NOT_FROM_USER))
        # As the SDK runner's hook: the mode Claude Code is in when the answer goes out decides
        # what a PASS means (D250), not the one the call started in.
        decision = hook_decision(gate_result.verdict, self.permission_mode, False)
        self.hook_answers.append((step.tool_name, decision))
        if decision is None and self.permission_mode != "auto":
            # No decision and no auto mode by now: Claude Code asks for its own approval.
            decision = "ask"
        if decision == "ask":
            # An interrupt cancels the waiting call, as Claude Code cancels its permission
            # request; the gate then closes the escalation as cancelled.
            allowed, message = False, INTERRUPTED_TOOL_RESULT
            with anyio.CancelScope() as waiting:
                self._asks.add(waiting)
                try:
                    answer = await gate.ask(step)
                    allowed = answer.allow
                    message = answer.message or "The user denied this step."
                finally:
                    self._asks.discard(waiting)
            return allowed, message
        # No decision in auto mode: the scripted auto mode classifier approves every step
        # (agent/records/PLAN.md D21).
        return decision in (None, "allow"), gate_result.reason

    async def _cut_short(self, seconds: float) -> bool:
        """Let a tool call take its scripted time.

        Args:
            seconds: How long the call runs.

        Returns:
            True if an interrupt ended the call before its time was over.
        """
        await self._pause(float(seconds))
        return self._interrupted


def _write_files(workspace: Path, writes: dict[str, str]) -> None:
    """Write or delete the files of a scripted call's "writes" mapping.

    Args:
        workspace: The call's working directory; every path is relative to it.
        writes: Path -> content; an empty content deletes the file.
    """
    for relative, content in writes.items():
        path = workspace / relative
        if content == "":
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
