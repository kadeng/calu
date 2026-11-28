"""Which code has to be judged again for honesty after a unit's contract changed.

The contract of a function, method or class is its name, its signature and its doc comment.
Code that uses the unit was judged honest against that contract. When an edit changes it,
the code that uses the unit may no longer say what it does, or may say it again. This module
finds that code (the owner's requirement 50, PLAN D294,
`agent/records/notes/honesty-dependencies-design.md`, section 3).

How it works:

1. At the gate, before an edit runs, `contract_changes` compares the file's text before the
   edit with the text the edit will write. It reports each definition whose signature or doc
   comment changes, and each definition that the edit removes or renames. A change of the
   body alone concerns only that unit, so it is not reported.
2. After the edit succeeded, `find_dependents` asks a reference index (`calu.refindex`)
   which places use the unit. Every place counts, also the ones the index could not decide.
   Judging a unit once too often is cheaper than missing one.
3. Each place is turned into the unit that contains it (`enclosing`): the function, method
   or class around it, or the top level of the file. A place in an `import` statement is
   left out: importing a unit does not rely on what it does. An import under another name,
   and a top-level assignment of the unit to a name, lead to the units that use that name.
   The session records those units in the honesty state as units to judge again
   (`HonestyState.add_recheck`).

Who has to name a side effect of a function that is passed on (the owner, requirement 54):
the unit that passes it. A receiver such as a sort that calls its comparator stays honest as
long as its contract says that it calls the function it is given. So only the places that
name the changed unit matter, and those are what the search finds.

What it does not cover yet:

- Only Python files. Other languages report no contract change.
- A name that reaches the unit through more than one file (`b.py` assigns it, `a.py` imports
  that name from `b.py`): only the first file is followed.
- Code in other languages that calls the unit (the owner's decision of 2026-11-27: same
  language first).
"""

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from calu.checkers.symbols import changed_definitions, definition_lines, parsed_definitions
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
    """The line of the definition after the edit (1-based); 0 if the edit removed it."""
    before: str
    """Its decorators, signature and doc comment before the edit."""
    after: str
    """The same after the edit; "" if the edit removed or renamed the definition."""

    @property
    def cause(self) -> str:
        """Name the changed unit for the honesty state.

        Returns:
            "path:dotted.name".
        """
        return f"{self.path}:{self.qualname}"


def contract_changes(path: str, before: str, after: str) -> list[ContractChange]:
    """List the definitions of a Python file whose name, signature or doc comment changes.

    A definition that exists before the edit and not after it was removed or renamed. It is
    listed with line 0 and an empty `after`, so that the code that still uses its old name is
    judged again. Of a removed class only the class is listed, not its methods.

    Args:
        path: The file, relative to the project directory.
        before: The file's text before the edit.
        after: The file's text after the edit.

    Returns:
        First the changed definitions in the order of the file after the edit, then the
        removed ones. [] for a file that is not Python, is too large, or does not parse
        before or after the edit (`calu.checkers.symbols.changed_definitions`).
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
    # A definition that is gone was removed or renamed: the code that still uses the old
    # name relied on a contract that no longer exists.
    now = definition_lines(after, path)
    old = parsed_definitions(before, path)
    if now is not None and old is not None:
        lines = before.splitlines()
        for name, node in old[1].items():
            if name in now or _inside_gone(name, old[1], now):
                continue
            header = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else name
            found.append(
                ContractChange(
                    path=path,
                    qualname=name,
                    line=0,
                    before=_contract(header, ast.get_docstring(node)),
                    after="",
                )
            )
    return found


def _inside_gone(name: str, earlier: Mapping[str, object], now: Mapping[str, int]) -> bool:
    """Tell whether a definition sits inside another definition that is gone as well.

    Args:
        name: The dotted name of the definition.
        earlier: The definitions before the edit, by dotted name.
        now: The definitions after the edit, by dotted name.

    Returns:
        True if an enclosing definition existed before and is gone now: only the outermost
        gone definition is reported.
    """
    parts = name.split(".")
    for end in range(1, len(parts)):
        outer = ".".join(parts[:end])
        if outer in earlier and outer not in now:
            return True
    return False


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
        Three more kinds of use are followed within the file of a place:

        - an import that binds the unit under another name (`import f as quiet`): the units
          that use that name;
        - a statement at the top level of a file that assigns the unit, or something that
          holds it, to a name (`printer = f`, `HANDLERS = {"x": "f"}`): the units that use
          that name;
        - for a method with a double-underscore name (`__call__`, `__iter__`), which code
          calls without naming it: the units that use its class.
    """
    targets = [Unit(change.path, change.qualname, change.line)]
    owner = _dunder_owner(change.qualname)
    if owner:
        targets.append(Unit(change.path, owner, _definition_line(root, change.path, owner)))
    places = []
    complete, problem = True, ""
    for target in targets:
        found = await index.referrers(target)
        places.extend((*found.referrers, *found.unclear))
        complete = complete and found.complete
        problem = problem or found.problem
    trees: dict[str, ast.Module | None] = {}
    units: set[tuple[str, str]] = set()
    name = change.qualname.rsplit(".", 1)[-1]
    for place in places:
        if place.path not in trees:
            trees[place.path] = _parse(root, place.path)
        tree = trees[place.path]
        if tree is None:
            units.add((place.path, ""))
            continue
        if _in_import(tree, place.line):
            aliases = _aliases(tree, place.line, name)
            units.update((place.path, unit) for unit in _users(tree, aliases))
            continue
        unit = enclosing(tree, place.line)
        units.add((place.path, unit))
        if unit == "":
            assigned = _assigned(tree, place.line)
            units.update((place.path, user) for user in _users(tree, assigned))
    return Dependents(tuple(sorted(units)), complete=complete, problem=problem)


