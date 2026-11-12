"""Allow and deny lists for commands, reads and writes.

Six lists hold Python regular expressions. calu keeps them in the database (table `rules`,
PLAN.md §7); the session loads them into a `Rules` object and replaces them when the database
announces a change (`config_changed`).

| List            | Matched against                                 |
|-----------------|-------------------------------------------------|
| `command-allow` | each simple command of a Bash call              |
| `command-deny`  | each simple command of a Bash call              |
| `read-allow`    | each path a tool call reads                     |
| `read-deny`     | each path a tool call reads                     |
| `write-allow`   | each path a tool call writes                    |
| `write-deny`    | each path a tool call writes                    |

Patterns match with `re.search`, so anchor them with `^` and `$`. Paths inside the workspace are
matched in workspace-relative form (`src/app.py`, `.claude/settings.json`), paths outside in
absolute form (`/home/me/.ssh/id_rsa`). Commands are matched as their words joined by single
spaces with quotes removed (see `calu.checkers.shell`).

Outcome of a tool call (`RuleOutcome`):

    any part matches a deny list           -> ask the human (ESCALATE), no checker call
    every part matches an allow list       -> may run without a checker call
    otherwise                              -> the checker decides; unlisted paths are noted

Which parts a tool call has: Bash -> its simple commands, the paths its commands read and write;
Read, NotebookRead, Grep, Glob, LS -> the path they read; Edit, Write, MultiEdit, NotebookEdit ->
the file they write. Other tools have no parts; the rules do not affect them.

Built-in rules that the lists cannot remove: reading or writing calu's own configuration
directory (`~/.config/calu`, which holds the encrypted credentials) always asks the human. An
invalid pattern makes every checked call ask the human until it is fixed, so a broken deny list
never lets a call through.
"""

import os
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from calu.checkers.model import Step
from calu.checkers.policy import SHELL_TOOLS
from calu.checkers.search import searched_files
from calu.checkers.shell import ShellPath, command_variants, parse_command

RULE_LISTS = (
    "command-allow",
    "command-deny",
    "read-allow",
    "read-deny",
    "write-allow",
    "write-deny",
)

READ_PATH_TOOLS = frozenset({"Read", "NotebookRead", "Grep", "Glob", "LS"})
"""Read-only tools whose path is checked against the read lists."""

WRITE_PATH_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
"""Edit tools whose file is checked against the write lists."""


@dataclass(frozen=True)
class Rule:
    """One pattern from a rules file (or a built-in rule)."""

    pattern: re.Pattern[str]
    source: str
    """Where the rule comes from, e.g. "write-deny#4" or "built-in"."""

    def describe(self) -> str:
        """Return the rule for messages.

        Returns:
            E.g. "write-deny#4 `^CLAUDE.md$`".
        """
        return f"{self.source} `{self.pattern.pattern}`"


_CALU_CONFIG = re.compile(r"(^|/)\.config/calu(/|$)")
BUILT_IN_WRITE_DENY = (Rule(_CALU_CONFIG, "built-in"),)
BUILT_IN_READ_DENY = (Rule(_CALU_CONFIG, "built-in"),)
PATH_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "file_path",
        "filepath",
        "filename",
        "dir",
        "directory",
        "source",
        "destination",
        "target",
        "src",
        "dst",
        "uri",
        "root",
        "cwd",
        "relative_path",
        "pathname",
        "abs_path",
        "absolute_path",
        "folder",
        "location",
    }
)
"""Argument names whose string values are read as paths in tools without a specific rule."""

CONTENT_KEYS = frozenset(
    {
        "content",
        "contents",
        "text",
        "body",
        "data",
        "message",
        "prompt",
        "description",
        "query",
        "sql",
        "code",
        "script",
        "new_string",
        "old_string",
        "command",
        "pattern",
    }
)
"""Argument names whose values are content, not a place: never read as paths (D166)."""

WRITE_WORDS = (
    "write",
    "edit",
    "create",
    "delete",
    "remove",
    "move",
    "rename",
    "put",
    "save",
    "copy",
    "append",
    "update",
    "patch",
    "replace",
    "insert",
)
"""Words in a tool's name that make the write deny list apply to the paths it names (D166)."""

OTHER_RULED_TOOLS = frozenset({"DesignSync", "ExitWorktree"})
"""Tools of CLI 2.1.319 that read files or destroy work without being a read, edit or shell
tool; `Rules._evaluate_other` applies the rules to them (D165)."""

BUILT_IN_COMMAND_DENY = (
    Rule(
        re.compile(
            r"^((uv|pipx|poetry|pdm|hatch) (run|exec) |uv tool run |uvx |python[0-9.]* -m )?"
            r"calu( |$)"
        ),
        "built-in",
    ),
)
"""Claude running calu itself always asks (PLAN.md D160). calu runs as the same OS user as
Claude, and `calu resolve`, `calu config set`, `calu rules`, `calu checker` or a `calu attach`
that mints its own token (D105) would let Claude decide its own escalations or switch off the
checks. With the sandbox on, `~/.config/calu` is unreadable to Claude's commands and such a
call fails anyway; without it, this rule is what stops it. Built in, like the `~/.config/calu`
path rules, so that no rule list can drop it and existing installations get it at once."""


