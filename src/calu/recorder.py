"""What a session writes to its store, and what happens when a write fails (`Recorder`).

`Recorder` is the component of `calu.session.Session` through which the session writes to the
`SessionStore`. The session and its other components hand it the awaitable of a store method;
it awaits the call, reports a failure to the clients and counts it:

    caller ── attempt(what, store.method(...)) ──► await the call
                                                     │ ok: the count of failures in a row = 0
                                                     │ exception ──► failed(what, exc)
                                                     ▼
                              `log` error for the clients, count + 1
                                                     │ count reaches the configured limit
                                                     ▼
                              a second `log` error, and the session is stopped (once)

Guarantees:

- `attempt`, `attempt_value`, `failed` and `add_step` never raise because the store failed. A
  step is checked also when its row could not be written; only its history is lost then.
- Every failed write is reported as a `log` error and counted. Only a write that succeeds
  resets the count (PLAN.md D59).
- The stop after too many failures is started once and in a task of its own, because the
  failed write runs inside a call that must not wait for the stop.

The recorder also builds the rows that need more than the caller's arguments: the `steps` row
of a tool call (`add_step`, which also notes when the step started, for `step_times`) and the
`checker_runs` row of one checker call (`checker_run`, `add_checker_run`).

Where the state is: the count of failures, the mark that a stop was started, the step times and
the row ids of single calls are fields of the session's `calu.run_state.RunState`, which the
recorder gets through a callable, because `Session.start` replaces the object. The recorder
holds no reference to the session.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from calu.checkers import Checker, CheckOutcome, Step
from calu.checkers.tools import StepTime
from calu.protocol.messages import Event, Log
from calu.run_state import RunState
from calu.store import CheckerDefinition, CheckerRun, SessionStore
from calu.store import StepRow as StoredStep

log = logging.getLogger(__name__)


class Recorder:
    """The store writes of one session, with their failure handling. See the module docstring.

    A `Recorder` lives as long as its `Session` object. It keeps no state of its own: what it
    counts and remembers is in the `RunState` that `run` returns.
    """

    def __init__(
        self,
        store: SessionStore,
        run: Callable[[], RunState],
        *,
        emit: Callable[[Event], Awaitable[None]],
        failure_limit: Callable[[], int],
        checker_types: Callable[[], Mapping[str, CheckerDefinition]],
        turn_number: Callable[[], int],
        git_head: Callable[[], Awaitable[str | None]],
        stop_session: Callable[[], None],
    ) -> None:
        """Remember the store and the providers; nothing is written yet.

        Args:
            store: Where the session records what it does.
            run: Returns the state of the current run at the moment of the call (the session
                replaces the object at every start).
            emit: Sends an event to the clients and never raises.
            failure_limit: Returns `config.store_failures_before_stop` at the moment of the
                call: the number of failed writes in a row that stops the session; 0 or less
                never stops it.
            checker_types: Returns the checker definitions from the database by type name, for
                the version in a `checker_runs` row.
            turn_number: Returns the number of the current turn, for a `steps` row.
            git_head: Returns the commit the project directory is at, or None if unknown.
            stop_session: Starts stopping the session in a task of its own and returns at once;
                does nothing if no run has a task group.
        """
        self._store = store
        self._run = run
        self._emit = emit
        self._failure_limit = failure_limit
        self._checker_types = checker_types
        self._turn_number = turn_number
        self._git_head = git_head
        self._stop_session = stop_session

    # ------------------------------------------------------------------ writes and failures

    async def attempt(self, what: str, call: Any) -> None:
        """Await a store call and report its failure as a `log` error; never raises.

        Args:
            what: What was attempted, for the message (e.g. "storing a turn").
            call: The awaitable returned by the store method.

        Side effects:
            Counts the failure (`failed`), which stops the session once writes have
            failed `config.store_failures_before_stop` times in a row (PLAN.md D59).
        """
        try:
            await call
        except Exception as exc:
            await self.failed(what, exc)
        else:
            self._run().store_failures = 0

    async def attempt_value[T](self, what: str, call: Awaitable[T]) -> T | None:
        """Await a store call that has a result, and report its failure; never raises.

        Like `attempt`, but for a read or a write whose return value the caller needs.

        Args:
            what: What was attempted, for the message.
            call: The awaitable returned by the store method.

        Returns:
            What the call returned, or None if it failed.

        Side effects:
            Counts the failure (`failed`) exactly as `attempt` does, so a store that
            keeps failing stops the session (D59).
        """
        try:
            value = await call
        except Exception as exc:
            await self.failed(what, exc)
            return None
        self._run().store_failures = 0
        return value

    async def failed(self, what: str, exc: BaseException) -> None:
        """Report a failed database write and stop the session if they keep failing.

        A session that cannot write is one nobody can watch or unblock: its steps and its
        escalations do not reach the database, and the fleet shows it as gone (D56). A single
        failure is tolerated, because a network hiccup must not end an overnight run; the
        `config.store_failures_before_stop`-th in a row stops the session, which gives the
        machine back and leaves the conversation resumable.

        Args:
            what: What was attempted, for the message.
            exc: Why it failed.

        Side effects:
            Emits a `log` error; past the limit, emits another and starts the stop in its own
            task (this runs inside whatever call failed, so it must not wait for the stop).
        """
        log.exception("%s failed", what, exc_info=exc)
        self._run().store_failures += 1
        await self._emit(Log(level="error", message=f"{what} failed: {exc}"))
        limit = self._failure_limit()
        if limit <= 0 or self._run().store_failures < limit or self._run().stopping:
            return
        self._run().stopping = True
        resume = (
            f"calu run --resume {self._run().session_id}"
            if self._run().session_id
            else "calu sessions"
        )
        await self._emit(
            Log(
                level="error",
                message=(
                    f"{self._run().store_failures} database writes failed in a row, so "
                    f"nobody can watch or unblock this session: it stops. Take it up again with "
                    f"`{resume}` once the database answers."
                ),
            )
        )
        self._stop_session()

    def succeeded(self) -> None:
        """Note that a database write succeeded: the count of failures in a row starts again."""
        self._run().store_failures = 0

    # ------------------------------------------------------------------ steps

    async def add_step(self, step: Step) -> int | None:
        """Record a tool call in the database before it is checked.

        Args:
            step: The tool call.

        Returns:
            The id of the `steps` row, or None if it could not be stored (then the step is
            still checked; only its history is lost).
        """
        row = StoredStep(
            seq=next(self._run().step_seq),
            tool_name=step.tool_name,
            tool_input=step.tool_input,
            turn=self._turn_number(),
            agent_id=step.agent_id,
            tool_use_id=step.tool_use_id,
            git_commit=await self._git_head(),
        )
        try:
            step_id = await self._store.add_step(row)
        except Exception as exc:
            await self.failed("recording a step in the database", exc)
            return None
        self._run().store_failures = 0
        self._run().times.append(
            StepTime(
                seq=row.seq,
                tool_name=step.tool_name,
                started_at=datetime.now(UTC),
                agent_id=step.agent_id,
            )
        )
        if step.tool_use_id:
            call = self._run().call(step.tool_use_id)
            call.time_seq = row.seq
            call.step_id = step_id
        return step_id

    def step_row(self, step: Step) -> int | None:
        """Return the id of the `steps` row of a tool call.

        Args:
            step: The tool call.

        Returns:
            The id, or None if the row could not be stored or the call has ended.
        """
        return self._run().peek(step.tool_use_id or "").step_id

    # ------------------------------------------------------------------ checker runs

    def version(self, checker: str) -> int:
        """Return the stored version of a checker definition.

        Args:
            checker: Checker type name.

        Returns:
            The version from the database, or 0 if the definition is built in.
        """
        definition = self._checker_types().get(checker)
        return definition.version if definition else 0

    def checker_run(self, checker: Checker, route: str, outcome: CheckOutcome) -> CheckerRun:
        """Build the `checker_runs` row of one step check.

        The row is keyed by the **checker's own name**, not by its route: a route may carry
        several checkers (PLAN.md D81), and a row named after the route would merge them, so
        `calu checker stats` and `calu checker misses` could no longer tell which of them
        decided or what each one costs.

        Args:
            checker: The checker that ran; its name and version go into the row.
            route: Its route value, kept for the caller's own bookkeeping.
            outcome: What the call returned and cost.

        Returns:
            The row.
        """
        return CheckerRun(
            checker_name=checker.name,
            checker_version=self.version(checker.name),
            model=model_name(checker.model),
            decision=outcome.decision.decision.value.lower(),
            reason=outcome.decision.reason,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            cache_read_tokens=outcome.cache_read_tokens,
            duration_ms=outcome.duration_ms,
        )

    async def add_checker_run(
        self, what: str, step_id: int, checker: Checker, route: str, outcome: CheckOutcome
    ) -> None:
        """Record one checker call of a step in `checker_runs`; never raises.

        Side effects: one store write through `attempt`, so a failure is reported and counted.

        Args:
            what: What is attempted, for the message of a failure (e.g. "recording a checker
                run").
            step_id: The `steps` row the run belongs to.
            checker: The checker that ran.
            route: Its route value (see `checker_run`).
            outcome: What the call returned and cost.
        """
        await self.attempt(
            what, self._store.add_checker_run(step_id, self.checker_run(checker, route, outcome))
        )

    # ------------------------------------------------------------------ step times

    def step_times(self) -> Sequence[StepTime]:
        """Return when the recent steps of this session ran, oldest first (PLAN.md D32).

        The checkers get this as a tool (`calu.checkers.tools.step_times`) and use it to
        attribute a file's change time to a step. Only the last `STEP_TIMES_KEPT` steps are
        kept, because the list exists for attributing recent changes, not as a history: the
        database holds the history.

        Returns:
            The steps, oldest first; empty before the first tool call.
        """
        return tuple(self._run().times)

    def finish_time(self, tool_use_id: str) -> None:
        """Note that a tool call ended, so its time window is closed.

        Args:
            tool_use_id: Claude's id of the call; an unknown id is ignored (its step may have
                fallen out of the kept window).
        """
        call = self._run().peek(tool_use_id)
        seq, call.time_seq = call.time_seq, None
        self._run().release(tool_use_id)
        if seq is None:
            return
        for index, step in enumerate(self._run().times):
            if step.seq == seq:
                self._run().times[index] = replace(step, finished_at=datetime.now(UTC))
                return

    def summarize_time(self, seq: int, summary: str) -> None:
        """Attach the checker's summary to a recorded step time, for `step_times`.

        Args:
            seq: Sequence number of the step.
            summary: What the checker said the step does.
        """
        if not summary:
            return
        for index, step in enumerate(self._run().times):
            if step.seq == seq:
                self._run().times[index] = replace(step, summary=summary)
                return


def model_name(model: object) -> str:
    """Return a printable name of a checker model.

    Args:
        model: A Pydantic AI model string or `Model` instance.

    Returns:
        The string itself, or the instance's `model_name`.
    """
    return model if isinstance(model, str) else str(getattr(model, "model_name", model))
