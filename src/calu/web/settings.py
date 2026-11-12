"""The settings page: everything that steers a session, as one editable page model.

Three stores hold what a human configures, and the page shows them together:

    config rows            every value of `calu.config.Config`, per scope (global or a project)
    checker_types rows     each checker's instructions, and for an extra checker its model
    rules rows             the six allow and deny lists, per scope

`load` reads them, `panes` turns them into the page model below, and `save` writes back what a
submitted form changed, each value through the same `calu.admin` function the command line uses.

    Pane      one screen of the page, chosen in the sidebar       e.g. "Command checker"
      Group   a titled card inside it                             e.g. "Model and limits"
        Field one control                                         e.g. `command_checker.model`

The configuration fields are generated from the model, so a new setting appears without a change
here: a field of a known section lands in that section's pane, and a new section gets a pane of
its own. Only the arrangement (which section belongs to which pane) is written down.

A field's form name says where it is stored:

    claude.model                 a configuration value (the key `calu config set` takes)
    prompt:command               the instructions of the checker type `command`
    checker:honesty:model        a column of the checker type `honesty`
    rules:command-deny           one rule list of the scope
"""

import json
import re
import types
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from calu import admin, herd
from calu.checkers.rules import RULE_LISTS
from calu.config import CheckerConfig, Config
from calu.db import Db
from calu.defaults.instructions import SYSTEM_PARTS, honesty_rules, system_part

Kind = Literal[
    "bool",
    "choice",
    "int",
    "number",
    "text",
    "model",
    "lines",
    "json",
    "markdown",
    "prompt",
    "rules",
]
"""Which control a field gets in the browser."""

BLOCK_KINDS = frozenset({"lines", "json", "markdown", "prompt", "rules"})
"""Kinds whose control is an editor below the label, as wide as the pane."""

MODEL_SUGGESTIONS = ("fable", "opus", "sonnet", "haiku")
"""Claude Code's model aliases, offered next to the free-text field (`claude.model`)."""

MODEL_OPTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Anthropic API",
        (
            ("anthropic:claude-haiku-5-5", "Claude Haiku 5.5"),
            ("anthropic:claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("anthropic:claude-opus-5-5", "Claude Opus 5.5"),
        ),
    ),
    (
        "AWS Bedrock",
        (
            ("bedrock:anthropic.claude-haiku-5-5", "Claude Haiku 5.5"),
            ("bedrock:anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("bedrock:anthropic.claude-opus-5-5", "Claude Opus 5.5"),
            (
                "bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                "Claude Haiku 4.5 (EU profile)",
            ),
        ),
    ),
    (
        "Fireworks",
        (
            (
                "fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
                "Nemotron Lightning 3.5 30B",
            ),
        ),
    ),
    (
        "Mistral",
        (
            ("mistral:mistral-small-2603", "Mistral Small 4"),
            ("mistral:mistral-medium-latest", "Mistral Medium"),
            ("mistral:ministral-3b-latest", "Ministral 3B"),
        ),
    ),
    (
        "This machine's Claude Code account (no API key, no tools)",
        (
            ("claude:haiku", "Haiku"),
            ("claude:sonnet", "Sonnet"),
            ("claude:opus", "Opus"),
        ),
    ),
)
"""The models offered for a checker, the narrator and the summarizer: (provider, ((Pydantic AI
model string, label), ...)). A suggestion list, not a limit: the field takes any model string.
The Bedrock entries are model ids; an account that reaches a model only through a regional
inference profile needs that profile's id instead (`calu config models auto` finds one)."""

MODELS_DOC = ("https://pydantic.dev/docs/ai/models/overview/", "Any model Pydantic AI supports")
"""Where the model strings are documented: (address, link text)."""

SKIP: frozenset[str] = frozenset()
"""Fields the form leaves out; none. `hidden_env` is editable here like every other list: it
holds the names of variables Claude Code must not see, never their values."""

MARKDOWN = frozenset({"honesty.rules"})
"""Text settings that hold a Markdown document and get an editor (PLAN.md D181)."""

ROUTE_CHECKERS = {
    "command": "command_checker",
    "edit": "edit_checker",
    "changeset": "changeset_checker",
    "subagent-result": "subagent_checker",
}
"""Checker type -> the configuration section that holds its model and limits. These four always
run; every other checker type is an extra one and carries its model in its own row."""

RULE_KINDS = (
    ("command", "Commands", "a command"),
    ("read", "File reads", "a path a tool reads"),
    ("write", "File writes", "a path a tool writes"),
)
"""The three kinds of rule list: (kind, pane title, what one pattern matches)."""

MISSES_READ = 40
"""How many of the newest approved-then-denied steps the page reads in all."""

MISSES_SHOWN = 5
"""How many of them one checker's pane shows."""

