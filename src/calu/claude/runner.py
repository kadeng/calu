"""Interface between the session orchestrator and a running Claude Code session.

    Session ──(ToolGate)──► decides about tool calls      ◄── runner asks before each call
    Session ──(EventSink)─► receives normalised events    ◄── runner reports what Claude does
    Session ──(ClaudeRunner.send/interrupt/stop)──► runner

`SdkRunner` (`sdk_runner.py`) implements `ClaudeRunner` with the Claude Agent SDK. `FakeRunner`
(`fake_runner.py`) replays a script and is used by tests and front-end development.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from calu.checkers.model import Step
from calu.checkers.result_checker import SubagentResult
from calu.protocol.messages import AgentInfo, Event

EventSink = Callable[[Event], Awaitable[None]]
"""Receives events in the order Claude produced them. Must not block for long.

The contract between a runner and its sink (`ClaudeRunner.run`): the runner calls the sink
once for every event, in order, and an exception the sink raises never ends the stream and
never keeps a later event from being delivered. The runner logs such an exception at level
"error" with its traceback. It does not deliver the event a second time, so a sink that keeps
state (the session's "a turn is running") has to bring that state up to date before anything
in it can raise, or in a `finally`."""

NOT_FROM_USER: Literal["system"] = "system"
"""`UserInput.source` of a user-role message that no human sent through calu.

Claude Code puts more into the user role than the user's words: calu's own notes (a checker's
remark on a subagent result or on a write set), notifications of finished background tasks,
hook feedback, the marker of an interrupted request. The checkers accept only the user's
messages as authorization, so a runner reports every such text with this source; the session
then records it narrated, as a separate turn entry under `NOT_FROM_USER_LABEL`, and never
under "User:". A message of another Claude session keeps its own source, "cross_session"
(`calu.context.foreign`)."""

NOT_FROM_USER_LABEL = "Automatic message from calu or Claude Code, not from the user"
"""Turn entry label of a `NOT_FROM_USER` message, as the checkers read it."""


class StartRefused(Exception):
    """A runner did not start the session, because calu could not be sure it would be safe.

    The message says what was found and what to do about it. Nothing was started: no Claude
    Code process runs, and no step was taken.
    """


Priority = Literal["next", "later"]
"""Delivery of a user message: "next" = after the running tool calls, "later" = after the turn."""


class Verdict(StrEnum):
    """Result of the first stage of a tool call check (PreToolUse), see PLAN.md D21.

    PASS leaves the decision to Claude Code's own approval (the hook answers without a
    decision); ALLOW runs the step whatever Claude Code would have decided; DENY stops it; ASK
    makes the runner call `ToolGate.ask` and wait for the human. Where Claude Code has no
    approval of its own to defer to (auto mode off or unavailable), PASS acts like ALLOW.
    """

    PASS = "pass"
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class GateResult:
    """Verdict on a tool call.

    `reason` is shown to Claude for DENY. `context` is extra text that Claude receives together
    with the verdict (used for remarks of a checker on an allowed step); empty for none.
    """

    verdict: Verdict
    reason: str = ""
    context: str = ""


@dataclass(frozen=True)
class HumanAnswer:
    """The answer to an ASK verdict.

    `message` is shown to Claude when `allow` is False. `note` is the user's own text (may be
    empty); it is passed on to Claude for allowed steps too.
    """

    allow: bool
    message: str = ""
    note: str = ""
    cancelled: bool = False
    """True if no human decided (interrupt, stop); callers must not present it as a decision."""


@dataclass(frozen=True)
class Fork:
    """Where a resumed session branches off (PLAN.md D36).

    `resume_at` is the transcript uuid of the last entry of the turn that is kept; `drops_turn`
    the uuid of the prompt that starts the first discarded turn. Claude Code validates the pair
    and refuses a mismatch, so both are required. A forked session gets a new session id, which
    leaves the original conversation untouched.
    """

    resume_at: str
    drops_turn: str


class ToolGate(Protocol):
    """Decides whether Claude's tool calls may run."""

    async def pre_tool_use(self, step: Step) -> GateResult:
        """Decide about `step`. The runner calls this before every tool call.

        Args:
            step: The tool call that is about to run.

        Returns:
            ALLOW, DENY (with a reason for Claude) or ASK (the runner then calls `ask`).
        """
        ...

    async def ask(self, step: Step) -> HumanAnswer:
        """Wait for the human's decision about a step that got an ASK verdict.

        May wait without a time limit.

        Args:
            step: The tool call that got the ASK verdict.

        Returns:
            Whether the step may run, and a message for Claude if it may not.
        """
        ...

    async def review_write_set(self, step: Step) -> str:
        """Review what a finished command wrote, before the agent goes on (PLAN.md D31).

        The runner calls this after a command has run, while the agent waits, so the review may
        take its time and may even ask a human. It is a no-op unless the write-set snapshot is
        configured (`changes.snapshot`) and something was written, so a runner may call it for
        every command.

        Args:
            step: The tool call that has just run.

        Returns:
            A note for the agent, or "" if there is nothing to say. A note says what the review
            found and, if the change was undone, that it was undone.
        """
        ...

    async def tool_succeeded(self, step: Step) -> None:
        """Take note that an edit has run without error, before the agent goes on.

        The runner calls this after a successful Edit, Write, MultiEdit or NotebookEdit call,
        while the agent waits; for a command, `review_write_set` is called instead. The gate
        then applies what the call's checkers recorded for the case that it succeeds. A runner
        may call it for every successful tool call: a call nothing was recorded for is ignored.

        Args:
            step: The tool call that has just run.
        """
        ...

    async def review_subagent_result(self, result: SubagentResult) -> str:
        """Review a subagent's final result before the main agent acts on it.

        The runner holds the result back while this runs (it may wait for the human) and
        sends a non-empty return value to the main agent as a priority "next" message.

        Args:
            result: The subagent's task and final result.

        Returns:
            A note for the main agent, or "" if there is nothing to say.
        """
        ...


