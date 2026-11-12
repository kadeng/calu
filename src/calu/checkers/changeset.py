"""The changeset a commit would create, and how to recognise a commit (PLAN.md D26).

A step checker judges one edit with limited context, which is the right altitude for style,
documentation and local design, and the wrong one for "is this change correct and complete?".
That question has an answer only once the change exists. The commit is the moment it does:

    Claude runs a Bash line that contains `git commit`
        rules (deny lists)        a force push or a deletion still asks the human first
        classify_commit(line)
            COMPOUND              DENY, deterministically: "run `git commit` as its own command"
            PLAIN                 calu collects the diff: `git diff --stat` and `git diff`
                                  the command checkers judge the line        (as for any command)
                                  the changeset checkers judge line and diff (in addition)
                                  the most restrictive verdict wins

The changeset review is **additional**: a line with a commit never skips the command checkers.

calu takes the diff before the line runs, so the diff is only the commit's content if nothing
else in the line can change the index or the files first. That is why only a PLAIN line is
reviewed and every other line with a commit is refused: `git add -A && git commit` would be
reviewed on the index as it was before the `git add`, and `cd sub && git commit` or
`git -C other commit` in the wrong repository. A refusal costs Claude one retry (stage first,
then commit), after which the diff is exact.

A PLAIN line is one simple command that starts with the two words `git commit`, with no path
arguments and none of the options that pick the content in the command itself (`-i`, `-o`,
`-p`, `--pathspec-from-file`). `-a` and `--amend` are supported, because the diff they record
can be computed: against HEAD for `-a`, against HEAD's parent for `--amend`. The message may
come from `$(cat <<'EOF' ... EOF)`, which is how Claude Code writes commit messages.

calu runs the git commands itself, in the directory the command starts in: it is the trusted
side, and the agent never sees them. The repository is the agent's to write, so the calls switch
off what a repository's configuration could make git execute (`GIT_OPTIONS`, `DIFF_OPTIONS`).

Not recognised as a commit, and therefore judged by the command checkers alone: a git alias
(`git ci`), a script or a program that commits, and a variable that expands to the command
(`$GIT commit`).
"""

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import anyio

from calu.checkers.shell import command_variants, parse_command

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 60_000
"""Characters of the diff the checker sees; a longer diff is cut, with the stat kept whole."""

GIT_TIMEOUT_S = 30.0
"""Upper bound for one git call; on a timeout the changeset is reported as unavailable."""

GIT_OPTIONS = ("--no-optional-locks", "-c", "core.fsmonitor=false")
"""Options before the git subcommand in calu's own calls. The repository is writable by the
agent, and `core.fsmonitor` in its configuration names a program git would run; without the
optional locks a diff does not rewrite the user's index either."""

DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv")
"""Options of calu's `git diff` calls: no external diff program and no text conversion filter,
both of which a repository's configuration can point at any program."""

GIT_GLOBAL_VALUED = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
"""Options between `git` and the subcommand that take a separate value."""

COMMIT_VALUED_LONG = frozenset(
    {
        "--message",
        "--file",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--author",
        "--date",
        "--template",
        "--cleanup",
        "--trailer",
    }
)
"""Long options of `git commit` whose value is the next word (or follows a `=`)."""

COMMIT_VALUED_SHORT = frozenset("mFCct")
"""Short options of `git commit` whose value is the rest of the word or the next word."""

COMMIT_ATTACHED_SHORT = frozenset("Su")
"""Short options of `git commit` with an optional value that is only ever attached (`-Skey`)."""

COMMIT_SELECTING = frozenset(
    {"--include", "--only", "--patch", "--interactive", "--pathspec-from-file"}
)
"""Long options of `git commit` that select the committed content in the command itself."""

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
"""Programs whose `-c` argument is a command line of its own."""

PRIVILEGE_WRAPPERS = frozenset({"sudo", "doas"})
"""Programs that run the command after their own options; not in `calu.checkers.shell.WRAPPERS`."""

PRIVILEGE_VALUED = frozenset({"-u", "-g", "-C", "-h", "-p"})
"""Options of `sudo` and `doas` that take a separate value."""

MAX_NESTING = 3
"""How many levels of `bash -c '...'` inside each other are looked into for a commit."""

