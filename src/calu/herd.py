"""Watching and unblocking sessions through the database (PLAN.md §8, REQUIREMENTS 12, 13).

These queries need no running session: they work on the database alone, from any machine that
can reach it. `resolve` calls `calu_resolve_escalation`, which notifies the waiting session, so
the session continues within a second.

`calu sessions`, `calu escalations`, `calu resolve` and `calu token` use the `admin` credential
for now. A separate login for the role `calu_operator` (reads everything, resolves escalations,
mints and revokes client tokens, changes nothing else) is still open (PLAN.md Q4).

`calu token` is the other half of D30: a session accepts every non-revoked token of its own
session, so a token minted here lets a client (an editor's ACP adapter, a local command, a
remote interface) attach without the terminal that started the session, and revoking it closes
that access again.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from calu.context.turns import TurnRecord
from calu.db import Db
from calu.store.session import turn_record
from calu.tokens import new_token, token_hash

STALE_AFTER_S = 180.0
"""Silence after which a session is called stale (PLAN.md D56).

A session stamps `last_seen` every `clock.check_interval_s` seconds, 10 by default, so three
minutes is many missed beats -- long enough that a slow machine or a paused process is not
called dead, short enough that an operator watching a herd notices within one coffee."""


@dataclass(frozen=True)
class SessionRow:
    """One session as the herd commands show it."""

    id: str
    hostname: str
    os_user: str
    directory: str
    state: str
    state_since: datetime
    started_at: datetime
    ended_at: datetime | None
    claude_session_id: str | None
    open_escalations: int
    address: str = ""
    """Where the session's process listens for clients (D29); "" if it serves no clients."""
    last_seen: datetime | None = None
    """When the process last said it was running (D56); None for a row from before that."""
    silent_for_s: float = 0.0
    """Seconds since `last_seen`, measured by the database, so a reader needs no clock of its
    own and clock differences between hosts do not matter."""

    @property
    def stale(self) -> bool:
        """Whether this session looks like a process that is gone.

        A session writes `last_seen` every `clock.check_interval_s` seconds (10 by default), so
        silence of several minutes means the process is not running any more -- it cannot set
        its own state when it is killed. A session that **ended** is never stale: it said so.

        Returns:
            True if the session has not ended and has been silent for longer than
            `STALE_AFTER_S`.
        """
        return self.ended_at is None and self.silent_for_s > STALE_AFTER_S

    def line(self) -> str:
        """Return the session as one terminal line.

        Returns:
            Id, state, host and directory, the number of open escalations if there are any,
            Claude's session id, which `calu run --resume` takes (D27), and a mark when the
            process has stopped saying it is there (D56).
        """
        waiting = f" ({self.open_escalations} open)" if self.open_escalations else ""
        resume = f"  resume {self.claude_session_id}" if self.claude_session_id else ""
        silent = f"  SILENT for {self.silent_for_s / 60:.0f} min" if self.stale else ""
        return (
            f"{self.id}  {self.state}{waiting}  {self.hostname}:{self.directory}"
            f"  since {self.state_since:%Y-%m-%d %H:%M:%S}{resume}{silent}"
        )


@dataclass(frozen=True)
class EscalationRow:
    """One escalation as the herd commands show it."""

    id: int
    session_id: str
    state: str
    reason: str
    created_at: datetime
    rendered: str
    hostname: str
    directory: str
    resolved_by: str = ""
    """Who answered it: an operator's name, or "client:<name>" for a decision a session's own
    client made (D28). Empty while it is open."""
    note: str = ""
    """What the human said when they answered ("" if nothing or still open)."""

    def line(self) -> str:
        """Return the escalation as one terminal line.

        Returns:
            Id, session, host and directory, age and reason.
        """
        return (
            f"{self.id}  session {self.session_id}  {self.hostname}:{self.directory}"
            f"  {self.created_at:%Y-%m-%d %H:%M:%S}\n    {' '.join(self.reason.split())[:300]}"
        )


