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

import dataclasses
import json
import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from calu import machine
from calu.claude.transcripts import Transcript
from calu.context.turns import TurnRecord
from calu.db import Db
from calu.store.session import turn_record
from calu.tokens import new_token, token_hash

log = logging.getLogger(__name__)

STALE_AFTER_S = 180.0
"""Silence after which a session is called stale (PLAN.md D56).

A session stamps `last_seen` every `HEARTBEAT_S` seconds, so three minutes is six missed beats
-- long enough that a slow machine, a paused process or a short database outage is not called
dead, short enough that an operator watching a herd notices within one coffee."""

HEARTBEAT_S = 30.0
"""Seconds between two `last_seen` stamps of a running session.

A constant of its own, not a setting: the staleness threshold above is fixed, so an interval
that an operator could raise (or switch off, as `clock.check_interval_s` allows for the clock
watch) would make live sessions look dead and their escalations unanswerable."""

assert HEARTBEAT_S * 3 <= STALE_AFTER_S, "a session must be able to miss two beats"

CLOSE_SILENT_AFTER_S = 24 * 3600.0
"""Silence after which the open escalations of a session are closed without further evidence
that its process is gone (`close_gone`). Much longer than `STALE_AFTER_S`: a session that is
still alive after a silence (a suspended machine, a network that was away) would find its
question cancelled, so the lists only mark a stale session as gone until then."""


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

        A session writes `last_seen` every `HEARTBEAT_S` seconds, so silence of several minutes
        means the process is not running any more (or cannot reach the database) -- it cannot
        set its own state when it is killed. A session that **ended** is never stale: it said so.

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
    session_silent_for_s: float = 0.0
    """Seconds since the session's process last said it was running (D56)."""
    session_ended: bool = False
    """Whether the session has ended; then the silence means nothing."""
    about: str = ""
    """What the step is: the command, the edited path, or the tool's name. A queue of reasons
    without the steps they are about cannot be triaged -- a reader had to open every entry to
    see what it was (PLAN.md D127)."""

    @property
    def unanswerable(self) -> bool:
        """Whether nobody is waiting for this answer any more.

        A decision reaches the session through a notification, and a process that is gone
        cannot act on it (D56): the escalation can still be recorded for the history, but the
        step will never run and the session will never continue. An operator working through a
        list should know which entries are in that state before spending attention on them.

        Returns:
            True if the session has ended or has been silent beyond `STALE_AFTER_S`.
        """
        return self.session_ended or self.session_silent_for_s > STALE_AFTER_S

    def line(self) -> str:
        """Return the escalation as one terminal line.

        Returns:
            Id, session, host and directory, age, what the step is, the reason, and a mark when
            answering it cannot reach anybody (D56).
        """
        gone = "  [the session is gone: answering changes nothing]" if self.unanswerable else ""
        about = f"\n    {' '.join(self.about.split())[:200]}" if self.about.strip() else ""
        return (
            f"{self.id}  session {self.session_id}  {self.hostname}:{self.directory}"
            f"  {self.created_at:%Y-%m-%d %H:%M:%S}{gone}{about}"
            f"\n    {' '.join(self.reason.split())[:300]}"
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


async def close_gone(db: Db) -> int:
    """Close the open escalations of sessions that are gone, so the queue holds none of them.

    An escalation is closed -- recorded as "cancelled" with a note that says why, because
    nobody decided it (PLAN.md D57) -- when its session

    1. has ended (`sessions.ended_at`), or
    2. is stale (silent for more than `STALE_AFTER_S`, D56), ran in this machine's process
       table, and no process with its pid exists here any more; or ran on this machine before
       its last reboot (`calu.machine`, D202), or
    3. has been silent for more than `CLOSE_SILENT_AFTER_S` while the database server was up.

    A stale session of another machine is only marked as gone in the lists until rule 3
    applies: silence does not prove that its process is gone (a suspended machine, a network
    that was away), and a process that is still alive treats a cancelled escalation as "not
    allowed". The same host name does not make it this machine: a session that recorded where
    it runs (`sessions.machine`) is judged by that, and only a session of a calu that recorded
    nothing is judged by the host name.

    Side effects: updates `escalations` through `calu_close_gone_escalations` and
    `calu_close_session_escalations`, which send `escalation_resolved` for each closed row.

    Args:
        db: A connection of the database owner or of `calu_operator`.

    Returns:
        How many escalations were closed; 0 as well when the connection's role may not close
        them (a role that only reads) or the database is read-only, because listing must
        still work then.

    Raises:
        psycopg.Error: On other database errors.
    """
    try:
        closed = int(
            await db.scalar("SELECT calu_close_gone_escalations(%s)", (CLOSE_SILENT_AFTER_S,))
        )
        rows = await db.all(
            "SELECT s.id::text, s.pid, s.machine FROM sessions s JOIN hosts h ON h.id = s.host_id"
            " WHERE s.ended_at IS NULL AND h.hostname = %s"
            " AND s.last_seen < now() - make_interval(secs => %s)"
            " AND EXISTS (SELECT 1 FROM escalations e"
            "             WHERE e.session_id = s.id AND e.state = 'open')",
            (socket.gethostname(), STALE_AFTER_S),
        )
        this = machine.here()
        for session_id, pid, recorded_text in rows:
            recorded = machine.Machine.parse(recorded_text)
            relation = "same" if recorded is None else this.relation(recorded)
            if relation == "other" or (relation == "same" and process_exists(pid)):
                continue
            note = (
                f"the session's process (pid {pid}) is gone"
                if relation == "same"
                else f"the session's process (pid {pid}) ended when its machine was restarted"
            )
            closed += int(
                await db.scalar(
                    "SELECT calu_close_session_escalations(%s::uuid, %s)", (session_id, note)
                )
            )
        return closed
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as exc:
        log.debug("not closing the escalations of gone sessions: %s", exc)
        return 0


def process_exists(pid: int) -> bool:
    """Tell whether a process with this pid exists on this machine.

    Args:
        pid: The process id.

    Returns:
        False only if the operating system says there is no such process. True otherwise,
        including when the process belongs to another user or the pid is not a usable number,
        so that a doubt never counts as "gone".
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def sessions(db: Db, active_only: bool = True) -> list[SessionRow]:
    """List the sessions of all hosts, newest first.

    Side effects: closes the open escalations of sessions that are gone first (`close_gone`),
    so `open_escalations` counts only questions somebody still waits for.

    Args:
        db: A connection with read access to `sessions` (owner or `calu_operator`).
        active_only: Only sessions that have not ended.

    Returns:
        The sessions, newest first.

    Raises:
        psycopg.Error: On database errors.
    """
    await close_gone(db)
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


def age_text(seconds: float) -> str:
    """Say how long ago something happened, in the largest unit that fits.

    Args:
        seconds: The age in seconds; a negative value counts as 0.

    Returns:
        For example "40 s", "12 min", "5 h" or "3 d". Below two minutes in seconds, below two
        hours in minutes, below two days in hours, else in days; each rounded down.
    """
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7200:
        return f"{int(seconds // 60)} min"
    if seconds < 172_800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86_400)} d"


@dataclass(frozen=True)
class ConversationRow:
    """One Claude conversation of a directory, as `calu list-sessions` shows it (D203).

    A conversation is what one Claude session id names. It may span several `sessions` rows,
    one per `calu run` that started or resumed it.
    """

    claude_session_id: str
    """The id `calu run --resume` and `calu run --fork` take."""
    age_s: float
    """Seconds since the last activity, measured by the database."""
    last_input: str
    """The last message of the user in the conversation; "" if no turn holds one."""
    turns: int
    """Number of the last stored turn."""
    running: bool
    """Whether a session of this conversation has not ended and still says it is running
    (silent for no longer than `STALE_AFTER_S`): attach to it instead of resuming it."""
    outside: bool = False
    """Whether the conversation was started with `claude` itself and calu has no record of
    it (D209): it is read from Claude Code's transcript, and none of its steps was checked."""

    def line(self, width: int = 100) -> str:
        """Return the conversation as one terminal line.

        Args:
            width: The length the line is cut to; the last input is what is shortened.

        Returns:
            The age, the Claude session id, "running" if a process still has the
            conversation, "outside calu" if calu has no record of it, and the last user
            input on one line. Whitespace runs of the input
            are one space, and a character that is not printable (control characters, escape
            sequences) is shown as U+FFFD. The length is counted in characters, so a line
            with wide characters can be wider than `width` columns.
        """
        mark = "  running" if self.running else "  outside calu" if self.outside else ""
        head = f"{age_text(self.age_s):>7}  {self.claude_session_id}{mark}  "
        # The input is text somebody typed or pasted: no character of it may steer the
        # terminal it is printed to.
        text = "".join(
            char if char.isprintable() else "\N{REPLACEMENT CHARACTER}"
            for char in " ".join(self.last_input.split())
        )
        text = text or "(no user input stored)"
        room = max(10, width - len(head))
        if len(text) > room:
            text = text[: room - 1] + "…"
        return head + text


async def conversations(
    db: Db, directory: str, hostname: str, os_user: str, limit: int = 5
) -> list[ConversationRow]:
    """List the Claude conversations of one directory that have a stored turn (D203).

    Only the runs of one host and one OS user count: Claude Code keeps a conversation's
    transcript in that user's home directory on that host, so a conversation recorded for the
    same path elsewhere cannot be resumed here.

    The last activity of a conversation is the latest of: the start of one of its `calu run`s
    and the start or end of one of its stored turns. A turn is stored when it ends, so a
    conversation whose first turn still runs is not listed yet, and the input of a running
    turn is not the "last input". A conversation without a stored turn has nothing to resume.
    A fork stores the turns it inherits when it starts, so it is listed from then on, with
    the last input of the conversation it copied until it has a turn of its own.

    Args:
        db: A connection with read access to `sessions`, `hosts` and `turns` (owner or
            `calu_operator`).
        directory: The project directory as `calu run` recorded it: the resolved absolute
            path.
        hostname: The host name `calu run` recorded (`socket.gethostname()`).
        os_user: The OS user `calu run` recorded (`getpass.getuser()`).
        limit: How many conversations to return at most; at least 1.

    Returns:
        The conversations, the one with the latest activity first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "WITH runs AS ("
        "  SELECT s.id, s.claude_session_id, s.started_at, s.ended_at, s.last_seen"
        "    FROM sessions s JOIN hosts h ON h.id = s.host_id"
        "   WHERE s.directory = %s AND h.hostname = %s AND s.os_user = %s"
        "     AND s.claude_session_id IS NOT NULL),"
        " stored AS ("
        "  SELECT r.claude_session_id, t.number, t.user_inputs, r.started_at AS run_started,"
        "         greatest(t.started_at, t.ended_at, t.created_at) AS at"
        "    FROM turns t JOIN runs r ON r.id = t.session_id),"
        " per AS ("
        "  SELECT claude_session_id, max(at) AS last_turn, max(number) AS turns"
        "    FROM stored GROUP BY claude_session_id),"
        " started AS ("
        "  SELECT claude_session_id, max(started_at) AS last_start,"
        "         bool_or(ended_at IS NULL"
        "                 AND last_seen > now() - make_interval(secs => %s)) AS running"
        "    FROM runs GROUP BY claude_session_id)"
        " SELECT per.claude_session_id,"
        # The database measures the age, so a reader needs no clock of its own.
        "        extract(epoch FROM (now() - greatest(per.last_turn, started.last_start)))::float8,"
        "        coalesce((SELECT s.user_inputs->>-1 FROM stored s"
        "                   WHERE s.claude_session_id = per.claude_session_id"
        "                     AND jsonb_array_length(s.user_inputs) > 0"
        "                   ORDER BY s.number DESC, s.run_started DESC LIMIT 1), ''),"
        "        per.turns, started.running"
        "   FROM per JOIN started USING (claude_session_id)"
        "  ORDER BY greatest(per.last_turn, started.last_start) DESC, per.claude_session_id"
        "  LIMIT %s",
        (directory, hostname, os_user, STALE_AFTER_S, max(1, limit)),
    )
    return [ConversationRow(*row) for row in rows]


@dataclass(frozen=True)
class ForkOrigin:
    """Where a forked session comes from."""

    claude_session_id: str
    """The Claude session id of the conversation that was copied."""
    before_turn: int | None
    """The first turn the fork dropped; None if it kept the whole conversation."""
    session_id: str | None
    """The newest session row of that conversation, for a link; None if there is none."""

    def kept(self) -> str:
        """Say in words what the fork took over.

        Returns:
            "turns 1 to N" for a fork before a turn, else "the whole conversation".
        """
        if self.before_turn is None:
            return "the whole conversation"
        last = self.before_turn - 1
        return "turn 1" if last == 1 else f"turns 1 to {last}"


async def fork_origin(db: Db, session_id: str) -> ForkOrigin | None:
    """Read what a session is a fork of.

    Args:
        db: A connection with read access to `sessions`.
        session_id: The session's row id.

    Returns:
        The origin, or None if the session is no fork (or ran before migration 0034).

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "SELECT s.forked_from, s.forked_before_turn,"
        "       (SELECT o.id::text FROM sessions o WHERE o.claude_session_id = s.forked_from"
        "         ORDER BY o.started_at DESC LIMIT 1)"
        "  FROM sessions s WHERE s.id = %s::uuid AND s.forked_from IS NOT NULL",
        (session_id,),
    )
    return None if row is None else ForkOrigin(row[0], row[1], row[2])