def parse_rules(text: str, name: str) -> tuple[list[Rule], list[str]]:
    """Parse the text of one rules file.

    Args:
        text: File content: one regex per line; lines starting with `#` (after leading
            whitespace) and blank lines are skipped; surrounding whitespace is stripped.
        name: The file name, used in `Rule.source` and in error messages.

    Returns:
        The valid rules, and one message per invalid pattern (e.g. "read-deny:3: invalid
        pattern `(`: missing ), unterminated subpattern at position 0").
    """
    rules: list[Rule] = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            rules.append(Rule(re.compile(stripped), f"{name}:{number}"))
        except re.error as exc:
            errors.append(f"{name}:{number}: invalid pattern `{stripped}`: {exc}")
    return rules, errors


@dataclass
class RuleOutcome:
    """What the rules say about one tool call (see the module docstring).

    `ask` holds reasons to escalate to the human (deny matches and rules file errors). `notes`
    are hints for the checker about parts that no list covers. `allowed` is True if the call has
    at least one part, every part matches an allow list and `ask` is empty.
    """

    ask: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    allowed: bool = False

    @property
    def reason(self) -> str:
        """The escalation reason for the human.

        Returns:
            The `ask` reasons joined by spaces ("" if there are none).
        """
        return " ".join(self.ask)


