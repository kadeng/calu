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

By default only files of the unit's own language are read (`same_language`): for a Python
unit the Python files. Whether code in one language calls code in another is a harder
question, left for later (the owner, 2026-11-27). On calu, 40 % of all hits for Python units
were unrelated uses of the same word in the Kotlin and TypeScript extensions.

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

For a method of a Python class there is one more rule. In another file, a method can only be
reached through an object or a class: `store.save()`, `Store.save`, `super().save()`. A bare
word `save` there is something else: a variable, a parameter, a keyword argument, another
function. So in files other than the unit's own, a hit is kept only when a dot comes right
before the name, or when it is a string that names the method. In the unit's own file every
hit is kept: a class body can use its own methods by their bare name (`@save.setter`). On
calu this rule removed about half of the hits for methods.

Comments and strings are recognised only in Python files, with the standard library's
`tokenize`. A Python file that cannot be tokenized, and every file of another language, is
searched as plain text. No project code is imported or run.
"""

import ast
import io
import os
import re
import subprocess
import tokenize
from dataclasses import dataclass, field
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

LANGUAGES: tuple[tuple[str, ...], ...] = (
    (".py", ".pyi"),
    (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    (".kt", ".kts"),
    (".java",),
    (".rs",),
    (".go",),
    (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"),
    (".sh", ".bash"),
)
"""File name endings that belong to one language, for `SearchIndex.same_language`. JavaScript
and TypeScript count as one, and so do C and C++, because they call each other directly."""

GIT_TIMEOUT_S = 20.0
"""Longest the listing of the files may take."""


@dataclass
class _Lexed:
    """The tokens of a Python source that `_outside` needs, for any name."""

    names: dict[str, list[tuple[tuple[int, int], bool]]]
    """Per name its occurrences in the code: (line, column), and whether it comes right
    after a dot."""
    texts: list[tuple[bool, str, tuple[int, int]]]
    """The comments and strings: whether it is a comment, its text, and where it starts."""


MAX_KEPT_HITS = 200_000
"""The most per-file results a `SearchMemo` keeps; at this number it forgets all of them."""

type _FileState = tuple[int, int]
"""A file's modification time in nanoseconds and its size: equal means "not changed"."""

type _HitKey = tuple[str, _FileState, str, bool, int]
"""What one per-file result depends on: the file, its state, the searched name, whether only
uses after a dot count, and the unit's own line if the file is the unit's (else 0)."""


@dataclass
class SearchMemo:
    """What a `SearchIndex` keeps between lookups, so that many lookups in a row are cheap.

    The honesty graph asks about many units at once, and again after every tool call. Without
    this, each lookup lists the files, reads all of them and tokenizes the ones that hold
    the name.

    Two kinds of memory:

    - **Per pass** (cleared by `begin`): the file list, each file's state and text, and each
      Python file's tokens. A pass is one row of lookups during which the caller accepts
      that a file changed meanwhile is read as it was at its first read.
    - **Across passes**: the result of searching one file for one name (`hits`). It is kept
      with the file's modification time and size, so a changed file is searched again and an
      unchanged one is not even read.

    A file changed without a new modification time or size is not noticed. That is the
    price; the index is used this way only for a view.
    """

    hits: dict[_HitKey, tuple[tuple[Place, ...], int]] = field(
        default_factory=dict[_HitKey, tuple[tuple[Place, ...], int]]
    )
    """Per file and name: the kept places and the number of hits left out."""
    listed: tuple[list[str], str] | None = None
    """The file list of this pass with its problem text; None until it was listed."""
    states: dict[str, _FileState | None] = field(default_factory=dict[str, _FileState | None])
    """Per file of this pass its state; None for a file that cannot be read as one."""
    texts: dict[str, str | None] = field(default_factory=dict[str, str | None])
    """Per file read in this pass what `SearchIndex._read` returned."""
    lexed: dict[str, _Lexed | None] = field(default_factory=dict[str, _Lexed | None])
    """Per Python file tokenized in this pass its tokens; None if it cannot be tokenized."""

    def begin(self) -> None:
        """Start a pass: forget the file list, the states, the texts and the tokens.

        The per-file results stay, unless there are `MAX_KEPT_HITS` of them: then they are
        forgotten too.
        """
        self.listed = None
        self.states = {}
        self.texts = {}
        self.lexed = {}
        if len(self.hits) >= MAX_KEPT_HITS:
            self.hits = {}

    def state(self, root: Path, name: str) -> _FileState | None:
        """Return a file's state, asked from the file system once per pass.

        Args:
            root: The project directory.
            name: The file, relative to it.

        Returns:
            (modification time in nanoseconds, size); None for a file that is gone or cannot
            be asked.
        """
        if name not in self.states:
            try:
                stat = (root / name).stat()
                self.states[name] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self.states[name] = None
        return self.states[name]


