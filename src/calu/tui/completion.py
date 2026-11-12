"""Completion of a project path after `@` in the composer.

The composer offers the files of the session's project directory when a word starts with `@`.
This works only where that directory exists on the machine the client runs on: the protocol
has no request that lists files. This module imports nothing from Textual.
"""

import os
from pathlib import Path

SKIPPED = frozenset({".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache"})
"""Directory names that are never offered."""


def at_word(text: str, column: int) -> tuple[int, str] | None:
    """Find the `@` word the cursor is at the end of.

    Args:
        text: One line of the composer.
        column: The cursor's column in that line.

    Returns:
        `(start, partial)`: the column of the `@` and what follows it up to the cursor; None
        if the cursor is not at the end of a word that starts with `@` at the start of the
        line or after a space.
    """
    start = text.rfind("@", 0, column)
    if start < 0 or (start > 0 and not text[start - 1].isspace()):
        return None
    partial = text[start + 1 : column]
    if any(char.isspace() for char in partial):
        return None
    return start, partial


def complete_path(workspace: str, partial: str, limit: int = 50) -> list[str]:
    """List the project paths that continue what was typed after `@`.

    Args:
        workspace: The session's project directory.
        partial: What was typed, relative to the project directory, e.g. `src/ca`.
        limit: The most paths returned.

    Returns:
        The matching paths relative to the project directory, directories with a trailing
        `/` and first, each group in alphabetical order. Names that start with a dot are
        offered only when the typed name starts with one. An empty list if the project
        directory is not on this machine, or if the typed path leaves it.
    """
    root = Path(workspace) if workspace else None
    if root is None or not root.is_dir():
        return []
    directory, _, prefix = partial.rpartition("/")
    base = (root / directory).resolve() if directory else root.resolve()
    try:
        base.relative_to(root.resolve())
        entries = list(os.scandir(base))
    except OSError, ValueError:
        return []
    found: list[tuple[bool, str]] = []
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix) or name in SKIPPED:
            continue
        if name.startswith(".") and not prefix.startswith("."):
            continue
        is_dir = entry.is_dir()
        path = f"{directory}/{name}" if directory else name
        found.append((not is_dir, path + ("/" if is_dir else "")))
    return [path for _, path in sorted(found)][:limit]


def common_prefix(paths: list[str]) -> str:
    """Return what all offered paths begin with.

    Args:
        paths: The paths of `complete_path`.

    Returns:
        The longest common beginning; "" for an empty list.
    """
    if not paths:
        return ""
    first, last = min(paths), max(paths)
    length = next(
        (index for index, (a, b) in enumerate(zip(first, last, strict=False)) if a != b),
        min(len(first), len(last)),
    )
    return first[:length]
