"""What a `calu.session.Session` knows about one run: one Claude session from `start` to its end.

A `Session` object outlives runs (`Session.start` may be called again after a run ended). The
fields that describe one run are collected here, so that a new run starts from the defaults of
`RunState` and a field is carried into the next run only where `RunState.following` names it:

    Session.__init__ ── RunState.before_first_run() ──► no run, `run_done` is set
    Session.start    ── previous.following(tg)      ──► a fresh RunState; carried over: see there

`PerCall` holds what the session remembers about one tool call, by Claude's `tool_use_id`.

This module holds data only. It does not know the session, the runner, the store or the clients.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import anyio
from anyio.abc import TaskGroup

from calu.changes import SnapshotStore
from calu.checkers import Step
from calu.checkers.tools import StepTime
from calu.claude.runner import HumanAnswer
from calu.context.narrator import ClaudeOutput
from calu.protocol.messages import EscalationRequest, QueuedMessage

STEP_TIMES_KEPT = 500
"""How many step times a session keeps in memory for `step_times` (PLAN.md D32)."""

Outcome = Literal[
    "allowed", "denied", "answered", "interrupted", "cancelled", "timed_out", "unstorable"
]
"""How an escalation ended. A human decided for "allowed", "denied" and "answered" (the answer
to a question of the agent). Nobody decided for the others: the turn was interrupted or the wait
cancelled ("interrupted"), the session stopped or the row was cancelled in the database
("cancelled"), the escalation timeout ran out, or the escalation could not be stored."""


@dataclass
class Escalation:
    """An open escalation: the step, its database row and, once decided, the human's answer."""

    step: Step
    reason: str = ""
    source: str = "checker"
    """Who escalated: the `source` of the step's `_Decided` ("checker", "policy", "rules",
    "auto", "held", "changeset", "clock", "question", "internal"), or "harness" (Claude Code
    asked by itself, D38), "write_set" or "subagent_result" (the reviews after a step). A
    "question" is the only source that does not hold other steps (`Session._holding`)."""
    db_id: int | None = None
    """Id of the `escalations` row, or None if it could not be stored."""
    request: EscalationRequest | None = None
    """The event that asked the human, kept so that a client which connects later sees it."""
    done: anyio.Event = field(default_factory=anyio.Event)
    wake: anyio.Event = field(default_factory=anyio.Event)
    """Set by `note_resolution` so the waiting session reads the row at once."""
    answer: HumanAnswer | None = None
    answered: bool = False
    """True if this was a question of the agent and `answer` carries the human's answer (D35)."""
    outcome: Outcome = "interrupted"
    """How the escalation ended; set together with `answer`. While `answer` is None it reads
    "interrupted", which is what an escalation is whose wait was cancelled before any answer."""
    deciding: bool = False
    """True while a client's decision is being written to the database (`decide_escalation`)."""


@dataclass
class PerCall:
    """What the session remembers about one tool call, by Claude's `tool_use_id`.

    Every field starts empty and is set and emptied on its own, at the moment the session learns
    or uses it. A record in which every field is empty says the same as no record
    (`RunState.release` removes it).
    """

    step_id: int | None = None
    """Id of the call's `steps` row; None if the row could not be stored or the call's result
    was recorded already."""
    time_seq: int | None = None
    """Sequence number of the call's entry in `RunState.times`; None once the call ended."""
    before: tuple[str, int | None, str] | None = None
    """The snapshot taken before an approved command (PLAN.md D31): the tree, the `steps` row id
    and the checker's summary of the step. None if no snapshot is open for the call."""
    overlapped: bool = False
    """True if another command ran while this call's snapshot was open, so the write set cannot
    be attributed to one of them."""
    changeset: str | None = None
    """The diff a commit would record, for the escalation's row; None if the call has none."""
    running: bool = False
    """True while the call is approved and has not finished."""
    pending_reason: tuple[str, str] | None = None
    """(reason, source) of an escalation the session decided on and for which `ask` has not been
    called yet; None otherwise."""

    def is_empty(self) -> bool:
        """Say whether the record holds nothing any more.

        Returns:
            True if every field has its default value.
        """
        return self == PerCall()


