"""The user's own edits in the IDE, recorded as diffs in the project (agent/records/PLAN.md D253).

    editor save ── human_diffs.add {path, before, after} ──► Session.human_diff
                                                               │ enabled? path inside, not
                                                               │ calu's own output? tracked
                                                               │ by git? text? changed?
                                                               ▼
                       record(): unified diff ──append──► <directory>/<date>/<user>/<slug>.tmp.diff
                       cleanup(): *.tmp.diff older than keep_days ──► deleted

The editor computes nothing but the two texts; this module builds the diff and writes the
file, so that every editor writes the same format. A file holds one block per save:

    # src/app.py: saved 2026-11-20 14:03:12 UTC in vscode, calu session 4f1c...
    --- a/src/app.py
    +++ b/src/app.py
    @@ -1,3 +1,3 @@
    ...

`git apply` and `patch` skip the comment lines. The diffs are written as they are, without
redaction (the owner's decision): they stay on this machine, and the `.gitignore` that calu
writes into a directory it creates keeps the `*.tmp.diff` files out of git. A user who wants
to keep a file renames it to `<name>.diff`; `cleanup` deletes only `*.tmp.diff` files.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from calu import asuser

log = logging.getLogger(__name__)

SUFFIX = ".tmp.diff"
"""The end of the name of every file calu writes; only such files are ever deleted."""

GITIGNORE = (
    "# Written by calu (human_diffs): the temporary diffs stay out of git.\n"
    "*.tmp.diff\n.gitignore\n"
)
"""The `.gitignore` calu writes into the directory when it creates it."""

MAX_CHARS = 1_000_000
"""The longest `before` or `after` text a request may carry."""

CONTEXT_LINES = 3
"""Unchanged lines around each change in a block."""

GIT_TIMEOUT_S = 10.0
"""How long the check whether git tracks a file may take."""

CLEANUP_INTERVAL_S = 24 * 3600
"""Seconds between two cleanups of a running session (at the first save after them)."""


class HumanDiffError(Exception):
    """A diff could not be written; the message says why."""


def unified(path: str, before: str, after: str) -> str:
    r"""Return the unified diff of one save, with `a/` and `b/` prefixes.

    Args:
        path: The file, relative to the project, with "/" separators.
        before: The text before the save.
        after: The saved text.

    Returns:
        The diff with `CONTEXT_LINES` lines of context, ending in a newline; "" if the texts
        are equal. A last line without a newline is marked as `diff` marks it
        (`\ No newline at end of file`).
    """
    if before == after:
        return ""
    lines: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        f"a/{path}",
        f"b/{path}",
        n=CONTEXT_LINES,
    ):
        if line.endswith("\n"):
            lines.append(line)
        else:
            lines.append(line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def slug(path: str) -> str:
    """Return the file name stem a recorded file's diffs are stored under.

    Args:
        path: The file, relative to the project, with "/" separators.

    Returns:
        The path with "/" written as "__" and every character outside `[A-Za-z0-9._-]`
        replaced by "_", without leading dots, at most 200 characters; "file" if nothing is
        left. Example: `src/calu/session.py` -> `src__calu__session.py`.
    """
    text = re.sub(r"[^A-Za-z0-9._-]", "_", path.replace("/", "__")).lstrip(".")
    return text[:200] or "file"


def user_component(user: str) -> str:
    """Return a user name as one safe path component.

    Args:
        user: The OS user's login name, or its uid as text.

    Returns:
        The name with every character outside `[A-Za-z0-9._-]` replaced by "_", without
        leading dots, at most 64 characters; "unknown" if nothing is left.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", user).lstrip(".")[:64] or "unknown"


def is_tracked(file: Path) -> bool:
    """Tell whether git tracks a file in its checkout.

    Runs `git ls-files --error-unmatch` in the file's directory as the user calu works for
    (`asuser.child`), so a file of a worktree or of a nested repository is asked in its own
    checkout.

    Args:
        file: The absolute path of the file.

    Returns:
        True if git lists the file; False if it does not, if the directory is in no
        repository, or if git is missing, fails or takes longer than `GIT_TIMEOUT_S`.
    """
    try:
        done = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", file.name],
            cwd=file.parent,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            **asuser.child({**os.environ, "GIT_TERMINAL_PROMPT": "0"}),
        )
    except OSError, subprocess.SubprocessError:
        return False
    return done.returncode == 0


def _refuse_outside(root: Path, directory: Path) -> None:
    """Refuse a directory that lies outside the project once its links are resolved.

    Args:
        root: The resolved project directory.
        directory: An existing directory.

    Raises:
        HumanDiffError: If it lies outside `root`.
    """
    real = directory.resolve()
    if real != root and root not in real.parents:
        raise HumanDiffError(f"{directory} leads out of the project directory ({real})")