class Rules:
    """The rules of one workspace: six lists of patterns, replaceable at run time."""

    def __init__(
        self,
        workspace: Path,
        lists: Mapping[str, tuple[list[Rule], list[str]]] | None = None,
    ) -> None:
        """Create the rules for a workspace.

        Args:
            workspace: The workspace root; paths inside it are matched in relative form.
            lists: Rules and error messages per list name (`RULE_LISTS`); missing lists are
                empty. None makes all lists empty.
        """
        self.workspace = workspace.resolve()
        self._lists: dict[str, tuple[list[Rule], list[str]]] = {}
        self.replace(lists or {})

    @classmethod
    def from_texts(cls, workspace: Path, texts: Mapping[str, str]) -> Rules:
        """Create rules from list texts in the line format of `parse_rules`.

        Args:
            workspace: The workspace root.
            texts: Text per list name, e.g. {"command-deny": "^sudo( |$)"}.

        Returns:
            The rules; rule sources are "<list>:<line>".
        """
        return cls(workspace, {name: parse_rules(text, name) for name, text in texts.items()})

    @classmethod
    def from_rows(cls, workspace: Path, rows: Iterable[tuple[str, str, object]]) -> Rules:
        """Create rules from database rows.

        Args:
            workspace: The workspace root.
            rows: (list name, pattern, row id) in the order the rules apply.

        Returns:
            The rules; rule sources are "<list>#<id>". An invalid pattern becomes an error
            message of its list (every checked call then asks the human).
        """
        lists: dict[str, tuple[list[Rule], list[str]]] = {}
        for name, pattern, row_id in rows:
            rules, errors = lists.setdefault(name, ([], []))
            try:
                rules.append(Rule(re.compile(pattern), f"{name}#{row_id}"))
            except re.error as exc:
                errors.append(f"{name}#{row_id}: invalid pattern `{pattern}`: {exc}")
        return cls(workspace, lists)

    def replace(self, lists: Mapping[str, tuple[list[Rule], list[str]]]) -> None:
        """Replace all lists at once (e.g. after the database announced a change).

        Args:
            lists: Rules and error messages per list name; missing lists become empty. Unknown
                list names are reported as errors of the pseudo list "unknown".
        """
        new: dict[str, tuple[list[Rule], list[str]]] = {name: ([], []) for name in RULE_LISTS}
        for name, (rules, errors) in lists.items():
            if name in new:
                new[name] = (list(rules), list(errors))
            else:
                new.setdefault("unknown", ([], []))[1].append(f"unknown rule list {name!r}")
        self._lists = new

    def lists(self, name: str) -> tuple[list[Rule], list[str]]:
        """Return the rules and errors of one list.

        Args:
            name: One of `RULE_LISTS`.

        Returns:
            The rules and the error messages of that list (empty for unknown names).
        """
        return self._lists.get(name, ([], []))

    def evaluate(self, step: Step) -> RuleOutcome:
        """Apply the rules to one tool call.

        Args:
            step: The tool call. `step.cwd` is the directory relative paths resolve against.

        Returns:
            The outcome. Tools without parts (WebFetch, MCP tools, TodoWrite, ...) get an empty
            outcome with `allowed` False. Example: `cat .env` with the default lists gives
            `ask == ["read-deny#6 `...` matches `.env` (argument of cat)."]`.
        """
        outcome = RuleOutcome()
        tool = step.tool_name
        if tool in OTHER_RULED_TOOLS:
            return self._evaluate_other(step, outcome)
        if tool not in SHELL_TOOLS and tool not in READ_PATH_TOOLS and tool not in WRITE_PATH_TOOLS:
            return self._evaluate_named_paths(step, outcome)
        for name in [*RULE_LISTS, "unknown"]:
            for error in self.lists(name)[1]:
                outcome.ask.append(f"The rule list {error}. Fix it to continue without asking.")
        cwd = Path(step.cwd or self.workspace)
        if not cwd.is_absolute():
            cwd = self.workspace / cwd
        all_allowed = True
        parts = 0
        if tool in SHELL_TOOLS:
            command = str(step.tool_input.get("command", ""))
            parsed = parse_command(command, cwd)
            if parsed.error is not None:
                all_allowed = False
                outcome.notes.append(
                    f"{parsed.error} The rules could not split it into simple commands; judge "
                    "the whole command."
                )
                for line in command.splitlines():
                    self._deny_command(line.strip(), outcome)
            for target in diff_targets(command, cwd):
                # `patch < x.diff` and `git apply x.diff` write the files the diff names, which
                # the command line does not show (D169).
                if not self._check_path(target, "write", "file changed by the diff", outcome):
                    all_allowed = False
            for literal in inline_code_paths(command):
                # A path written into an interpreter's inline code is read like an argument
                # (D167); without this `node -e "...readFileSync('.env')"` never met the read
                # deny list. From the raw command: the splitter's form has lost the quotes.
                path = _absolute(literal, cwd)
                if path is not None and self.read_denied(path):
                    all_allowed = False
                    outcome.ask.append(
                        f"Inline code in `{command[:60]}` names `{self.display(path)}`, which "
                        "the read deny list protects."
                    )
            for simple in parsed.commands:
                parts += 1
                if self._deny_command(simple, outcome):
                    continue
                if not _first_match(self.lists("command-allow")[0], simple):
                    all_allowed = False
            for used in parsed.paths:
                parts += 1
                if used.search is not None and not self._recursive_search_allowed(used, outcome):
                    all_allowed = False
                for candidate in used.paths:
                    if candidate is None:
                        all_allowed = False
                        outcome.notes.append(
                            f"`{used.raw}` ({used.via}) uses a variable, a glob, a recursive "
                            f"search or an unknown directory; the rules cannot tell which files "
                            f"it {used.kind}s."
                        )
                    elif not self._check_path(candidate, used.kind, used.via, outcome):
                        all_allowed = False
        else:
            kind = "read" if tool in READ_PATH_TOOLS else "write"
            for raw in _tool_paths(step):
                parts += 1
                resolved = _absolute(raw, cwd)
                if resolved is None:
                    all_allowed = False
                    outcome.ask.append(f"`{raw[:80]}` ({tool} tool) is not a usable path.")
                    continue
                path = resolved
                if not self._check_path(path, kind, f"{tool} tool", outcome):
                    all_allowed = False
                if (
                    tool == "Grep"
                    and path.is_dir()
                    and not self._search_allowed(step, path, outcome)
                ):
                    all_allowed = False
        outcome.allowed = parts > 0 and all_allowed and not outcome.ask
        return outcome

    def _deny_command(self, command: str, outcome: RuleOutcome) -> bool:
        """Add an `ask` reason if `command`, or a form of it, matches the command deny list.

        Args:
            command: One simple command; `shell.command_variants` gives the forms checked.
            outcome: Collects the reason.

        Returns:
            True if a deny rule matched.
        """
        # Also the forms without leading assignments, wrappers (`env`, `nohup`, `timeout 5`)
        # and a path before the program name: deny rules are anchored at the start (D145).
        for variant in command_variants(command):
            if CALU_CONTAINER.search(variant):
                outcome.ask.append(
                    f"The command `{command}` reaches calu's own database container, which "
                    "always asks (built-in, D171): it holds the escalations Claude is waiting on."
                )
                return True
            if runs_calu(variant):
                outcome.ask.append(
                    f"The command `{command}` runs calu itself, which always asks (built-in, "
                    "D160): calu decides Claude's escalations."
                )
                return True
            rule = _first_match([*BUILT_IN_COMMAND_DENY, *self.lists("command-deny")[0]], variant)
            if rule is None:
                continue
            seen = "" if variant == command else f" (as `{variant}`)"
            outcome.ask.append(
                f"The command `{command}`{seen} matches the deny rule {rule.describe()}."
            )
            return True
        return False

    def _check_path(self, path: Path, kind: str, via: str, outcome: RuleOutcome) -> bool:
        """Check one path against the read or write lists.

        Args:
            path: The absolute path.
            kind: "read" or "write".
            via: Where the path came from, for messages (e.g. "redirect >").
            outcome: Collects `ask` reasons and checker notes.

        Returns:
            True if the path matches the allow list and no deny rule.
        """
        shown = self.display(path)
        built_in = BUILT_IN_READ_DENY if kind == "read" else BUILT_IN_WRITE_DENY
        deny = [*built_in, *self.lists(f"{kind}-deny")[0]]
        matched = self.match_form(path)
        rule = _first_match(deny, matched)
        if rule is not None:
            outcome.ask.append(
                f"{'Reading' if kind == 'read' else 'Writing'} `{shown}` ({via}) matches the deny "
                f"rule {rule.describe()}."
            )
            return False
        if _first_match(self.lists(f"{kind}-allow")[0], matched):
            return True
        outcome.notes.append(
            f"`{shown}` ({via}) is not on the {kind} allowlist: decide whether the user's request "
            f"covers {'reading' if kind == 'read' else 'writing'} it."
        )
        return False

    def _search_allowed(self, step: Step, directory: Path, outcome: RuleOutcome) -> bool:
        """Apply the read deny list to every file a Grep of `directory` reads (PLAN.md D146).

        Claude Code's Grep reads every file below the directory that git does not ignore,
        hidden ones included, so a check of the directory alone let it read `.env` or a `.key`
        file unchecked. A protected file among them makes the step ask, as a `Read` of it would.

        Args:
            step: The Grep call; its `glob` narrows the files.
            directory: The directory it searches, absolute.
            outcome: Collects the `ask` reason, or a note when the files could not all be listed.

        Returns:
            False if a protected file would be read or the listing is incomplete, else True.
        """
        glob = step.tool_input.get("glob")
        listed = searched_files(directory, glob if isinstance(glob, str) else None)
        denied = [self.display(path) for path in listed.files if self.read_denied(path)]
        if denied:
            shown = ", ".join(f"`{name}`" for name in denied[:5])
            more = f" and {len(denied) - 5} more" if len(denied) > 5 else ""
            outcome.ask.append(
                f"Searching `{self.display(directory)}` (Grep tool) would read {shown}{more}, "
                "which the read deny list protects."
            )
            return False
        if not listed.complete:
            outcome.notes.append(
                f"Searching `{self.display(directory)}` covers too many files to check each "
                "against the read deny list; decide whether the search may read them."
            )
            return False
        return True

    def _evaluate_named_paths(self, step: Step, outcome: RuleOutcome) -> RuleOutcome:
        """Apply the deny lists to the paths any other tool's input names (PLAN.md D166).

        An MCP server -- a filesystem server, say -- reads and writes files the rules never see:
        `mcp__fs__read_file(path="~/.ssh/id_rsa")` reached only the checker. For every tool the
        rules have nothing specific for, each string argument under a path-like key
        (`PATH_KEYS`) or that looks like a path (`/`, `~/`, `./`, `../`, `file://`) is checked:
        against the read deny list always, and against the write deny list too when the tool's
        name says it writes (`WRITE_WORDS`). A match asks; anything else leaves the step to the
        checker as before, so this can make a step wait for a person, never let one through.

        Args:
            step: The tool call.
            outcome: The outcome to fill; `allowed` stays False.

        Returns:
            `outcome`.
        """
        cwd = Path(step.cwd or self.workspace)
        if not cwd.is_absolute():
            cwd = self.workspace / cwd
        lowered = step.tool_name.lower()
        writes = any(word in lowered for word in WRITE_WORDS)
        for text in _named_paths(step.tool_input):
            path = _absolute(text.removeprefix("file://"), cwd)
            if path is None:
                continue
            kinds = ("read", "write") if writes else ("read",)
            for kind in kinds:
                built_in = BUILT_IN_READ_DENY if kind == "read" else BUILT_IN_WRITE_DENY
                rule = _first_match(
                    [*built_in, *self.lists(f"{kind}-deny")[0]], self.match_form(path)
                )
                if rule is not None:
                    outcome.ask.append(
                        f"`{step.tool_name}` names `{self.display(path)}`, which the {kind} deny "
                        f"rule {rule.describe()} protects."
                    )
                    break
        return outcome

    def _evaluate_other(self, step: Step, outcome: RuleOutcome) -> RuleOutcome:
        """Apply the rules to the tools of `OTHER_RULED_TOOLS` (PLAN.md D165).

        `DesignSync` reads files from disk itself and uploads them to claude.ai: the directory
        of `finalize_plan` (`localDir`) and each `localPath` of `write_files` are reads, so the
        read deny list applies to them, as to a `Read`. `ExitWorktree` with
        `discard_changes: true` deletes uncommitted work, which asks like `git reset --hard`.
        Such a step is never free: the checker still judges what leaves the machine.

        Args:
            step: The tool call.
            outcome: The outcome to fill.

        Returns:
            `outcome`, with `allowed` False.
        """
        inp = step.tool_input
        cwd = Path(step.cwd or self.workspace)
        if not cwd.is_absolute():
            cwd = self.workspace / cwd
        if step.tool_name == "ExitWorktree":
            if inp.get("action") == "remove" and inp.get("discard_changes") is True:
                outcome.ask.append(
                    "Removing the worktree with `discard_changes` deletes its uncommitted work "
                    "and unmerged commits."
                )
            return outcome
        method = inp.get("method")
        raw: list[str] = []
        if method == "finalize_plan":
            raw.append(str(inp.get("localDir") or "."))
        elif method == "write_files":
            given = inp.get("files")
            files: list[Any] = given if isinstance(given, list) else []
            raw += [
                str(entry["localPath"])
                for entry in files
                if isinstance(entry, dict) and isinstance(entry.get("localPath"), str)
            ]
        for text in raw:
            path = _absolute(text, cwd)
            if path is None:
                outcome.ask.append(f"DesignSync names `{text[:80]}`, which is not a usable path.")
                continue
            via = f"uploaded by DesignSync {method}"
            self._check_path(path, "read", via, outcome)
            if path.is_dir():
                listed = searched_files(path, git_ignore=False, hidden=True)
                denied = [self.display(p) for p in listed.files if self.read_denied(p)]
                if denied:
                    shown = ", ".join(f"`{name}`" for name in denied[:5])
                    outcome.ask.append(
                        f"DesignSync may upload any file of `{self.display(path)}`, which holds "
                        f"{shown} -- files the read deny list protects."
                    )
        return outcome

    def _recursive_search_allowed(self, used: ShellPath, outcome: RuleOutcome) -> bool:
        """Apply the read deny list to the files a `grep -r` or `rg` reads (PLAN.md D149).

        Args:
            used: The searched path, with its `search` description.
            outcome: Collects the `ask` reason.

        Returns:
            False if a protected file would be read or the listing is incomplete, else True.
        """
        spec = used.search
        assert spec is not None
        for directory in (d for d in spec.directories if d is not None):
            listed = searched_files(
                directory, spec.glob, git_ignore=spec.git_ignore, hidden=spec.hidden
            )
            denied = [self.display(p) for p in listed.files if self.read_denied(p)]
            if denied:
                shown = ", ".join(f"`{name}`" for name in denied[:5])
                more = f" and {len(denied) - 5} more" if len(denied) > 5 else ""
                outcome.ask.append(
                    f"`{used.raw}` ({used.via}) would read {shown}{more}, which the read deny "
                    "list protects."
                )
                return False
            if not listed.complete:
                return False
        return True

    def read_denied(self, path: Path) -> bool:
        """Tell whether reading `path` matches a read deny rule (built-in or `read-deny`).

        Args:
            path: An absolute path.

        Returns:
            True if a read deny rule matches the path's display form (`display`).
        """
        deny = [*BUILT_IN_READ_DENY, *self.lists("read-deny")[0]]
        return _first_match(deny, self.match_form(path)) is not None

    def match_form(self, path: Path) -> str:
        r"""Return the form of `path` that the path rules are matched against (PLAN.md D155).

        Like `display`, except inside a Claude Code worktree: `EnterWorktree` and subagents with
        `isolation: worktree` work in `<workspace>/.claude/worktrees/<name>/`, a full checkout of
        the project. Read relative to the workspace, every file there is below `.claude/`, so the
        write deny rule for `.claude` refused every edit in it -- while the rules anchored at the
        project root (`^CLAUDE\.md$`, `^\.git(/|$)`) never matched the worktree's own copies.
        A path inside a worktree is therefore read relative to the worktree's root, so the
        worktree is judged like the project it is a checkout of.

        Args:
            path: An absolute path.

        Returns:
            The worktree-relative POSIX path for a path inside a worktree ("." for its root),
            else `display(path)`. Only a directory with a `.git` entry counts as a worktree,
            the way `git worktree add` leaves one; anything else below `.claude/` -- a file
            next to the worktrees, a worktree that does not exist yet -- stays as it is and so
            stays protected.
        """
        shown = self.display(path)
        parts = shown.split("/")
        if (
            len(parts) >= 3
            and parts[0] == ".claude"
            and parts[1] == "worktrees"
            and (self.workspace / ".claude" / "worktrees" / parts[2] / ".git").exists()
        ):
            return "/".join(parts[3:]) or "."
        return shown

    def display(self, path: Path) -> str:
        """Return the form of `path` that the path rules match.

        Args:
            path: An absolute path.

        Returns:
            The workspace-relative POSIX path ("." for the workspace itself) if `path` is inside
            the workspace, else the absolute POSIX path.
        """
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()


