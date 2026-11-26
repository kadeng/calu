"""Temporary rules: sentences a person wrote, which the checkers read until they expire.

The design is `agent/records/notes/temporary-rules-design.md`. A rule widens or narrows what the
command, edit and changeset checkers approve, for a limited time and a scope:

    scope     applies to                                       stored as
    session   one conversation (Claude's session id), so it    conversation
              survives a resume of that conversation
    project   every session of one registered project          project_id
    global    every session                                    -

    duration  1m .. 7d ("30m", "1h", "3d"); default 1d; 1d at most for a rule that lifts
              calu's built-in protections (`lifts_builtin`)

The table `temporary_rules` (migration 0002) enforces the limits too, so a client that skips
this module cannot store a rule of eight days. Adding and removing go through the user login
(`calu_admin`), which Claude does not have; the audit trigger records each change and announces
it as `config_changed`, so a running session reads its rules again.

Nothing here tells Claude which rules are active (the owner's decision, 2026-11-26).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from calu.db import Db

Scope = Literal["session", "project", "global"]

DEFAULT_DURATION = timedelta(days=1)
"""The duration of a rule when none is given."""

MIN_DURATION = timedelta(minutes=1)
MAX_DURATION = timedelta(days=7)
MAX_LIFTING_DURATION = timedelta(days=1)
"""The longest duration of a rule that lifts calu's built-in protections."""

MAX_TEXT_CHARS = 1000
"""The longest text of a rule, as the table's check constraint allows."""

_DURATION = re.compile(r"(\d+)([mhd])")
_FIRST_WORD = re.compile(r"(\S+)(\s*)(.*)", re.DOTALL)
"""The first word of a non-empty text without leading white space, the white space after it,
and the rest as written (`split_command`)."""
_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


class RuleError(ValueError):
    """A rule that cannot be stored; the message says why, for the person who wrote it."""


@dataclass(frozen=True)
class TemporaryRule:
    """One stored rule, as the clients and the checkers see it."""

    id: int
    text: str
    scope: Scope
    project: str | None
    """The project's name for scope "project"; None otherwise."""
    conversation: str | None
    """Claude's session id for scope "session"; None otherwise."""
    lifts_builtin: bool
    created_at: datetime
    expires_at: datetime
    created_by: str
    origin: str
    """"manual", or "escalation <id>" for a rule added from an escalation dialog."""
    removed_at: datetime | None = None
    removed_by: str | None = None

    def active(self, now: datetime) -> bool:
        """Tell whether the rule counts at `now`.

        Args:
            now: The time to judge, with a time zone.

        Returns:
            True if it was not removed and does not expire before `now`.
        """
        return self.removed_at is None and self.expires_at > now

    def where(self) -> str:
        """Describe the rule's scope for a person or a checker.

        Returns:
            "this session", "project <name>" or "every project".
        """
        if self.scope == "session":
            return "this session"
        if self.scope == "project":
            return f"project {self.project}"
        return "every project"


def parse_duration(text: str | None) -> timedelta:
    """Read a duration as the slash command and the clients write it.

    Args:
        text: A whole number with `m`, `h` or `d` ("30m", "1h", "3d"); None or "" for the
            default.

    Returns:
        The duration; `DEFAULT_DURATION` for None or "".

    Raises:
        RuleError: If the text is not of that form, or the duration is below one minute or
            above seven days.
    """
    if not text:
        return DEFAULT_DURATION
    match = _DURATION.fullmatch(text.strip().lower())
    if match is None:
        raise RuleError(
            f"{text!r} is no duration: write a number with m, h or d, e.g. 30m, 1h or 3d"
        )
    duration = int(match.group(1)) * _UNITS[match.group(2)]
    if duration < MIN_DURATION:
        raise RuleError("a rule lasts at least one minute")
    if duration > MAX_DURATION:
        raise RuleError(
            "a rule lasts at most 7 days; a rule for longer belongs in the checker instructions"
        )
    return duration