async def forks_of(db: Db, claude_session_id: str) -> list[tuple[str, int | None]]:
    """List the sessions that are forks of a conversation, oldest first.

    Args:
        db: A connection with read access to `sessions`.
        claude_session_id: The conversation's Claude session id.

    Returns:
        The row id of each fork and the first turn it dropped (None: it kept all).

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT id::text, forked_before_turn FROM sessions WHERE forked_from = %s"
        " ORDER BY started_at",
        (claude_session_id,),
    )
    return [(row[0], row[1]) for row in rows]


async def known_conversations(db: Db, directory: str, hostname: str, os_user: str) -> set[str]:
    """Return the Claude session ids calu has recorded for one directory, host and user.

    Unlike `conversations` this includes a conversation whose first turn has not ended.

    Args:
        db: A connection with read access to `sessions` and `hosts`.
        directory: The project directory as `calu run` recorded it.
        hostname: The host name `calu run` recorded.
        os_user: The OS user `calu run` recorded.

    Returns:
        The ids.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT DISTINCT s.claude_session_id FROM sessions s JOIN hosts h ON h.id = s.host_id"
        " WHERE s.directory = %s AND h.hostname = %s AND s.os_user = %s"
        "   AND s.claude_session_id IS NOT NULL",
        (directory, hostname, os_user),
    )
    return {row[0] for row in rows}