@dataclass(frozen=True)
class TokenRow:
    """One client token as `calu token list` shows it (never the token itself)."""

    id: int
    session_id: str
    label: str
    address: str
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool

    def line(self) -> str:
        """Return the token as one terminal line.

        Returns:
            Id, state, session, address, label and when it was last used.
        """
        used = f"{self.last_used_at:%Y-%m-%d %H:%M:%S}" if self.last_used_at else "never used"
        state = "revoked" if self.revoked else "valid"
        label = f"  {self.label}" if self.label else ""
        return (
            f"{self.id}  {state}  session {self.session_id}  {self.address or '-'}  {used}{label}"
        )


async def sessions(db: Db, active_only: bool = True) -> list[SessionRow]:
    """List the sessions of all hosts, newest first.

    Args:
        db: A connection with read access to `sessions` (owner or `calu_operator`).
        active_only: Only sessions that have not ended.

    Returns:
        The sessions, newest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT s.id::text, h.hostname, s.os_user, s.directory, s.state, s.state_since,"
        " s.started_at, s.ended_at, s.claude_session_id,"
        " (SELECT count(*) FROM escalations e"
        "  WHERE e.session_id = s.id AND e.state = 'open') AS open_escalations,"
        " coalesce(s.address, ''), s.last_seen,"
        # The database measures the silence, so a reader needs no clock of its own.
        " extract(epoch FROM (now() - s.last_seen))::float8"
        " FROM sessions s JOIN hosts h ON h.id = s.host_id"
        " WHERE (NOT %s::boolean OR s.ended_at IS NULL) ORDER BY s.started_at DESC",
        (active_only,),
    )
    return [SessionRow(*row) for row in rows]


async def escalations(db: Db, session_id: str | None = None) -> list[EscalationRow]:
    """List the open escalations, oldest first.

    Args:
        db: A connection with read access to `escalations`.
        session_id: Only escalations of this session, or None for all.

    Returns:
        The open escalations, oldest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT e.id, e.session_id::text, e.state, e.reason, e.created_at,"
        " coalesce(e.context->>'rendered', ''), h.hostname, s.directory,"
        " coalesce(e.resolved_by, ''), coalesce(e.note, '')"
        " FROM escalations e JOIN sessions s ON s.id = e.session_id"
        " JOIN hosts h ON h.id = s.host_id"
        " WHERE e.state = 'open' AND (%s::uuid IS NULL OR e.session_id = %s::uuid)"
        " ORDER BY e.created_at",
        (session_id, session_id),
    )
    return [EscalationRow(*row) for row in rows]


async def resolve(db: Db, escalation_id: int, decision: str, note: str = "") -> bool:
    """Allow, deny or answer an open escalation and notify the waiting session.

    "answer" belongs to a question of the agent (PLAN.md D35): `note` is then the answer Claude
    receives as the result of its `AskUserQuestion` call, and the call itself does not run.

    Args:
        db: A connection whose role may call `calu_resolve_escalation` (the database owner or
            `calu_operator`).
        escalation_id: Id of the `escalations` row.
        decision: "allow", "deny" or "answer".
        note: Text for Claude and for the record; the answer itself for "answer".

    Returns:
        True if the escalation was open and is now resolved; False if it was already resolved,
        cancelled or does not exist.

    Raises:
        ValueError: If `decision` is none of "allow", "deny" and "answer".
        psycopg.Error: On database errors, including a role that may not resolve.
    """
    states = {"allow": "allowed", "deny": "denied", "answer": "answered"}
    if decision not in states:
        raise ValueError(f"decision must be allow, deny or answer, not {decision!r}")
    state = states[decision]
    return bool(
        await db.scalar(
            "SELECT calu_resolve_escalation(%s, %s, %s)", (escalation_id, state, note or None)
        )
    )


