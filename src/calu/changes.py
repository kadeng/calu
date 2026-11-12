"""What a command actually wrote, measured with git snapshots (PLAN.md D31).

`Edit`, `Write` and `NotebookEdit` reach the tool gate with their exact content, so calu knows
what they change before they run. A Bash command does not: `sed -i`, a formatter, a code
generator or a script can write anything, and the command checker only *describes* what it
expects to happen. This module closes that gap for the cases where a project wants it.

    before = await snapshots.take()      # right before the command runs
    ... the command runs ...
    after = await snapshots.take()
    change = await snapshots.changes(before, after)   # paths, sizes, a bounded patch
    await snapshots.revert(before, after)             # if a checker refuses the result

The snapshots are git trees in a git directory of **calu's own**, outside the project
(`$XDG_STATE_HOME/calu/snapshots/<session>`), used with `--work-tree <project>`:

- the user's own repository gets no unreachable objects, and its index and branches are untouched;
- it works for a project directory that is not a git repository at all;
- the pre-state and the revert source are out of the agent's reach (the directory belongs on the
  sandbox's `denyRead` list);
- in-tree `.gitignore` files are still honoured, so `node_modules` is not hashed, while
  `always_capture` force-captures the few paths that matter and `.gitignore` would hide
  (`.env` and the like).

Costs, measured on 2026-11-09: 107 ms for the first snapshot of a 236-file tree, 2 ms for later
ones, 2.5 MB of objects. Hashing is the price, so `max_file_bytes` keeps a big or binary file out
of the content (it is still reported by path and size) and `max_patch_bytes` bounds what a
checker ever sees.

What this does not do: a content-identical rewrite is invisible (intended), ownership and
extended attributes are not covered, and a window is only sound while one command runs — with
overlapping background commands or subagents the write set is reported as "overlapping", since
it then holds the writes of more than one step. Attribution without content, for every step and
without this machinery, is D32 (`calu.clock`, `calu.checkers.tools.changed_files`).
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import anyio.to_thread

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 120.0
"""Upper bound for one git call. A first snapshot of a large tree hashes every file, so this is
much larger than the 30 s of `calu.checkers.changeset`."""

BINARY_PROBE = 8_000
"""Bytes of a file that are read to tell whether its content is binary (git's own heuristic: a
NUL byte within the first 8000 bytes)."""

DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text")
"""Options of the `git diff` that makes the patch for a checker. `--text` shows the content of a
file that a `.gitattributes` in the project declares binary (`payload.py -diff`): which files
are binary is decided by `_measure`, not by a file the agent can write. `--no-renames` keeps one
entry per path, as `diff-tree` lists them."""

STATUS_NAMES = {"A": "added", "M": "changed", "D": "deleted", "R": "renamed", "T": "type changed"}
"""Letter of `git diff-tree --name-status` -> word for a human and for a checker prompt."""


def default_root() -> Path:
    """Return the directory calu keeps its snapshot repositories in.

    It is `$XDG_STATE_HOME/calu/snapshots` (or `~/.local/state/calu/snapshots`): calu's own
    state, outside every project, and on the sandbox's read deny list, so that a command cannot
    read the pre-state of the project or the source of a revert.

    Returns:
        The directory; it is not created here (`SnapshotStore.prepare` does that).
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "calu" / "snapshots"


class SnapshotError(Exception):
    """A git call of the snapshot store failed; the caller continues without a snapshot."""


@dataclass(frozen=True)
class Change:
    """One path that a command wrote.

    `status` is the word from `STATUS_NAMES`. `size` is the file's size after the command (0 for
    a deleted file). `binary` is True if git treats the content as binary, in which case no
    patch for this path is shown.
    """

    status: str
    path: str
    size: int = 0
    binary: bool = False

    def line(self) -> str:
        """Describe the change in one line.

        Returns:
            Status, path and, for a large or binary file, its size.
        """
        parts = [f"{self.size} bytes"] if self.size else []
        if self.binary:
            parts.append("binary")
        detail = f" ({', '.join(parts)})" if parts else ""
        return f"{self.status}: {self.path}{detail}"


