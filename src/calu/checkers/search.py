"""Which files a search tool reads when it is pointed at a directory (PLAN.md D146).

A `Read` of `.env` names the file, so the read deny list sees it. A `Grep` of the project
directory names only the directory, and Claude Code's Grep reads every file below it -- hidden
files included, git-ignored files excluded (measured with CLI 2.1.294, `spikes/RESULTS.md`
D146) -- so a protected file that is not git-ignored was read with no check at all. This module
lists the files such a search reads, the way Claude Code does, so that the rules can apply the
read deny list to each of them.

    Grep(path=D, glob=G)
        D in a git work tree  ->  git ls-files --cached --others --exclude-standard  (under D)
        otherwise             ->  every file below D, hidden ones included
        then                  ->  keep the files that G matches (all of them without G)

The listing errs towards too many files: a file listed that the search would skip only makes
the step ask, while a file missed would be read unchecked.
"""

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LIMIT = 200_000
"""Most files listed for one search; a larger tree is reported as incomplete."""


@dataclass
class SearchedFiles:
    """The files a directory search reads, as far as they could be listed."""

    files: list[Path] = field(default_factory=list)
    complete: bool = True
    """False when the listing stopped at `LIMIT` or the glob cannot be interpreted."""


def searched_files(
    directory: Path, glob: str | None = None, *, git_ignore: bool = True, hidden: bool = True
) -> SearchedFiles:
    """List the files a search of `directory` reads.

    The defaults describe Claude Code's Grep tool. `grep -r` reads git-ignored files too
    (`git_ignore=False`); `rg` and `ag` skip hidden ones unless told otherwise
    (`hidden=False`), D149.

    Side effects: runs `git ls-files` in `directory` (5 s timeout) when `git_ignore` is True,
    and otherwise -- or outside a git work tree, or when git fails -- walks the directory
    without following symbolic links.

    Args:
        directory: An absolute directory. A path that is not a directory lists nothing.
        glob: The search's file glob: a pattern for the base name, or for the path relative to
            `directory` if it contains "/"; one level of braces is expanded (`*.{ts,tsx}`). A
            negated glob ("!...") filters nothing.
        git_ignore: Whether the search leaves out what git ignores.
        hidden: Whether the search reads files whose path has a part starting with ".".

    Returns:
        The files, absolute, and whether the listing is complete.
    """
    if not directory.is_dir():
        return SearchedFiles()
    found = SearchedFiles()
    relative = _git_files(directory) if git_ignore else None
    if relative is None:
        relative = []
        for root, dirs, names in os.walk(directory):
            dirs[:] = [d for d in dirs if d != ".git"]
            base = Path(root).relative_to(directory)
            relative += [str(base / name) if str(base) != "." else name for name in names]
            if len(relative) > LIMIT:
                found.complete = False
                break
    elif len(relative) > LIMIT:
        found.complete = False
    patterns = _expand_braces(glob) if glob and not glob.startswith("!") else []
    for rel in relative[:LIMIT]:
        if patterns and not any(_glob_matches(p, rel) for p in patterns):
            continue
        if not hidden and any(part.startswith(".") for part in Path(rel).parts):
            continue
        found.files.append(directory / rel)
    return found


def _git_files(directory: Path) -> list[str] | None:
    """Return the tracked and the untracked, not ignored files below `directory`.

    Args:
        directory: The directory.

    Returns:
        Paths relative to `directory`, or None if it is not in a git work tree or git fails.
    """
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=directory,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if done.returncode != 0:
        return None
    return [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]


def _expand_braces(glob: str) -> list[str]:
    """Expand one level of `{a,b}` in a glob.

    Args:
        glob: The glob, e.g. "*.{ts,tsx}".

    Returns:
        The alternatives, e.g. ["*.ts", "*.tsx"]; the glob itself if it has no braces.
    """
    match = re.search(r"\{([^{}]*)\}", glob)
    if match is None:
        return [glob]
    head, tail = glob[: match.start()], glob[match.end() :]
    return [head + option + tail for option in match.group(1).split(",")]


def _glob_matches(pattern: str, relative: str) -> bool:
    """Match a file against one Grep glob the way ripgrep does, erring towards a match.

    Args:
        pattern: One glob without braces.
        relative: The file's path relative to the searched directory.

    Returns:
        True if a glob without "/" matches the base name, or any glob matches the relative
        path (`**/` also matching no directory at all).
    """
    if "/" not in pattern and fnmatch.fnmatch(os.path.basename(relative), pattern):
        return True
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
        relative, pattern.replace("**/", "")
    )
