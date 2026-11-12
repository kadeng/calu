"""Changing what steers the checkers: projects, configuration, checker definitions and rules.

These are the operations behind `calu project`, `calu config`, `calu checker` and `calu rules`
(PLAN.md §8). They need the `admin` credential, because the role of a session may only read the
configuration (PLAN.md D6, §5): a running session cannot change the rules it is judged by, and
neither can the agent.

Every change announces itself on `config_changed`, so a running session reloads within a
moment (`calu.store.ConfigStore.reload_loop`), and the audit trigger records who changed what.

Validation happens before the write: a configuration value must fit `Config`, a rule pattern
must compile, and a rule list must be one of the six. A typo would otherwise reach every
session — an invalid rule list makes every checked step ask the human.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError

from calu.checkers.rules import RULE_LISTS
from calu.config import (
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_NETWORK_POLICY,
    SETTINGS_KEY,
    Config,
    merge_settings,
)
from calu.db import Db
from calu.setup import RuleRow as SetupRuleRow
from calu.setup import parse_rule_file, recommended_rules

NETWORK_POLICIES = ("none", "proxy", "full")
"""Values of `projects.network_policy` (PLAN.md §7)."""

ROUTES = ("command", "edit", "subagent", "narrator")
"""Values of `checker_types.route`."""


class AdminError(Exception):
    """A change was refused; the message explains what is wrong with the request."""


@dataclass(frozen=True)
class ProjectRow:
    """One row of `projects`."""

    id: int
    name: str
    git_remote: str | None
    network_policy: str

    def line(self) -> str:
        """Return the project as one terminal line.

        Returns:
            Name, network policy and git remote.
        """
        remote = f"  {self.git_remote}" if self.git_remote else ""
        return f"{self.name}  network {self.network_policy}{remote}"


@dataclass(frozen=True)
class CheckerRow:
    """One row of `checker_types` without its instructions."""

    name: str
    route: str
    model: str
    request_limit: int
    output_tokens_limit: int
    max_tokens: int
    enabled: bool
    version: int

    def line(self) -> str:
        """Return the checker as one terminal line.

        Returns:
            Name, route, model, version and whether it is enabled.
        """
        state = "" if self.enabled else "  (disabled)"
        return (
            f"{self.name}  route {self.route}  {self.model}  v{self.version}"
            f"  limits {self.request_limit}/{self.output_tokens_limit}/{self.max_tokens}{state}"
        )


@dataclass(frozen=True)
class RuleRow:
    """One row of `rules`."""

    id: int
    scope: str
    list: str
    pattern: str
    comment: str
    position: int
    enabled: bool

    def line(self) -> str:
        """Return the rule as one terminal line.

        Returns:
            Id, scope, list, pattern and comment.
        """
        state = "" if self.enabled else " (disabled)"
        comment = f"  # {self.comment}" if self.comment else ""
        return f"{self.id:>5}  {self.scope:<8} {self.list:<13} {self.pattern}{state}{comment}"


# ------------------------------------------------------------------------------------ projects


async def add_project(
    db: Db,
    name: str,
    git_remote: str | None = None,
    network_policy: str = DEFAULT_NETWORK_POLICY,
) -> ProjectRow:
    """Register a project, so that its own configuration and rules can be set.

    Nothing is written into the project directory: a project is a row that `calu run --project`
    refers to.

    With the "proxy" policy and no allow list of its own, the project is given
    `DEFAULT_ALLOWED_DOMAINS` as its starter list (PLAN.md D45). That is written into the
    project's settings rather than applied silently at load time, so `calu config get --project`
    shows it and `calu config add|remove claude.sandbox.allowed_domains` edits it. Without it
    "proxy" would mean the same as "none" -- an empty allow list reaches nothing -- and every
    package install in a new project would fail.

    Args:
        db: A connection of the database owner.
        name: Name of the project, unique in the database.
        git_remote: Its git remote, for the record.
        network_policy: One of `NETWORK_POLICIES`.

    Returns:
        The new row.

    Raises:
        AdminError: If the name is empty, the policy is unknown, or the name exists.
        psycopg.Error: On database errors.
    """
    if not name.strip():
        raise AdminError("a project needs a name")
    if network_policy not in NETWORK_POLICIES:
        raise AdminError(f"network policy must be one of {', '.join(NETWORK_POLICIES)}")
    row = await db.one(
        "INSERT INTO projects (name, git_remote, network_policy) VALUES (%s, %s, %s)"
        " ON CONFLICT (name) DO NOTHING RETURNING id, name, git_remote, network_policy",
        (name.strip(), git_remote, network_policy),
    )
    if row is None:
        raise AdminError(f"a project named {name!r} exists already")
    project = ProjectRow(*row)
    if project.network_policy == "proxy":
        await set_setting(
            db,
            "claude.sandbox.allowed_domains",
            list(DEFAULT_ALLOWED_DOMAINS),
            project=project.name,
        )
    return project


async def chat_summary(db: Db, session_id: str) -> str:
    """Read the chat summary of one session (PLAN.md D40).

    Args:
        db: The connection.
        session_id: Id of the `sessions` row.

    Returns:
        The stored summary, or "" if that session has none yet (it is written when the first
        turns are folded).

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "SELECT summary FROM chat_summaries WHERE session_id = %s::uuid", (session_id,)
    )
    return str(row[0]) if row and row[0] else ""