def _dunder_owner(qualname: str) -> str:
    """Name the class of a method that code calls without naming it.

    Args:
        qualname: The dotted name of the changed unit.

    Returns:
        The class's dotted name for a method such as `Cls.__call__`; "" for anything else,
        including `__init__`, which a call of the class names anyway.
    """
    owner, _, last = qualname.rpartition(".")
    if owner and last.startswith("__") and last.endswith("__") and last != "__init__":
        return owner
    return ""


def _definition_line(root: Path, path: str, qualname: str) -> int:
    """Find the line of a definition in a file of the project.

    Args:
        root: The project directory.
        path: The file, relative to it.
        qualname: The dotted name.

    Returns:
        The 1-based line of its `def` or `class`; 0 if the file does not define it.
    """
    try:
        text = (root / path).read_text("utf-8", errors="replace")
    except OSError:
        return 0
    return (definition_lines(text, path) or {}).get(qualname, 0)


def _aliases(tree: ast.Module, line: int, name: str) -> set[str]:
    """List the names under which an import statement binds a unit.

    Args:
        tree: The parsed file.
        line: A line of the import statement.
        name: The unit's bare name.

    Returns:
        The `as` names of the statement's entries that import `name`; empty if it imports
        it under its own name.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not node.lineno <= line <= (node.end_lineno or node.lineno):
            continue
        for alias in node.names:
            if alias.asname and alias.name.rsplit(".", 1)[-1] == name:
                found.add(alias.asname)
    return found


def _assigned(tree: ast.Module, line: int) -> set[str]:
    """List the names a top-level assignment binds, for the statement around a line.

    Args:
        tree: The parsed file.
        line: A line at the top level of the file.

    Returns:
        The names the statement assigns (`a = ...`, `a: T = ...`, `a, b = ...`); empty if
        the statement around the line is no assignment.
    """
    for node in tree.body:
        if not node.lineno <= line <= (node.end_lineno or node.lineno):
            continue
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            return set()
        return {
            leaf.id
            for target in targets
            for leaf in ast.walk(target)
            if isinstance(leaf, ast.Name)
        }
    return set()


def _users(tree: ast.Module, names: set[str]) -> set[str]:
    """Name the units of a file that read one of some names.

    Args:
        tree: The parsed file.
        names: Bare names bound in the file.

    Returns:
        The dotted name of the unit around each read of one of the names ("" for the top
        level of the file).
    """
    if not names:
        return set()
    return {
        enclosing(tree, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in names and isinstance(node.ctx, ast.Load)
    }


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


type ParsedFiles = dict[str, tuple[list[str], list[tuple[int, int]]]]
"""Per file what `enclosing_units` needs of it:

- the unit around each line (`_line_units`); empty for a file that cannot be parsed;
- the first and the last line of each of its import statements."""


def _line_units(tree: ast.Module) -> list[str]:
    """Name the innermost function, method or class around every line of a file.

    Args:
        tree: The parsed file.

    Returns:
        Entry `line - 1` is what `enclosing(tree, line)` returns. Lines after the last
        definition have no entry; they are at the top level.
    """
    names: list[str] = []

    def fill(scope: ast.AST, prefix: str) -> None:
        """Write the names of the definitions directly in a scope, then of those inside.

        Args:
            scope: A module, function or class.
            prefix: The dotted name of the scope with a trailing dot; "" for the module.
        """
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                end = node.end_lineno or node.lineno
                if len(names) < end:
                    names.extend([""] * (end - len(names)))
                names[node.lineno - 1 : end] = [name] * (end - node.lineno + 1)
                # After the outer name, so that the inner one replaces it on its lines.
                fill(node, f"{name}.")
            elif not isinstance(node, ast.expr):
                stack.extend(ast.iter_child_nodes(node))

    fill(tree, "")
    return names


def enclosing_units(
    root: Path, places: Sequence[tuple[str, int]], parsed: ParsedFiles | None = None
) -> list[str | None]:
    """Name the unit around each of many lines; each file is read and parsed once.

    Side effects: reads and parses the files of the places that `parsed` does not hold, and
    adds them to `parsed`.

    Args:
        root: The project directory.
        places: (path relative to the project directory, 1-based line) per place.
        parsed: Files parsed by earlier calls, to reuse. The caller keeps it only as long
            as it accepts that a file changed meanwhile is read as it was. None reads every
            file of this call once.

    Returns:
        Per place what `enclosing_unit` returns for it, in the same order.
    """
    known: ParsedFiles = {} if parsed is None else parsed
    found: list[str | None] = []
    for path, line in places:
        if path not in known:
            tree = _parse(root, path)
            spans = [
                (node.lineno, node.end_lineno or node.lineno)
                for node in (ast.walk(tree) if tree is not None else ())
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            known[path] = ([] if tree is None else _line_units(tree), spans)
        units, spans = known[path]
        if any(first <= line <= last for first, last in spans):
            found.append(None)
        else:
            found.append(units[line - 1] if 1 <= line <= len(units) else "")
    return found


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
