"""What a `calu.session.Session` knows about one run: one Claude session from `start` to its end.

A `Session` object outlives runs (`Session.start` may be called again after a run ended). The
fields that describe one run are collected here, so that a new run starts from the defaults of
`RunState` and a field is carried into the next run only where `RunState.following` names it:

    Session.__init__ ── RunState.before_first_run() ──► no run, `run_done` is set
    Session.start    ── previous.following(tg)      ──► a fresh RunState; carried over: see there

`PerCall` holds what the session remembers about one tool call, by Claude's `tool_use_id`.

The open escalations and the hold they put on other steps are not part of a run's state: they
belong to `calu.escalations.EscalationDesk`, which a start does not replace.

This module holds data only. It does not know the session, the runner, the store or the clients.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field

import anyio
from anyio.abc import TaskGroup

from calu.changes import SnapshotStore
from calu.checkers.model import CodeMark
from calu.checkers.tools import StepTime
from calu.context.narrator import ClaudeOutput
from calu.protocol.messages import QueuedMessage

STEP_TIMES_KEPT = 500
"""How many step times a session keeps in memory for `step_times` (PLAN.md D32)."""


@dataclass
class PendingHonesty:
    """What one tool call changes in the session's honesty state if it succeeds.

    The gate's checkers judge a step before it runs. Their marks and the verification of an
    edit are kept here and applied to `calu.honesty.HonestyState` only after the tool result
    arrived without error (`Session`); a denied or failed call discards the record.
    """

    marks: list[tuple[str, list[CodeMark], list[CodeMark]]] = field(
        default_factory=list[tuple[str, list[CodeMark], list[CodeMark]]]
    )
    """(checker name, its `marks_dirty`, its `marks_clean`) for each checker that wrote marks."""
    edit_path: str | None = None
    """The file an Edit, Write, MultiEdit or NotebookEdit call targets, as the tool names it;
    None for every other call."""
    expected_sha256: str | None = None
    """The SHA-256 of the content the edit produces, computed before the checkers judged it
    (`HonestyState.expect_edit`); None if it could not be predicted."""
    checked: bool = False
    """True if checkers of the edit route judged the edit and every one of them approved."""
    clean_commit: bool = False
    """True for a plain `git commit` that the gate decided while the honesty state was clean."""
    head_before: str | None = None
    """HEAD before a `clean_commit` ran; the commit is certified only if HEAD moved."""


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
    honesty: PendingHonesty | None = None
    """What the call changes in the honesty state once it succeeded; None if nothing, or once
    it was applied or discarded."""

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
    queue: list[tuple[QueuedMessage, str]] = field(default_factory=list[tuple[QueuedMessage, str]])
    """The chat messages Claude has not read yet, each with the text that was sent for it."""
    clock_alarm: str | None = None  # a backward clock jump no human has answered yet (D32)
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

        - `queue`: the end of a run empties it, not its start.
        - `clock_alarm`: it stays until a human has decided the escalation that shows it.

        `store_failures` and `stopping` start from their defaults like the rest: a run that
        was stopped after repeated failed database writes must not leave the next run
        without that stop.

        `run_done` of the result is not set: a run is beginning.

        Args:
            tg: The task group that runs the new session.

        Returns:
            The new state. `self` is not changed.
        """
        return RunState(
            tg=tg,
            queue=self.queue,
            clock_alarm=self.clock_alarm,
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

        It is called wherever a field was emptied; the record goes with the last field.
        (`forget` removes the record of a step that is no tool call.)

        Args:
            tool_use_id: Claude's id of the call; an unknown id is ignored.
        """
        record = self.calls.get(tool_use_id)
        if record is not None and record.is_empty():
            del self.calls[tool_use_id]

    def forget(self, tool_use_id: str) -> None:
        """Remove the record of a call that gets no tool result, with whatever is in it.

        For a step that is no tool call of Claude Code, such as the review of a subagent's
        result: no result event arrives that would empty its fields, so `release` would keep
        the record, and its `running` mark would keep the session in the state "tool_call".

        Args:
            tool_use_id: The id the step was recorded under; an unknown id is ignored.
        """
        self.calls.pop(tool_use_id, None)

    def any_running(self) -> bool:
        """Say whether an approved tool call has not finished yet.

        Returns:
            True if at least one record in `calls` has `running` set.
        """
        return any(record.running for record in self.calls.values())