COMMIT_ALONE = (
    "calu reviews the change a commit records before the commit runs, and it can only tell "
    "what that change is when `git commit` is the whole command line. {cause} Run everything "
    "else first as its own command (for example `git add <files>`), then run "
    "`git commit -m ...` alone, in the repository's directory. `-a` and `--amend` are fine; "
    "paths after the commit, `git -C <dir>`, `cd ... &&`, pipes and redirections into a file "
    "are not."
)
"""What Claude is told when a line with a commit is refused; `cause` is one sentence."""


class CommitKind(StrEnum):
    """What a Bash line is, as far as commits are concerned."""

    NONE = "none"
    """No commit was recognised in the line."""
    PLAIN = "plain"
    """The line is one `git commit` whose content calu can compute before it runs."""
    COMPOUND = "compound"
    """The line has a commit and something else that decides what the commit records."""


@dataclass(frozen=True)
class CommitOptions:
    """The options of one `git commit` that decide which diff it records.

    `all_tracked` is `-a`/`--all` (tracked changes that were not staged are committed too),
    `amend` is `--amend` (the commit replaces HEAD). `problem` is "" for a commit whose content
    follows from these two and the index, and otherwise one sentence saying what in the command
    selects the content itself (a path argument, `-i`, `-o`, `-p`, `--pathspec-from-file`, or a
    word the shell expands, which calu cannot read).
    """

    all_tracked: bool = False
    amend: bool = False
    problem: str = ""


@dataclass(frozen=True)
class CommitLine:
    """The result of `classify_commit`.

    `cause` is one sentence for Claude saying why a COMPOUND line is not reviewable ("" for the
    other kinds). `options` is set for a PLAIN line only.
    """

    kind: CommitKind
    cause: str = ""
    options: CommitOptions | None = None

    def refusal(self) -> str:
        """Return the reason Claude receives when this line is refused.

        Returns:
            `COMMIT_ALONE` with this line's cause; meaningful for a COMPOUND line only.
        """
        return COMMIT_ALONE.format(cause=self.cause)


def commit_options(argv: list[str]) -> CommitOptions:
    """Read the options of one `git commit` from its own words.

    Only the words of the commit command are read, and the value of an option that takes one
    (`-m`, `-F`, `--author`, ...) is skipped, so a message that contains "-a" or "--amend" does
    not change the result.

    Args:
        argv: The words after `git commit`, quotes removed, one entry per shell word.

    Returns:
        The options. `problem` is set if a word names a path, selects content (`-i`, `-o`,
        `-p`, `--interactive`, `--pathspec-from-file`) or is expanded by the shell (`$VAR`,
        a backtick) where an option or a path would stand.

    Example:
        commit_options(["-am", "add -a flag"]) == CommitOptions(all_tracked=True)
        commit_options(["-m", "x", "a.py"]).problem != ""
    """
    all_tracked = amend = False
    problem = ""
    index = 0
    while index < len(argv) and not problem:
        word = argv[index]
        index += 1
        if word == "--":
            if index < len(argv):
                problem = "This commit names the paths to commit."
            break
        if "$" in word or "`" in word:
            problem = (
                f"The shell expands `{word}` before git reads it, so calu cannot tell which "
                "options or paths the commit gets."
            )
        elif word.startswith("--"):
            name, separator, _ = word.partition("=")
            if name == "--all":
                all_tracked = True
            elif name == "--amend":
                amend = True
            elif name in COMMIT_SELECTING:
                problem = f"This commit selects its content with `{name}`."
            elif name in COMMIT_VALUED_LONG and not separator:
                index += 1  # the value is the next word
        elif word.startswith("-") and len(word) > 1:
            for position, letter in enumerate(word[1:], start=1):
                if letter in COMMIT_VALUED_SHORT:
                    if position == len(word) - 1:
                        index += 1  # the value is the next word
                    break
                if letter in COMMIT_ATTACHED_SHORT:
                    break
                if letter == "a":
                    all_tracked = True
                elif letter in "iop":
                    problem = f"This commit selects its content with `-{letter}`."
        else:
            problem = f"This commit names a path to commit (`{word}`)."
    return CommitOptions(all_tracked=all_tracked, amend=amend, problem=problem)


