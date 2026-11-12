"""Escalations of one session: asking a human about a step, and holding other steps meanwhile.

`EscalationDesk` is the component of `calu.session.Session` that owns every escalation:

    Session.pre_tool_use ── verdict ASK ──► desk.expect_ask      "being opened" from here on
    Session.ask / the reviews after a step ► desk.ask
                                               │ store the row (with retries)
                                               │ emit `escalation.request`
                                               │ wait: client decision │ database row │ timeout
                                               │ emit `escalation.resolved`
                                               ▼
                                             Asked(answer, outcome)
    a client's `escalation.decide` ────────► desk.decide          (the database decides first)
    `escalation_resolved` notification ────► desk.note_resolution
    stop, end of the run ──────────────────► desk.cancel_all
    interrupt ─────────────────────────────► desk.turn_interrupted
    the gate, for a call that needs a check ► desk.hold / desk.holding

The desk does not know the session. It gets the store, the event sink and the few things it
needs to know about the session (the escalation timeout, the context of a row, how to stop)
through its constructor.
"""

import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio

from calu.checkers import Step
from calu.checkers.render import render_step
from calu.claude.runner import HumanAnswer
from calu.protocol.messages import EscalationRequest, EscalationResolved, Event, Log
from calu.store import SessionStore

log = logging.getLogger(__name__)

ANSWER_TEMPLATE = (
    "The user answered your question: {answer}\nThe AskUserQuestion call itself was not run; "
    "this is the answer. Carry on with it."
)
"""What Claude receives when a human answers its question (PLAN.md D35). It travels as the
message of the refusal that keeps the call from running, which the spike showed Claude acts on."""

ASK_PENDING_S = 30.0
"""How long a step whose gate verdict was ASK counts as an escalation that is being opened
(`EscalationDesk.expect_ask`) if `ask` is never called for it. Claude Code calls `ask` right
after the hook answered, so this only bounds the case where it does not; without the bound such
a step would hold every later one for `calu.gate.HOLD_BUDGET_S`."""
STORE_DECISION_TIMEOUT_S = 30.0
"""Upper bound for writing a client's decision about an escalation, or the cancellation of an
escalation, to the database. These writes are shielded from cancellation, so without a bound a
database that does not answer would keep the escalation from ever being decided here."""
OPEN_ESCALATION_RETRY_S: tuple[float, ...] = (0.5, 2.0)
"""Pauses before the second and the third attempt to store an escalation. A step whose
escalation cannot be stored is denied, so one failed INSERT should not decide that."""
ESCALATION_POLL_S = 1.0
"""How often an open escalation's database row is read while the session waits for a human."""

DB_UNREACHABLE = (
    "calu could not record this step in the database, so no human can be asked about it. "
    "The step was denied. Tell the user that calu's database is unreachable."
)
"""Reason Claude receives when an escalation cannot be stored (the step is denied)."""

Outcome = Literal[
    "allowed", "denied", "answered", "interrupted", "cancelled", "timed_out", "unstorable"
]
"""How an escalation ended. A human decided for "allowed", "denied" and "answered" (the answer
to a question of the agent). Nobody decided for the others: the turn was interrupted or the wait
cancelled ("interrupted"), the session stopped or the row was cancelled in the database
("cancelled"), the escalation timeout ran out, or the escalation could not be stored."""


class EscalationError(Exception):
    """A client's decision about an escalation was not applied; the message says why."""


@dataclass
class Escalation:
    """An open escalation: the step, its database row and, once decided, the human's answer."""

    step: Step
    reason: str = ""
    source: str = "checker"
    """Who escalated: the `source` of the step's `calu.gate.Decided` ("checker", "policy", "rules",
    "auto", "held", "changeset", "clock", "question", "internal"), or "harness" (Claude Code
    asked by itself, D38), "write_set" or "subagent_result" (the reviews after a step). A
    "question" is the only source that does not hold other steps (`EscalationDesk.holding`)."""
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
    """True while a client's decision is being written to the database (`decide`)."""


