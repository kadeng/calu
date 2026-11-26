"""Code honesty: which code was verified to say what it does (specification of 2026-11-12).

Code is honest when its names and documentation say what it does (the honesty rules,
`calu/defaults/checkers/honesty-rules.md`). Checkers and people judge call sites and commands
by names and documentation, which is sound only for code that was verified. calu therefore
tracks which code is verified:

    trusted commit   a commit a person or calu certified (database table `trusted_commits`)
    source           who established a trust: user > review > check (`calu.store.trust`);
                     `honesty.sufficient_trust` names the weakest source that counts
    baseline         the nearest ancestor of HEAD that is trusted by a sufficient source
                     (`find_baseline`)
    mark             a function, method, class or file a checker found dishonest
                     (`HonestyMark`); a file is dishonest while it has an open mark
    HonestyState     per session, in memory: the marks, the verified contents (path ->
                     SHA-256 and source), the stored trust of file contents it was given,
                     the files to watch that git does not list, "review required"
    staged content   what the index holds for a path, which a commit records; it counts
                     like the working file (`HonestyState.unreviewed`)
    HonestySnapshot  the state at one moment: baseline, open marks, unreviewed files,
                     review required; `clean` and the note for the checkers are computed
                     from it
    honesty review   a reviewer agent judges the unreviewed and the marked files
                     (`calu.checkers.honesty_review`); the state computes what it reads
                     (`HonestyState.review_plan`: the diff against the baseline or the whole
                     file, each bound to the SHA-256 of the content it was made from) and
                     applies its result (`apply_current_review`)

`HonestyState` reads git and files and nothing else; the database is asked by its caller
(`find_baseline` takes the lookup as a function). The session (`calu.session`) applies a
checker's marks and verifications to the state after the tool call succeeded, and the gate
(`calu.gate`) decides from a snapshot whether a step may run while `honesty.mode` is
"enforce". Editing dishonest code is never blocked: fixing it is an edit.
"""

from __future__ import annotations

import ast
import difflib
import functools
import hashlib
import os
import re
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Concatenate, Literal

import anyio.to_thread

from calu import asuser
from calu.checkers import CodeMark
from calu.checkers.changeset import GIT_OPTIONS, HEAD_ID
from calu.checkers.model import ALL_SYMBOLS
from calu.checkers.symbols import definition_names
from calu.store.trust import TrustSource, stronger, sufficient, weakest


@dataclass(frozen=True)
class HonestyMark:
    """One mark of a session's honesty state: a spot a checker or a review found dishonest.

    `symbol` is empty when the whole file is meant. `closed_at` is None while the mark is open.
    """

    id: int
    path: str
    symbol: str
    reason: str
    opened_at: datetime
    opened_by: str | None = None
    """Name of the checker whose answer opened it, or "the honesty review"."""
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


def diff_paths(diff: str) -> set[str]:
    """Return the project-relative paths a `git diff` text touches.

    A commit names no file in its command, so the gate uses this to tell the checkers which
    open marks are on the files the commit records. Only the `diff --git a/x b/x` headers are
    read, and the `b/` side is taken, because that is the file as it will be after the commit
    (a rename's `a/` side no longer exists).

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


def checked_marks(
    marks: Sequence[CodeMark], workspace: Path, *, must_exist: bool
) -> tuple[list[CodeMark], list[str]]:
    """Keep the marks whose path calu may record, and name the rest.

    A mark is a field of a checker's structured answer: the model writes the path as text.
    calu records a mark only for a path that is relative to the project and has no `..`
    component. A mark that opens a spot must also name an existing file; marks are applied
    after the tool call succeeded, so a file the step created exists by then. A mark that
    clears a spot is checked for its shape only, because the file may have been deleted.

    Args:
        marks: The marks as the checker wrote them.
        workspace: The project root; paths are read relative to it.
        must_exist: Whether the file has to exist (True for dirty marks, False for clean ones).

    Returns:
        (the marks to use, one message per rejected mark saying which mark and why). A
        rejected mark is dropped and reported in the session's log.
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
        if must_exist and not (workspace / path).is_file():
            rejected.append(f"{mark.label()}: there is no such file in the project")
            continue
        kept.append(mark.model_copy(update={"path": str(candidate)}))
    return kept, rejected


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
"""The mode of a submodule entry in a git tree. calu does not look into submodules; it compares
the commit a submodule is at with the commit the baseline records for it."""

LINK_MODE = "120000"
"""The mode of a symbolic link in a git tree. Its content is the link's target, not the text of
a file, so an honesty review compares no file with it."""

READ_LIMIT = 8_000_000
"""The largest file, in bytes, that `read_regular` returns. A larger file is hashed in pieces
(`content_ids`), but an edit of it is not predicted, so it is unreviewed after the edit."""

NOTE_PATH_CHARS = 300
"""Characters of a path or symbol that the note for a checker shows before it cuts."""

NOTE_REASON_CHARS = 200
"""Characters of a mark's reason that the note for a checker shows before it cuts."""

SETTLE_NS = 2_000_000_000
"""A file's hashes are remembered with its size and times only when its last change is at
least this old, so that two writes within the clock's resolution are both seen."""

REVIEW_FILE_BYTES = 600_000
"""The default of `honesty.review_file_bytes`: the largest text, in bytes of UTF-8, that an
honesty review hands to its reviewer for one file (about 150,000 tokens): its diff, the whole
file, or a marked file's diff with its marked definitions. A file with a longer text is
skipped and stays unreviewed, so a person decides about it (`calu honesty trust --file`)."""

SMALL_FILE_BYTES = 30_000
"""A file with an open mark up to this size is shown whole; `HonestyState.review_plan` shows a
larger one as its diff with the present source of its marked definitions, where
`marked_excerpts` can cut them out."""

REVIEW_READ_BYTES = 8_000_000
"""The largest file, in bytes, that an honesty review reads to hash and to compare with the
baseline's content, and the largest baseline content it reads. A larger file is skipped like
one whose text for the reviewer is over `REVIEW_FILE_BYTES`."""

VERIFIED_TEXTS_BYTES = 64_000_000
"""The most bytes of file texts a session keeps for the diffs of later reviews
(`VerifiedTexts`)."""

DIFF_CONTEXT_LINES = 20
"""Lines of context around each change in a diff for the reviewer; `honesty.review_context_lines`
sets it per session."""

NO_CODE_EXTENSIONS = frozenset(
    {
        # plain text
        ".txt", ".text", ".log",
        # tabular data
        ".csv", ".tsv",
        # images (not SVG: it can hold scripts)
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
        # sound, video, fonts, documents
        ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".mov",
        ".ttf", ".otf", ".woff", ".woff2", ".pdf",
    }
)  # fmt: skip
"""File name extensions (compared in lower case) of the files that hold no code unit: the
honesty review verifies such a file without a model call (`holds_no_code`). Configuration and
structured data (`.json`, `.yaml`, `.toml`, `.ini`, `.env`, ...) are not listed: code may act on
them, and a configuration that widens what a unit does counts as code (the honesty rules,
"Configuration"). Serialised objects (`.pkl`, `.pt`, `.npy`) are not listed either: loading them
can run code. Markdown and other markup stay reviewed too: they can hold the documentation of a
unit, and Claude Code reads instructions from them."""

PROGRAM_MAGIC = (b"#!", b"\x7fELF", b"MZ", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\0asm")
"""The first bytes of a file that the system or a runtime runs as a program: a script with an
interpreter line, ELF, Windows PE, Java class or universal Mach-O, 64-bit Mach-O, WebAssembly.
A file that starts with one of them holds code, whatever its name."""

REVIEWER = "the honesty review"
"""Who opens and closes marks for an honesty review (`HonestyMark.opened_by`)."""

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
class Baseline(GitCommit):
    """The trusted commit a session compares its files with, and who established its trust."""

    source: TrustSource
    """The source the database records for the commit, or the strongest one recorded for its
    tree: "user", "review" or "check"."""


@dataclass(frozen=True)
class Verified:
    """One verified file content and who verified it."""

    sha256: str
    source: TrustSource
    """"check" for the result of an edit the step checkers judged, "review" for a content an
    honesty review read."""


@dataclass(frozen=True)
class Certification:
    """Whether a commit may be certified as trusted, and with which source."""

    problem: str
    """"" if the commit may be certified; otherwise one sentence saying why not."""
    source: TrustSource | None = None
    """The source to record: the weakest among the verifications of the files the commit
    changes and the baseline's own source. None when `problem` is set."""


def shown(text: str, limit: int) -> str:
    r"""Render untrusted text for one line of the note: no line break, bounded length.

    File names come from the working tree and reasons from a model's answer, so neither may
    add lines to a note that calu computes.

    Args:
        text: A path, a symbol or a reason.
        limit: The largest number of characters of `text` that are rendered.

    Returns:
        `text` with every character that is not printable (control characters, line and
        paragraph separators, format characters) written as a backslash escape, cut after
        `limit` characters with "..." appended.

    Example:
        shown("a" + chr(10) + "b", 10) == "a\\nb": the line break is written as a backslash
        and the letter n.
    """
    cut = text[:limit]
    safe = "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in cut
    )
    return safe + ("..." if len(text) > limit else "")


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
    restage: tuple[str, ...] = ()
    """The files of `paths` that are unreviewed only because of the index: the working file's
    content is reviewed (the baseline's, or verified), and the index holds another content
    for the path that nobody verified. Staging the file again (`git add <path>`) settles
    it, which the agent can do itself; no review can (a review reads working files)."""

    @property
    def only_restage(self) -> bool:
        """Tell whether staging files again is all that is missing.

        Returns:
            True if at least one file is unreviewed and every unreviewed file is in `restage`.
        """
        return bool(self.paths) and not self.everything and set(self.paths) <= set(self.restage)

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
    marks: tuple[int, ...] | None = None
    """The ids of the open marks on the file when the review read it; None if not recorded.
    `HonestyState.apply_current_review` drops a result whose file has other open marks now."""


type FileSeen = tuple[str, tuple[int, ...]]
"""What an honesty review saw of one file: (the SHA-256 of its content, or "" if it could not be
hashed; the ids of its open marks, sorted)."""


@dataclass(frozen=True)
class ReviewFile:
    """One file an honesty review hands to its reviewer agent."""

    path: str
    """The file as `HonestyState.key` names it."""
    sha256: str
    """SHA-256 of the content `text` was computed from."""
    text: str
    """The whole file if `whole`, else its unified diff against the earlier content `against`
    names."""
    whole: bool
    marks: tuple[HonestyMark, ...] = ()
    """The open marks on the file when the plan was made."""
    against: Literal["baseline", "verified"] = "baseline"
    """For a diff: whether it is the change since the baseline's content, or since the content
    last verified in this session (`VerifiedTexts`)."""
    excerpts: str = ""
    """For a diff of a file with open marks: the present source of the marked definitions
    (`marked_excerpts`), shown after the diff."""
    content: str = ""
    """For a diff: the whole present content, which a second round shows when the reviewer
    asks for it (`FileVerdict.needs_whole_file`); "" when `whole`."""


