"""Split a Bash command line into simple commands and the paths they read or write.

The rules (`calu.checkers.rules`) match each simple command of a Bash call
separately, so a chain such as `ls && rm -rf x` is not allowed by a rule for `ls`. The parser is
tree-sitter-bash; it handles lists, pipelines, subshells, `$(...)`, heredocs and redirects:

    "cd src && cat a.py | grep x > out.txt"     (started in /w)
      -> commands: ["cd src", "cat a.py", "grep x"]
      -> reads:    a.py -> /w/a.py or /w/src/a.py    arguments of commands
      -> writes:   out.txt -> /w/out.txt or /w/src/out.txt   redirect target

Paths are heuristic and err on the side of "unknown":

- Every argument that is not an option counts as a read; the arguments of known writing
  commands (`rm`, `mv`, `cp`, `tee`, `sed -i`, ...) and the targets of `>`/`>>` count as writes.
  Option values (`--out=x`, `-ox`) count as both, because some options write files.
- `cd`/`pushd` do not replace the working directory: a later relative path is resolved against
  every directory the command line may be in at that point (the start directory and each `cd`
  target), because a `cd` can fail, run in a subshell or in a pipeline. The rules check every
  candidate.
- A path is unknown (`None` among `ShellPath.paths`) if it uses a variable other than `$HOME`,
  an unquoted glob or brace pattern (`.en?`, `*.py`, `{a,b}`), follows `cd $X`, or is searched
  recursively (`grep -r`, `diff -r`, `rg`): the rules cannot tell which files it covers.
"""

import glob
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

_PARSER = Parser(Language(tree_sitter_bash.language()))

WRITE_COMMANDS = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "mv",
        "cp",
        "install",
        "rsync",
        "ln",
        "tee",
        "touch",
        "truncate",
        "mkdir",
        "chmod",
        "chown",
        "chgrp",
        "patch",
    }
)
"""Commands whose path arguments are all treated as writes (sources of `cp`/`mv` included)."""

OUTPUT_OPTIONS: dict[str, frozenset[str]] = {
    "curl": frozenset({"-o", "--output"}),
    "wget": frozenset({"-O", "--output-document"}),
    "sort": frozenset({"-o", "--output"}),
    "gcc": frozenset({"-o"}),
    "cc": frozenset({"-o"}),
    "clang": frozenset({"-o"}),
    "g++": frozenset({"-o"}),
}
"""Options whose separate value is a file the command writes (`curl -o FILE`, D148). The
attached forms (`--output=FILE`, `-oFILE`) are covered by `_option_value` for every command."""

IN_PLACE_COMMANDS = frozenset({"sed", "perl"})
"""Commands that write their file arguments when called with `-i` / `--in-place`."""

_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_READ_REDIRECTS = frozenset({"<"})
_STATEMENT_TYPES = frozenset({"declaration_command", "unset_command", "test_command"})
_GREP_RECURSIVE = re.compile(r"^(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)$")


@dataclass(frozen=True)
class ShellPath:
    """A path that a command reads or writes.

    `raw` is the text as written. `paths` holds the absolute, normalised path for each directory
    the command may run in (see the module docstring); an entry is None if the path cannot be
    resolved. `via` names where the path came from, e.g. "argument of rm" or "redirect >".
    """

    raw: str
    paths: tuple[Path | None, ...]
    kind: Literal["read", "write"]
    via: str


@dataclass
class ParsedCommand:
    """The result of `parse_command`.

    `commands` are the simple commands in source order, each as its words joined by single
    spaces with quotes removed (e.g. `git commit -m fix x`). Variable assignments before a
    command stay part of it (`FOO=1 make`). `error` is set if the command line has a syntax
    error; `commands` and `paths` then hold only what could be parsed.
    """

    commands: list[str] = field(default_factory=list)
    paths: list[ShellPath] = field(default_factory=list)
    error: str | None = None


WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    # name: (options that take a separate value, positional arguments before the command)
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "nohup": (frozenset(), 0),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p", "-P", "-u"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "xargs": (
        frozenset({"-I", "-i", "-n", "-P", "-d", "-E", "-e", "-L", "-l", "-s", "-a"}),
        0,
    ),
    "stdbuf": (frozenset({"-i", "-o", "-e"}), 0),
    "setsid": (frozenset(), 0),
    "chrt": (frozenset(), 1),
    "taskset": (frozenset(), 1),
    "unbuffer": (frozenset(), 0),
}
"""Commands that run the command after them, with how to skip their own arguments (D145)."""

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def command_variants(command: str) -> list[str]:
    """Return a simple command and the forms of it a deny rule must also see (PLAN.md D145).

    Deny rules are anchored at the start of a command (`^(sudo|su|doas)( |$)`), and three
    ordinary spellings put something else there: a variable assignment (`X=1 sudo ...`), a
    command that runs the next one (`env`, `nohup`, `timeout 5`, `xargs`, ...) and a path
    (`/usr/bin/sudo`). Each of them used to take a command past the deny list. This strips them
    one layer at a time and returns every stage, so a rule written for the plain command matches
    all of them. It is used for the deny list only: an allow rule must match the command as
    written, so the extra forms can only make a step wait for a person, never let one through.

    Args:
        command: One simple command as the splitter recorded it.

    Returns:
        The command itself first, then each stripped form that differs from the one before,
        without duplicates. A command shlex cannot split is returned alone.

    Example:
        command_variants("X=1 nohup /usr/bin/sudo ls") ==
            ["X=1 nohup /usr/bin/sudo ls", "nohup /usr/bin/sudo ls", "/usr/bin/sudo ls",
             "sudo ls"]
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return [command]
    variants = [command]

    def add(tokens: list[str]) -> None:
        """Append the joined tokens to `variants` unless empty or already there.

        Args:
            tokens: The words of one stripped form.
        """
        text = " ".join(tokens)
        if tokens and text not in variants:
            variants.append(text)

    for _ in range(10):  # each round removes at least one word; nesting deeper is not usual
        before = list(words)
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        add(words)
        if words and "/" in words[0]:
            words = [os.path.basename(words[0]), *words[1:]]
            add(words)
        if words and words[0] in WRAPPERS:
            valued, positional = WRAPPERS[words[0]]
            rest = words[1:]
            while rest and (rest[0].startswith("-") or (words[0] == "env" and "=" in rest[0])):
                option = rest.pop(0)
                if option == "--":
                    break
                if option in valued and rest:
                    rest.pop(0)
            rest = rest[positional:]
            words = rest
            add(words)
        if words == before:
            break
    return variants


def parse_command(command: str, cwd: Path, home: Path | None = None) -> ParsedCommand:
    """Split a Bash command line into simple commands and the paths they read or write.

    Pure apart from `Path.resolve` (which follows existing symlinks); never raises for any input
    text.

    Args:
        command: The command line, possibly with several lines, pipes and substitutions.
        cwd: The directory the command starts in; relative paths resolve against it.
        home: The home directory for `~` and `$HOME` (default: `Path.home()`).

    Returns:
        The simple commands and paths. Example: `parse_command("ls && rm -rf b", Path("/w"))`
        gives commands `["ls", "rm -rf b"]` and one write path with `paths == (/w/b,)`.
    """
    walker = _Walker(command.encode("utf-8"), cwd, home if home is not None else Path.home())
    tree = _PARSER.parse(walker.source)
    walker.visit(tree.root_node)
    if tree.root_node.has_error:
        walker.result.error = "The command has a syntax error that the parser cannot resolve."
    return walker.result


@dataclass(frozen=True)
class _Word:
    """One word of a command: its unquoted value and whether it has an unquoted glob."""

    value: str
    glob: bool


class _Walker:
    """Collects commands and paths from a tree-sitter-bash syntax tree (see `parse_command`)."""

    def __init__(self, source: bytes, cwd: Path, home: Path) -> None:
        """Start with an empty result.

        Args:
            source: The UTF-8 command text.
            cwd: The starting directory.
            home: The home directory.
        """
        self.source = source
        self.cwds: list[Path | None] = [cwd]
        self.home = home
        self.result = ParsedCommand()

    def text(self, node: Node) -> str:
        """Return the source text of `node`.

        Args:
            node: A syntax node.

        Returns:
            The text, decoded as UTF-8 (invalid bytes replaced).
        """
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def word(self, node: Node) -> _Word:
        """Return the value of a word node with shell quotes removed (best effort).

        Args:
            node: A word, string, concatenation or expansion node.

        Returns:
            The unquoted text (the raw text if it cannot be unquoted), and whether the raw text
            has an unquoted glob or brace pattern.
        """
        raw = self.text(node)
        try:
            parts = shlex.split(raw)
            value = " ".join(parts) if parts else ""
        except ValueError:
            value = raw
        return _Word(value, _has_glob(raw))

    def visit(self, node: Node) -> None:
        """Collect the commands and paths below `node`, in source order.

        Args:
            node: A syntax node.
        """
        kind = node.type
        if kind == "command":
            self.command(node)
        elif kind in _STATEMENT_TYPES or (
            kind == "variable_assignment"
            and node.parent is not None
            and node.parent.type not in ("command", *_STATEMENT_TYPES)
        ):
            self.result.commands.append(" ".join(self.text(node).split()))
            self.visit_children(node)
        elif kind == "file_redirect":
            self.redirect(node)
        else:
            self.visit_children(node)

    def visit_children(self, node: Node) -> None:
        """Visit all children of `node`.

        Args:
            node: A syntax node.
        """
        for child in node.children:
            self.visit(child)

    def command(self, node: Node) -> None:
        """Record a simple command, its path arguments and the commands nested in its words.

        Args:
            node: A `command` node.
        """
        prefix: list[str] = []
        argv: list[_Word] = []
        for i, child in enumerate(node.children):
            if child.type == "variable_assignment":
                prefix.append(self.text(child))
            elif child.type == "file_redirect":
                self.redirect(child)
                continue
            elif child.type == "command_name" or node.field_name_for_child(i) == "argument":
                argv.append(self.word(child))
            self.visit_children(child)  # commands inside $(...), <(...)
        if not argv and not prefix:
            return
        self.result.commands.append(" ".join(prefix + [w.value for w in argv]))
        if not argv:
            return
        name = os.path.basename(argv[0].value)
        args = argv[1:]
        values = [a.value for a in args]
        if name in ("cd", "pushd"):
            self.change_directory(args)
            return
        if name == "git":
            self.git_objects(args)
            self.git_writes(args)
        in_place = name in IN_PLACE_COMMANDS and any(
            a == "--in-place" or (a.startswith("-i") and not a.startswith("--")) for a in values
        )
        writes = name in WRITE_COMMANDS or in_place
        recursive = name in ("rg", "ag") or (
            name in ("grep", "egrep", "fgrep", "diff")
            and any(
                _GREP_RECURSIVE.match(a) or a in ("-d", "--directories=recurse") for a in values
            )
        )
        outputs = OUTPUT_OPTIONS.get(name, frozenset())
        output_next = False
        for arg in args:
            value = arg.value
            if output_next:
                output_next = False
                self.add_path(arg, "write", f"output file of {name}")
                continue
            if value in outputs:
                output_next = True
                continue
            if name == "dd" and "=" in value and not value.startswith("-"):
                key, _, target = value.partition("=")
                if key in ("if", "of"):
                    self.add_path(
                        _Word(target, arg.glob), "write" if key == "of" else "read", f"{key}= of dd"
                    )
                continue
            if value.startswith("-"):
                option_value = _option_value(value)
                if option_value:
                    word = _Word(option_value, arg.glob)
                    self.add_path(word, "read", f"value of an option of {name}")
                    self.add_path(word, "write", f"value of an option of {name}")
                continue
            if not value or "://" in value:
                continue
            via = f"argument of {name}"
            if recursive:
                self.result.paths.append(
                    ShellPath(value, (None,), "read", f"{via}, searched recursively")
                )
            else:
                self.add_path(arg, "write" if writes else "read", via)
        if recursive:
            # Without a path argument (`rg x`, `grep -r x`) the working directory is searched.
            self.result.paths.append(
                ShellPath(
                    ".", (None,), "read", f"working directory, searched recursively by {name}"
                )
            )

    def git_objects(self, args: list[_Word]) -> None:
        """Record the file that `git show REV:PATH` or `git cat-file ... REV:PATH` prints.

        Such a command prints a file's content from git's history, so for the read lists it is
        a read of PATH (D147); `git show HEAD:.env` was allowed because `git show` is.

        Args:
            args: The arguments after `git`. Global options before the subcommand (`-C dir`,
                `-c key=value`, `--no-pager`) are skipped.
        """
        values = [a.value for a in args]
        i = 0
        while i < len(values) and values[i].startswith("-"):
            i += 2 if values[i] in ("-C", "-c") else 1
        if i >= len(values) or values[i] not in ("show", "cat-file"):
            return
        for value in values[i + 1 :]:
            if value.startswith("-") or ":" not in value:
                continue
            path = value.split(":", 1)[1].removeprefix("./")
            if path:
                self.add_path(
                    _Word(path, False), "read", f"file in git's history, via git {values[i]}"
                )

    def git_writes(self, args: list[_Word]) -> None:
        """Record the files that git subcommands overwrite without naming them as writes (D148).

        `git config KEY VALUE` writes `.git/config` (or `~/.gitconfig` with `--global`), which
        decides which hooks and programs git runs; `git checkout -- PATH` and `git restore PATH`
        overwrite PATH with another version. All other arguments stay reads.

        Args:
            args: The arguments after `git`.
        """
        values = [a.value for a in args]
        i = 0
        while i < len(values) and values[i].startswith("-"):
            i += 2 if values[i] in ("-C", "-c") else 1
        if i >= len(values):
            return
        sub, rest = values[i], args[i + 1 :]
        options = [a.value for a in rest if a.value.startswith("-")]
        plain = [a for a in rest if not a.value.startswith("-")]
        if sub == "config":
            reading = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin"}
            if reading & set(options) or len(plain) < 2:
                return
            if "--global" in options:
                target = "~/.gitconfig"
            elif "--system" in options:
                target = "/etc/gitconfig"
            else:
                target = ".git/config"
            self.add_path(_Word(target, False), "write", "configuration written by git config")
        elif sub in ("checkout", "restore") and ("--" in values or sub == "restore"):
            after = rest[[a.value for a in rest].index("--") + 1 :] if "--" in values else plain
            for arg in after:
                if not arg.value.startswith("-"):
                    self.add_path(arg, "write", f"file overwritten by git {sub}")

    def change_directory(self, args: list[_Word]) -> None:
        """Add the target of `cd`/`pushd` to the directories later relative paths may refer to.

        Args:
            args: The arguments of `cd`; none means the home directory. A variable, glob, `-` or
                more than one argument adds an unknown directory.
        """
        targets = [a for a in args if not a.value.startswith("-") or a.value == "-"]
        if not targets:
            self.cwds.append(self.home)
            return
        if len(targets) != 1 or targets[0].value == "-":
            self.cwds.append(None)
            return
        self.cwds.extend(p for p in self.resolve(targets[0]) if p not in self.cwds)

    def redirect(self, node: Node) -> None:
        """Record the file that a redirect reads or writes.

        Args:
            node: A `file_redirect` node, e.g. `> out.txt`, `2>&1` or `< in.txt`.
        """
        operator = ""
        destination: Node | None = None
        for i, child in enumerate(node.children):
            if node.field_name_for_child(i) == "destination":
                destination = destination or child
            elif not child.is_named:
                operator = self.text(child)
        if destination is None:
            return
        self.visit_children(destination)
        target = self.word(destination)
        if operator in (">&", "<&") and (target.value.isdigit() or target.value == "-"):
            return  # duplicates a file descriptor
        if operator in _WRITE_REDIRECTS or operator == ">&":
            self.add_path(target, "write", f"redirect {operator}")
        elif operator in _READ_REDIRECTS or operator == "<&":
            self.add_path(target, "read", f"redirect {operator}")

    def add_path(self, word: _Word, kind: Literal["read", "write"], via: str) -> None:
        """Resolve `word` against all candidate directories and record it.

        Args:
            word: The path as written.
            kind: Whether the command reads or writes it.
            via: Where the path came from, for messages.
        """
        self.result.paths.append(ShellPath(word.value, self.resolve(word), kind, via))

    def expand(self, pattern: str) -> list[Path]:
        """Return the existing paths a glob matches now, for each known working directory.

        Python's `glob` follows the shell's default: `*` and `?` do not match a leading dot
        unless the pattern's part starts with one, so `cat *` does not list `.env` and
        `cat .e*` does. Braces, extended globs and variables are not expanded.

        Args:
            pattern: The glob as written, quotes removed.

        Returns:
            Absolute, normalised matches, at most `GLOB_LIMIT`; [] when the pattern holds
            something this does not expand.
        """
        text = _expand_home(pattern, self.home)
        if any(mark in text for mark in ("$", "`", "{", "(")):
            return []
        bases = [None] if Path(text).is_absolute() else [c for c in self.cwds if c is not None]
        found: list[Path] = []
        for base in bases:
            for match in glob.iglob(text, root_dir=base):
                found.append(_normalise(Path(match) if base is None else base / match))
                if len(found) >= GLOB_LIMIT:
                    return found
        return found

    def resolve(self, word: _Word) -> tuple[Path | None, ...]:
        """Return the absolute path of `word` for each candidate working directory.

        Args:
            word: A path; `~`, `~/x`, `$HOME/x` and `${HOME}/x` are expanded.

        Returns:
            One entry per candidate directory (one entry for absolute paths). An entry is None
            if the word has another variable or a command substitution, or if that directory is
            unknown. A glob gives what it matches now (`expand`) followed by None. Symlinks of
            existing parts are followed, except below `/dev` and `/proc`.
        """
        if word.glob:
            # What the glob matches now, so that a deny rule sees `cat .e*` reading `.env`
            # (D147) -- plus None, because the files may change before the command runs and a
            # glob therefore never counts as a known set of paths.
            return (*self.expand(word.value), None)
        text = _expand_home(word.value, self.home)
        if "$" in text or "`" in text:
            return (None,)
        path = Path(text)
        if path.is_absolute():
            return (_normalise(path),)
        return tuple(None if cwd is None else _normalise(cwd / path) for cwd in self.cwds)


GLOB_LIMIT = 1000
"""Most matches of one glob that `expand` returns."""


def _expand_home(text: str, home: Path) -> str:
    """Replace a leading `~`, `$HOME` or `${HOME}` with the home directory.

    Args:
        text: A path as written.
        home: The home directory.

    Returns:
        The text with the prefix replaced, or unchanged.
    """
    for prefix in ("${HOME}", "$HOME", "~"):
        if text == prefix or text.startswith(prefix + "/"):
            return str(home) + text[len(prefix) :]
    return text


def _normalise(path: Path) -> Path:
    """Return `path` without `..` parts, with symlinks followed except below /dev and /proc.

    Args:
        path: An absolute path.

    Returns:
        The normalised path.
    """
    plain = Path(os.path.normpath(path))
    if plain.parts[1:2] in (("dev",), ("proc",)):
        return plain
    try:
        return path.resolve()
    except OSError, RuntimeError:
        return plain


def _option_value(option: str) -> str | None:
    """Return the value attached to an option, or None.

    Args:
        option: An argument starting with "-", e.g. `--output=x.txt`, `-ox.txt`, `-la` or `-`.

    Returns:
        "x.txt" for `--output=x.txt`; the text after the first letter for short options with
        more than one letter (`-ox.txt` -> "x.txt", `-la` -> "a"; it may be a path); None for
        `--flag`, `-x` and `-`.
    """
    if option.startswith("--"):
        return option.partition("=")[2] or None
    return option[2:] or None


def _has_glob(raw: str) -> bool:
    """Return True if the raw shell word has an unquoted glob or brace pattern.

    Args:
        raw: The word as written, with quotes, e.g. `.en?`, `"*.py"` or `{a,b}.txt`.

    Returns:
        True for unquoted `*`, `?`, `[`, extglob openers (`@(`, `!(`, `+(`) and braces with a
        comma or `..`; False if these only occur inside quotes or after a backslash.
    """
    quote = ""
    escaped = False
    for i, ch in enumerate(raw):
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "*?[" or (ch in "@!+" and raw[i + 1 : i + 2] == "("):
            return True
        elif ch == "{" and ("," in raw[i:] or ".." in raw[i:]):
            return True
    return False
