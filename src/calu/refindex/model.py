"""The types every reference index shares, and the interface `ReferenceIndex`.

Nothing here reads a file or starts a program.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Unit:
    """One unit of code whose referrers are asked for: a function, a method or a class."""

    path: str
    """The file that defines it, relative to the project directory."""
    qualname: str
    """Its dotted name inside the file, e.g. "Parser.parse"."""
    line: int = 0
    """The line of its definition (1-based); 0 if not known. A hit on that line of that file
    is the definition itself and no reference."""

    @property
    def name(self) -> str:
        """The bare name, the last part of `qualname`.

        Returns:
            E.g. "parse" for "Parser.parse".
        """
        return self.qualname.rpartition(".")[2]


@dataclass(frozen=True)
class Place:
    """One occurrence of a unit's name in a file of the project."""

    path: str
    """The file, relative to the project directory."""
    line: int
    """The line of the occurrence (1-based)."""
    column: int
    """The column of the name's first character in that line (0-based)."""
    text: str = ""
    """The line itself, stripped and cut to `PLACE_TEXT_CHARS`, for a person or a checker."""


PLACE_TEXT_CHARS = 200
"""The longest `Place.text`."""


@dataclass(frozen=True)
class Referrers:
    """What an index says about the references of one unit."""

    referrers: tuple[Place, ...] = ()
    """Places the index knows to reference the unit."""
    unclear: tuple[Place, ...] = ()
    """Places that hold the unit's name and that the index could neither confirm nor dismiss.
    A checker has to read them."""
    dismissed: int = 0
    """How many occurrences of the name the index left out: the definition itself, and the
    ones in a comment or a string."""
    complete: bool = True
    """False if the index did not look at every file (a limit was reached, or files could not
    be listed or read). Places may then be missing, and `problem` says why."""
    problem: str = ""
    """Why `complete` is False; "" otherwise."""


class ReferenceIndex(Protocol):
    """An index of the references between the units of one project directory."""

    def referrers(self, unit: Unit) -> Referrers:
        """Find the places that reference `unit`, as the files are now.

        An implementation reads the working files, uncommitted changes and untracked files
        included, at the time of the call.

        Args:
            unit: The unit.

        Returns:
            The confirmed and the unclear places. Never raises for a file or tool that
            fails: the answer is then not `complete` and names the problem.
        """
        ...