@dataclass(frozen=True)
class ReviewPlan:
    """What an honesty review has to do, computed from the state (`HonestyState.review_plan`)."""

    baseline: Baseline | None
    """The baseline the files are compared with; None reviews every file whole."""
    count: int = 0
    """How many files are unreviewed or carry an open mark."""
    problem: str = ""
    """Why no review can run: more files than the limit, or git could not list them. The
    other fields are then empty."""
    files: tuple[ReviewFile, ...] = ()
    """The files for the reviewer agent, sorted by path."""
    shortcuts: Mapping[str, FileReview] = field(default_factory=dict[str, FileReview])
    """Files verified without a model: their change against the baseline is whitespace only
    (`whitespace_only`), or they hold no code (`holds_no_code`, listed in `no_code`)."""
    no_code: tuple[str, ...] = ()
    """The files of `shortcuts` that are verified because they hold no code unit (plain text,
    data, images: `holds_no_code`), sorted."""
    generated: tuple[str, ...] = ()
    """The files of `shortcuts` that are verified because they are generated
    (`GeneratedRules`), sorted."""
    skipped: tuple[tuple[str, str], ...] = ()
    """(path, why) of each file the review cannot judge; it stays as it is: a file that is
    not readable text within the size limits, and, with a baseline, a file whose working
    content is reviewed while the index holds another content for it."""
    verified_already: int = 0
    """Only without a baseline: how many of the files git lists have no open mark and a
    content that is verified already. They are not part of the review and not in `count`."""
    seen: Mapping[str, FileSeen] = field(default_factory=dict[str, FileSeen])
    """Every file of `files`, `shortcuts` and `skipped` -> what the plan saw of it."""
    review_required: bool = False
    """Whether a review was required when the plan was made."""


@dataclass(frozen=True)
class Reviewed:
    """What the last honesty review looked at, kept by the session (`Session._run_review`).

    The session does not start a review by itself while `covers` is true for the new plan:
    that review would read what the last one read.
    """

    baseline: str
    """The commit id of the baseline after the review; "" if there was none."""
    review_required: bool
    """Whether a review was required after the review's result was applied."""
    seen: Mapping[str, FileSeen]
    """File -> the content the review judged and the file's open marks after its result was
    applied; for a file the review skipped, what the plan saw. A file whose result was dropped
    because it changed while the review ran is not in it."""

    def covers(self, plan: ReviewPlan) -> bool:
        """Tell whether the last review looked at everything a new review would read.

        Args:
            plan: The plan of the new review, without a `problem`.

        Returns:
            True if the baseline and "review required" are as they were after the last
            review, and every file of the plan was judged or skipped by the last review with
            the content and the open marks it has now. False for a file that changed since,
            that became unreviewed since, or whose result the last review dropped.
        """
        base = plan.baseline.commit_id if plan.baseline else ""
        if base != self.baseline or plan.review_required != self.review_required:
            return False
        return all(self.seen.get(key) == seen for key, seen in plan.seen.items())


@dataclass
class MarkChanges:
    """What one call changed in the marks of a `HonestyState`."""

    opened: list[HonestyMark] = field(default_factory=list)
    """Marks that were opened, or whose reason was refreshed."""
    closed: list[HonestyMark] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    """One sentence per mark of the answer that was not applied, saying which and why. The
    session logs them and tells the checker."""
    dropped: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """Only from `HonestyState.apply_current_review`: (path, why) of each result of an honesty
    review that was not applied because the file or its marks changed while the review ran.
    Such a file is as it was; the next review reads it."""
    review_required: bool = False
    """Whether a review is required after the call."""
    withdrawn: list[str] = field(default_factory=list)
    """SHA-256 of each file content on which a mark was opened. The session withdraws the
    stored trust of these contents (`SessionStore.distrust_file`), so that a content a checker
    or a review found dishonest is not verified after a restart, when the marks are gone."""


@dataclass(frozen=True)
class HonestySnapshot:
    """The honesty state of a session at one moment (specification section 2)."""

    baseline: Baseline | None
    """The baseline with the source of its trust; None when there is none or its source is
    weaker than `honesty.sufficient_trust`."""
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
        fixed instruction part `system/marks.md`. Paths, symbols and reasons are rendered with
        `shown`, so the note has one line per item below whatever they contain.

        Args:
            own_paths: The workspace-relative paths of the step being judged; the marks on
                them are named first and all of them.
            limit: How many unreviewed files, and how many marks on other files, are named
                before the rest is counted.

        Returns:
            Four to six lines: the heading, the baseline (short id and source, or that there
            is none), the open marks with their reasons (one line for this step's files, one
            for other files, or one saying there are none), the unreviewed files and their
            number (left out without a baseline), and whether a review is required.

        Example:
            Code honesty state, computed by calu.
            Baseline: trusted commit 1a2b3c4d5e6f (trust established by: user).
            Open marks on this step's files: src/parser.py:fields (returns unsorted).
            Unreviewed files (2): scripts/gen.py, src/new.py.
            Review required: no.
        """

        def spot(mark: HonestyMark) -> str:
            """Render one mark.

            Args:
                mark: An open mark.

            Returns:
                "path:symbol (reason)", each part rendered with `shown`.
            """
            where = shown(mark.path, NOTE_PATH_CHARS)
            if mark.symbol:
                where += ":" + shown(mark.symbol, NOTE_PATH_CHARS)
            return f"{where} ({shown(mark.reason, NOTE_REASON_CHARS)})"

        lines = ["Code honesty state, computed by calu."]
        if self.baseline is None:
            lines.append(
                "Baseline: none. No ancestor of HEAD is a trusted commit, so every file counts "
                "as unreviewed."
            )
        else:
            lines.append(
                f"Baseline: trusted commit {self.baseline.short} "
                f"(trust established by: {self.baseline.source})."
            )
        own = [mark for mark in self.marks if mark.path in own_paths]
        others = [mark for mark in self.marks if mark.path not in own_paths]
        if own:
            lines.append(f"Open marks on this step's files: {'; '.join(map(spot, own))}.")
        if others:
            seen = others[:limit]
            rest = f"; and {len(others) - len(seen)} more" if len(others) > len(seen) else ""
            lines.append(f"Open marks on other files: {'; '.join(map(spot, seen))}{rest}.")
        if not self.marks:
            lines.append("Open marks: none.")
        if self.unreviewed.everything:
            if self.baseline is not None:
                problem = shown(self.unreviewed.problem, NOTE_PATH_CHARS) or "git did not answer"
                lines.append(
                    "Unreviewed files: all (the files could not be compared with the "
                    f"baseline: {problem})."
                )
        elif self.unreviewed.paths:
            paths = self.unreviewed.paths
            rest = f", and {len(paths) - limit} more" if len(paths) > limit else ""
            names = ", ".join(shown(path, NOTE_PATH_CHARS) for path in paths[:limit])
            lines.append(f"Unreviewed files ({len(paths)}): {names}{rest}.")
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
    git reads every object as the repository stores it under its own id: object replacement
    (`refs/replace`) and the grafts file are switched off, so the tree git names for a commit
    id is the tree of that commit.
    The honesty state uses commands that list names and ids (`rev-parse`, `rev-list`,
    `ls-tree`, `ls-files`) and reads the baseline's content of a file for an honesty review
    (`cat-file`); calu reads and hashes the working files itself.

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
    environment = {
        **os.environ,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": os.devnull,
    }
    try:
        done = subprocess.run(
            ["git", *GIT_OPTIONS, *arguments],
            cwd=cwd,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            **asuser.child(environment),
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
    commits: Sequence[GitCommit],
    trusted_commits: Mapping[str, TrustSource],
    trusted_trees: Mapping[str, TrustSource],
    minimum: TrustSource = "check",
) -> Baseline | None:
    """Pick the baseline out of a list of ancestors.

    Args:
        commits: A commit and its ancestors, nearest first (`ancestors`).
        trusted_commits: Commit id -> the source of its trust, for the commits among them
            that the database holds as trusted.
        trusted_trees: Tree id -> the strongest source among the trusted commits that record
            that tree.
        minimum: `honesty.sufficient_trust`: the weakest source that counts.

    Returns:
        The first commit whose commit id or tree id is trusted by a source at least as strong
        as `minimum`, with the stronger of its two sources; None if there is none. A commit
        whose recorded source is weaker than `minimum` is passed over like an untrusted one.
    """
    for commit in commits:
        source = stronger(trusted_commits.get(commit.commit_id), trusted_trees.get(commit.tree_id))
        if source is not None and sufficient(source, minimum):
            return Baseline(commit.commit_id, commit.tree_id, source)
    return None


type TrustLookup = Callable[
    [Sequence[str], Sequence[str]],
    Awaitable[tuple[Mapping[str, TrustSource], Mapping[str, TrustSource]]],
]
"""Asks the database which of some commit ids and tree ids are trusted
(`SessionStore.trusted_among`): (commit ids, tree ids) -> (commit id -> source for the trusted
ones, tree id -> strongest source for the trees a trusted commit records)."""


async def find_baseline(
    workspace: Path,
    lookup: TrustLookup,
    limit: int = BASELINE_SEARCH_LIMIT,
    minimum: TrustSource = "check",
) -> Baseline | None:
    """Find the baseline of a session: the nearest trusted ancestor of HEAD, HEAD included.

    The search is bounded: only the last `limit` commits (default `BASELINE_SEARCH_LIMIT`,
    5000) are read, in the order of `git rev-list`. A trusted commit further back is not
    found. One database question is asked for all of them.

    Args:
        workspace: The project directory.
        lookup: Asks the database which ids are trusted (`TrustLookup`).
        limit: The largest number of commits to look at.
        minimum: `honesty.sufficient_trust`: a commit trusted by a weaker source is no
            baseline.

    Returns:
        The baseline with the source of its trust, or None when the directory is in no git
        repository, HEAD has no commit, git could not be asked, or none of the commits read is
        trusted by a sufficient source.

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
    return nearest_trusted(commits, trusted_commits, trusted_trees, minimum)


