"""The final redaction pass of the session traces, before a commit (agent/records/PLAN.md D246).

The per-block pass (`calu.traces.writer`) keeps the live file safe on disk at any time. Before
a commit, the part of each trace file that the commit would add is redacted once more, as a
whole, so that the agent sees a whole stretch of the session (REQUIREMENTS 45). The part that
is committed already is never read by the agent and never changed:

    HEAD:path       ┌───────────── committed ─────────────┐
    file now        ├───────────── committed ─────────────┼──── new since the commit ────┤
    file afterwards ├───────────── committed ─────────────┼── new, redacted as a whole ──┤
                                                                                  marker line

The marker line names a check value (SHA-256 of everything above it, 16 hex digits). A later
pass that finds a valid marker after the committed part starts behind it, so a second pass
before the same commit (Claude's commit and the pre-commit hook) redacts nothing twice. A
file that differs from `HEAD:path` other than by an appended part (somebody edited it by
hand) is compared line by line (`difflib`): each added or changed stretch of lines is
redacted on its own, the lines the file shares with `HEAD` stay as they are.

Where the text comes from and where it goes:

    mode        reads                 writes back                      stages
    "worktree"  the file on disk      the file (live tail kept)        if `stage` is set
    "staged"    the staged content    the index; the file on disk too  always (the index)
                                      when it holds the staged text
                                      plus an appended tail

A file on disk is rewritten in place under an exclusive `flock`, which the trace writer also
takes for every append, so that a session that still writes to the file loses no block.

What happens when something fails:

    a piece whose answer is unusable        that piece is replaced by `WITHHELD_PART`; the
    (`UnusableAnswer`, a broken answer)     pass goes on
    the model cannot be reached, a timeout, `FinalizeError` for that file; nothing of the
    a git command fails, the file changed   file is changed, and the caller refuses the
    in a way that is not an appended tail   commit
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anyio
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from calu.traces.redact import (
    FINAL_CHUNK_CHARS,
    FINAL_MAX_TOKENS,
    RedactionFailed,
    Redactor,
    Secrets,
    redact_deterministic,
)

log = logging.getLogger(__name__)

TRACE_TITLE = "# calu session trace\n"
"""The first line of every trace file (`calu.traces.render.render_header`)."""

TRACE_NAME = re.compile(r"(?:^|/)session_[^/]+\.md$")
"""The name of a trace file (`calu.traces.render.create_trace_file`)."""

MARKER = "_[calu: the final redaction pass ran up to this line (check {check})]_"
"""The line that marks how far the final pass ran; `check` is `check_of` the text above."""

_MARKER_LINE = re.compile(
    r"(?m)^_\[calu: the final redaction pass ran up to this line \(check ([0-9a-f]{16})\)\]_\n"
)

WITHHELD_PART = "_[this part of the trace was withheld: the final redaction gave no usable answer]_"
"""What the file holds in place of a piece of a stretch whose redaction was unusable."""

Mode = Literal["worktree", "staged"]


class FinalizeError(Exception):
    """The final pass could not finish for a file; the message says why. Nothing was changed."""


def check_of(text: str) -> str:
    """Return the check value a marker line names for the text above it.

    Args:
        text: Everything in the file before the marker line.

    Returns:
        The first 16 hex digits of the SHA-256 of the UTF-8 text.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def marker_for(text: str) -> str:
    """Return the marker block that follows `text`.

    Args:
        text: Everything before the marker; it ends with a blank line (`with_blank_line`).

    Returns:
        The marker line followed by a blank line.
    """
    return MARKER.format(check=check_of(text)) + "\n\n"


def finalized_end(text: str) -> int:
    """Find where the part of a file ends that a final pass already covered.

    Args:
        text: The content of a trace file.

    Returns:
        The offset just after the last valid marker line (and the blank line after it, if
        any), or 0 if the text holds no valid marker. A marker line is valid if its check
        value is `check_of` the text before it; a marker that was copied or edited is not.
    """
    end = 0
    for match in _MARKER_LINE.finditer(text):
        if match.group(1) == check_of(text[: match.start()]):
            end = match.end()
            if text.startswith("\n", end):
                end += 1
    return end


