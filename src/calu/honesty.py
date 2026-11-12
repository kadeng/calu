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

The second half of the module is the **honesty state** of the redesign (trusted commits, the
owner's decisions of 2026-11-12). calu tracks which code was verified to be honest:

    trusted commit   a commit a person or calu certified (database table `trusted_commits`)
    baseline         the nearest trusted ancestor of HEAD (`find_baseline`)
    HonestyState     per session, in memory: the marks, the verified contents (path ->
                     SHA-256), the files to watch that git does not list, "review required"
    HonestySnapshot  the state at one moment: baseline, open marks, unreviewed files,
                     review required; `clean` and the note for the checkers are computed
                     from it

`HonestyState` reads git and files and nothing else; the database is asked by its caller
(`find_baseline` takes the lookup as a function). The session still enforces the register
above; the state replaces it in a later step.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import subprocess
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import anyio.to_thread

from calu.checkers import CodeMark, Step
from calu.checkers.changeset import GIT_OPTIONS, HEAD_ID
from calu.checkers.policy import SHELL_TOOLS
from calu.checkers.shell import parse_command
from calu.checkers.symbols import definition_names


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
    workspace: str | None = None
    """Project directory of the session that opened the mark, or None for a row whose session
    is gone. `judged_here` uses it: whether a marked file still exists can only be answered
    where that project is (PLAN.md D126)."""
    hostname: str | None = None
    """Host of that session, for the same reason: the same path on another machine is another
    file."""
    beyond_step: bool | None = None
    """True when the mark names a file that the step which opened it did not touch, False when
    it names one of that step's files, None when the step named no file or the row predates the
    record (PLAN.md D141, Q20). Only shown, never enforced differently."""

    @property
    def reach(self) -> str:
        """Say that the mark reaches beyond its step, if it does.

        Returns:
            "not a file of its step" for a mark with `beyond_step` True, else "".
        """
        return "not a file of its step" if self.beyond_step else ""

    def judged_here(self, workspace: Path, hostname: str) -> bool:
        """Tell whether this mark's file can be looked at from here.

        An operator reads the register from wherever they are, usually not from the machine the
        session ran on. "Is the marked file still there?" is then unanswerable, and answering it
        from the current directory says "the file is gone" about every mark of every other
        project -- which invites clearing marks that are perfectly valid (D126).

        Args:
            workspace: The directory the command is being run in.
            hostname: The host it is being run on.

        Returns:
            True only when this mark was opened for that directory on that host.
        """
        if not self.workspace or not self.hostname:
            return False
        return self.hostname == hostname and Path(self.workspace) == workspace

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
            workspace: The project root, to look for the file; None skips that check, which is
                the right answer away from the project itself (`judged_here`).
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
            right any more only costs the user steps (D91). A mark on a file its step did not
            touch says so (`reach`, D141).
        """
        state = "" if self.open else f"  (closed by {self.closed_by or 'somebody'})"
        branch_part = f"  [{self.git_branch}]" if self.git_branch else ""
        author = f"  ({self.opened_by})" if self.opened_by else ""
        why = self.stale(workspace, branch)
        flag = f"  <stale: {why}>" if why else ""
        reach = f"  <{self.reach}>" if self.reach else ""
        return f"{self.label()}{branch_part}{author}: {self.reason}{state}{reach}{flag}"

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

    The paths the shell splitter resolves are added as well, so a `cd` earlier in the command
    and a glob are followed (D150).

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
    # The shell splitter follows `cd` and expands globs, which a word-by-word reading cannot:
    # `cd src && python parser.py` and `python src/pars*.py` run src/parser.py too (D150).
    for used in parse_command(command, base).paths:
        for path in used.paths:
            if path is None:
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
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
    if step.tool_name not in SHELL_TOOLS:
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
    if step.tool_name in SHELL_TOOLS:
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


class Register:
    """The honesty register of one session, held in memory only (PLAN.md D180).

    Dirty and clean are transient states of the code a session is working on: the owner's
    answer to Q19 is that they are kept in memory and persisted nowhere -- certainly not in a
    place as easy to change as a database table. A register therefore lives as long as its
    session and starts empty; another session of the same project has its own.

        apply(dirty, clean)  ->  opens or refreshes a mark per dirty spot, closes clean ones
        clear(id)            ->  a person's override: closes one open mark
        marks                ->  every mark of the session, open and closed, oldest first
    """

    def __init__(self) -> None:
        """Create an empty register."""
        self.marks: list[HonestyMark] = []
        self._next_id = 1

    @property
    def open(self) -> list[HonestyMark]:
        """The open marks, oldest first.

        Returns:
            The marks nothing has closed yet; these are what blocks a step.
        """
        return [mark for mark in self.marks if mark.open]

    def apply(
        self,
        dirty: Sequence[CodeMark],
        clean: Sequence[CodeMark],
        by: str = "",
        files: dict[str, str] | None = None,
        step_paths: Collection[str] = (),
    ) -> tuple[int, int]:
        """Open and close marks as a checker's verdict asked (D82).

        A dirty mark for a spot that is already open refreshes its reason, author and reach
        instead of adding a mark. A clean mark closes the open mark of its spot; one that
        matches nothing open changes nothing and is not counted.

        Args:
            dirty: Spots the step left dishonest.
            clean: Spots that say what they do again.
            by: Name of the checker whose verdict carried the marks (D87).
            files: The file the step edits, as it was, kept with a mark it opens (D95).
            step_paths: The paths the step is about, to record each mark's reach (D141).

        Returns:
            How many marks were opened or refreshed, and how many were closed.
        """
        opened = 0
        for entry in dirty:
            reach = beyond_step(entry.path, step_paths)
            existing = next((m for m in self.open if m.mark().key() == entry.key()), None)
            if existing is not None:
                self.marks[self.marks.index(existing)] = replace(
                    existing, reason=entry.reason, opened_by=by or None, beyond_step=reach
                )
            else:
                self.marks.append(
                    HonestyMark(
                        id=self._next_id,
                        path=entry.path,
                        symbol=entry.symbol,
                        reason=entry.reason,
                        opened_at=datetime.now(UTC),
                        opened_by=by or None,
                        files=dict(files or {}),
                        beyond_step=reach,
                    )
                )
                self._next_id += 1
            opened += 1
        closed = 0
        for entry in clean:
            existing = next((m for m in self.open if m.mark().key() == entry.key()), None)
            if existing is None:
                continue
            self.marks[self.marks.index(existing)] = replace(
                existing, closed_at=datetime.now(UTC), closed_by="a checker"
            )
            closed += 1
        return opened, closed

    def clear(self, mark_id: int, by: str) -> HonestyMark | None:
        """Close one open mark by hand -- the override when a checker is wrong (D83).

        Args:
            mark_id: The mark's id, as the session reported it.
            by: Who closed it, for the session's log.

        Returns:
            The closed mark, or None if no open mark has that id.
        """
        for index, mark in enumerate(self.marks):
            if mark.id == mark_id and mark.open:
                self.marks[index] = replace(mark, closed_at=datetime.now(UTC), closed_by=by)
                return self.marks[index]
        return None


def beyond_step(path: str, step_paths: Collection[str]) -> bool | None:
    """Tell whether a mark's file lies outside the files of the step that produced it (D141).

    Q20 asks the owner whether a checker may mark a file the step did not touch. Until that is
    decided calu allows it (D112) and records the reach, so a person can see it.

    Args:
        path: The project-relative path of the mark, as `checked_marks` normalised it.
        step_paths: The project-relative paths the step is about.

    Returns:
        None when the step names no file, so that nothing can be said; otherwise whether
        `path` is not one of them.

    Example:
        beyond_step("README.md", {"src/parser.py"}) is True;
        beyond_step("src/parser.py", {"src/parser.py"}) is False;
        beyond_step("src/parser.py", ()) is None.
    """
    if not step_paths:
        return None
    return path not in step_paths


def checked_marks(
    marks: Sequence[CodeMark],
    workspace: Path,
    step_paths: Collection[str] = (),
    *,
    must_exist: bool,
) -> tuple[list[CodeMark], list[str]]:
    """Keep the marks whose path calu may put in the register, and name the rest (PLAN.md D112).

    A mark is not a tool call: it is a field of the checker's structured answer, so the model
    writes the path as text and nothing checked it. An invented path has two bad outcomes --
    `../../etc/passwd` or `/tmp/x` sits in the register forever, because only a person can
    clear it, and a plausible but unrelated project file **blocks commands that run that file**
    although no checker ever looked at it. Both are filtered here, at the one place that knows
    the workspace.

    What is required of a path: relative, no `..` component, not absolute, and -- for a mark
    that opens a spot -- a file that either exists in the workspace or is one of the paths the
    step itself is about (`step_paths`). The second half matters because a checker judges a
    step **before** it runs: a mark about the file a `Write` is creating names a file that is
    not there yet, and rejecting it would throw away the one judgement the checker was asked
    for.

    A mark that **clears** a spot is checked for shape only. Deleting a dishonest file is a
    legitimate way to make the register true again, and the file is then gone.

    Args:
        marks: The marks as the checker wrote them.
        workspace: The project root; paths are read relative to it.
        step_paths: Project-relative paths the step itself touches, which count as existing.
        must_exist: Whether the file has to exist (True for dirty marks, False for clean ones).

    Returns:
        (the marks to use, one message per rejected mark saying which mark and why). The
        messages are for the log and for `calu checker stats`; a rejected mark is dropped, not
        escalated, because a checker that names a wrong file has not said anything about the
        step.
    """
    kept: list[CodeMark] = []
    rejected: list[str] = []
    for mark in marks:
        path = mark.path.strip()
        if not path:
            rejected.append("a mark without a path")
            continue
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or path.startswith("~") or ".." in candidate.parts:
            rejected.append(f"{mark.label()}: the path must be relative to the project")
            continue
        if must_exist and path not in step_paths and not (workspace / path).is_file():
            rejected.append(f"{mark.label()}: there is no such file in the project")
            continue
        kept.append(mark.model_copy(update={"path": str(candidate)}))
    return kept, rejected


# ---------------------------------------------------------------------------------------------
# The honesty state: trusted commits, the baseline, verified contents, unreviewed files.
# ---------------------------------------------------------------------------------------------

BASELINE_SEARCH_LIMIT = 5000
"""How many commits `find_baseline` reads from HEAD backwards. A trusted commit further back is
not found, and the session then has no baseline."""

NOTE_LIMIT = 20
"""How many unreviewed files, and how many marks on other files, the note for a checker names
before it only counts the rest."""

SHORT_ID = 12
"""Characters of a commit id that are shown to a person or a checker."""

GIT_TIMEOUT_S = 30.0
"""Upper bound for one git call of the honesty state. A call that takes longer counts as
failed, and every failure is read as "not verified"."""

GITLINK_MODE = "160000"
"""The mode of a submodule entry in a git tree. calu does not look into submodules."""

SETTLE_NS = 2_000_000_000
"""A file's hashes are remembered with its size and times only when its last change is at
least this old, so that two writes within the clock's resolution are both seen."""

CONTENT_ID = re.compile(r"[0-9a-f]{64}")
"""A SHA-256 as the verified map and the table `trusted_files` hold it."""


class GitError(Exception):
    """git could not answer a question of the honesty state.

    The message is one line. Callers read a failure as the unverified case: no baseline, every
    file unreviewed, no certification.
    """


@dataclass(frozen=True)
class GitCommit:
    """A commit and the tree it records. A commit is trusted by either id."""

    commit_id: str
    tree_id: str

    @property
    def short(self) -> str:
        """The commit id as it is shown.

        Returns:
            Its first `SHORT_ID` characters.
        """
        return self.commit_id[:SHORT_ID]


@dataclass(frozen=True)
class ContentIds:
    """Two hashes of one file content."""

    sha256: str
    """SHA-256 of the content: what the verified map and the table `trusted_files` hold."""
    blob: str
    """The id git gives a blob with this content, for the comparison with a git tree."""


@dataclass(frozen=True)
class Unreviewed:
    """The files whose present content nobody verified."""

    everything: bool = False
    """True when every file counts as unreviewed: there is no baseline, or git could not list
    the files (`problem` then says why)."""
    paths: tuple[str, ...] = ()
    """The unreviewed files, sorted: relative to the workspace, or absolute for a file outside
    it. Empty when `everything` is set."""
    problem: str = ""
    """Why git could not be asked, or ""."""

    def __bool__(self) -> bool:
        """Tell whether anything is unreviewed.

        Returns:
            True if `everything` is set or at least one file is named.
        """
        return self.everything or bool(self.paths)


@dataclass(frozen=True)
class Finding:
    """One spot an honesty review found dishonest in a file."""

    symbol: str
    """The function, method or class; "" for the whole file."""
    reason: str


@dataclass(frozen=True)
class FileReview:
    """What an honesty review says about one file."""

    sha256: str
    """SHA-256 of the content the review read (`HonestyState.content_sha256` before it read)."""
    findings: tuple[Finding, ...] = ()
    """Empty when the review judged the file honest."""


@dataclass
class MarkChanges:
    """What one call changed in the marks of a `HonestyState`."""

    opened: list[HonestyMark] = field(default_factory=list)
    """Marks that were opened, or whose reason was refreshed."""
    closed: list[HonestyMark] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    """One sentence per mark of the answer that was not applied, saying which and why. The
    session logs them and tells the checker."""
    review_required: bool = False
    """Whether a review is required after the call."""


@dataclass(frozen=True)
class HonestySnapshot:
    """The honesty state of a session at one moment (specification section 2)."""

    baseline: GitCommit | None
    marks: tuple[HonestyMark, ...]
    """The open marks, oldest first."""
    unreviewed: Unreviewed
    review_required: bool

    @property
    def clean(self) -> bool:
        """Whether code may be run and committed under `honesty.mode = enforce`.

        Returns:
            True when there is a baseline, no open mark, no unreviewed file and no review is
            required.
        """
        return (
            self.baseline is not None
            and not self.marks
            and not self.unreviewed
            and not self.review_required
        )

    def note(self, own_paths: Collection[str] = (), limit: int = NOTE_LIMIT) -> str:
        """Render the state as the note every change checker gets (specification section 6).

        The note is data computed by calu. What a checker does with it is said once, in the
        fixed instruction part `system/marks.md`.

        Args:
            own_paths: The workspace-relative paths of the step being judged; the marks on
                them are named first and all of them.
            limit: How many unreviewed files, and how many marks on other files, are named
                before the rest is counted.

        Returns:
            Several lines: the baseline (short id, or that there is none), the open marks with
            their reasons, the unreviewed files and their number, and whether a review is
            required. Never empty.

        Example:
            Code honesty state, computed by calu.
            Baseline: trusted commit 1a2b3c4d5e6f.
            Open marks on this step's files: src/parser.py:fields (returns unsorted).
            Unreviewed files (2): scripts/gen.py, src/new.py.
            Review required: no.
        """
        lines = ["Code honesty state, computed by calu."]
        if self.baseline is None:
            lines.append(
                "Baseline: none. No ancestor of HEAD is a trusted commit, so every file counts "
                "as unreviewed."
            )
        else:
            lines.append(f"Baseline: trusted commit {self.baseline.short}.")
        own = [mark for mark in self.marks if mark.path in own_paths]
        others = [mark for mark in self.marks if mark.path not in own_paths]
        if own:
            spots = "; ".join(f"{mark.label()} ({mark.reason})" for mark in own)
            lines.append(f"Open marks on this step's files: {spots}.")
        if others:
            shown = others[:limit]
            spots = "; ".join(f"{mark.label()} ({mark.reason})" for mark in shown)
            rest = f"; and {len(others) - len(shown)} more" if len(others) > len(shown) else ""
            lines.append(f"Open marks on other files: {spots}{rest}.")
        if not self.marks:
            lines.append("Open marks: none.")
        if self.unreviewed.everything:
            if self.baseline is not None:
                lines.append(
                    "Unreviewed files: all (the files could not be compared with the "
                    f"baseline: {self.unreviewed.problem or 'git did not answer'})."
                )
        elif self.unreviewed.paths:
            paths = self.unreviewed.paths
            rest = f", and {len(paths) - limit} more" if len(paths) > limit else ""
            lines.append(f"Unreviewed files ({len(paths)}): {', '.join(paths[:limit])}{rest}.")
        else:
            lines.append("Unreviewed files: none.")
        lines.append(
            "Review required: yes. A clean mark is ignored until an honesty review passes."
            if self.review_required
            else "Review required: no."
        )
        return "\n".join(lines)


def run_git(cwd: Path, arguments: Sequence[str], *, accept: tuple[int, ...] = (0,)) -> bytes:
    """Run one git command for the honesty state and return its output.

    The command runs with `calu.checkers.changeset.GIT_OPTIONS` (no pager, no file system
    monitor, no rewrite of the index), without prompting and without fetching missing objects.
    The honesty state only uses commands that list names and ids (`rev-parse`, `rev-list`,
    `ls-tree`, `ls-files`); calu reads and hashes file contents itself.

    Args:
        cwd: The directory to run in.
        arguments: The git arguments, e.g. `["rev-parse", "HEAD"]`.
        accept: The exit codes that are no error.

    Returns:
        The standard output.

    Raises:
        GitError: If git is missing, does not answer within `GIT_TIMEOUT_S` or exits with a
            code that is not in `accept`.
    """
    environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            ["git", *GIT_OPTIONS, *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {arguments[0]} failed ({type(exc).__name__})") from exc
    if done.returncode not in accept:
        message = done.stderr.decode(errors="replace").strip().splitlines()
        reason = message[0][:300] if message else f"exit code {done.returncode}"
        raise GitError(f"git {arguments[0]}: {reason}")
    return done.stdout


def ancestors(
    workspace: Path, limit: int = BASELINE_SEARCH_LIMIT, rev: str = "HEAD"
) -> list[GitCommit]:
    """List a commit and its ancestors, nearest first, with the tree of each.

    Args:
        workspace: A directory inside the repository.
        limit: The largest number of commits returned.
        rev: The commit to start from: "HEAD" or a full commit id.

    Returns:
        Up to `limit` commits in the order of `git rev-list`, starting with `rev` itself. []
        if the directory is in no repository, the repository has no commit yet, `rev` names
        none, or git could not be asked.
    """
    if limit < 1 or not (rev == "HEAD" or HEAD_ID.fullmatch(rev)):
        return []
    try:
        output = run_git(workspace, ["rev-list", "--format=%T", "-n", str(limit), rev])
    except GitError:
        return []
    found: list[GitCommit] = []
    commit_id = ""
    for line in output.decode(errors="replace").splitlines():
        if line.startswith("commit "):
            commit_id = line.removeprefix("commit ").strip()
        elif commit_id and HEAD_ID.fullmatch(line.strip()):
            found.append(GitCommit(commit_id, line.strip()))
            commit_id = ""
    return found


def resolve_commit(directory: Path, rev: str) -> GitCommit:
    """Resolve a commit id, branch or tag to the commit it names now.

    A branch is resolved to the commit it points at when this is called; nothing remembers the
    branch.

    Args:
        directory: A directory inside the repository.
        rev: What the person wrote: a commit id or its prefix, a branch, a tag, "HEAD".

    Returns:
        The commit with its tree.

    Raises:
        GitError: If `rev` is empty, starts with "-", names no single commit of the
            repository, or git could not be asked.
    """
    text = rev.strip()
    if not text or text.startswith("-"):
        raise GitError(f"{rev!r} is not a commit or a branch")
    try:
        output = run_git(directory, ["rev-parse", "--verify", "--quiet", f"{text}^{{commit}}"])
    except GitError as exc:
        raise GitError(f"{rev!r} names no commit in {directory} ({exc})") from exc
    commit_id = output.decode(errors="replace").strip()
    found = ancestors(directory, 1, commit_id)
    if len(found) != 1 or found[0].commit_id != commit_id:
        raise GitError(f"git did not name the tree of {rev!r}")
    return found[0]


def nearest_trusted(
    commits: Sequence[GitCommit], trusted_commits: Collection[str], trusted_trees: Collection[str]
) -> GitCommit | None:
    """Pick the baseline out of a list of ancestors.

    Args:
        commits: A commit and its ancestors, nearest first (`ancestors`).
        trusted_commits: The commit ids among them that the database holds as trusted.
        trusted_trees: The tree ids among theirs that belong to a trusted commit.

    Returns:
        The first commit whose commit id or tree id is trusted; None if there is none.
    """
    for commit in commits:
        if commit.commit_id in trusted_commits or commit.tree_id in trusted_trees:
            return commit
    return None


type TrustLookup = Callable[
    [Sequence[str], Sequence[str]], Awaitable[tuple[Collection[str], Collection[str]]]
]
"""Asks the database which of some commit ids and tree ids are trusted
(`SessionStore.trusted_among`): (commit ids, tree ids) -> (the trusted commit ids among them,
the tree ids among them that a trusted commit records)."""


async def find_baseline(
    workspace: Path, lookup: TrustLookup, limit: int = BASELINE_SEARCH_LIMIT
) -> GitCommit | None:
    """Find the baseline of a session: the nearest trusted ancestor of HEAD, HEAD included.

    The search is bounded: only the last `limit` commits (default `BASELINE_SEARCH_LIMIT`,
    5000) are read, in the order of `git rev-list`. A trusted commit further back is not
    found. One database question is asked for all of them.

    Args:
        workspace: The project directory.
        lookup: Asks the database which ids are trusted (`TrustLookup`).
        limit: The largest number of commits to look at.

    Returns:
        The baseline, or None when the directory is in no git repository, HEAD has no commit,
        git could not be asked, or none of the commits read is trusted.

    Raises:
        Exception: Whatever `lookup` raises (`psycopg.Error` for the database store). The
            caller must then treat the session as having no baseline.
    """
    commits = await anyio.to_thread.run_sync(ancestors, workspace, limit)
    if not commits:
        return None
    trusted_commits, trusted_trees = await lookup(
        [commit.commit_id for commit in commits],
        sorted({commit.tree_id for commit in commits}),
    )
    return nearest_trusted(commits, trusted_commits, trusted_trees)


def content_ids(path: Path, object_format: str = "sha1") -> ContentIds | None:
    """Hash the content of one file, without following a symbolic link.

    Args:
        path: The file. For a symbolic link the content is the link's target path, which is
            also what git records of it.
        object_format: The hash of the repository's object ids, "sha1" or "sha256".

    Returns:
        Both hashes; None if the path is missing, is neither a regular file nor a symbolic
        link, could not be read, or changed in size while it was read.
    """
    try:
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
            blob = hashlib.new(object_format, b"blob %d\0" % len(data), usedforsecurity=False)
            blob.update(data)
            return ContentIds(hashlib.sha256(data).hexdigest(), blob.hexdigest())
        with path.open("rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                return None
            plain = hashlib.sha256()
            blob = hashlib.new(object_format, b"blob %d\0" % status.st_size, usedforsecurity=False)
            read = 0
            while chunk := handle.read(1 << 20):
                read += len(chunk)
                plain.update(chunk)
                blob.update(chunk)
            if read != status.st_size:
                return None
            return ContentIds(plain.hexdigest(), blob.hexdigest())
    except OSError, ValueError:
        return None


def normal_symbol(symbol: str) -> str:
    """Normalise a symbol as a checker wrote it.

    Args:
        symbol: E.g. "Parser.fields()" or " fields ".

    Returns:
        The symbol without surrounding blanks and without a trailing "()".
    """
    text = symbol.strip()
    while text.endswith("()"):
        text = text[:-2].rstrip()
    return text


def match_symbol(symbol: str, names: Collection[str]) -> str | None:
    """Find the name a checker means among the names calu knows.

    Args:
        symbol: The normalised symbol (`normal_symbol`); "" means the whole file.
        names: The known names: the definitions of a Python file, or the symbols of the open
            marks of a file.

    Returns:
        `symbol` itself if it is one of `names`; else the one name that ends in `.symbol`
        (the bare name `fields` for `Parser.fields`); None if no name or more than one fits.

    Example:
        match_symbol("fields", ["Parser", "Parser.fields"]) == "Parser.fields"
        match_symbol("run", ["A.run", "B.run"]) is None
    """
    if symbol in names:
        return symbol
    if not symbol:
        return None
    fitting = [name for name in names if name.endswith("." + symbol)]
    return fitting[0] if len(fitting) == 1 else None


class HonestyState:
    """The honesty state of one session, in memory only (specification section 2, D180).

    The state holds what cannot be computed: the marks, the verified contents, the files to
    watch that git does not list, the hashes a person trusted, and whether a review is
    required. Everything else is computed when asked, from the working tree and the baseline:

        record(dirty, clean, by, edited)   a checker's marks, after the tool call succeeded
        record_edit(path, basis_reviewed)  a checked edit: its file is verified at its content
        apply_review(results)              an honesty review: verified files and findings
        close_stale() / clear(id, by)      marks calu or a person closes
        watch(path)                        a file git does not list that the session wrote
        snapshot()                         baseline, open marks, unreviewed files, required
        uncertified(commit)                why a commit may not be certified, or ""

    Paths are relative to the workspace, with forward slashes; a file outside the workspace is
    named by its absolute path. A file is **verified** while the SHA-256 of its content is the
    one recorded for its path, or one a person stored as trusted. A file is **unreviewed**
    when it exists, differs from the baseline (or the baseline does not hold it) and is not
    verified. Every failure to read git or a file counts as unreviewed.

    The methods read git and files synchronously and take no lock: the session calls them from
    one task at a time (in a worker thread where a call may be slow).
    """

    def __init__(self, workspace: Path, max_open_marks: int = 3) -> None:
        """Create an empty state: no baseline, no mark, nothing verified.

        Args:
            workspace: The project directory.
            max_open_marks: `honesty.max_open_marks`: with more open marks than this a review
                is required. The attribute may be set again when the configuration changes.
        """
        self.workspace = workspace.resolve()
        self.max_open_marks = max_open_marks
        self.baseline: GitCommit | None = None
        """The nearest trusted ancestor of HEAD, set by the session (`find_baseline`)."""
        self.marks: list[HonestyMark] = []
        """Every mark of the session, open and closed, oldest first."""
        self.verified: dict[str, str] = {}
        """Path -> SHA-256 of the content that a checked edit or an honesty review judged."""
        self.watched: set[str] = set()
        """Files git does not list (ignored, or outside the repository) that the session wrote
        or a person named. calu does not search for others."""
        self.trusted_hashes: frozenset[str] = frozenset()
        """The SHA-256 values of the table `trusted_files`, set by the session. A file with
        such a content is verified wherever it lies."""
        self.review_required = False
        self._next_id = 1
        self._top: Path | None = None
        self._object_format: str | None = None
        self._trees: dict[str, dict[str, tuple[str, str]]] = {}
        self._hashed: dict[str, tuple[tuple[int, int, int, int], ContentIds]] = {}

    # ------------------------------------------------------------------ paths and contents

    def key(self, path: str | Path) -> str:
        """Return the name the state uses for a file.

        Args:
            path: Absolute, or relative to the workspace. A symbolic link as the last
                component is not followed.

        Returns:
            The path relative to the workspace with forward slashes, or the normalised
            absolute path for a file outside the workspace.
        """
        absolute = Path(os.path.normpath(self.workspace / path))
        try:
            return absolute.relative_to(self.workspace).as_posix()
        except ValueError:
            pass
        # The workspace is held resolved; a path written through a linked directory may
        # still lie inside it.
        try:
            absolute = absolute.parent.resolve() / absolute.name
            return absolute.relative_to(self.workspace).as_posix()
        except OSError, ValueError:
            return absolute.as_posix()

    def _content(self, key: str) -> ContentIds | None:
        """Hash a file's present content, reusing the last result while the file is unchanged.

        A result is reused when the file's size, change times and inode are the ones it was
        computed for, and it is only remembered for a file whose last change is `SETTLE_NS`
        old.

        Args:
            key: The file, as `key` names it.

        Returns:
            The hashes, or None as `content_ids` returns it.
        """
        path = self.workspace / key
        try:
            status = path.lstat()
        except OSError:
            self._hashed.pop(key, None)
            return None
        seen = (status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino)
        remembered = self._hashed.get(key)
        if remembered is not None and remembered[0] == seen:
            return remembered[1]
        ids = content_ids(path, self._format())
        self._hashed.pop(key, None)
        if ids is not None and time.time_ns() - max(seen[1], seen[2]) > SETTLE_NS:
            self._hashed[key] = (seen, ids)
        return ids

    def content_sha256(self, path: str | Path) -> str | None:
        """Return the SHA-256 of a file's present content.

        An honesty review asks for it before it reads a file and hands it back in its
        `FileReview`, so that the file is verified at the content the review read.

        Args:
            path: Absolute, or relative to the workspace.

        Returns:
            The hash, or None if the file is missing or cannot be hashed (`content_ids`).
        """
        ids = self._content(self.key(path))
        return ids.sha256 if ids is not None else None

    def _format(self) -> str:
        """Return the hash of the repository's object ids.

        Returns:
            "sha1" or "sha256". "sha1" while git cannot be asked; a wrong guess makes a file
            differ from the baseline, which is the unverified case.
        """
        if self._object_format is None:
            try:
                answer = run_git(self.workspace, ["rev-parse", "--show-object-format"])
            except GitError:
                return "sha1"
            name = answer.decode(errors="replace").strip()
            self._object_format = name if name in ("sha1", "sha256") else "sha1"
        return self._object_format

    def _repository(self) -> Path:
        """Return the top directory of the repository the workspace is in.

        Returns:
            The directory, resolved.

        Raises:
            GitError: If the workspace is in no git repository or git could not be asked.
        """
        if self._top is None:
            answer = run_git(self.workspace, ["rev-parse", "--show-toplevel"])
            text = os.fsdecode(answer).rstrip("\n")
            if not text:
                raise GitError("git did not name the repository's directory")
            self._top = Path(text).resolve()
        return self._top

    def _tree(self, commit_id: str) -> dict[str, tuple[str, str]]:
        """List the files a commit records.

        Args:
            commit_id: A full commit id.

        Returns:
            Path relative to the repository's top directory -> (mode, object id). The last
            few listings are kept, because a commit's tree never changes.

        Raises:
            GitError: If git could not list the commit.
        """
        known = self._trees.get(commit_id)
        if known is not None:
            return known
        output = run_git(self._repository(), ["ls-tree", "-r", "-z", "--full-tree", commit_id])
        listed: dict[str, tuple[str, str]] = {}
        for entry in output.split(b"\0"):
            head, tab, name = entry.partition(b"\t")
            parts = head.split()
            if not tab or len(parts) != 3:
                continue
            listed[os.fsdecode(name)] = (parts[0].decode(), parts[2].decode())
        while len(self._trees) >= 4:
            self._trees.pop(next(iter(self._trees)))
        self._trees[commit_id] = listed
        return listed

    def _verified_content(self, key: str, ids: ContentIds) -> bool:
        """Tell whether a content is verified for a file.

        Args:
            key: The file.
            ids: The hashes of its present content.

        Returns:
            True if the SHA-256 is the one recorded for the path or one a person stored as
            trusted.
        """
        return self.verified.get(key) == ids.sha256 or ids.sha256 in self.trusted_hashes

    def is_reviewed(self, path: str | Path) -> bool:
        """Tell whether a file's present content is one that was reviewed.

        The session asks this before an edit runs: an edit that changes a part of a file
        verifies the file only if the content it starts from was reviewed (`record_edit`).

        Args:
            path: Absolute, or relative to the workspace.

        Returns:
            True if the file does not exist, if its content is verified, or if it is the
            content the baseline records for that path. False otherwise, also when the file
            cannot be read or git cannot be asked.
        """
        key = self.key(path)
        ids = self._content(key)
        if ids is None:
            return not os.path.lexists(self.workspace / key)
        if self._verified_content(key, ids):
            return True
        if self.baseline is None:
            return False
        try:
            top = self._repository()
            inside = (self.workspace / key).relative_to(top).as_posix()
            entry = self._tree(self.baseline.commit_id).get(inside)
        except GitError, ValueError:
            return False
        return entry is not None and entry[1] == ids.blob

    # ------------------------------------------------------------------ verification

    def watch(self, path: str | Path) -> str:
        """Remember a file so that it is tracked even if git does not list it.

        For a file the session wrote through an edit or that a measured write set names, and
        for one a person names. A file git lists is tracked anyway; watching it changes
        nothing.

        Args:
            path: Absolute, or relative to the workspace.

        Returns:
            The file's name in the state (`key`).
        """
        key = self.key(path)
        self.watched.add(key)
        return key

    def record_edit(self, path: str | Path, *, basis_reviewed: bool) -> bool:
        """Record that the edit checkers judged an edit of a file, after the edit succeeded.

        The file is watched from now on. If the edit's basis was reviewed, the file is
        verified at its present content; otherwise what was recorded for the path is dropped,
        so the file stays unreviewed until an honesty review reads it. A mark the checkers
        left on the file is separate: a file with an open mark is dishonest whatever is
        recorded here (`record`).

        Args:
            path: The edited file; absolute, or relative to the workspace.
            basis_reviewed: Whether the checkers judged the whole content the file now has:
                True when `is_reviewed(path)` was True before the edit ran, and for a tool
                that writes the whole file (Write).

        Returns:
            True if the file is now verified at its present content.
        """
        key = self.watch(path)
        ids = self._content(key) if basis_reviewed else None
        if ids is None:
            self.verified.pop(key, None)
            return False
        self.verified[key] = ids.sha256
        return True

    def apply_review(self, results: Mapping[str, FileReview]) -> MarkChanges:
        """Apply the result of an honesty review (specification section 4).

        For every reviewed file: its open marks are closed, the file is verified at the
        content the review read, and each finding opens a mark. A result without a SHA-256
        changes nothing for its file. Afterwards a review is
        required if more than `max_open_marks` marks are open, and no longer required if none
        is open. A review that failed must not be passed here: it verifies nothing.

        Args:
            results: Path (as `key` names it) -> what the review said about the file.

        Returns:
            The marks closed and opened, findings that could not be recorded, and whether a
            review is required now.
        """
        changes = MarkChanges()
        for path, review in results.items():
            key = self.key(path)
            if not CONTENT_ID.fullmatch(review.sha256):
                changes.ignored.append(f"{key}: the review named no content hash")
                continue
            for mark in [m for m in self.open_marks if m.path == key]:
                changes.closed.append(self._close(mark, "the honesty review"))
            self.verified[key] = review.sha256
            self.watched.add(key)
            for finding in review.findings:
                changes.opened.append(
                    self._open(key, finding.symbol, finding.reason, "the honesty review")
                )
        if not self.open_marks:
            self.review_required = False
        self._check_threshold()
        changes.review_required = self.review_required
        return changes

    # ------------------------------------------------------------------ marks

    @property
    def open_marks(self) -> list[HonestyMark]:
        """The open marks, oldest first.

        Returns:
            The marks nothing has closed yet.
        """
        return [mark for mark in self.marks if mark.open]

    def dishonest_files(self) -> set[str]:
        """Name the files that carry at least one open mark.

        Returns:
            Their paths.
        """
        return {mark.path for mark in self.open_marks}

    def record(
        self,
        dirty: Sequence[CodeMark],
        clean: Sequence[CodeMark],
        by: str = "",
        edited: Collection[str] = (),
    ) -> MarkChanges:
        """Apply the marks of one checker's answer, after its tool call succeeded (section 3).

        Clean marks are applied first, then dirty ones, so a spot that the same answer names
        in both stays open. A clean mark closes an open mark only if no review is required,
        the step edits that file, and path and symbol match (`normal_symbol`, `match_symbol`;
        in a Python file that parses, the symbol is first read as the definition it names).
        A dirty mark must name an existing file of the workspace (`checked_marks`); for a
        Python file that parses, its symbol is replaced by the definition's dotted name, and a
        symbol that names no definition becomes a mark on the whole file. A dirty mark on a
        spot that is already open refreshes its reason.

        Args:
            dirty: The answer's `marks_dirty`.
            clean: The answer's `marks_clean`.
            by: Name of the checker.
            edited: The workspace-relative paths the step edits; empty for a command.

        Returns:
            What was opened, closed and ignored, and whether a review is required now (it is
            as soon as more than `max_open_marks` marks are open).
        """
        changes = MarkChanges()
        kept, rejected = checked_marks(clean, self.workspace, must_exist=False)
        changes.ignored += rejected
        for entry in kept:
            if self.review_required:
                changes.ignored.append(
                    f"{entry.label()}: a review is required, so a clean mark is ignored"
                )
                continue
            if entry.path not in edited:
                changes.ignored.append(f"{entry.label()}: the step does not edit that file")
                continue
            standing = {mark.symbol: mark for mark in self.open_marks if mark.path == entry.path}
            wanted = normal_symbol(entry.symbol)
            names = self._definitions(entry.path) if wanted else None
            named = match_symbol(wanted, names) if names is not None else None
            if named is not None:
                # In a Python file the name is read as the definition it names, so the bare
                # `run` means the function `run` where both it and `Parser.run` exist.
                found = named if named in standing else None
            else:
                found = match_symbol(wanted, list(standing))
            if found is None:
                changes.ignored.append(f"{entry.label()}: no open mark matches")
                continue
            changes.closed.append(self._close(standing[found], by or "a checker"))
        kept, rejected = checked_marks(dirty, self.workspace, must_exist=True)
        changes.ignored += rejected
        for entry in kept:
            changes.opened.append(self._open(entry.path, entry.symbol, entry.reason, by))
        self._check_threshold()
        changes.review_required = self.review_required
        return changes

    def clear(self, mark_id: int, by: str) -> HonestyMark | None:
        """Close one open mark by hand: a person's override of a checker.

        Whether a review is required does not change.

        Args:
            mark_id: The mark's id, as the session reported it.
            by: Who closed it.

        Returns:
            The closed mark, or None if no open mark has that id.
        """
        for mark in self.open_marks:
            if mark.id == mark_id:
                return self._close(mark, by)
        return None

    def close_stale(self) -> list[HonestyMark]:
        """Close the open marks whose code is gone (section 3).

        A mark is closed when its file no longer exists, or when it names a symbol of a
        Python file that parses and no longer defines it. A file that does not parse keeps
        its marks.

        Returns:
            The marks that were closed.
        """
        closed: list[HonestyMark] = []
        for mark in self.open_marks:
            if not (self.workspace / mark.path).is_file():
                closed.append(self._close(mark, "calu: the file is gone"))
                continue
            if not mark.symbol:
                continue
            names = self._definitions(mark.path)
            if names is not None and mark.symbol not in names:
                closed.append(self._close(mark, "calu: the symbol is gone"))
        return closed

    def _definitions(self, path: str) -> list[str] | None:
        """List the definitions of a Python file of the workspace.

        Args:
            path: The file, relative to the workspace.

        Returns:
            The dotted names; None if the file is no Python file, cannot be read or does not
            parse (`calu.checkers.symbols.definition_names`).
        """
        if not path.endswith((".py", ".pyi")):
            return None
        try:
            source = (self.workspace / path).read_text(encoding="utf-8")
        except OSError, ValueError:
            return None
        return definition_names(source, path)

    def _open(self, path: str, symbol: str, reason: str, by: str) -> HonestyMark:
        """Open a mark, or refresh the open mark of the same spot.

        Args:
            path: The file, relative to the workspace.
            symbol: The symbol as the checker or the review wrote it.
            reason: Why the spot is dishonest.
            by: Who found it.

        Returns:
            The open mark. For a Python file that parses, its symbol is the definition's
            dotted name; a symbol that names no single definition gives a mark on the whole
            file whose reason starts with the symbol as written.
        """
        wanted = normal_symbol(symbol)
        names = self._definitions(path) if wanted else None
        if wanted and names is not None:
            found = match_symbol(wanted, names)
            if found is None:
                reason = f"{wanted}: {reason}"
            wanted = found or ""
        for index, mark in enumerate(self.marks):
            if mark.open and mark.path == path and mark.symbol == wanted:
                self.marks[index] = replace(mark, reason=reason, opened_by=by or None)
                return self.marks[index]
        mark = HonestyMark(
            id=self._next_id,
            path=path,
            symbol=wanted,
            reason=reason,
            opened_at=datetime.now(UTC),
            opened_by=by or None,
        )
        self._next_id += 1
        self.marks.append(mark)
        return mark

    def _close(self, mark: HonestyMark, by: str) -> HonestyMark:
        """Close one mark of the state.

        Args:
            mark: A mark of `self.marks`.
            by: Who closes it.

        Returns:
            The closed mark, which replaces `mark` in `self.marks`.
        """
        index = self.marks.index(mark)
        self.marks[index] = replace(mark, closed_at=datetime.now(UTC), closed_by=by)
        return self.marks[index]

    def _check_threshold(self) -> None:
        """Require a review if more than `max_open_marks` marks are open."""
        if len(self.open_marks) > self.max_open_marks:
            self.review_required = True

    # ------------------------------------------------------------------ computed state

    def unreviewed(self) -> Unreviewed:
        """Compute the files whose present content nobody verified.

        Looked at are the files git lists for the repository (tracked files and untracked
        files that git does not ignore) and the watched files. Not looked at: other ignored
        files, other files outside the repository, deleted files, and the inside of
        submodules.

        Returns:
            The unreviewed files. `everything` is set when there is no baseline, and when git
            could not list the files (then with `problem`).
        """
        if self.baseline is None:
            return Unreviewed(everything=True)
        try:
            top = self._repository()
            base = self._tree(self.baseline.commit_id)
            output = run_git(top, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        except GitError as exc:
            return Unreviewed(everything=True, problem=str(exc))
        found: set[str] = set()
        listed: set[str] = set()
        for raw in set(output.split(b"\0")) - {b""}:
            inside = os.fsdecode(raw).rstrip("/")
            entry = base.get(inside)
            key = self.key(top / inside)
            listed.add(key)
            try:
                status = (top / inside).lstat()
            except OSError:
                continue  # deleted: nothing is left to review
            if stat.S_ISDIR(status.st_mode):
                # A submodule the baseline records is not looked into; any other directory
                # git names as one entry is a repository of its own that nobody reviewed.
                if entry is None or entry[0] != GITLINK_MODE:
                    found.add(key)
                continue
            ids = self._content(key)
            if ids is None:
                found.add(key)
            elif entry is not None and entry[1] == ids.blob:
                continue
            elif not self._verified_content(key, ids):
                found.add(key)
        for key in self.watched - listed:
            if not os.path.lexists(self.workspace / key):
                continue
            ids = self._content(key)
            if ids is None or not self._verified_content(key, ids):
                found.add(key)
        return Unreviewed(paths=tuple(sorted(found)))

    def snapshot(self) -> HonestySnapshot:
        """Compute the state as it is now.

        Stale marks are not closed here; call `close_stale` first.

        Returns:
            The baseline, the open marks, the unreviewed files and whether a review is
            required.
        """
        return HonestySnapshot(
            baseline=self.baseline,
            marks=tuple(self.open_marks),
            unreviewed=self.unreviewed(),
            review_required=self.review_required,
        )

    def uncertified(self, commit_id: str) -> str:
        """Say why a commit must not be certified as trusted, if anything speaks against it.

        calu certifies a commit by what the commit records, not by the working tree: every
        file the commit holds must have the content the baseline holds for that path, or the
        content of the working file with that content verified. Files the commit deletes and
        submodule entries are not looked at.

        Args:
            commit_id: The full id of the commit, usually the new HEAD after a `git commit`.

        Returns:
            "" if the commit may be certified. Otherwise one sentence: there is no baseline,
            marks are open, a review is required, git could not list the commit, or which
            files of the commit are not verified.
        """
        if self.baseline is None:
            return "there is no baseline"
        if self.open_marks:
            return f"{len(self.open_marks)} mark(s) are open"
        if self.review_required:
            return "an honesty review is required"
        if not HEAD_ID.fullmatch(commit_id):
            return "the commit id is not a full commit id"
        try:
            top = self._repository()
            base = self._tree(self.baseline.commit_id)
            recorded = self._tree(commit_id)
        except GitError as exc:
            return f"git could not list the commit ({exc})"
        failing: list[str] = []
        for inside, (mode, object_id) in recorded.items():
            if mode == GITLINK_MODE:
                continue
            entry = base.get(inside)
            if entry is not None and entry[1] == object_id:
                continue
            key = self.key(top / inside)
            ids = self._content(key)
            if ids is None or ids.blob != object_id or not self._verified_content(key, ids):
                failing.append(key)
        if failing:
            failing.sort()
            rest = f" and {len(failing) - 5} more" if len(failing) > 5 else ""
            return (
                f"{len(failing)} file(s) of the commit are not verified: "
                f"{', '.join(failing[:5])}{rest}"
            )
        return ""