def split_command(arguments: str) -> tuple[str | None, Scope | None, str]:
    """Split the arguments of `/rule` into duration, scope and text.

        /rule [duration] [--session | --project | --global] <text>

    The duration and the scope may come in either order before the text; anything after the
    first word that is neither is the text.

    Args:
        arguments: What follows "/rule ".

    Returns:
        (the duration as written or None, the scope or None, the text without surrounding
        white space).
    """
    duration: str | None = None
    scope: Scope | None = None
    rest = arguments.strip()
    while rest:
        word, _, after = _FIRST_WORD.fullmatch(rest).groups()  # type: ignore[union-attr]
        if duration is None and _DURATION.fullmatch(word.lower()):
            duration = word
        elif scope is None and word in ("--session", "--project", "--global"):
            scope = word.removeprefix("--")  # type: ignore[assignment]  # one of the three
        else:
            break
        rest = after.lstrip()
    return duration, scope, rest


def check_text(text: str) -> str:
    """Check the text of a new rule.

    Args:
        text: The rule as written.

    Returns:
        The text without surrounding white space.

    Raises:
        RuleError: If it is empty or white space only, or longer than `MAX_TEXT_CHARS`.
    """
    stripped = text.strip()
    if not stripped:
        raise RuleError("a rule needs a text")
    if len(stripped) > MAX_TEXT_CHARS:
        raise RuleError(f"a rule has at most {MAX_TEXT_CHARS} characters")
    return stripped


_COLUMNS = (
    "r.id, r.text, r.scope, p.name, r.conversation, r.lifts_builtin, r.created_at, "
    "r.expires_at, r.created_by, r.origin, r.removed_at, r.removed_by"
)
_FROM = " FROM temporary_rules r LEFT JOIN projects p ON p.id = r.project_id"


def _rule(row: Sequence[object]) -> TemporaryRule:
    """Build a rule from a row of `_COLUMNS`.

    Args:
        row: The row.

    Returns:
        The rule.
    """
    return TemporaryRule(*row)  # type: ignore[arg-type]  # the columns are in field order


async def add(
    db: Db,
    text: str,
    *,
    created_by: str,
    duration: timedelta = DEFAULT_DURATION,
    scope: Scope = "session",
    project: str | None = None,
    conversation: str | None = None,
    lifts_builtin: bool = False,
    origin: str = "manual",
) -> TemporaryRule:
    """Store a new rule. It counts from the database's present time.

    Side effects: inserts a `temporary_rules` row; the audit trigger records it and announces
    `config_changed`.

    Args:
        db: A connection with the user login.
        text: The rule (`check_text`).
        created_by: Who adds it, e.g. "client:calu ide" or "cli:kai"; not empty.
        duration: How long it counts (`parse_duration`).
        scope: "session", "project" or "global".
        project: The project's name, for scope "project".
        conversation: Claude's session id, for scope "session".
        lifts_builtin: Whether it lifts calu's built-in protections; such a rule lasts one day
            at most. Only the web settings and `calu rule add --lift-builtin` pass True.
        origin: "manual", or "escalation <id>" for a rule from an escalation dialog.

    Returns:
        The stored rule.

    Raises:
        RuleError: If the text, the duration or the scope's argument is not valid, or the
            project is not registered.
        psycopg.Error: On database errors.
    """
    clean = check_text(text)
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise RuleError("a rule lasts between one minute and 7 days")
    if lifts_builtin and duration > MAX_LIFTING_DURATION:
        raise RuleError("a rule that lifts calu's built-in protections lasts one day at most")
    if not created_by:
        raise RuleError("a rule needs the name of who adds it")
    project_id: int | None = None
    if scope == "project":
        if not project:
            raise RuleError("a project rule needs a registered project")
        found = await db.one("SELECT id FROM projects WHERE name = %s", (project,))
        if found is None:
            raise RuleError(f"no project {project!r} is registered")
        project_id = int(found[0])
    elif scope == "session":
        if not conversation:
            raise RuleError("a session rule needs the session's conversation")
    elif scope != "global":
        raise RuleError(f"{scope!r} is no scope: session, project or global")
    row = await db.one(
        "WITH new AS (INSERT INTO temporary_rules (text, scope, project_id, conversation,"
        " lifts_builtin, expires_at, created_by, origin)"
        " VALUES (%s, %s, %s, %s, %s, now() + %s, %s, %s) RETURNING *)"
        " SELECT new.id, new.text, new.scope, p.name, new.conversation, new.lifts_builtin,"
        " new.created_at, new.expires_at, new.created_by, new.origin, new.removed_at,"
        " new.removed_by FROM new LEFT JOIN projects p ON p.id = new.project_id",
        (
            clean,
            scope,
            project_id,
            conversation if scope == "session" else None,
            lifts_builtin,
            duration,
            created_by,
            origin,
        ),
    )
    assert row is not None  # INSERT ... RETURNING returns the row or raises
    return _rule(row)