async def set_chat_summary(db: Db, session_id: str, summary: str) -> None:
    """Replace the chat summary of one session and tell its session process (PLAN.md D40).

    The summary is the conversation the checkers read for everything older than the unfolded
    turns, so correcting it changes what they are told — which is the point. The session picks
    it up through the `config_changed` notification this function sends, and applies it at its
    next check; the number of covered turns is left as it is, so the next folding continues
    where the summarizer left off.

    Args:
        db: A connection with write access to `chat_summaries` (the owner).
        session_id: Id of the `sessions` row.
        summary: The new text.

    Raises:
        AdminError: If no session has that id, or the text is empty — a summary of nothing
            would silently hide the folded turns from the checkers, which is why the message
            says what to write instead (PLAN.md D114).
        psycopg.Error: On database errors.
    """
    if not summary.strip():
        raise AdminError(
            "the chat summary may not be empty: it covers turns that are no longer in the "
            "recent window, so an empty text would hide them from the checkers instead of "
            "summarizing them. Replace it with what the checkers should know -- or with "
            '"No turns have been folded yet." if the answer is nothing'
        )
    row = await db.one(
        "INSERT INTO chat_summaries (session_id, summary, covered_turns)"
        " SELECT s.id, %s, 0 FROM sessions s WHERE s.id = %s::uuid"
        " ON CONFLICT (session_id) DO UPDATE SET summary = excluded.summary,"
        " updated_at = now() RETURNING session_id",
        (summary, session_id),
    )
    if row is None:
        raise AdminError(f"no session {session_id}")
    await db.execute(
        "SELECT pg_notify('config_changed', json_build_object('table', 'chat_summaries')::text)"
    )


async def set_network_policy(db: Db, name: str, network_policy: str) -> ProjectRow:
    """Change what a project's sandboxed commands may reach (PLAN.md D39).

    A running session applies it at its **next start**, not at its next step: the policy becomes
    part of the sandbox that Claude Code was started with.

    Args:
        db: A connection with write access to `projects` (the owner).
        name: The project's name.
        network_policy: One of `NETWORK_POLICIES` — "none" (nothing reachable), "proxy" (only
            the configured `claude.sandbox.allowed_domains`) or "full" (everything except
            `denied_domains`).

    Returns:
        The project row as it is now.

    Raises:
        AdminError: If the policy is not one of `NETWORK_POLICIES`, or no project has that name.
        psycopg.Error: On database errors.
    """
    if network_policy not in NETWORK_POLICIES:
        raise AdminError(
            f"network policy must be one of {', '.join(NETWORK_POLICIES)}, not {network_policy!r}"
        )
    row = await db.one(
        "UPDATE projects SET network_policy = %s WHERE name = %s"
        " RETURNING id, name, git_remote, network_policy",
        (network_policy, name.strip()),
    )
    if row is None:
        raise AdminError(f"no project {name!r}")
    return ProjectRow(*row)