def _open_regular(path: Path) -> tuple[int, os.stat_result] | None:
    """Open a regular file for reading without waiting and without following a link.

    Args:
        path: The file.

    Returns:
        (the open descriptor, which the caller closes; its status). None if the path is
        missing, is not a regular file, or cannot be opened.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        descriptor = os.open(path, flags)
    except OSError, ValueError:
        return None
    try:
        status = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        return None
    return descriptor, status


def read_regular(path: Path, limit: int = READ_LIMIT) -> bytes | None:
    """Read the bytes of a regular file, without following a link and without waiting.

    Args:
        path: The file.
        limit: The largest size in bytes that is read.

    Returns:
        The content; None if the path is missing, is not a regular file (a symbolic link, a
        directory, a pipe, a device), is larger than `limit`, cannot be read, or changed in
        size while it was read.
    """
    opened = _open_regular(path)
    if opened is None:
        return None
    descriptor, status = opened
    try:
        if status.st_size > limit:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return data if len(data) == status.st_size else None


def hash_bytes(data: bytes, object_format: str = "sha1") -> ContentIds:
    """Hash a content that is held in memory.

    Args:
        data: The content of a file.
        object_format: The hash of the repository's object ids, "sha1" or "sha256".

    Returns:
        Both hashes, equal to what `content_ids` returns for a file with that content.
    """
    blob = hashlib.new(object_format, b"blob %d\0" % len(data), usedforsecurity=False)
    blob.update(data)
    return ContentIds(hashlib.sha256(data).hexdigest(), blob.hexdigest())


def expected_edit(
    tool_name: str, tool_input: Mapping[str, Any], before: bytes | None
) -> bytes | None:
    """Compute the content a file has after an Edit, Write or MultiEdit, from the tool's input.

    The edit checkers judge this content: the whole of it for a Write, the replacements for an
    Edit or MultiEdit. calu verifies a file after an edit only at exactly this content
    (`HonestyState.expect_edit`, `HonestyState.record_edit`).

    The prediction is exact for UTF-8 text that the tool writes back unchanged outside the
    replacements. Where the tool writes other bytes (another encoding, converted line
    endings), the file's content after the edit differs from the prediction and the file is
    unreviewed.

    Args:
        tool_name: "Write", "Edit" or "MultiEdit".
        tool_input: The tool's input: `content` for Write; `old_string`, `new_string` and
            optionally `replace_all` for Edit; `edits` (a list of such objects) for MultiEdit.
        before: The file's bytes before the edit; None if there is no file.

    Returns:
        The expected bytes. None if the tool is another one, the input does not have that
        shape, the present content is not UTF-8, or a replacement does not apply (its
        `old_string` does not occur; an empty `old_string` applies only to a missing or empty
        file).
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        return content.encode("utf-8", "surrogatepass") if isinstance(content, str) else None
    if tool_name == "Edit":
        edits: object = [tool_input]
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
    else:
        return None
    if not isinstance(edits, list) or not edits:
        return None
    try:
        text = (before or b"").decode("utf-8")
    except UnicodeDecodeError:
        return None
    for edit in edits:
        if not isinstance(edit, Mapping):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if not old:
            if text:
                return None
            text = new
        elif old not in text:
            return None
        else:
            text = (
                text.replace(old, new)
                if edit.get("replace_all") is True
                else text.replace(old, new, 1)
            )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def content_ids(path: Path, object_format: str = "sha1") -> ContentIds | None:
    """Hash the content of one file, without following a symbolic link and without waiting.

    The type of the path is read before anything is opened, and a regular file is opened
    without blocking, so a pipe or a device at the path is answered with None at once.

    Args:
        path: The file. For a symbolic link the content is the link's target path, which is
            also what git records of it.
        object_format: The hash of the repository's object ids, "sha1" or "sha256".

    Returns:
        Both hashes; None if the path is missing, is neither a regular file nor a symbolic
        link, could not be read, or changed in size while it was read.
    """
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return hash_bytes(os.fsencode(os.readlink(path)), object_format)
    except OSError, ValueError:
        return None
    opened = _open_regular(path)
    if opened is None:
        return None
    descriptor, status = opened
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            plain = hashlib.sha256()
            blob = hashlib.new(object_format, b"blob %d\0" % status.st_size, usedforsecurity=False)
            read = 0
            while chunk := handle.read(1 << 20):
                read += len(chunk)
                plain.update(chunk)
                blob.update(chunk)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if read != status.st_size:
        return None
    return ContentIds(plain.hexdigest(), blob.hexdigest())


def holds_no_code(name: str, head: bytes, mode: int) -> bool:
    """Tell whether a file holds no code unit, so that an honesty review verifies it unread.

    A file holds no code when all three hold:

        its name ends in one of `NO_CODE_EXTENSIONS` (case ignored)   e.g. `out.txt`, `logo.PNG`
        no execute permission bit is set in `mode`
        its content does not start with one of `PROGRAM_MAGIC`      no `#!`, no ELF, ...

    So `notes.txt` holds no code, `run.txt` with the execute bit or starting with `#!/bin/sh`
    does, and `settings.json` does (configuration stays reviewed).

    Args:
        name: The file's name or path; only its extension is read.
        head: The first bytes of its content (at least 4 where the file has them).
        mode: Its `st_mode`.

    Returns:
        True if the file holds no code.
    """
    if PurePosixPath(name).suffix.lower() not in NO_CODE_EXTENSIONS:
        return False
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return False
    return not head.startswith(PROGRAM_MAGIC)


def no_code_sha256(path: Path) -> str | None:
    """Hash a file of any size that holds no code (`holds_no_code`), from one read.

    The check and the hash read the same open file, so the hash is of the content that was
    found to hold no code.

    Args:
        path: The file; a symbolic link or anything but a regular file is not followed.

    Returns:
        The SHA-256 of its content; None if it holds code, is not a regular file, cannot be
        read, or changed in size while it was read.
    """
    if path.suffix.lower() not in NO_CODE_EXTENSIONS:
        return None
    opened = _open_regular(path)
    if opened is None:
        return None
    descriptor, status = opened
    digest = hashlib.sha256()
    read = 0
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            head = handle.read(8)
            if not holds_no_code(path.name, head, status.st_mode):
                return None
            digest.update(head)
            read = len(head)
            while chunk := handle.read(1 << 20):
                read += len(chunk)
                digest.update(chunk)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest() if read == status.st_size else None


def whitespace_only(before: str, after: str) -> bool:
    r"""Tell whether two texts differ in nothing but trailing whitespace and blank lines.

    An honesty review verifies such a change without a model. Compared are the lines of both
    texts after the whitespace at each line's end (line endings included) is removed and
    empty lines are left out. Whitespace at the start of a line or inside it is compared as
    written, because indentation can decide what code does.

    Args:
        before: The content the baseline records.
        after: The present content.

    Returns:
        True if the remaining lines are the same in both texts.

    Example:
        whitespace_only("a = 1  \r\n\nb = 2", "a = 1\nb = 2\n") is True;
        whitespace_only("if x:\n    y()", "if x:\ny()") is False.
    """

    def kept(text: str) -> list[str]:
        """Return the lines that are compared.

        Args:
            text: One of the two texts.

        Returns:
            The lines without trailing whitespace, empty ones left out.
        """
        return [line.rstrip() for line in text.split("\n") if line.strip()]

    return kept(before) == kept(after)


def change_diff(before: str, after: str, path: str, context: int = 3) -> str:
    """Render the change of one file as a unified diff.

    Args:
        before: The earlier content: the one last verified, or the one the baseline records.
        after: The present content.
        path: The file's name for the two header lines.
        context: Lines of context around each change.

    Returns:
        The diff as text; "" if the two contents have the same lines.
    """
    name = shown(path, NOTE_PATH_CHARS)
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        f"a/{name}",
        f"b/{name}",
        lineterm="",
        n=max(context, 0),
    )
    return "\n".join(lines)


GENERATED_HEAD_BYTES = 8192
"""How much of a file's start `GeneratedRules.reason` matches the content patterns against."""


@dataclass(frozen=True)
class GeneratedRules:
    """Which files count as generated: `honesty.generated_paths` and `generated_contents`.

    Both lists are empty by default, so nothing is generated until a person configures it.
    A generated file is not judged for honesty: an honesty review verifies it without a
    model, and a mark on it is not recorded. A hand edit of one escalates
    (`calu.gate.StepGate`), so that only a build writes it without a person's say.
    """

    paths: tuple[re.Pattern[str], ...] = ()
    """Patterns searched (`re.search`) in the path relative to the project directory."""
    contents: tuple[re.Pattern[str], ...] = ()
    """Patterns searched in the first `GENERATED_HEAD_BYTES` of the file, decoded as UTF-8
    with replacement characters; anchor with `\\A` for "the file starts with"."""

    @classmethod
    def of(cls, paths: Sequence[str], contents: Sequence[str]) -> GeneratedRules:
        """Compile the two settings.

        Args:
            paths: `honesty.generated_paths`.
            contents: `honesty.generated_contents`.

        Returns:
            The rules.

        Raises:
            re.error: If a pattern does not compile (the configuration checks them first).
        """
        return cls(
            tuple(re.compile(one) for one in paths), tuple(re.compile(one) for one in contents)
        )

    def reason(self, key: str, head: bytes | str | None) -> str:
        """Tell whether a file counts as generated, and by which pattern.

        Args:
            key: The file's path relative to the project directory, with "/".
            head: The start of its content (only the first `GENERATED_HEAD_BYTES` are
                looked at), or None when it is not known; then only the paths count.

        Returns:
            E.g. "honesty.generated_paths `^dist/`"; "" if the file is not generated.
        """
        for pattern in self.paths:
            if pattern.search(key):
                return f"honesty.generated_paths `{pattern.pattern}`"
        if head is None or not self.contents:
            return ""
        if isinstance(head, bytes):
            head = head[:GENERATED_HEAD_BYTES].decode("utf-8", errors="replace")
        text = head[:GENERATED_HEAD_BYTES]
        for pattern in self.contents:
            if pattern.search(text):
                return f"honesty.generated_contents `{pattern.pattern}`"
        return ""

    def file_reason(self, workspace: Path, key: str) -> str:
        """Tell whether a file of the workspace counts as generated, reading its start.

        Args:
            workspace: The project directory.
            key: The file, relative to it.

        Returns:
            As `reason`; a file that cannot be read is judged by its path alone.
        """
        if not self.paths and not self.contents:
            return ""
        head: bytes | None = None
        if self.contents:
            with suppress(OSError):
                with (workspace / key).open("rb") as handle:
                    head = handle.read(GENERATED_HEAD_BYTES)
        return self.reason(key, head)