@dataclass
class SearchIndex:
    """A word search over the files of one project directory (see the module docstring)."""

    root: Path
    """The project directory."""
    same_language: bool = True
    """Read only files of the unit's own language (`LANGUAGES`). A unit in a file of no listed
    language is searched for in every file. False reads every file for every unit."""
    memo: SearchMemo | None = None
    """What to keep between lookups; None keeps nothing, so every lookup reads the files as
    they are at that moment."""

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
        memo = self.memo
        if memo is None:
            files, problem = self._files()
        else:
            if memo.listed is None:
                memo.listed = self._files()
            files, problem = memo.listed
        unclear: list[Place] = []
        dismissed = 0
        skipped = 0
        own = next((group for group in LANGUAGES if unit.path.endswith(group)), None)
        method = is_method(self.root, unit)
        for name in files:
            if self.same_language and own is not None and not name.endswith(own):
                continue
            member_only = method and name != unit.path and name.endswith((".py", ".pyi"))
            key: _HitKey | None = None
            if memo is not None:
                state = memo.state(self.root, name)
                if state is not None:
                    own_line = unit.line if name == unit.path else 0
                    key = (name, state, unit.name, member_only, own_line)
                    kept = memo.hits.get(key)
                    if kept is not None:
                        unclear.extend(kept[0])
                        dismissed += kept[1]
                        continue
            text = self._text(name)
            if text is None:
                skipped += 1
                continue
            places, left_out = self._search_file(name, text, unit, word, member_only)
            unclear.extend(places)
            dismissed += left_out
            if memo is not None and key is not None:
                memo.hits[key] = (tuple(places), left_out)
        if skipped:
            problem = f"{problem} {skipped} file(s) were too large or unreadable.".strip()
        return Referrers(
            unclear=tuple(unclear), dismissed=dismissed, complete=not problem, problem=problem
        )

    def _text(self, name: str) -> str | None:
        """Read one file as `_read` does, once per pass if there is a memo.

        Args:
            name: The path relative to the project directory.

        Returns:
            What `_read` returns.
        """
        if self.memo is None:
            return self._read(name)
        if name not in self.memo.texts:
            self.memo.texts[name] = self._read(name)
        return self.memo.texts[name]

    def _search_file(
        self, name: str, text: str, unit: Unit, word: re.Pattern[str], member_only: bool
    ) -> tuple[list[Place], int]:
        """Search one file's text for the unit's bare name.

        Args:
            name: The file, relative to the project directory.
            text: Its text.
            unit: The unit.
            word: The pattern of the unit's name as a whole word.
            member_only: Whether only a use right after a dot counts in this file (a method,
                searched in another Python file).

        Returns:
            (the places that may refer to the unit; the number of hits left out: the unit's
            own definition line, hits in prose files, and in Python the hits in comments, in
            other strings and, with `member_only`, the bare ones).
        """
        if unit.name not in text:
            return [], 0
        if name.lower().endswith(PROSE_SUFFIXES):
            return [], sum(len(word.findall(line)) for line in text.splitlines())
        outside = None
        if name.endswith((".py", ".pyi")):
            if self.memo is None:
                lexed = _lex(text)
            else:
                if name not in self.memo.lexed:
                    self.memo.lexed[name] = _lex(text)
                lexed = self.memo.lexed[name]
            outside = None if lexed is None else _outside(lexed, unit.name, member_only)
        places: list[Place] = []
        left_out = 0
        for number, line in enumerate(text.splitlines(), start=1):
            for found in word.finditer(line):
                if name == unit.path and number == unit.line:
                    left_out += 1
                elif outside is not None and (number, found.start()) in outside:
                    left_out += 1
                else:
                    shown = line.strip()[:PLACE_TEXT_CHARS]
                    places.append(Place(name, number, found.start(), shown))
        return places, left_out

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