def with_transcripts(
    rows: list[ConversationRow],
    known: set[str],
    found: list[Transcript],
    last_input: Callable[[str], str],
    limit: int,
    now: datetime | None = None,
) -> list[ConversationRow]:
    """Add the conversations that only Claude Code knows to a listing (D209).

    Args:
        rows: The conversations calu has stored turns of, the freshest first.
        known: Every Claude session id calu has recorded for the directory; a transcript
            with one of them is calu's own conversation and is not added.
        found: The transcripts Claude Code keeps for the directory.
        last_input: Returns the last message the user typed in a transcript, by Claude
            session id ("" if none). Called only for the transcripts that are listed.
        limit: How many conversations to return at most.
        now: The current time; None reads the clock.

    Returns:
        At most `limit` conversations of both kinds, the freshest first. One that only
        Claude Code knows has `outside` set and no turn count.
    """
    current = now or datetime.now(UTC)
    outside = [
        ConversationRow(
            claude_session_id=transcript.session_id,
            age_s=max(0.0, (current - transcript.last_activity).total_seconds()),
            last_input=transcript.first_input,
            turns=0,
            running=False,
            outside=True,
        )
        for transcript in found
        if transcript.session_id not in known
    ]
    merged = sorted([*rows, *outside], key=lambda row: row.age_s)[: max(1, limit)]
    return [
        dataclasses.replace(row, last_input=last_input(row.claude_session_id) or row.last_input)
        if row.outside
        else row
        for row in merged
    ]