def with_blank_line(text: str) -> str:
    r"""Return the text so that it ends with a blank line (or is empty).

    Args:
        text: Any text.

    Returns:
        "" for "", else the text followed by as many newlines as make it end in "\n\n".
    """
    if not text:
        return text
    stripped = text.rstrip("\n")
    return stripped + "\n\n"


def _trailing_newlines(text: str, most: int) -> str:
    """Return the newlines at the end of a text, at most `most` of them.

    Args:
        text: Any text.
        most: The largest count returned.

    Returns:
        A string of 0 to `most` newlines.
    """
    count = len(text) - len(text.rstrip("\n"))
    return "\n" * min(count, most)


@dataclass
class Segment:
    """A stretch of a trace file: kept as it is, or redacted by the final pass."""

    text: str
    redact: bool


@dataclass
class FilePlan:
    """What the final pass does with one trace file. `path` is relative to the repository."""

    path: str
    current: str
    """The text the pass reads: the file on disk ("worktree") or the staged blob ("staged")."""
    segments: list[Segment]
    mode: str = "100644"
    """The file mode for the index."""

    @property
    def new_chars(self) -> int:
        """How many characters the pass redacts.

        Returns:
            The length of the segments to redact.
        """
        return sum(len(s.text) for s in self.segments if s.redact)


@dataclass
class FileResult:
    """What the final pass did with one trace file."""

    path: str
    redacted_chars: int = 0
    withheld: int = 0
    """Pieces replaced by `WITHHELD_PART`."""
    staged: bool = False
    note: str = ""
    """A sentence about the file on disk if it was left as it was, else ""."""


@dataclass
class Outcome:
    """What the final pass did: one result per file it changed, and the files that failed."""

    results: list[FileResult] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason) of every file the pass could not finish; those files are unchanged."""

    def summary(self) -> str:
        """Return one sentence per file for the user.

        Returns:
            The sentences joined by newlines; "" if the pass changed nothing and nothing
            failed.
        """
        lines: list[str] = []
        for result in self.results:
            line = (
                f"{result.path}: {result.redacted_chars} characters redacted as a whole"
                + (f", {result.withheld} part(s) withheld" if result.withheld else "")
                + (", staged" if result.staged else "")
            )
            lines.append(line + (f" ({result.note})" if result.note else ""))
        lines += [f"{path}: not finalized: {reason}" for path, reason in self.failures]
        return "\n".join(lines)


# ---------------------------------------------------------------------- git


def git(repo: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a git command in a repository and return what it printed.

    Args:
        repo: The repository's top-level directory.
        *args: The git arguments.
        data: Bytes for git's standard input, or None.

    Returns:
        The finished process; the caller checks `returncode`.

    Raises:
        FinalizeError: If git cannot be started.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=data,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise FinalizeError(f"git cannot be run: {exc}") from exc


def git_ok(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    """Run a git command that must succeed.

    Args:
        repo: The repository's top-level directory.
        *args: The git arguments.
        data: Bytes for git's standard input, or None.

    Returns:
        Its standard output.

    Raises:
        FinalizeError: If it fails; the message holds git's first line of error output.
    """
    done = git(repo, *args, data=data)
    if done.returncode != 0:
        error = done.stderr.decode(errors="replace").strip().splitlines()
        raise FinalizeError(f"git {args[0]} failed: {error[0] if error else done.returncode}")
    return done.stdout


def repository_of(directory: Path) -> Path | None:
    """Return the top-level directory of the git work tree that holds `directory`.

    Args:
        directory: Any directory.

    Returns:
        The top-level directory, or None if `directory` is in no work tree or git is missing.
    """
    try:
        done = git(directory, "rev-parse", "--show-toplevel")
    except FinalizeError:
        return None
    if done.returncode != 0:
        return None
    return Path(done.stdout.decode().strip())


def _has_head(repo: Path) -> bool:
    """Tell whether the repository has a commit at HEAD.

    Args:
        repo: The repository's top-level directory.

    Returns:
        False on an unborn branch.
    """
    return git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}").returncode == 0


def _blob(repo: Path, spec: str) -> bytes | None:
    """Read a blob, e.g. `HEAD:path` or `:path` (the staged content).

    Args:
        repo: The repository's top-level directory.
        spec: The object name.

    Returns:
        Its bytes, or None if there is no such object.
    """
    done = git(repo, "cat-file", "blob", spec)
    return done.stdout if done.returncode == 0 else None


def _decode(data: bytes, path: str) -> str:
    """Decode a trace file's bytes.

    Args:
        data: The bytes.
        path: The file's path, for the message.

    Returns:
        The text.

    Raises:
        FinalizeError: If the bytes are not UTF-8.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FinalizeError(f"{path} is not UTF-8 text") from exc


