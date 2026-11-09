"""The changeset a commit would create, and how to recognise a commit (PLAN.md D26).

A step checker judges one edit with limited context, which is the right altitude for style,
documentation and local design, and the wrong one for "is this change correct and complete?".
That question has an answer only once the change exists. The commit is the moment it does:

    Claude runs `git commit …`
        rules (deny lists)      a force push or a deletion still asks the human first
        route = CHANGESET       instead of the command checker
        calu collects the diff  `git diff --stat` and `git diff` of what would be committed
        the changeset checker   PASS, ALLOW, DENY (fix it first) or ESCALATE (ask the human)

calu runs the git commands itself, in the workspace: it is the trusted side, and the agent never
sees them. `git commit -a` also commits tracked files that were not staged, so the diff is taken
against HEAD in that case and against the index otherwise.
"""

import logging
import re
from pathlib import Path

import anyio

from calu.checkers.shell import parse_command

log = logging.getLogger(__name__)

COMMIT = re.compile(r"^git( -[Cc] \S+)* commit( |$)")
"""A simple command that creates a commit, in the normalized form of `parse_command`."""

ALL_TRACKED = re.compile(r"(^| )(-[a-zA-Z]*a[a-zA-Z]*|--all)( |$)")
"""`-a` / `--all`: the commit also takes tracked changes that were not staged."""

AMEND = re.compile(r"(^| )--amend( |$)")
"""`--amend`: the commit replaces HEAD, so the change is measured against its parent."""

MAX_DIFF_CHARS = 60_000
"""Characters of the diff the checker sees; a longer diff is cut, with the stat kept whole."""

GIT_TIMEOUT_S = 30.0
"""Upper bound for one git call; on a timeout the changeset is reported as unavailable."""


def is_commit(command: str, cwd: Path) -> bool:
    """Tell whether a Bash command line creates a git commit.

    Args:
        command: The command line as Claude sent it.
        cwd: The directory the command starts in (for the shell parser).

    Returns:
        True if any of its simple commands is a `git commit`. A command the parser cannot split
        is matched line by line, so `git commit` inside an unparsable command is still found.
    """
    parsed = parse_command(command, cwd)
    candidates = parsed.commands or [line.strip() for line in command.splitlines()]
    return any(COMMIT.search(candidate) for candidate in candidates)


def commit_options(command: str) -> tuple[bool, bool]:
    """Read the two options that change which diff a commit would make.

    Args:
        command: The command line.

    Returns:
        (whether it commits tracked changes that were not staged, whether it amends HEAD).
    """
    return bool(ALL_TRACKED.search(command)), bool(AMEND.search(command))


async def changeset(workspace: Path, command: str) -> str:
    """Return the change that a commit command would record, as text for the checker.

    Runs `git diff --stat` and `git diff` in the workspace. Never raises: a missing git, a
    directory that is no repository and a timeout all come back as a line saying so, because a
    checker that cannot see the change should say that rather than fail.

    Args:
        workspace: The project directory.
        command: The commit command, for its options (`-a`, `--amend`).

    Returns:
        The summary and the diff, the diff cut at `MAX_DIFF_CHARS`; "" if there is nothing to
        commit; a "(…)" note if git could not be asked (no repository, no git, a timeout, or an
        `--amend` in a repository with a single commit).
    """
    all_tracked, amend = commit_options(command)
    if amend:
        # The amended commit replaces HEAD, so the change it records is the one against the
        # previous commit's parent.
        against = ["HEAD~1"]
    elif all_tracked:
        against = ["HEAD"]  # `-a` also takes tracked changes that were not staged
    else:
        against = ["--cached"]  # a plain commit records the index
    stat = await _git(workspace, ["diff", "--stat", *against])
    if stat.startswith("("):
        return stat
    patch = await _git(workspace, ["diff", *against])
    if not stat.strip() and not patch.strip():
        return ""
    if len(patch) > MAX_DIFF_CHARS:
        patch = patch[:MAX_DIFF_CHARS] + "\n[... the diff is cut here; the summary above is whole]"
    return f"{stat.rstrip()}\n\n{patch}"


async def _git(workspace: Path, arguments: list[str]) -> str:
    """Run one git command in the workspace and return its output; never raises.

    Args:
        workspace: The directory to run in.
        arguments: The git arguments, e.g. `["diff", "--cached"]`.

    Returns:
        The standard output, or a line in parentheses saying why git could not be asked.
    """
    try:
        with anyio.fail_after(GIT_TIMEOUT_S):
            done = await anyio.run_process(["git", *arguments], cwd=workspace, check=False)
    except (OSError, TimeoutError) as exc:
        log.warning("git %s failed: %s", " ".join(arguments), exc)
        return f"(the changeset could not be read: git {' '.join(arguments)} failed)"
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip().splitlines()
        reason = message[0] if message else f"exit code {done.returncode}"
        return f"(the changeset could not be read: {reason})"
    return done.stdout.decode(errors="replace")
