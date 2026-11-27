"""The interface to a code navigator, and the reference index built on any navigator.

A code navigator is a program that understands the code of one language and can answer
four questions about a position in a file, as an editor asks them:

    references(position)       where is the name at this position used?
    definitions(position)      where is the name at this position defined?
    implementations(position)  which methods implement or override the method defined here?
    symbols(name)              where are functions and methods of this exact name defined?

A language server answers all four (`calu.refindex.lsp.LspNavigator`). `CodeNavigator` names
only these questions and the bookkeeping around them (start, readiness, changed files,
close), so another program that can answer them, such as jedi or a code index with type
information, can replace the language server without a change anywhere else.

`NavigatorIndex` turns a navigator into a reference index (`ReferenceIndex`) and a resolver
(`Resolver`):

1. **References of a unit**: the references of the unit's name, plus the references of each
   base-class or Protocol method that the unit implements. A call `store.save()` where
   `store` is only known by its base type is a reference of the base method, so it would
   otherwise be missed. The base methods are found by asking `symbols` for the unit's name
   and then `implementations` of each match.
2. **Resolving a place**: `definitions` at the place. If one of them is the unit, the place
   refers to it. If all of them are somewhere else, it does not. If there is none, the
   navigator does not know.

A navigator that is not ready answers with too little and without an error (measured with
basedpyright, `spikes/RESULTS.md`). So `NavigatorIndex` marks every answer of a navigator that
is not ready as not complete, and never resolves a place with it.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anyio

from calu.refindex.model import PLACE_TEXT_CHARS, Place, Referrers, Resolution, Unit


@dataclass(frozen=True)
class Position:
    """A position in a file: where a name begins."""

    path: str
    """The file, relative to the project directory. For a position outside the project (a
    library, the standard library) the absolute path, and `outside` is True."""
    line: int
    """The line (1-based)."""
    column: int
    """The column of the name's first character (0-based)."""
    outside: bool = False
    """True for a position that is not in the project directory."""


class NavigatorError(Exception):
    """The navigator cannot be started, or failed to answer a question."""


class CodeNavigator(Protocol):
    """A program that answers questions about names in the code of one language.

    Every question is about the files as they are on disk now. The methods block until the
    answer is there; `NavigatorIndex` calls them from a worker thread, one at a time.
    """

    def covers(self, path: str) -> bool:
        """Say whether the navigator understands a file.

        Args:
            path: The file, relative to the project directory.

        Returns:
            True for a file of its language.
        """
        ...

    def start(self) -> None:
        """Start the navigator if it does not run yet, and wait until it is ready.

        The wait has a time limit; after it the navigator runs but is not `ready`.

        Raises:
            NavigatorError: If it is not installed or cannot be started.
        """
        ...

    @property
    def ready(self) -> bool:
        """Say whether the navigator has read the whole project.

        Returns:
            True when it has. A navigator that is not ready may answer with too little.
        """
        ...

    def references(self, at: Position) -> list[Position]:
        """List the uses of the name at `at`, without its definition.

        Args:
            at: The position of a name, usually of a definition.

        Returns:
            The positions of the uses inside the project.

        Raises:
            NavigatorError: If the navigator fails.
        """
        ...

    def definitions(self, at: Position) -> list[Position]:
        """List where the name at `at` is defined.

        Args:
            at: The position of a name.

        Returns:
            The definitions, inside or outside the project; empty if the navigator does not
            know.

        Raises:
            NavigatorError: If the navigator fails.
        """
        ...

    def implementations(self, at: Position) -> list[Position]:
        """List the methods that implement or override the method defined at `at`.

        Args:
            at: The position of a method's name in its definition.

        Returns:
            The positions of their names, inside the project.

        Raises:
            NavigatorError: If the navigator fails.
        """
        ...

    def symbols(self, name: str) -> list[Position]:
        """List the functions and methods whose name is exactly `name`.

        Args:
            name: The bare name.

        Returns:
            The positions of their names in their definitions, inside the project.

        Raises:
            NavigatorError: If the navigator fails.
        """
        ...

    def changed(self, paths: list[str]) -> None:
        """Note that files were written, created or deleted. Never raises.

        Args:
            paths: The files, relative to the project directory.
        """
        ...

    def close(self) -> None:
        """Stop the navigator. Never raises."""
        ...


NOT_READY = (
    "The code navigator did not report that it has read the project, so references may be missing."
)
"""The problem of an answer from a navigator that is not ready."""