async def projects(db: Db) -> list[ProjectRow]:
    """List the registered projects by name.

    Args:
        db: A connection that may read `projects`.

    Returns:
        The projects, in name order.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all("SELECT id, name, git_remote, network_policy FROM projects ORDER BY name")
    return [ProjectRow(*row) for row in rows]


async def project_id(db: Db, name: str | None) -> int | None:
    """Look up a project's row id.

    Args:
        db: A connection that may read `projects`.
        name: The project's name, or None for the global scope.

    Returns:
        The id, or None if `name` is None.

    Raises:
        AdminError: If no project has that name.
        psycopg.Error: On database errors.
    """
    if name is None:
        return None
    row = await db.one("SELECT id FROM projects WHERE name = %s", (name,))
    if row is None:
        raise AdminError(f"no project named {name!r}; add it with `calu project add`")
    return int(row[0])


# ------------------------------------------------------------------------------- configuration


async def settings(db: Db, project: str | None = None) -> dict[str, Any]:
    """Read the stored settings of one scope (not merged with the other scopes).

    Args:
        db: A connection that may read `config`.
        project: The project whose settings to read, or None for the global ones.

    Returns:
        The stored partial configuration; `{}` if the scope has no row.

    Raises:
        AdminError: If no project has that name.
        psycopg.Error: On database errors.
    """
    if project is not None:
        await project_id(db, project)  # fail early on a typo
    row = await db.one(
        "SELECT value FROM config WHERE scope = %s AND scope_id = %s AND key = %s",
        ("project" if project else "global", project or "", SETTINGS_KEY),
    )
    value = row[0] if row else {}
    return dict(value) if isinstance(value, dict) else {}


def parse_value(text: str) -> Any:
    """Turn a command-line value into the JSON value that is stored.

    Args:
        text: The value as typed, e.g. "opus", "3", "true", "null", '["a", "b"]'.

    Returns:
        The parsed JSON value, or the text itself if it is not valid JSON (so a plain string
        needs no quotes).
    """
    try:
        return json.loads(text)
    except ValueError:
        return text


def _nest(key: str, value: Any) -> dict[str, Any]:
    """Build the nested dict of a dotted key.

    Args:
        key: A dotted path, e.g. "claude.model".
        value: The value at that path.

    Returns:
        E.g. `{"claude": {"model": value}}`.

    Raises:
        AdminError: If the key is empty or has an empty part.
    """
    parts = key.split(".")
    if not key or any(not part for part in parts):
        raise AdminError(f"not a configuration key: {key!r}")
    nested: Any = value
    for part in reversed(parts):
        nested = {part: nested}
    return nested


def _drop(current: dict[str, Any], key: str) -> dict[str, Any]:
    """Return `current` without the value at a dotted key.

    Args:
        current: The stored settings.
        key: A dotted path.

    Returns:
        A copy without that key; unchanged if the key is not set.

    Raises:
        AdminError: If the key is empty or has an empty part.
    """
    parts = key.split(".")
    if not key or any(not part for part in parts):
        raise AdminError(f"not a configuration key: {key!r}")
    result = json.loads(json.dumps(current))
    node = result
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return result
        node = child
    node.pop(parts[-1], None)
    return result


async def set_setting(db: Db, key: str, value: Any, project: str | None = None) -> dict[str, Any]:
    """Set one configuration value of one scope.

    The new settings of the scope are validated against `Config` before they are written, so a
    value that no session could use is refused here.

    Args:
        db: A connection of the database owner.
        key: A dotted path into the configuration, e.g. "claude.model" or "context.narrate".
        value: The value to store (see `parse_value`).
        project: The project whose settings to change, or None for the global ones.

    Returns:
        The stored settings of that scope after the change.

    Raises:
        AdminError: If the key is malformed, the project is unknown, or the result does not fit
            `Config`.
        psycopg.Error: On database errors.
    """
    current = await settings(db, project)
    merged = merge_settings(current, _nest(key, value))
    _validate(merged)
    await _write_settings(db, merged, project)
    return merged


def effective_value(current: dict[str, Any], key: str) -> Any:
    """Return the value a session would use for `key` in a scope.

    This is not the same as what the scope *stores*: a key that was never set is absent from
    the stored settings but has a default in `Config`. A list is changed relative to what the
    session really uses, so adding one entry to `claude.sandbox.deny_read` must extend the
    default list instead of replacing it with a single entry.

    Args:
        current: The stored settings of the scope, as `settings` returns them.
        key: A dotted path into the configuration, e.g. "claude.sandbox.deny_read".

    Returns:
        The value from the stored settings where there is one, else the default of `Config`.

    Raises:
        AdminError: If the key names nothing in `Config`, or leads through a value that is not
            a model (e.g. "claude.model.x").
    """
    node: Any = Config.model_validate(current)
    walked: list[str] = []
    for part in key.split("."):
        if not part:
            raise AdminError(f"{key!r} is not a configuration key")
        walked.append(part)
        if isinstance(node, BaseModel):
            if part not in type(node).model_fields:
                raise AdminError(f"the configuration has no {'.'.join(walked)}")
            node = getattr(node, part)
            continue
        if isinstance(node, dict):
            node = node.get(part)
            continue
        raise AdminError(f"{'.'.join(walked[:-1])} is not a group of settings")
    return node


def _list_at(current: dict[str, Any], key: str) -> list[Any]:
    """Return the list a scope effectively uses for `key`.

    Args:
        current: The stored settings of the scope.
        key: A dotted path into the configuration.

    Returns:
        A copy of the list, so the caller may change it.

    Raises:
        AdminError: If the key is unknown or does not hold a list.
    """
    found = effective_value(current, key)
    if not isinstance(found, list):
        kind = type(found).__name__
        raise AdminError(
            f"{key} is not a list but a {kind}; use `calu config set {key} <value>` for it"
        )
    return list(found)


async def add_to_setting(
    db: Db, key: str, value: Any, project: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Add one entry to a list-valued setting, keeping the entries it already has.

    The entry is appended to the list the scope **effectively** uses, which is the default list
    of `Config` when the scope stores nothing for the key -- so adding one denied path does not
    throw the built-in ones away. Adding an entry that is already there changes nothing.

    Args:
        db: A connection of the database owner.
        key: A dotted path to a list, e.g. "claude.sandbox.deny_read".
        value: The entry to add (see `parse_value`).
        project: The project whose settings to change, or None for the global ones.

    Returns:
        The stored settings of that scope after the change, and whether anything was added.

    Raises:
        AdminError: If the key is unknown or not a list, the project is unknown, or the result
            does not fit `Config`.
        psycopg.Error: On database errors.
    """
    current = await settings(db, project)
    entries = _list_at(current, key)
    if value in entries:
        return current, False
    entries.append(value)
    return await set_setting(db, key, entries, project), True