def _first_match(rules: list[Rule] | tuple[Rule, ...], text: str) -> Rule | None:
    """Return the first rule whose pattern is found in `text`.

    Args:
        rules: The rules to try, in order.
        text: A simple command or a displayed path.

    Returns:
        The matching rule, or None.
    """
    for rule in rules:
        if rule.pattern.search(text):
            return rule
    return None


def _tool_paths(step: Step) -> list[str]:
    """Return the paths a read or edit tool call names.

    Args:
        step: A call of a tool in `READ_PATH_TOOLS` or `WRITE_PATH_TOOLS`.

    Returns:
        `file_path` / `notebook_path` / `path`; for Glob also an absolute `pattern`. A read tool
        without any path reads the working directory (".").
    """
    inp = step.tool_input
    paths = [
        str(inp[key])
        for key in ("file_path", "notebook_path", "path")
        if isinstance(inp.get(key), str) and inp[key]
    ]
    pattern = inp.get("pattern")
    if step.tool_name == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~")):
        paths.append(pattern)
    if not paths and step.tool_name in READ_PATH_TOOLS:
        paths.append(".")
    return paths


def _named_paths(value: Any, key: str = "", depth: int = 0) -> list[str]:
    """Collect the strings of a tool input that name a path (D166).

    Args:
        value: The input, or a part of it.
        key: The argument name `value` is under ("" at the top).
        depth: Nesting depth; inputs deeper than 4 levels are not searched.

    Returns:
        Strings under a `PATH_KEYS` key, and strings that start like a path, without newlines
        and at most 4096 characters long; nothing under a `CONTENT_KEYS` key.
    """
    if depth > 4:
        return []
    if isinstance(value, dict):
        return [
            p
            for k, v in value.items()
            if str(k).lower() not in CONTENT_KEYS
            for p in _named_paths(v, str(k).lower(), depth + 1)
        ]
    if isinstance(value, list):
        return [p for v in value for p in _named_paths(v, key, depth + 1)]
    if not isinstance(value, str) or not value or "\n" in value or len(value) > 4096:
        return []
    looks = value.startswith(("/", "~/", "./", "../", "file://")) or value == "~"
    return [value] if key in PATH_KEYS or looks else []