@dataclass
class NavigatorIndex:
    """A reference index and a resolver on a code navigator (see the module docstring).

    Use it as a context manager, or call `close`, so that the navigator is stopped.
    """

    root: Path
    """The project directory."""
    navigator: CodeNavigator
    """The program that answers the questions."""
    _one_at_a_time: anyio.Lock = field(default_factory=anyio.Lock, init=False, repr=False)

    def __enter__(self) -> NavigatorIndex:
        """Enter the context; the navigator starts with the first question.

        Returns:
            The index.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the navigator.

        Args:
            *_exc: The exception of the block, unused.
        """
        self.close()

    def close(self) -> None:
        """Stop the navigator. Never raises."""
        self.navigator.close()

    def changed(self, paths: list[str]) -> None:
        """Pass on that files were written, created or deleted. Never raises.

        Args:
            paths: The files, relative to the project directory.
        """
        self.navigator.changed(paths)

    async def referrers(self, unit: Unit) -> Referrers:
        """Find the places that reference `unit`, in a worker thread, one question at a time.

        Side effects: those of `find`.

        Args:
            unit: The unit.

        Returns:
            As `find`.
        """
        async with self._one_at_a_time:
            return await anyio.to_thread.run_sync(self.find, unit)

    def find(self, unit: Unit) -> Referrers:
        """Find the places that reference `unit`; blocks while the navigator works.

        Side effects: starts the navigator if needed; reads the project's files for the text
        of each place.

        Args:
            unit: The unit; its `line` must be known.

        Returns:
            The references of the unit and of every base-class or Protocol method it
            implements, as referrers; none unclear. Not `complete`, with the reason, when the
            navigator cannot be started, fails, is not ready, or the unit's name is not on
            its line.
        """
        try:
            self.navigator.start()
            own = Position(unit.path, unit.line, self._column(unit.path, unit.line, unit.name))
            found = set(self.navigator.references(own))
            for base in self._bases(unit, own):
                found |= set(self.navigator.references(base))
        except NavigatorError as exc:
            return Referrers(complete=False, problem=str(exc))
        spots = sorted(
            (spot.path, spot.line, spot.column)
            for spot in found
            if not spot.outside and (spot.path, spot.line) != (unit.path, unit.line)
        )
        places = tuple(self._place(*spot) for spot in spots)
        if not self.navigator.ready:
            return Referrers(referrers=places, complete=False, problem=NOT_READY)
        return Referrers(referrers=places)

    async def resolve(self, place: Place, unit: Unit) -> Resolution:
        """Say what the name at `place` refers to, in a worker thread, one question at a time.

        Side effects: those of `resolve_blocking`.

        Args:
            place: An occurrence of the unit's name.
            unit: The unit that was asked about.

        Returns:
            As `resolve_blocking`.
        """
        async with self._one_at_a_time:
            return await anyio.to_thread.run_sync(self.resolve_blocking, place, unit)

    def resolve_blocking(self, place: Place, unit: Unit) -> Resolution:
        """Ask where the name at `place` is defined, and compare that with `unit`.

        Side effects: starts the navigator if needed.

        Args:
            place: An occurrence of the unit's name.
            unit: The unit; its `line` must be known.

        Returns:
            `UNIT` if one of the definitions is the unit; `OTHER` if there are definitions
            and none is the unit (one outside the project included); `UNKNOWN` if there is
            none, the navigator does not cover the file, is not ready, or fails. A call
            through a base class is defined at the base method and so is `OTHER` here;
            `find` is what reports such calls.
        """
        if not self.navigator.covers(place.path):
            return Resolution.UNKNOWN
        try:
            self.navigator.start()
            if not self.navigator.ready:
                return Resolution.UNKNOWN
            found = self.navigator.definitions(Position(place.path, place.line, place.column))
        except NavigatorError:
            return Resolution.UNKNOWN
        if not found:
            return Resolution.UNKNOWN
        if any(
            not spot.outside and (spot.path, spot.line) == (unit.path, unit.line) for spot in found
        ):
            return Resolution.UNIT
        return Resolution.OTHER

    def _bases(self, unit: Unit, own: Position) -> list[Position]:
        """Find the base-class and Protocol methods that the unit implements.

        Args:
            unit: The unit.
            own: The position of the unit's name in its definition.

        Returns:
            Each function or method of the same name whose implementations include the unit.

        Raises:
            NavigatorError: If the navigator fails.
        """
        bases: list[Position] = []
        for symbol in self.navigator.symbols(unit.name):
            if (symbol.path, symbol.line) == (own.path, own.line):
                continue
            if any(
                (spot.path, spot.line) == (own.path, own.line)
                for spot in self.navigator.implementations(symbol)
            ):
                bases.append(symbol)
        return bases

    def _column(self, path: str, line: int, name: str) -> int:
        """Find the column of a name on a line of a file of the project.

        Args:
            path: The file, relative to the project directory.
            line: The 1-based line.
            name: The bare name.

        Returns:
            The 0-based column of its first occurrence as a whole word.

        Raises:
            NavigatorError: If the file cannot be read or the name is not on that line.
        """
        column = name_column(self.root, path, line, name)
        if column is None:
            raise NavigatorError(f"{name} is not on line {line} of {path}.")
        return column

    def _place(self, path: str, line: int, column: int) -> Place:
        """Build the place of one reference, with the text of its line.

        Args:
            path: The file, relative to the project directory.
            line: The 1-based line.
            column: The 0-based column.

        Returns:
            The place; its text is "" if the file or the line cannot be read.
        """
        try:
            lines = (self.root / path).read_text("utf-8", errors="replace").splitlines()
            text = lines[line - 1].strip()[:PLACE_TEXT_CHARS]
        except OSError, IndexError:
            text = ""
        return Place(path, line, column, text)


def name_column(root: Path, path: str, line: int, name: str) -> int | None:
    """Find the column of a name on one line of a file.

    Args:
        root: The project directory.
        path: The file, relative to `root`.
        line: The 1-based line.
        name: The bare name.

    Returns:
        The 0-based column of its first occurrence as a whole word; None if the file or the
        line cannot be read or the name is not there.
    """
    try:
        text = (root / path).read_text("utf-8", errors="replace").splitlines()[line - 1]
    except OSError, IndexError:
        return None
    found = re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text) if line >= 1 else None
    return found.start() if found else None