def _git_subcommand(words: list[str]) -> str | None:
    """Find the subcommand of a git call, behind git's own options.

    Args:
        words: The words of one command, the first being `git` (with or without a path).

    Returns:
        The subcommand (`commit` for `git -C dir --no-pager commit -m x`), or None if the call
        has none.
    """
    index = 1
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in GIT_GLOBAL_VALUED else 1
    return words[index] if index < len(words) else None


def _commits(words: list[str], cwd: Path, depth: int) -> bool:
    """Tell whether one simple command creates a commit, in any spelling calu knows.

    Looks through what `command_variants` strips (variable assignments, a path before `git`,
    wrappers such as `env`, `nohup`, `timeout 5`, `xargs`), through `sudo`/`doas`, and into the
    command line of `bash -c '...'` and `eval`.

    Args:
        words: The words of the command as parsed.
        cwd: The directory the line starts in (for parsing a nested command line).
        depth: How many command lines this one is nested in.

    Returns:
        True if the command runs `git ... commit`.
    """
    if not words:
        return False
    forms = [list(words)]
    forms += [variant.split(" ") for variant in command_variants(" ".join(words), words)[1:]]
    for form in forms:
        while form and os.path.basename(form[0]) in PRIVILEGE_WRAPPERS:
            form = form[1:]
            while form and form[0].startswith("-"):
                form = form[2:] if form[0] in PRIVILEGE_VALUED else form[1:]
        if not form:
            continue
        name = os.path.basename(form[0])
        if name == "git" and _git_subcommand(form) == "commit":
            return True
        if depth >= MAX_NESTING:
            continue
        nested = ""
        if name in SHELLS:
            for position, word in enumerate(form[1:], start=1):
                if word.startswith("-") and not word.startswith("--") and "c" in word:
                    nested = " ".join(form[position + 1 :])
                    break
        elif name == "eval":
            nested = " ".join(form[1:])
        if nested and _classify(nested, cwd, depth + 1).kind is not CommitKind.NONE:
            return True
    return False


def classify_commit(command: str, cwd: Path) -> CommitLine:
    """Tell whether a Bash line creates a git commit and whether calu can review its content.

    Pure apart from what `parse_command` does (it resolves paths); never raises.

    Args:
        command: The command line as Claude sent it.
        cwd: The directory the command starts in (for the shell parser).

    Returns:
        NONE if no simple command of the line is a commit. PLAIN if the line is exactly one
        `git commit` command whose content follows from the index and its options (see the
        module docstring); `options` is set then. COMPOUND otherwise, with the cause: other
        commands in the line, a wrapper or options before `commit`, path arguments, a
        redirection into a file, or a line the parser cannot split (it is then also searched
        line by line, so a commit inside an unparsable command is still found).

    Example:
        classify_commit("git commit -m x", cwd).kind is CommitKind.PLAIN
        classify_commit("git add -A && git commit -m x", cwd).kind is CommitKind.COMPOUND
        classify_commit("git status", cwd).kind is CommitKind.NONE
    """
    return _classify(command, cwd, 0)


def _classify(command: str, cwd: Path, depth: int) -> CommitLine:
    """Implement `classify_commit` for a line that may be nested in another one.

    Args:
        command: The command line.
        cwd: The directory the command starts in.
        depth: How many command lines this one is nested in (`bash -c`), which bounds the
            recursion at `MAX_NESTING`.

    Returns:
        As `classify_commit`.
    """
    parsed = parse_command(command, cwd)
    argvs = list(parsed.argvs)
    if parsed.error or not argvs:
        argvs += [line.split() for line in command.splitlines()]
    committing = [words for words in argvs if _commits(words, cwd, depth)]
    if not committing:
        return CommitLine(CommitKind.NONE)

    def compound(cause: str) -> CommitLine:
        """Build the COMPOUND result.

        Args:
            cause: One sentence saying what else the line does.

        Returns:
            The result.
        """
        return CommitLine(CommitKind.COMPOUND, cause=cause)

    if parsed.error:
        return compound("calu could not parse this command line.")
    if len(committing) > 1:
        return compound("This line commits more than once.")
    words = committing[0]
    if words[:2] != ["git", "commit"]:
        return compound(
            "This line does not start the commit with the two words `git commit` "
            f"(it runs `{' '.join(words[:4])[:60]} ...`)."
        )
    # `cat` without arguments is the one companion allowed: `-m "$(cat <<'EOF' ... EOF)"`.
    others = [other for other in argvs if other is not words and other != ["cat"]]
    if others:
        return compound(
            f"This line runs other commands besides the commit (`{' '.join(others[0])[:60]}`)."
        )
    written = [
        path.raw
        for path in parsed.paths
        if path.via.startswith("redirect") and path.kind == "write" and path.raw != "/dev/null"
    ]
    if written:
        return compound(f"This line also writes a file (`{written[0]}`).")
    options = commit_options(words[2:])
    if options.problem:
        return compound(options.problem)
    return CommitLine(CommitKind.PLAIN, options=options)


