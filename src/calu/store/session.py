"""What a running session writes to the database (PLAN.md §7, REQUIREMENTS 11).

    calu run ──► SessionStore.start          sessions row, state "starting"
                 set_state                   every state change (PLAN.md §3.1)
                 add_step  ─► decide_step ─► finish_step        one row per tool call
                 add_checker_run             one row per checker model call
                 open_escalation ─► escalation ─► close_escalation
                 add_comment                 a user's comment on a selection
                 save_turn, save_summary     condensed conversation for the checkers
                 set_address, add_agent, update_agents
                                             how to reach the process, and its agent tree
                 end                         state "stopped" or "failed", ended_at

`SessionStore` is the interface the session orchestrator uses. `DatabaseSessionStore` implements
it against PostgreSQL with the rights of the role `calu_session`; `MemorySessionStore`
(`calu.store.memory`) implements it without a database, for unit tests.

Escalations can be resolved from two sides (PLAN.md D28):

- an **operator** resolves any escalation in the database (`calu_resolve_escalation`, used by
  `calu resolve` and by a herd interface with its own database login); the waiting session
  notices it through `escalation_resolved` or its poll;
- a **client of the session** — `calu attach`, the ACP adapter, an IDE panel — has no database
  login and tells the session, which records the decision with `decide_escalation`
  (`calu_client_decision`). That function resolves only an escalation of this session, requires
  the hash of one of its client tokens, and records in `resolved_by` that a client decided.

`close_escalation` stays for a genuine cancellation: the session stopped, the turn was
interrupted, or nobody answered in time.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import psycopg

from calu.comments import Comment
from calu.context.turns import StepRecord, TurnRecord
from calu.db import Db
from calu.protocol.messages import (
    AgentInfo,
    CheckerRunInfo,
    EscalationContextResult,
    SessionHistoryResult,
    SessionSummary,
    State,
    StoredStep,
)

EscalationState = Literal["open", "allowed", "denied", "answered", "cancelled"]
"""States of an `escalations` row. "answered" belongs to a question of the agent: the step
neither ran nor was refused, and the note holds the answer (PLAN.md D35, migration 0011)."""


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
    def answered(self) -> bool:
        """Tell whether this was a question of the agent that a human answered.

        Returns:
            True if the state is "answered"; `note` then holds the answer (D35).
        """
        return self.state == "answered"

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
    reports a failed call as a `log` error and counts it: a single failure is tolerated, because
    a network hiccup must not end an overnight run, and
    `config.store_failures_before_stop` failures in a row stop the session, because one nobody
    can watch or unblock should give the machine back (PLAN.md D59). The count resets on the
    next call that works.
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

    async def set_address(self, address: str) -> None:
        """Record where this session process listens for clients (PLAN.md D29).

        Args:
            address: "ws://host:port" or a Unix socket path.
        """
        ...

    async def add_agent(self, agent_id: str, parent_agent_id: str | None = None) -> None:
        """Record a subagent of this session and which agent started it.

        Called when the Agent tool call that starts the subagent is seen, so the hierarchy is
        known before the subagent's own steps arrive. Calling it again for the same agent keeps
        the first parent.

        Args:
            agent_id: Id of the Agent tool call that starts the subagent.
            parent_agent_id: The agent that made that call; None means the main agent.
        """
        ...

    async def update_agents(self, agents: Sequence[AgentInfo]) -> None:
        """Record what is known about the subagents of this session.

        Args:
            agents: The session's subagents as the runner reports them; a status other than
                "running" also sets `ended_at`.
        """
        ...

    async def note_setting_change(
        self, setting: str, old: dict[str, Any], new: dict[str, Any], actor: str
    ) -> None:
        """Record that a setting of this running session changed, and who asked for it.

        For the audit log, not for the session's own behaviour: `auto_escalate.set` is the one
        setting a client may change through the protocol (PLAN.md D42), and switching it off
        weakens the session, so an operator reading the history afterwards must be able to see
        it (D43). Changes of the project's configuration are logged by database triggers
        instead; this is for the ones that live in the process.

        Args:
            setting: Which setting changed, e.g. "auto_escalate".
            old: Its value before the change, as a JSON object.
            new: Its value after the change, as a JSON object.
            actor: Who asked for it: the name of the client, or "session" when a reloaded
                configuration brought the change.
        """
        ...

    async def heartbeat(self) -> None:
        """Record that this session's process is still running (PLAN.md D56).

        Called on an interval while the session lives. A reader compares `sessions.last_seen`
        with the current time to tell a working session from one whose process is gone: nothing
        else distinguishes them, because a killed process cannot set its own state.

        Errors are the caller's to handle; a session whose heartbeat fails is still a session,
        so `calu run` logs the failure rather than stopping.
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

    async def record_changes(
        self,
        step_id: int,
        before: str,
        after: str,
        paths: Sequence[str],
        undeclared: Sequence[str] = (),
        overlapping: bool = False,
    ) -> None:
        """Record what a step wrote, as the optional snapshot measured it (PLAN.md D31).

        Args:
            step_id: Id from `add_step`.
            before: Git tree of the project directory before the step.
            after: Git tree after it.
            paths: The paths that changed, project-relative.
            undeclared: Those of them that the checker's summary did not mention — the honesty
                signal, which is a hint for a human, not a verdict.
            overlapping: True if another tool call ran inside the same window, so the paths
                cannot be attributed to this step alone.
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

    async def load_summary(self) -> str:
        """Read the chat summary of this session back from the database.

        A session reads it again after a `config_changed` notification, because an operator may
        have corrected it (PLAN.md D40): the summary is part of what steers the checkers.

        Returns:
            The stored summary, or "" if this session has none.
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

    async def add_client_token(self, token_sha256: str, address: str) -> int:
        """Record the hash of a client token of this session (PLAN.md §5).

        The token itself is never stored: a client sends it, and the session compares the hash.

        Args:
            token_sha256: SHA-256 of the token (`calu.tokens.token_hash`).
            address: The address clients connect to, e.g. "ws://127.0.0.1:8765".

        Returns:
            The id of the new `client_tokens` row.
        """
        ...

    async def client_token_ok(self, token_sha256: str) -> bool:
        """Tell whether a hash belongs to a non-revoked client token of this session (D30).

        A session accepts every token an operator minted for it, not only the one it printed,
        so that a stored token (an editor's ACP adapter, a local command, a remote interface)
        survives a restart of the session and can be revoked on its own.

        Args:
            token_sha256: SHA-256 of the token a client sent (`calu.tokens.token_hash`).

        Returns:
            True if this session has a non-revoked token with that hash.
        """
        ...

    async def load_history(
        self, claude_session_id: str | None = None, limit: int = 500
    ) -> SessionHistoryResult:
        """Read the recorded conversation of a session, for a client that joins late.

        Args:
            claude_session_id: Which session; None is this one.
            limit: Maximum number of steps returned (the newest ones).

        Returns:
            Its turns, steps and chat summary, oldest first; empty fields for a session that
            the database does not know.
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

        Does nothing if the escalation was already resolved. A cancellation is not a decision:
        it records that nobody decided (the session stopped, the turn was interrupted, the wait
        timed out).

        Args:
            escalation_id: Id from `open_escalation`.
            note: Why it was cancelled.
        """
        ...

    async def decide_escalation(
        self,
        escalation_id: int,
        allow: bool,
        note: str = "",
        actor: str = "",
        answered: bool = False,
    ) -> bool:
        """Record the decision of a client of this session (PLAN.md D28).

        Args:
            escalation_id: Id from `open_escalation`.
            allow: Whether the step may run.
            note: The human's note, which Claude receives with a denial, or the answer to a
                question of the agent.
            actor: Name of the client that decided, for the record.
            answered: True if this was a question of the agent and `note` holds its answer
                (PLAN.md D35); the row is then recorded as "answered", because the step neither
                ran nor was refused.

        Returns:
            True if the escalation was open and is now resolved; False if it was already
            resolved or this session may not decide it.
        """
        ...

    async def escalation_context(self, escalation_id: int) -> EscalationContextResult | None:
        """Read what the checker saw and said when it escalated a step.

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            The stored context with the checker runs of the step, or None if no escalation has
            that id.
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
        self._token_hash: str | None = None

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
            # Through the function, not with a plain INSERT: under the row-level security of
            # migration 0015 a session may only touch rows that carry its own id, and at this
            # moment that id does not exist yet. The function runs as the owner and hands it
            # back (PLAN.md D49).
            cur = await conn.execute(
                b"SELECT calu_start_session(%s, %s, %s, %s, %s, %s, %s)",
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
            if row is None or row[0] is None:
                raise RuntimeError("the sessions row was not created")
            # From here on this connection may read and write this session's rows and no
            # others. `false` makes the setting last for the connection, not the transaction.
            await conn.execute(b"SELECT set_config('calu.session_id', %s, false)", (str(row[0]),))
        self.session_id = str(row[0])
        return self.session_id

    async def heartbeat(self) -> None:
        """Stamp `last_seen` on this session's row (see `SessionStore.heartbeat`).

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE sessions SET last_seen = now() WHERE id = %s", (self._require_session(),)
        )

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

    async def note_setting_change(
        self, setting: str, old: dict[str, Any], new: dict[str, Any], actor: str
    ) -> None:
        """Write an audit entry for a changed session setting (see `SessionStore`).

        Args:
            setting: Which setting changed, e.g. "auto_escalate".
            old: Its value before the change.
            new: Its value after the change.
            actor: Who asked for it.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "SELECT calu_session_setting_change(%s::uuid, %s, %s::jsonb, %s::jsonb, %s)",
            (self._require_session(), setting, json.dumps(old), json.dumps(new), actor),
        )

    async def set_address(self, address: str) -> None:
        """Record where the session listens (see `SessionStore.set_address`).

        Args:
            address: "ws://host:port" or a Unix socket path.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE sessions SET address = %s WHERE id = %s", (address, self._require_session())
        )

    async def add_agent(self, agent_id: str, parent_agent_id: str | None = None) -> None:
        """Record a subagent and its parent (see `SessionStore.add_agent`).

        Args:
            agent_id: Id of the Agent tool call that starts the subagent.
            parent_agent_id: The agent that made that call; None means the main agent.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "INSERT INTO agents (session_id, agent_id, parent_agent_id) VALUES (%s, %s, %s)"
            " ON CONFLICT (session_id, agent_id) DO NOTHING",
            (self._require_session(), agent_id, parent_agent_id),
        )

    async def update_agents(self, agents: Sequence[AgentInfo]) -> None:
        """Record what is known about the subagents (see `SessionStore.update_agents`).

        Args:
            agents: The session's subagents as the runner reports them.

        Raises:
            psycopg.Error: On database errors.
        """
        session_id = self._require_session()
        async with self._db.exclusive() as conn:
            for agent in agents:
                await conn.execute(
                    b"INSERT INTO agents (session_id, agent_id, sdk_agent_id, description,"
                    b" subagent_type, background, status, ended_at)"
                    b" VALUES (%s, %s, %s, %s, %s, %s, %s,"
                    b"         CASE WHEN %s = 'running' THEN NULL ELSE now() END)"
                    b" ON CONFLICT (session_id, agent_id) DO UPDATE SET"
                    b" sdk_agent_id = excluded.sdk_agent_id,"
                    b" description = excluded.description,"
                    b" subagent_type = excluded.subagent_type,"
                    b" background = excluded.background,"
                    b" status = excluded.status,"
                    b" ended_at = coalesce(agents.ended_at, excluded.ended_at)",
                    (
                        session_id,
                        agent.agent_id,
                        agent.sdk_agent_id,
                        agent.description,
                        agent.subagent_type,
                        agent.background,
                        agent.status,
                        agent.status,
                    ),
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

    async def record_changes(
        self,
        step_id: int,
        before: str,
        after: str,
        paths: Sequence[str],
        undeclared: Sequence[str] = (),
        overlapping: bool = False,
    ) -> None:
        """Record a step's write set (see `SessionStore.record_changes`).

        Args:
            step_id: Id from `add_step`.
            before: Git tree before the step.
            after: Git tree after it.
            paths: The paths that changed.
            undeclared: The paths the checker's summary did not mention.
            overlapping: Whether another call ran in the same window.

        Raises:
            psycopg.Error: On database errors.
        """
        await self._db.execute(
            "UPDATE steps SET snapshot_before = %s, snapshot_after = %s, wrote = %s WHERE id = %s",
            (
                before,
                after,
                json.dumps(
                    {
                        "paths": list(paths),
                        "undeclared": list(undeclared),
                        "overlapping": overlapping,
                    }
                ),
                step_id,
            ),
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
            " narrated, started_at, ended_at, prompt_uuid, last_entry_uuid)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (session_id, number) DO UPDATE SET"
            " user_inputs = excluded.user_inputs, steps = excluded.steps,"
            " answer = excluded.answer, narrated_answer = excluded.narrated_answer,"
            " narrated = excluded.narrated, ended_at = excluded.ended_at,"
            " prompt_uuid = excluded.prompt_uuid, last_entry_uuid = excluded.last_entry_uuid",
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
                turn.prompt_uuid or None,
                turn.last_entry_uuid or None,
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

    async def load_summary(self) -> str:
        """Read this session's chat summary (see `SessionStore.load_summary`).

        Returns:
            The stored summary, or "" if there is none.

        Raises:
            psycopg.Error: On database errors.
        """
        row = await self._db.one(
            "SELECT summary FROM chat_summaries WHERE session_id = %s::uuid",
            (self._require_session(),),
        )
        return str(row[0]) if row and row[0] else ""

    async def _claim_conversation(
        self, conn: psycopg.AsyncConnection, claude_session_id: str
    ) -> None:
        """Record on this session's row which conversation it reads, if it has none yet.

        Reading a conversation spans the `sessions` rows of its earlier runs (a resume or a
        fork continues it under a new row), and the row-level security of migration 0015 lets a
        session read only the rows of sessions that share its own Claude session id. A session
        that is about to read a conversation therefore says which one, by writing that id to
        its own row -- which is both the key to the policy and a record an operator can see: a
        session that claimed a conversation it has no business with stands in the database next
        to the real one, with a different directory.

        An id that is already recorded is left alone, so this never overwrites what Claude Code
        reported.

        Args:
            conn: The connection to use (the caller holds the lock).
            claude_session_id: The conversation about to be read.

        Raises:
            psycopg.Error: On database errors.
        """
        if self.session_id is None:
            return
        await conn.execute(
            b"UPDATE sessions SET claude_session_id = %s"
            b" WHERE id = %s::uuid AND claude_session_id IS NULL",
            (claude_session_id, self.session_id),
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
            # Through the function: the sibling rows of one conversation are what row-level
            # security keeps this role out of (PLAN.md D49).
            await self._claim_conversation(conn, claude_session_id)
            cur = await conn.execute(
                b"SELECT id FROM calu_conversation_sessions(%s)"
                b" WHERE id IS DISTINCT FROM %s::uuid LIMIT 1",
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
            turns = [turn_record(row) for row in await cur.fetchall()]
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
        # Through the function: this spans other sessions' rows, which row-level security
        # keeps this role out of, and the function returns their metadata and first prompt and
        # nothing else of them (PLAN.md D49).
        rows = await self._db.all(
            "SELECT id, claude_session_id, state, directory, started_at, ended_at, first_input"
            " FROM calu_recent_sessions(%s, %s)",
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

    async def load_history(
        self, claude_session_id: str | None = None, limit: int = 500
    ) -> SessionHistoryResult:
        """Read the recorded conversation of a session (see `SessionStore.load_history`).

        Args:
            claude_session_id: Which session; None is this one.
            limit: Maximum number of steps returned.

        Returns:
            The turns, the steps and the chat summary, oldest first.

        Raises:
            psycopg.Error: On database errors.
        """
        async with self._db.exclusive() as conn:
            if claude_session_id is None:
                session_id = self.session_id
            else:
                await self._claim_conversation(conn, claude_session_id)
                cur = await conn.execute(
                    b"SELECT id FROM calu_conversation_sessions(%s) LIMIT 1",
                    (claude_session_id,),
                )
                row = await cur.fetchone()
                session_id = str(row[0]) if row else None
            if session_id is None:
                return SessionHistoryResult(claude_session_id=claude_session_id)
            cur = await conn.execute(
                b"SELECT claude_session_id FROM sessions WHERE id = %s::uuid", (session_id,)
            )
            row = await cur.fetchone()
            claude_id = row[0] if row else None
            cur = await conn.execute(
                b"SELECT number, user_inputs, steps, answer, narrated_answer, narrated,"
                b" started_at, ended_at FROM turns WHERE session_id = %s::uuid ORDER BY number",
                (session_id,),
            )
            turns = [turn_record(row) for row in await cur.fetchall()]
            cur = await conn.execute(
                b"SELECT seq, turn, agent_id, tool_use_id, tool_name, tool_input, decision,"
                b" reason, summary, result_summary, created_at FROM steps"
                b" WHERE session_id = %s::uuid ORDER BY seq DESC LIMIT %s",
                (session_id, limit),
            )
            steps = [
                StoredStep(
                    seq=row[0],
                    turn=row[1],
                    agent_id=row[2],
                    tool_use_id=row[3],
                    tool_name=row[4],
                    tool_input=dict(row[5] or {}),
                    decision=row[6],
                    reason=row[7] or "",
                    summary=row[8] or "",
                    result_summary=row[9] or "",
                    created_at=row[10],
                )
                for row in reversed(await cur.fetchall())
            ]
            cur = await conn.execute(
                b"SELECT summary FROM chat_summaries WHERE session_id = %s::uuid", (session_id,)
            )
            row = await cur.fetchone()
        return SessionHistoryResult(
            calu_session_id=session_id,
            claude_session_id=claude_id,
            chat_summary=row[0] if row else "",
            turns=turns,
            steps=steps,
        )

    async def add_client_token(self, token_sha256: str, address: str) -> int:
        """Record a client token's hash (see `SessionStore.add_client_token`).

        Side effect: the hash is kept in memory as well, because `decide_escalation` needs it to
        prove that the decision comes from this session's own client (D28). The role cannot read
        it back from the table.

        Args:
            token_sha256: SHA-256 of the token.
            address: The address clients connect to.

        Returns:
            The id of the new row.

        Raises:
            psycopg.Error: On database errors.
            RuntimeError: If the server returns no row for the insert.
        """
        row_id = await self._db.scalar(
            "INSERT INTO client_tokens (session_id, token_sha256, address)"
            " VALUES (%s, %s, %s) RETURNING id",
            (self._require_session(), token_sha256, address),
        )
        self._token_hash = token_sha256
        return row_id

    async def client_token_ok(self, token_sha256: str) -> bool:
        """Ask the database whether a hash is one of this session's tokens (see the protocol).

        Side effect: `calu_client_token_ok` stamps `last_used_at` on the matching row, and the
        hash is kept in memory if none was kept yet, so that a client which authenticated with a
        minted token can still have its decisions recorded (`decide_escalation` needs a hash of
        this session, and the role cannot read one back).

        Args:
            token_sha256: SHA-256 of the token a client sent.

        Returns:
            True if the token belongs to this session and is not revoked.

        Raises:
            psycopg.Error: On database errors.
        """
        ok = bool(
            await self._db.scalar(
                "SELECT calu_client_token_ok(%s::uuid, %s)",
                (self._require_session(), token_sha256),
            )
        )
        if ok and self._token_hash is None:
            self._token_hash = token_sha256
        return ok

    async def decide_escalation(
        self,
        escalation_id: int,
        allow: bool,
        note: str = "",
        actor: str = "",
        answered: bool = False,
    ) -> bool:
        """Record a client's decision (see `SessionStore.decide_escalation`).

        Args:
            escalation_id: Id from `open_escalation`.
            allow: Whether the step may run.
            note: The human's note, or the answer to a question.
            actor: Name of the client that decided.
            answered: True for the answer to a question of the agent (D35).

        Returns:
            True if the escalation is now resolved; False if it was already resolved, if this
            session has no client token recorded, or if the escalation belongs to another
            session.

        Raises:
            psycopg.Error: On database errors.
        """
        if self._token_hash is None:
            return False
        state = "answered" if answered else ("allowed" if allow else "denied")
        return bool(
            await self._db.scalar(
                "SELECT calu_client_decision(%s, %s::uuid, %s, %s, %s, %s)",
                (
                    escalation_id,
                    self._require_session(),
                    self._token_hash,
                    state,
                    note or None,
                    actor or "unknown",
                ),
            )
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

    async def escalation_context(self, escalation_id: int) -> EscalationContextResult | None:
        """Read the stored context of an escalation (see `SessionStore.escalation_context`).

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            The context with the checker runs of its step, or None if the id is unknown.

        Raises:
            psycopg.Error: On database errors.
        """
        async with self._db.exclusive() as conn:
            cur = await conn.execute(
                b"SELECT state, reason, context, note, resolved_by, step_id FROM escalations"
                b" WHERE id = %s",
                (escalation_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            state, reason, context, note, resolved_by, step_id = row
            runs: list[CheckerRunInfo] = []
            if step_id is not None:
                cur = await conn.execute(
                    b"SELECT checker_name, checker_version, model, decision, reason,"
                    b" input_tokens, output_tokens, cache_read_tokens, duration_ms"
                    b" FROM checker_runs WHERE step_id = %s ORDER BY id",
                    (step_id,),
                )
                runs = [
                    CheckerRunInfo(
                        checker_name=run[0],
                        checker_version=run[1],
                        model=run[2],
                        decision=run[3],
                        reason=run[4] or "",
                        input_tokens=run[5],
                        output_tokens=run[6],
                        cache_read_tokens=run[7],
                        duration_ms=run[8],
                    )
                    for run in await cur.fetchall()
                ]
        stored = context if isinstance(context, dict) else {}
        files = stored.get("files")
        return EscalationContextResult(
            escalation_id=str(escalation_id),
            state=state,
            reason=reason or "",
            rendered=str(stored.get("rendered") or ""),
            tool_name=str(stored.get("tool_name") or ""),
            tool_input=dict(stored.get("tool_input") or {}),
            agent_id=stored.get("agent_id"),
            files=dict(files) if isinstance(files, dict) else {},
            recent=str(stored.get("recent") or ""),
            chat_summary=str(stored.get("chat_summary") or ""),
            checker_runs=runs,
            note=note or "",
            resolved_by=resolved_by,
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


def turn_record(row: tuple[Any, ...]) -> TurnRecord:
    """Build a `TurnRecord` from a row of `turns`.

    Args:
        row: (number, user_inputs, steps, answer, narrated_answer, narrated, started_at,
            ended_at) and optionally (prompt_uuid, last_entry_uuid), in the order the queries
            above select them.

    Returns:
        The turn record.

    Raises:
        pydantic.ValidationError: If a stored step record does not match `StepRecord`.
    """
    number, user_inputs, steps, answer, narration, narrated, started, ended = row[:8]
    prompt_uuid, last_entry_uuid = (row[8], row[9]) if len(row) > 9 else ("", "")
    return TurnRecord(
        index=number,
        user_inputs=list(user_inputs),
        steps=[StepRecord.model_validate(s) for s in steps],
        answer=answer,
        answer_narration=narration or "",
        narrated=narrated,
        prompt_uuid=prompt_uuid or "",
        last_entry_uuid=last_entry_uuid or "",
        **({"started_at": started} if started else {}),
        ended_at=ended,
    )