@dataclass(frozen=True)
class StepRow:
    """One recorded step of any session, for a detail view."""

    seq: int
    turn: int | None
    tool_name: str
    decision: str | None
    reason: str
    summary: str
    result_summary: str
    created_at: datetime
    agent_id: str | None
    wrote: dict[str, Any] | None
    """What the step wrote, if the snapshot of D31 measured it."""
    tool_input: dict[str, Any] = field(default_factory=dict)
    narration: str = ""
    """Claude's thinking and text before the step, in indirect speech ("" if not narrated)."""


async def steps(db: Db, session_id: str, limit: int = 200) -> list[StepRow]:
    """Read the recorded steps of one session, oldest first.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.
        limit: Maximum number of steps; the newest ones are kept if there are more.

    Returns:
        The steps, oldest first.

    Raises:
        psycopg.Error: On database errors.
    """
    found = await db.all(
        "SELECT seq, turn, tool_name, decision, coalesce(reason, ''), coalesce(summary, ''),"
        " coalesce(result_summary, ''), created_at, agent_id, wrote, tool_input,"
        " coalesce(narration, '')"
        " FROM steps WHERE session_id = %s::uuid ORDER BY seq DESC LIMIT %s",
        (session_id, limit),
    )
    return [StepRow(*row) for row in reversed(found)]


@dataclass(frozen=True)
class ForkPoint:
    """Where a fork of a session starts (PLAN.md D36).

    `resume_at` is the last transcript entry of the turn that is kept, `drops_turn` the prompt of
    the first turn that is discarded. The CLI validates the pair and refuses a mismatch, so both
    are needed.
    """

    turn: int
    """The first turn that the fork discards."""
    resume_at: str
    drops_turn: str
    claude_session_id: str

    def command(self) -> str:
        """Return the command that starts the fork.

        Returns:
            The `calu run` line, which an operator copies into a terminal on the session's host.
        """
        return f"calu run --resume {self.claude_session_id} --fork-before-turn {self.turn}"


async def fork_point(db: Db, claude_session_id: str, turn: int) -> ForkPoint | None:
    """Read where a conversation has to be resumed to fork before one of its turns.

    Keyed by **Claude's** session id, because that is what `calu run --resume` takes. A
    conversation that was resumed several times has a `sessions` row per run, all with the same
    Claude session id; the newest row that recorded both uuids for these turns is used.

    Args:
        db: The connection.
        claude_session_id: Claude Code's own session id.
        turn: Number of the first turn the fork discards. Turn 1 has no predecessor, so there is
            nothing to keep and the answer is None.

    Returns:
        The fork point, or None if no session has that id, the turn or the one before it is
        unknown, or Claude Code reported no uuids for them (a conversation recorded by a calu
        before migration 0012).

    Raises:
        psycopg.Error: On database errors.
    """
    # One conversation has a `sessions` row per `calu run`, and its turns are spread over them
    # (a resumed run records only the turns it saw itself), so the lookup spans every row with
    # this Claude session id instead of joining within one row.
    row = await db.one(
        "WITH conversation AS (SELECT id FROM sessions WHERE claude_session_id = %s)"
        " SELECT previous.last_entry_uuid, dropped.prompt_uuid"
        " FROM turns dropped JOIN turns previous ON previous.number = dropped.number - 1"
        " WHERE dropped.session_id IN (SELECT id FROM conversation)"
        "  AND previous.session_id IN (SELECT id FROM conversation)"
        "  AND dropped.number = %s"
        "  AND previous.last_entry_uuid IS NOT NULL AND dropped.prompt_uuid IS NOT NULL"
        " ORDER BY dropped.started_at DESC LIMIT 1",
        (claude_session_id, turn),
    )
    if row is None:
        return None
    return ForkPoint(
        turn=turn,
        resume_at=str(row[0]),
        drops_turn=str(row[1]),
        claude_session_id=claude_session_id,
    )