class ClaudeRunner(Protocol):
    """A Claude Code session that is driven programmatically.

    Lifecycle: `run` is started in a task and returns after `stop` (or when the session ends).
    `send` and `interrupt` may be called while `run` is active, after `wait_ready` returned.

    `session_id` is Claude's session id once known. `agents` holds the subagents of the session
    by protocol agent id (the id of the Agent tool call that started them).
    """

    session_id: str | None
    agents: dict[str, AgentInfo]

    async def run(
        self,
        gate: ToolGate,
        sink: EventSink,
        resume_id: str | None = None,
        fork: Fork | None = None,
    ) -> None:
        """Start the session and forward its events to `sink` until stopped.

        The runner calls `sink` once for every event, in the order Claude produced them. A
        sink that raises does not end the session and does not stop the stream: the runner
        logs the exception at level "error" and goes on with the next event (`EventSink`).
        Every turn that Claude Code ends is reported with one `turn.result` event, also when
        the runner could not read the details of the result.

        Args:
            gate: Asked before every tool call of the main agent and of subagents.
            sink: Receives the session's events in order.
            resume_id: Claude session id to resume, or None for a new session.
            fork: Where to branch off the resumed conversation (PLAN.md D36); None resumes it
                whole. Needs `resume_id`, and gives the new session its own id.

        Raises:
            StartRefused: If the runner did not start the session because calu could not be
                sure of a protection it relies on; the message says which and what to do.
        """
        ...

    async def wait_ready(self) -> None:
        """Return once the session accepts `send` calls."""
        ...

    async def send(self, text: str, priority: Priority) -> None:
        """Send a message of the human user to the main agent.

        Only text that a human client sent belongs here (a prompt, a queued or injected chat
        message, a comment): the runner reports the replay of exactly these texts as
        `UserInput` with source "user", and every other user-role message of the stream with
        `NOT_FROM_USER`.

        Args:
            text: The message text.
            priority: "next" delivers it after the running tool calls, "later" after the turn.
        """
        ...

    async def interrupt(self) -> None:
        """Stop Claude's current turn. The session stays usable."""
        ...

    async def stop(self) -> None:
        """End the session; `run` returns soon after."""
        ...
