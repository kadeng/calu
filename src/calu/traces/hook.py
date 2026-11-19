"""The git pre-commit hook of the session traces (agent/records/PLAN.md D246).

`calu traces hook install` writes a pre-commit hook that runs `calu traces finalize --staged`
for commits made outside a calu session (in an IDE or a terminal); `calu traces hook remove`
removes it. calu never installs it by itself.

    where the hook goes                 `git rev-parse --git-path hooks/pre-commit`
                                        (follows `core.hooksPath`)
    no hook there                       calu's hook is written
    calu's hook there                   it is written again (a newer calu path)
    another hook there                  refused; with `--chain` the other hook is renamed to
                                        `pre-commit.calu-chained`, and calu's hook runs it
                                        after the final pass
    remove                              calu's hook is deleted, a chained hook gets its name
                                        back; another hook is refused

A hook manager that owns the hooks directory (pre-commit, husky, lefthook) is configured
instead with a hook that runs `calu traces finalize --staged` (DOCUMENTATION.md, "Session
traces").
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from calu.traces.finalize import FinalizeError, git_ok

HOOK_TAG = "# calu-traces-hook"
"""The line by which calu recognises its own hook."""

CHAINED = "pre-commit.calu-chained"
"""The name another hook gets when calu's hook is installed with `--chain`."""


class HookError(Exception):
    """The hook cannot be installed or removed; the message says why and what to do."""


def hook_path(repo: Path) -> Path:
    """Return where git looks for the pre-commit hook of a repository.

    Args:
        repo: The repository's top-level directory.

    Returns:
        The path (it may not exist).

    Raises:
        HookError: If git cannot tell.
    """
    try:
        out = git_ok(repo, "rev-parse", "--git-path", "hooks/pre-commit")
    except FinalizeError as exc:
        raise HookError(str(exc)) from exc
    path = Path(out.decode().strip())
    return path if path.is_absolute() else repo / path


def hook_script(calu: str) -> str:
    """Return the text of calu's pre-commit hook.

    Args:
        calu: The path of the `calu` command to run; if it is not executable when the hook
            runs, the `calu` found on `PATH` is used.

    Returns:
        A POSIX shell script: it runs `calu traces finalize --staged`, refuses the commit
        when that fails (or when no calu is found), and then runs a chained hook if there
        is one.
    """
    return f"""#!/bin/sh
{HOOK_TAG}: the final redaction of the session traces before a commit.
# Installed by `calu traces hook install`; `calu traces hook remove` removes it.
calu={shlex.quote(calu)}
if [ ! -x "$calu" ]; then
  calu=$(command -v calu) || {{
    echo "calu pre-commit hook: calu was not found, so the session traces cannot be" \\
      "finalized; the commit is refused. Install calu, or remove the hook with" \\
      "\\`calu traces hook remove\\`." >&2
    exit 1
  }}
fi
"$calu" traces finalize --staged || exit 1
chained="$(dirname "$0")/{CHAINED}"
if [ -x "$chained" ]; then
  exec "$chained" "$@"
fi
exit 0
"""


def is_calus(path: Path) -> bool:
    """Tell whether a hook file is calu's.

    Args:
        path: The hook file.

    Returns:
        True if it exists and holds `HOOK_TAG`.
    """
    try:
        return HOOK_TAG in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install(repo: Path, calu: str, chain: bool = False) -> str:
    """Install calu's pre-commit hook in a repository.

    Side effects: creates the hooks directory if needed, writes the hook (mode 0755), and with
    `chain` renames another hook to `CHAINED`.

    Args:
        repo: The repository's top-level directory.
        calu: The path of the `calu` command the hook runs.
        chain: Keep another hook by renaming it and running it after calu's.

    Returns:
        A sentence that says what was done.

    Raises:
        HookError: If another hook is there and `chain` is not set, if a chained hook is
            there already, or if a file cannot be written.
    """
    path = hook_path(repo)
    chained = path.with_name(CHAINED)
    note = ""
    if path.exists() or path.is_symlink():
        if not is_calus(path):
            if not chain:
                raise HookError(
                    f"{path} is a pre-commit hook that calu did not write; nothing was changed. "
                    "`calu traces hook install --chain` keeps it and runs it after calu's, or "
                    "add `calu traces finalize --staged` to that hook or to your hook manager"
                )
            if chained.exists() or chained.is_symlink():
                raise HookError(f"{chained} exists already; nothing was changed")
            try:
                path.rename(chained)
            except OSError as exc:
                raise HookError(f"cannot rename {path}: {exc}") from exc
            note = f"; the hook that was there runs after it, as {chained.name}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hook_script(calu), encoding="utf-8")
        os.chmod(path, 0o755)
    except OSError as exc:
        raise HookError(f"cannot write {path}: {exc}") from exc
    return f"installed the pre-commit hook {path}{note}"


def remove(repo: Path) -> str:
    """Remove calu's pre-commit hook from a repository.

    Side effects: deletes the hook; a chained hook gets the name `pre-commit` back.

    Args:
        repo: The repository's top-level directory.

    Returns:
        A sentence that says what was done ("no ... hook" if there was none).

    Raises:
        HookError: If the hook there is not calu's, or a file cannot be changed.
    """
    path = hook_path(repo)
    if not (path.exists() or path.is_symlink()):
        return f"no pre-commit hook at {path}; nothing was changed"
    if not is_calus(path):
        raise HookError(f"{path} is a pre-commit hook that calu did not write; it was left")
    chained = path.with_name(CHAINED)
    try:
        path.unlink()
        if chained.exists() or chained.is_symlink():
            chained.rename(path)
            return f"removed calu's pre-commit hook; {path.name} is the chained hook again"
    except OSError as exc:
        raise HookError(f"cannot remove {path}: {exc}") from exc
    return f"removed calu's pre-commit hook {path}"