@dataclass(frozen=True)
class Asked:
    """The result of asking a human: the answer for the runner and how the escalation ended.

    Before a step, only `answer.allow` matters: every outcome but "allowed" keeps the step from
    running. The reviews after a step need the outcome, because there "nobody decided" must not
    be treated as "a human refused".
    """

    answer: HumanAnswer
    outcome: Outcome


NOBODY_ASKED: dict[str, str] = {
    "unstorable": (
        "calu could not store the request in its database, so nobody could be asked. Tell the "
        "user that calu's database is unreachable."
    ),
    "timed_out": "Nobody answered within the escalation timeout, and the session stops.",
}
"""Why a review after a step got no human decision, by escalation outcome (see `Asked`); any
other outcome without a decision reads "No decision was made."."""


class EscalationDesk:
    """The escalations of one `Session` object, and the hold they put on other steps.

    One desk lives as long as its session object; a new run (`Session.start`) does not replace
    it. The end of a run empties it (`cancel_all`).

    Invariants:

    1. Session-wide hold. `holding()` is not "" exactly while at least one of these is true:
       an escalation whose source is not "question" is in `escalations`; a call of `ask` for
       such a source is storing its row (`_opening > 0`); an entry of `expect_ask` has not
       expired (`_asking`). The gate approves no step that needs a check while `holding()` is
       not "".
    2. No gap while an escalation is opened. `ask` counts itself in `_opening` before it removes
       the step's entry from `_asking`, and counts itself out only after the escalation is in
       `escalations`; no `await` stands between these steps and their neighbours, so `holding()`
       never reads "" in between.
    3. A change that can end a hold wakes the waiting calls (`_wake_held`): `ask` when it
       ends, with or without an escalation; `call_ended`, `turn_interrupted` and `cancel_all`
       when they remove entries. An entry of `_asking` that expires needs no wake, because
       `hold` wakes up by itself at the earliest expiry.
    4. One answer. `Escalation.answer` is set once, together with `outcome`, and `done` is set
       with it. Whoever comes first sets it: a client's decision that the database accepted
       (`decide`), the database row (`_wait_for_decision`), the timeout, `cancel_all` or
       `turn_interrupted`.
    5. The database decides first. A client's decision releases a step only after
       `store.decide_escalation` returned True for it.
    6. Only "allowed" lets a step run. Every other outcome carries an answer with `allow` False;
       an escalation that could not be stored is one of them ("unstorable").
    7. No row stays open behind an escalation that ended here: it was resolved in the database,
       or `_close_row` cancelled it. `_close_row` is the only place that calls
       `store.close_escalation`.
    """

    def __init__(
        self,
        store: SessionStore,
        workspace: Path,
        *,
        emit: Callable[[Event], Awaitable[None]],
        refresh_state: Callable[[], Awaitable[None]],
        store_call: Callable[[str, Any], Awaitable[None]],
        store_failed: Callable[[str, BaseException], Awaitable[None]],
        store_succeeded: Callable[[], None],
        timeout_s: Callable[[], float],
        claude_session_id: Callable[[], str | None],
        row_context: Callable[[Step, str], dict[str, Any]],
        step_row: Callable[[Step], int | None],
        stop_session: Callable[[], None],
    ) -> None:
        """Create a desk without escalations.

        Args:
            store: Where escalation rows are opened, read, decided and cancelled.
            workspace: Workspace root, for rendering a step with relative paths.
            emit: Sends an event to the clients and never raises.
            refresh_state: Sets the session state to what the running work implies; called
                after `escalations` changed.
            store_call: Awaits a store call (second argument) and reports its failure under
                the given description (first argument); never raises.
            store_failed: Reports a failed store write: (what was attempted, the exception).
            store_succeeded: Notes that a store write succeeded, which ends a run of failures.
            timeout_s: Returns the configured escalation timeout in seconds at the moment of
                the call; 0 means no timeout.
            claude_session_id: Returns Claude Code's session id, or None if it is not known.
            row_context: Returns the context to store with a step's escalation row, given the
                step and its Markdown rendering.
            step_row: Returns the id of a step's `steps` row, or None if it has none.
            stop_session: Starts stopping the session in a task of its own and returns at once.
        """
        self._store = store
        self._workspace = workspace
        self._emit = emit
        self._refresh_state = refresh_state
        self._store_call = store_call
        self._store_failed = store_failed
        self._store_succeeded = store_succeeded
        self._timeout_s = timeout_s
        self._claude_session_id = claude_session_id
        self._row_context = row_context
        self._step_row = step_row
        self._stop_session = stop_session
        self.escalations: dict[str, Escalation] = {}
        """The open escalations, by the id clients know them by (the row id as a string).
        Others read it; only the desk changes it."""
        self._closed = anyio.Event()
        """Set, and replaced by a new event, each time an escalation leaves `escalations` or
        stops being opened (`_wake_held`); `hold` waits for it."""
        self._asking: dict[str, float] = {}
        """Steps whose gate verdict was ASK and for which `ask` has not been called yet: key
        (the tool_use_id, or a generated one) -> the `anyio.current_time()` at which the entry
        stops counting (`ASK_PENDING_S`)."""
        self._opening = 0
        """Calls of `ask` that are storing their escalation and have not registered it yet."""
        self._local_ids = itertools.count(1)

    # ------------------------------------------------------------------ the hold

    async def hold(self, step: Step, budget: anyio.CancelScope | None, limit_s: float) -> str:
        """Hold a tool call while an escalation of this session is open or being opened.

        A human is deciding about a step; until then no other step of the session is approved,
        so that every step is judged with the human's decision known. Escalations that are
        questions of the agent (source "question") do not hold anything: nothing was objected
        to.

        The wait ends when nothing holds any more (`holding`) or after `limit_s`, because the
        PreToolUse hook that carries this call has a timeout. It ends at once with the caller's
        cancellation (an interrupt cancels the hook). It cannot deadlock with the reviews after
        a step: they do not come through here.

        Side effects: emits a `log` event saying that the call waits and for what; moves the
        deadline of `budget` by the time waited, so that the hold does not use up the time for
        checking.

        Args:
            step: The tool call that reached the gate.
            budget: The cancel scope that bounds the caller's decision, or None if there is
                none.
            limit_s: The longest wait in seconds; a negative value counts as 0.

        Returns:
            "" if nothing holds the step (any more); otherwise what still held it when the
            wait ran out, as `holding` describes it, for the reason of the caller's decision.
        """
        limit = max(limit_s, 0.0)
        holding = self.holding()
        if not holding:
            return ""
        started = anyio.current_time()
        if budget is not None:
            budget.deadline += limit
        who = f"subagent {step.agent_id}" if step.agent_id else "the main agent"
        await self._emit(
            Log(
                level="info",
                message=(
                    f"{step.tool_name} of {who} waits: {holding} is not decided yet, and no "
                    "further step is approved until then"
                ),
            )
        )
        try:
            with anyio.move_on_after(limit):
                while self.holding():
                    closed = self._closed
                    # An expected `ask` that never comes stops counting without an event.
                    expiry = min(self._asking.values(), default=None)
                    with anyio.move_on_after(
                        None if expiry is None else max(expiry - anyio.current_time(), 0.0)
                    ):
                        await closed.wait()
        finally:
            if budget is not None:
                budget.deadline -= limit - (anyio.current_time() - started)
        return self.holding()

    def holding(self) -> str:
        """Describe what keeps new tool calls from being approved at this moment.

        Side effect: forgets the steps whose `ask` did not come within `ASK_PENDING_S`.

        Returns:
            "" if nothing does. Otherwise a phrase for a message, e.g. "escalation 4" or
            "escalation 4, 5 and an escalation that is being opened": every open escalation
            except a question of the agent, and "being opened" if a step's gate verdict was
            ASK and its escalation is not registered yet (`expect_ask`, `ask`).
        """
        now = anyio.current_time()
        for key in [key for key, until in self._asking.items() if until <= now]:
            del self._asking[key]
        ids = [
            escalation_id
            for escalation_id, esc in self.escalations.items()
            if esc.source != "question"
        ]
        parts = [f"escalation {', '.join(ids)}"] if ids else []
        if self._opening or self._asking:
            parts.append("an escalation that is being opened")
        return " and ".join(parts)

    def expect_ask(self, step: Step, source: str) -> None:
        """Count a step whose gate verdict is ASK as an escalation that is being opened.

        Between that verdict and the escalation's row lie the recording of the decision, Claude
        Code's call of `ask` and the INSERT with its retries. A step whose check finishes in
        that time must not be approved, so the hold starts here. The entry ends when `ask`
        takes over for the step, with the step's tool result (`call_ended`), with an interrupt
        or a stop, or after `ASK_PENDING_S`.

        Args:
            step: The step that will be asked about.
            source: Who escalated; a "question" of the agent holds nothing and is not counted.
        """
        if source == "question":
            return
        key = step.tool_use_id or f"unnamed-{next(self._local_ids)}"
        self._asking[key] = anyio.current_time() + ASK_PENDING_S

    def call_ended(self, tool_use_id: str) -> None:
        """Stop counting a tool call as an escalation that is being opened: its result is here.

        Wakes the held calls if the call was counted (it ended without `ask`).

        Args:
            tool_use_id: Claude's id of the call; an id that was not counted is ignored.
        """
        if self._asking.pop(tool_use_id, None) is not None:
            self._wake_held()  # the call ended without `ask`

    def _wake_held(self) -> None:
        """Wake the tool calls that `hold` keeps waiting, so they look at `holding` again."""
        closed, self._closed = self._closed, anyio.Event()
        closed.set()

    # ------------------------------------------------------------------ asking

    def requests(self) -> list[EscalationRequest]:
        """Return the events that asked for the open escalations, for a client that connects.

        Returns:
            One `escalation.request` event per open escalation, oldest first.
        """
        return [esc.request for esc in self.escalations.values() if esc.request is not None]

    async def ask(self, step: Step, reason: str, source: str) -> Asked:
        """Ask the human about `step` and wait for the answer; say how the escalation ended.

        Side effects: opens an `escalations` row, emits `escalation.request`, refreshes the
        session state, and emits `escalation.resolved` when the escalation ends — also when it
        is cancelled (stop, interrupt, end of the session), with decision "cancelled". The row
        is closed in every case: if this call itself is cancelled while it waits (Claude Code
        cancels the permission callback on an interrupt), the row is cancelled with a note.
        The wait has no time limit except the configured escalation timeout.

        Args:
            step: The tool call that needs a decision.
            reason: Why it needs a decision; the human reads it.
            source: Who escalated (see `Escalation.source`).

        Returns:
            The answer and the outcome (see `Outcome`). The outcome is what the reviews after a
            step act on: only "denied" is a refusal by a human. An escalation that ended without
            a human decision (stop, timeout, interrupt) counts as a denial and has `cancelled`
            set. If the escalation cannot be stored, the step is denied with `DB_UNREACHABLE`,
            because no human could see it.
        """
        # Until the escalation is in `escalations`, this call is what holds other steps:
        # the count goes up before the entry of `expect_ask` goes, and down only after the
        # registration, with no await in between.
        holds = source != "question"
        self._opening += 1 if holds else 0
        self._asking.pop(step.tool_use_id or "", None)
        esc: Escalation | None = None
        try:
            if source == "harness" and step.asked_about:
                # Claude Code's own prompt sentence first, then why it reached a human: for a
                # capability request the tool name and the input say nothing actionable (D60).
                reason = f"{step.asked_about}\n\n{reason}"
            rendered = _render_safely(step, self._workspace)
            db_id = await self._open_row(step, reason, rendered)
            if db_id is None:
                return Asked(
                    HumanAnswer(allow=False, message=DB_UNREACHABLE, cancelled=True),
                    "unstorable",
                )
            escalation_id = str(db_id)
            esc = Escalation(step=step, reason=reason, source=source, db_id=db_id)
            esc.request = EscalationRequest(
                escalation_id=escalation_id,
                questions=_questions(step),
                tool_use_id=step.tool_use_id,
                agent_id=step.agent_id,
                tool_name=step.tool_name,
                tool_input=step.tool_input,
                reason=reason,
                rendered=rendered,
            )
            self.escalations[escalation_id] = esc
        finally:
            self._opening -= 1 if holds else 0
            if esc is None:
                self._wake_held()  # nothing was opened: the held calls look again
        assert esc is not None and esc.request is not None
        escalation_id = str(esc.db_id)
        try:
            await self._emit(esc.request)
            await self._refresh_state()
            await self._wait_for_decision(esc)
        finally:
            self.escalations.pop(escalation_id, None)
            self._wake_held()  # the tool calls that `hold` keeps waiting for this escalation
            with anyio.CancelScope(shield=True):
                answer = esc.answer
                if answer is None and esc.db_id is not None:
                    # This call was cancelled while it waited, so nothing else closes the row.
                    await self._close_row(
                        esc.db_id,
                        "cancelled: the wait for a decision was interrupted",
                        bounded=True,
                    )
                if answer is None or esc.outcome not in ("allowed", "denied", "answered"):
                    decision = "cancelled"
                else:
                    decision = "allow" if answer.allow else "deny"
                await self._emit(
                    EscalationResolved(
                        escalation_id=escalation_id,
                        decision=decision,
                        note=None
                        if answer is None
                        else (answer.note or None)
                        if answer.allow
                        else answer.message,
                    )
                )
                await self._refresh_state()
        assert esc.answer is not None
        return Asked(esc.answer, esc.outcome)

    async def _open_row(self, step: Step, reason: str, rendered: str) -> int | None:
        """Open an `escalations` row for a step that needs a human decision.

        Args:
            step: The tool call.
            reason: Why it needs a decision.
            rendered: The step rendered as Markdown, for the operator.

        Returns:
            The id of the row, or None if it could not be stored in
            `1 + len(OPEN_ESCALATION_RETRY_S)` attempts. Only the last failure is reported as a
            store failure (`store_failed` of the constructor).
        """
        context = self._row_context(step, rendered)
        pauses: tuple[float | None, ...] = (*OPEN_ESCALATION_RETRY_S, None)
        for pause in pauses:
            try:
                stored = await self._store.open_escalation(reason, context, self._step_row(step))
            except Exception as exc:
                if pause is None:
                    await self._store_failed("storing an escalation", exc)
                    return None
                log.warning("storing an escalation failed, trying again in %s s: %s", pause, exc)
                await anyio.sleep(pause)
            else:
                self._store_succeeded()
                return stored
        return None

    async def _close_row(self, db_id: int, message: str, bounded: bool = False) -> None:
        """Mark an escalation row as cancelled: nobody decided. Never raises.

        The only place that calls `store.close_escalation`. A failure is reported through
        `store_call` of the constructor; with `bounded`, a database that does not answer within
        `STORE_DECISION_TIMEOUT_S` is such a failure, and the row then stays open and
        answerable while no step waits for it any more.

        Args:
            db_id: The `escalations` row.
            message: Why it was cancelled, for the row's note.
            bounded: True if the write must not wait longer than `STORE_DECISION_TIMEOUT_S`
                (the caller is itself being cancelled).
        """

        async def close_bounded() -> None:
            """Close the row within `STORE_DECISION_TIMEOUT_S`.

            Raises:
                TimeoutError: If the database does not answer in time.
            """
            with anyio.fail_after(STORE_DECISION_TIMEOUT_S):
                await self._store.close_escalation(db_id, message)

        await self._store_call(
            "cancelling an escalation",
            close_bounded() if bounded else self._store.close_escalation(db_id, message),
        )

    async def _wait_for_decision(self, esc: Escalation) -> None:
        """Wait until `esc` is decided locally or in the database.

        Reads the escalation row every `ESCALATION_POLL_S` seconds, and at once when
        `note_resolution` was called for it, until it is no longer open; a local
        `decide` ends the wait as well. Store errors are logged and the poll
        continues, so a short database outage does not turn into a denial.

        Args:
            esc: The open escalation; its `answer` and `done` are set by this call or by
                `decide`.
        """
        timeout = self._timeout_s() or float("inf")
        if esc.db_id is None:
            with anyio.move_on_after(timeout):
                await esc.done.wait()
            if esc.answer is None:
                await self._timed_out(esc)
            return

        async def poll() -> None:
            """Read the escalation row until it is resolved."""
            while True:
                wake = esc.wake
                with anyio.move_on_after(ESCALATION_POLL_S):
                    await wake.wait()
                esc.wake = anyio.Event()
                assert esc.db_id is not None
                try:
                    row = await self._store.escalation(esc.db_id)
                except Exception:
                    log.exception("reading an escalation failed")
                    continue
                if row.open:
                    continue
                if esc.answer is not None:
                    return  # a client's decision was accepted while the row was being read
                if row.answered:
                    # A question of the agent that an operator answered in the database (D35).
                    esc.answered = True
                    esc.answer = HumanAnswer(
                        allow=False, message=ANSWER_TEMPLATE.format(answer=row.note)
                    )
                    esc.outcome = "answered"
                    esc.done.set()
                    return
                message = (
                    "The user denied this step."
                    if not row.allowed
                    else "The user allowed this step."
                )
                if row.note:
                    message += f" Note from the user: {row.note}"
                esc.answer = HumanAnswer(allow=row.allowed, message=message, note=row.note)
                if row.allowed:
                    esc.outcome = "allowed"
                else:
                    # A row that was cancelled was decided by nobody; only "denied" is a refusal.
                    esc.outcome = "denied" if row.state == "denied" else "cancelled"
                esc.done.set()
                return

        async with anyio.create_task_group() as tg:
            tg.start_soon(poll)
            with anyio.move_on_after(timeout):
                await esc.done.wait()
            tg.cancel_scope.cancel()
        if esc.answer is None:
            await self._timed_out(esc)

    async def _timed_out(self, esc: Escalation) -> None:
        """Deny the step of an escalation nobody answered, and stop the session (D27).

        The session gives the machine and the Claude session back instead of waiting for ever.
        Claude's session id is in the database, so `calu run --resume <id>` takes the
        conversation up again; the escalation row is cancelled with the reason. The stop runs in
        its own task (`stop_session` of the constructor), because this call happens inside the
        tool call that is being held: waiting for the runner to end here would deadlock.

        Args:
            esc: The escalation that was not answered.
        """
        seconds = self._timeout_s()
        session_id = self._claude_session_id()
        resume = f"calu run --resume {session_id}" if session_id else "calu sessions"
        message = (
            f"No decision within {seconds / 3600:.1f} h. The step was denied and the session "
            f"stops; take the conversation up again with `{resume}`."
        )
        esc.answer = HumanAnswer(allow=False, message=message, cancelled=True)
        esc.outcome = "timed_out"
        esc.done.set()
        if esc.db_id is not None:
            await self._close_row(esc.db_id, message)
        await self._emit(Log(level="error", message=message))
        self._stop_session()

    # ------------------------------------------------------------------ deciding

    async def decide(
        self,
        escalation_id: str,
        decision: str,
        note: str | None,
        client: str = "",
        answer: str | None = None,
        token_sha256: str | None = None,
    ) -> None:
        """Resolve an open escalation from a client. Only the first decision counts.

        The decision is recorded in the `escalations` row as the resolution it is, with
        `resolved_by` saying that a client of this session decided and which one (PLAN.md D28).
        A client needs no database login for that: it authenticates to the session with its
        token, and the session speaks to the database for it. An operator who resolves the row
        in the database instead reaches the session through `note_resolution` or its poll.

        The database decides first, and the step is released only by a decision it accepted:

            store.decide_escalation(...)    step                      this call
            True                            released with the answer  returns
            False (row no longer open, or   stays held; the poll      raises EscalationError
              this session may not decide)  adopts what the row says
            raises, or no answer within     stays held; the poll      raises EscalationError
              `STORE_DECISION_TIMEOUT_S`    adopts what the row says

        The write and the release happen in a scope shielded from cancellation, because the
        request of a client that disconnects right after sending it is cancelled: the row and
        the step must not end up saying different things. The write is bounded, so that a
        database which does not answer cannot keep every later decision out (`esc.deciding`).

        Args:
            escalation_id: Id from the `escalation.request` event.
            decision: "allow" or "deny"; ignored when `answer` is given.
            note: Optional text from the user; Claude receives it with a denial.
            client: Name of the client that decided, for the record.
            answer: The answer to a question of the agent (`escalation.request` with
                `questions`, PLAN.md D35). The call does not run; Claude receives this text as
                its result, and the row is recorded as "answered".
            token_sha256: SHA-256 of the token the deciding client authenticated with, which
                the database checks before it records the decision; None uses a token of this
                session that the store remembers.

        Raises:
            EscalationError: If no escalation with this id is open (unknown, already decided, or
                another decision for it is being recorded), if the database refused the
                decision, or if the decision could not be written. In the last two cases the
                step has not been released. Also if the escalation was cancelled (stop,
                timeout, interrupt) while the decision was written: the step did not run.
        """
        esc = self.escalations.get(escalation_id)
        if esc is None or esc.answer is not None:
            raise EscalationError(f"no open escalation {escalation_id!r}")
        if esc.deciding:
            raise EscalationError(f"escalation {escalation_id!r} is being decided right now")
        allow = answer is None and decision == "allow"
        if answer is not None:
            human = HumanAnswer(allow=False, message=ANSWER_TEMPLATE.format(answer=answer))
            outcome: Outcome = "answered"
        else:
            message = "The user denied this step."
            if note:
                message += f" Note from the user: {note}"
            human = HumanAnswer(allow=allow, message=message, note=note or "")
            outcome = "allowed" if allow else "denied"
        esc.deciding = True
        try:
            with anyio.CancelScope(shield=True):
                if esc.db_id is not None:
                    try:
                        with anyio.fail_after(STORE_DECISION_TIMEOUT_S):
                            recorded = await self._store.decide_escalation(
                                esc.db_id,
                                allow,
                                answer if answer is not None else (note or ""),
                                client,
                                answered=answer is not None,
                                token_sha256=token_sha256,
                            )
                    except Exception as exc:
                        log.exception("recording a client decision failed")
                        await self._emit(
                            Log(
                                level="error",
                                message=f"recording a client decision failed: {exc}",
                            )
                        )
                        raise EscalationError(
                            f"the decision could not be recorded, so escalation "
                            f"{escalation_id} stays open: {exc}"
                        ) from exc
                    if not recorded:
                        esc.wake.set()  # the poll reads what the row says now
                        raise EscalationError(
                            f"escalation {escalation_id} was already resolved in the "
                            "database, or this session may not decide it; the step follows "
                            "what the database says"
                        )
                if esc.answer is None:  # the poll may have adopted a resolution meanwhile
                    esc.answer = human
                    esc.answered = answer is not None
                    esc.outcome = outcome
                    esc.done.set()
                elif esc.answer.cancelled:
                    raise EscalationError(
                        f"escalation {escalation_id} was cancelled while the decision was "
                        "recorded, so the step did not run"
                    )
        finally:
            esc.deciding = False

    def note_resolution(self, escalation_id: int) -> None:
        """Wake the wait for an escalation so it reads the resolved row at once.

        Called with the id from an `escalation_resolved` notification (`calu.db.notify`). An
        unknown id is ignored; without this call the resolution is noticed with the next poll
        (`ESCALATION_POLL_S`).

        Args:
            escalation_id: Id of the resolved `escalations` row.
        """
        for esc in self.escalations.values():
            if esc.db_id == escalation_id:
                esc.wake.set()

    # ------------------------------------------------------------------ cancelling

    async def cancel_all(self, message: str) -> None:
        """Deny all open escalations with `message` (their `ask` calls then return).

        The answers are marked as cancelled with the outcome "cancelled": no human decided, so a
        review after a step neither reverts anything nor reports a decision of the user.

        Side effects: cancels the rows, forgets the steps that `expect_ask` counted, and wakes
        the held calls.

        Args:
            message: The text Claude receives with the denial.
        """
        for esc in list(self.escalations.values()):
            if esc.answer is None:
                esc.answer = HumanAnswer(allow=False, message=message, cancelled=True)
                esc.outcome = "cancelled"
            esc.done.set()
            if esc.db_id is not None:
                await self._close_row(esc.db_id, message)
        self.escalations.clear()
        self._asking.clear()
        self._wake_held()

    async def turn_interrupted(self) -> None:
        """End what an interrupt of Claude's turn leaves without a caller.

        Side effects: forgets the steps that `expect_ask` counted, wakes the held calls, and
        ends the pending reviews of foreground subagent results with the outcome "interrupted"
        (their rows are cancelled). Other open escalations stay; their `ask` calls end when
        Claude Code cancels them.
        """
        # The interrupt cancels the hooks, so no `ask` follows for the verdicts they carried.
        self._asking.clear()
        self._wake_held()
        # Pending reviews of foreground subagent results belong to the interrupted turn; the
        # hooks that wait for them may not be cancelled by Claude Code, so they are ended here
        # (as cancelled: no note is sent to Claude). Background subagents keep running.
        for esc_id, esc in list(self.escalations.items()):
            if esc.step.tool_name == "SubagentResult" and not esc.step.tool_input.get("background"):
                self.escalations.pop(esc_id, None)
                esc.answer = HumanAnswer(
                    allow=False, message="The turn was interrupted.", cancelled=True
                )
                esc.outcome = "interrupted"
                esc.done.set()
                if esc.db_id is not None:
                    await self._close_row(esc.db_id, "the turn was interrupted")


def _render_safely(step: Step, workspace: Path) -> str:
    """Render `step` for an escalation; fall back to the raw input if rendering fails.

    Args:
        step: The tool call.
        workspace: Workspace root for relative paths.

    Returns:
        The Markdown rendering of the step.
    """
    try:
        return render_step(step, workspace)
    except Exception:
        log.exception("rendering a step failed")
        return f"`{step.tool_name}` {step.tool_input!r}"


def _questions(step: Step) -> list[dict[str, Any]]:
    """Return the questions of an `AskUserQuestion` call, for the client that shows them.

    Args:
        step: The tool call.

    Returns:
        The `questions` list as Claude sent it (each with its text, header, options and whether
        several may be chosen), or an empty list for any other tool.
    """
    if step.tool_name != "AskUserQuestion":
        return []
    questions = step.tool_input.get("questions")
    return (
        [question for question in questions if isinstance(question, dict)]
        if (isinstance(questions, list))
        else []
    )