@dataclass(frozen=True)
class ChangeSet:
    """Everything a command wrote, between two snapshots.

    `patch` is the unified diff, cut to `max_patch_bytes` and without binary and oversized
    files; `cut` says whether something was left out of it. `overlapping` is True if another
    tool call ran inside the same window, so the paths cannot be attributed to one command.
    """

    changes: tuple[Change, ...] = ()
    patch: str = ""
    cut: bool = False
    overlapping: bool = False

    @property
    def paths(self) -> tuple[str, ...]:
        """The paths that were written.

        Returns:
            One path per change, in the order git reported them.
        """
        return tuple(change.path for change in self.changes)

    def summary(self) -> str:
        """Describe the write set in one line, for an event or a log.

        Returns:
            The number of paths and the first few of them, or "no files changed".
        """
        if not self.changes:
            return "no files changed"
        shown = ", ".join(change.line() for change in self.changes[:5])
        more = f" and {len(self.changes) - 5} more" if len(self.changes) > 5 else ""
        overlap = (
            " (overlapping calls: not attributable to one command)" if self.overlapping else ""
        )
        return f"{len(self.changes)} path(s): {shown}{more}{overlap}"

    def undeclared(self, declaration: str) -> tuple[str, ...]:
        """Return the written paths that a text does not mention (the honesty check of D31).

        The command checker is asked to name the files a command edits. Comparing that prose
        with the write set is a blunt instrument — a summary may name a directory, or a path in
        another spelling — so this is a signal to show a human and a checker, never a decision
        on its own.

        Args:
            declaration: What the checker said the step does (its step summary).

        Returns:
            The paths that do not occur in `declaration`, neither whole nor by file name.
        """
        text = declaration.lower()
        missing: list[str] = []
        for change in self.changes:
            path = change.path.lower()
            if path in text or Path(path).name in text:
                continue
            missing.append(change.path)
        return tuple(missing)