INTERPRETERS = frozenset(
    {"python", "python3", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "Rscript"}
)
"""Programs whose `-c`/`-e`/`--eval` argument is code (D167)."""

_CODE_OPTIONS = frozenset({"-c", "-e", "-E", "--eval", "-r", "eval", "-p", "--print"})
_LITERAL = re.compile(r"""(['"`])((?:(?!\1).){1,4096}?)\1""")


def inline_code_paths(command: str) -> list[str]:
    r"""Return the string literals of an interpreter's inline code that could be paths (D167).

    `python3 -c "open('.env').read()"` reads `.env`, but the rules saw only one long argument,
    which a deny rule anchored at the end of a path (`.env`, `.aws/credentials`) cannot match.
    The quoted literals of the code are taken out and returned when they look like a path:
    they contain "/" or start with "." or "~". Code that builds its path at run time is still
    the checker's to judge.

    Args:
        command: The command as Claude wrote it, quotes included; every interpreter in it
            (after `&&`, `|`, `;` too) is looked at.

    Returns:
        The literals, in order; [] when no interpreter with inline code is in it.

    Example:
        inline_code_paths("node -e \"fs.readFileSync('.env')\"") == [".env"]
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    found: list[str] = []
    seen_interpreter = False
    for index, word in enumerate(words):
        name = os.path.basename(word)
        attached = word.startswith(("--eval=", "--print=")) or (
            # `python -c'open(".env")'`: the code joined to its flag (D172)
            seen_interpreter
            and (
                re.fullmatch(r"-[ce].+", word) is not None
                # Python's combined flags with the code attached: `-Ic"open('.env')"` (D173)
                or re.fullmatch(r"-[bBdEiIOPqsSuvx]+c.+", word) is not None
            )
        )
        separate = index + 1 < len(words) and (
            word in _CODE_OPTIONS or re.fullmatch(r"-[A-Za-z]*[ce]", word) is not None
        )
        if name in INTERPRETERS or re.fullmatch(r"python[0-9.]*", name):
            seen_interpreter = True
        elif word in ("&&", "||", "|", ";"):
            seen_interpreter = False
        elif seen_interpreter and (attached or separate):
            if word.startswith("--"):
                code = word.partition("=")[2]
            elif attached:
                code = re.sub(r"^-[bBdEiIOPqsSuvx]*[ce]", "", word, count=1)
            else:
                code = words[index + 1]
            for match in _LITERAL.finditer(code):
                literal = match.group(2)
                if "\n" not in literal and ("/" in literal or literal.startswith((".", "~"))):
                    found.append(literal)
    return found


CALU_CONTAINER = re.compile(
    r"^(docker|podman|nerdctl|docker-compose|podman-compose)( |$)"
    r"(?=.*((^|[\s:=/])calu-db([-_:\s/]|$)|calu/db/local-compose\.yml))"
)
"""A container command that names calu's database container or compose project (`calu-db`,
`calu.db.local.PROJECT`), or its compose file `calu/db/local-compose.yml`, whose `name: calu-db`
reaches the container without naming it (D171, D172). Docker access is root-equivalent, so
this narrows the way around D163 -- `docker exec calu-db-postgres-1 psql` -- and does not
close it: a script that looks the container up by id first is still the checker's."""