def ensure_directory(workspace: Path, directory: str) -> Path:
    """Create the diff directory if needed, with its `.gitignore`.

    Side effects: when the directory does not exist, it is created with its parents and gets
    the file `.gitignore` (`GITIGNORE`). An existing directory is left as it is, also without
    a `.gitignore`. Nothing is created when the deepest existing directory lies outside the
    project.

    Args:
        workspace: The project directory.
        directory: `human_diffs.directory`, relative to the project.

    Returns:
        The absolute path of the directory.

    Raises:
        HumanDiffError: If the directory, with its links resolved, lies outside the project,
            or cannot be created.
    """
    root = workspace.resolve()
    target = workspace / directory
    try:
        existing = target
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        _refuse_outside(root, existing)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                target / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
            )
            try:
                os.write(fd, GITIGNORE.encode())
            finally:
                os.close(fd)
        _refuse_outside(root, target)
    except OSError as exc:
        raise HumanDiffError(f"cannot create {target}: {exc}") from exc
    return target


@dataclass(frozen=True)
class Saved:
    """One save of the user, as the session hands it to `record`."""

    path: str
    """The file, relative to the project, with "/" separators."""
    before: str
    """The text before the save."""
    after: str
    """The saved text."""
    when: datetime
    """When the save was reported, in UTC."""
    user: str
    """The OS user the session works for."""
    session_id: str | None
    """The calu session's id, or None before the session has a row."""
    client: str
    """The editor's client name, e.g. "vscode"."""


def record(workspace: Path, directory: str, saved: Saved) -> Path | None:
    """Append the diff of one save to its file.

    The file is `<directory>/<YYYY-MM-DD>/<user>/<slug(path)>.tmp.diff`. The block is one
    comment line and the unified diff, written with one `write` call to a file opened with
    `O_APPEND` and `O_NOFOLLOW`, so concurrent sessions do not interleave their blocks. A
    short write is reported as an error; the part that was written stays in the file.

    Side effects: creates the directory as `ensure_directory` does, the date and user
    folders, and the file (mode 0644, less the umask).

    Args:
        workspace: The project directory.
        directory: `human_diffs.directory`, relative to the project.
        saved: The save.

    Returns:
        The written file; None if the texts are equal (nothing is written).

    Raises:
        HumanDiffError: If the directory leads out of the project or the file cannot be
            written.
    """
    diff = unified(saved.path, saved.before, saved.after)
    if not diff:
        return None
    base = ensure_directory(workspace, directory)
    folder = base / saved.when.strftime("%Y-%m-%d") / user_component(saved.user)
    target = folder / f"{slug(saved.path)}{SUFFIX}"
    session = saved.session_id or "none"
    head = (
        f"# {saved.path}: saved {saved.when.strftime('%Y-%m-%d %H:%M:%S')} UTC in "
        f"{saved.client or 'unknown'}, calu session {session}\n"
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _refuse_outside(workspace.resolve(), folder)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
        try:
            block = (head + diff + "\n").encode("utf-8", "surrogatepass")
            if os.write(fd, block) != len(block):
                raise HumanDiffError(f"cannot write {target}: short write")
        finally:
            os.close(fd)
    except OSError as exc:
        raise HumanDiffError(f"cannot write {target}: {exc}") from exc
    return target


def cleanup(workspace: Path, directory: str, keep_days: int, now: float | None = None) -> list[str]:
    """Delete the `*.tmp.diff` files whose last change is older than `keep_days`.

    Walks only the configured directory and never follows a link: a link is neither entered
    nor deleted, also one named `*.tmp.diff`. Only regular files whose name ends in
    `.tmp.diff` are deleted; a file renamed to `<name>.diff` stays. Empty folders below the
    directory are removed, including those that were empty before; the directory itself stays.

    Side effects: deletes files and empty folders; logs each removed file. Errors on single
    entries are logged and skipped.

    Args:
        workspace: The project directory.
        directory: `human_diffs.directory`, relative to the project.
        keep_days: `human_diffs.keep_days`; 0 deletes nothing.
        now: The current time as a Unix timestamp; None takes the clock's.

    Returns:
        The removed files, relative to the project directory, in walk order.
    """
    if keep_days <= 0:
        return []
    base = workspace / directory
    try:
        info = os.lstat(base)
    except OSError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        return []  # a link or a file in place of the directory is not walked
    try:
        _refuse_outside(workspace.resolve(), base)
    except HumanDiffError, OSError:
        return []
    limit = (time.time() if now is None else now) - keep_days * 86400
    removed: list[str] = []
    # With follow_symlinks=False, fwalk lists a link to a directory but does not enter it.
    for top, _dirs, files, top_fd in os.fwalk(base, follow_symlinks=False):
        for name in files:
            if not name.endswith(SUFFIX):
                continue
            try:
                entry = os.stat(name, dir_fd=top_fd, follow_symlinks=False)
                if not stat.S_ISREG(entry.st_mode) or entry.st_mtime >= limit:
                    continue
                os.unlink(name, dir_fd=top_fd)
            except OSError as exc:
                log.warning("human diffs: %s/%s was not deleted: %s", top, name, exc)
                continue
            shown = (Path(top) / name).relative_to(workspace).as_posix()
            removed.append(shown)
            log.info("human diffs: deleted %s (older than %d days)", shown, keep_days)
    for top, _dirs, _files in os.walk(base, topdown=False, followlinks=False):
        if Path(top) == base:
            continue
        try:
            os.rmdir(top)  # only an empty folder is removed
        except OSError:
            pass
    return removed