async def remove_from_setting(
    db: Db, key: str, value: Any, project: str | None = None
) -> dict[str, Any]:
    """Remove one entry from a list-valued setting, keeping the rest.

    Removing an entry that is only in the default list is allowed: the scope then stores the
    default list without that entry, which is what "remove it" means to a user.

    Args:
        db: A connection of the database owner.
        key: A dotted path to a list, e.g. "claude.sandbox.deny_read".
        value: The entry to remove (see `parse_value`).
        project: The project whose settings to change, or None for the global ones.

    Returns:
        The stored settings of that scope after the change.

    Raises:
        AdminError: If the key is unknown or not a list, the entry is not in the list, the
            project is unknown, or the result does not fit `Config`.
        psycopg.Error: On database errors.
    """
    current = await settings(db, project)
    entries = _list_at(current, key)
    if value not in entries:
        raise AdminError(f"{value!r} is not in {key}; it holds {entries!r}")
    entries.remove(value)
    return await set_setting(db, key, entries, project)


async def unset_setting(db: Db, key: str, project: str | None = None) -> dict[str, Any]:
    """Remove one configuration value of one scope, so the default applies again.

    Args:
        db: A connection of the database owner.
        key: A dotted path into the configuration.
        project: The project whose settings to change, or None for the global ones.

    Returns:
        The stored settings of that scope after the change.

    Raises:
        AdminError: If the key is malformed or the project is unknown.
        psycopg.Error: On database errors.
    """
    current = await settings(db, project)
    remaining = _drop(current, key)
    _validate(remaining)
    await _write_settings(db, remaining, project)
    return remaining


def _validate(values: dict[str, Any]) -> None:
    """Check that a partial configuration fits `Config`.

    Args:
        values: The settings to store.

    Raises:
        AdminError: If the values do not validate; the message names the field.
    """
    try:
        Config.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise AdminError(f"the configuration would be invalid: {problems}") from exc