async def escalations(db: Db, session_id: str | None = None) -> list[EscalationRow]:
    """List the open escalations, oldest first.

    Side effects: closes the open escalations of sessions that are gone first (`close_gone`),
    so the list holds only questions whose answer a running session would act on.

    Args:
        db: A connection with read access to `escalations`.
        session_id: Only escalations of this session, or None for all.

    Returns:
        The open escalations, oldest first.

    Raises:
        psycopg.Error: On database errors.
    """
    await close_gone(db)
    rows = await db.all(
        "SELECT e.id, e.session_id::text, e.state, e.reason, e.created_at,"
        " coalesce(e.context->>'rendered', ''), h.hostname, s.directory,"
        " coalesce(e.resolved_by, ''), coalesce(e.note, ''),"
        # The database measures the silence, as for a session row (D56).
        " extract(epoch FROM (now() - s.last_seen))::float8, s.ended_at IS NOT NULL,"
        " coalesce(e.context->'tool_input'->>'command',"
        "          e.context->'tool_input'->>'file_path',"
        "          e.context->>'tool_name', '')"
        " FROM escalations e JOIN sessions s ON s.id = e.session_id"
        " JOIN hosts h ON h.id = s.host_id"
        " WHERE e.state = 'open' AND (%s::uuid IS NULL OR e.session_id = %s::uuid)"
        " ORDER BY e.created_at",
        (session_id, session_id),
    )
    return [EscalationRow(*row) for row in rows]


