"""The changeset a commit would create, and how to recognise a commit (PLAN.md D26).

A step checker judges one edit with limited context, which is the right altitude for style,
documentation and local design, and the wrong one for "is this change correct and complete?".
That question has an answer only once the change exists. The commit is the moment it does:

    Claude runs a shell line (Bash, Monitor) that contains `git commit`
        rules (deny lists)        a force push or a deletion still asks the human first
        classify_commit(line)
            COMPOUND              DENY, deterministically: "run `git commit` as its own command"
            PLAIN                 calu collects the diff: `git diff --stat` and `git diff`
                                  not readable: ESCALATE without a model call
                                                (DENY "stage first" where that makes it readable)
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
come from `$(cat <<'EOF' ... EOF)`, which is how Claude Code writes commit messages; a `cat`
without arguments is therefore the one other command a PLAIN line may hold.

calu reads the options as git does. git accepts a long option shortened to any prefix that
names one option only, so `commit_options` resolves every long option against the list of
`git commit`'s options (`COMMIT_LONG`) by the same rule. A word it cannot resolve to exactly one
known option, an option it does not know, and a short letter it does not know make the line
COMPOUND: the classification never rests on a guess.

calu runs the git commands itself, in the directory the command starts in: it is the trusted
side, and the agent never sees them. The repository is the agent's to write, so the diff is
taken **without the repository's configuration**:

    git rev-parse ...                  where the repository's files are, and what HEAD is
    a temporary directory of calu's    `config` written by calu, an empty `refs/`
    git diff, with GIT_COMMON_DIR = that directory, GIT_DIR and GIT_OBJECT_DIRECTORY = the
              repository's, and no user or system configuration

git reads `config` from the common directory, so no setting of the repository reaches the
diff: no program it names is run (file system monitor, content filters, external diff, text
conversion, a fetch of missing objects), and no setting can leave a file out of the diff or
change how it is shown. `DIFF_OPTIONS` states the same on the command line, and `--text` shows
the content of a file that an attribute declares binary.

`-a` makes git read the files of the working tree while it commits. calu's diff reads them the
same way, with the settings that decide how a file's content becomes a blob copied from the
repository as plain values (`MIRRORED_CORE`). A content filter is a program, so calu does not
run it: if a changed file has a `filter` attribute, the changeset of an `-a` commit counts as
unreadable, and Claude is told to stage the change first (the staged content is already what
the commit records).

A changeset that calu could not read completely is never left to a model: `Changeset.problem`
makes the session escalate the commit (or refuse it, where staging first solves it), and
`Changeset.cut` turns a checker's approval into an escalation.

The changeset review applies to a commit calu can read off the line. Every command line, with
or without a commit, is judged by the command checkers.
"""

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import anyio
import anyio.to_thread

from calu import asuser
from calu.checkers.shell import command_variants, parse_command

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 240_000
"""Characters of the diff the checker sees; a longer diff is cut, with the stat kept whole."""

GIT_TIMEOUT_S = 30.0
"""Upper bound for one git call; on a timeout the changeset is reported as unavailable."""

GIT_OPTIONS = ("--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false")
"""Options before the git subcommand in calu's own calls: no pager, no program that watches the
file system, and no rewrite of the user's index (without the optional locks a diff does not
refresh it)."""

DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--text",
    "--no-renames",
    "--no-relative",
    "--submodule=short",
    "--ignore-submodules=dirty",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
"""Options of calu's `git diff` calls. No external diff program and no text conversion; `--text`
shows the content of every file, whatever an attribute says about it; one entry per path; the
whole repository, also from a subdirectory; a submodule as its recorded commit, which is what a
commit records of it, without asking the submodule."""

GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
"""Environment of calu's diff calls, next to the directories of `_diff_environment`: no user or
system configuration, and no fetch of an object that is missing."""

MIRRORED_CORE = (
    "filemode",
    "symlinks",
    "ignorecase",
    "autocrlf",
    "eol",
    "safecrlf",
    "precomposeunicode",
)
"""`core.*` settings that decide how git turns a working file into the content it commits. For
an `-a` commit calu copies their values into its own configuration, and only values that are a
single word of letters (`true`, `input`, `crlf`)."""

HEAD_ID = re.compile(r"[0-9a-f]{40,64}")
"""A commit id as `git rev-parse` prints it (SHA-1 or SHA-256)."""

