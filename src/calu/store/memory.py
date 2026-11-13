"""A `SessionStore` without a database, for unit tests and dry runs.

It keeps everything it is given in lists and dicts, so a test can assert what the session
recorded, and it never fails. Escalations stay open until a test resolves them with `resolve`,
which stands for the operator's `calu_resolve_escalation`.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from calu.comments import Comment
from calu.context.turns import TurnRecord
from calu.protocol.messages import (
    AgentInfo,
    EscalationContextResult,
    SessionHistoryResult,
    SessionSummary,
    State,
)
from calu.store.session import (
    SESSION_ENDED_NOTE,
    CheckerRun,
    Escalation,
    EscalationState,
    SessionIdentity,
    StepDecision,
    StepRow,
)
from calu.store.trust import TRUST_STRENGTH, TrustedCommit, TrustedFile, TrustSource, stronger


@dataclass
class RecordedStep:
    """A step as the in-memory store holds it: the call, its decision and its checker runs."""

    id: int
    row: StepRow
    decision: StepDecision | None = None
    result_summary: str | None = None
    checker_runs: list[CheckerRun] = field(default_factory=list)


@dataclass
class MemorySessionStore:
    """In-memory implementation of `calu.store.session.SessionStore`.

    Attributes hold what was recorded: `states` every state in order, `steps` the steps by id,
    `turns` the stored turns by number, `summaries` every stored summary with its covered turn,
    `escalations` the escalations by id. `previous` is what `load_previous` returns, by Claude
    session id (a test sets it to simulate a resumed session).
    """

    session_id: str | None = None
    identity: SessionIdentity | None = None
    claude_session_id: str | None = None
    model: str | None = None
    states: list[State] = field(default_factory=list)
    ended: State | None = None
    steps: dict[int, RecordedStep] = field(default_factory=dict)
    turns: dict[int, TurnRecord] = field(default_factory=dict)
    folded_up_to: int = 0
    summaries: list[tuple[str, int]] = field(default_factory=list)
    escalations: dict[int, Escalation] = field(default_factory=dict)
    contexts: dict[int, dict[str, Any]] = field(default_factory=dict)
    """The context each escalation was opened with, by id."""
    reasons: dict[int, str] = field(default_factory=dict)
    """The reason each escalation was opened with, by id. The database keeps it (it is what a
    human reads when they decide, PLAN.md D30), so the stand-in keeps it too."""
    previous: dict[str, tuple[list[TurnRecord], str]] = field(default_factory=dict)
    stored_sessions: list[SessionSummary] = field(default_factory=list)
    """What `recent_sessions` returns (a test sets it)."""
    comments: list[tuple[int, Comment, int | None]] = field(default_factory=list)
    client_tokens: list[tuple[str, str]] = field(default_factory=list)
    decided_with: dict[int, str | None] = field(default_factory=dict)
    write_sets: dict[int, dict[str, Any]] = field(default_factory=dict)
    """What each step wrote, as `record_changes` recorded it (PLAN.md D31)."""
    address: str = ""
    heartbeats: int = 0
    """How often `heartbeat` was called (PLAN.md D56)."""

    setting_changes: list[tuple[str, dict[str, Any], dict[str, Any], str]] = field(
        default_factory=list
    )
    """Changed session settings as `note_setting_change` recorded them (PLAN.md D43)."""
    agents: dict[str, AgentInfo] = field(default_factory=dict)
    """The subagents as `update_agents` recorded them, by agent id."""
    agent_parents: dict[str, str | None] = field(default_factory=dict)
    """Which agent started which; None means the main agent."""
    history: SessionHistoryResult | None = None
    """What `load_history` returns; None builds it from the stored turns."""
    trusted_commits: dict[str, TrustedCommit] = field(default_factory=dict)
    """The trusted commits by commit id, as `certify_commit` recorded them; a test may also
    fill it to stand for a certification made elsewhere."""
    trusted_files: dict[str, TrustedFile] = field(default_factory=dict)
    """The trusted file contents by SHA-256, as `trust_file` recorded them."""
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def start(self, identity: SessionIdentity) -> str:
        """Record the identity and return a fixed session id.

        Args:
            identity: Where the session runs.

        Returns:
            The session id "memory-session".
        """
        self.identity = identity
        self.session_id = "memory-session"
        self.states.append("starting")
        return self.session_id

    async def set_state(self, state: State) -> None:
        """Append `state` to `states`.

        Args:
            state: The new state.
        """
        self.states.append(state)

    async def set_fork_origin(self, claude_session_id: str, before_turn: int | None) -> None:
        """Remember what this session is a fork of (see `SessionStore.set_fork_origin`).

        Args:
            claude_session_id: The Claude session id of the conversation that was copied.
            before_turn: The first turn the fork dropped, or None.
        """
        self.fork_origin = (claude_session_id, before_turn)

    async def set_claude_session(self, claude_session_id: str, model: str | None = None) -> None:
        """Record Claude Code's session id and model.

        Args:
            claude_session_id: The id Claude Code reports.
            model: The Claude model, or None to leave it unchanged.
        """
        self.claude_session_id = claude_session_id
        self.model = model or self.model

    async def set_address(self, address: str) -> None:
        """Record where the session listens.

        Args:
            address: "ws://host:port" or a Unix socket path.
        """
        self.address = address

    async def note_setting_change(
        self, setting: str, old: dict[str, Any], new: dict[str, Any], actor: str
    ) -> None:
        """Remember a changed session setting in `setting_changes`.

        Args:
            setting: Which setting changed.
            old: Its value before the change.
            new: Its value after the change.
            actor: Who asked for it.
        """
        self.setting_changes.append((setting, old, new, actor))

    async def heartbeat(self) -> None:
        """Count a heartbeat in `heartbeats`."""
        self.heartbeats += 1

    async def add_agent(self, agent_id: str, parent_agent_id: str | None = None) -> None:
        """Record a subagent and its parent, keeping the first parent seen.

        Args:
            agent_id: Id of the Agent tool call that starts the subagent.
            parent_agent_id: The agent that made that call; None means the main agent.
        """
        self.agent_parents.setdefault(agent_id, parent_agent_id)

    async def update_agents(self, agents: Sequence[AgentInfo]) -> None:
        """Record what is known about the subagents.

        Args:
            agents: The session's subagents as the runner reports them.
        """
        for agent in agents:
            self.agents[agent.agent_id] = agent
            self.agent_parents.setdefault(agent.agent_id, None)

    async def end(self, state: State) -> None:
        """Record the final state, cancel open escalations and drop the client tokens.

        See `SessionStore.end`.

        The tokens belong to this session alone, so they are worthless once it has ended; the
        database store marks them revoked, and here they are simply forgotten, which makes
        `client_token_ok` say no in the same way (D108).

        Args:
            state: "stopped" or "failed".
        """
        self.ended = state
        self.states.append(state)
        for escalation_id, current in list(self.escalations.items()):
            if current.state == "open":
                self.escalations[escalation_id] = Escalation(
                    id=escalation_id, state="cancelled", note=SESSION_ENDED_NOTE
                )
        self.client_tokens.clear()

    async def add_step(self, step: StepRow) -> int:
        """Record a tool call.

        Args:
            step: The tool call.

        Returns:
            A new id, counted from 1.
        """
        step_id = next(self._ids)
        self.steps[step_id] = RecordedStep(id=step_id, row=step)
        return step_id

    async def decide_step(self, step_id: int, decision: StepDecision) -> None:
        """Record the decision about a step.

        Args:
            step_id: Id from `add_step`.
            decision: What was decided.
        """
        self.steps[step_id].decision = decision

    async def finish_step(self, step_id: int, result_summary: str = "") -> None:
        """Record that a step has run.

        Args:
            step_id: Id from `add_step`.
            result_summary: Short description of the result.
        """
        self.steps[step_id].result_summary = result_summary

    async def add_checker_run(self, step_id: int, run: CheckerRun) -> None:
        """Record one checker model call.

        Args:
            step_id: Id from `add_step`.
            run: The call and its result.
        """
        self.steps[step_id].checker_runs.append(run)

    async def save_turn(self, turn: TurnRecord) -> None:
        """Store a turn under its number.

        Args:
            turn: The turn record.
        """
        self.turns[turn.index] = turn

    async def mark_folded(self, up_to_index: int) -> None:
        """Record how far the turns are folded.

        Args:
            up_to_index: Number of the newest folded turn.
        """
        self.folded_up_to = up_to_index

    async def save_summary(self, summary: str, covered_turns: int) -> None:
        """Append the summary to `summaries`.

        Args:
            summary: The summary text.
            covered_turns: Number of the newest turn it covers.
        """
        self.summaries.append((summary, covered_turns))

    async def load_summary(self) -> str:
        """Return the last stored summary (see `SessionStore.load_summary`).

        Returns:
            The text of the newest entry of `summaries`, or "" if nothing was stored.
        """
        return self.summaries[-1][0] if self.summaries else ""

    async def load_previous(self, claude_session_id: str) -> tuple[list[TurnRecord], str]:
        """Return the prepared context of a resumed session.

        Args:
            claude_session_id: The Claude session id that is resumed.

        Returns:
            The entry of `previous` for that id, or `([], "")`.
        """
        return self.previous.get(claude_session_id, ([], ""))

    async def add_comment(self, comment: Comment, turn: int | None = None) -> int:
        """Record a user comment.

        Args:
            comment: The comment as it was relayed to Claude.
            turn: Number of the turn it was sent in, or None.

        Returns:
            A new id, counted from 1 (shared with the step ids).
        """
        comment_id = next(self._ids)
        self.comments.append((comment_id, comment, turn))
        return comment_id

    async def load_history(
        self, claude_session_id: str | None = None, limit: int = 500
    ) -> SessionHistoryResult:
        """Return the prepared history.

        Args:
            claude_session_id: Which session; None is this one.
            limit: Maximum number of steps returned.

        Returns:
            `history` with its steps cut to `limit`, or the stored turns of this session.
        """
        if self.history is not None:
            return self.history.model_copy(update={"steps": self.history.steps[-limit:]})
        return SessionHistoryResult(
            calu_session_id=self.session_id,
            claude_session_id=claude_session_id or self.claude_session_id,
            turns=[self.turns[number] for number in sorted(self.turns)],
            chat_summary=self.summaries[-1][0] if self.summaries else "",
        )

    async def add_client_token(self, token_sha256: str, address: str) -> int:
        """Record a client token's hash.

        Args:
            token_sha256: SHA-256 of the token.
            address: The address clients connect to.

        Returns:
            A new id, counted from 1 (shared with the step ids).
        """
        token_id = next(self._ids)
        self.client_tokens.append((token_sha256, address))
        return token_id

    async def record_changes(
        self,
        step_id: int,
        before: str,
        after: str,
        paths: Sequence[str],
        undeclared: Sequence[str] = (),
        overlapping: bool = False,
    ) -> None:
        """Keep a step's write set in `write_sets` (see `SessionStore.record_changes`).

        Args:
            step_id: Id from `add_step`.
            before: Git tree before the step.
            after: Git tree after it.
            paths: The paths that changed.
            undeclared: The paths the checker's summary did not mention.
            overlapping: Whether another call ran in the same window.
        """
        self.write_sets[step_id] = {
            "before": before,
            "after": after,
            "paths": list(paths),
            "undeclared": list(undeclared),
            "overlapping": overlapping,
        }

    async def client_token_ok(self, token_sha256: str) -> bool:
        """Tell whether a recorded token has this hash (see `SessionStore.client_token_ok`).

        Args:
            token_sha256: SHA-256 of the token a client sent.

        Returns:
            True if `client_tokens` holds that hash.
        """
        return any(stored == token_sha256 for stored, _ in self.client_tokens)

    async def recent_sessions(self, directory: str, limit: int = 20) -> list[SessionSummary]:
        """Return the prepared session list.

        Args:
            directory: The project directory (ignored; `stored_sessions` is returned as is).
            limit: Maximum number of sessions returned.

        Returns:
            The first `limit` entries of `stored_sessions`.
        """
        return self.stored_sessions[:limit]

    async def open_escalation(
        self, reason: str, context: dict[str, Any], step_id: int | None = None
    ) -> int:
        """Record an open escalation.

        Args:
            reason: Why the step needs a decision.
            context: What an operator needs to decide.
            step_id: Id of the step, or None.

        Returns:
            A new id, counted from 1 (shared with the step ids).
        """
        escalation_id = next(self._ids)
        self.escalations[escalation_id] = Escalation(id=escalation_id, state="open")
        self.contexts[escalation_id] = context
        self.reasons[escalation_id] = reason
        return escalation_id

    async def escalation(self, escalation_id: int) -> Escalation:
        """Return the state of an escalation.

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            The recorded escalation; state "cancelled" if it is unknown.
        """
        return self.escalations.get(escalation_id) or Escalation(
            id=escalation_id, state="cancelled"
        )

    async def close_escalation(self, escalation_id: int, note: str = "") -> None:
        """Cancel an escalation that is still open.

        Args:
            escalation_id: Id from `open_escalation`.
            note: Why it was cancelled.
        """
        current = self.escalations.get(escalation_id)
        if current is not None and current.open:
            self.escalations[escalation_id] = Escalation(
                id=escalation_id, state="cancelled", note=note
            )

    async def escalation_context(self, escalation_id: int) -> EscalationContextResult | None:
        """Return the recorded context of an escalation.

        Args:
            escalation_id: Id from `open_escalation`.

        Returns:
            A result built from `escalation_context`, or None if the id is unknown.
        """
        current = self.escalations.get(escalation_id)
        if current is None:
            return None
        stored = self.contexts.get(escalation_id, {})
        return EscalationContextResult(
            escalation_id=str(escalation_id),
            state=current.state,
            rendered=str(stored.get("rendered") or ""),
            tool_name=str(stored.get("tool_name") or ""),
            tool_input=dict(stored.get("tool_input") or {}),
            agent_id=stored.get("agent_id"),
            files=dict(stored.get("files") or {}),
            recent=str(stored.get("recent") or ""),
            chat_summary=str(stored.get("chat_summary") or ""),
            note=current.note,
            resolved_by=current.resolved_by,
        )

    async def decide_escalation(
        self,
        escalation_id: int,
        allow: bool,
        note: str = "",
        actor: str = "",
        answered: bool = False,
        token_sha256: str | None = None,
    ) -> bool:
        """Record a client's decision on an open escalation.

        Args:
            escalation_id: Id from `open_escalation`.
            allow: Whether the step may run.
            note: The human's note, or the answer to a question.
            actor: Name of the client that decided.
            answered: True for the answer to a question of the agent (D35).
            token_sha256: SHA-256 of the deciding client's token; kept in `decided_with` and
                not checked, because this store has no revoked tokens.

        Returns:
            True if the escalation was open; False otherwise.
        """
        current = self.escalations.get(escalation_id)
        if current is None or not current.open:
            return False
        self.decided_with[escalation_id] = token_sha256
        self.escalations[escalation_id] = Escalation(
            id=escalation_id,
            state="answered" if answered else ("allowed" if allow else "denied"),
            note=note,
            resolved_by=f"client:{actor or 'unknown'}",
        )
        return True

    async def trusted_among(
        self, commit_ids: Sequence[str], tree_ids: Sequence[str]
    ) -> tuple[dict[str, TrustSource], dict[str, TrustSource]]:
        """Tell which ids are in `trusted_commits` (see `SessionStore.trusted_among`).

        Args:
            commit_ids: Commit ids to look up.
            tree_ids: Tree ids to look up.

        Returns:
            Commit id -> source for the trusted ones among `commit_ids`, and tree id -> the
            strongest source among the trusted commits that record it, for the ones among
            `tree_ids`.
        """
        trees: dict[str, TrustSource] = {}
        for row in self.trusted_commits.values():
            if row.tree_id in tree_ids:
                method = row.method
                trees[row.tree_id] = stronger(trees.get(row.tree_id), method) or method
        commits: dict[str, TrustSource] = {
            commit: self.trusted_commits[commit].method
            for commit in commit_ids
            if commit in self.trusted_commits
        }
        return commits, trees

    async def certify_commit(
        self, commit_id: str, tree_id: str, method: TrustSource, by: str, note: str = ""
    ) -> bool:
        """Record a trusted commit (see `SessionStore.certify_commit`).

        Args:
            commit_id: The full commit id.
            tree_id: The id of the tree the commit records.
            method: "user", "review" or "check".
            by: Who certified it.
            note: An optional note.

        Returns:
            True if the commit was recorded or its record was strengthened; False if a record
            of the same or a stronger source exists.

        Raises:
            ValueError: If `by` is empty or `method` is none of the three, which the database
                refuses as well.
        """
        if not by:
            raise ValueError("a certification names who certified")
        if method not in TRUST_STRENGTH:
            raise ValueError("the method is one of user, review, check")
        held = self.trusted_commits.get(commit_id)
        if held is not None and TRUST_STRENGTH[held.method] >= TRUST_STRENGTH[method]:
            return False
        self.trusted_commits[commit_id] = TrustedCommit(
            commit_id, tree_id, datetime.now(UTC), by, method, note
        )
        return True

    async def trusted_file_sources(
        self, hashes: Sequence[str] | None = None
    ) -> dict[str, TrustSource]:
        """Return the sources of `trusted_files` (see `SessionStore.trusted_file_sources`).

        Args:
            hashes: The SHA-256 values to look up; None for all.

        Returns:
            SHA-256 -> source.
        """
        return {
            sha256: row.method
            for sha256, row in self.trusted_files.items()
            if hashes is None or sha256 in hashes
        }

    async def trust_file(
        self, content_sha256: str, path: str, method: TrustSource, by: str, note: str = ""
    ) -> bool:
        """Record a verified file content (see `SessionStore.trust_file`).

        Args:
            content_sha256: SHA-256 of the file's content.
            path: Where the file is, as a hint for people.
            method: "user", "review" or "check".
            by: Who certified it.
            note: An optional note.

        Returns:
            True if the hash was recorded or its record was strengthened; False if a record
            of the same or a stronger source exists.

        Raises:
            ValueError: If `by` is empty or `method` is none of the three, which the database
                refuses as well.
        """
        if not by:
            raise ValueError("a certification names who certified")
        if method not in TRUST_STRENGTH:
            raise ValueError("the method is one of user, review, check")
        held = self.trusted_files.get(content_sha256)
        if held is not None and TRUST_STRENGTH[held.method] >= TRUST_STRENGTH[method]:
            return False
        self.trusted_files[content_sha256] = TrustedFile(
            content_sha256, path, datetime.now(UTC), by, method, note
        )
        return True

    async def distrust_file(self, content_sha256: str, by: str) -> bool:
        """Withdraw a stored file content (see `SessionStore.distrust_file`).

        Args:
            content_sha256: SHA-256 of the file's content.
            by: Who withdrew it.

        Returns:
            True if a record of the source "check" or "review" was removed; False if there is
            none or a person recorded it.

        Raises:
            ValueError: If `by` is empty, which the database refuses as well.
        """
        if not by:
            raise ValueError("a withdrawal names who withdrew")
        held = self.trusted_files.get(content_sha256)
        if held is None or held.method == "user":
            return False
        del self.trusted_files[content_sha256]
        return True

    def resolve(
        self, escalation_id: int, state: EscalationState, note: str = "", by: str = "operator"
    ) -> None:
        """Resolve an escalation as an operator would (for tests).

        Args:
            escalation_id: Id from `open_escalation`.
            state: "allowed", "denied", "answered" (a question of the agent, the note holding
                the answer) or "cancelled".
            note: The operator's note.
            by: Who resolved it.
        """
        self.escalations[escalation_id] = Escalation(
            id=escalation_id, state=state, note=note, resolved_by=by
        )