CHECKER_COLUMNS = ("enabled", "model", "request_limit", "output_tokens_limit", "max_tokens")
"""The columns of an extra checker type the page edits, in display order."""


@dataclass
class Field:
    """One control of the page.

    `value` is what the control shows: the value stored in this scope, else what the scope
    inherits (`base`). `base` is what "Reset" returns to: the model's default in the global
    scope, the global value in a project.
    """

    name: str
    """The form field name, which says where the value is stored (see the module docstring)."""
    label: str
    kind: Kind
    value: Any
    help: str = ""
    key: str = ""
    """What the command line calls this value, shown for reference; "" if it has no key."""
    choices: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    base: Any = None
    is_set: bool = False
    """Whether this scope stores the value itself (then the page offers "Reset")."""
    inherits: str = "default"
    """Where `base` comes from: "default" or "global"."""
    locked: str = ""
    """Why the field cannot be edited in this scope; "" if it can."""
    error: str = ""
    """Why the last save refused this field; "" if it did not."""
    raw: str | None = None
    """What was typed when a save refused the field, shown again instead of `value`."""
    meta: str = ""
    """A short fact shown beside the editor, e.g. "8 patterns" or "version 3"."""
    tone: str = ""
    """"allow" or "deny" for a rule list, which colours its editor; else ""."""
    extra_title: str = ""
    extra: str = ""
    """Read-only text shown folded under the editor: the fixed part calu puts in front of a
    checker's instructions, or the global rules that apply on top of a project's list."""
    placeholder: str = ""
    options: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    """For a model field: the offered models by provider, as `MODEL_OPTIONS`."""
    doc: tuple[str, str] | None = None
    """A page that documents the value: (address, link text); None for no link."""

    @property
    def block(self) -> bool:
        """Whether the control is an editor below the label.

        Returns:
            True for the kinds in `BLOCK_KINDS`.
        """
        return self.kind in BLOCK_KINDS

    @property
    def text(self) -> str:
        """Render the shown value for a text control.

        Returns:
            What was typed if the last save refused it, else the value: one item per line for a
            list, JSON for a dict, "" for None.
        """
        return self.raw if self.raw is not None else _text(self.kind, self.value)

    @property
    def base_text(self) -> str:
        """Render what "Reset" returns to, in the form the control holds it.

        Returns:
            `base` as the control's text; "true" or "false" for a switch.
        """
        if self.kind == "bool":
            return "true" if self.base else "false"
        return _text(self.kind, self.base)

    @property
    def base_label(self) -> str:
        """Describe what "Reset" returns to, for the button's tooltip.

        Returns:
            E.g. "the default value: high" or "the global value: (empty)".
        """
        shown = self.base_text if self.kind != "bool" else ("on" if self.base else "off")
        shown = " ".join(shown.split())
        if len(shown) > 60:
            shown = shown[:57] + "..."
        return f"the {self.inherits} value: {shown or '(empty)'}"

    @property
    def help_parts(self) -> tuple[str, str]:
        """Split the help into its first sentence and the rest.

        The page shows the first sentence and folds the rest: most descriptions explain a
        decision in several sentences.

        Returns:
            (first sentence, everything after it, possibly "").
        """
        text = " ".join(self.help.split())
        # Only at a full stop followed by a capital letter: a colon or a semicolon usually
        # introduces an example, and cutting there left half a code sample.
        for match in re.finditer(r"\.(\s+)(?=[A-Z])", text):
            head = text[: match.start() + 1]
            if head.endswith(("e.g.", "i.e.", "etc.", "vs.")):
                continue
            return head, text[match.end() :]
        return text, ""

    @property
    def search(self) -> str:
        """The text the page's search matches against.

        Returns:
            Label, key and help, lower case.
        """
        return f"{self.label} {self.key} {self.name} {self.help}".lower()


@dataclass
class Group:
    """A titled card of fields inside a pane."""

    title: str
    fields: list[Field] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class RuleSet:
    """A ready-made rule set offered on a rules pane."""

    name: str
    what: str


@dataclass
class Pane:
    """One screen of the settings page."""

    id: str
    """The URL fragment that opens the pane."""
    section: str
    """The sidebar heading it is listed under."""
    title: str
    description: str = ""
    groups: list[Group] = field(default_factory=list)
    rule_sets: list[RuleSet] = field(default_factory=list)
    facts: list[tuple[str, str, bool]] = field(default_factory=list)
    """The record shown on the pane, read-only: (label, value, whether to mark it as a
    problem). For a checker: what it decided, where a human disagreed, what it cost."""
    links: list[tuple[str, str, str]] = field(default_factory=list)
    """Other panes this one points to: (pane id, title, one-line summary)."""
    misses: list[herd.CheckerMiss] = field(default_factory=list)
    """Steps this pane's checker approved and a human then denied, newest first: the cases to
    correct its instructions against."""

    @property
    def fields(self) -> list[Field]:
        """Every field of the pane.

        Returns:
            The fields of all groups, in display order.
        """
        return [item for group in self.groups for item in group.fields]

    @property
    def set_count(self) -> int:
        """Count the fields this scope stores a value for.

        Returns:
            The number the sidebar shows beside the pane.
        """
        return sum(1 for item in self.fields if item.is_set and not item.locked)