async def fork_points(db: Db, session_id: str) -> list[ForkPoint]:
    """Read every turn of a session that can be forked before.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.

    Returns:
        One entry per turn of the **conversation** (every `sessions` row with the same Claude
        session id) that has both uuids and a predecessor that has them, oldest first. Empty for
        a session without a Claude session id.

    Raises:
        psycopg.Error: On database errors.
    """
    # Like `fork_point`: the turns of one conversation may sit in several `sessions` rows, so
    # the conversation is resolved by its Claude session id first.
    rows = await db.all(
        "WITH conversation AS ("
        "  SELECT other.id, other.claude_session_id FROM sessions mine"
        "  JOIN sessions other ON other.claude_session_id = mine.claude_session_id"
        "  WHERE mine.id = %s::uuid AND mine.claude_session_id IS NOT NULL)"
        " SELECT DISTINCT dropped.number, previous.last_entry_uuid, dropped.prompt_uuid,"
        " (SELECT claude_session_id FROM conversation LIMIT 1)"
        " FROM turns dropped JOIN turns previous ON previous.number = dropped.number - 1"
        " WHERE dropped.session_id IN (SELECT id FROM conversation)"
        "  AND previous.session_id IN (SELECT id FROM conversation)"
        "  AND previous.last_entry_uuid IS NOT NULL AND dropped.prompt_uuid IS NOT NULL"
        " ORDER BY dropped.number",
        (session_id,),
    )
    return [
        ForkPoint(
            turn=int(row[0]),
            resume_at=str(row[1]),
            drops_turn=str(row[2]),
            claude_session_id=str(row[3]),
        )
        for row in rows
    ]


async def turns(db: Db, session_id: str, limit: int = 50) -> list[TurnRecord]:
    """Read the recorded turns of one session, oldest first.

    A turn is the conversation as the **checkers** see it: the user's messages verbatim, one
    record per step, and Claude's answer (narrated in indirect speech if narration was on).
    `TurnRecord.render()` turns it into the Markdown a checker reads, which is what the web
    interface shows as the "narrated summary" level of detail.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.
        limit: Maximum number of turns; the newest ones are kept if there are more.

    Returns:
        The turns, oldest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT number, user_inputs, steps, answer, narrated_answer, narrated,"
        " started_at, ended_at FROM turns WHERE session_id = %s::uuid"
        " ORDER BY number DESC LIMIT %s",
        (session_id, limit),
    )
    return [turn_record(row) for row in reversed(rows)]


@dataclass(frozen=True)
class AgentRow:
    """One subagent of a session (a row of `agents`, migration 0008)."""

    agent_id: str
    parent_agent_id: str | None
    description: str
    subagent_type: str | None
    background: bool
    status: str
    started_at: datetime
    ended_at: datetime | None


async def agents(db: Db, session_id: str) -> list[AgentRow]:
    """Read the subagents of one session, oldest first.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.

    Returns:
        The subagents, oldest first; `parent_agent_id` is None for one the main agent started.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT agent_id, parent_agent_id, description, subagent_type, background, status,"
        " started_at, ended_at FROM agents WHERE session_id = %s::uuid ORDER BY id",
        (session_id,),
    )
    return [AgentRow(*row) for row in rows]


@dataclass(frozen=True)
class SettingChange:
    """One change of a running session's settings, from the audit log (PLAN.md D43)."""

    at: datetime
    actor: str
    setting: str
    old: dict[str, Any]
    new: dict[str, Any]

    def changed(self) -> list[str]:
        """Name the fields that really changed, and how.

        Returns:
            One "field: old -> new" entry per field whose value differs, sorted by field name;
            fields only present on one side count as changed.
        """
        keys = sorted(set(self.old) | set(self.new))
        return [
            f"{key}: {self.old.get(key)!r} -> {self.new.get(key)!r}"
            for key in keys
            if self.old.get(key) != self.new.get(key)
        ]


