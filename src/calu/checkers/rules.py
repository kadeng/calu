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

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from calu.checkers.model import Step
from calu.checkers.search import searched_files
from calu.checkers.shell import command_variants, parse_command

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
        if tool != "Bash" and tool not in READ_PATH_TOOLS and tool not in WRITE_PATH_TOOLS:
            return outcome
        for name in [*RULE_LISTS, "unknown"]:
            for error in self.lists(name)[1]:
                outcome.ask.append(f"The rule list {error}. Fix it to continue without asking.")
        cwd = Path(step.cwd or self.workspace)
        if not cwd.is_absolute():
            cwd = self.workspace / cwd
        all_allowed = True
        parts = 0
        if tool == "Bash":
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
            for simple in parsed.commands:
                parts += 1
                if self._deny_command(simple, outcome):
                    continue
                if not _first_match(self.lists("command-allow")[0], simple):
                    all_allowed = False
            for used in parsed.paths:
                parts += 1
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
                path = Path(raw).expanduser()
                path = (path if path.is_absolute() else cwd / path).resolve()
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
            rule = _first_match(self.lists("command-deny")[0], variant)
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
        rule = _first_match(deny, shown)
        if rule is not None:
            outcome.ask.append(
                f"{'Reading' if kind == 'read' else 'Writing'} `{shown}` ({via}) matches the deny "
                f"rule {rule.describe()}."
            )
            return False
        if _first_match(self.lists(f"{kind}-allow")[0], shown):
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

    def read_denied(self, path: Path) -> bool:
        """Tell whether reading `path` matches a read deny rule (built-in or `read-deny`).

        Args:
            path: An absolute path.

        Returns:
            True if a read deny rule matches the path's display form (`display`).
        """
        deny = [*BUILT_IN_READ_DENY, *self.lists("read-deny")[0]]
        return _first_match(deny, self.display(path)) is not None

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