def marked_excerpts(text: str, path: str, symbols: Sequence[str]) -> str | None:
    """Cut the present source of a file's marked definitions out of it, for the reviewer.

    Only Python is understood: the file must parse, and every symbol must name a function or
    class by its dotted name (`Parser.fields`; a trailing "()" is ignored). Each excerpt is
    the definition with its decorators, headed by its line numbers.

    Args:
        text: The file's present content.
        path: Its path; only a `.py` file gives excerpts.
        symbols: The symbols of the file's open marks; "" stands for the whole file.

    Returns:
        The excerpts, one after the other; None if the file is not Python, does not parse, a
        mark is on the whole file, or a symbol names no definition (then the whole file is to
        be shown).
    """
    if not path.endswith(".py") or not symbols:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError, ValueError:
        return None
    found: dict[str, ast.AST] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        """Collect the functions and classes below `node` by their dotted names.

        Args:
            node: A module, class or function node.
            prefix: The dotted name of `node`, "" for the module.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                found.setdefault(name, child)
                visit(child, name)

    visit(tree, "")
    lines = text.splitlines()
    parts: list[str] = []
    for symbol in dict.fromkeys(normal_symbol(one) for one in symbols):
        node = found.get(symbol)
        if not symbol or node is None:
            return None
        decorators = getattr(node, "decorator_list", [])
        first = min([node.lineno, *(one.lineno for one in decorators)])  # type: ignore[attr-defined]
        last = node.end_lineno or node.lineno  # type: ignore[attr-defined]
        parts.append(f"# lines {first}-{last}: {symbol}\n" + "\n".join(lines[first - 1 : last]))
    return "\n\n".join(parts)


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


class HonestyShared:
    """What the honesty states of one session's checkouts share (D251).

    A session keeps one `HonestyState` per checkout (the main checkout and each git worktree,
    `calu.checkouts`). What is a fact about a file **content** and not about a place is shared
    by all of them, so that a content verified in one worktree counts in another and in the
    main checkout (a merge, a copy):

        lock            one reentrant lock for all states: calls of any of them run one after
                        the other
        trusted_hashes  SHA-256 -> source, the stored trust of contents (`trusted_files`) and
                        what this session verified since
        verified_blobs  git blob id -> source, for every content verified in this session
        texts           SHA-256 -> text of contents this session read, so that a review can
                        show the change since the content last verified (`VerifiedTexts`)
        next_id         the id the next mark gets, unique over all states
    """

    def __init__(self) -> None:
        """Create the shared part of a session that has verified nothing yet."""
        self.lock = threading.RLock()
        self.trusted_hashes: Mapping[str, TrustSource] = {}
        self.verified_blobs: dict[str, TrustSource] = {}
        self.texts = VerifiedTexts()
        self.next_id = 1


class VerifiedTexts:
    """The texts of file contents a session read, by SHA-256, the oldest dropped first.

    An honesty review shows the reviewer a file's change since the content last verified in
    this session, when that content's text is here; otherwise the change since the baseline
    (`HonestyState.review_plan`). Without it, every review after a small edit of a large file
    would show all of the file's changes since the last trusted commit again. The texts are in
    memory only: after a restart the baseline is used.

    The texts are put in when a checked edit is recorded (`record_edit`) and when a review
    plan reads a file; a text counts only when it is looked up by the SHA-256 of a content that
    was verified, so a text kept for a content nobody verified is never used.
    """

    def __init__(self, max_bytes: int = VERIFIED_TEXTS_BYTES) -> None:
        """Create an empty store.

        Args:
            max_bytes: The most UTF-8 bytes of text kept together; the texts used least
                recently are dropped beyond it, and a single text longer than it is not kept.
        """
        self.max_bytes = max_bytes
        self._texts: OrderedDict[str, str] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total = 0

    def put(self, sha256: str, text: str) -> None:
        """Keep the text of a content.

        Args:
            sha256: The SHA-256 of the content (of its bytes as read, `hash_bytes`).
            text: The content, decoded as UTF-8.
        """
        size = len(text.encode("utf-8"))
        if size > self.max_bytes:
            return
        if sha256 in self._texts:
            self._texts.move_to_end(sha256)
            return
        self._texts[sha256] = text
        self._sizes[sha256] = size
        self._total += size
        while self._total > self.max_bytes:
            oldest, _ = self._texts.popitem(last=False)
            self._total -= self._sizes.pop(oldest)

    def get(self, sha256: str) -> str | None:
        """Return the text of a content, if it is kept.

        Args:
            sha256: The SHA-256 of the content.

        Returns:
            The text; None if it was never put in or has been dropped.
        """
        text = self._texts.get(sha256)
        if text is not None:
            self._texts.move_to_end(sha256)
        return text


def combine_snapshots(
    snapshots: Sequence[HonestySnapshot], labels: Sequence[str] | None = None
) -> HonestySnapshot:
    """Combine the honesty states of several checkouts into one (D251).

    A step that runs in more than one checkout (`cd .claude/worktrees/b && pytest` from the
    main checkout) needs each of them clean; the clients get one state for the whole session.
    The result is clean exactly if every part is.

    Args:
        snapshots: One snapshot per checkout, the step's own checkout (or the main checkout)
            first; at least one.
        labels: For the overview of the whole session: the checkouts' labels in the order of
            `snapshots`. None combines for a step.

    Returns:
        The first snapshot itself if there is one. Otherwise the open marks of all parts by id,
        "review required" if any part requires one, and:

            for a step       the first part's baseline if every part has one, else None;
                             every part's unreviewed files (`everything` if any part has it,
                             with the parts' problems joined)
            for an overview  the first part's baseline and `everything`; a later part
                             without a baseline, or whose files git could not list, is
                             named by its label among the unreviewed files
    """
    if len(snapshots) == 1:
        return snapshots[0]
    bases = [snapshot.baseline for snapshot in snapshots]
    marks = sorted((mark for snapshot in snapshots for mark in snapshot.marks), key=lambda m: m.id)
    parts = [snapshot.unreviewed for snapshot in snapshots]
    if labels is None and any(part.everything for part in parts):
        problem = "; ".join(part.problem for part in parts if part.problem)
        unreviewed = Unreviewed(everything=True, problem=problem)
        base = None
    elif labels is not None and parts[0].everything:
        unreviewed, base = parts[0], bases[0]
    else:
        whole = (
            {labels[index] for index, part in enumerate(parts) if part.everything}
            if labels is not None
            else set[str]()
        )
        unreviewed = Unreviewed(
            paths=tuple(sorted({path for part in parts for path in part.paths} | whole)),
            restage=tuple(sorted({path for part in parts for path in part.restage})),
        )
        complete = all(one is not None for one in bases)
        base = bases[0] if labels is not None or complete else None
    return HonestySnapshot(
        baseline=base,
        marks=tuple(marks),
        unreviewed=unreviewed,
        review_required=any(snapshot.review_required for snapshot in snapshots),
    )


def merge_plans(plans: Sequence[ReviewPlan]) -> ReviewPlan:
    """Combine the review plans of several checkouts into one honesty review (D251).

    Args:
        plans: One plan per checkout, the step's own checkout first; at least one.

    Returns:
        The first plan itself if there is one. Otherwise one plan with the first plan's
        baseline if every plan has one (else None), every file of every plan sorted by path,
        the counts added, and "review required" if any plan required one. If a plan has a
        `problem`, the result has only that problem and the total count.
    """
    if len(plans) == 1:
        return plans[0]
    bases = [plan.baseline for plan in plans]
    base = bases[0] if all(one is not None for one in bases) else None
    count = sum(plan.count for plan in plans)
    problem = next((plan.problem for plan in plans if plan.problem), "")
    if problem:
        return ReviewPlan(base, count=count, problem=problem)
    return ReviewPlan(
        base,
        count=count,
        files=tuple(sorted((one for plan in plans for one in plan.files), key=lambda f: f.path)),
        shortcuts={key: value for plan in plans for key, value in plan.shortcuts.items()},
        no_code=tuple(sorted(one for plan in plans for one in plan.no_code)),
        generated=tuple(sorted(one for plan in plans for one in plan.generated)),
        skipped=tuple(one for plan in plans for one in plan.skipped),
        verified_already=sum(plan.verified_already for plan in plans),
        seen={key: value for plan in plans for key, value in plan.seen.items()},
        review_required=any(plan.review_required for plan in plans),
    )


def _locked[**P, R](
    method: Callable[Concatenate[HonestyState, P], R],
) -> Callable[Concatenate[HonestyState, P], R]:
    """Make a method of `HonestyState` run while it holds the state's lock.

    Args:
        method: The method.

    Returns:
        A method with the same contract that takes the state's reentrant lock for the whole
        call, so that calls from several threads run one after the other.
    """

    @functools.wraps(method)
    def held(self: HonestyState, /, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run the method under the lock.

        Args:
            self: The state.
            *args: The method's positional arguments.
            **kwargs: The method's keyword arguments.

        Returns:
            What the method returns.
        """
        with self._lock:  # pyright: ignore[reportPrivateUsage]
            return method(self, *args, **kwargs)

    return held


