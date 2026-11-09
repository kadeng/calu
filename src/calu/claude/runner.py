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
"""Receives events in the order Claude produced them. Must not block for long."""

Priority = Literal["next", "later"]
"""Delivery of a user message: "next" = after the running tool calls, "later" = after the turn."""


class Verdict(StrEnum):
    """Result of the first stage of a tool call check (PreToolUse)."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # the runner then calls `ToolGate.ask` and waits for its answer


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

    async def run(self, gate: ToolGate, sink: EventSink, resume_id: str | None = None) -> None:
        """Start the session and forward its events to `sink` until stopped.

        Args:
            gate: Asked before every tool call of the main agent and of subagents.
            sink: Receives the session's events in order.
            resume_id: Claude session id to resume, or None for a new session.
        """
        ...

    async def wait_ready(self) -> None:
        """Return once the session accepts `send` calls."""
        ...

    async def send(self, text: str, priority: Priority) -> None:
        """Send a user message to the main agent.

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