async def resolve(db: Db, escalation_id: int, decision: str, note: str = "") -> bool:
    """Allow, deny, answer or cancel an open escalation and notify the waiting session.

    "answer" belongs to a question of the agent (PLAN.md D35): `note` is then the answer Claude
    receives as the result of its `AskUserQuestion` call, and the call itself does not run.

    "cancel" is **not** a decision and is recorded as such: it clears an escalation nobody can
    act on any more -- a session whose process is gone (D56) -- so the queue a human works
    through holds only entries that still mean something, and the history does not claim that
    somebody allowed or denied a step that never ran (D57).

    Args:
        db: A connection whose role may call `calu_resolve_escalation` (the database owner or
            `calu_operator`).
        escalation_id: Id of the `escalations` row.
        decision: "allow", "deny", "answer" or "cancel".
        note: Text for Claude and for the record; the answer itself for "answer"; the reason
            for "cancel".

    Returns:
        True if the escalation was open and is now resolved or cancelled; False if it was
        already resolved, cancelled or does not exist.

    Raises:
        ValueError: If `decision` is none of "allow", "deny", "answer" and "cancel".
        psycopg.Error: On database errors, including a role that may not resolve.
    """
    if decision == "cancel":
        # Not a decision: the step never ran and nobody acted on it. A separate function,
        # because `calu_resolve_escalation` only writes the three states a human chose
        # (PLAN.md D57).
        return bool(
            await db.scalar("SELECT calu_cancel_escalation(%s, %s)", (escalation_id, note or None))
        )
    states = {"allow": "allowed", "deny": "denied", "answer": "answered"}
    if decision not in states:
        raise ValueError(f"decision must be allow, deny, answer or cancel, not {decision!r}")
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
    # Through `calu_fork_point` (migration 0025), which runs as the owner: `calu run` asks on a
    # `calu_session` connection before its own session row exists, and row-level security
    # hides every `sessions` and `turns` row from that connection. Owner and operator use the
    # same path.
    row = await db.one(
        "SELECT last_entry_uuid, prompt_uuid FROM calu_fork_point(%s, %s)",
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
        " coalesce(e.resolved_by, ''), coalesce(e.note, ''),"
        " extract(epoch FROM (now() - s.last_seen))::float8, s.ended_at IS NOT NULL,"
        " coalesce(e.context->'tool_input'->>'command',"
        "          e.context->'tool_input'->>'file_path',"
        "          e.context->>'tool_name', '')"
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
    elif row.unanswerable:
        # The decision would be recorded and never acted on, because the process is gone (D56).
        parts.append(
            "\nthe session's process is gone: an answer is recorded for the history, but the"
            " step will not run and the session will not continue"
        )
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
        if row.unanswerable:
            # Nobody is waiting, so the useful command is the one that clears the queue
            # honestly rather than one that records a decision nothing acted on (D57).
            answer = f'calu resolve {row.id} cancel "the session is gone"'
        elif questions:
            answer = f'calu resolve {row.id} answer "your answer"'
        else:
            answer = f"calu resolve {row.id} allow|deny [--note ...]"
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


def _tokens(n: int) -> str:
    """Render a token count short enough for one terminal line.

    Args:
        n: A token count, zero or more.

    Returns:
        The number itself below 10 000, else thousands ("31k") or millions ("2.4M").
    """
    if n < 10_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"


@dataclass(frozen=True)
class CheckerStats:
    """How one checker has decided so far, and how often a human disagreed (PLAN.md D61)."""

    checker_name: str
    runs: int
    passed: int
    escalated: int
    allowed: int
    denied: int
    escalated_then_allowed: int
    """Escalations it caused that a human allowed: caution that cost an interruption."""
    escalated_then_denied: int
    """Escalations it caused that a human denied: the caution was right."""
    passed_then_denied: int
    """Steps it approved that a human then denied, because an auto-escalation category held
    them anyway (D42): the misses, which are the error that matters (D50)."""
    median_ms: int | None
    """Median duration of one check, or None when nothing was timed."""
    input_tokens: int
    """Input tokens of all its checks, cached ones included (0 if nothing was counted)."""
    output_tokens: int
    """Output tokens of all its checks."""
    cache_read_tokens: int
    """Input tokens the provider served from its cache (PLAN.md D63).

    calu builds the checkers' context so that the long part can be cached (the cached prefix of
    §2.4, `CachePoint`). If this stays near zero while `input_tokens` grows, the caching is not
    working and every check pays for the whole prefix again."""

    @property
    def cached_share(self) -> float | None:
        """Share of the input tokens that came from the provider's cache.

        Returns:
            A value between 0 and 1, or None when no input tokens were counted (a fake model,
            or a provider that reports no usage).
        """
        if self.input_tokens <= 0:
            return None
        return self.cache_read_tokens / self.input_tokens

    @property
    def tokens_text(self) -> str:
        """The token totals in short form, or "" when the model reported no usage.

        Returns:
            Something like `31k in / 2.4k out`, or an empty string when nothing was counted.
        """
        if not self.input_tokens:
            return ""
        return f"{_tokens(self.input_tokens)} in / {_tokens(self.output_tokens)} out"

    @property
    def cache_text(self) -> str:
        """How much of the input the provider served from its cache, as text.

        Returns:
            Something like `48% of the input from cache`, or an empty string when no input
            tokens were counted. See `cached_share` for what a low value means.
        """
        share = self.cached_share
        return "" if share is None else f"{share * 100:.0f}% of the input from cache"

    @property
    def cache_is_cold(self) -> bool:
        """Whether the cached prefix looks like it is not working (D63).

        A checker runs on every step with nearly the same long prefix, so after a few thousand
        input tokens most of the input should come from the provider's cache. True means it does
        not, so the checks cost several times what they could. The cause is not always calu: a
        provider may not cache at all, and a hybrid-SSM model such as Nemotron on Fireworks
        reuses a cache only for an identical prompt (`calu.checkers.checker` module docstring).
        The flag therefore says "look at this", not "calu is broken".

        Returns:
            True when at least 5000 input tokens were counted and under a third of them came
            from the cache; False when the share is healthy or too little ran to tell.
        """
        share = self.cached_share
        return share is not None and self.input_tokens >= 5000 and share < 1 / 3

    def line(self) -> str:
        """Render the checker's record as one terminal line.

        Returns:
            The counts, and the human's verdicts on what it escalated and what it passed.
        """
        timing = f"  ~{self.median_ms} ms" if self.median_ms is not None else ""
        tokens = f"  {self.tokens_text}" if self.tokens_text else ""
        cached = f", {self.cache_text}" if self.cache_text else ""
        if self.cache_is_cold:
            cached += " -- the prefix is not being reused; check the model's provider caches"
        judged = self.escalated_then_allowed + self.escalated_then_denied
        agreed = (
            f"  humans: {self.escalated_then_denied}/{judged} agreed with its escalations"
            if judged
            else ""
        )
        missed = (
            f", {self.passed_then_denied} it approved were denied"
            if self.passed_then_denied
            else ""
        )
        return (
            f"{self.checker_name}  {self.runs} run(s): {self.passed} pass, "
            f"{self.escalated} escalate, {self.allowed} allow, {self.denied} deny{timing}"
            f"{tokens}{cached}{agreed}{missed}"
        )


@dataclass(frozen=True)
class StepStats:
    """How many steps calu decided, by which path, and how long they waited (PLAN.md D64)."""

    steps: int
    """Steps recorded in the window."""
    rules_allowed: int
    """Steps every part of which was allow-listed, so no checker ran: the free path."""
    rules_denied: int
    """Steps a deny rule matched, so no checker ran either."""
    rules_unlisted: int
    """Steps the lists cover but did not settle, which is what a checker is for."""
    rules_silent: int
    """Steps of a tool the lists do not cover at all (`calu.gate._rules_outcome` returns None)."""
    checked: int
    """Steps at least one checker ran on, which are the only ones that cost tokens."""
    median_gate_ms: int | None
    """Median time from recording a step to deciding it, over steps nobody was asked about.
    This is what calu adds to a step that runs through: the rules, the checkers and the
    database, without any human waiting time. None when nothing was decided yet."""
    slowest_gate_ms: int | None
    """The same measurement's maximum, which is the delay a user actually notices."""
    median_wait_s: int | None
    """Median time a resolved escalation waited for its answer, in seconds. This is a measure
    of the people, not of calu. None when no escalation was resolved in the window."""
    resolved: int
    """Escalations resolved in the window (allowed, denied or cancelled)."""

    @property
    def free_share(self) -> float | None:
        """Share of the steps that no checker ran on.

        Returns:
            A value between 0 and 1, or None when no step was recorded. The rules settle these
            steps without a model call, so the share is what keeps the token bill down.
        """
        if self.steps <= 0:
            return None
        return (self.steps - self.checked) / self.steps

    def lines(self) -> list[str]:
        """Render the step record as a few terminal lines.

        Returns:
            One line about the paths through the gate and, when there is something to say,
            one about the delay and one about the waiting for people. An empty list when no
            step was recorded.
        """
        if not self.steps:
            return []
        share = self.free_share
        assert share is not None  # self.steps > 0 here
        out = [
            f"{self.steps} step(s): {self.checked} went to a checker, "
            f"{self.steps - self.checked} ({share * 100:.0f}%) did not -- "
            f"{self.rules_allowed} allow-listed, {self.rules_denied} denied by a rule, "
            f"{self.rules_silent} of a tool the lists do not cover"
        ]
        if self.median_gate_ms is not None:
            out.append(
                f"calu's own delay per step: ~{self.median_gate_ms} ms, "
                f"worst {self.slowest_gate_ms} ms (no human waiting time in this)"
            )
        if self.median_wait_s is not None:
            out.append(
                f"{self.resolved} escalation(s) answered, after ~{self.median_wait_s} s of "
                "waiting for a person"
            )
        return out


async def step_stats(db: Db, since_days: int | None = None) -> StepStats:
    """Summarise which path the steps took through the gate and how long they waited.

    This is the other half of `checker_stats`: that one says what the checkers did with the
    steps they saw, this one says how many steps reached them at all. Both read the record and
    cost nothing. The point of the first number is that the allow and deny lists settle a step
    without a model call (`calu rules`), so the share of steps no checker ran on is the share
    that is free; the point of the second is that everything calu does sits between Claude Code
    asking to run a tool and the tool running, so it is measurable as a delay.

    Args:
        db: A connection that may read `steps`, `checker_runs` and `escalations`.
        since_days: Only count steps and escalations of the last N days; None counts
            everything.

    Returns:
        The counts and the two timings. All counts are 0 and all timings None on an empty
        database, so a caller can print `StepStats.lines()` without a special case.

    Raises:
        psycopg.Error: On database errors.
    """
    window = "(%s::int IS NULL OR {col} > now() - make_interval(days => %s::int))"
    row = await db.one(
        "WITH s AS ("
        "  SELECT st.rules_outcome,"
        "   EXISTS (SELECT 1 FROM checker_runs c WHERE c.step_id = st.id) AS checked,"
        "   EXISTS (SELECT 1 FROM escalations e WHERE e.step_id = st.id) AS escalated,"
        "   (extract(epoch FROM (st.decided_at - st.created_at)) * 1000)::int AS gate_ms"
        "  FROM steps st"
        f"  WHERE {window.format(col='st.created_at')}"
        ")"
        "SELECT count(*)::int,"
        " count(*) FILTER (WHERE rules_outcome = 'allow')::int,"
        " count(*) FILTER (WHERE rules_outcome = 'deny')::int,"
        " count(*) FILTER (WHERE rules_outcome = 'unlisted')::int,"
        " count(*) FILTER (WHERE rules_outcome IS NULL)::int,"
        " count(*) FILTER (WHERE checked)::int,"
        # The delay is measured on steps nobody was asked about: with an escalation it would
        # measure how fast the person answered, which the next query reports separately.
        " percentile_disc(0.5) WITHIN GROUP (ORDER BY gate_ms) FILTER (WHERE NOT escalated),"
        " max(gate_ms) FILTER (WHERE NOT escalated)"
        " FROM s",
        (since_days, since_days),
    )
    assert row is not None  # an aggregate over no rows is still one row
    waits = await db.one(
        "SELECT percentile_disc(0.5) WITHIN GROUP ("
        "  ORDER BY extract(epoch FROM (resolved_at - created_at))::int),"
        " count(*)::int"
        " FROM escalations WHERE resolved_at IS NOT NULL AND " + window.format(col="resolved_at"),
        (since_days, since_days),
    )
    assert waits is not None  # as above
    return StepStats(*row, median_wait_s=waits[0], resolved=waits[1])


async def checker_stats(db: Db, since_days: int | None = None) -> list[CheckerStats]:
    """Summarise what the checkers decided and how often a human disagreed.

    This is the free half of `calu evals`: it reads what already happened instead of running
    the checkers again, so it costs nothing and says something the eval set cannot -- how they
    behave on this project's real steps. Two numbers matter most. An escalation a human
    **allowed** cost an interruption for nothing; a step a checker **passed** that a human then
    denied is a miss, which only exists in the record when an auto-escalation category made the
    human look (D42, D50).

    Args:
        db: A connection that may read `checker_runs`, `steps` and `escalations`.
        since_days: Only count runs of the last N days; None counts everything.

    Returns:
        One entry per checker that has run, most runs first, with its token totals and the
        share of the input that the provider served from its cache (D63).

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        # The token sums come from their own aggregate, not from the joined rows: a step may
        # have more than one escalation, and the LEFT JOIN would then count its tokens twice.
        "WITH tokens AS ("
        "  SELECT checker_name,"
        "   coalesce(sum(input_tokens), 0)::int AS input_tokens,"
        "   coalesce(sum(output_tokens), 0)::int AS output_tokens,"
        "   coalesce(sum(cache_read_tokens), 0)::int AS cache_read_tokens"
        "  FROM checker_runs"
        "  WHERE (%s::int IS NULL OR created_at > now() - make_interval(days => %s::int))"
        "  GROUP BY checker_name"
        ")"
        "SELECT c.checker_name,"
        " count(*)::int,"
        " count(*) FILTER (WHERE c.decision = 'pass')::int,"
        " count(*) FILTER (WHERE c.decision = 'escalate')::int,"
        " count(*) FILTER (WHERE c.decision = 'allow')::int,"
        " count(*) FILTER (WHERE c.decision = 'deny')::int,"
        " count(*) FILTER (WHERE c.decision = 'escalate' AND e.state = 'allowed')::int,"
        " count(*) FILTER (WHERE c.decision = 'escalate' AND e.state = 'denied')::int,"
        " count(*) FILTER (WHERE c.decision = 'pass' AND e.state = 'denied')::int,"
        " percentile_disc(0.5) WITHIN GROUP (ORDER BY c.duration_ms),"
        # What the checkers cost, and whether the cached prefix is working (D63). The columns
        # were recorded from the first version on and nothing read them before.
        " max(t.input_tokens),"
        " max(t.output_tokens),"
        " max(t.cache_read_tokens)"
        " FROM checker_runs c"
        " LEFT JOIN escalations e ON e.step_id = c.step_id"
        " JOIN tokens t ON t.checker_name = c.checker_name"
        " WHERE (%s::int IS NULL OR c.created_at > now() - make_interval(days => %s::int))"
        " GROUP BY c.checker_name ORDER BY count(*) DESC, c.checker_name",
        (since_days, since_days, since_days, since_days),
    )
    # By keyword, not by position: the row has fifteen columns and a field inserted in the
    # wrong place would silently shift every later one.
    return [
        CheckerStats(
            checker_name=row[0],
            runs=row[1],
            passed=row[2],
            escalated=row[3],
            allowed=row[4],
            denied=row[5],
            escalated_then_allowed=row[6],
            escalated_then_denied=row[7],
            passed_then_denied=row[8],
            median_ms=row[9],
            input_tokens=row[10],
            output_tokens=row[11],
            cache_read_tokens=row[12],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class CheckerMiss:
    """A step a checker approved that a human then denied (PLAN.md D62)."""

    escalation_id: int
    checker_name: str
    decision: str
    """What the checker decided -- "pass" or "allow"."""
    checker_reason: str
    tool_name: str
    summary: str
    """The checker's one-line summary of the step, or "" if it wrote none."""
    command: str
    """The command for a Bash step, else "" (the whole input is in the escalation)."""
    human_note: str
    """What the human wrote when they denied it; "" if they wrote nothing."""
    at: datetime

    def line(self) -> str:
        """Render the miss as two terminal lines.

        Returns:
            What the checker said and what the human did, with the escalation id so that
            `calu escalation <id>` shows the whole context.
        """
        what = self.command or self.summary or self.tool_name
        said = " ".join((self.checker_reason or "(no reason)").split())[:160]
        note = " ".join((self.human_note or "(no note)").split())[:160]
        return (
            f"escalation {self.escalation_id}  {self.checker_name} said {self.decision}"
            f"  {self.at:%Y-%m-%d %H:%M}\n    step: {' '.join(what.split())[:160]}"
            f"\n    checker: {said}\n    human: denied -- {note}"
        )


async def checker_misses(
    db: Db, checker: str | None = None, since_days: int | None = None, limit: int = 20
) -> list[CheckerMiss]:
    """List the steps a checker approved and a human then denied, newest first.

    These are the rows worth reading when the instructions are tuned: a checker that escalates
    too much is an annoyance, one that approves what a human refuses is the error calu exists to
    prevent. They only reach the record because an auto-escalation category made somebody look
    at an approved step (D42, D50), so this list is as long as that category was switched on.

    Args:
        db: A connection that may read `checker_runs`, `steps` and `escalations`.
        checker: Only this checker's misses; None for all of them.
        since_days: Only the last N days; None for all of them.
        limit: Maximum number returned, newest first.

    Returns:
        One entry per miss, newest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT e.id, c.checker_name, c.decision, coalesce(c.reason, ''), s.tool_name,"
        " coalesce(s.summary, ''), coalesce(s.tool_input->>'command', ''),"
        " coalesce(e.note, ''), e.resolved_at"
        " FROM checker_runs c"
        " JOIN steps s ON s.id = c.step_id"
        " JOIN escalations e ON e.step_id = c.step_id"
        " WHERE c.decision IN ('pass', 'allow') AND e.state = 'denied'"
        "   AND (%s::text IS NULL OR c.checker_name = %s::text)"
        "   AND (%s::int IS NULL OR e.resolved_at > now() - make_interval(days => %s::int))"
        " ORDER BY e.resolved_at DESC NULLS LAST LIMIT %s",
        (checker, checker, since_days, since_days, limit),
    )
    return [CheckerMiss(*row) for row in rows]


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

    A client that is already connected with it loses its connection as well: the session checks
    the token again before every request that changes something and on a schedule of a few
    seconds (`calu.protocol.server`), and closes the connection of a revoked token.

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