LAUNCHERS = frozenset({"uv", "uvx", "pipx", "poetry", "pdm", "hatch", "rye", "pixi", "conda"})
"""Programs that start another program named later on their command line (D168)."""


def runs_calu(command: str) -> bool:
    """Tell whether a simple command starts calu, however it is launched (D160, D168, D173).

    Asking wrongly costs a click; missing it lets Claude decide its own escalations. So this
    fails closed:

    - the program is `calu` (any path), or a Python interpreter whose `-m` names `calu` or
      `calu.<module>` -- found past the values of `-X`/`-W` and before a script argument;
    - or it is a launcher (`LAUNCHERS`) and, after its run verb (`run`, `exec`, `x`,
      `tool run`, anywhere among its leading words; none for `uvx` and `pipx run`), a word is
      `calu` (version suffix such as `@latest` or `==0.4` ignored), or `-m`/`--module` names
      calu -- unless that word is the value of a known path or package option
      (`LAUNCH_VALUE_OPTIONS`), contains "/", or comes after a known ordinary program
      (`ORDINARY_PROGRAMS`: `uv run pytest -k calu` runs pytest).

    Wrappers between launcher and calu (`uv run env calu`, `uv run timeout 5 calu`) and
    unknown launcher options (`uv run -n calu`) therefore still count.

    Args:
        command: One simple command, as `shell.command_variants` gives it.

    Returns:
        True if it may start calu.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if not words:
        return False
    program = os.path.basename(words[0])
    if program == "calu":
        return True
    if re.fullmatch(r"python[0-9.]*", program):
        return _python_module_calu(words[1:])
    if program not in LAUNCHERS:
        return False
    rest = words[1:]
    if program not in ("uvx",):
        if program == "pipx" and rest[:1] == ["run"]:
            rest = rest[1:]
        else:
            for index, word in enumerate(rest):
                if word in ("run", "exec", "x") or (
                    word == "tool" and rest[index + 1 : index + 2] == ["run"]
                ):
                    rest = rest[index + (2 if word == "tool" else 1) :]
                    break
                if not word.startswith("-") and (
                    index == 0 or rest[index - 1] not in LAUNCH_VALUE_OPTIONS
                ):
                    return False  # another subcommand: `uv add calu`, `uv pip install calu`
            else:
                return False
    skip = False
    for index, word in enumerate(rest):
        if skip:
            skip = False
            continue
        if word in ("-m", "--module"):
            following = rest[index + 1] if index + 1 < len(rest) else ""
            if re.fullmatch(r"calu(\.\w+)*", following):
                return True
            skip = True
            continue
        if word in LAUNCH_VALUE_OPTIONS:
            skip = True
            continue
        if word.startswith("-"):
            continue
        name = re.split(r"[@=<>~!]", word, maxsplit=1)[0]
        if "/" in name:
            continue
        if name == "calu":
            return True
        if re.fullmatch(r"python[0-9.]*", name):
            return _python_module_calu(rest[index + 1 :])
        if name in ORDINARY_PROGRAMS:
            return False
    return False


def _python_module_calu(rest: list[str]) -> bool:
    """Tell whether a Python interpreter's arguments run `-m calu` or `-m calu.<module>`.

    Args:
        rest: The words after the interpreter.

    Returns:
        True if a `-m` (also combined, `-Im`, and attached, `-mcalu`) names calu before the
        first argument that is neither an option nor the value of `-X`, `-W` or `--check-hash-
        based-pycs`; such an argument is the script, and a `-m` after it belongs to the script.
    """
    skip = False
    for index, word in enumerate(rest):
        if skip:
            skip = False
            continue
        if word in ("-X", "-W", "--check-hash-based-pycs"):
            skip = True
            continue
        if not word.startswith("-"):
            return False
        if re.fullmatch(r"-[A-Za-z]*m", word):
            following = rest[index + 1] if index + 1 < len(rest) else ""
            return re.fullmatch(r"calu(\.\w+)*", following) is not None
        if re.fullmatch(r"-[A-Za-z]*mcalu(\.\w+)*", word):
            return True
        if re.fullmatch(r"-[A-Za-z]*c.*", word):
            return False  # `-c CODE`: the rest is the code's arguments
    return False


LAUNCH_VALUE_OPTIONS = frozenset(
    {
        "--project",
        "--directory",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--from",
        "--python",
        "--package",
        "--extra",
        "--group",
        "--env-file",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--spec",
        "--name",
        "--environment",
        "--env",
        "--manifest-path",
        "--config",
        "--cwd",
        "--refresh-package",
        "--python-preference",
        "-w",
        "-p",
        "-C",
        "-e",
    }
)
"""Launcher options whose separate value is a path, package or setting, not a program to run
(D172, D173). An option not listed here is treated as a flag, which fails closed: its value
is then looked at as a possible program."""

ORDINARY_PROGRAMS = frozenset(
    {
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "black",
        "isort",
        "flake8",
        "pylint",
        "coverage",
        "tox",
        "nox",
        "pre-commit",
        "sphinx-build",
        "mkdocs",
        "jupyter",
        "ipython",
        "pip",
        "npm",
        "npx",
        "node",
        "make",
        "test",
        "lint",
        "build",
        "docs",
    }
)
"""Programs a launcher commonly runs: a `calu` after one of them is its argument (D173)."""


def _absolute(text: str, cwd: Path) -> Path | None:
    """Turn a path as a tool input names it into an absolute path, or None if it cannot be one.

    `~user/...` is expanded like the shell does; an unknown user, a NUL character or a path the
    OS refuses gives None instead of an exception, which would escalate the step as an
    internal error (D168).

    Args:
        text: The path as written.
        cwd: The directory a relative path is relative to.

    Returns:
        The resolved path, or None.
    """
    if "\0" in text:
        return None
    try:
        path = Path(os.path.expanduser(text))
        return (path if path.is_absolute() else cwd / path).resolve()
    except OSError, RuntimeError, ValueError:
        return None


DIFF_LIMIT = 1_000_000
"""Most bytes of a diff file read to find the files it changes."""


def diff_targets(command: str, cwd: Path) -> list[Path]:
    """Return the files that `patch`, `git apply` or `git am` in `command` would change (D169).

    `patch -p1 < fix.diff` writes the files named inside the diff, not on the command line.
    The diff -- a file named by `-i FILE`, `-iFILE`, `--input=FILE`, `< FILE`, `<FILE` or a
    `cat FILE |` in front, the file arguments of `git apply`/`git am`, or a heredoc in the
    command itself -- is read (at most `DIFF_LIMIT` bytes), and each header path is stripped
    the way the tool strips it (D173):

        patch -pN        N leading components
        patch            the base name (and the full path, to be safe)
        git apply/am     -pN, default 1, then `--directory=ROOT` in front

    and resolved against `patch -d DIR` or `git -C DIR`. Quoted names (`"b/CLAUDE.md"`, as
    git writes names with special characters) are unquoted.

    Side effects: reads the diff files.

    Args:
        command: The command as Claude wrote it.
        cwd: The directory it runs in.

    Returns:
        Absolute target paths, without duplicates; [] when no diff can be read, which leaves
        the step to the checker as before.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    segments: list[list[str]] = [[]]
    joiners: list[str] = []
    for word in words:
        if word in ("&&", "||", "|", ";"):
            segments.append([])
            joiners.append(word)
        else:
            segments[-1].append(word)
    jobs: list[tuple[list[str], Path, int | None, str]] = []  # diff lines, base, strip, prefix
    heredoc = _heredoc_lines(command)
    for number, segment in enumerate(segments):
        if not segment:
            continue
        program = os.path.basename(segment[0])
        piped: list[str] = []
        if number > 0 and joiners[number - 1] == "|":
            before = segments[number - 1]
            if before and os.path.basename(before[0]) == "cat":
                piped = [part for part in before[1:] if not part.startswith("-")]
        if program == "patch":
            base, strip, sources = cwd, None, list(piped)
            for i, part in enumerate(segment[1:], start=1):
                following = segment[i + 1] if i + 1 < len(segment) else ""
                if part in ("-d", "--directory") and following:
                    base = _absolute(following, cwd) or cwd
                elif part.startswith("--directory="):
                    base = _absolute(part.partition("=")[2], cwd) or cwd
                elif re.fullmatch(r"-p\d+", part):
                    strip = int(part[2:])
                elif part in ("-p", "--strip") and following.isdigit():
                    strip = int(following)
                elif part.startswith("--strip=") and part[8:].isdigit():
                    strip = int(part[8:])
                elif part in ("-i", "--input", "<") and following:
                    sources.append(following)
                elif part.startswith("--input="):
                    sources.append(part.partition("=")[2])
                elif part.startswith("-i") and len(part) > 2:
                    sources.append(part[2:])
                elif part.startswith("<") and len(part) > 1 and not part.startswith("<<"):
                    sources.append(part[1:])
            for source in sources:
                jobs.append((_read_diff(source, cwd), base, strip, ""))
            if heredoc and not sources:
                jobs.append((heredoc, base, strip, ""))
        elif program == "git":
            base = cwd
            rest = segment[1:]
            while rest and rest[0].startswith("-"):
                option = rest.pop(0)
                if option == "-C" and rest:
                    base = _absolute(rest.pop(0), cwd) or cwd
                elif option in ("-c", "--git-dir", "--work-tree", "--namespace") and rest:
                    rest.pop(0)
            if not rest or rest[0] not in ("apply", "am"):
                continue
            strip, prefix, sources = 1, "", list(piped)
            after = rest[1:]
            for i, part in enumerate(after):
                following = after[i + 1] if i + 1 < len(after) else ""
                if re.fullmatch(r"-p\d+", part):
                    strip = int(part[2:])
                elif part.startswith("--directory="):
                    prefix = part.partition("=")[2].rstrip("/") + "/"
                elif part == "--directory" and following:
                    prefix = following.rstrip("/") + "/"
                elif part == "<" and following:
                    sources.append(following)
                elif part.startswith("<") and len(part) > 1 and not part.startswith("<<"):
                    sources.append(part[1:])
                elif not part.startswith("-") and (
                    i == 0 or after[i - 1] not in ("<", "--directory")
                ):
                    sources.append(part)
            for source in sources:
                lines = _read_diff(source, base) or _read_diff(source, cwd)
                # The paths are relative to the repository's top level, which may be `-C DIR`
                # or the starting directory: both are tried.
                jobs.append((lines, base, strip, prefix))
                if base != cwd:
                    jobs.append((lines, cwd, strip, prefix))
            if heredoc and not sources:
                jobs.append((heredoc, base, strip, prefix))
    targets: list[Path] = []
    seen: set[Path] = set()
    for lines, base, strip, prefix in jobs:
        for index, line in enumerate(lines):
            raw = _diff_path(line, lines[index + 1] if index + 1 < len(lines) else "")
            if raw is None:
                continue
            parts = [part for part in raw.split("/") if part]
            if not parts:
                continue
            if strip is None:
                candidates = [parts[-1], "/".join(parts)]
            else:
                candidates = ["/".join(parts[strip:])] if len(parts) > strip else []
            for candidate in candidates:
                target = _absolute(prefix + candidate, base)
                if target is not None and target not in seen:
                    seen.add(target)
                    targets.append(target)
    return targets