async def setting_changes(db: Db, session_id: str, limit: int = 50) -> list[SettingChange]:
    """Read the changes of one session's own settings, newest first.

    These are the changes that live in the running process rather than in the project's
    configuration -- `auto_escalate.set` from a client, and the categories a settings reload
    brought -- written by `calu_session_setting_change` (migration 0013). Changes of the
    project's configuration are in the same table with `session_id IS NULL`, recorded by the
    triggers of migration 0001, and are not returned here.

    Args:
        db: The connection (needs SELECT on `audit_log`: the owner or `calu_operator`).
        session_id: Id of the `sessions` row.
        limit: How many to read at most, newest first.

    Returns:
        The changes, newest first; empty if the session changed nothing.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT at, actor, action, old_row, new_row FROM audit_log"
        " WHERE session_id = %s::uuid AND table_name = 'session_settings'"
        " ORDER BY at DESC, id DESC LIMIT %s",
        (session_id, limit),
    )
    return [
        SettingChange(at, actor, action, old or {}, new or {})
        for at, actor, action, old, new in rows
    ]


async def session(db: Db, session_id: str) -> SessionRow | None:
    """Read one session by its id.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.

    Returns:
        The session, or None if no session has that id.

    Raises:
        psycopg.Error: On database errors.
    """
    found = [row for row in await sessions(db, active_only=False) if row.id == session_id]
    return found[0] if found else None


async def escalation(db: Db, escalation_id: int) -> EscalationRow | None:
    """Read one escalation by its id, whatever its state.

    Args:
        db: The connection.
        escalation_id: Id of the `escalations` row.

    Returns:
        The escalation, or None if no escalation has that id.

    Raises:
        psycopg.Error: On database errors.
    """
    found = await db.all(
        "SELECT e.id, e.session_id::text, e.state, e.reason, e.created_at,"
        " coalesce(e.context->>'rendered', ''), h.hostname, s.directory,"
        " coalesce(e.resolved_by, ''), coalesce(e.note, '')"
        " FROM escalations e JOIN sessions s ON s.id = e.session_id"
        " JOIN hosts h ON h.id = s.host_id WHERE e.id = %s",
        (escalation_id,),
    )
    return EscalationRow(*found[0]) if found else None


MAX_SECTION_CHARS = 4_000
"""Characters of a long context section that `render_escalation` prints without `--full`."""


@dataclass(frozen=True)
class CheckerRunRow:
    """What one checker decided about one step (a row of `checker_runs`)."""

    checker_name: str
    checker_version: int
    model: str
    decision: str
    reason: str

    def line(self) -> str:
        """Render the run as one line for a terminal.

        Returns:
            "[command v3, test:model] escalate: the reason", the reason on one line.
        """
        reason = " ".join((self.reason or "").split())
        return f"[{self.checker_name} v{self.checker_version}, {self.model}] {self.decision}" + (
            f": {reason}" if reason else ""
        )


async def escalation_runs(db: Db, escalation_id: int) -> list[CheckerRunRow]:
    """Read what the checkers decided about the step of one escalation.

    The escalation's own `reason` says why the step stopped, which is not the same thing: when
    an auto-escalation category holds a step back (PLAN.md D42) the reason is "every edit waits
    for a human" and what the checkers actually thought is only here. `calu escalation` prints
    these, so a decision taken from the database alone sees them (D30).

    Args:
        db: The connection.
        escalation_id: Id of the `escalations` row.

    Returns:
        The runs of that escalation's step, oldest first; empty if the escalation has no step
        (a session-level escalation) or no checker ran on it (a rule decided).

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT c.checker_name, c.checker_version, c.model, c.decision, coalesce(c.reason, '')"
        " FROM checker_runs c JOIN escalations e ON e.step_id = c.step_id"
        " WHERE e.id = %s ORDER BY c.id",
        (escalation_id,),
    )
    return [CheckerRunRow(*row) for row in rows]