@dataclass
class RunState:
    """The state of one run of a `Session`. See the module docstring.

    Create one with `before_first_run` (no run yet) or `following` (a run begins). The session
    replaces its `RunState` only in `Session.start`; everything else changes the fields of the
    current one.
    """

    tg: TaskGroup | None = None
    """The task group that runs the session's tasks; None before the first run."""
    run_done: anyio.Event = field(default_factory=anyio.Event)
    """Set once the runner of this run has ended."""
    run_error: str | None = None
    """Why the runner failed, or None if it has not failed."""
    session_id: str | None = None
    """Claude Code's own session id, once known."""
    busy: bool = False  # Claude works on a turn
    checking: int = 0  # checker and narrator calls in flight
    step_seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    times: deque[StepTime] = field(default_factory=lambda: deque(maxlen=STEP_TIMES_KEPT))
    calls: dict[str, PerCall] = field(default_factory=dict[str, PerCall])
    """What is remembered about single tool calls, by `tool_use_id`. Calls without an id share
    the key "". Read with `peek`, write with `call`, and let `release` remove a record."""
    snapshots: SnapshotStore | None = None  # the optional write-set snapshot (D31)
    # agent id (None = main agent) -> thinking and text since that agent's last tool call
    claude_outputs: dict[str | None, list[ClaudeOutput]] = field(
        default_factory=dict[str | None, list[ClaudeOutput]]
    )

    # The fields below are not reset by a start: `following` carries them into the next run.
    escalations: dict[str, Escalation] = field(default_factory=dict[str, Escalation])
    escalation_closed: anyio.Event = field(default_factory=anyio.Event)
    """Set, and replaced by a new event, each time an escalation leaves `escalations` or
    stops being opened (`Session._wake_held`); `Session._hold` waits for it."""
    asking: dict[str, float] = field(default_factory=dict[str, float])
    """Steps whose gate verdict was ASK and for which `ask` has not been called yet: key
    (the tool_use_id, or a generated one) -> the `anyio.current_time()` at which the entry
    stops counting (`ASK_PENDING_S`)."""
    opening: int = 0
    """Calls of `Session._ask` that are storing their escalation and have not registered it
    yet."""
    queue: list[tuple[QueuedMessage, str]] = field(default_factory=list[tuple[QueuedMessage, str]])
    """The chat messages Claude has not read yet, each with the text that was sent for it."""
    clock_alarm: str | None = None  # a backward clock jump nobody has seen yet (D32)
    store_failures: int = 0  # database writes that failed in a row (D59)
    stopping: bool = False  # a stop was already started, so it is not started twice

    @classmethod
    def before_first_run(cls) -> RunState:
        """Return the state of a session that has not run yet.

        Returns:
            A `RunState` with the defaults and `run_done` set, because no runner is running.
        """
        state = cls()
        state.run_done.set()
        return state

    def following(self, tg: TaskGroup) -> RunState:
        """Return the state for the run that begins after this one.

        Every field has its default again, with these exceptions, which keep what `self` has
        (the same objects, so a task that still holds one of them sees the same content):

        - `escalations`, `escalation_closed`, `asking`, `opening` and `queue`: the end of a run
          empties them, not its start.
        - `clock_alarm`: it stays until a step has shown it to a human.
        - `store_failures`: only a database write that succeeds resets the count.
        - `stopping`: nothing resets it.

        `run_done` of the result is not set: a run is beginning.

        Args:
            tg: The task group that runs the new session.

        Returns:
            The new state. `self` is not changed.
        """
        return RunState(
            tg=tg,
            escalations=self.escalations,
            escalation_closed=self.escalation_closed,
            asking=self.asking,
            opening=self.opening,
            queue=self.queue,
            clock_alarm=self.clock_alarm,
            store_failures=self.store_failures,
            stopping=self.stopping,
        )

    def call(self, tool_use_id: str) -> PerCall:
        """Return the record of a tool call for writing, creating it if there is none.

        Args:
            tool_use_id: Claude's id of the call ("" for a call without one).

        Returns:
            The record in `calls`.
        """
        return self.calls.setdefault(tool_use_id, PerCall())

    def peek(self, tool_use_id: str) -> PerCall:
        """Return the record of a tool call for reading, without creating one.

        Args:
            tool_use_id: Claude's id of the call ("" for a call without one).

        Returns:
            The record in `calls`, or an empty record that is not in `calls` if there is none.
            Emptying a field of the result is therefore always safe; setting one is lost for an
            unknown call, which is what `call` is for.
        """
        return self.calls.get(tool_use_id) or PerCall()

    def release(self, tool_use_id: str) -> None:
        """Remove the record of a tool call if nothing is left in it.

        The only place that removes a record from `calls`. It is called wherever a field was
        emptied; the record goes with the last field.

        Args:
            tool_use_id: Claude's id of the call; an unknown id is ignored.
        """
        record = self.calls.get(tool_use_id)
        if record is not None and record.is_empty():
            del self.calls[tool_use_id]

    def any_running(self) -> bool:
        """Say whether an approved tool call has not finished yet.

        Returns:
            True if at least one record in `calls` has `running` set.
        """
        return any(record.running for record in self.calls.values())
