"""What a Python unit uses, read from its syntax alone.

The honesty graph shows, next to a unit that is not clean, the units it uses
(`calu.honesty_graph.add_neighbours`). This module finds them without an index and without a
model. It reads the calls in the unit's body and follows four kinds:

- a name the same file defines: `helper()`, `Parser()`, `Parser.make()`;
- a method on `self` or `cls` that the unit's own class defines: `self.save()`;
- a name the file imports from another file of the project:
  `from pkg.store import save`, then `save()`;
- a name reached through an imported module: `import pkg.store as store`, then
  `store.save()`.

Everything else is left out:

- a method on any other object (`reader.read()`);
- a method that only a base class defines;
- a function that is passed around as a value and never called by name;
- a name that another module only re-exports.

So the list is incomplete on purpose. It can also be wrong in one case: a local variable
that hides an imported or a module-level name is not noticed.

Only Python files are read. Nothing is imported or run.
"""

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from calu.checkers.symbols import Definition, parsed_definitions

MAX_USED = 40
"""The most units `used_units` returns for one unit."""

type Read = Callable[[str], str | None]
"""Returns the text of a file of the project by its relative path; None if it cannot be read."""


@dataclass(frozen=True)
class UsedUnit:
    """A unit that another unit calls."""

    path: str
    """The file that defines it, relative to the project directory."""
    symbol: str
    """Its dotted name in that file."""


@dataclass
class _Files:
    """The project's Python files as far as one lookup read them."""

    read: Read
    seen: dict[str, tuple[ast.Module, dict[str, Definition]] | None] = field(
        default_factory=dict[str, tuple[ast.Module, dict[str, Definition]] | None]
    )
    bound: dict[str, _Imports] = field(default_factory=dict[str, "_Imports"])

    def parsed(self, path: str) -> tuple[ast.Module, dict[str, Definition]] | None:
        """Return a file's syntax tree and definitions, read once.

        Args:
            path: The file, relative to the project directory.

        Returns:
            The tree and dotted name -> node; None for a file that is missing, not Python or
            does not parse.
        """
        if path not in self.seen:
            text = self.read(path)
            self.seen[path] = None if text is None else parsed_definitions(text, path)
        return self.seen[path]

    def imports(self, path: str) -> _Imports:
        """Return what the import statements of a file bind, collected once.

        Args:
            path: The file, relative to the project directory.

        Returns:
            The bound names; none for a file that cannot be parsed.
        """
        if path not in self.bound:
            found = self.parsed(path)
            self.bound[path] = _Imports() if found is None else _imports(found[0])
        return self.bound[path]

    def defines(self, path: str, symbol: str) -> bool:
        """Tell whether a file defines a dotted name.

        Args:
            path: The file, relative to the project directory.
            symbol: The dotted name.

        Returns:
            True if the file parses and defines it.
        """
        found = self.parsed(path)
        return found is not None and symbol in found[1]

    def module_file(self, importer: str, module: str, level: int = 0) -> str | None:
        """Find the project file of an imported module.

        An absolute import is looked for from the project directory and from every directory
        above the importing file (so `src/pkg/a.py` finds `pkg.b` in `src/pkg/b.py`). A
        relative import starts at the importing file's package.

        Args:
            importer: The importing file, relative to the project directory.
            module: The dotted module name; "" for `from . import x`.
            level: The number of leading dots of a relative import; 0 for an absolute one.

        Returns:
            The path of `module.py` or of the package's `__init__.py`; None if the project
            has neither, which is the case for the standard library and third-party code.
        """
        folders = importer.split("/")[:-1]
        if level:
            if level - 1 > len(folders):
                return None
            bases = ["/".join(folders[: len(folders) - (level - 1)])]
        else:
            bases = ["/".join(folders[:depth]) for depth in range(len(folders) + 1)]
        tail = module.replace(".", "/")
        for base in bases:
            stem = "/".join(part for part in (base, tail) if part)
            if not stem:
                continue
            for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                if self.parsed(candidate) is not None:
                    return candidate
        return None


@dataclass
class _Imports:
    """What the import statements of one file bind."""

    names: dict[str, tuple[str, int, str]] = field(default_factory=dict[str, tuple[str, int, str]])
    """Bound name -> (module, level, name in that module), from `from module import name`."""
    modules: dict[str, str] = field(default_factory=dict[str, str])
    """Bound dotted name -> module, from `import module` and `import module as name`."""