async def _write_settings(db: Db, values: dict[str, Any], project: str | None) -> None:
    """Store the settings of one scope, replacing what was there.

    Args:
        db: A connection of the database owner.
        values: The settings of the scope.
        project: The project, or None for the global scope.

    Raises:
        psycopg.Error: On database errors.
    """
    await db.execute(
        "INSERT INTO config (scope, scope_id, key, value) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (scope, scope_id, key) DO UPDATE SET value = excluded.value",
        ("project" if project else "global", project or "", SETTINGS_KEY, json.dumps(values)),
    )


# --------------------------------------------------------------------------- checker definitions


async def checkers(db: Db) -> list[CheckerRow]:
    """List the checker definitions by name.

    Args:
        db: A connection that may read `checker_types`.

    Returns:
        The definitions without their instructions, in name order.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT name, route, model, request_limit, output_tokens_limit, max_tokens, enabled,"
        " version FROM checker_types ORDER BY name"
    )
    return [CheckerRow(*row) for row in rows]


async def instructions(db: Db, name: str) -> str:
    """Read the instructions of one checker type.

    Args:
        db: A connection that may read `checker_types`.
        name: The checker type's name.

    Returns:
        Its Markdown instructions.

    Raises:
        AdminError: If no checker type has that name.
        psycopg.Error: On database errors.
    """
    row = await db.one("SELECT instructions FROM checker_types WHERE name = %s", (name,))
    if row is None:
        raise AdminError(f"no checker named {name!r}")
    return str(row[0])


async def set_checker(
    db: Db,
    name: str,
    instructions_text: str | None = None,
    model: str | None = None,
    enabled: bool | None = None,
    request_limit: int | None = None,
    output_tokens_limit: int | None = None,
    max_tokens: int | None = None,
    route: str | None = None,
) -> CheckerRow:
    """Change one checker definition, or create it with `route`.

    The database raises the definition's version on every change, so a `checker_runs` row
    records which version decided. A running session applies the new instructions at its next
    check.

    Args:
        db: A connection of the database owner.
        name: The checker type's name ("command", "edit", "subagent-result", "narrator", ...).
        instructions_text: New Markdown instructions, or None to keep them.
        model: New Pydantic AI model string, or None to keep it.
        enabled: Whether the checker is used, or None to keep it.
        request_limit: New limit, or None to keep it.
        output_tokens_limit: New limit, or None to keep it.
        max_tokens: New limit, or None to keep it.
        route: Route of a checker that does not exist yet (`ROUTES`); for an existing checker
            it changes the route.

    Returns:
        The definition after the change.

    Raises:
        AdminError: If the checker does not exist and no route was given, if the route is
            unknown, if the instructions are empty, or if nothing was given to change.
        psycopg.Error: On database errors.
    """
    if route is not None and route not in ROUTES:
        raise AdminError(f"route must be one of {', '.join(ROUTES)}")
    if instructions_text is not None and not instructions_text.strip():
        raise AdminError("the instructions must not be empty")
    exists = await db.one("SELECT 1 FROM checker_types WHERE name = %s", (name,))
    if exists is None:
        if route is None or instructions_text is None:
            raise AdminError(
                f"checker {name!r} does not exist; create it with a route and instructions"
            )
        await db.execute(
            "INSERT INTO checker_types (name, route, instructions, model) VALUES (%s, %s, %s, %s)",
            (name, route, instructions_text, model or ""),
        )
        instructions_text, model, route = None, None, None
    changes = {
        "instructions": instructions_text,
        "model": model,
        "enabled": enabled,
        "request_limit": request_limit,
        "output_tokens_limit": output_tokens_limit,
        "max_tokens": max_tokens,
        "route": route,
    }
    given = {column: value for column, value in changes.items() if value is not None}
    if given:
        # The column names come from the dict above, never from user input.
        assignments = ", ".join(f"{column} = %s" for column in given)
        await db.execute(
            f"UPDATE checker_types SET {assignments} WHERE name = %s",
            (*given.values(), name),
        )
    found = [row for row in await checkers(db) if row.name == name]
    if not found:
        raise AdminError(f"no checker named {name!r}")
    return found[0]


# ------------------------------------------------------------------------------------- rules


async def rules(db: Db, project: str | None = None, rule_list: str | None = None) -> list[RuleRow]:
    """List the rules of the global scope and of one project.

    Args:
        db: A connection that may read `rules`.
        project: Also show the rules of this project, or None for the global ones only.
        rule_list: Only this list (one of `RULE_LISTS`), or None for all.

    Returns:
        The rules, global first, then the project's, each by position and id.

    Raises:
        AdminError: If the project or the list name is unknown.
        psycopg.Error: On database errors.
    """
    if rule_list is not None and rule_list not in RULE_LISTS:
        raise AdminError(f"rule list must be one of {', '.join(RULE_LISTS)}")
    pid = await project_id(db, project)
    rows = await db.all(
        "SELECT r.id, coalesce(p.name, 'global'), r.list, r.pattern, r.comment, r.position,"
        " r.enabled FROM rules r LEFT JOIN projects p ON p.id = r.project_id"
        " WHERE (r.project_id IS NULL OR r.project_id = %s)"
        "   AND (%s::text IS NULL OR r.list = %s)"
        " ORDER BY (r.project_id IS NOT NULL), r.list, r.position, r.id",
        (pid, rule_list, rule_list),
    )
    return [RuleRow(*row) for row in rows]


RULE_FILE_HEADER = """\
# Rules of the list {list} ({scope} scope), one regular expression per line.
#
# A comment block directly above a pattern (no blank line between them) becomes that pattern's
# comment; this header is dropped, because a blank line follows it. Patterns are matched with
# `re.search`, so anchor them with ^ and $. The order of the lines is the order of the rules.
# Saving an empty file removes every rule of this list in this scope.
"""
"""Header of the file that `calu rules edit` opens; `parse_rule_file` drops it."""


def render_rule_list(rows: Sequence[RuleRow], rule_list: str, scope: str) -> str:
    """Render the rules of one list as the text that an editor opens.

    Args:
        rows: The rules of that list, in order.
        rule_list: The list's name, for the header.
        scope: "global" or the project's name, for the header.

    Returns:
        The header, then one block per rule: its comment lines, then the pattern.
    """
    blocks = [RULE_FILE_HEADER.format(list=rule_list, scope=scope)]
    for row in rows:
        comment = f"# {row.comment}\n" if row.comment else ""
        blocks.append(f"{comment}{row.pattern}\n")
    return "\n".join(blocks)


def rule_list_text(rows: Sequence[RuleRow]) -> str:
    """Render the rules of one list as the text the web interface edits (PLAN.md D137).

    The format of `calu rules edit` (`render_rule_list`) without its explanatory header, which
    the page states once above all lists: one pattern per line, and a rule's comment as a
    `# ...` line directly above it. A blank line goes before each comment so that the lists
    stay readable; `calu.setup.parse_rule_file` reads it back to the same rules.

    Args:
        rows: The enabled rules of one list in one scope, in order.

    Returns:
        The text, ending with a newline, or "" for an empty list.
    """
    lines: list[str] = []
    for row in rows:
        if row.comment:
            if lines:
                lines.append("")
            lines.append(f"# {row.comment}")
        lines.append(row.pattern)
    return "\n".join(lines) + "\n" if lines else ""


async def replace_rule_list(
    db: Db, rule_list: str, text: str, project: str | None = None
) -> tuple[int, int]:
    """Replace the enabled rules of one list in one scope with the rules of `text`.

    Every pattern is compiled before anything is deleted, so a typo leaves the list as it was.
    Disabled rules of that list are not touched and are not part of `text`.

    Args:
        db: A connection of the database owner.
        rule_list: One of `RULE_LISTS`.
        text: The list in the line format of `calu.setup.parse_rule_file`.
        project: The project the rules belong to, or None for every project.

    Returns:
        (number of rules removed, number of rules stored).

    Raises:
        AdminError: If the list name is unknown, a pattern does not compile, or the project is
            unknown.
        psycopg.Error: On database errors.
    """
    if rule_list not in RULE_LISTS:
        raise AdminError(f"rule list must be one of {', '.join(RULE_LISTS)}")
    parsed = parse_rule_file(rule_list, text)
    for rule in parsed:
        try:
            re.compile(rule.pattern)
        except re.error as exc:
            raise AdminError(f"not a valid regular expression `{rule.pattern}`: {exc}") from exc
    pid = await project_id(db, project)
    async with db.exclusive() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                b"DELETE FROM rules WHERE list = %s AND enabled"
                b" AND project_id IS NOT DISTINCT FROM %s",
                (rule_list, pid),
            )
            removed = cur.rowcount
            for rule in parsed:
                await conn.execute(
                    b"INSERT INTO rules (project_id, list, pattern, comment, position)"
                    b" VALUES (%s, %s, %s, %s, %s)",
                    (pid, rule_list, rule.pattern, rule.comment, rule.position),
                )
    return removed, len(parsed)


RULE_SETS: dict[str, tuple[str, str]] = {
    "recommended": (
        "all",
        "reads of project files run without a model call; sudo, force pushes, remote "
        "deletions, reads of credentials and writes to .git, .claude and CLAUDE.md ask. No "
        "command is allowed by it",
    ),
    "publishing": (
        "command-deny",
        "any git push, cluster and infrastructure changes, cloud deploys, package and image"
        " uploads, GitHub releases and merges",
    ),
}
"""Ready-made rule sets, none of them in the (empty) default lists: name -> (list, what it
covers). A list of "all" means the set spans every list: `recommended` is the six files
`calu/defaults/rules/<list>.txt`, the defaults before D179. Every other set is the file
`calu/defaults/rules/sets/<name>.txt` for one list. Adding a set is one command, and afterwards
its rules are ordinary rules that can be removed one by one (D143, D179)."""


def rule_set(name: str) -> tuple[str, list[str], list[str]]:
    """Read a shipped rule set.

    Args:
        name: One of `RULE_SETS`.

    Returns:
        (the list it belongs to, its patterns in file order, the comment of each pattern).

    Raises:
        AdminError: If there is no set of that name.
    """
    if name not in RULE_SETS:
        raise AdminError(f"no rule set {name!r}; the sets are: {', '.join(RULE_SETS)}")
    rule_list = RULE_SETS[name][0]
    if rule_list == "all":
        raise AdminError(f"rule set {name!r} spans every list; use rule_set_rows")
    text = (
        resources.files("calu.defaults")
        .joinpath("rules", "sets", f"{name}.txt")
        .read_text(encoding="utf-8")
    )
    rows = parse_rule_file(rule_list, text)
    return rule_list, [row.pattern for row in rows], [row.comment for row in rows]


def rule_set_rows(name: str) -> list[SetupRuleRow]:
    """Read a shipped rule set, whatever lists it spans (D179).

    Args:
        name: One of `RULE_SETS`.

    Returns:
        Its rules, list by list, in file order.

    Raises:
        AdminError: If there is no set of that name.
    """
    if name not in RULE_SETS:
        raise AdminError(f"no rule set {name!r}; the sets are: {', '.join(RULE_SETS)}")
    if RULE_SETS[name][0] == "all":
        return recommended_rules()
    rule_list, patterns, comments = rule_set(name)
    return [
        SetupRuleRow(rule_list, pattern, comment, index)
        for index, (pattern, comment) in enumerate(zip(patterns, comments, strict=True))
    ]


async def add_rule_set(
    db: Db, name: str, project: str | None = None
) -> tuple[list[RuleRow], list[str]]:
    """Append a shipped rule set to its list, skipping the patterns the list already has.

    Running it twice adds nothing the second time, and a pattern someone already added by hand
    is not duplicated. A pattern that was **disabled** counts as present: re-adding it would
    undo a person's decision to switch it off.

    Args:
        db: A connection of the database owner.
        name: One of `RULE_SETS`.
        project: The project whose list gets the rules, or None for the global list.

    Returns:
        (the rules added, the patterns skipped because the list already had them).

    Raises:
        AdminError: If the set or the project is unknown.
        psycopg.Error: On database errors.
    """
    rows = rule_set_rows(name)
    pid = await project_id(db, project)
    present = {
        (row[0], row[1])
        for row in await db.all(
            "SELECT list, pattern FROM rules WHERE project_id IS NOT DISTINCT FROM %s", (pid,)
        )
    }
    added: list[RuleRow] = []
    skipped: list[str] = []
    for row in rows:
        if (row.list, row.pattern) in present:
            skipped.append(row.pattern)
            continue
        added.append(await add_rule(db, row.list, row.pattern, row.comment, project))
    return added, skipped


async def add_rule(
    db: Db,
    rule_list: str,
    pattern: str,
    comment: str = "",
    project: str | None = None,
    position: int | None = None,
) -> RuleRow:
    """Add one rule to an allow or deny list.

    The pattern is compiled before it is stored: an invalid pattern would otherwise make every
    checked step ask the human until someone fixes it.

    Args:
        db: A connection of the database owner.
        rule_list: One of `RULE_LISTS`.
        pattern: A Python regular expression, matched with `re.search`.
        comment: Why the rule exists.
        project: The project the rule belongs to, or None for every project.
        position: Position in its list; None appends it.

    Returns:
        The new rule.

    Raises:
        AdminError: If the list name is unknown, the pattern does not compile, or the project
            is unknown.
        psycopg.Error: On database errors.
    """
    if rule_list not in RULE_LISTS:
        raise AdminError(f"rule list must be one of {', '.join(RULE_LISTS)}")
    if not pattern.strip():
        raise AdminError("a rule needs a pattern")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise AdminError(f"not a valid regular expression: {exc}") from exc
    pid = await project_id(db, project)
    if position is None:
        row = await db.one(
            "SELECT coalesce(max(position) + 1, 0) FROM rules"
            " WHERE list = %s AND project_id IS NOT DISTINCT FROM %s",
            (rule_list, pid),
        )
        position = int(row[0]) if row else 0
    new = await db.one(
        "INSERT INTO rules (project_id, list, pattern, comment, position)"
        " VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id, %s, list, pattern, comment, position, enabled",
        (pid, rule_list, pattern, comment, position, project or "global"),
    )
    if new is None:
        raise AdminError("the rule was not stored")
    return RuleRow(*new)


async def set_rule(db: Db, rule_id: int, enabled: bool) -> bool:
    """Switch one rule on or off.

    Args:
        db: A connection of the database owner.
        rule_id: Id of the rule.
        enabled: Whether the rule applies.

    Returns:
        True if the rule exists.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "UPDATE rules SET enabled = %s WHERE id = %s RETURNING id", (enabled, rule_id)
    )
    return row is not None