async def active(
    db: Db, project: str | None = None, conversation: str | None = None
) -> list[TemporaryRule]:
    """List the rules that count now for a session, oldest first.

    Args:
        db: A connection with read access to `temporary_rules`.
        project: The session's project, or None if it has none.
        conversation: The session's conversation (Claude's session id), or None.

    Returns:
        The rules that were not removed and do not expire before the database's present
        time, of scope "global", of the session's project, or of its conversation.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT "
        + _COLUMNS
        + _FROM
        + " WHERE r.removed_at IS NULL AND r.expires_at > now() AND (r.scope = 'global'"
        " OR (r.scope = 'project' AND p.name = %s)"
        " OR (r.scope = 'session' AND r.conversation = %s))"
        " ORDER BY r.created_at, r.id",
        (project, conversation),
    )
    return [_rule(row) for row in rows]


async def listed(db: Db, include_ended: bool = False) -> list[TemporaryRule]:
    """List the stored rules for a person, newest first.

    Args:
        db: A connection with read access to `temporary_rules`.
        include_ended: Also the rules that expired or were removed.

    Returns:
        The rules.

    Raises:
        psycopg.Error: On database errors.
    """
    where = "" if include_ended else " WHERE r.removed_at IS NULL AND r.expires_at > now()"
    rows = await db.all("SELECT " + _COLUMNS + _FROM + where + " ORDER BY r.created_at DESC")
    return [_rule(row) for row in rows]


async def remove(db: Db, rule_id: int, removed_by: str) -> TemporaryRule | None:
    """Remove a rule before it expires; it stays in the table for the record.

    Side effects: sets `removed_at` and `removed_by`; the audit trigger records it and
    announces `config_changed`.

    Args:
        db: A connection with the user login.
        rule_id: The rule's id.
        removed_by: Who removes it, as `created_by` in `add`.

    Returns:
        The removed rule; None if no rule with that id is active (unknown, removed or
        expired).

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "WITH gone AS (UPDATE temporary_rules SET removed_at = now(), removed_by = %s"
        " WHERE id = %s AND removed_at IS NULL AND expires_at > now() RETURNING *)"
        " SELECT gone.id, gone.text, gone.scope, p.name, gone.conversation,"
        " gone.lifts_builtin, gone.created_at, gone.expires_at, gone.created_by, gone.origin,"
        " gone.removed_at, gone.removed_by FROM gone LEFT JOIN projects p"
        " ON p.id = gone.project_id",
        (removed_by, rule_id),
    )
    return _rule(row) if row else None


PROMPT_HEADING = "# Temporary rules"

PROMPT_INTRO = (
    "The user added these rules for a limited time. They have the weight of the user's "
    "instructions above; where they contradict them, the temporary rule wins until it expires."
)


def prompt_part(rules: Sequence[TemporaryRule], now: datetime) -> str:
    """Render the rules that count at `now` for a checker's prompt.

    Args:
        rules: The session's rules (`active`), possibly read some time ago.
        now: The present time; a rule that expired since it was read is left out.

    Returns:
        The heading, `PROMPT_INTRO` and one line per rule ("- (until 2026-11-29 14:00 UTC)
        text"); "" when no rule counts.
    """
    counting = [rule for rule in rules if rule.active(now)]
    if not counting:
        return ""
    lines = [PROMPT_HEADING, "", PROMPT_INTRO, ""]
    for rule in counting:
        until = rule.expires_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"- (until {until}) {' '.join(rule.text.split())}")
    return "\n".join(lines) + "\n"
