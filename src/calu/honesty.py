"""The honesty register: code that says something other than what it does (PLAN.md §13).

A complex change makes a codebase temporarily dishonest on purpose: a signature changes in one
step and its callers and its documentation follow in the next five. A checker that objected to
every such step would produce noise, and one that denied them would stop the work. So a checker
**records** the spot instead (`CheckDecision.marks_dirty`), clears it when the code and what it
says about itself agree again (`marks_clean`), and calu enforces the record at the one point
where temporary dishonesty becomes a risk: a step that would **run** the dirty code is denied
(D83).

    edit   ──► checker marks src/parser.py:fields dirty
    edit   ──► still dirty (the callers are not updated yet)
    edit   ──► clean: the docstring matches again
    `pytest tests/test_parser.py`  ──► denied while dirty, allowed once clean

Dirty code may always be edited and may not be run. That asymmetry is the point: fixing the
dishonesty is an edit, so a register that blocked edits would deadlock.

This module holds the parts that need no database and no model: what a mark is, which open marks
a step would run, and how the register is put in front of a checker.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from calu.checkers import CodeMark, Step


@dataclass(frozen=True)
class HonestyMark:
    """An open or closed entry of the register, as the database holds it.

    `symbol` is empty when the whole file is meant. `closed_at` is None while the mark is open,
    which is what blocks the code from being run.
    """

    id: int
    path: str
    symbol: str
    reason: str
    opened_at: datetime
    git_branch: str | None = None
    opened_by: str | None = None
    """Name of the checker whose verdict opened it, or None for a row written before the author
    was recorded (PLAN.md D87)."""
    closed_at: datetime | None = None
    closed_by: str | None = None

    @property
    def open(self) -> bool:
        """Whether the mark still stands.

        Returns:
            True while nothing has closed it.
        """
        return self.closed_at is None

    def label(self) -> str:
        """Render the marked spot.

        Returns:
            `path:symbol`, or the path alone when no symbol is named.
        """
        return f"{self.path}:{self.symbol}" if self.symbol else self.path

    def line(self) -> str:
        """Render the mark as one terminal line.

        Returns:
            The spot, the reason and -- for a closed mark -- who closed it.
        """
        state = "" if self.open else f"  (closed by {self.closed_by or 'somebody'})"
        branch = f"  [{self.git_branch}]" if self.git_branch else ""
        return f"{self.label()}{branch}: {self.reason}{state}"

    def mark(self) -> CodeMark:
        """Return this entry as the mark a checker would have written.

        Returns:
            A `CodeMark` with the same spot and reason, for comparing a checker's marks with
            the register.
        """
        return CodeMark(path=self.path, symbol=self.symbol, reason=self.reason)


def command_paths(command: str, cwd: Path, workspace: Path) -> set[str]:
    """Return the project-relative paths a shell command names.

    Each word of the command is read as a path and normalized the way the register stores one:
    relative to the project root, without `./` and without a `..` segment. A word that resolves
    outside the project is dropped, and so is one that names nothing. Two suffixes are cut
    first, because they name a place *inside* a file and not another file: pytest's
    `file.py::test_name` and the `file.py:12` of a grep or an editor.

    Args:
        command: The shell command as Claude sent it.
        cwd: The directory the command runs in; a relative word resolves against it.
        workspace: The project root that the register's paths are relative to.

    Returns:
        The paths, project-relative with forward slashes. Empty when the command names none.
    """
    try:
        words = shlex.split(command)
    except ValueError:  # an unbalanced quote: fall back to whitespace
        words = command.split()
    root = workspace.resolve()
    base = Path(cwd) if cwd else root
    if not base.is_absolute():
        base = root / base
    found: set[str] = set()
    for word in words:
        text = word.split("::", 1)[0]
        text = re.sub(r":\d+(:\d+)?$", "", text)
        if not text or text.startswith("-"):
            continue
        try:
            resolved = (
                (base / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()
            )
            found.add(resolved.relative_to(root).as_posix())
        except ValueError, OSError:
            continue  # outside the project, or not a usable path
    return found


def blocking_marks(step: Step, marks: Sequence[HonestyMark], workspace: Path) -> list[HonestyMark]:
    """Return the open marks that `step` would run.

    Only a command can run code, so only a `Bash` step is matched, and it is matched by path and
    not by text: every word of the command is normalized to a project-relative path
    (`command_paths`), so `./src/parser.py` and `/home/me/project/src/parser.py` match a mark on
    `src/parser.py` while `vendor/src/parser.py` does not. A command that names no marked path
    -- `make test`, `python -m myapp` -- is not matched here; the register is also put in front
    of the checker, whose judgement covers those (§13.4).

    An edit or a read is never matched, however dirty the file is: editing the dishonest code is
    how it gets fixed.

    Args:
        step: The tool call about to run.
        marks: The marks to consider; closed ones are ignored.
        workspace: The project root that the marks' paths are relative to.

    Returns:
        The open marks the command names, in the order given; empty for every tool other than
        `Bash` and for a command that names none of them.
    """
    if step.tool_name != "Bash":
        return []
    command = str(step.tool_input.get("command", ""))
    if not command:
        return []
    named = command_paths(command, Path(step.cwd) if step.cwd else workspace, workspace)
    return [entry for entry in marks if entry.open and entry.path and entry.path in named]


def register_note(marks: Sequence[HonestyMark], limit: int = 20) -> str:
    """Render the open register as a note for a checker (§13.4).

    The note travels in `Step.notes`, where the rule hints already are, so every checker of the
    step sees it and can judge whether the call would reach one of the spots -- which is what
    covers a command that names no path.

    Args:
        marks: The marks to show; closed ones are left out.
        limit: How many are named before the rest are only counted.

    Returns:
        One line naming the spots and their reasons, or "" when nothing is open.
    """
    standing = [entry for entry in marks if entry.open]
    if not standing:
        return ""
    shown = standing[:limit]
    spots = "; ".join(f"{entry.label()} ({entry.reason})" for entry in shown)
    rest = f" and {len(standing) - len(shown)} more" if len(standing) > len(shown) else ""
    return (
        "Code recorded as dishonest, which must not be run until it is fixed: "
        f"{spots}{rest}. A step that would run one of these is a DENY; editing them to make "
        "the code and its documentation agree is what clears the record."
    )
