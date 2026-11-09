"""What a running session writes to the database (PLAN.md §7, REQUIREMENTS 11).

    calu run ──► SessionStore.start          sessions row, state "starting"
                 set_state                   every state change (PLAN.md §3.1)
                 add_step  ─► decide_step ─► finish_step        one row per tool call
                 add_checker_run             one row per checker model call
                 open_escalation ─► escalation ─► close_escalation
                 add_comment                 a user's comment on a selection
                 save_turn, save_summary     condensed conversation for the checkers
                 end                         state "stopped" or "failed", ended_at

`SessionStore` is the interface the session orchestrator uses. `DatabaseSessionStore` implements
it against PostgreSQL with the rights of the role `calu_session`; `MemorySessionStore`
(`calu.store.memory`) implements it without a database, for unit tests.

The session role may **not** allow or deny an escalation: only an operator may, through
`calu_resolve_escalation` (`calu resolve`, a web UI, SQL). The session waits for that resolution
and may only cancel its own open escalations, which counts as a denial. A decision that reaches
the session from an attached client is therefore recorded with `close_escalation`, which cancels
the row and keeps the decision in its note.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from calu.comments import Comment
from calu.context.turns import StepRecord, TurnRecord
from calu.db import Db
from calu.protocol.messages import SessionSummary, State

EscalationState = Literal["open", "allowed", "denied", "cancelled"]
"""States of an `escalations` row."""


@dataclass(frozen=True)
class SessionIdentity:
    """Where and by whom a session runs (`sessions` and `hosts` rows).

    `project` is the name of a `projects` row; a name that no row matches, and None, leave
    `sessions.project_id` empty.
    """

    hostname: str
    os_user: str
    directory: str
    pid: int
    os: str = ""
    git_commit: str | None = None
    project: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class StepRow:
    """A tool call as it is recorded when it arrives, before it is decided."""

    seq: int
    """Position of the step in the session, counted from 1."""
    tool_name: str
    tool_input: dict[str, Any]
    turn: int | None = None
    agent_id: str | None = None
    tool_use_id: str | None = None
    git_commit: str | None = None


@dataclass(frozen=True)
class StepDecision:
    """The outcome of checking one tool call.

    `decision` is "allow", "deny" or "escalate". `rules_outcome` says what the allow and deny
    lists found ("deny", "allow" or "unlisted"); `reason` and `summary` come from the checker or
    the rules, `narration` is Claude's thinking and text before the call in indirect speech.
    """

    decision: str
    reason: str = ""
    summary: str = ""
    narration: str = ""
    rules_outcome: str | None = None


@dataclass(frozen=True)
class CheckerRun:
    """One model call of a checker, for cost and quality statistics (`checker_runs`)."""

    checker_name: str
    checker_version: int
    model: str
    decision: str
    reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class Escalation:
    """The state of an escalation row, as the waiting session reads it."""

    id: int
    state: EscalationState
    note: str = ""
    resolved_by: str | None = None

    @property
    def open(self) -> bool:
        """Tell whether the escalation still waits for a decision.

        Returns:
            True while the state is "open".
        """
        return self.state == "open"

    @property
    def allowed(self) -> bool:
        """Tell whether an operator allowed the step.

        Returns:
            True only for state "allowed"; "denied" and "cancelled" are denials.
        """
        return self.state == "allowed"


class SessionStore(Protocol):
    """Where a session records what it does. See the module docstring.

    Every method may raise `psycopg.Error` (or, for the in-memory store, nothing). The session
    treats store errors as fatal for the session: without the database it cannot be watched or
    unblocked, so it stops.
    """

    session_id: str | None
    """Id of the `sessions` row, set by `start`."""

    async def start(self, identity: SessionIdentity) -> str:
        """Create the host and session rows; the session starts in state "starting".

        Args:
            identity: Where the session runs.

        Returns:
            The id of the new session row (also in `session_id`).
        """
        ...

    async def set_state(self, state: State) -> None:
        """Record the current state and the time it was entered.

        Args:
            state: The new state.
        """
        ...

    async def set_claude_session(self, claude_session_id: str, model: str | None = None) -> None:
        """Record Claude Code's own session id (and model) once it is known.

        Args:
            claude_session_id: The id Claude Code reports.
            model: The Claude model of the session, or None to leave it unchanged.
        """
        ...

    async def end(self, state: State) -> None:
        """Record the end of the session.

        Args:
            state: The final state, "stopped" or "failed".
        """
        ...

    async def add_step(self, step: StepRow) -> int:
        """Record a tool call that is about to be checked.

        Args:
            step: The tool call.

        Returns:
            The id of the new `steps` row.
        """
        ...

    async def decide_step(self, step_id: int, decision: StepDecision) -> None:
        """Record the decision about a step.

        Args:
            step_id: Id from `add_step`.
            decision: What was decided and why.
        """
        ...

    async def finish_step(self, step_id: int, result_summary: str = "") -> None:
        """Record that a step has run.

        Args:
            step_id: Id from `add_step`.
            result_summary: Short description of the result ("" if none).
        """
        ...

    async def add_checker_run(self, step_id: int, run: CheckerRun) -> None:
        """Record one checker model call.

        Args:
            step_id: Id from `add_step`.
            run: The call and its result.
        """
        ...

    async def save_turn(self, turn: TurnRecord) -> None:
        """Store a finished turn, replacing an earlier version of the same turn.

        Args:
            turn: The turn record; `turn.index` is its number in the session.
        """
        ...

    async def mark_folded(self, up_to_index: int) -> None:
        """Mark the turns up to `up_to_index` as folded into the chat summary.

        Args:
            up_to_index: Number of the newest folded turn.
        """
        ...

    async def save_summary(self, summary: str, covered_turns: int) -> None:
        """Store the chat summary of this session.

        Args:
            summary: The summary text.
            covered_turns: Number of the newest turn it covers.
        """
        ...

    async def load_previous(self, claude_session_id: str) -> tuple[list[TurnRecord], str]:
        """Load the context of an earlier calu session that ran the same Claude session.

        Args:
            claude_session_id: The Claude session id that is resumed.

        Returns:
            Its turns (oldest first, unfolded ones only) and its chat summary; `([], "")` if no
            other session ran that Claude session.
        """
        ...

    async def recent_sessions(self, directory: str, limit: int = 20) -> list[SessionSummary]:
        """List the stored sessions of one project directory, newest first.

        Args:
            directory: The project directory (`SessionIdentity.directory`).
            limit: Maximum number of sessions returned.

        Returns:
            The sessions, newest first, each with its first user input (for a client's "resume"
            list).
        """
        ...

    async def add_comment(self, comment: Comment, turn: int | None = None) -> int:
        """Record a comment that a user made on a selection and that Claude received.

        Args:
            comment: The comment as it was relayed to Claude.
            turn: Number of the turn it was sent in, or None.

        Returns:
            The id of the new `comments` row.
        """
        ...

    async def open_escalation(
        self, reason: str, context: dict[str, Any], step_id: int | None = None
    ) -> int:
        """Record an escalation that waits for a human.

        Args:
            reason: Why the step needs a decision.
            context: Everything an operator needs to decide (rendered step, checker output,
                recent turns); stored as JSON.
            step_id: Id of the step from `add_step`, or None if the escalation has no step.

        Returns:
            The id of the new `escalations` row.
        """
        ...

    async def escalation(self, escalation_id: int) -> Escalation:
        """Read the current state of an escalation.

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            The row's state, note and resolver.
        """
        ...

    async def close_escalation(self, escalation_id: int, note: str = "") -> None:
        """Cancel an escalation that is still open (a cancelled escalation is a denial).

        Does nothing if the escalation was already resolved.

        Args:
            escalation_id: Id from `open_escalation`.
            note: Why it was cancelled, e.g. a decision that reached the session from a client.
        """
        ...


class DatabaseSessionStore:
    """`SessionStore` on a PostgreSQL connection with the rights of `calu_session`.

    The connection is opened by `calu run` before the credentials are wiped and stays open for
    the whole session; the store does not close it. Steps of the main agent and of subagents are
    checked at the same time, so every statement goes through `Db`, which serializes them. All
    methods need `start` to have run, except `start` itself.
    """

    def __init__(self, db: Db) -> None:
        """Create a store on a shared connection.

        Args:
            db: The session's connection (`calu.db.Db`); several tasks may use it, and the
                store does not close it.
        """
        self._db = db
        self.session_id: str | None = None

    async def start(self, identity: SessionIdentity) -> str:
        """Create the host and session rows (see `SessionStore.start`).

        Args:
            identity: Where the session runs.

        Returns:
            The id of the new session row.

        Raises:
            psycopg.Error: On database errors.
            RuntimeError: If the server returns no row for an insert.
        """
        async with self._db.exclusive() as conn:
            cur = await conn.execute(
                b"INSERT INTO hosts (hostname, os) VALUES (%s, %s)"
                b" ON CONFLICT (hostname) DO UPDATE SET os = excluded.os, last_seen = now()"
                b" RETURNING id",
                (identity.hostname, identity.os),
            )
            host = await cur.fetchone()
            if host is None:
                raise RuntimeError("the hosts row was not created")
            project_id = None
            if identity.project is not None:
                cur = await conn.execute(
                    b"SELECT id FROM projects WHERE name = %s", (identity.project,)
                )
                row = await cur.fetchone()
                project_id = row[0] if row else None
            cur = await conn.execute(
                b"INSERT INTO sessions"
                b" (project_id, host_id, os_user, directory, git_commit, pid, model)"
                b" VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    project_id,
                    host[0],
                    identity.os_user,
                    identity.directory,
                    identity.git_commit,
                    identity.pid,
                    identity.model,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("the sessions row was not created")
        self.session_id = str(row[0])
        return self.session_id

    async def set_state(self, state: State) -> None:
        """Record the current state (see `SessionStore.set_state`).

        Args:
            state: The new state.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE sessions SET state = %s, state_since = now() WHERE id = %s",
            (state, self._require_session()),
        )

    async def set_claude_session(self, claude_session_id: str, model: str | None = None) -> None:
        """Record Claude Code's session id (see `SessionStore.set_claude_session`).

        Args:
            claude_session_id: The id Claude Code reports.
            model: The Claude model, or None to leave it unchanged.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE sessions SET claude_session_id = %s, model = coalesce(%s, model) WHERE id = %s",
            (claude_session_id, model, self._require_session()),
        )

    async def end(self, state: State) -> None:
        """Record the end of the session (see `SessionStore.end`).

        Args:
            state: The final state.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE sessions SET state = %s, state_since = now(), ended_at = now() WHERE id = %s",
            (state, self._require_session()),
        )

    async def add_step(self, step: StepRow) -> int:
        """Record a tool call (see `SessionStore.add_step`).

        Args:
            step: The tool call.

        Returns:
            The id of the new row.

        Raises:
            psycopg.Error: On database errors.
            RuntimeError: If the server returns no row for the insert.
        """
        return await self._db.scalar(
            "INSERT INTO steps"
            " (session_id, turn, seq, agent_id, tool_use_id, tool_name, tool_input, git_commit)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                self._require_session(),
                step.turn,
                step.seq,
                step.agent_id,
                step.tool_use_id,
                step.tool_name,
                json.dumps(step.tool_input, default=str),
                step.git_commit,
            ),
        )

    async def decide_step(self, step_id: int, decision: StepDecision) -> None:
        """Record the decision about a step (see `SessionStore.decide_step`).

        Args:
            step_id: Id from `add_step`.
            decision: What was decided and why.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE steps SET rules_outcome = %s, decision = %s, reason = %s, summary = %s,"
            " narration = %s, decided_at = now() WHERE id = %s",
            (
                decision.rules_outcome,
                decision.decision,
                decision.reason,
                decision.summary,
                decision.narration,
                step_id,
            ),
        )

    async def finish_step(self, step_id: int, result_summary: str = "") -> None:
        """Record that a step has run (see `SessionStore.finish_step`).

        Args:
            step_id: Id from `add_step`.
            result_summary: Short description of the result.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE steps SET result_summary = %s, finished_at = now() WHERE id = %s",
            (result_summary, step_id),
        )

    async def add_checker_run(self, step_id: int, run: CheckerRun) -> None:
        """Record one checker model call (see `SessionStore.add_checker_run`).

        Args:
            step_id: Id from `add_step`.
            run: The call and its result.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "INSERT INTO checker_runs (step_id, checker_name, checker_version, model, decision,"
            " reason, input_tokens, output_tokens, cache_read_tokens, duration_ms)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                step_id,
                run.checker_name,
                run.checker_version,
                run.model,
                run.decision,
                run.reason,
                run.input_tokens,
                run.output_tokens,
                run.cache_read_tokens,
                run.duration_ms,
            ),
        )

    async def save_turn(self, turn: TurnRecord) -> None:
        """Store a finished turn (see `SessionStore.save_turn`).

        Args:
            turn: The turn record.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "INSERT INTO turns (session_id, number, user_inputs, steps, answer, narrated_answer,"
            " narrated, started_at, ended_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (session_id, number) DO UPDATE SET"
            " user_inputs = excluded.user_inputs, steps = excluded.steps,"
            " answer = excluded.answer, narrated_answer = excluded.narrated_answer,"
            " narrated = excluded.narrated, ended_at = excluded.ended_at",
            (
                self._require_session(),
                turn.index,
                json.dumps(turn.user_inputs),
                json.dumps([s.model_dump(mode="json") for s in turn.steps]),
                turn.answer,
                turn.answer_narration,
                turn.narrated,
                turn.started_at,
                turn.ended_at,
            ),
        )

    async def mark_folded(self, up_to_index: int) -> None:
        """Mark turns as folded (see `SessionStore.mark_folded`).

        Args:
            up_to_index: Number of the newest folded turn.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE turns SET folded = true WHERE session_id = %s AND number <= %s",
            (self._require_session(), up_to_index),
        )

    async def save_summary(self, summary: str, covered_turns: int) -> None:
        """Store the chat summary (see `SessionStore.save_summary`).

        Args:
            summary: The summary text.
            covered_turns: Number of the newest turn it covers.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "INSERT INTO chat_summaries (session_id, summary, covered_turns)"
            " VALUES (%s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET"
            " summary = excluded.summary, covered_turns = excluded.covered_turns,"
            " updated_at = now()",
            (self._require_session(), summary, covered_turns),
        )

    async def load_previous(self, claude_session_id: str) -> tuple[list[TurnRecord], str]:
        """Load turns and summary of the last session of a Claude session (see `SessionStore`).

        Args:
            claude_session_id: The Claude session id that is resumed.

        Returns:
            Its unfolded turns (oldest first) and its chat summary.

        Raises:
            psycopg.Error: On database errors.
        """
        async with self._db.exclusive() as conn:
            cur = await conn.execute(
                b"SELECT id FROM sessions WHERE claude_session_id = %s"
                b" AND id IS DISTINCT FROM %s::uuid ORDER BY started_at DESC LIMIT 1",
                (claude_session_id, self.session_id),
            )
            row = await cur.fetchone()
            if row is None:
                return [], ""
            previous = row[0]
            cur = await conn.execute(
                b"SELECT number, user_inputs, steps, answer, narrated_answer, narrated,"
                b" started_at, ended_at FROM turns WHERE session_id = %s AND NOT folded"
                b" ORDER BY number",
                (previous,),
            )
            turns: list[TurnRecord] = []
            for row in await cur.fetchall():
                number, user_inputs, steps, answer, narration, narrated, started, ended = row
                turns.append(
                    TurnRecord(
                        index=number,
                        user_inputs=list(user_inputs),
                        steps=[StepRecord.model_validate(s) for s in steps],
                        answer=answer,
                        answer_narration=narration or "",
                        narrated=narrated,
                        **({"started_at": started} if started else {}),
                        ended_at=ended,
                    )
                )
            cur = await conn.execute(
                b"SELECT summary FROM chat_summaries WHERE session_id = %s", (previous,)
            )
            row = await cur.fetchone()
        return turns, row[0] if row else ""

    async def recent_sessions(self, directory: str, limit: int = 20) -> list[SessionSummary]:
        """List the stored sessions of one directory (see `SessionStore.recent_sessions`).

        Args:
            directory: The project directory.
            limit: Maximum number of sessions returned.

        Returns:
            The sessions, newest first.

        Raises:
            psycopg.Error: On database errors.
        """
        rows = await self._db.all(
            "SELECT s.id::text, s.claude_session_id, s.state, s.directory, s.started_at,"
            " s.ended_at, coalesce((SELECT t.user_inputs->>0 FROM turns t"
            "   WHERE t.session_id = s.id ORDER BY t.number LIMIT 1), '')"
            " FROM sessions s WHERE s.directory = %s ORDER BY s.started_at DESC LIMIT %s",
            (directory, limit),
        )
        return [
            SessionSummary(
                calu_session_id=row[0],
                claude_session_id=row[1],
                state=row[2],
                directory=row[3],
                started_at=row[4],
                ended_at=row[5],
                first_input=(row[6] or "")[:200],
            )
            for row in rows
        ]

    async def add_comment(self, comment: Comment, turn: int | None = None) -> int:
        """Record a user comment (see `SessionStore.add_comment`).

        Args:
            comment: The comment as it was relayed to Claude.
            turn: Number of the turn it was sent in, or None.

        Returns:
            The id of the new row.

        Raises:
            psycopg.Error: On database errors.
            RuntimeError: If the server returns no row for the insert.
        """
        return await self._db.scalar(
            "INSERT INTO comments (session_id, turn, kind, path, start_line, end_line, ref,"
            " comment, content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                self._require_session(),
                turn,
                comment.kind,
                comment.path,
                comment.after.start_line,
                comment.after.end_line,
                comment.ref,
                comment.text,
                comment.model_dump_json(),
            ),
        )

    async def open_escalation(
        self, reason: str, context: dict[str, Any], step_id: int | None = None
    ) -> int:
        """Record an escalation (see `SessionStore.open_escalation`).

        Args:
            reason: Why the step needs a decision.
            context: What an operator needs to decide, stored as JSON.
            step_id: Id of the step, or None.

        Returns:
            The id of the new row.

        Raises:
            psycopg.Error: On database errors.
            RuntimeError: If the server returns no row for the insert.
        """
        return await self._db.scalar(
            "INSERT INTO escalations (session_id, step_id, reason, context)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (self._require_session(), step_id, reason, json.dumps(context, default=str)),
        )

    async def escalation(self, escalation_id: int) -> Escalation:
        """Read the state of an escalation (see `SessionStore.escalation`).

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            The row's state, note and resolver; state "cancelled" if the row is gone.

        Raises:
            psycopg.Error: On database errors.
        """
        row = await self._db.one(
            "SELECT state, note, resolved_by FROM escalations WHERE id = %s", (escalation_id,)
        )
        if row is None:
            return Escalation(id=escalation_id, state="cancelled")
        state, note, resolved_by = row
        return Escalation(id=escalation_id, state=state, note=note or "", resolved_by=resolved_by)

    async def close_escalation(self, escalation_id: int, note: str = "") -> None:
        """Cancel an open escalation (see `SessionStore.close_escalation`).

        Args:
            escalation_id: Id from `open_escalation`.
            note: Why it was cancelled.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "SELECT calu_cancel_escalation(%s, %s)", (escalation_id, note or None)
        )

    # ------------------------------------------------------------------ helpers

    def _require_session(self) -> str:
        """Return the session row id.

        Returns:
            The id set by `start`.

        Raises:
            RuntimeError: If `start` has not run.
        """
        if self.session_id is None:
            raise RuntimeError("the session store was used before start()")
        return self.session_id
