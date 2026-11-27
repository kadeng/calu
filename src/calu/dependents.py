"""Which code has to be judged again for honesty after a unit's contract changed.

The contract of a function, method or class is its name, its signature and its doc comment.
Code that uses the unit was judged honest against that contract. When an edit changes it,
the code that uses the unit may no longer say what it does, or may say it again. This module
finds that code (the owner's requirement 50, PLAN D294,
`agent/records/notes/honesty-dependencies-design.md`, section 3).

How it works:

1. At the gate, before an edit runs, `contract_changes` compares the file's text before the
   edit with the text the edit will write. It reports each definition whose signature or doc
   comment changes. A change of the body alone concerns only that unit, so it is not
   reported.
2. After the edit succeeded, `find_dependents` asks a reference index (`calu.refindex`)
   which places use the unit. Every place counts, also the ones the index could not decide.
   Judging a unit once too often is cheaper than missing one.
3. Each place is turned into the unit that contains it (`enclosing`): the function, method
   or class around it, or the top level of the file. A place in an `import` statement is
   left out: importing a unit does not rely on what it does. The session records those units in
   the honesty state as units to judge again (`HonestyState.add_recheck`).

What it does not cover yet:

- Only Python files. Other languages report no contract change.
- A unit that is removed or renamed: there is no definition with the old name after the edit
  to compare. Its callers then refer to a name that no longer exists, which running the code
  shows.
- Code in other languages that calls the unit (the owner's decision of 2026-11-27: same
  language first).
"""

import ast
from dataclasses import dataclass
from pathlib import Path

from calu.checkers.symbols import changed_definitions
from calu.refindex import ReferenceIndex, Unit

type DEFINITION = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
"""A syntax node that names a unit."""

MAX_SOURCE_BYTES = 2_000_000
"""Files larger than this are not parsed to find the unit around a place; such a place counts
for the top level of its file."""


@dataclass(frozen=True)
class ContractChange:
    """One definition whose signature or doc comment an edit changes."""

    path: str
    """The file, relative to the project directory."""
    qualname: str
    """The dotted name in the file, e.g. "Parser.parse"."""
    line: int
    """The line of the definition after the edit (1-based)."""
    before: str
    """Its decorators, signature and doc comment before the edit."""
    after: str
    """The same after the edit."""

    @property
    def cause(self) -> str:
        """Name the changed unit for the honesty state.

        Returns:
            "path:dotted.name".
        """
        return f"{self.path}:{self.qualname}"


def contract_changes(path: str, before: str, after: str) -> list[ContractChange]:
    """List the definitions of a Python file whose signature or doc comment changes.

    Args:
        path: The file, relative to the project directory.
        before: The file's text before the edit.
        after: The file's text after the edit.

    Returns:
        One entry per such definition, in the order of the file after the edit. [] for a file
        that is not Python, is too large, or does not parse before or after the edit
        (`calu.checkers.symbols.changed_definitions`).
    """
    later = changed_definitions(before, after, path)
    earlier = {item.name: item for item in changed_definitions(after, before, path)}
    found: list[ContractChange] = []
    for item in later:
        if not (item.header_changed or item.docstring_changed) or item.name not in earlier:
            continue
        old = earlier[item.name]
        found.append(
            ContractChange(
                path=path,
                qualname=item.name,
                line=item.line,
                before=_contract(old.header, old.docstring),
                after=_contract(item.header, item.docstring),
            )
        )
    return found


@dataclass(frozen=True)
class Dependents:
    """The units that use a changed unit, as far as the index could tell."""

    units: tuple[tuple[str, str], ...]
    """(path, dotted name or "" for the top level of the file) of each unit, sorted."""
    complete: bool = True
    """False if the index did not look at every file; units may then be missing."""
    problem: str = ""
    """Why `complete` is False, or a note from a stage of the index that could not help."""