@dataclass(frozen=True)
class Sources:
    """Everything `panes` reads, loaded from the database by `load`."""

    project: str | None
    values: Mapping[str, Any]
    """The configuration this scope stores (a nested dict, not merged with other scopes)."""
    inherited: Mapping[str, Any]
    """The global configuration when the scope is a project, else `{}`."""
    checkers: Sequence[admin.CheckerRow]
    instructions: Mapping[str, str]
    """Checker type name -> its instructions."""
    rules: Sequence[admin.RuleRow]
    """The rules of the global scope and, for a project, of that project."""
    stats: Mapping[str, herd.CheckerStats] = field(default_factory=dict)
    """Checker name -> what it decided so far, across every project."""
    misses: Sequence[herd.CheckerMiss] = ()
    """The newest steps a checker approved that a human then denied."""
    step_lines: Sequence[str] = ()
    """What `herd.StepStats.lines` says about all steps: paths through the gate and delays."""


@dataclass
class Saved:
    """What `save` did with a submitted form."""

    count: int = 0
    """How many values were written."""
    errors: dict[str, str] = field(default_factory=dict)
    """Form field name -> why its value was refused. Fields not named here were stored."""


async def load(db: Db, project: str | None, record: bool = True) -> Sources:
    """Read everything the settings page shows for one scope.

    Args:
        db: A connection that may read `config`, `checker_types`, `rules` and `projects`, and
            for the record `steps`, `checker_runs` and `escalations`.
        project: The project whose scope to show, or None for the global scope.
        record: Whether to read what the checkers decided so far as well; False where only
            the editable values are needed.

    Returns:
        The sources of the page.

    Raises:
        admin.AdminError: If no project has that name.
        psycopg.Error: On database errors.
    """
    checkers = await admin.checkers(db)
    return Sources(
        project=project,
        values=await admin.settings(db, project),
        inherited=await admin.settings(db) if project else {},
        checkers=checkers,
        instructions={row.name: await admin.instructions(db, row.name) for row in checkers},
        rules=await admin.rules(db, project),
        stats={row.checker_name: row for row in await herd.checker_stats(db)} if record else {},
        misses=await herd.checker_misses(db, limit=MISSES_READ) if record else (),
        step_lines=(await herd.step_stats(db)).lines() if record else (),
    )


def panes(src: Sources) -> list[Pane]:
    """Build the page model from its sources.

    Args:
        src: What `load` read.

    Returns:
        The panes in sidebar order: the checkers, the rule lists, Claude Code, then calu's own
        sections. Groups and panes without fields are left out.
    """
    used: set[str] = set()
    found = [
        *_checker_panes(src, used),
        *_rule_panes(src),
        *_claude_panes(src, used),
        *_session_panes(src, used),
    ]
    for pane in found:
        pane.groups = [group for group in pane.groups if group.fields]
    return [pane for pane in found if pane.groups or pane.facts or pane.links]


def overlay(found: Sequence[Pane], posted: Mapping[str, str], errors: Mapping[str, str]) -> None:
    """Mark the fields a save refused, keeping what was typed in them.

    Args:
        found: The page model, changed in place.
        posted: The submitted form.
        errors: Form field name -> why it was refused (`Saved.errors`).
    """
    for pane in found:
        for item in pane.fields:
            if item.name in errors:
                item.error = errors[item.name]
                if item.kind != "bool":
                    item.raw = posted.get(item.name, "").replace("\r\n", "\n")