def _split(out: bytes) -> list[str]:
    """Split git's NUL-separated list of paths.

    Args:
        out: The output of a `-z` command.

    Returns:
        The paths, without empty entries.
    """
    return [p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p]


def candidates(repo: Path, mode: Mode, directory: str | None = None) -> list[str]:
    """List the files the final pass looks at.

    Args:
        repo: The repository's top-level directory.
        mode: "staged": the files the index adds or changes against HEAD; "worktree": the
            tracked files that differ from HEAD and the untracked files that are not ignored.
        directory: Only files below this path (relative to `repo`); None for the whole
            repository.

    Returns:
        Paths relative to `repo` whose name is that of a trace file (`TRACE_NAME`), sorted.
        Whether a file is a trace is decided by its content later (`plan`).

    Raises:
        FinalizeError: If git fails.
    """
    scope = ["--", directory] if directory else []
    head = _has_head(repo)
    if mode == "staged":
        if head:
            out = git_ok(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=AM", *scope)
        else:
            out = git_ok(repo, "ls-files", "-z", "--cached", *scope)
        names = set(_split(out))
    else:
        names = set(
            _split(git_ok(repo, "ls-files", "-z", "--others", "--exclude-standard", *scope))
        )
        if head:
            names |= set(_split(git_ok(repo, "diff", "HEAD", "--name-only", "-z", *scope)))
        else:
            names |= set(_split(git_ok(repo, "ls-files", "-z", "--cached", *scope)))
    return sorted(name for name in names if TRACE_NAME.search(name))


def segments_of(base: str | None, current: str) -> list[Segment]:
    r"""Split a trace file into what the final pass keeps and what it redacts.

    Args:
        base: The committed text (`HEAD:path`), or None for a file that is not committed.
        current: The text now.

    Returns:
        The segments in order; joined, their texts are `current`. With `base` a prefix of
        `current` (the usual case), or no `base`: one kept segment up to the committed end or
        the last valid marker behind it (`finalized_end`), whichever is later, and one
        segment to redact with the rest. Otherwise (an edit by hand): `difflib` over the
        lines; lines `current` shares with `base` are kept, every inserted or replaced run
        of lines is a segment to redact.

    Example:
        segments_of("a\n", "a\nb\n") == [Segment("a\n", False), Segment("b\n", True)]
    """
    if base is None or current.startswith(base):
        start = max(len(base or ""), finalized_end(current))
        parts = [Segment(current[:start], False), Segment(current[start:], True)]
        return [part for part in parts if part.text]
    old, new = base.splitlines(keepends=True), current.splitlines(keepends=True)
    out: list[Segment] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if j1 == j2:
            continue
        text = "".join(new[j1:j2])
        redact = tag != "equal"
        if out and out[-1].redact == redact:
            out[-1].text += text
        else:
            out.append(Segment(text, redact))
    return out


def plan(repo: Path, path: str, mode: Mode) -> FilePlan | None:
    """Read one file and decide what the final pass does with it.

    Args:
        repo: The repository's top-level directory.
        path: The file, relative to `repo`.
        mode: Where the text comes from: the file on disk or the index.

    Returns:
        The plan, or None if the file is no trace file (its text does not start with
        `TRACE_TITLE`), is gone, or has nothing visible to redact.

    Raises:
        FinalizeError: If the text is not UTF-8.
    """
    if mode == "staged":
        data = _blob(repo, f":{path}")
        if data is None:
            return None
    else:
        try:
            data = (repo / path).read_bytes()
        except OSError:
            return None
    current = _decode(data, path)
    if not current.startswith(TRACE_TITLE):
        return None
    base_data = _blob(repo, f"HEAD:{path}") if _has_head(repo) else None
    base = None if base_data is None else _decode(base_data, path)
    segments = segments_of(base, current)
    if not any(s.redact and s.text.strip() for s in segments):
        return None
    staged = git(repo, "ls-files", "-s", "-z", "--", path).stdout.decode(errors="replace")
    file_mode = staged.split(" ", 1)[0] if staged else "100644"
    return FilePlan(path, current, segments, file_mode if file_mode else "100644")


def plans(repo: Path, mode: Mode, directory: str | None = None) -> list[FilePlan]:
    """Plan the final pass over every candidate file.

    Args:
        repo: The repository's top-level directory.
        mode: As `candidates`.
        directory: As `candidates`.

    Returns:
        The plans of the trace files with something to redact. Reads no model; with an empty
        result the caller needs neither settings nor credentials.

    Raises:
        FinalizeError: If git fails or a trace file is not UTF-8.
    """
    found = (plan(repo, path, mode) for path in candidates(repo, mode, directory))
    return [p for p in found if p is not None]


# ---------------------------------------------------------------------- redaction


async def finalized_text(
    file: FilePlan, redactor: Redactor | None, secrets: Secrets
) -> tuple[str, int, int]:
    """Redact the segments of a plan and return the file's new text with its marker.

    Args:
        file: The plan.
        redactor: The redaction agent for the final pass (`final_redactor`), or None to run
            stage 1 only (`traces.redact` off).
        secrets: The values calu knows to be secret (stage 1, before and after the agent).

    Returns:
        (the new text: kept segments as they are, redacted segments, a marker after the last
        redacted segment's text; the characters redacted; the pieces withheld).

    Raises:
        FinalizeError: If the redaction agent could not be reached or took too long
            (`RedactionFailed`); nothing is to be written then.
    """
    parts: list[str] = []
    redacted = withheld = 0
    last = max(i for i, s in enumerate(file.segments) if s.redact)
    for index, segment in enumerate(file.segments):
        if not segment.redact:
            parts.append(segment.text)
            continue
        redacted += len(segment.text)
        text = redact_deterministic(segment.text, secrets)
        if redactor is not None and text.strip():
            try:
                pieces = await redactor.redact_stretch(text)
            except RedactionFailed as exc:
                raise FinalizeError(f"the redaction agent failed: {exc}") from exc
            out: list[str] = []
            for _piece, answer in pieces:
                if answer is None:
                    withheld += 1
                    gap = "\n" * (2 - len(_trailing_newlines("".join(out), 2))) if out else ""
                    out.append(gap + WITHHELD_PART + "\n\n")
                else:
                    out.append(redact_deterministic(answer, secrets))
            text = "".join(out)
        if index == last:
            before = with_blank_line("".join(parts) + text)
            parts = [before, marker_for(before)]
        else:
            parts.append(text)
    return "".join(parts), redacted, withheld


def final_redactor(instructions: str, model: str | Model, timeout_s: float) -> Redactor:
    """Build the redaction agent of the final pass.

    Args:
        instructions: `calu.defaults.instructions.final_redaction_instructions(...)`.
        model: A Pydantic AI model string or `Model`.
        timeout_s: Seconds the pass may take for one file (`traces.final_timeout_s`).

    Returns:
        A `Redactor` that reads pieces of `FINAL_CHUNK_CHARS` characters.
    """
    return Redactor(
        instructions,
        model,
        timeout_s=timeout_s,
        usage_limits=UsageLimits(request_limit=2, output_tokens_limit=2 * FINAL_MAX_TOKENS),
        chunk_chars=FINAL_CHUNK_CHARS,
        max_tokens=FINAL_MAX_TOKENS,
    )


# ---------------------------------------------------------------------- writing back


def rewrite_locked(path: Path, expected: str, replacement: str) -> bool:
    """Replace the start of a file in place, under an exclusive `flock`, keeping its tail.

    The trace writer appends with `O_APPEND` and takes the same lock for every append, so a
    block it appends while the pass ran is kept after the replacement.

    Args:
        path: The file.
        expected: The text the file must start with: what the pass read.
        replacement: The text that replaces `expected`.

    Returns:
        True if the file was rewritten; False if it no longer starts with `expected` (it is
        left as it is).

    Raises:
        FinalizeError: If the file cannot be opened, read or written.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise FinalizeError(f"cannot open {path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        chunks: list[bytes] = []
        while block := os.pread(fd, 1 << 20, sum(len(c) for c in chunks)):
            chunks.append(block)
        now = b"".join(chunks)
        head = expected.encode("utf-8")
        if not now.startswith(head):
            return False
        data = replacement.encode("utf-8") + now[len(head) :]
        written = 0
        while written < len(data):
            written += os.pwrite(fd, data[written:], written)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
        return True
    except OSError as exc:
        raise FinalizeError(f"cannot rewrite {path}: {exc}") from exc
    finally:
        os.close(fd)  # also releases the lock


def stage(repo: Path, file: FilePlan, text: str) -> None:
    """Put a text into the index as the content of a file.

    Args:
        repo: The repository's top-level directory.
        file: The plan (path and mode).
        text: The content.

    Raises:
        FinalizeError: If git fails.
    """
    blob = git_ok(repo, "hash-object", "-w", "--stdin", data=text.encode("utf-8"))
    sha = blob.decode().strip()
    git_ok(repo, "update-index", "--add", "--cacheinfo", f"{file.mode},{sha},{file.path}")


def apply(repo: Path, file: FilePlan, text: str, mode: Mode, staging: bool) -> FileResult:
    """Write the finalized text of one file back.

    Args:
        repo: The repository's top-level directory.
        file: The plan.
        text: The finalized text (`finalized_text`).
        mode: "worktree": the file on disk is rewritten (it must still start with what was
            read); "staged": the index gets the text, and the file on disk is rewritten too
            if it starts with the staged text (an appended tail is kept), else left alone.
        staging: In "worktree" mode, also put the text into the index.

    Returns:
        The result for the file (`redacted_chars` and `withheld` are left to the caller).

    Raises:
        FinalizeError: In "worktree" mode, if the file changed other than by an appended
            tail while the pass ran; if a write or git fails.
    """
    result = FileResult(file.path)
    disk = repo / file.path
    if mode == "worktree":
        if not rewrite_locked(disk, file.current, text):
            raise FinalizeError(f"{file.path} changed while the final pass ran; run it again")
        if staging:
            stage(repo, file, text)
            result.staged = True
        return result
    stage(repo, file, text)
    result.staged = True
    try:
        rewritten = disk.exists() and rewrite_locked(disk, file.current, text)
    except FinalizeError as exc:
        rewritten = False
        log.warning("%s", exc)
    if not rewritten:
        result.note = "the file on disk differs from the staged one and was left as it is"
    return result


async def finalize(
    repo: Path,
    mode: Mode,
    redactor: Redactor | None,
    secrets: Secrets,
    directory: str | None = None,
    staging: bool = False,
    found: Iterable[FilePlan] | None = None,
) -> Outcome:
    """Run the final pass over the trace files of a repository.

    Side effects: model calls; rewrites trace files in place and changes the index (see
    `apply`); runs git in worker threads.

    Args:
        repo: The repository's top-level directory.
        mode: "worktree" or "staged" (the module docstring's table).
        redactor: The agent of the final pass, or None for stage 1 only.
        secrets: The values calu knows to be secret.
        directory: Only trace files below this path relative to `repo`; None for all.
        staging: In "worktree" mode, put each finalized text into the index.
        found: Plans computed already (`plans`), or None to compute them now.

    Returns:
        What was done. A file that failed is unchanged and listed in `failures`; the other
        files are finalized regardless.

    Raises:
        FinalizeError: If the files cannot be listed (git fails).
    """
    todo = (
        list(found)
        if found is not None
        else await anyio.to_thread.run_sync(plans, repo, mode, directory)
    )
    outcome = Outcome()
    for file in todo:
        try:
            text, redacted, withheld = await finalized_text(file, redactor, secrets)
            result = await anyio.to_thread.run_sync(apply, repo, file, text, mode, staging)
        except FinalizeError as exc:
            outcome.failures.append((file.path, str(exc)))
            continue
        result.redacted_chars, result.withheld = redacted, withheld
        outcome.results.append(result)
    return outcome
