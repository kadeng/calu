"""The checkouts of a session's repository: its own working tree and the git worktrees (D251).

Claude Code works in git worktrees (`EnterWorktree`, a subagent with `isolation: "worktree"`,
or `git worktree add` through Bash). Each worktree is a separate working tree of the same
repository, with its own `HEAD`, index and files. calu keeps what depends on a working tree
(the honesty state, the write-set snapshots) per checkout:

    checkout        a working tree of the session's repository: the main checkout (the
                    session directory) or a worktree that `git worktree list` names, inside
                    the project (`.claude/worktrees/<name>`) or outside it (`../x`, `/tmp/x`)
    Checkouts       the registry of one session (`of`, `refresh`, `others`)
    step_checkouts  the checkouts one tool call belongs to: the one of its working directory
                    and, for a shell command, the one of each `cd`/`pushd` target

A directory of another repository is no checkout: `of` returns None for it, and calu treats a
step there as before, as a step outside the project.

    step.cwd ──┐
    cd targets ┴─> Checkouts.of() ─> {checkout, ...} ─> honesty state, snapshots, checker note

This module reads git and the file system only; it imports nothing of calu's checkers, so the
rules engine, the honesty state and the session can all use it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from calu import asuser

GIT_TIMEOUT_S = 10.0
"""Upper bound for `git worktree list`. A call that takes longer leaves the registry as it was."""

WORKTREES_DIR = (".claude", "worktrees")
"""Where Claude Code creates its worktrees, relative to the project directory."""

OTHERS_SHOWN = 10
"""How many other checkouts the line for a checker names before it counts the rest."""


@dataclass(frozen=True)
class Checkout:
    """One working tree of the session's repository."""

    root: Path
    """The top directory, resolved."""
    label: str
    """The root as calu shows it: relative to the session directory ("." for the main
    checkout, `.claude/worktrees/wta` for a nested worktree), or absolute outside it."""
    branch: str = ""
    """The branch checked out there, without `refs/heads/`; "" when `HEAD` is detached or the
    branch is not known."""
    main: bool = False
    """True for the session's own checkout."""
    outside: bool = False
    """True for a worktree that lies outside the session directory."""

    def describe(self) -> str:
        """Name the checkout for a checker or a person, in one line.

        Returns:
            The label as a code span and its type, e.g. "`.` (main checkout)",
            "`.claude/worktrees/wta` (worktree, branch `worktree-wta`)" or
            "`/tmp/x` (worktree outside the project directory, detached HEAD)".
        """
        label = _plain(self.label)
        if self.main:
            return f"`{label}` (main checkout)"
        where = "worktree outside the project directory" if self.outside else "worktree"
        head = f"branch `{_plain(self.branch)}`" if self.branch else "detached HEAD"
        return f"`{label}` ({where}, {head})"


def _plain(text: str, limit: int = 300) -> str:
    """Render a path or a branch name, which the agent chose, as the inside of a code span.

    Args:
        text: The name.
        limit: The largest number of characters shown.

    Returns:
        `text` with every character that is not printable written as a backslash escape and
        every backtick as an apostrophe, so that it stays on one line inside its span; cut
        after `limit` characters with "..." appended.
    """
    safe = "".join(
        ("'" if char == "`" else char)
        if char.isprintable()
        else char.encode("unicode_escape").decode("ascii")
        for char in text[:limit]
    )
    return safe + ("..." if len(text) > limit else "")


def parse_worktrees(output: bytes) -> list[tuple[Path, str]]:
    """Read the output of `git worktree list --porcelain -z`.

    Args:
        output: The NUL-separated records: `worktree <path>`, `HEAD <id>`, `branch <ref>` or
            `detached`, further attributes, and an empty field after each worktree.

    Returns:
        (path, branch without `refs/heads/`, "" if detached) per worktree, in git's order (the
        repository's main worktree first). A record without a `worktree` field is skipped.
    """
    found: list[tuple[Path, str]] = []
    path: Path | None = None
    branch = ""
    for field in [*output.split(b"\0"), b""]:
        text = os.fsdecode(field)
        if not text:
            if path is not None:
                found.append((path, branch))
            path, branch = None, ""
        elif text.startswith("worktree "):
            path = Path(text.removeprefix("worktree "))
        elif text.startswith("branch "):
            branch = text.removeprefix("branch ").removeprefix("refs/heads/")
    return found


