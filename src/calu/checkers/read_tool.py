"""The one tool a checker agent has: reading a file of the workspace (PLAN.md D22).

A checker sees the step, the condensed conversation and the project context, which is enough
for most decisions. When it is not — the rest of the function being changed, the module's
docstring, the script a command runs — the checker may read one file instead of guessing or
escalating.

The tool is read-only and bounded:

    path          must stay inside the workspace (no "..", no absolute path outside)
    deny lists    a path that the read deny rules match is refused (`Rules.read_denied`)
    size          at most `MAX_CHARS` characters per call, from `start_line` on
    text only     a file that is not UTF-8 text, or that holds NUL bytes, is refused

It is the only tool a checker has: it cannot write, run commands or reach the network.
"""

import logging
from pathlib import Path

from calu.checkers.rules import Rules

log = logging.getLogger(__name__)

MAX_CHARS = 20_000
"""Characters returned at most per call; the rest is cut with a marker."""
MAX_LINES = 400
"""Lines returned at most per call."""


class ReadRefused(Exception):
    """The file may not be read; the message is returned to the checker model."""


def read_file(workspace: Path, rules: Rules | None, path: str, start_line: int = 1) -> str:
    """Return the text of a file of the workspace, for a checker that needs more context.

    Args:
        workspace: The workspace root; `path` is resolved against it.
        rules: The workspace's rules, for the read deny lists; None skips that check.
        path: Workspace-relative path of the file, e.g. "src/app.py".
        start_line: First line to return (1-based).

    Returns:
        At most `MAX_LINES` lines and `MAX_CHARS` characters, each line prefixed with its
        number, and a last line saying how much was left out.

    Raises:
        ReadRefused: If the path leaves the workspace, is read-denied by the rules, does not
            exist, is not a file, or is not UTF-8 text.
    """
    target = _resolve(workspace, path)
    if rules is not None and rules.read_denied(target):
        raise ReadRefused(f"reading {path} is denied by the user's read deny list")
    if not target.is_file():
        raise ReadRefused(f"{path} does not exist or is not a file")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReadRefused(f"{path} cannot be read as text: {type(exc).__name__}") from exc
    if "\x00" in text:
        raise ReadRefused(f"{path} cannot be read as text: it holds NUL bytes")
    lines = text.splitlines()
    first = max(start_line, 1)
    shown = lines[first - 1 : first - 1 + MAX_LINES]
    body = "\n".join(f"{first + i:>5}  {line}" for i, line in enumerate(shown))
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
    left = len(lines) - (first - 1 + len(shown))
    if left > 0:
        body += f"\n[... {left} more lines; call read_file again with start_line]"
    return body or f"{path} is empty"


def _resolve(workspace: Path, path: str) -> Path:
    """Resolve a checker-supplied path inside the workspace.

    Args:
        workspace: The workspace root.
        path: The path the checker asked for.

    Returns:
        The absolute path.

    Raises:
        ReadRefused: If the path is empty or points outside the workspace.
    """
    if not path.strip():
        raise ReadRefused("no path given")
    root = workspace.resolve()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ReadRefused(f"{path} is outside the workspace; a checker may only read its files")
    return target
