"""The slow and complete reference index: a word search over the project's files.

It answers the question "where does the name of this function, method or class occur?".
It cannot tell whether an occurrence really means that unit or something else with the same
name. So it confirms nothing: every place it reports is "unclear" and someone else has to
decide.

Which files it reads:

- In a git repository: the tracked files and the untracked files, but not what git ignores.
  Library folders such as `.venv` and `node_modules` are normally ignored, so they are
  skipped.
- Outside a git repository: every file below the project directory.

What counts as a hit: the unit's bare name as a whole word. `add_user` is found in
`x.add_user(1)`, and not in `add_user_later`.

Hits it leaves out, and only counts:

- The definition of the unit itself.
- Every hit in a prose file, such as Markdown or plain text (`PROSE_SUFFIXES`). A document
  describes code. It contains no function that could depend on the unit.
- In a Python file, a hit in a comment.
- In a Python file, a hit in a string that only mentions the name. A string that is exactly
  the name, or a dotted path ending with it ("pkg.mod.add_user"), is kept: that is how
  `getattr` and configuration files refer to code.

Comments and strings are recognised only in Python files, with the standard library's
`tokenize`. A Python file that cannot be tokenized, and every file of another language, is
searched as plain text. No project code is imported or run.
"""

import io
import os
import re
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path

import anyio

from calu.refindex.model import PLACE_TEXT_CHARS, Place, Referrers, Unit

MAX_FILES = 50_000
"""Most files one search reads; a larger project is answered as not complete."""

MAX_FILE_BYTES = 2_000_000
"""Files larger than this are not searched; the answer is then not complete."""

PROSE_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
"""File names that hold prose, not code. A project's documents name its functions all the
time: on calu more than half of all hits were in such files."""

GIT_TIMEOUT_S = 20.0
"""Longest the listing of the files may take."""


@dataclass
class SearchIndex:
    """A word search over the files of one project directory (see the module docstring)."""

    root: Path
    """The project directory."""

    async def referrers(self, unit: Unit) -> Referrers:
        """Find every place that holds the bare name of `unit`, in a worker thread.

        Side effects: those of `find`.

        Args:
            unit: The unit.

        Returns:
            As `find`.
        """
        return await anyio.to_thread.run_sync(self.find, unit)

    def find(self, unit: Unit) -> Referrers:
        """Find every place that holds the bare name of `unit`; blocks while it reads.

        Side effects: runs `git ls-files` in the project directory and reads its files.

        Args:
            unit: The unit.

        Returns:
            No confirmed referrer, every kept hit as unclear, and the number of hits left
            out. Not `complete` when the files could not be listed, more than `MAX_FILES`
            exist, or a file was too large or unreadable; `problem` then says which.
        """
        word = re.compile(rf"(?<![\w]){re.escape(unit.name)}(?![\w])")
        files, problem = self._files()
        unclear: list[Place] = []
        dismissed = 0
        skipped = 0
        for name in files:
            text = self._read(name)
            if text is None:
                skipped += 1
                continue
            if unit.name not in text:
                continue
            if name.lower().endswith(PROSE_SUFFIXES):
                dismissed += sum(len(word.findall(line)) for line in text.splitlines())
                continue
            outside = _not_code(text, unit.name) if name.endswith((".py", ".pyi")) else None
            for number, line in enumerate(text.splitlines(), start=1):
                for found in word.finditer(line):
                    if name == unit.path and number == unit.line:
                        dismissed += 1
                    elif outside is not None and (number, found.start()) in outside:
                        dismissed += 1
                    else:
                        shown = line.strip()[:PLACE_TEXT_CHARS]
                        unclear.append(Place(name, number, found.start(), shown))
        if skipped:
            problem = f"{problem} {skipped} file(s) were too large or unreadable.".strip()
        return Referrers(
            unclear=tuple(unclear), dismissed=dismissed, complete=not problem, problem=problem
        )

    def _files(self) -> tuple[list[str], str]:
        """List the files to search, relative to the project directory.

        Returns:
            (the paths, sorted; "" or the reason the list is not complete).
        """
        try:
            done = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=self.root,
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], f"The files could not be listed ({type(exc).__name__})."
        if done.returncode == 0:
            names = sorted(set(os.fsdecode(done.stdout).split("\0")) - {""})
        else:
            names = sorted(
                str(path.relative_to(self.root))
                for path in self.root.rglob("*")
                if path.is_file() and ".git" not in path.parts
            )
        if len(names) > MAX_FILES:
            return names[:MAX_FILES], f"Only {MAX_FILES} of {len(names)} files were searched."
        return names, ""

    def _read(self, name: str) -> str | None:
        """Read one file of the project as text.

        Args:
            name: The path relative to the project directory.

        Returns:
            The text; "" for a file that is gone, a directory, a symbolic link or binary data
            (nothing to search); None for a file over `MAX_FILE_BYTES` or one that cannot be
            read.
        """
        path = self.root / name
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\0" in data:
            return ""
        return data.decode("utf-8", errors="replace")


def _not_code(source: str, name: str) -> set[tuple[int, int]] | None:
    """Find the occurrences of `name` in a Python source that are no code.

    Those are the occurrences in a comment, and the ones in a string that is neither the name
    itself nor a dotted path ending with it. A string that is the name can be a reference
    (`getattr(x, "name")`, a path in a configuration), so it stays.

    Args:
        source: The file's text.
        name: The bare name.

    Returns:
        (line, column) of each such occurrence, 1-based line and 0-based column; None if the
        text cannot be tokenized.
    """
    word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    found: set[tuple[int, int]] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError, SyntaxError, ValueError:
        return None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            pass
        elif token.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE):
            inner = token.string.strip("rbfuRBFU").strip("\"'")
            if inner == name or inner.endswith(f".{name}"):
                continue
        else:
            continue
        row, column = token.start
        for offset, line in enumerate(token.string.split("\n")):
            base = column if offset == 0 else 0
            found.update((row + offset, base + hit.start()) for hit in word.finditer(line))
    return found