async def find_dependents(index: ReferenceIndex, root: Path, change: ContractChange) -> Dependents:
    """Find the units that use a changed unit.

    Side effects: those of the index (reading the project's files, asking a code navigator,
    model calls); reads the files of the places to find the unit around each.

    Args:
        index: The reference index to ask.
        root: The project directory.
        change: The changed unit.

    Returns:
        The units around every place the index reported, confirmed or unclear, except places
        in an `import` statement. The changed unit itself is left out, unless it uses itself.
    """
    found = await index.referrers(Unit(change.path, change.qualname, change.line))
    trees: dict[str, ast.Module | None] = {}
    units: set[tuple[str, str]] = set()
    for place in (*found.referrers, *found.unclear):
        if place.path not in trees:
            trees[place.path] = _parse(root, place.path)
        tree = trees[place.path]
        if tree is not None and _in_import(tree, place.line):
            continue
        units.add((place.path, enclosing(tree, place.line)))
    return Dependents(tuple(sorted(units)), complete=found.complete, problem=found.problem)


def enclosing(tree: ast.Module | None, line: int) -> str:
    """Name the innermost function, method or class that contains a line.

    Args:
        tree: The parsed file; None for a file that could not be parsed.
        line: The 1-based line.

    Returns:
        Its dotted name, e.g. "Parser.parse"; "" when the line is at the top level of the
        file, or the file could not be parsed.
    """
    names: list[str] = []
    scope: ast.AST | None = tree
    while scope is not None and (inner := _definition_at(scope, line)) is not None:
        names.append(inner.name)
        scope = inner
    return ".".join(names)


def enclosing_unit(root: Path, path: str, line: int) -> str | None:
    """Name the unit around a line of a file of the project.

    Side effects: reads and parses the file.

    Args:
        root: The project directory.
        path: The file, relative to it.
        line: The 1-based line.

    Returns:
        The dotted name of the innermost function, method or class around the line. "" for
        the top level of the file, and for a file that is not Python or does not parse. None
        if the line belongs to an `import` statement, which uses nothing.
    """
    tree = _parse(root, path)
    if tree is not None and _in_import(tree, line):
        return None
    return enclosing(tree, line)


def _definition_at(scope: ast.AST, line: int) -> DEFINITION | None:
    """Find the definition directly in a scope that contains a line.

    A definition inside an `if`, `try`, `with` or loop block belongs to the scope around the
    block, as in `calu.checkers.symbols`.

    Args:
        scope: A module, function or class.
        line: The 1-based line.

    Returns:
        The function or class, or None if the line is in the scope's own code.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                return node
        elif not isinstance(node, ast.expr):
            stack.extend(ast.iter_child_nodes(node))
    return None


def _in_import(tree: ast.Module, line: int) -> bool:
    """Say whether a line belongs to an `import` or `from ... import` statement.

    Args:
        tree: The parsed file.
        line: The 1-based line.

    Returns:
        True if an import statement spans the line.
    """
    return any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and node.lineno <= line <= (node.end_lineno or node.lineno)
        for node in ast.walk(tree)
    )


def _parse(root: Path, path: str) -> ast.Module | None:
    """Parse one Python file of the project.

    Args:
        root: The project directory.
        path: The file, relative to it.

    Returns:
        The syntax tree; None for another file type, a file that is too large or cannot be
        read, or one that does not parse.
    """
    if not path.endswith((".py", ".pyi")):
        return None
    try:
        data = (root / path).read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            return None
        return ast.parse(data.decode("utf-8", errors="replace"))
    except OSError, SyntaxError, ValueError, RecursionError:
        return None


def _contract(header: str, docstring: str | None) -> str:
    """Write a definition's contract as one text.

    Args:
        header: Its decorators and signature, as in the file.
        docstring: Its docstring, cleaned; None if it has none.

    Returns:
        The header, and the docstring below it in triple quotes if there is one.
    """
    if docstring is None:
        return header
    return f'{header}\n    """{docstring}"""'