STAGE_FIRST = (
    "Stage the change first (`git add -u`, or `git add <files>`), then run `git commit` "
    "without `-a`: the staged content is what the commit records, and calu can read it."
)
"""What Claude is told when the changeset of an `-a` commit cannot be read but the staged one
could (`Changeset.stage_first`)."""

GIT_GLOBAL_VALUED = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
"""Options between `git` and the subcommand that take a separate value."""

COMMIT_LONG: Mapping[str, str] = {
    "quiet": "flag",
    "verbose": "flag",
    "file": "value",
    "author": "value",
    "date": "value",
    "message": "value",
    "reedit-message": "value",
    "reuse-message": "value",
    "fixup": "value",
    "squash": "value",
    "reset-author": "flag",
    "trailer": "value",
    "signoff": "flag",
    "template": "value",
    "edit": "flag",
    "cleanup": "value",
    "status": "flag",
    "gpg-sign": "optional",
    "all": "all",
    "include": "select",
    "interactive": "select",
    "patch": "select",
    "unified": "select",
    "inter-hunk-context": "select",
    "only": "select",
    "verify": "flag",
    "dry-run": "flag",
    "short": "flag",
    "branch": "flag",
    "ahead-behind": "flag",
    "porcelain": "flag",
    "long": "flag",
    "null": "flag",
    "amend": "amend",
    "post-rewrite": "flag",
    "untracked-files": "optional",
    "pathspec-from-file": "select",
    "pathspec-file-nul": "select",
    "allow-empty": "flag",
    "allow-empty-message": "flag",
}
"""Every long option of `git commit` (git 2.43 with its hidden options, and the two that later
versions added for `--patch`), without the leading `--`, and what it means for the changeset:

    flag      takes no value and does not change which content is committed
    optional  as flag; a value is only ever attached with `=`
    value     takes a value: the next word, or what follows a `=`
    all       `--all`: tracked changes that were not staged are committed too
    amend     `--amend`: the commit replaces HEAD
    select    picks the committed content in the command itself, or belongs to an option
              that does

git also accepts each of them with `no-` in front (`--no-verify` is the option `verify` negated,
`--no-edit` the option `edit`), and any prefix that names one spelling only. A test compares
this list with what the installed git prints for `git commit -h`."""

COMMIT_SHORT_FLAGS = frozenset("qvsenz")
"""Short options of `git commit` that take no value and do not change the committed content."""

COMMIT_VALUED_SHORT = frozenset("mFCct")
"""Short options of `git commit` whose value is the rest of the word or the next word."""

COMMIT_ATTACHED_SHORT = frozenset("Su")
"""Short options of `git commit` with an optional value that is only ever attached (`-Skey`)."""

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
    "`git commit -m ...` alone, in the repository's directory. `-a` and `--amend` are fine, "
    "with every option written out in full; paths after the commit, `git -C <dir>`, "
    "`cd ... &&`, a pipe into another command and a redirection into a file are not."
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
    selects the content itself (a path argument, `-i`, `-o`, `-p`, `--pathspec-from-file`), or
    which word calu cannot read with certainty (a word the shell expands, an option that is
    unknown or shortened to a prefix of several options).
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


def resolve_long_option(name: str) -> tuple[str, bool] | None:
    """Tell which long option of `git commit` a word names, by git's own rule.

    git takes a word that is an option's full name as that option, and otherwise accepts a
    prefix that fits one spelling only, where the spellings are every option of `COMMIT_LONG`
    and each of them with `no-` in front. This function applies the rule to that whole set, so
    its candidates include every candidate git has for a known option.

    Args:
        name: The word without the leading `--` and without a `=value` part.

    Returns:
        `(option, negated)` with `option` a key of `COMMIT_LONG`, or None if the word names no
        known option or fits more than one spelling.

    Example:
        resolve_long_option("amend") == ("amend", False)
        resolve_long_option("no-verify") == ("verify", True)
        resolve_long_option("pathspec-from-f") == ("pathspec-from-file", False)
        resolve_long_option("a") is None  # `--all`, `--amend`, `--author`, ...
    """
    spellings = {
        spelled: (option, negated)
        for option in COMMIT_LONG
        for spelled, negated in ((option, False), (f"no-{option}", True))
    }
    if name in spellings:
        return spellings[name]
    if not name:
        return None
    fitting = {found for spelled, found in spellings.items() if spelled.startswith(name)}
    return fitting.pop() if len(fitting) == 1 else None


