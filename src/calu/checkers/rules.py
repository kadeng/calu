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
            for used in parsed.paths:
                for candidate in used.paths:
                    if candidate is not None and _is_calu_compose(candidate):
                        all_allowed = False
                        outcome.ask.append(
                            f"`{command[:60]}` uses calu's own compose file "
                            f"`{self.display(candidate)}`, which reaches its database container "
                            "(built-in, D171, D174)."
                        )
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
    found: list[str] = []
    interpreter_named = re.search(
        r"(^|[\s;&|(/])(python[0-9.]*|node|nodejs|deno|bun|perl|ruby|php|lua|Rscript)(\s|$)",
        command,
    )
    if interpreter_named and ("<<" in command or "\n" in command):
        # A heredoc or a script on several lines given to an interpreter (`python3 - <<EOF`):
        # its literals are code like `-c CODE` (D174).
        found += _path_literals(command.partition("\n")[2])
    try:
        words = shlex.split(command)
    except ValueError:
        # Unbalanced quotes (an apostrophe in a comment): look at the whole command (D174).
        return found + (_path_literals(command) if interpreter_named else [])
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
    """Tell whether a simple command may start calu, however it is launched (D160-D174).

    Asking wrongly costs a click; missing it lets Claude decide its own escalations. So this
    fails closed:

    - the program is `calu` (any path), or a Python interpreter whose `-m` names `calu` or
      `calu.<module>` (`_python_module_calu`);
    - or it is a launcher (`LAUNCHERS`): its leading words are read until the run verb (`run`,
      `exec`, `x`, `tool run`; none for `uvx` and `pipx run`) -- a known non-running
      subcommand (`NON_RUN_SUBCOMMANDS`: `uv add`, `uv pip`, `poetry install`) ends the check,
      any other word counts as an option's value. After the verb, a word counts when it is
      `calu` (version suffix such as `@latest` ignored), a path to a program named calu
      (`.venv/bin/calu`, `./calu`, `~/...`, `$VAR/...`), or a `-m`/`--module` naming calu;
      not when it is the value of a known option (`launch_value_options`) or a source path
      (`src/calu`). A `-m` naming another module, an ordinary program (`ORDINARY_PROGRAMS`)
      or a hatch `env:script` ends the check, except that a `-m calu` among its arguments
      still counts (`uv run coverage run -m calu`).

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
    values = launch_value_options(program)
    rest = words[1:]
    if program == "pipx" and rest[:1] == ["run"]:
        rest = rest[1:]
    elif program != "uvx":
        skip = False
        for index, word in enumerate(rest):
            if skip:
                skip = False
                continue
            if word in ("run", "exec", "x"):
                rest = rest[index + 1 :]
                break
            if word == "tool" and rest[index + 1 : index + 2] == ["run"]:
                rest = rest[index + 2 :]
                break
            if word.startswith("-"):
                skip = word in values
                continue
            if word in NON_RUN_SUBCOMMANDS.get(program, frozenset()):
                return False
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
            return _mentions_module_calu(rest[index + 2 :])
        if word in values:
            skip = True
            continue
        if word.startswith("-"):
            continue
        name = re.split(r"[@=<>~!]", word, maxsplit=1)[0] if not word.startswith("~") else word
        if "/" in name:
            if os.path.basename(name) == "calu" and (
                "/bin/" in name or name.startswith(("./", "~", "$", "/"))
            ):
                return True
            continue
        if name == "calu":
            return True
        if re.fullmatch(r"python[0-9.]*", name):
            return _python_module_calu(rest[index + 1 :])
        if name in ORDINARY_PROGRAMS or (program == "hatch" and ":" in name):
            return _mentions_module_calu(rest[index + 1 :])
    return False


def _mentions_module_calu(rest: list[str]) -> bool:
    """Tell whether `-m calu` (or `-m calu.<module>`) appears among `rest` (D174).

    Args:
        rest: The arguments of a program a launcher runs.

    Returns:
        True for `coverage run -m calu`, `ipython -m calu.cli` and the like.
    """
    return any(
        re.fullmatch(r"-[A-Za-z]*m", word) is not None
        and index + 1 < len(rest)
        and re.fullmatch(r"calu(\.\w+)*", rest[index + 1]) is not None
        for index, word in enumerate(rest)
    )


def _python_module_calu(rest: list[str]) -> bool:
    """Tell whether a Python interpreter's arguments run `-m calu` or `-m calu.<module>`.

    Args:
        rest: The words after the interpreter.

    Returns:
        True if a `-m` (also combined, `-Im`, and attached, `-mcalu`) names calu before the
        first argument that is neither an option nor the value of `-X`/`-W` (separate or
        attached: `-X dev`, `-Wonce`); such an argument is the script, and a `-m` after it
        belongs to the script. `-c CODE` ends it as well.
    """
    skip = False
    for index, word in enumerate(rest):
        if skip:
            skip = False
            continue
        if word in ("-X", "-W", "--check-hash-based-pycs"):
            skip = True
            continue
        if word.startswith(("-X", "-W")):
            continue  # attached value: `-Wonce`, `-Xtracemalloc`
        if not word.startswith("-"):
            return False
        if re.fullmatch(r"-[A-Za-z]*m", word):
            following = rest[index + 1] if index + 1 < len(rest) else ""
            return re.fullmatch(r"calu(\.\w+)*", following) is not None
        if re.fullmatch(r"-[A-Za-z]*mcalu(\.\w+)*", word):
            return True
        if re.fullmatch(r"-[bBdEiIOPqsSuv]*c.*", word):
            return False  # `-c CODE`: the rest is the code's arguments
    return False


COMMON_VALUE_OPTIONS = frozenset(
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
        "--only-group",
        "--no-group",
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
        "--color",
        "--cache-dir",
        "--prefix",
    }
)
"""Long launcher options whose separate value is not the program they start (D172-D174)."""

LAUNCHER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "uv": frozenset({"-w", "-p", "-C", "-i", "-f", "-P"}),
    "uvx": frozenset({"-w", "-p", "-C", "-i", "-f", "-P"}),
    "pipx": frozenset({"-p"}),
    "poetry": frozenset({"-P", "-C", "-E"}),
    "pdm": frozenset({"-p", "-G", "-g"}),
    "hatch": frozenset({"-e", "-p"}),
    "conda": frozenset({"-n", "-p"}),
    "pixi": frozenset({"-e", "-p"}),
    "rye": frozenset(),
}
"""Short launcher options that take a separate value, per launcher: uv's `-n` is a flag, conda's
takes the environment name (D174)."""


def launch_value_options(launcher: str) -> frozenset[str]:
    """Return the options of `launcher` whose separate value is not the program it starts.

    Args:
        launcher: The launcher's name, e.g. "uv".

    Returns:
        `COMMON_VALUE_OPTIONS` plus its own short options.
    """
    return COMMON_VALUE_OPTIONS | LAUNCHER_VALUE_OPTIONS.get(launcher, frozenset())


NON_RUN_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "uv": frozenset(
        {
            "add",
            "remove",
            "sync",
            "lock",
            "pip",
            "venv",
            "python",
            "build",
            "publish",
            "init",
            "tree",
            "export",
            "cache",
            "self",
            "version",
            "format",
            "auth",
        }
    ),
    "poetry": frozenset(
        {
            "install",
            "add",
            "remove",
            "lock",
            "update",
            "build",
            "publish",
            "show",
            "config",
            "init",
            "new",
            "check",
            "version",
            "shell",
            "export",
            "search",
            "source",
            "self",
        }
    ),
    "pdm": frozenset({"install", "add", "remove", "lock", "update", "build", "publish", "init"}),
    "hatch": frozenset({"build", "publish", "new", "version", "clean", "config", "dep"}),
    "pipx": frozenset({"install", "uninstall", "upgrade", "list", "inject", "ensurepath"}),
    "conda": frozenset({"install", "create", "remove", "update", "list", "env", "activate"}),
    "pixi": frozenset({"add", "remove", "install", "init", "list"}),
    "rye": frozenset({"add", "remove", "sync", "lock", "build", "publish", "init"}),
}
"""Subcommands that do not start a program: a `calu` after them is a package or a name."""

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
        "ty",
        "pdoc",
        "bandit",
        "vulture",
        "codespell",
        "interrogate",
        "pyupgrade",
        "deptry",
    }
)
"""Programs a launcher commonly runs: a `calu` after one of them is its argument, unless it
follows `-m` (D173, D174)."""


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


DIFF_TOOLS = re.compile(r"(^|[\s;&|(/])(patch|git\s.*\b(apply|am))(\s|$)")
"""A command that applies a diff somewhere in it, behind a wrapper or not (D169, D174)."""

MAX_STRIP = 3
"""Strip levels tried for each path of a diff (`-p0` to `-p3`), plus the base name."""


def diff_targets(command: str, cwd: Path) -> list[Path]:
    """Return the files a diff applied by `command` may change (PLAN.md D169, D174).

    `patch` and `git apply`/`git am` write the files named inside the diff, which the command
    line does not show. Three rounds of parsing their options precisely each missed spellings
    (`-Np1`, `-p 2`, `-dDIR`, wrappers, heredocs mistaken for file names), so this fails
    closed instead: whenever such a tool appears anywhere in the command,

    - the diff text is every heredoc line of the command plus every existing file the command
      names (at most `DIFF_LIMIT` bytes each), so `cat > fix.diff <<EOF ... patch < fix.diff`
      is covered too;
    - the header paths are those of `+++`/`---`, context-diff `***`/`---`, `diff --git`,
      `rename from`/`rename to` and `copy to` lines, quotes removed;
    - each path is tried with 0 to `MAX_STRIP` leading components stripped and as its base
      name, relative to the starting directory and to every `-d`/`-C`/`--directory` value.

    A diff that changes a nested `docs/CLAUDE.md` therefore asks as if it changed the root
    one -- the price of not guessing the strip level.

    Side effects: reads the files the command names.

    Args:
        command: The command as Claude wrote it.
        cwd: The directory it runs in.

    Returns:
        Absolute candidate paths, without duplicates; [] when no diff tool is in the command.
    """
    if not DIFF_TOOLS.search(command):
        return []
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    bases = [cwd]
    for index, word in enumerate(words):
        value = ""
        if word in ("-d", "-C", "--directory") and index + 1 < len(words):
            value = words[index + 1]
        elif word.startswith("--directory="):
            value = word.partition("=")[2]
        elif re.fullmatch(r"-[dC].+", word):
            value = word[2:]
        if value:
            found = _absolute(value, cwd)
            if found is not None and found not in bases:
                bases.append(found)
    lines = list(_heredoc_lines(command))
    read: set[Path] = set()
    for word in words:
        for name in {word, word.lstrip("<"), word.partition("=")[2], word[2:]}:
            if not name or len(read) >= 50:
                continue
            for base in bases:
                source = _absolute(name, base)
                if source is not None and source not in read and source.is_file():
                    read.add(source)
                    lines += _read_diff(str(source), base)
    targets: list[Path] = []
    seen: set[Path] = set()
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        for raw in _diff_paths(line, following):
            parts = [part for part in raw.split("/") if part]
            if not parts:
                continue
            candidates = {"/".join(parts[k:]) for k in range(min(MAX_STRIP, len(parts) - 1) + 1)}
            candidates.add(parts[-1])
            for candidate in candidates:
                for base in bases:
                    target = _absolute(candidate, base)
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


def _diff_paths(line: str, following: str) -> list[str]:
    """Return the files a diff header line names (D169, D172, D174).

    Args:
        line: One line of the diff.
        following: The next line: `--- x` counts only before `+++`, and `*** x` (a context
            diff) only before `--- `, so that a removed or changed line of code inside a hunk
            is not taken for a file name.

    Returns:
        The paths as written (prefix included, quotes removed); `/dev/null` is left out.
    """
    found: list[str] = []
    if line.startswith("+++ ") or (
        line.startswith(("--- ", "*** ")) and following.startswith(("+++ ", "--- "))
    ):
        found.append(line[4:].split("\t")[0].strip())
    elif line.startswith("diff --git "):
        found += line[len("diff --git ") :].split(" ")[-2:]
    elif line.startswith(("rename from ", "rename to ", "copy to ", "copy from ")):
        found.append(line.split(" ", 2)[2].strip())
    result: list[str] = []
    for path in found:
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            try:
                path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
            except UnicodeDecodeError:
                path = path[1:-1]
        if path and path != "/dev/null":
            result.append(path)
    return result


def _path_literals(code: str) -> list[str]:
    """Return the quoted literals of `code` that look like paths (D167, D174).

    Args:
        code: Source code, or a command holding some.

    Returns:
        The literals that contain "/" or start with "." or "~", in order.
    """
    found: list[str] = []
    for quote in "'\"`":
        # Each kind of quote on its own, so that `"open('.env')"` yields `.env` as well.
        for match in re.finditer(quote + r"([^" + quote + r"\n]{1,4096})" + quote, code):
            literal = match.group(1)
            if ("/" in literal or literal.startswith((".", "~"))) and literal not in found:
                found.append(literal)
    return found


def _is_calu_compose(path: Path) -> bool:
    """Tell whether `path` is calu's own compose file, by its content (D174).

    `docker compose -f local-compose.yml down -v` from calu's db folder names neither the
    container nor the project, so the name in the command is not enough; the file itself says
    `name: calu-db`.

    Args:
        path: A path a command names.

    Returns:
        True for a readable YAML file named like a compose file whose project is `calu-db`.
    """
    if not re.search(r"compose.*\.ya?ml$", path.name) or not path.is_file():
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return re.search(r"(?m)^name:\s*[\"']?calu-db[\"']?\s*$", head) is not None
