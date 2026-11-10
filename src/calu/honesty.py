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
from dataclasses import dataclass, field
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
    files: dict[str, str] = field(default_factory=dict)
    """The file this mark was opened on, as it was then, so that an evaluation case built from
    the mark can be reproduced (D95). Empty for a mark on a step that is not an edit."""
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

    def stale(self, workspace: Path | None = None, branch: str | None = None) -> str:
        """Say why this mark looks stale, if it does (PLAN.md D91).

        An open mark blocks every command that would run its code, so a mark that cannot be
        right any more is pure obstruction. Two cases are recognisable without a model:

        - the **file is gone**: nothing can run it, so the mark only blocks commands that
          mention the path, and the checker that would clear it will never see that file again;
        - the mark was made on **another branch**: the file there may be a different file, which
          is the half of Q19 that does not need the owner's decision -- it is reported, not
          acted on.

        Args:
            workspace: The project root, to look for the file; None skips that check.
            branch: The branch the project is on now; None skips that check.

        Returns:
            A short phrase for the report, or "" when nothing looks wrong. A closed mark is
            never stale: it blocks nothing.
        """
        if not self.open:
            return ""
        reasons: list[str] = []
        if workspace is not None and not (workspace / self.path).exists():
            reasons.append("the file is gone")
        if branch and self.git_branch and branch != self.git_branch:
            reasons.append(f"made on {self.git_branch}, now on {branch}")
        return "; ".join(reasons)

    def line(self, workspace: Path | None = None, branch: str | None = None) -> str:
        """Render the mark as one terminal line.

        Args:
            workspace: The project root, so that a mark whose file is gone can be flagged;
                None leaves that unchecked.
            branch: The branch the project is on now, so that a mark from another branch can
                be flagged; None leaves that unchecked.

        Returns:
            The spot, the branch, which checker opened it, the reason and -- for a closed mark
            -- who closed it. The author is named because several checkers may write to the
            register (PLAN.md D87), so "which of them thinks this" is part of reading a mark.
            A mark that looks stale says why (`stale`), because an open mark that cannot be
            right any more only costs the user steps (D91).
        """
        state = "" if self.open else f"  (closed by {self.closed_by or 'somebody'})"
        branch_part = f"  [{self.git_branch}]" if self.git_branch else ""
        author = f"  ({self.opened_by})" if self.opened_by else ""
        why = self.stale(workspace, branch)
        flag = f"  <stale: {why}>" if why else ""
        return f"{self.label()}{branch_part}{author}: {self.reason}{state}{flag}"

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

    A **module** argument is read as well (PLAN.md D89): `python -m myapp` and
    `python -m pkg.module` run project code without naming a file, which is the common way
    dirty code actually gets executed. The dotted name is turned into the files it could be --
    `pkg/module.py` and `pkg/module/__init__.py` -- and those are kept when they exist in the
    project. `-m` is only read after an interpreter (`python`, `python3`, `uv`, `poetry`,
    `pipenv`), so a `-m` that means something else to another program is ignored.

    Args:
        command: The shell command as Claude sent it.
        cwd: The directory the command runs in; a relative word resolves against it.
        workspace: The project root that the register's paths are relative to.

    Returns:
        The paths of files that exist in the project, project-relative with forward slashes.
        Empty when the command names none. A word that normalizes to a path with no file
        behind it is left out, because every word of a command normalizes to something.
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
    for index, word in enumerate(words):
        if word == "-m" and index + 1 < len(words) and _after_interpreter(words, index):
            found |= _module_files(words[index + 1], root)
            continue
        text = word.split("::", 1)[0]
        text = re.sub(r":\d+(:\d+)?$", "", text)
        if not text or text.startswith("-"):
            continue
        try:
            resolved = (
                (base / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()
            )
            relative = resolved.relative_to(root).as_posix()
        except ValueError, OSError:
            continue  # outside the project, or not a usable path
        # Only a file that is really there: every word of a command normalizes to some path,
        # so `make test` would otherwise report "make" and "test" as paths of the project. A
        # mark is always on a file a checker read, so requiring the file loses no true match --
        # and a mark on a file that has since been deleted cannot be run anyway.
        if (root / relative).is_file():
            found.add(relative)
    return found


INTERPRETERS = frozenset({"python", "python3", "python3.14", "uv", "poetry", "pipenv", "pdm"})
"""Programs whose `-m` argument names a Python module (PLAN.md D89). Another program's `-m`
means something else, so the module is only read after one of these."""


def _after_interpreter(words: Sequence[str], index: int) -> bool:
    """Tell whether the `-m` at `index` belongs to a Python interpreter.

    Args:
        words: The words of the command.
        index: Position of the `-m`.

    Returns:
        True if an earlier word is an interpreter, by its base name, so that `/usr/bin/python3`
        and `.venv/bin/python` count as well.
    """
    return any(Path(word).name in INTERPRETERS for word in words[:index])


def _module_files(module: str, root: Path) -> set[str]:
    """Return the project files a dotted module name could be (PLAN.md D89).

    Args:
        module: The module as `-m` names it, e.g. `pkg.module`; a `:` suffix is cut, because
            `python -m pytest tests/x.py::t` puts the node id elsewhere.
        root: The project root.

    Returns:
        The project-relative paths of `pkg/module.py` and `pkg/module/__init__.py` that exist,
        plus the same under a `src/` layout. Empty when the module names nothing in the project
        -- an installed dependency is not this project's code.
    """
    name = module.split(":", 1)[0].strip()
    if not name or name.startswith("-"):
        return set()
    stem = Path(*name.split("."))
    candidates = [stem.with_suffix(".py"), stem / "__init__.py"]
    candidates += [Path("src") / candidate for candidate in list(candidates)]
    return {candidate.as_posix() for candidate in candidates if (root / candidate).is_file()}


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


def marks_of_step(step: Step, marks: Sequence[HonestyMark], workspace: Path) -> list[HonestyMark]:
    """Return the open marks on the files this step touches (PLAN.md D88).

    For an edit or a read that is the file named in the tool input; for a command it is every
    path the command names. The caller uses this to tell a checker what the register already
    says about the code in front of it, which is what lets the checker clear a mark.

    Args:
        step: The tool call.
        marks: The marks to consider; closed ones are ignored.
        workspace: The project root the marks' paths are relative to.

    Returns:
        The open marks on the step's own files, in the order given.
    """
    named: set[str] = set()
    for key in ("file_path", "notebook_path", "path"):
        value = step.tool_input.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value)
            base = Path(step.cwd) if step.cwd else workspace
            try:
                resolved = candidate if candidate.is_absolute() else base / candidate
                named.add(resolved.resolve().relative_to(workspace.resolve()).as_posix())
            except ValueError, OSError:
                continue
    if step.tool_name == "Bash":
        named |= command_paths(
            str(step.tool_input.get("command", "")),
            Path(step.cwd) if step.cwd else workspace,
            workspace,
        )
    return [entry for entry in marks if entry.open and entry.path in named]