async def save(
    db: Db,
    project: str | None,
    posted: Mapping[str, str],
    present: Collection[str] = (),
    resets: Collection[str] = (),
) -> Saved:
    """Write what a submitted form changed, one value at a time.

    Each field is compared with what the page showed, so an untouched field writes nothing and
    the audit log stays meaningful. A refused value does not stop the others: it is reported in
    `Saved.errors` and everything else is stored.

    Args:
        db: A connection of the database owner.
        project: The scope the form was shown for.
        posted: The form, by field name.
        present: The switches the form contained. A browser sends nothing for a switch that is
            off, so only a switch named here counts as "off" when it is missing; a form that
            leaves a switch out entirely does not change it.
        resets: Configuration keys whose stored value is to be removed from this scope, so the
            inherited value applies again.

    Returns:
        How many values were written and which fields were refused.

    Raises:
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    src = await load(db, project, record=False)
    result = Saved()
    checker_changes: dict[str, dict[str, Any]] = {}
    for pane in panes(src):
        for item in pane.fields:
            if item.locked:
                continue
            try:
                await _save_field(db, src, item, posted, present, resets, checker_changes, result)
            except (admin.AdminError, ValueError) as exc:
                result.errors[item.name] = _reason(exc)
    for name, columns in checker_changes.items():
        try:
            await admin.set_checker(db, name, **columns)
            result.count += len(columns)
        except (admin.AdminError, ValueError) as exc:
            for column in columns:
                result.errors[f"checker:{name}:{column}"] = _reason(exc)
    return result


async def _save_field(
    db: Db,
    src: Sources,
    item: Field,
    posted: Mapping[str, str],
    present: Collection[str],
    resets: Collection[str],
    checker_changes: dict[str, dict[str, Any]],
    result: Saved,
) -> None:
    """Write one field if the form changed it.

    Args:
        db: A connection of the database owner.
        src: The sources the page was built from.
        item: The field.
        posted: The form.
        present: The switches the form contained.
        resets: Configuration keys to remove from the scope.
        checker_changes: Collects the changed columns of extra checker types by name; the
            caller writes each checker once.
        result: Counts the writes.

    Raises:
        admin.AdminError: If the value is refused.
        ValueError: If the value cannot be parsed.
        psycopg.Error: On database errors.
    """
    if item.kind == "bool":
        if item.name not in present and item.name not in posted:
            return
        new: Any = item.name in posted
    elif item.name not in posted:
        return
    else:
        new = parse(item.kind, posted[item.name])
    space, _, rest = item.name.partition(":")
    if space == "prompt":
        if not new:
            raise ValueError("the instructions may not be empty")
        if new != str(item.value).replace("\r\n", "\n").strip():
            await admin.set_checker(db, rest, instructions_text=new)
            result.count += 1
    elif space == "rules":
        if new != item.value:
            await admin.replace_rule_list(db, rest, new, src.project)
            result.count += 1
    elif space == "checker":
        name, _, column = rest.partition(":")
        if new is None:
            raise ValueError("a value is needed")
        if new != item.value:
            checker_changes.setdefault(name, {})[column] = new
    elif item.name in resets or (item.name in MARKDOWN and new is None):
        if item.is_set:
            await admin.unset_setting(db, item.name, src.project)
            result.count += 1
    elif new != _comparable(item):
        await admin.set_setting(db, item.name, new, src.project)
        result.count += 1


def _comparable(item: Field) -> Any:
    """Return a field's shown value in the form `parse` yields, to tell a change from none.

    Args:
        item: A configuration field.

    Returns:
        The value as `parse(item.kind, item.text)` would return it; the value itself if its
        text cannot be parsed.
    """
    if item.kind == "bool":
        return bool(item.value)
    try:
        return parse(item.kind, _text(item.kind, item.value))
    except ValueError:
        return item.value


def parse(kind: Kind, raw: str) -> Any:
    """Turn one submitted field into the value its store expects.

    Args:
        kind: The control the field was rendered with.
        raw: What the browser sent.

    Returns:
        The parsed value; an empty text field becomes None, so that a field can be cleared back
        to what it inherits. A document ends with exactly one newline; a checker's instructions
        have none at their end.

    Raises:
        ValueError: If a number is not a number or a JSON field is not valid JSON.
    """
    text = raw.strip()
    match kind:
        case "bool":
            return text not in ("", "0", "false", "off")
        case "int":
            return int(text) if text else None
        case "number":
            return float(text) if text else None
        case "lines":
            return [line.strip() for line in raw.splitlines() if line.strip()]
        case "json":
            return json.loads(text) if text else {}
        case "markdown":
            return _clean(raw) or None
        case "prompt":
            return _clean(raw).strip()
        case "rules":
            return _clean(raw)
        case _:
            return text or None


# ------------------------------------------------------------------ the panes

CHECKER_PANES = {
    "command": (
        "Command checker",
        "Judges every shell command, and every tool call routed to it, before it runs.",
    ),
    "edit": ("Edit checker", "Judges every file edit before it is written."),
    "changeset": (
        "Changeset checker",
        "Reviews the whole change when Claude commits: complete, in scope, honestly described.",
    ),
    "subagent-result": (
        "Subagent results",
        "Compares what a subagent hands back with what you asked for, before Claude acts on it.",
    ),
}
"""Checker type -> (pane title, what the checker does) for the four that always run."""


def _checker_panes(src: Sources, used: set[str]) -> list[Pane]:
    """Build one pane per checker: the four that always run, honesty, then any extra type.

    Args:
        src: The sources.
        used: Collects the configuration sections and fields these panes show.

    Returns:
        The panes of the "Checkers" sidebar section.
    """
    rows = {row.name: row for row in src.checkers}
    found: list[Pane] = []
    for name, section in ROUTE_CHECKERS.items():
        title, description = CHECKER_PANES[name]
        pane = Pane(id=f"checker-{name}", section="Checkers", title=title, description=description)
        if name == "subagent-result":
            pane.groups.append(Group("Use", _config_fields(src, ["check_subagent_results"], used)))
        if name in rows:
            pane.groups.append(Group("Instructions", [_prompt_field(src, rows[name])]))
        pane.groups.append(Group("Model and limits", _section_fields(src, section, used)))
        found.append(pane)

    honesty = Pane(
        id="honesty",
        section="Checkers",
        title="Code honesty",
        description=(
            "Records code whose names or documentation no longer match what it does, and can"
            " refuse to run it until they agree again."
        ),
    )
    enforcement = Group("Enforcement", _config_fields(src, ["honesty.mode"], used))
    honesty.groups.append(enforcement)
    if "honesty" in rows:
        columns = _checker_fields(src, rows["honesty"])
        enforcement.fields.append(columns[0])
        honesty.groups.append(
            Group("Honesty checker", [_prompt_field(src, rows["honesty"]), *columns[1:]])
        )
    honesty.groups.append(
        Group(
            "Honesty rules",
            _config_fields(src, ["honesty.rules"], used),
            note=(
                "What an API contract must say and what counts as dishonest. The command, edit"
                " and changeset checkers read these too. Change them to fit your project's"
                " documentation standard; they are not meant to change often."
            ),
        )
    )
    honesty.groups.append(Group("More", _section_fields(src, "honesty", used)))
    found.append(honesty)

    for name, row in rows.items():
        if name in ROUTE_CHECKERS or name in ("honesty", "narrator"):
            continue
        columns = _checker_fields(src, row)
        found.append(
            Pane(
                id=f"checker-{name}",
                section="Checkers",
                title=f"{name} checker",
                description=f"An extra checker on the {row.route} route, beside the built-in one.",
                groups=[
                    Group("Use", columns[:1]),
                    Group("Instructions", [_prompt_field(src, row)]),
                    Group("Model and limits", columns[1:]),
                ],
            )
        )
    overview = Pane(
        id="checkers",
        section="Checkers",
        title="Overview",
        description=(
            "What the checkers decided so far, in every project. This is the record, not a"
            " measurement: an escalation a human allowed cost an interruption for nothing, and"
            " a step a checker approved that a human then denied is a miss. `calu evals`"
            " measures the checkers against a case set instead."
        ),
        facts=[("All steps", line, False) for line in src.step_lines],
    )
    for pane in found:
        name = "honesty" if pane.id == "honesty" else pane.id.removeprefix("checker-")
        stats = src.stats.get(name)
        pane.facts = _record(stats)
        pane.misses = [miss for miss in src.misses if miss.checker_name == name][:MISSES_SHOWN]
        summary = f"{stats.runs} run(s), {stats.passed_then_denied} miss(es)" if stats else ""
        overview.links.append((pane.id, pane.title, summary or "no runs recorded yet"))
    found.insert(0, overview)
    return found


def _record(stats: herd.CheckerStats | None) -> list[tuple[str, str, bool]]:
    """Describe what one checker decided so far.

    Args:
        stats: The checker's record, or None if it never ran.

    Returns:
        (label, value, whether to mark it as a problem) lines: what it decided, where a human
        disagreed, and what it cost; [] for a checker without a run.
    """
    if stats is None or not stats.runs:
        return []
    decided = f"{stats.runs} run(s): {stats.passed} pass, {stats.escalated} escalate"
    if stats.allowed or stats.denied:
        decided += f", {stats.allowed} allow, {stats.denied} deny"
    facts = [("Decided", decided, False)]
    if stats.escalated_then_allowed:
        facts.append(
            (
                "Escalated, then allowed by a human",
                f"{stats.escalated_then_allowed} (an interruption that was not needed)",
                False,
            )
        )
    if stats.passed_then_denied:
        facts.append(
            ("Approved, then denied by a human", f"{stats.passed_then_denied} (a miss)", True)
        )
    if stats.tokens_text:
        facts.append(("Tokens", stats.tokens_text, False))
        facts.append(("Cache", stats.cache_text, stats.cache_is_cold))
    return facts


def _rule_panes(src: Sources) -> list[Pane]:
    """Build the three rule panes, each with its allow list and its deny list.

    Args:
        src: The sources.

    Returns:
        The panes of the "Rules" sidebar section.
    """
    scope = src.project or "global"
    found: list[Pane] = []
    for kind, title, what in RULE_KINDS:
        pane = Pane(
            id=f"rules-{kind}",
            section="Rules",
            title=title,
            description=(
                f"Regular expressions tried on {what} before any model is asked. A deny match"
                " waits for you; an allow match without a deny match runs with no model call;"
                " everything else goes to the checker."
            ),
        )
        group = Group(
            "Lists",
            note=(
                "One pattern per line, matched anywhere in the text (anchor with ^ and $). A"
                " # comment line directly above a pattern is kept as its comment."
            ),
        )
        for side in ("allow", "deny"):
            name = f"{kind}-{side}"
            if name not in RULE_LISTS:
                continue
            own = [r for r in src.rules if r.list == name and r.scope == scope]
            inherited = (
                [r for r in src.rules if r.list == name and r.scope == "global" and r.enabled]
                if src.project
                else []
            )
            count = sum(1 for r in own if r.enabled)
            disabled = sum(1 for r in own if not r.enabled)
            meta = f"{count} pattern{'' if count == 1 else 's'}"
            if disabled:
                meta += f" · {disabled} disabled, kept"
            group.fields.append(
                Field(
                    name=f"rules:{name}",
                    label="Allow" if side == "allow" else "Deny",
                    kind="rules",
                    value=admin.rule_list_text([r for r in own if r.enabled]),
                    key=name,
                    help=(
                        f"{what.capitalize()} that matches runs without a model call."
                        if side == "allow"
                        else f"{what.capitalize()} that matches always waits for you."
                    ),
                    meta=meta,
                    tone=side,
                    extra_title=(
                        f"{len(inherited)} global pattern(s) apply as well" if inherited else ""
                    ),
                    extra=admin.rule_list_text(inherited),
                    placeholder="^pattern( |$)",
                )
            )
        pane.groups.append(group)
        pane.rule_sets = [
            RuleSet(name, what_set)
            for name, (target, what_set) in admin.RULE_SETS.items()
            if target in ("all", f"{kind}-deny", f"{kind}-allow")
        ]
        found.append(pane)
    return found


def _claude_panes(src: Sources, used: set[str]) -> list[Pane]:
    """Build the panes of the Claude Code session and of its sandbox.

    Args:
        src: The sources.
        used: Collects the configuration sections these panes show.

    Returns:
        The panes of the "Claude Code" sidebar section.
    """
    sandbox = _section_fields(src, "claude.sandbox", used)
    return [
        Pane(
            id="claude",
            section="Claude Code",
            title="Session",
            description=(
                "How calu starts Claude Code. Read when a session starts, so a change applies"
                " to the next session, not to a running one."
            ),
            groups=[Group("Claude Code", _section_fields(src, "claude", used))],
        ),
        Pane(
            id="sandbox",
            section="Claude Code",
            title="Sandbox",
            description=(
                "What the commands Claude runs may read, write and reach. Read when a session"
                " starts."
            ),
            groups=[Group("Sandbox", sandbox)],
        ),
    ]


SECTION_PANES = {
    "changes": ("Change review", "Measuring what a command wrote, and reviewing it."),
    "clock": ("Clock watch", "Noticing a system clock that was set back."),
    "web": ("Web interface", "Settings of this interface."),
}
"""Configuration section -> (pane title, description); a section not named here gets a pane
titled after its field name."""


def _session_panes(src: Sources, used: set[str]) -> list[Pane]:
    """Build the panes of calu's own sections, and one for every section not placed elsewhere.

    Args:
        src: The sources.
        used: The configuration sections and fields the earlier panes show; whatever the model
            has beyond them is placed here, so a new setting is never missing from the page.

    Returns:
        The panes of the "Session" sidebar section.
    """
    escalation = Pane(
        id="escalation",
        section="Session",
        title="Escalation and routing",
        description="When a step waits for a human, and which checker a tool call goes to.",
        groups=[
            Group(
                "Escalation",
                _config_fields(src, ["escalation_timeout_s", "store_failures_before_stop"], used),
            ),
            Group("Always ask a human", _section_fields(src, "auto_escalate", used)),
            Group("Routing", _config_fields(src, ["default_route", "tool_routes"], used)),
        ],
    )
    context = Pane(
        id="context",
        section="Session",
        title="Context and narration",
        description=(
            "What the checkers read about the conversation: the narrated turns and the folded"
            " summary."
        ),
        groups=[Group("Context", _section_fields(src, "context", used))],
    )
    narrator = next((row for row in src.checkers if row.name == "narrator"), None)
    if narrator is not None:
        context.groups.append(Group("Narrator instructions", [_prompt_field(src, narrator)]))
    found = [escalation, context]
    stray: list[Field] = []
    for name, info in Config.model_fields.items():
        if name in SKIP or name in used:
            continue
        if _model_of(info.annotation) is None:
            stray.extend(_config_fields(src, [name], used))
            continue
        title, description = SECTION_PANES.get(name, (_label(name), ""))
        found.append(
            Pane(
                id=name.replace("_", "-"),
                section="Session",
                title=title,
                description=description,
                groups=[Group(title, _section_fields(src, name, used))],
            )
        )
    if stray:
        found.append(
            Pane(id="other", section="Session", title="Other", groups=[Group("Other", stray)])
        )
    return found


# ------------------------------------------------------------------ the fields


def _prompt_field(src: Sources, row: admin.CheckerRow) -> Field:
    """Build the editor of one checker type's instructions.

    Checker types are shared by every project, so the editor is locked in a project's scope.

    Args:
        src: The sources.
        row: The checker type.

    Returns:
        The field `prompt:<name>`.
    """
    fixed = system_part(row.route)
    return Field(
        name=f"prompt:{row.name}",
        label="Your instructions",
        kind="prompt",
        value=src.instructions.get(row.name, ""),
        key=f"calu checker edit {row.name}",
        help=(
            "What to pass, deny and escalate, as Markdown. calu adds the checker's role and"
            " answer format itself. Running sessions use a change at their next step."
            if row.route in SYSTEM_PARTS
            else "Markdown. Running sessions use a change at their next step."
        ),
        meta=f"version {row.version}",
        locked=_shared(src),
        extra_title="What calu puts in front of your instructions" if fixed else "",
        extra=fixed,
    )


def _checker_fields(src: Sources, row: admin.CheckerRow) -> list[Field]:
    """Build the fields for the columns of an extra checker type.

    Args:
        src: The sources.
        row: The checker type.

    Returns:
        One field per column of `CHECKER_COLUMNS`, the switch first. The model and limit
        fields take their help from `CheckerConfig`, which documents the same values.
    """
    found = [
        Field(
            name=f"checker:{row.name}:enabled",
            label=f"Run the {row.name} checker",
            kind="bool",
            value=row.enabled,
            key=f"calu checker set {row.name} --enable",
            help=f"An extra checker on the {row.route} route. Off: sessions do not load it.",
            locked=_shared(src),
        )
    ]
    for column in CHECKER_COLUMNS[1:]:
        info = CheckerConfig.model_fields[column]
        found.append(
            Field(
                name=f"checker:{row.name}:{column}",
                label=_label(column),
                kind="model" if column == "model" else "int",
                options=MODEL_OPTIONS if column == "model" else (),
                doc=MODELS_DOC if column == "model" else None,
                value=getattr(row, column),
                key=f"calu checker set {row.name} --{column.replace('_', '-')}",
                help=_help(info.description),
                locked=_shared(src),
            )
        )
    return found


def _shared(src: Sources) -> str:
    """Say why a checker type cannot be edited in the shown scope.

    Args:
        src: The sources.

    Returns:
        The reason in a project's scope, "" in the global one.
    """
    if src.project:
        return "Shared by every project. Switch the scope to Global to change it."
    return ""


def _section_fields(src: Sources, section: str, used: set[str]) -> list[Field]:
    """Build the fields of one configuration section that no pane shows yet.

    Args:
        src: The sources.
        section: The dotted path of a nested model, e.g. "claude" or "claude.sandbox".
        used: The paths already shown; the section and its new fields are added.

    Returns:
        The section's own fields in declaration order. A model nested inside it is not
        included: it is a section of its own.
    """
    model: type[BaseModel] = Config
    for part in section.split("."):
        nested = _model_of(model.model_fields[part].annotation)
        if nested is None:
            return []
        model = nested
    used.add(section)
    paths = [
        f"{section}.{name}"
        for name, info in model.model_fields.items()
        if name not in SKIP and _model_of(info.annotation) is None
    ]
    return _config_fields(src, paths, used)


def _config_fields(src: Sources, paths: Sequence[str], used: set[str]) -> list[Field]:
    """Build the fields of the given configuration values that no pane shows yet.

    Args:
        src: The sources.
        paths: Dotted keys, e.g. "claude.model".
        used: The paths already shown; the new ones are added.

    Returns:
        One field per path the form can render, in the given order.
    """
    found: list[Field] = []
    for path in paths:
        if path in used:
            continue
        used.add(path)
        item = _config_field(src, path)
        if item is not None:
            found.append(item)
    return found


def _config_field(src: Sources, path: str) -> Field | None:
    """Build the field of one configuration value.

    Args:
        src: The sources.
        path: Its dotted key.

    Returns:
        The field, or None for a type the form cannot show.
    """
    model: type[BaseModel] = Config
    *parents, name = path.split(".")
    for part in parents:
        nested = _model_of(model.model_fields[part].annotation)
        if nested is None:
            return None
        model = nested
    info = model.model_fields[name]
    kind, choices = _kind_of(info.annotation)
    if kind is None:
        return None
    if path in MARKDOWN:
        kind = "markdown"
    # A Pydantic AI model string: every `model` field but Claude Code's own (`claude.model`).
    is_model = kind == "text" and name.endswith("model") and path != "claude.model"
    if is_model:
        kind = "model"
    default = _default(info)
    if path == "honesty.rules":
        default = honesty_rules()
    stored = _lookup(src.values, path)
    inherited = _lookup(src.inherited, path) if src.project else None
    base = inherited if inherited is not None else default
    return Field(
        name=path,
        label=_label(name),
        kind=kind,
        value=stored if stored is not None else base,
        key=path,
        choices=choices,
        suggestions=MODEL_SUGGESTIONS if path == "claude.model" else (),
        options=MODEL_OPTIONS if is_model else (),
        doc=MODELS_DOC if is_model else None,
        help=_help(info.description),
        base=base,
        is_set=stored is not None,
        inherits="global" if inherited is not None else "default",
    )


def _lookup(values: Mapping[str, Any], path: str) -> Any:
    """Read a dotted key out of a nested dict.

    Args:
        values: The nested dict.
        path: The dotted key.

    Returns:
        The value, or None if any part of the path is missing.
    """
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _text(kind: Kind, value: Any) -> str:
    """Render a value the way its control holds it.

    Args:
        kind: The control.
        value: The value.

    Returns:
        One item per line for a list, JSON for a dict, "" for None, a whole number without a
        fraction, else the value as text.
    """
    if value is None:
        return ""
    if kind == "lines":
        return "\n".join(str(item) for item in value or ())
    if kind == "json":
        return json.dumps(value, indent=2, sort_keys=True) if value else ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 3600, not 3600.0
    return str(value)


def _clean(raw: str) -> str:
    """Normalise a submitted document.

    Args:
        raw: The text of an editor.

    Returns:
        The text with Unix line ends and exactly one newline at its end; "" if it is blank.
    """
    text = raw.replace("\r\n", "\n").strip()
    return text + "\n" if text else ""


def _reason(exc: Exception) -> str:
    """Shorten an exception to what a human needs to fix the field.

    Args:
        exc: What the save raised.

    Returns:
        Its message on one line.
    """
    return " ".join(str(exc).split()) or type(exc).__name__


_POINTER = r"(?:[DQ]\d+|§[\d.]+)"
REFERENCE = re.compile(rf"\s*\((?:see\s+)?(?:PLAN\.md\s+)?{_POINTER}(?:[,;]\s*{_POINTER})*\)")
"""A parenthesised pointer into PLAN.md, e.g. " (PLAN.md D27)" or " (§13)"."""


def _help(description: str | None) -> str:
    """Turn a field's docstring into the help text of its control.

    Args:
        description: The field description of the configuration model.

    Returns:
        The text without pointers into PLAN.md, which mean nothing on the page.
    """
    return REFERENCE.sub("", description or "")


def _kind_of(annotation: Any) -> tuple[Kind | None, tuple[str, ...]]:
    """Decide which control a type gets.

    Args:
        annotation: The field's type annotation.

    Returns:
        (kind, choices): the control and, for a dropdown, its options. (None, ()) for a type the
        form does not render.
    """
    inner = _without_none(annotation)
    origin = get_origin(inner)
    if origin is Literal:
        return "choice", tuple(str(value) for value in get_args(inner))
    if isinstance(inner, type) and issubclass(inner, Enum):
        return "choice", tuple(str(member.value) for member in inner)
    if inner is bool:
        return "bool", ()
    if inner is int:
        return "int", ()
    if inner is float:
        return "number", ()
    if inner is str:
        return "text", ()
    if origin in (list, tuple, Sequence):
        return "lines", ()
    if origin is dict:
        return "json", ()
    return None, ()


def _without_none(annotation: Any) -> Any:
    """Strip `| None` from a type.

    Args:
        annotation: The annotation.

    Returns:
        The annotation without `NoneType`, or the annotation itself if it is not a union.
    """
    if get_origin(annotation) in (types.UnionType, Union):
        rest = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0]
        for arg in rest:  # a union of several Literals: take the first that is one
            if get_origin(arg) is Literal:
                return arg
        return rest[0] if rest else annotation
    return annotation


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """Return the nested configuration model of a field, if it is one.

    Args:
        annotation: The field's type annotation.

    Returns:
        The model class, or None.
    """
    inner = _without_none(annotation)
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner
    return None


def _default(info: Any) -> Any:
    """Return a field's default value.

    Args:
        info: The Pydantic `FieldInfo`.

    Returns:
        The default, the result of its factory, or None if it has neither.
    """
    if info.default_factory is not None:
        return info.default_factory()
    return None if repr(info.default) == "PydanticUndefined" else info.default


UNITS = {"s": "seconds", "ms": "milliseconds", "mb": "MB", "kb": "kB"}
"""Unit suffixes of field names, spelled out in a label."""


def _label(name: str) -> str:
    """Turn a field name into a label.

    Args:
        name: The field name, e.g. "escalation_timeout_s".

    Returns:
        The label, e.g. "Escalation timeout (seconds)".
    """
    words = name.split("_")
    unit = ""
    if len(words) > 1 and words[-1] in UNITS:
        unit = f" ({UNITS[words.pop()]})"
    return " ".join(words).capitalize() + unit