def render_escalation(
    row: EscalationRow,
    context: Mapping[str, Any],
    full: bool = False,
    runs: Sequence[CheckerRunRow] = (),
) -> str:
    """Render everything needed to decide an escalation, for a terminal.

    This is the **database-only** view (PLAN.md D30): an operator who can reach the database can
    see why a step was escalated, what it would do, what Claude asked if it was a question, and
    the conversation as the checkers saw it — without connecting to the session process, which
    may be on another host or already gone.

    Args:
        row: The escalation row.
        context: Its stored context (`escalation_context`).
        full: Print the long sections whole; otherwise each is cut to `MAX_SECTION_CHARS`.
        runs: What the checkers decided about the step (`escalation_runs`); left out when
            empty. They are not the same as `row.reason`: a step held back by an
            auto-escalation category (D42) has a reason that says so and checker runs that say
            the checkers approved it.

    Returns:
        The rendering, ending with the command that answers it.
    """

    def section(title: str, body: str) -> str:
        """Render one titled section, cut unless `full`.

        Args:
            title: The heading.
            body: The section's text.

        Returns:
            The section, or "" if the body is empty.
        """
        text = body.rstrip()
        if not text:
            return ""
        if not full and len(text) > MAX_SECTION_CHARS:
            text = text[:MAX_SECTION_CHARS] + "\n[... cut; run with --full]"
        return f"\n## {title}\n\n{text}\n"

    tool_name = str(context.get("tool_name") or "")
    tool_input = context.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    questions = tool_input.get("questions") if tool_name == "AskUserQuestion" else None
    parts = [
        f"escalation {row.id} [{row.state}]  {row.hostname}:{row.directory}",
        f"session {row.session_id}  asked {row.created_at:%Y-%m-%d %H:%M:%S}",
        f"\n{row.reason.strip()}",
    ]
    if row.resolved_by:
        parts.append(f"\nanswered by {row.resolved_by}: {row.note or '(no note)'}")
    if questions:
        asked = []
        for question in questions if isinstance(questions, list) else []:
            if not isinstance(question, dict):
                continue
            asked.append(str(question.get("question") or question.get("header") or "a question"))
            for option in question.get("options") or []:
                if isinstance(option, dict):
                    label = str(option.get("label", ""))
                    why = str(option.get("description") or "")
                    asked.append(f"  - {label}{f': {why}' if why else ''}")
        parts.append(section("Claude asks", "\n".join(asked)))
    elif tool_name:
        command = str(tool_input.get("command") or "")
        shown = command or json.dumps(tool_input, indent=2, sort_keys=True)[:MAX_SECTION_CHARS]
        parts.append(section(f"The tool call ({tool_name})", shown))
    if runs:
        parts.append(section("What the checkers decided", "\n".join(run.line() for run in runs)))
    parts.append(section("The step as the checker saw it", str(context.get("rendered") or "")))
    parts.append(section("The change that would be committed", str(context.get("changeset") or "")))
    files = context.get("files")
    if isinstance(files, dict):
        for path, content in files.items():
            parts.append(section(f"{path}, before the change", str(content)))
    parts.append(
        section("The conversation, as the checkers see it", str(context.get("recent") or ""))
    )
    parts.append(section("The folded summary", str(context.get("chat_summary") or "")))
    if row.state == "open":
        answer = (
            f'calu resolve {row.id} answer "your answer"'
            if questions
            else f"calu resolve {row.id} allow|deny [--note ...]"
        )
        parts.append(f"\nanswer it with: {answer}\n")
    return "\n".join(part for part in parts if part)