def is_commit(command: str, cwd: Path) -> bool:
    """Tell whether a Bash command line creates a git commit.

    Args:
        command: The command line as Claude sent it.
        cwd: The directory the command starts in (for the shell parser).

    Returns:
        True if `classify_commit` finds a commit in the line, PLAIN or COMPOUND.
    """
    return classify_commit(command, cwd).kind is not CommitKind.NONE


async def changeset(cwd: Path, command: str) -> str:
    """Return the change that a plain commit command would record, as text for the checker.

    Runs `git diff --stat` and `git diff` in `cwd`. Never raises: a missing git, a directory
    that is no repository and a timeout all come back as a line saying so, because a checker
    that cannot see the change should say that rather than fail.

    Which diff:

        git commit            index against HEAD         git diff --cached
        git commit -a         files against HEAD         git diff HEAD
        git commit --amend    index against HEAD~1       git diff --cached HEAD~1
        git commit -a --amend files against HEAD~1       git diff HEAD~1

    Args:
        cwd: The directory the command runs in, which decides the repository.
        command: The commit command line; it must be PLAIN for `classify_commit`.

    Returns:
        The summary and the diff, the diff cut at `MAX_DIFF_CHARS`; "" if there is nothing to
        commit; a "(...)" note if the line is not a plain commit or git could not be asked (no
        repository, no git, a timeout, or an `--amend` in a repository with a single commit).
    """
    line = classify_commit(command, cwd)
    if line.kind is not CommitKind.PLAIN or line.options is None:
        return "(the changeset could not be read: the line is not a plain `git commit`)"
    against = [] if line.options.all_tracked else ["--cached"]
    if line.options.amend:
        # The amended commit replaces HEAD, so the change it records is the one against the
        # previous commit's parent.
        against.append("HEAD~1")
    elif line.options.all_tracked:
        against.append("HEAD")  # `-a` also takes tracked changes that were not staged
    stat = await _git(cwd, ["diff", *DIFF_OPTIONS, "--stat", *against])
    if stat.startswith("("):
        return stat
    patch = await _git(cwd, ["diff", *DIFF_OPTIONS, *against])
    if patch.startswith("("):
        return patch
    if not stat.strip() and not patch.strip():
        return ""
    if len(patch) > MAX_DIFF_CHARS:
        patch = patch[:MAX_DIFF_CHARS] + "\n[... the diff is cut here; the summary above is whole]"
    return f"{stat.rstrip()}\n\n{patch}"


async def _git(cwd: Path, arguments: list[str]) -> str:
    """Run one git command with `GIT_OPTIONS` and return its output; never raises.

    Args:
        cwd: The directory to run in.
        arguments: The git arguments, e.g. `["diff", "--cached"]`.

    Returns:
        The standard output, or a line in parentheses saying why git could not be asked.
    """
    try:
        with anyio.fail_after(GIT_TIMEOUT_S):
            done = await anyio.run_process(["git", *GIT_OPTIONS, *arguments], cwd=cwd, check=False)
    except (OSError, TimeoutError) as exc:
        log.warning("git %s failed: %s", " ".join(arguments), exc)
        return f"(the changeset could not be read: git {arguments[0]} failed)"
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip().splitlines()
        reason = message[0] if message else f"exit code {done.returncode}"
        return f"(the changeset could not be read: {reason})"
    return done.stdout.decode(errors="replace")