class HonestyState:
    """The honesty state of one session, in memory only (specification sections 2 and 13).

    The state holds what cannot be computed: the marks, the contents verified in this session,
    the files to watch that git does not list, the stored trust it was given, and whether a
    review is required. Everything else is computed when asked, from the working tree and the
    baseline:

        record(dirty, clean, by, edited)   a checker's marks, after the tool call succeeded
        expect_edit(path, tool, input)     at the gate: the hash a checked edit will produce
        record_edit(path, expected)        after the edit: verified if it has that content
        apply_review(results)              an honesty review: verified files and findings
        review_plan(max_files)             what an honesty review has to read now
        apply_current_review(results)      apply_review for the files that did not change
                                           while the review ran
        seen_now(paths)                    content and marks of files a review just judged,
                                           for the session's record of the last review
        storable(path)                     the verification to store in `trusted_files`
        close_stale() / clear(id, by)      marks calu or a person closes
        watch(path) / watch_all(paths)     files git does not list that the session wrote
        snapshot()                         baseline, open marks, unreviewed files, required
        certification(commit)              whether a commit may be certified, and its source

    Paths are relative to the workspace, with forward slashes; a file outside the workspace is
    named by its absolute path. A session keeps one state per checkout (D251): each asks git
    in its own `root` (its files, index and baseline), names files relative to the same
    workspace, and shares the trust of contents and the mark ids (`HonestyShared`).

    A file is **verified** while the SHA-256 of its content is the one recorded for its path
    in this session, or one of `trusted_hashes`, and the source of that record is at least
    `sufficient_trust`. A file is **unreviewed** when it exists,
    differs from the baseline (or the baseline does not hold it) and is not verified, and also
    when the index holds a content for it that is not reviewed (`unreviewed`). Every failure
    to read git or a file counts as unreviewed.

    A mark and a verification are separate records. Opening a mark on a file keeps what was
    verified for its path in this session, so that the edit which settles the mark starts from
    reviewed content; the open mark alone makes the state not clean. The stored trust of the
    marked content is withdrawn (`MarkChanges.withdrawn`).

    Contents are compared byte for byte with what git stores. In a repository that converts
    line endings or applies content filters on checkout, a converted file differs from the
    baseline and is unreviewed until its working content is verified, and a commit of it is
    not certified by the session, because the commit records other bytes than the verified
    ones. A person certifies such commits.

    The public methods read git and files synchronously and may be called from several
    threads: each takes the state's lock for its whole call, so a slow `unreviewed()` makes
    other callers wait. Call them from a worker thread, never from the event loop. The
    attributes may be set by the session between calls.
    """

    def __init__(
        self,
        workspace: Path,
        max_open_marks: int = 6,
        sufficient_trust: TrustSource = "check",
        *,
        root: Path | None = None,
        shared: HonestyShared | None = None,
    ) -> None:
        """Create an empty state: no baseline, no mark, nothing verified.

        Args:
            workspace: The project directory. Files are named relative to it (`key`).
            max_open_marks: `honesty.max_open_marks`: with more open marks than this a review
                is required. The attribute may be set again when the configuration changes.
            sufficient_trust: `honesty.sufficient_trust`: the weakest source of trust that
                counts. The attribute may be set again when the configuration changes; the
                session then also looks up its baseline again.
            root: The checkout this state judges (D251): git is asked there, so its files,
                its index and its baseline are the ones of that working tree. None is the
                workspace itself (the main checkout).
            shared: What the states of the session's other checkouts share with this one
                (`HonestyShared`); None gives this state a part of its own.
        """
        self.workspace = workspace.resolve()
        self.root = root.resolve() if root is not None else self.workspace
        """The top directory of the checkout this state judges (D251)."""
        self.shared = shared if shared is not None else HonestyShared()
        """The lock, the trust of contents and the mark ids, shared with the states of the
        session's other checkouts."""
        self.max_open_marks = max_open_marks
        self.sufficient_trust: TrustSource = sufficient_trust
        self.baseline: Baseline | None = None
        """The nearest trusted ancestor of HEAD with the source of its trust, set by the
        session (`find_baseline`). It counts only while its source is sufficient."""
        self.marks: list[HonestyMark] = []
        """Every mark of the session, open and closed, oldest first."""
        self.verified: dict[str, Verified] = {}
        """Path -> the content that a checked edit or an honesty review verified in this
        session, with its source."""
        self.watched: set[str] = set()
        """Files git does not list (ignored, or outside the repository) that the session wrote
        or a person named. calu does not search for others."""
        self.excluded: tuple[str, ...] = ()
        """Directories, relative to the workspace, whose files are never unreviewed and that
        no honesty review reads: calu's own output inside the project (the session traces,
        agent/records/PLAN.md D232). The session sets it when the configuration changes."""
        self.review_required = False
        self.generated = GeneratedRules()
        """Which files count as generated (`review_plan`, `record`); `Session._refresh_trust`
        sets it from `honesty.generated_paths` and `honesty.generated_contents`."""
        self.file_bytes = REVIEW_FILE_BYTES
        """The largest text for the reviewer of one file (`review_plan`).
        `Session._refresh_trust` copies `honesty.review_file_bytes` into it."""
        self.context_lines = DIFF_CONTEXT_LINES
        """Lines of context around each change of a diff for the reviewer (`review_plan`).
        `Session._refresh_trust` copies `honesty.review_context_lines` into it before each
        look at the honesty state; `DIFF_CONTEXT_LINES` until then."""
        self._lock = self.shared.lock
        self._top: Path | None = None
        self._object_format: str | None = None
        self._trees: dict[str, dict[str, tuple[str, str]]] = {}
        self._hashed: dict[str, tuple[tuple[int, int, int, int], ContentIds]] = {}

    @property
    def trusted_hashes(self) -> Mapping[str, TrustSource]:
        """The trust of file contents: SHA-256 -> source, shared by every checkout.

        It holds the table `trusted_files`, set by the session
        (`SessionStore.trusted_file_sources`), plus every content this session verified.
        A file with such a content is verified wherever it lies, in every checkout, if the
        source is sufficient. Shared by the states of all checkouts (`HonestyShared`).

        Returns:
            The mapping.
        """
        return self.shared.trusted_hashes

    @trusted_hashes.setter
    def trusted_hashes(self, value: Mapping[str, TrustSource]) -> None:
        """Replace the trust of contents for every checkout of the session.

        Args:
            value: SHA-256 -> source.
        """
        self.shared.trusted_hashes = value

    @property
    def _verified_blobs(self) -> dict[str, TrustSource]:
        """Map the git blob id of every content verified in this session to its source.

        The index may hold such a content after the working file was edited again. One entry
        per verified edit or reviewed file; it is empty after a restart. Shared by the states
        of all checkouts.

        Returns:
            The mapping, which callers change in place.
        """
        return self.shared.verified_blobs

    # ------------------------------------------------------------------ paths and contents

    def left_out(self, key: str) -> bool:
        """Tell whether a file lies in a directory the state does not judge (`excluded`).

        Args:
            key: The file's name in the state (`key`).

        Returns:
            True if the file is one of the `excluded` directories or lies below one.
        """
        return any(key == path or key.startswith(path + "/") for path in self.excluded)

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
                answer = run_git(self.root, ["rev-parse", "--show-object-format"])
            except GitError:
                return "sha1"
            name = answer.decode(errors="replace").strip()
            self._object_format = name if name in ("sha1", "sha256") else "sha1"
        return self._object_format

    def _repository(self) -> Path:
        """Return the top directory of the checkout this state judges (`root`).

        Returns:
            The directory, resolved.

        Raises:
            GitError: If the workspace is in no git repository or git could not be asked.
        """
        if self._top is None:
            answer = run_git(self.root, ["rev-parse", "--show-toplevel"])
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

    def _base(self) -> Baseline | None:
        """Return the baseline that counts.

        Returns:
            `self.baseline`, or None if there is none or its source is weaker than
            `sufficient_trust`.
        """
        base = self.baseline
        if base is None or not sufficient(base.source, self.sufficient_trust):
            return None
        return base

    def _verified_source(self, key: str, ids: ContentIds) -> TrustSource | None:
        """Tell who verified a content of a file, if a sufficient source did.

        Args:
            key: The file.
            ids: The hashes of the content.

        Returns:
            The stronger of the source recorded for the path in this session (if it records
            this SHA-256) and the source `trusted_hashes` holds for the SHA-256; None if
            neither exists or the stronger one is weaker than `sufficient_trust`.
        """
        held = self.verified.get(key)
        own = held.source if held is not None and held.sha256 == ids.sha256 else None
        source = stronger(own, self.trusted_hashes.get(ids.sha256))
        return source if sufficient(source, self.sufficient_trust) else None

    def _reviewed_source(self, key: str, ids: ContentIds) -> TrustSource | None:
        """Tell by which source a content of a file counts as reviewed.

        Args:
            key: The file.
            ids: The hashes of the content.

        Returns:
            The source of its verification (`_verified_source`); else the baseline's source if
            the content is the one the baseline records for that path; else None, also when
            git cannot be asked.
        """
        source = self._verified_source(key, ids)
        base = self._base()
        if source is not None or base is None:
            return source
        try:
            top = self._repository()
            inside = (self.workspace / key).relative_to(top).as_posix()
            entry = self._tree(base.commit_id).get(inside)
        except GitError, ValueError:
            return None
        return base.source if entry is not None and entry[1] == ids.blob else None

    def _blob_source(self, blob: str) -> TrustSource | None:
        """Tell by which source a content that only git holds counts as reviewed.

        Args:
            blob: The git blob id of a staged or committed content.

        Returns:
            The source that verified a content with this blob id in this session, if it is at
            least `sufficient_trust`; "user" for the empty content, which has no name and no
            documentation to disagree; None otherwise.
        """
        if blob == hash_bytes(b"", self._format()).blob:
            return "user"
        source = self._verified_blobs.get(blob)
        return source if sufficient(source, self.sufficient_trust) else None

    def _remember_verified(self, key: str, record: Verified) -> None:
        """Remember a content that was just verified, for `_blob_source` and every checkout.

        Its blob id goes to the verified blobs, and its SHA-256 to `trusted_hashes` with the
        record's source (unless a stronger source is held already), so that the same content
        counts as verified at any path of any checkout of the session (D251). Nothing is
        remembered while the file carries an open mark, or when its present content is not
        the verified one.

        Args:
            key: The file.
            record: Its verification.
        """
        if key in self.dishonest_files():
            return
        ids = self._content(key)
        if ids is not None and ids.sha256 == record.sha256:
            self._verified_blobs[ids.blob] = record.source
            # A content verified here counts in every checkout at once (D251): a copy or a
            # merge that does not move HEAD needs no reload of the stored trust.
            held = self.trusted_hashes.get(record.sha256)
            best = stronger(held, record.source)
            if best is not None and best != held:
                self.trusted_hashes = {**self.trusted_hashes, record.sha256: best}

    def _marked(self, key: str, changes: MarkChanges) -> None:
        """Withdraw what calu holds about the present content of a file a mark was opened on.

        The content's blob id is no longer a verified one, its entry in `trusted_hashes` is
        dropped unless a person recorded it, and its SHA-256 is added to `changes.withdrawn`
        for the session to withdraw the stored row. `verified` keeps its entry (see the class
        docstring).

        Args:
            key: The marked file.
            changes: The changes of the running call.
        """
        ids = self._content(key)
        if ids is None:
            return
        self._verified_blobs.pop(ids.blob, None)
        if self.trusted_hashes.get(ids.sha256) in ("check", "review"):
            self.trusted_hashes = {
                sha256: source
                for sha256, source in self.trusted_hashes.items()
                if sha256 != ids.sha256
            }
        if ids.sha256 not in changes.withdrawn:
            changes.withdrawn.append(ids.sha256)

    @_locked
    def is_reviewed(self, path: str | Path) -> bool:
        """Tell whether a file's present content is one that was reviewed.

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
        return self._reviewed_source(key, ids) is not None

    # ------------------------------------------------------------------ verification

    @_locked
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

    @_locked
    def watch_all(self, paths: Collection[str | Path]) -> None:
        """Remember several files as `watch` does, e.g. the files of a measured write set.

        Args:
            paths: Absolute paths, or paths relative to the workspace.
        """
        for path in paths:
            self.watched.add(self.key(path))

    @_locked
    def expect_edit(
        self, path: str | Path, tool_name: str, tool_input: Mapping[str, Any]
    ) -> str | None:
        """Compute, before an edit runs, the hash of the content the edit checkers judge.

        The session calls this when an Edit, Write or MultiEdit reaches the gate and passes
        the result to `record_edit` after the tool succeeded. The file is then verified only
        if it has exactly this content, so a change that anything else makes to the file
        between the gate and the tool's result leaves the file unreviewed.

        The bytes of the file are read once. For a Write the expected content is the tool's
        `content`. For an Edit or MultiEdit it is the replacements applied to the bytes read
        (`expected_edit`), and those bytes must themselves be reviewed (verified, or the
        baseline's content for the path): the checkers judge the replacements, so the rest of
        the file must have been reviewed before.

        Args:
            path: The file the tool names; absolute, or relative to the workspace.
            tool_name: "Write", "Edit" or "MultiEdit".
            tool_input: The tool's input as Claude sent it.

        Returns:
            The SHA-256 of the expected content. None if the result would not be a verified
            content: the path is a symbolic link or no regular file, the file is larger than
            `READ_LIMIT` or cannot be read, the edit cannot be predicted (`expected_edit`),
            or the bytes a partial edit starts from are not reviewed.
        """
        key = self.key(path)
        target = self.workspace / key
        before: bytes | None = None
        if os.path.lexists(target):
            before = read_regular(target)
            if before is None:
                return None
        after = expected_edit(tool_name, tool_input, before)
        if after is None:
            return None
        if tool_name != "Write" and before is not None:
            if self._reviewed_source(key, hash_bytes(before, self._format())) is None:
                return None
        return hashlib.sha256(after).hexdigest()

    @_locked
    def record_edit(self, path: str | Path, expected_sha256: str | None) -> Verified | None:
        """Record that the edit checkers judged an edit of a file, after the edit succeeded.

        The file is watched from now on. If its present content has the hash `expect_edit`
        computed at the gate, the file is recorded as verified at that content by the source
        "check", and its text is kept for the diffs of later reviews (`_keep_text`, which
        reads the file once more). Otherwise what was recorded for the path is dropped, so the
        file is unreviewed until an honesty review reads it. If the path is a symbolic link,
        the file it points at is watched too and nothing is verified.

        A mark the checkers left on the file is separate: a file with an open mark is
        dishonest whatever is recorded here (`record`), and `storable` hands out nothing for
        it. Whether the record counts depends on `sufficient_trust`: with "review" or "user" a
        checked edit does not make the file reviewed (`is_reviewed`).

        Args:
            path: The edited file; absolute, or relative to the workspace.
            expected_sha256: What `expect_edit` returned for this edit before it ran.

        Returns:
            The record if the file has the expected content; None otherwise.
        """
        key = self.watch(path)
        target = self.workspace / key
        if target.is_symlink():
            with suppress(OSError):
                self.watch(target.resolve())
            self.verified.pop(key, None)
            return None
        ids = None
        if expected_sha256 is not None and CONTENT_ID.fullmatch(expected_sha256):
            ids = self._content(key)
        if ids is None or ids.sha256 != expected_sha256:
            self.verified.pop(key, None)
            return None
        record = Verified(ids.sha256, "check")
        self.verified[key] = record
        self._remember_verified(key, record)
        self._keep_text(key, ids.sha256)
        return record

    def _keep_text(self, key: str, sha256: str) -> None:
        """Keep the text of a file's present content for the diffs of later reviews.

        Side effect: reads the file (at most `REVIEW_READ_BYTES`); puts its text into
        `HonestyShared.texts` if it is UTF-8 and still has the content `sha256` names, else
        keeps nothing (the file changed meanwhile, or it is not text).

        Args:
            key: The file.
            sha256: The SHA-256 of the content that was just verified.
        """
        data = read_regular(self.workspace / key, REVIEW_READ_BYTES)
        if data is None or hash_bytes(data, self._format()).sha256 != sha256:
            return
        try:
            self.shared.texts.put(sha256, data.decode("utf-8"))
        except UnicodeDecodeError:
            return

    @_locked
    def storable(self, path: str | Path) -> Verified | None:
        """Return the verification of a file that the session should store in the database.

        calu stores what it verifies (specification section 13): after `record_edit` or
        `apply_review` the session asks this for each file and records the answer with
        `SessionStore.trust_file`, so the content stays verified after a restart.

        Args:
            path: Absolute, or relative to the workspace.

        Returns:
            The record of this session for the path, if the file's present content is the
            recorded one and the file carries no open mark. None otherwise: a content that was
            found dishonest is not stored, because the marks live only in this session.
        """
        key = self.key(path)
        held = self.verified.get(key)
        if held is None or key in self.dishonest_files():
            return None
        ids = self._content(key)
        return held if ids is not None and ids.sha256 == held.sha256 else None

    @_locked
    def apply_review(self, results: Mapping[str, FileReview]) -> MarkChanges:
        """Apply the result of an honesty review (specification section 4).

        For every reviewed file: its open marks are closed, the file is recorded as verified
        by the source "review" at the content the review read, and each finding opens a mark.
        A file with findings is dishonest while they are open; `storable` hands out nothing
        for it, and the stored trust of its present content is to be withdrawn
        (`MarkChanges.withdrawn`). A result without a SHA-256 changes nothing for its file.
        Afterwards a review is required exactly when more than `max_open_marks` marks are
        open: a review that leaves fewer ends the requirement, so the remaining marks are
        settled by edits again, and a command under `enforce` is denied with them instead of
        starting the next review. A review that failed must not be passed here: it verifies
        nothing.

        Args:
            results: Path (as `key` names it) -> what the review said about the file.

        Returns:
            The marks closed and opened, findings that could not be recorded, the contents
            whose stored trust is to be withdrawn, and whether a review is required now.
        """
        changes = MarkChanges()
        for path, review in results.items():
            key = self.key(path)
            if not CONTENT_ID.fullmatch(review.sha256):
                changes.ignored.append(f"{key}: the review named no content hash")
                continue
            for mark in [m for m in self.open_marks if m.path == key]:
                changes.closed.append(self._close(mark, REVIEWER))
            self.verified[key] = Verified(review.sha256, "review")
            self.watched.add(key)
            for finding in review.findings:
                changes.opened.append(self._open(key, finding.symbol, finding.reason, REVIEWER))
            if review.findings:
                self._marked(key, changes)
            else:
                self._remember_verified(key, self.verified[key])
        # The review ran: the requirement is what its result leaves, not what it was before.
        self.review_required = len(self.open_marks) > self.max_open_marks
        changes.review_required = self.review_required
        return changes

    @_locked
    def apply_current_review(self, results: Mapping[str, FileReview]) -> MarkChanges:
        """Apply the results of an honesty review that still describe their files.

        A review takes time, and steps go on meanwhile. A result is applied (`apply_review`)
        only if the file's present content has the SHA-256 the review read and, where the
        result records them (`FileReview.marks`), the file's open marks are the ones the
        review saw. Every other result changes nothing: its file is as it was.

        Args:
            results: Path (as `key` names it) -> what the review said about the file.

        Returns:
            What `apply_review` returns for the applied results, with one entry in
            `dropped` per result that was not applied.
        """
        current: dict[str, FileReview] = {}
        dropped: list[tuple[str, str]] = []
        for path, review in results.items():
            key = self.key(path)
            ids = self._content(key)
            held = tuple(sorted(mark.id for mark in self.open_marks if mark.path == key))
            if ids is None or ids.sha256 != review.sha256:
                dropped.append((key, "the file changed while the review ran"))
            elif review.marks is not None and tuple(sorted(review.marks)) != held:
                dropped.append((key, "its marks changed while the review ran"))
            else:
                current[key] = review
        changes = self.apply_review(current)
        changes.dropped += dropped
        return changes

    @_locked
    def seen_now(self, paths: Iterable[str]) -> dict[str, FileSeen]:
        """Say what the state holds for files an honesty review just judged.

        The session calls this after `apply_current_review`, for the files whose results were
        applied, and keeps the answer in `Reviewed.seen`.

        Args:
            paths: The files, as `key` names them.

        Returns:
            File -> (the SHA-256 the state records as verified for it, the ids of its open
            marks, sorted). A file without a recorded verification is left out.
        """
        seen: dict[str, FileSeen] = {}
        for path in paths:
            key = self.key(path)
            held = self.verified.get(key)
            if held is not None:
                seen[key] = (held.sha256, self._mark_ids(key))
        return seen

    def _mark_ids(self, key: str) -> tuple[int, ...]:
        """List the ids of the open marks on a file.

        Args:
            key: The file, as `key` names it.

        Returns:
            The ids, sorted.
        """
        return tuple(sorted(mark.id for mark in self.open_marks if mark.path == key))

    def _review_candidates(self) -> tuple[list[str], str]:
        """List the files an honesty review has to judge: unreviewed or with an open mark.

        Returns:
            (the paths, sorted; "" or why git could not list them, in which case no path is
            named). Without a baseline that counts, the files are every file git lists for
            the repository, the watched files that exist, and the marked files.
        """
        found = self.dishonest_files()
        if self._base() is not None:
            unreviewed = self.unreviewed()
            if unreviewed.everything:
                return [], unreviewed.problem or "git did not list the files"
            found.update(unreviewed.paths)
            return sorted(key for key in found if not self.left_out(key)), ""
        try:
            top = self._repository()
            output = run_git(top, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        except GitError as exc:
            return [], str(exc)
        for raw in set(output.split(b"\0")) - {b""}:
            inside = os.fsdecode(raw).rstrip("/")
            if os.path.lexists(top / inside):
                found.add(self.key(top / inside))
        found.update(key for key in self.watched if os.path.lexists(self.workspace / key))
        return sorted(key for key in found if not self.left_out(key)), ""

    def _holds_no_code(self, key: str, data: bytes) -> bool:
        """Tell whether a file read for a review holds no code (`holds_no_code`).

        Args:
            key: The file, as `key` names it.
            data: Its content, as the review read it.

        Returns:
            True if it holds no code; False also when its mode cannot be read.
        """
        try:
            mode = (self.workspace / key).lstat().st_mode
        except OSError:
            return False
        return holds_no_code(key, data[:8], mode)

    @_locked
    def review_plan(self, max_files: int) -> ReviewPlan:
        """Compute what an honesty review has to do now (specification section 4).

        The files to judge are the unreviewed files and the files with an open mark. For each
        the content is read once; the SHA-256 and the text for the reviewer both come from
        those bytes, so a file is later verified at exactly the content the reviewer was
        shown. Per file, the first rule that applies decides:

            not a regular file calu can read, larger than
            `REVIEW_READ_BYTES`, or not UTF-8 text          -> skipped
            no open mark, and the present content is
            reviewed already:
              without a baseline                            -> not part of the review
                                                               (`verified_already`)
              with a baseline (only the content the index
              holds for the path is not reviewed)           -> skipped
            no open mark, and the file holds no code
            (`holds_no_code`: a plain-text, data or media
            extension, no execute bit, no `#!` or program
            header); for a file that is not UTF-8 or
            larger than `REVIEW_READ_BYTES` this rule
            comes before the rules above                    -> verified without a model
            generated (`generated`: a pattern of
            `honesty.generated_paths` or
            `honesty.generated_contents` matches), with or
            without marks                                   -> verified without a model;
                                                               its open marks are closed
                                                               (`ReviewPlan.generated`)
            no open mark, the baseline holds the path as
            a file, and the change is whitespace only       -> verified without a model
            an open mark, a file larger than
            `SMALL_FILE_BYTES` with an earlier content to
            compare with (below), and every marked symbol
            names a definition (`marked_excerpts`)          -> the reviewer gets the diff and
                                                               the present source of the
                                                               marked definitions
            another open mark                               -> the reviewer gets the whole file
            the content this session verified last for
            the file is another one, and its text is kept
            (`VerifiedTexts`)                               -> the reviewer gets the diff
                                                               since that content
            a path the baseline does not hold as a file
            of at most `REVIEW_READ_BYTES` of UTF-8 text,
            or no baseline                                  -> the reviewer gets the whole file
            otherwise                                       -> the reviewer gets the diff
                                                               against the baseline's content

        The diffs have `context_lines` lines of context around each change. The text of every
        content read here is kept in `HonestyShared.texts`, so that the next review of the file,
        once this content is verified, shows only the change since it. A diff carries the
        whole present content too (`ReviewFile.content`), for a second round in which the
        reviewer reads the whole file (`calu.checkers.honesty_review`). A file whose text for
        the reviewer (the diff with its excerpts, or the whole file) is larger than
        `file_bytes` is skipped. So a small change of a large file is reviewed, and a large new
        file is not.

        Args:
            max_files: `honesty.review_max_files`. With more files to judge the plan has only
                `count` and a `problem`, and no file is read.

        Returns:
            The plan. `problem` is set, and nothing else but `count`, when there are more
            files than `max_files` or git could not list the files.
        """
        base = self._base()
        candidates, problem = self._review_candidates()
        if problem:
            return ReviewPlan(base, problem=f"git could not list the files to review ({problem})")
        if len(candidates) > max_files:
            return ReviewPlan(
                base,
                count=len(candidates),
                problem=(
                    f"{len(candidates)} files are unreviewed or marked, and one honesty review "
                    f"reads at most {max_files} (honesty.review_max_files)"
                ),
            )
        tree: dict[str, tuple[str, str]] = {}
        top: Path | None = None
        if base is not None:
            try:
                top = self._repository()
                tree = self._tree(base.commit_id)
            except GitError as exc:
                return ReviewPlan(base, problem=f"git could not list the baseline ({exc})")
        files: list[ReviewFile] = []
        shortcuts: dict[str, FileReview] = {}
        skipped: list[tuple[str, str]] = []
        seen: dict[str, FileSeen] = {}
        no_code: list[str] = []
        generated: list[str] = []
        already = 0
        for key in candidates:
            marks = tuple(mark for mark in self.open_marks if mark.path == key)
            mark_ids = self._mark_ids(key)
            data = read_regular(self.workspace / key, REVIEW_READ_BYTES)
            if data is None and not marks:
                large = no_code_sha256(self.workspace / key)
                if large is not None:
                    seen[key] = (large, mark_ids)
                    shortcuts[key] = FileReview(large, marks=())
                    no_code.append(key)
                    continue
            if data is None:
                now = self._content(key)
                seen[key] = (now.sha256 if now else "", mark_ids)
                why = f"not a readable regular file of at most {REVIEW_READ_BYTES} bytes"
                skipped.append((key, why))
                continue
            ids = hash_bytes(data, self._format())
            unread = not marks and self._holds_no_code(key, data)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                seen[key] = (ids.sha256, mark_ids)
                if unread:
                    shortcuts[key] = FileReview(ids.sha256, marks=())
                    no_code.append(key)
                else:
                    skipped.append((key, "not UTF-8 text"))
                continue
            if not marks and self._reviewed_source(key, ids) is not None:
                if base is None:
                    already += 1
                    continue
                seen[key] = (ids.sha256, mark_ids)
                skipped.append(
                    (
                        key,
                        "the index holds an earlier content for it that nobody verified; "
                        "`git add` of the file settles that",
                    )
                )
                continue
            seen[key] = (ids.sha256, mark_ids)
            if unread:
                shortcuts[key] = FileReview(ids.sha256, marks=())
                no_code.append(key)
                continue
            if self.generated.reason(key, data):
                # Generated code is not judged; a mark that is open on it is closed.
                shortcuts[key] = FileReview(ids.sha256, marks=mark_ids)
                generated.append(key)
                continue
            self.shared.texts.put(ids.sha256, text)
            against: Literal["baseline", "verified"] = "baseline"
            before = self._last_verified_text(key, ids.sha256)
            if before is not None:
                against = "verified"
            elif top is not None:
                before = self._baseline_text(top, tree, key)
            excerpts = ""
            if marks and before is not None and len(data) > SMALL_FILE_BYTES:
                # A large marked file: its change, and the marked definitions as they are now.
                excerpts = marked_excerpts(text, key, [mark.symbol for mark in marks]) or ""
            whole = before is None or (bool(marks) and not excerpts)
            if before is not None and not marks and whitespace_only(before, text):
                shortcuts[key] = FileReview(ids.sha256, marks=())
                continue
            shown_text = text if whole else change_diff(before or "", text, key, self.context_lines)
            if len(shown_text.encode("utf-8")) + len(excerpts.encode("utf-8")) > self.file_bytes:
                what = (
                    "the file"
                    if whole
                    else "its change against the baseline"
                    if against == "baseline"
                    else "its change since the content last verified"
                )
                skipped.append((key, f"{what} is larger than {self.file_bytes} bytes"))
                continue
            files.append(
                ReviewFile(
                    key,
                    ids.sha256,
                    shown_text,
                    whole=whole,
                    marks=marks,
                    against=against,
                    excerpts=excerpts,
                    content="" if whole else text,
                )
            )
        return ReviewPlan(
            base,
            count=len(candidates) - already,
            files=tuple(files),
            shortcuts=shortcuts,
            skipped=tuple(skipped),
            no_code=tuple(no_code),
            generated=tuple(sorted(generated)),
            verified_already=already,
            seen=seen,
            review_required=self.review_required,
        )

    def _last_verified_text(self, key: str, present: str) -> str | None:
        """Return the text of the content of a file that this session verified last.

        Args:
            key: The file.
            present: The SHA-256 of its present content.

        Returns:
            The text of the content `verified` records for the file, if it is another content
            than the present one and its text is kept (`VerifiedTexts`); else None.
        """
        record = self.verified.get(key)
        if record is None or record.sha256 == present:
            return None
        return self.shared.texts.get(record.sha256)

    def _baseline_text(
        self, top: Path, tree: Mapping[str, tuple[str, str]], key: str
    ) -> str | None:
        """Read the content the baseline records for a file, as text.

        Args:
            top: The repository's top directory.
            tree: The baseline's listing (`_tree`).
            key: The file.

        Returns:
            The content; None if the baseline does not hold the path as a regular file (it
            is missing, a submodule or a symbolic link), the content is larger than
            `REVIEW_READ_BYTES` (asked from git before it is read) or not UTF-8, or git could
            not be asked.
        """
        try:
            entry = tree.get((self.workspace / key).relative_to(top).as_posix())
        except ValueError:
            return None
        if entry is None or entry[0] in (GITLINK_MODE, LINK_MODE):
            return None
        try:
            size = int(run_git(top, ["cat-file", "-s", entry[1]]).strip())
            if size > REVIEW_READ_BYTES:
                return None
            data = run_git(top, ["cat-file", "blob", entry[1]])
        except GitError, ValueError:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # ------------------------------------------------------------------ marks

    @property
    @_locked
    def open_marks(self) -> list[HonestyMark]:
        """The open marks, oldest first.

        Returns:
            The marks nothing has closed yet.
        """
        return [mark for mark in self.marks if mark.open]

    @_locked
    def dishonest_files(self) -> set[str]:
        """Name the files that carry at least one open mark.

        Returns:
            Their paths.
        """
        return {mark.path for mark in self.open_marks}

    @_locked
    def record_step(
        self,
        answers: Sequence[tuple[str, Sequence[CodeMark], Sequence[CodeMark]]],
        edited: Collection[str] = (),
    ) -> MarkChanges:
        """Apply the marks of all checkers that judged one step (section 3).

        The clean marks of every answer are applied first, then the dirty marks of every
        answer (both via `record`). So a clean mark closes only marks that were open before
        the step: a file-wide HONEST of the honesty checker (`ALL_SYMBOLS`) does not close the
        dirty mark another checker opens on the same file in the same step. Applied answer by
        answer, the route's checker comes first and its fresh mark would be closed at once.

        Side effects: as `record`, once per answer and kind of mark.

        Args:
            answers: One (checker name, dirty marks, clean marks) per checker of the step, in
                the order the checkers ran.
            edited: The workspace-relative paths the step edits; empty for a command.

        Returns:
            The changes of all answers together. Each `ignored` sentence starts with the name
            of the checker whose mark it is ("honesty: ..."); `withdrawn` holds each content
            once; `review_required` is as after the last mark.
        """
        total = MarkChanges()

        def add(name: str, changes: MarkChanges) -> None:
            """Add one `record` call's changes to `total`.

            Args:
                name: The checker whose marks they are.
                changes: What the call changed.
            """
            total.opened += changes.opened
            total.closed += changes.closed
            total.ignored += [f"{name}: {message}" for message in changes.ignored]
            total.withdrawn += [one for one in changes.withdrawn if one not in total.withdrawn]

        for name, _dirty, clean in answers:
            if clean:
                add(name, self.record((), clean, name, edited))
        for name, dirty, _clean in answers:
            if dirty:
                add(name, self.record(dirty, (), name, edited))
        total.review_required = self.review_required
        return total

    @_locked
    def record(
        self,
        dirty: Sequence[CodeMark],
        clean: Sequence[CodeMark],
        by: str = "",
        edited: Collection[str] = (),
    ) -> MarkChanges:
        """Apply the marks of one checker's answer (section 3).

        The session calls this after the tool call succeeded; for the dirty marks of a command
        or a commit it calls it when the checkers answered, because they name code that exists
        whatever the command does.

        Clean marks are applied first, then dirty ones, so a spot that the same answer names
        in both stays open. A clean mark closes an open mark only if no review is required,
        the step edits that file, and path and symbol match (`normal_symbol`, `match_symbol`;
        in a Python file that parses, the symbol is first read as the definition it names).
        A clean mark with the symbol `ALL_SYMBOLS` closes every open mark of its file.
        A dirty mark on a file that counts as generated (`generated`, `GeneratedRules`) is not
        recorded, and is listed in `ignored` with the pattern that matched.
        A dirty mark must name an existing file of the workspace (`checked_marks`); for a
        Python file that parses, its symbol is replaced by the definition's dotted name, and a
        symbol that names no definition becomes a mark on the whole file. A dirty mark whose
        path contains a character that is not printable (a line break, a control character) is
        not recorded; a symbol with such a character becomes a mark on the whole file. A dirty
        mark on a spot that is already open refreshes its reason. For every file a dirty mark
        is recorded on, the SHA-256 of its present content is returned in `withdrawn`.

        Args:
            dirty: The answer's `marks_dirty`.
            clean: The answer's `marks_clean`.
            by: Name of the checker.
            edited: The workspace-relative paths the step edits; empty for a command.

        Returns:
            What was opened, closed and ignored, the contents whose stored trust is to be
            withdrawn, and whether a review is required now (it is as soon as more than
            `max_open_marks` marks are open).
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
            if entry.symbol == ALL_SYMBOLS:
                # The honesty checker's HONEST for the file: every open mark of it closes.
                for mark in standing.values():
                    changes.closed.append(self._close(mark, by or "a checker"))
                continue
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
            if not entry.path.isprintable():
                changes.ignored.append(
                    f"{shown(entry.path, NOTE_PATH_CHARS)}: the path contains a character "
                    "that is not printable"
                )
                continue
            generated = self.generated.file_reason(self.workspace, entry.path)
            if generated:
                changes.ignored.append(
                    f"{entry.label()}: the file counts as generated ({generated}), so it is "
                    "not judged for honesty"
                )
                continue
            changes.opened.append(self._open(entry.path, entry.symbol, entry.reason, by))
            self._marked(entry.path, changes)
        self._check_threshold()
        changes.review_required = self.review_required
        return changes

    @_locked
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

    @_locked
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
            The dotted names; None if the file is no Python file, is no regular file, cannot
            be read (`read_regular`), is not UTF-8 or does not parse
            (`calu.checkers.symbols.definition_names`).
        """
        if not path.endswith((".py", ".pyi")):
            return None
        data = read_regular(self.workspace / path)
        if data is None:
            return None
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError:
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
            file whose reason starts with the symbol as written. A symbol with a character
            that is not printable gives a mark on the whole file in any file.
        """
        wanted = normal_symbol(symbol)
        if not wanted.isprintable():
            reason = f"{shown(wanted, NOTE_PATH_CHARS)}: {reason}"
            wanted = ""
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
            id=self.shared.next_id,
            path=path,
            symbol=wanted,
            reason=reason,
            opened_at=datetime.now(UTC),
            opened_by=by or None,
        )
        self.shared.next_id += 1
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

    def _submodule_at(self, directory: Path, commit_id: str) -> bool:
        """Tell whether a submodule's directory is at the commit the baseline records.

        Args:
            directory: The directory of the submodule in the working tree.
            commit_id: The commit id of the baseline's submodule entry.

        Returns:
            True if the directory is empty (the submodule is not checked out, so nothing of
            it is there to review), or if it is the top of a repository whose HEAD is
            `commit_id`. False otherwise, also when the directory or git cannot be read.
        """
        try:
            names = os.listdir(directory)
        except OSError:
            return False
        if not names:
            return True
        if ".git" not in names:
            return False
        try:
            answer = run_git(directory, ["rev-parse", "--show-toplevel", "HEAD"])
        except GitError:
            return False
        lines = os.fsdecode(answer).splitlines()
        if len(lines) != 2:
            return False
        try:
            same_top = Path(lines[0]).resolve() == directory.resolve()
        except OSError:
            return False
        return same_top and lines[1].strip() == commit_id

    @_locked
    def unreviewed(self) -> Unreviewed:
        """Compute the files whose present content nobody verified.

        Looked at are the files git lists for the repository (tracked files and untracked
        files that git does not ignore), the contents the index holds for them, and the
        watched files. A submodule is looked at as one entry: its directory is unreviewed
        unless it is at the commit the baseline records for it or is not checked out; the
        files inside it are not looked at. A directory that git names as one entry and the
        baseline does not record as a submodule is unreviewed. Not looked at: other ignored
        files, other files outside the repository, deleted files, and the files of the
        `excluded` directories (calu's own session traces).

        A commit records what the index holds, so a path is also unreviewed when its staged
        content is not reviewed. A staged content is reviewed when it is

            the baseline's content for the path, or
            the present content of the working file (which is then judged as that file), or
            a content verified earlier in this session, or
            empty.

        Every other staged content makes its path unreviewed: also for a path whose working
        file is deleted, for a path with a merge conflict, and for a submodule whose staged
        commit is not the baseline's. After a restart a staged content that differs from the
        working file is unreviewed until the file is staged again.

        Where the working file of such a path is reviewed and only the index is behind, the
        path is also named in `restage`: staging the file again makes it reviewed.

        Returns:
            The unreviewed files. `everything` is set when there is no baseline that counts,
            and when git could not list the files (then with `problem`).
        """
        base_commit = self._base()
        if base_commit is None:
            return Unreviewed(everything=True)
        try:
            top = self._repository()
            base = self._tree(base_commit.commit_id)
            output = run_git(top, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
            staged = run_git(top, ["ls-files", "-z", "--stage"])
        except GitError as exc:
            return Unreviewed(everything=True, problem=str(exc))
        found: set[str] = set()
        listed: set[str] = set()
        stale: dict[str, ContentIds] = {}
        """Path -> the working file's content, where the index holds another, unverified one."""
        for record in staged.split(b"\0"):
            head, tab, name = record.partition(b"\t")
            parts = head.split()
            if not tab or len(parts) != 3:
                continue
            inside = os.fsdecode(name)
            blob, stage = parts[1].decode(), parts[2]
            entry = base.get(inside)
            if stage == b"0" and entry is not None and entry[1] == blob:
                continue
            key = self.key(top / inside)
            if stage == b"0" and self._blob_source(blob) is not None:
                continue
            ids = self._content(key) if stage == b"0" else None
            # The working file with the staged content is judged below, with every other
            # file. Any other staged content is not reviewed.
            if ids is None or ids.blob != blob:
                found.add(key)
                if ids is not None:
                    stale[key] = ids
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
                at_baseline = (
                    entry is not None
                    and entry[0] == GITLINK_MODE
                    and self._submodule_at(top / inside, entry[1])
                )
                if not at_baseline:
                    found.add(key)
                continue
            ids = self._content(key)
            if ids is None:
                found.add(key)
            elif entry is not None and entry[0] != GITLINK_MODE and entry[1] == ids.blob:
                continue
            elif self._verified_source(key, ids) is None:
                found.add(key)
        for key in self.watched - listed:
            if not os.path.lexists(self.workspace / key):
                continue
            ids = self._content(key)
            if ids is None or self._verified_source(key, ids) is None:
                found.add(key)
        paths = tuple(sorted(key for key in found if not self.left_out(key)))
        restage = tuple(
            key
            for key in paths
            if key in stale and self._reviewed_source(key, stale[key]) is not None
        )
        return Unreviewed(paths=paths, restage=restage)

    @_locked
    def snapshot(self) -> HonestySnapshot:
        """Compute the state as it is now.

        Stale marks are not closed here; call `close_stale` first.

        Returns:
            The baseline that counts, the open marks, the unreviewed files and whether a
            review is required.
        """
        return HonestySnapshot(
            baseline=self._base(),
            marks=tuple(self.open_marks),
            unreviewed=self.unreviewed(),
            review_required=self.review_required,
        )

    @_locked
    def certification(self, commit_id: str, *, rooted: bool = True) -> Certification:
        """Decide whether a commit may be certified as trusted, and with which source.

        calu certifies a commit by what the commit records, not by the working tree: every
        entry the commit holds must be the one the baseline holds for that path, or a file
        whose committed content is the content of the working file with that content
        verified, or a content verified earlier in this session, or empty. A submodule entry
        that the baseline does not hold with the same commit id is never verified. Files the
        commit deletes are not looked at.

        This looks only at what the commit records. The session additionally requires a clean
        `snapshot()` before the commit ran, because unreviewed files outside the commit do not
        show here.

        Without a baseline a commit can still be certified when `rooted` is False: then every
        entry the commit holds must be verified as above (there is no baseline entry to equal),
        and the commit must hold at least one. The session asks this after an honesty review
        that read every file of a repository without a trusted commit.

        Args:
            commit_id: The full id of the commit, usually the new HEAD after a `git commit`.
            rooted: True requires a baseline. False also judges a commit when there is none.

        Returns:
            With `problem` "": the commit may be certified with `source`, the weakest among
            the baseline's source and the sources that verified the changed files (all by
            honesty review and a baseline of "review" or "user" give "review"; any step check
            gives "check"). Otherwise `problem` is one sentence: there is no baseline, marks
            are open, a review is required, git could not list the commit, the commit records
            no file, or which entries of the commit are not verified.
        """
        base_commit = self._base()
        if base_commit is None and rooted:
            return Certification("there is no baseline")
        if self.open_marks:
            return Certification(f"{len(self.open_marks)} mark(s) are open")
        if self.review_required:
            return Certification("an honesty review is required")
        if not HEAD_ID.fullmatch(commit_id):
            return Certification("the commit id is not a full commit id")
        try:
            top = self._repository()
            base = self._tree(base_commit.commit_id) if base_commit is not None else {}
            recorded = self._tree(commit_id)
        except GitError as exc:
            return Certification(f"git could not list the commit ({exc})")
        failing: list[str] = []
        sources: list[TrustSource] = [base_commit.source] if base_commit is not None else []
        for inside, (mode, object_id) in recorded.items():
            entry = base.get(inside)
            key = self.key(top / inside)
            if mode == GITLINK_MODE or (entry is not None and entry[0] == GITLINK_MODE):
                if entry != (mode, object_id):
                    failing.append(key)
                continue
            if entry is not None and entry[1] == object_id:
                continue
            ids = self._content(key)
            source = None
            if ids is not None and ids.blob == object_id:
                source = self._verified_source(key, ids)
            if source is None:
                source = self._blob_source(object_id)
            if source is None:
                failing.append(key)
            else:
                sources.append(source)
        if failing:
            failing.sort()
            rest = f" and {len(failing) - 5} more" if len(failing) > 5 else ""
            names = ", ".join(shown(name, NOTE_PATH_CHARS) for name in failing[:5])
            return Certification(
                f"{len(failing)} file(s) of the commit are not verified: {names}{rest}"
            )
        if not sources:
            return Certification("the commit records no file")
        return Certification("", weakest(sources))

    def uncertified(self, commit_id: str) -> str:
        """Say why a commit must not be certified as trusted, if anything speaks against it.

        Args:
            commit_id: The full id of the commit.

        Returns:
            `certification(commit_id).problem`: "" if the commit may be certified, otherwise
            one sentence.
        """
        return self.certification(commit_id).problem