def _imports(tree: ast.Module) -> _Imports:
    """Collect what the import statements of a file bind, at any depth.

    Args:
        tree: The file's syntax tree.

    Returns:
        The bound names. `from module import *` binds nothing here.
    """
    found = _Imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    found.names[alias.asname or alias.name] = (
                        node.module or "",
                        node.level,
                        alias.name,
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.modules[alias.asname or alias.name] = alias.name
    return found


def _dotted(node: ast.expr) -> str | None:
    """Write a chain of names and attributes as a dotted name.

    Args:
        node: An expression.

    Returns:
        `a.b.c` for a chain of plain names and attributes; None for anything else (a call, a
        subscript, a literal).
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _own_class(symbol: str, definitions: dict[str, Definition]) -> str | None:
    """Name the class a unit belongs to.

    Args:
        symbol: The unit's dotted name.
        definitions: The definitions of its file.

    Returns:
        The dotted name of the innermost class around the unit, or of the unit itself if it
        is a class; None for a plain function.
    """
    parts = symbol.split(".")
    for end in range(len(parts), 0, -1):
        name = ".".join(parts[:end])
        if isinstance(definitions.get(name), ast.ClassDef):
            return name
    return None


class Callees:
    """Answers `used_units` for many units and reads each file of the project once."""

    def __init__(self, read: Read) -> None:
        """Start with no file read.

        Args:
            read: Returns the text of a project file by its relative path; None if it
                cannot be read.
        """
        self._files = _Files(read)

    def of(self, path: str, symbol: str) -> list[UsedUnit]:
        """List the units a Python unit calls, as `used_units` does.

        Side effects: reads the unit's file and the files it imports from, each once per
        `Callees` object.

        Args:
            path: The unit's file, relative to the project directory.
            symbol: The unit's dotted name in that file.

        Returns:
            What `used_units` returns.
        """
        return _used(self._files, path, symbol)


def used_units(read: Read, path: str, symbol: str) -> list[UsedUnit]:
    """List the units of the project that a Python unit calls, as far as its syntax tells.

    The module docstring says which calls are followed and which are not.

    Side effects: calls `read` for the unit's file and for the files it imports from.

    Args:
        read: Returns the text of a project file by its relative path; None if it cannot be
            read.
        path: The unit's file, relative to the project directory.
        symbol: The unit's dotted name in that file.

    Returns:
        The units, in the order of their first call, each once and without the unit itself;
        at most `MAX_USED`. Empty for a file that is not Python or does not parse, and for a
        name the file does not define.
    """
    return _used(_Files(read), path, symbol)


def _used(files: _Files, path: str, symbol: str) -> list[UsedUnit]:
    """Do the work of `used_units` on files that may be read already.

    Args:
        files: The project's files, with what was read of them so far.
        path: The unit's file, relative to the project directory.
        symbol: The unit's dotted name in that file.

    Returns:
        What `used_units` returns.
    """
    parsed = files.parsed(path)
    if parsed is None or symbol not in parsed[1]:
        return []
    definitions = parsed[1]
    imports = files.imports(path)
    own_class = _own_class(symbol, definitions)
    found: dict[UsedUnit, None] = {}

    def note(target: str, name: str) -> bool:
        """Record a unit if its file defines it.

        Args:
            target: The file that may define it.
            name: The dotted name.

        Returns:
            True if the file defines the name.
        """
        if not files.defines(target, name):
            return False
        if (target, name) != (path, symbol):
            found[UsedUnit(target, name)] = None
        return True

    def imported(bound: str, rest: str) -> bool:
        """Follow a name that an import statement bound.

        Args:
            bound: The name as the unit's file knows it.
            rest: The attributes after it, dotted; "" for a call of the name itself.

        Returns:
            True if a file of the project defines what is called.
        """
        if bound in imports.names:
            module, level, original = imports.names[bound]
            source = files.module_file(path, module, level)
            wanted = f"{original}.{rest}" if rest else original
            if source is not None and note(source, wanted):
                return True
            # `from pkg import module`, then `module.name()`.
            inner = f"{module}.{original}" if module else original
            source = files.module_file(path, inner, level)
            return bool(rest) and source is not None and note(source, rest)
        if bound in imports.modules and rest:
            source = files.module_file(path, imports.modules[bound])
            return source is not None and note(source, rest)
        return False

    for node in ast.walk(definitions[symbol]):
        if not isinstance(node, ast.Call):
            continue
        called = _dotted(node.func)
        if called is None:
            continue
        head, _, rest = called.partition(".")
        if head in ("self", "cls") and rest and own_class is not None:
            note(path, f"{own_class}.{rest}")
            continue
        # A definition inside the unit, then one of the file, then an imported one.
        if note(path, f"{symbol}.{called}") or note(path, called):
            continue
        # `import a.b` binds the dotted name `a.b`: try the longest module name first.
        parts = called.split(".")
        for end in range(len(parts), 0, -1):
            if imported(".".join(parts[:end]), ".".join(parts[end:])):
                break
        if len(found) >= MAX_USED:
            break
    return list(found)[:MAX_USED]