def commit_options(argv: list[str]) -> CommitOptions:
    """Read the options of one `git commit` from its own words, as git reads them.

    Only the words of the commit command are read, and the value of an option that takes one
    (`-m`, `-F`, `--author`, ...) is skipped, so a message that contains "-a" or "--amend" does
    not change the result. Long options are resolved with `resolve_long_option`, so a shortened
    option counts as the option git takes it for.

    Args:
        argv: The words after `git commit`, quotes removed, one entry per shell word.

    Returns:
        The options. `problem` is set if a word names a path, selects content (`-i`, `-o`,
        `-p`, `--interactive`, `--pathspec-from-file`), is expanded by the shell (`$VAR`, a
        backtick) where an option or a path would stand, or is an option that calu cannot
        resolve to exactly one option it knows -- also a known option in a form whose meaning
        calu does not know (`--no-all`, `--amend=x`).

    Example:
        commit_options(["-am", "add -a flag"]) == CommitOptions(all_tracked=True)
        commit_options(["--ame"]) == CommitOptions(amend=True)
        commit_options(["-m", "x", "a.py"]).problem != ""
        commit_options(["--a"]).problem != ""
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
            spelled, separator, _ = word.partition("=")
            resolved = resolve_long_option(spelled[2:])
            if resolved is None:
                problem = (
                    f"calu cannot tell which option of `git commit` `{spelled}` is. Write "
                    "every option out in full."
                )
                continue
            option, negated = resolved
            kind = COMMIT_LONG[option]
            if kind == "select":
                problem = f"This commit selects its content with `--{option}`."
            elif kind == "optional" or (kind == "flag" and not separator):
                pass
            elif negated or (separator and kind != "value"):
                problem = (
                    f"calu does not know what `{word[:60]}` means for the content of the "
                    f"commit. Write `--{option}` in its usual form, or leave it out."
                )
            elif kind == "all":
                all_tracked = True
            elif kind == "amend":
                amend = True
            elif not separator:
                index += 1  # a "value" option: the value is the next word
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
                elif letter not in COMMIT_SHORT_FLAGS:
                    problem = f"calu does not know the option `-{letter}` of `git commit`."
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


HEREDOC = re.compile(r"<<(-?)\s*(\\?['\"]?)([A-Za-z_][A-Za-z0-9_]*)")
"""A here-document operator: the dash of `<<-`, how its delimiter is quoted, the delimiter."""

UNCERTAIN = (
    "calu cannot tell from this line what the shell runs while it builds the command "
    "(a backtick, `$'...'` or a line continuation). Write the commit with a plain quoted message."
)
"""The cause given when a commit line holds a construct `_shell_runs_more` does not resolve."""


def _shell_runs_more(command: str) -> bool:
    """Tell whether a line holds shell syntax whose effect calu does not read off the words.

    `parse_command` resolves `$(...)`. This looks for what it does not resolve: a backtick the
    shell would expand, ANSI-C quoting (`$'...'`), and a backslash at the end of a line, which
    joins words. Text the shell takes literally is skipped: single-quoted strings, the body
    of a here-document whose delimiter is quoted, and a character behind a backslash.

    Args:
        command: The command line as Claude sent it.

    Returns:
        True if such a construct is present, or if the quoting is not closed (then calu cannot
        say what is literal).

    Example:
        _shell_runs_more("git commit -m 'a `b` c'") is False
        _shell_runs_more('git commit -m "a `b` c"') is True
    """
    if "\\\n" in command:
        return True
    kept: list[str] = []
    pending: list[tuple[str, bool, bool]] = []  # (delimiter, quoted, strip tabs)
    for line in command.split("\n"):
        if pending:
            delimiter, quoted, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delimiter:
                pending.pop(0)
            elif not quoted and "`" in line:
                return True  # an expanded here-document body: quotes do not protect here
            continue
        kept.append(line)
        pending += [(match[3], bool(match[2]), bool(match[1])) for match in HEREDOC.finditer(line)]
    text = "\n".join(kept)
    index, double = 0, False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if double:
            if char == "`":
                return True
            double = char != '"'
        elif char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                return True
            index = end
        elif char == '"':
            double = True
        elif char == "`" or text.startswith("$'", index):
            return True
        index += 1
    return double


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
        redirection into a file, shell syntax calu does not resolve (`_shell_runs_more`), or
        a line the parser cannot split (it is then also searched
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
    uncertain = _shell_runs_more(command)
    # Bash joins a line that ends in a backslash with the next one before it reads words.
    parsed = parse_command(command.replace("\\\n", ""), cwd)
    argvs = list(parsed.argvs)
    if parsed.error or not argvs:
        argvs += [line.split() for line in command.splitlines()]
    committing = [words for words in argvs if _commits(words, cwd, depth)]
    if not committing:
        # A git call whose subcommand calu cannot read as a word may be a commit.
        unread = any(
            words
            and Path(words[0]).name == "git"
            and (_git_subcommand(words) or "").startswith(("$", "`"))
            for words in argvs
        )
        if unread and uncertain:
            return CommitLine(CommitKind.COMPOUND, cause=UNCERTAIN)
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
    if uncertain:
        return compound(UNCERTAIN)
    if len(committing) > 1:
        return compound("This line commits more than once.")
    words = committing[0]
    if words[:2] != ["git", "commit"]:
        return compound(
            "This line does not start the commit with the two words `git commit` "
            f"(it runs `{' '.join(words[:4])[:60]} ...`)."
        )
    # `cat` without arguments is the one companion allowed, wherever it stands: it is how
    # `-m "$(cat <<'EOF' ... EOF)"` reads the message, and it changes neither index nor files.
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


@dataclass(frozen=True)
class Changeset:
    """What calu read of the change a plain commit would record.

    Exactly one of three holds: `problem` is set and `text` is "" (the change could not be
    read); `text` is "" and `problem` is "" (the commit would record nothing); `text` holds
    `git diff --stat` and the diff. `cut` says that the diff in `text` is not whole. Only a
    result without `problem` and without `cut` shows a checker the complete change.
    """

    text: str = ""
    problem: str = ""
    """Why the change could not be read, as one sentence; "" if it was read."""
    cut: bool = False
    """True if the diff was longer than `MAX_DIFF_CHARS` and `text` holds its start only."""
    stage_first: bool = False
    """True if `problem` only concerns an `-a` commit: the same change, staged and committed
    without `-a`, can be read (`STAGE_FIRST`)."""

    def shown(self) -> str:
        """Return the result as text for a human or a checker.

        Returns:
            `text`, or one line in parentheses saying that the change could not be read and
            why, or that nothing would be committed.
        """
        if self.problem:
            return f"(the changeset could not be read: {self.problem})"
        return self.text or "(nothing would be committed: the diff is empty)"


class _UnreadableError(Exception):
    """calu could not read the change of a commit; the message says why, as one sentence."""

    def __init__(self, problem: str, stage_first: bool = False) -> None:
        """Keep the reason.

        Args:
            problem: Why the change could not be read.
            stage_first: Whether the staged change could be read instead.
        """
        super().__init__(problem)
        self.stage_first = stage_first


async def changeset(cwd: Path, command: str) -> Changeset:
    """Read the change that a plain commit command would record.

    Runs `git rev-parse`, then `git diff --stat` and `git diff` with a configuration of calu's
    own (module docstring), in the repository of `cwd`. Never raises: a missing git, a directory
    that is no repository, a timeout and a line that is no plain commit come back as a result
    with `problem` set, which the caller must not hand to a model as if it were the change.

    Which diff:

        git commit            index against HEAD         git diff --cached <HEAD>
        git commit -a         files against HEAD         git diff <HEAD>
        git commit --amend    index against HEAD~1       git diff --cached <HEAD>~1
        git commit -a --amend files against HEAD~1       git diff <HEAD>~1

    Side effects: creates a temporary directory and removes it again; reads the files of the
    working tree for `-a`. Nothing in the repository is written.

    Args:
        cwd: The directory the command runs in, which decides the repository.
        command: The commit command line; it must be PLAIN for `classify_commit`.

    Returns:
        The result (see `Changeset`): the summary and the diff, the diff cut at
        `MAX_DIFF_CHARS` with `cut` set; an empty result if there is nothing to commit; a
        `problem` if the line is not a plain commit, git could not be asked (no repository, no
        git, a timeout, an `--amend` without a commit before HEAD), or an `-a` commit changes a
        file whose content a filter would convert (`stage_first` is set then).
    """
    line = classify_commit(command, cwd)
    if line.kind is not CommitKind.PLAIN or line.options is None:
        return Changeset(problem="the line is not a plain `git commit`")
    scratch: str | None = None
    try:
        scratch = await anyio.to_thread.run_sync(lambda: tempfile.mkdtemp(prefix="calu-diff-"))
        return await _read_changeset(cwd, line.options, Path(scratch))
    except _UnreadableError as exc:
        return Changeset(problem=str(exc), stage_first=exc.stage_first)
    except OSError as exc:
        log.warning("reading a changeset failed: %s", exc)
        return Changeset(problem=f"{type(exc).__name__}: {exc}")
    finally:
        if scratch is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: shutil.rmtree(scratch, ignore_errors=True))


async def _read_changeset(cwd: Path, options: CommitOptions, scratch: Path) -> Changeset:
    """Implement `changeset` for a plain commit, with a temporary directory to work in.

    Args:
        cwd: The directory the command runs in.
        options: The commit's options.
        scratch: An empty directory of calu's own; the caller removes it.

    Returns:
        The change, or an empty result if there is nothing to commit.

    Raises:
        _UnreadableError: If git could not be asked or the change cannot be read whole.
        OSError: If the temporary configuration could not be written.
    """
    where = (
        await _git(
            cwd,
            [
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
                "--git-path",
                "objects",
                "--show-toplevel",
                "--show-object-format",
            ],
        )
    ).splitlines()
    if len(where) != 5:
        raise _UnreadableError("git did not name the repository's directories")
    git_dir, common_dir, objects, top, object_format = where
    head = (await _git(cwd, ["rev-parse", "--verify", "--quiet", "HEAD"], accept=(0, 1))).strip()
    if head and not HEAD_ID.fullmatch(head):
        raise _UnreadableError("git did not name the commit of HEAD")
    if not head and (options.amend or options.all_tracked):
        raise _UnreadableError(
            "the branch has no commit yet, and the commit uses `-a` or `--amend`",
            stage_first=not options.amend,
        )
    core = {"repositoryformatversion": "0", "bare": "false"}
    if options.all_tracked:
        core.update(await _mirrored_core(cwd))
    extensions = ""
    if object_format != "sha1":
        if not object_format.isalnum():
            raise _UnreadableError("git did not name the repository's object format")
        core["repositoryformatversion"] = "1"
        extensions = f"[extensions]\n\tobjectformat = {object_format}\n"
    config = "[core]\n" + "".join(f"\t{key} = {value}\n" for key, value in core.items())
    await anyio.to_thread.run_sync(_write_scratch, scratch, config + extensions, Path(common_dir))
    environment = {
        **os.environ,
        **GIT_ENVIRONMENT,
        "GIT_DIR": git_dir,
        "GIT_COMMON_DIR": str(scratch),
        "GIT_OBJECT_DIRECTORY": objects,
        "GIT_WORK_TREE": top,
    }
    against = [] if options.all_tracked else ["--cached"]
    if head:
        # The amended commit replaces HEAD, so the change it records is the one against the
        # previous commit's parent. HEAD is passed as an id: calu's directory holds no refs.
        against.append(f"{head}~1" if options.amend else head)
    diff = ["diff", *DIFF_OPTIONS]
    top_dir = Path(top)
    if options.all_tracked:
        await _refuse_filtered(top_dir, [*diff, "--name-only", "-z", *against], environment)
    stat = await _git(top_dir, [*diff, "--stat", *against], environment)
    patch = await _git(top_dir, [*diff, *against], environment)
    if not stat.strip() and not patch.strip():
        return Changeset()
    cut = len(patch) > MAX_DIFF_CHARS
    if cut:
        patch = patch[:MAX_DIFF_CHARS] + "\n[... the diff is cut here; the summary above is whole]"
    return Changeset(text=f"{stat.rstrip()}\n\n{patch}", cut=cut)


def _write_scratch(scratch: Path, config: str, common_dir: Path) -> None:
    """Fill calu's temporary directory so that git accepts it as a common directory.

    Writes `config` and an empty `refs/`, and copies the repository's `info/attributes` if it
    has one: attributes are data (which files are text, which have a filter), and calu's diff
    should see the files as the commit will.

    Args:
        scratch: The empty temporary directory.
        config: The text of the configuration file.
        common_dir: The repository's own common directory.

    Raises:
        OSError: If a file could not be written.
    """
    (scratch / "refs").mkdir()
    (scratch / "config").write_text(config, encoding="utf-8")
    try:
        attributes = (common_dir / "info" / "attributes").read_bytes()
    except OSError:
        return
    (scratch / "info").mkdir()
    (scratch / "info" / "attributes").write_bytes(attributes)


async def _mirrored_core(cwd: Path) -> dict[str, str]:
    """Read the repository's values of the `MIRRORED_CORE` settings.

    Args:
        cwd: A directory inside the repository.

    Returns:
        Setting name (lower case, without `core.`) -> value, for each setting that is set to a
        single word of letters; where a setting is set more than once, the last value, as git
        takes it.

    Raises:
        _UnreadableError: If git could not be asked.
    """
    pattern = rf"^core\.({'|'.join(MIRRORED_CORE)})$"
    listed = await _git(cwd, ["config", "-z", "--get-regexp", pattern], accept=(0, 1))
    values: dict[str, str] = {}
    for entry in listed.split("\0"):
        key, _, value = entry.partition("\n")
        name = key.lower().removeprefix("core.")
        if name in MIRRORED_CORE and value.isascii() and value.isalpha():
            values[name] = value
    return values


async def _refuse_filtered(top: Path, names: list[str], environment: Mapping[str, str]) -> None:
    """Refuse to read an `-a` commit that changes a file with a `filter` attribute.

    git would pass such a file's content through the filter program while it commits. calu
    does not run a program the repository names, so it cannot show what would be committed.

    Args:
        top: The top directory of the working tree.
        names: The `git diff --name-only -z` call that lists the changed paths.
        environment: The environment of calu's diff calls.

    Raises:
        _UnreadableError: If a changed path has a `filter` attribute, or git could not be asked.
    """
    changed = await _git_raw(top, names, environment)
    if not changed:
        return
    answer = await _git_raw(
        top, ["check-attr", "-z", "--stdin", "filter"], environment, stdin=changed
    )
    fields = answer.decode(errors="replace").split("\0")
    for path, value in zip(fields[0::3], fields[2::3], strict=False):
        if value not in ("unspecified", "unset"):
            raise _UnreadableError(
                f"`-a` would commit a file whose content a filter converts ({path[:200]!r}, "
                f"filter {value[:60]!r}), and calu does not run that program",
                stage_first=True,
            )


async def _git(
    cwd: Path,
    arguments: list[str],
    environment: Mapping[str, str] | None = None,
    *,
    accept: tuple[int, ...] = (0,),
) -> str:
    """Run one git command with `GIT_OPTIONS` and return its output as text.

    Args:
        cwd: The directory to run in.
        arguments: The git arguments, e.g. `["diff", "--cached"]`.
        environment: The environment of the call; None for calu's own.
        accept: The exit codes that are no error.

    Returns:
        The standard output; bytes that are no UTF-8 are replaced.

    Raises:
        _UnreadableError: As `_git_raw`.
    """
    output = await _git_raw(cwd, arguments, environment, accept=accept)
    return output.decode(errors="replace")


async def _git_raw(
    cwd: Path,
    arguments: list[str],
    environment: Mapping[str, str] | None = None,
    *,
    stdin: bytes = b"",
    accept: tuple[int, ...] = (0,),
) -> bytes:
    """Run one git command with `GIT_OPTIONS` and return its output as bytes.

    Args:
        cwd: The directory to run in.
        arguments: The git arguments, e.g. `["diff", "--cached"]`.
        environment: The environment of the call; None for calu's own, with fetching of
            missing objects and prompting switched off.
        stdin: Bytes for the command's standard input.
        accept: The exit codes that are no error.

    Returns:
        The standard output.

    Raises:
        _UnreadableError: If git is missing, does not answer within `GIT_TIMEOUT_S` or exits
            with a code that is not in `accept`. The message is one line.
    """
    if environment is None:
        # A call that reads the repository's own configuration (where its directories are,
        # a few `core.*` values): it must not fetch or prompt either.
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        with anyio.fail_after(GIT_TIMEOUT_S):
            done = await anyio.run_process(
                ["git", *GIT_OPTIONS, *arguments],
                cwd=cwd,
                check=False,
                input=stdin or None,
                **asuser.child(environment),
            )
    except (OSError, TimeoutError) as exc:
        log.warning("git %s failed: %s", " ".join(arguments), exc)
        raise _UnreadableError(f"git {arguments[0]} failed ({type(exc).__name__})") from exc
    if done.returncode not in accept:
        message = done.stderr.decode(errors="replace").strip().splitlines()
        reason = message[0][:300] if message else f"exit code {done.returncode}"
        raise _UnreadableError(f"git {arguments[0]}: {reason}")
    return done.stdout