def is_method(root: Path, unit: Unit) -> bool:
    """Say whether a unit is a method of a class, by reading its Python file.

    Args:
        root: The project directory.
        unit: The unit.

    Returns:
        True if the unit's file is a Python file that parses, and the definition the unit's
        dotted name names is directly inside a class. False otherwise, also for a function
        nested in a function, which can be called by its bare name.
    """
    if not unit.path.endswith((".py", ".pyi")) or "." not in unit.qualname:
        return False
    try:
        tree = ast.parse((root / unit.path).read_text("utf-8", errors="replace"))
    except OSError, SyntaxError, ValueError:
        return False
    *parents, last = unit.qualname.split(".")
    scope: ast.AST = tree
    for part in parents:
        found = next(
            (
                node
                for node in getattr(scope, "body", [])
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if found is None:
            return False
        scope = found
    return isinstance(scope, ast.ClassDef) and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == last
        for node in scope.body
    )


def _not_code(source: str, name: str, member_only: bool = False) -> set[tuple[int, int]] | None:
    """Find the occurrences of `name` in a Python source that cannot refer to the unit.

    Always: the occurrences in a comment, and the ones in a string that is neither the name
    itself nor a dotted path ending with it. A string that is the name can be a reference
    (`getattr(x, "name")`, a path in a configuration), so it stays.

    With `member_only` also every occurrence in the code that does not come right after a
    dot: the bare word `name` cannot reach a method from another file.

    Args:
        source: The file's text.
        name: The bare name.
        member_only: Whether the unit is a method and the file is not the unit's own.

    Returns:
        (line, column) of each such occurrence, 1-based line and 0-based column; None if the
        text cannot be tokenized.
    """
    lexed = _lex(source)
    return None if lexed is None else _outside(lexed, name, member_only)


def _lex(source: str) -> _Lexed | None:
    """Tokenize a Python source once, for the searches of any number of names.

    Args:
        source: The file's text.

    Returns:
        The names in the code and the comments and strings; None if the text cannot be
        tokenized.
    """
    names: dict[str, list[tuple[tuple[int, int], bool]]] = {}
    texts: list[tuple[bool, str, tuple[int, int]]] = []
    previous = ""
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                names.setdefault(token.string, []).append((token.start, previous == "."))
            elif token.type == tokenize.COMMENT:
                texts.append((True, token.string, token.start))
            elif token.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE):
                texts.append((False, token.string, token.start))
            if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT):
                previous = token.string
    except tokenize.TokenError, SyntaxError, ValueError:
        return None
    return _Lexed(names, texts)


def _outside(lexed: _Lexed, name: str, member_only: bool) -> set[tuple[int, int]]:
    """Find the occurrences of `name` that cannot refer to the unit, as `_not_code` says.

    Args:
        lexed: The tokens of the file.
        name: The bare name.
        member_only: Whether the unit is a method and the file is not the unit's own.

    Returns:
        (line, column) of each such occurrence, 1-based line and 0-based column.
    """
    word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    found: set[tuple[int, int]] = set()
    if member_only:
        found.update(start for start, after_dot in lexed.names.get(name, ()) if not after_dot)
    for comment, text, (row, column) in lexed.texts:
        if name not in text:
            continue
        if not comment:
            inner = text.strip("rbfuRBFU").strip("\"'")
            if inner == name or inner.endswith(f".{name}"):
                continue
        for offset, line in enumerate(text.split("\n")):
            base = column if offset == 0 else 0
            found.update((row + offset, base + hit.start()) for hit in word.finditer(line))
    return found