async def remove_rule(db: Db, rule_id: int) -> bool:
    """Delete one rule.

    Args:
        db: A connection of the database owner.
        rule_id: Id of the rule.

    Returns:
        True if the rule existed.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one("DELETE FROM rules WHERE id = %s RETURNING id", (rule_id,))
    return row is not None


MODEL_KEYS = (
    "command_checker.model",
    "edit_checker.model",
    "changeset_checker.model",
    "subagent_checker.model",
    "context.narrator_model",
    "context.summarizer_model",
)
"""Every configuration key that names a model a session calls (`Config.with_model`)."""

EFFORT_KEYS = (
    "command_checker.effort",
    "edit_checker.effort",
    "changeset_checker.effort",
    "subagent_checker.effort",
)
"""The checkers' effort keys; the narrator and the summarizer take no effort."""


async def set_all_models(
    db: Db, model: str, effort: str | None = None, project: str | None = None
) -> list[str]:
    """Point every model role at one model, in one scope (PLAN.md D129).

    Two places hold models, and a model set in only one of them is not what runs: the
    configuration's role keys (`MODEL_KEYS`, which win over a definition, D51) and the checker
    **definition** rows, which are the only source for an extra checker on a route such as
    `honesty` (D81). In the global scope both are changed; a project scope has no definitions of
    its own, so there only the keys are.

    The effort is set on the checkers when one is given, and **unset** otherwise: a level chosen
    for one model must not silently carry over to another -- "medium" set for Haiku would reach
    Nemotron as a thinking setting it does not have.

    Args:
        db: A connection of the database owner.
        model: The Pydantic AI model string, or "" to store none, so that every role uses the
            default model (`calu.config.default_model`).
        effort: The checkers' effort, or None to remove any that is set.
        project: The project's scope, or None for the global one.

    Returns:
        One line per change, for the report.

    Raises:
        AdminError: If the project does not exist or a value is refused.
        psycopg.Error: On database errors.
    """
    changed: list[str] = []
    for key in MODEL_KEYS:
        if model:
            await set_setting(db, key, model, project)
            changed.append(f"{key} = {model}")
        else:
            await unset_setting(db, key, project)
            changed.append(f"{key}: the default")
    for key in EFFORT_KEYS:
        if effort is None:
            await unset_setting(db, key, project)
        else:
            await set_setting(db, key, effort, project)
    changed.append(
        f"checker effort = {effort}" if effort else "checker effort: the provider's default"
    )
    if project is None:
        for row in await checkers(db):
            if row.model != model:
                await set_checker(db, row.name, model=model)
                changed.append(f"checker definition {row.name}: model = {model or 'the default'}")
    return changed