def diff_paths(diff: str) -> set[str]:
    """Return the project-relative paths a `git diff` text touches (PLAN.md D88).

    A commit names no file in its command, so this is how the changeset checker is told which
    of the files it is about to record are in the register. Only the `diff --git a/x b/x`
    headers are read, and the `b/` side is taken, because that is the file as it will be after
    the commit (a rename's `a/` side no longer exists).

    Args:
        diff: The diff as `git diff` prints it; may be empty or a placeholder.

    Returns:
        The paths, project-relative with forward slashes. Empty when the text has no diff
        headers.
    """
    found: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" b/", 1)
        if len(parts) == 2 and parts[1].strip():
            found.add(parts[1].strip())
    return found


def register_note(
    marks: Sequence[HonestyMark], own: Sequence[HonestyMark] = (), limit: int = 20
) -> str:
    """Render the register as a note for a checker (§13.4, D88).

    The note travels in `Step.notes`, where the rule hints already are, so every checker of the
    step sees it. It has two parts, because the two routes need different things:

    - the marks on **this step's own files** (`own`), named with their reasons, because an edit
      checker judging that file is the one that can clear them;
    - the rest, counted and named up to `limit`, because a command that names no path at all
      (`make test`) can still reach them and only the checker can tell.

    Args:
        marks: Every open mark of the project; closed ones are left out.
        own: The marks on the files this step touches (`marks_of_step`), which are named first.
        limit: How many of the rest are named before they are only counted.

    Returns:
        The note, or "" when nothing is open.
    """
    standing = [entry for entry in marks if entry.open]
    if not standing:
        return ""
    mine = [entry for entry in own if entry.open]
    keys = {entry.mark().key() for entry in mine}
    others = [entry for entry in standing if entry.mark().key() not in keys]
    parts: list[str] = []
    if mine:
        spots = "; ".join(f"{entry.label()} ({entry.reason})" for entry in mine)
        parts.append(
            "This step's own code is recorded as dishonest: "
            f"{spots}. If this step makes the code and its documentation agree, say so in "
            "`marks_clean`; if it leaves them disagreeing, keep the mark in `marks_dirty`."
        )
    if others:
        shown = others[:limit]
        spots = "; ".join(f"{entry.label()} ({entry.reason})" for entry in shown)
        rest = f" and {len(others) - len(shown)} more" if len(others) > len(shown) else ""
        parts.append(
            "Other code recorded as dishonest, which must not be run until it is fixed: "
            f"{spots}{rest}. A step that would run one of these is a DENY; editing them to "
            "make the code and its documentation agree is what clears the record."
        )
    return " ".join(parts)