def _resolved(path: Path) -> Path:
    """Resolve a path as far as it exists.

    Args:
        path: An absolute path.

    Returns:
        `path.resolve()`, or the normalised path when it cannot be resolved.
    """
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.normpath(path))


def _within(path: Path, root: Path) -> bool:
    """Tell whether a path is a directory or lies below it.

    Args:
        path: A resolved path.
        root: A resolved directory.

    Returns:
        True if `path` is `root` or lies below it.
    """
    return path == root or root in path.parents


class Checkouts:
    """The checkouts of one session (see the module docstring).

    The registry starts with the main checkout only; `refresh` reads the worktrees from git.
    The session calls `refresh` at its start, after `EnterWorktree`, `ExitWorktree` and `Agent`
    results, and after a shell command that contains `git worktree`; `of` refreshes by itself
    when it gets a path below `.claude/worktrees/<name>/` that no known worktree covers.
    Reading is safe from several threads: `refresh` replaces the list in one assignment.
    """

    def __init__(self, workspace: Path) -> None:
        """Create the registry with the main checkout only.

        Args:
            workspace: The session directory.
        """
        self.workspace = _resolved(workspace)
        self.main = Checkout(root=self.workspace, label=".", main=True)
        self._others: tuple[Checkout, ...] = ()
        self._top: Path | None = None
        """The top directory of the main checkout's repository, from the last refresh."""

    @property
    def others(self) -> tuple[Checkout, ...]:
        """The worktrees other than the main checkout, from the last refresh.

        Returns:
            Them, sorted by label.
        """
        return self._others

    def all(self) -> tuple[Checkout, ...]:
        """List every known checkout.

        Returns:
            The main checkout first, then `others`.
        """
        return (self.main, *self._others)

    def nested(self) -> tuple[str, ...]:
        """Name the worktrees that lie inside the session directory.

        Returns:
            Their labels (relative to the session directory). The main checkout's honesty state
            and snapshots leave these directories out: each is a checkout of its own.
        """
        return tuple(checkout.label for checkout in self._others if not checkout.outside)

    def label(self, root: Path) -> str:
        """Write a directory as calu shows it.

        Args:
            root: A resolved directory.

        Returns:
            Its path relative to the session directory ("." for the directory itself), or the
            absolute path outside it.
        """
        try:
            relative = root.relative_to(self.workspace).as_posix()
        except ValueError:
            return root.as_posix()
        return relative or "."

    def refresh(self) -> bool:
        """Read the worktrees from `git worktree list --porcelain -z`.

        The checkout whose root is the session directory or the nearest parent of it is the
        main checkout's repository root; every other worktree git names is one of `others`.
        A worktree whose directory is gone (`prunable`) is kept while git lists it.

        Side effects: replaces `others`. When git cannot be asked (no repository, no git, a
        timeout), the registry keeps what it had.

        Returns:
            True if the list of other checkouts changed.
        """
        try:
            done = subprocess.run(
                ["git", "worktree", "list", "--porcelain", "-z"],
                cwd=self.workspace,
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
                **asuser.child({**os.environ, "GIT_TERMINAL_PROMPT": "0"}),
            )
        except OSError, subprocess.TimeoutExpired:
            return False
        if done.returncode != 0:
            return False
        listed = [(_resolved(path), branch) for path, branch in parse_worktrees(done.stdout)]
        own = [root for root, _branch in listed if _within(self.workspace, root)]
        top = max(own, key=lambda root: len(root.parts), default=None)
        others = tuple(
            sorted(
                (
                    Checkout(
                        root=root,
                        label=self.label(root),
                        branch=branch,
                        outside=not _within(root, self.workspace),
                    )
                    for root, branch in listed
                    if root != top
                ),
                key=lambda checkout: checkout.label,
            )
        )
        self._top = top
        changed = others != self._others
        self._others = others
        return changed

    def of(self, path: str | Path) -> Checkout | None:
        """Find the checkout a file or directory belongs to.

        Args:
            path: Absolute, or relative to the session directory.

        Returns:
            The worktree whose root is the nearest parent of `path` (a worktree nested in the
            main checkout wins over it); else the main checkout if `path` lies in the session
            directory or in the main checkout's repository; else None (another repository, or
            no repository at all). A path below `.claude/worktrees/<name>/` that no known
            worktree covers makes the registry refresh once first (`refresh`).
        """
        target = _resolved(self.workspace / path)
        found = self._find(target)
        if found is self.main and self._unknown_worktree(target) and self.refresh():
            found = self._find(target)
        return found

    def _find(self, target: Path) -> Checkout | None:
        """Look a resolved path up in the registry as it is.

        Args:
            target: A resolved path.

        Returns:
            As `of`, without refreshing.
        """
        inside = [checkout for checkout in self._others if _within(target, checkout.root)]
        if inside:
            return max(inside, key=lambda checkout: len(checkout.root.parts))
        if _within(target, self.workspace) or (
            self._top is not None and _within(target, self._top)
        ):
            return self.main
        return None

    def _unknown_worktree(self, target: Path) -> bool:
        """Tell whether a path lies below a directory where Claude Code creates worktrees.

        Args:
            target: A resolved path that the registry assigned to the main checkout.

        Returns:
            True if it is `<session>/.claude/worktrees/<name>` or below it.
        """
        try:
            parts = target.relative_to(self.workspace).parts
        except ValueError:
            return False
        return len(parts) >= 3 and parts[:2] == WORKTREES_DIR

    def step_checkouts(
        self, cwd: str | Path | None, directories: Sequence[Path] = ()
    ) -> list[Checkout]:
        """Find the checkouts one tool call belongs to.

        Args:
            cwd: The call's working directory (`Step.cwd`); None or "" is the session
                directory.
            directories: For a shell command, the directories it may run in
                (`calu.checkers.shell.ParsedCommand.directories`); empty otherwise.

        Returns:
            The checkout of `cwd` first, then the checkout of each directory in order, each
            once. A directory outside every checkout adds nothing; a `cwd` outside every
            checkout gives the main checkout, which is how calu treated such a step before
            (outside the project).

        Example:
            `cd .claude/worktrees/b && python x.py` from the session directory belongs to the
            main checkout and to worktree `b`.
        """
        first = self.of(cwd or self.workspace) or self.main
        found = [first]
        for directory in directories:
            checkout = self.of(directory)
            if checkout is not None and checkout not in found:
                found.append(checkout)
        return found

    def header(self, own: Sequence[Checkout]) -> tuple[str, str]:
        """Render the two lines a checker reads about a step's checkouts.

        Args:
            own: The checkouts the step belongs to (`step_checkouts`: the checkout of its
                working directory first, then those its `cd` targets lie in); not empty.

        Returns:
            (the step's checkouts, the other checkouts) as `Checkout.describe` names them, each
            line a comma-separated list; the second names at most `OTHERS_SHOWN` and then
            "and N more". ("", "") when the registry knows no worktree: a project without
            worktrees gets no such lines.

        Example:
            `cd .claude/worktrees/b && pytest` from the session directory: the first line names
            "`.` (main checkout), `.claude/worktrees/b` (worktree, branch ...)", so a checker
            does not read the test run in `b` as code of the main checkout loading `b`'s code.
        """
        if not self._others:
            return "", ""
        mine = list(dict.fromkeys(own))
        rest = [checkout for checkout in self.all() if checkout not in mine]
        shown = ", ".join(checkout.describe() for checkout in rest[:OTHERS_SHOWN])
        if len(rest) > OTHERS_SHOWN:
            shown += f", and {len(rest) - OTHERS_SHOWN} more"
        return ", ".join(checkout.describe() for checkout in mine), shown