async def escalation_context(db: Db, escalation_id: int) -> dict[str, Any]:
    """Read what the checker saw when it escalated a step (the stored context).

    Args:
        db: The connection.
        escalation_id: Id of the `escalations` row.

    Returns:
        The stored context (rendered step, recent turns, the file before an edit, ...), or an
        empty dict if the escalation is unknown or has no context.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one("SELECT context FROM escalations WHERE id = %s", (escalation_id,))
    context = row[0] if row else None
    return context if isinstance(context, dict) else {}


async def checker_runs(db: Db, session_id: str, limit: int = 200) -> dict[int, list[str]]:
    """Read which checkers judged which step of a session.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.
        limit: Maximum number of rows to read.

    Returns:
        For each step sequence number, the checker names that ran for it, in order.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT s.seq, r.checker_name FROM checker_runs r JOIN steps s ON s.id = r.step_id"
        " WHERE s.session_id = %s::uuid ORDER BY r.id LIMIT %s",
        (session_id, limit),
    )
    found: dict[int, list[str]] = {}
    for seq, name in rows:
        found.setdefault(int(seq), []).append(str(name))
    return found


async def project_of(db: Db, session_id: str) -> tuple[str, str] | None:
    """Read the project a session belongs to.

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.

    Returns:
        The project's name and its git remote ("" if it has none), or None if the session has no
        project row.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "SELECT p.name, coalesce(p.git_remote, '') FROM sessions s"
        " JOIN projects p ON p.id = s.project_id WHERE s.id = %s::uuid",
        (session_id,),
    )
    return (str(row[0]), str(row[1])) if row else None


async def mint_token(db: Db, session_id: str, label: str = "") -> tuple[int, str]:
    """Create a client token for a session (PLAN.md D30).

    Only the token's hash is stored, so the returned token is the only copy: the caller prints
    it once. The session accepts it at its next `initialize`, without being restarted.

    Args:
        db: The connection (owner or `calu_operator`).
        session_id: Id of the `sessions` row (`calu sessions` prints it).
        label: What the token is for, e.g. "zed on my laptop"; shown by `calu token list`.

    Returns:
        The id of the new row and the token.

    Raises:
        ValueError: If no session has that id.
        psycopg.Error: On database errors.
    """
    token = new_token()
    # INSERT ... SELECT inserts nothing for an unknown session, so the missing row is the error.
    row = await db.one(
        """INSERT INTO client_tokens (session_id, token_sha256, label, address)
           SELECT s.id, %s, %s, coalesce(s.address, '') FROM sessions s WHERE s.id = %s::uuid
           RETURNING id""",
        (token_hash(token), label, session_id),
    )
    if row is None:
        raise ValueError(f"no session with id {session_id}")
    return int(row[0]), token


async def list_tokens(db: Db, session_id: str | None = None) -> list[TokenRow]:
    """Read the client tokens of one session or of all sessions, newest first.

    Args:
        db: The connection.
        session_id: Which session; None lists the tokens of every session.

    Returns:
        The rows, newest first. The tokens themselves are not stored and never returned.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        """SELECT id, session_id::text, label, address, created_at, last_used_at, revoked
             FROM client_tokens
            WHERE %s::uuid IS NULL OR session_id = %s::uuid
            ORDER BY id DESC""",
        (session_id, session_id),
    )
    return [
        TokenRow(
            id=int(row[0]),
            session_id=str(row[1]),
            label=str(row[2]),
            address=str(row[3]),
            created_at=row[4],
            last_used_at=row[5],
            revoked=bool(row[6]),
        )
        for row in rows
    ]


async def revoke_token(db: Db, token_id: int) -> bool:
    """Revoke a client token, so that no client can attach with it any more.

    A client that is already connected keeps its connection: the token is checked at
    `initialize`, not per request. Stopping such a client needs `session.stop` or the end of the
    calu process.

    Args:
        db: The connection (owner or `calu_operator`).
        token_id: Id from `calu token list`.

    Returns:
        True if the token existed and was not revoked yet.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "UPDATE client_tokens SET revoked = true WHERE id = %s AND NOT revoked RETURNING id",
        (token_id,),
    )
    return row is not None