@dataclass
class SnapshotStore:
    """Takes git snapshots of a project directory, outside that directory (D31).

    `prepare` must run once before `take`. Every method reports a failure by raising
    `SnapshotError`; a caller treats that as "no snapshot" and goes on, because a missing write
    set must not stop a session.
    """

    workspace: Path
    root: Path
    """calu's own git directory for this session; created by `prepare`."""
    max_file_bytes: int = 1_000_000
    max_patch_bytes: int = 200_000
    always_capture: Sequence[str] = ()
    """Paths to capture even though a `.gitignore` covers them, e.g. `.env`."""
    _ready: bool = field(default=False, init=False)

    async def prepare(self) -> None:
        """Create the git directory and its configuration.

        Side effects: creates `root` (with its parents) and initializes a bare repository in it.
        Safe to call twice.

        Raises:
            SnapshotError: If the directory cannot be created or git is not available.
        """
        try:
            await anyio.to_thread.run_sync(lambda: self.root.mkdir(parents=True, exist_ok=True))
        except OSError as exc:
            raise SnapshotError(f"cannot create {self.root}: {exc}") from exc
        if not (self.root / "HEAD").exists():
            await self._git(["init", "--bare", "--quiet", str(self.root)], in_root=False)
        self._ready = True

    async def take(self) -> str:
        """Snapshot the project directory and return the id of the resulting git tree.

        Side effects: writes git objects into `root` for everything that changed since the last
        snapshot. In-tree `.gitignore` files are honoured; `always_capture` is added regardless.

        Returns:
            The tree id (a hexadecimal object name).

        Raises:
            SnapshotError: If `prepare` has not run or a git call failed.
        """
        if not self._ready:
            raise SnapshotError("prepare() has not run")
        await self._git(["add", "-A", "--", "."])
        for path in self.always_capture:
            # A path a `.gitignore` covers is only captured when it is named explicitly; an
            # absent path is not an error, so failures here are logged and ignored.
            try:
                await self._git(["add", "-f", "--", path])
            except SnapshotError as exc:
                log.debug("not capturing %s: %s", path, exc)
        return (await self._git(["write-tree"])).strip()

    async def changes(self, before: str, after: str) -> ChangeSet:
        """Return what changed between two snapshots.

        Args:
            before: Tree id from `take` before the command ran.
            after: Tree id from `take` after it ran.

        Returns:
            The write set: one `Change` per path, and a patch cut to `max_patch_bytes` that
            leaves out binary files and files above `max_file_bytes`. Unless `cut` is True, the
            patch holds the diff of every other path: file names are passed to git as literal
            paths, and a `.gitattributes` entry cannot hide content (`DIFF_OPTIONS`).

        Raises:
            SnapshotError: If a git call failed, or if git left a path out of the patch that it
                was asked for. The caller must then treat the write set as not measured.
        """
        if before == after:
            return ChangeSet()
        listed = await self._git(["diff-tree", "-r", "--name-status", "-z", before, after])
        changes = await self._parse(listed)
        if not changes:
            return ChangeSet()
        small = [
            change.path
            for change in changes
            if not change.binary and change.size <= self.max_file_bytes
        ]
        patch, cut = "", len(small) != len(changes)
        if small:
            patch = await self._git(["diff", *DIFF_OPTIONS, before, after, "--", *small])
            # The patch is what a checker judges, so it must hold every path it was asked for.
            # Asking git for the names with the same arguments shows what it really selected.
            names = await self._git(
                ["diff", *DIFF_OPTIONS, "--name-only", "-z", before, after, "--", *small]
            )
            missing = sorted(set(small) - {name for name in names.split("\0") if name})
            if missing:
                raise SnapshotError(
                    f"the patch leaves out {len(missing)} changed path(s): "
                    + ", ".join(missing[:5])
                )
            if len(patch) > self.max_patch_bytes:
                patch, cut = patch[: self.max_patch_bytes] + "\n[... the diff was cut]\n", True
        return ChangeSet(changes=tuple(changes), patch=patch, cut=cut)

    async def revert(self, before: str, after: str, paths: Sequence[str] = ()) -> bool:
        """Undo the change between two snapshots in the working tree.

        Nothing is lost: both trees stay in `root`, so the reverted state can be read back with
        `git show <after>:<path>` if the revert turns out to be wrong.

        Side effect: changes files in the project directory.

        Args:
            before: Tree id of the state to go back to.
            after: Tree id of the current state.
            paths: Which paths to revert; empty reverts all of them. They are literal file
                names: git's pathspec magic (`:!name`, `:(glob)...`) is switched off.

        Returns:
            True if the revert applied. False means the working tree was left untouched, which
            happens when it has moved on since the snapshot (git refuses a patch that does not
            apply cleanly).

        Raises:
            SnapshotError: If git could not be run at all.
        """
        arguments = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--binary"]
        arguments += [before, after]
        if paths:
            arguments += ["--", *paths]
        patch = await self._git(arguments)
        if not patch.strip():
            return True
        try:
            await self._git(["apply", "--reverse", "--whitespace=nowarn", "-"], stdin=patch)
        except SnapshotError as exc:
            log.warning("the revert did not apply: %s", exc)
            return False
        return True

    async def _parse(self, listed: str) -> list[Change]:
        """Turn the output of `diff-tree --name-status -z` into changes with sizes.

        Args:
            listed: The NUL-separated output: a status field, then its path, and for a rename or
                a copy the old path and then the new one.

        Returns:
            One `Change` per path, with the size of the file as it is now and whether its
            content is binary.
        """
        fields = [part for part in listed.split("\0") if part]
        pairs: list[tuple[str, str]] = []
        index = 0
        while index < len(fields):
            status = fields[index][:1]
            index += 1
            step = 2 if status in ("R", "C") else 1  # a rename names the old path first
            if index + step - 1 >= len(fields):
                break
            pairs.append((status, fields[index + step - 1]))
            index += step
        measured = await anyio.to_thread.run_sync(lambda: _measure(self.workspace, pairs))
        return [
            Change(status=STATUS_NAMES.get(status, status), path=path, size=size, binary=binary)
            for (status, path), (size, binary) in zip(pairs, measured, strict=True)
        ]

    async def _git(self, arguments: list[str], *, in_root: bool = True, stdin: str = "") -> str:
        """Run one git command against the snapshot repository.

        Git reads neither the user's nor the system's configuration, and every path argument
        is a literal file name (`GIT_LITERAL_PATHSPECS`).

        Args:
            arguments: The git arguments, e.g. `["write-tree"]`.
            in_root: Whether to point git at the snapshot directory and the work tree (False for
                `init`, which takes the path itself).
            stdin: Text to write to the command's standard input.

        Returns:
            The standard output.

        Raises:
            SnapshotError: If git is missing, timed out or exited with an error.
        """
        command = ["git"]
        if in_root:
            command += [
                f"--git-dir={self.root}",
                f"--work-tree={self.workspace}",
                "-c",
                "core.bare=false",
                "-c",
                "gc.auto=0",
                "-c",
                "core.untrackedCache=true",
            ]
        command += arguments
        try:
            with anyio.fail_after(GIT_TIMEOUT_S):
                done = await anyio.run_process(
                    command,
                    cwd=self.workspace,
                    check=False,
                    input=stdin.encode() if stdin else None,
                    env={
                        **os.environ,
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_SYSTEM": "/dev/null",
                        # File names come from the project, which the agent writes: a file
                        # called `:!other.py` must name itself and not "everything but
                        # other.py" (pathspec magic also applies after `--`).
                        "GIT_LITERAL_PATHSPECS": "1",
                    },
                )
        except (OSError, TimeoutError) as exc:
            raise SnapshotError(f"git {arguments[0]} failed: {exc}") from exc
        if done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip().splitlines()
            raise SnapshotError(
                message[0] if message else f"git {arguments[0]} exited {done.returncode}"
            )
        return done.stdout.decode(errors="replace")


def _measure(workspace: Path, pairs: Sequence[tuple[str, str]]) -> list[tuple[int, bool]]:
    """Read the size of each changed file and whether its content is binary.

    The binary test is git's own: a NUL byte within the first `BINARY_PROBE` bytes. Reading only
    that much keeps the test cheap for the gigabyte case, which is the one that matters.

    Runs synchronously in a worker thread, because it stats and reads files.

    Args:
        workspace: The project directory.
        pairs: The (status, path) pairs from `diff-tree`.

    Returns:
        One (size, binary) pair per entry, in the same order. A path that cannot be read (a
        deleted file, for example) is reported as (0, False).
    """
    measured: list[tuple[int, bool]] = []
    for _status, path in pairs:
        absolute = workspace / path
        try:
            if not absolute.is_file():
                measured.append((0, False))
                continue
            size = absolute.stat().st_size
            with absolute.open("rb") as handle:
                binary = b"\0" in handle.read(BINARY_PROBE)
        except OSError:
            measured.append((0, False))
            continue
        measured.append((size, binary))
    return measured