def _read_diff(name: str, base: Path) -> list[str]:
    """Read the lines of a diff file, or nothing.

    Args:
        name: The file as the command names it.
        base: The directory a relative name is relative to.

    Returns:
        Its lines (at most `DIFF_LIMIT` bytes read), or [] when it is no readable file.
    """
    source = _absolute(name, base)
    if source is None or not source.is_file():
        return []
    try:
        with open(source, encoding="utf-8", errors="replace") as handle:
            return handle.read(DIFF_LIMIT).splitlines()
    except OSError:
        return []


def _heredoc_lines(command: str) -> list[str]:
    """Return the lines of the heredocs in `command`, which may carry a diff (D173).

    Args:
        command: The command as written.

    Returns:
        Every line after the first line that holds a `<<`, or [] when there is none.
    """
    lines = command.splitlines()
    for index, line in enumerate(lines):
        if "<<" in line:
            return lines[index + 1 :]
    return []


def _diff_path(line: str, following: str) -> str | None:
    """Return the file a diff header line names, or None for any other line (D169, D172).

    Args:
        line: One line of the diff.
        following: The next line: a `--- x` counts only when a `+++` line follows it, so that
            a removed line of code starting with "-- " inside a hunk is not taken for a file.

    Returns:
        The path as written in the diff (prefix included, quotes removed), or None;
        `/dev/null` gives None.
    """
    path: str | None = None
    if line.startswith("+++ ") or (line.startswith("--- ") and following.startswith("+++ ")):
        path = line[4:].split("\t")[0].strip()
    elif line.startswith("diff --git "):
        path = line.rsplit(" ", 1)[-1]
    elif line.startswith("rename to "):
        path = line[len("rename to ") :].strip()
    if path and len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        try:
            path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        except UnicodeDecodeError:
            path = path[1:-1]
    if not path or path == "/dev/null":
        return None
    return path
