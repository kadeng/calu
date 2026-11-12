"""The definitions an edit changes, with their contract as it will be after the edit.

A diff shows the lines that change. The contract of the function those lines belong to -- its
signature and its docstring -- is often outside the diff, so a checker that judges "does this
code still do what it says" would judge without the text that says it. This module finds, for
a Python file before and after an edit, the functions, methods and classes the edit changes and
says for each which part changed:

    before, after  ->  [ChangedDefinition(name, line, header, docstring,
                                          header_changed, docstring_changed, body_changed)]

It reads only the two texts it is given and parses them with the standard library's `ast`;
nothing is imported or run. Other languages yield nothing.
"""

import ast
from dataclasses import dataclass

type Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
"""A syntax node that carries a contract: a function, a method or a class."""

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
"""The classes of `Definition`, for `isinstance`."""

MAX_SOURCE_CHARS = 1_000_000
"""Files longer than this are not parsed: a diff in such a file is judged without this help."""


@dataclass(frozen=True)
class ChangedDefinition:
    """One function, method or class that an edit changes."""

    name: str
    """Its dotted name inside the file, e.g. "Parser.parse"."""
    kind: str
    """"function" or "class"."""
    line: int
    """The line of its `def` or `class` keyword in the file after the edit (1-based)."""
    header: str
    """Its source from the first decorator to the line before its body, after the edit."""
    docstring: str | None
    """Its docstring after the edit, cleaned as `ast.get_docstring` does; None if it has none."""
    header_changed: bool
    """Whether its decorators, name, parameters, bases or return annotation changed."""
    docstring_changed: bool
    body_changed: bool
    """Whether its code changed, the docstring and nested definitions aside."""

    @property
    def what_changed(self) -> str:
        """One clause that says which parts changed and which did not.

        Returns:
            E.g. "the body changed; the signature and the docstring did not".
        """
        parts = (
            ("signature" if self.kind == "function" else "class line", self.header_changed),
            ("docstring", self.docstring_changed),
            ("body", self.body_changed),
        )
        changed = [name for name, flag in parts if flag]
        same = [name for name, flag in parts if not flag]
        if self.docstring is None and "docstring" in same:
            same.remove("docstring")
        clause = f"the {_and(changed)} changed"
        if same:
            clause += f"; the {_and(same)} did not"
        if self.docstring is None:
            clause += "; it has no docstring"
        return clause


def changed_definitions(before: str, after: str, path: str) -> list[ChangedDefinition]:
    """List the definitions of a Python file that differ between two versions of it.

    A definition counts when it exists in both versions under the same dotted name and its
    header, docstring or own code differs. A definition that is new in `after` is left out: an
    edit that adds it shows all of it. A change inside a nested definition counts for the
    nested one only, so a method's change does not also list its class.

    Args:
        before: The file's content before the edit.
        after: The file's content after the edit.
        path: The file's name; only `.py` and `.pyi` files are read.

    Returns:
        The changed definitions in the order of the file after the edit. [] for another file
        type, for a file over `MAX_SOURCE_CHARS`, and when either version is not valid Python
        (then there is nothing reliable to say).
    """
    if not path.endswith((".py", ".pyi")):
        return []
    if max(len(before), len(after)) > MAX_SOURCE_CHARS:
        return []
    try:
        old = _definitions(ast.parse(before))
        new_tree = ast.parse(after)
    except SyntaxError, ValueError, RecursionError:
        return []
    lines = after.splitlines()
    found: list[ChangedDefinition] = []
    for name, node in _definitions(new_tree).items():
        earlier = old.get(name)
        if earlier is None or type(earlier) is not type(node):
            continue
        header_changed = _header_key(node) != _header_key(earlier)
        docstring = ast.get_docstring(node)
        docstring_changed = docstring != ast.get_docstring(earlier)
        body_changed = _body_key(node) != _body_key(earlier)
        if not (header_changed or docstring_changed or body_changed):
            continue
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        found.append(
            ChangedDefinition(
                name=name,
                kind="class" if isinstance(node, ast.ClassDef) else "function",
                line=node.lineno,
                header="\n".join(lines[first - 1 : node.body[0].lineno - 1]) or lines[first - 1],
                docstring=docstring,
                header_changed=header_changed,
                docstring_changed=docstring_changed,
                body_changed=body_changed,
            )
        )
    return sorted(found, key=lambda item: item.line)


def _definitions(tree: ast.AST, prefix: str = "") -> dict[str, Definition]:
    """Collect the definitions of a syntax tree by dotted name.

    Args:
        tree: A module or a definition.
        prefix: The dotted name of `tree`, with a trailing dot, or "" for a module.

    Returns:
        Dotted name -> node, for every function, method and class at any depth. Of two
        definitions with the same name (an overload, a property setter) the last one is kept,
        which is the one the name refers to at run time.
    """
    found: dict[str, Definition] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, DEFINITIONS):
            name = prefix + node.name
            found[name] = node
            found.update(_definitions(node, name + "."))
        elif not isinstance(node, ast.expr):
            # Definitions inside `if`, `try` and `with` blocks belong to the enclosing scope.
            found.update(_definitions(node, prefix))
    return found


def _header_key(node: Definition) -> str:
    """Render what a definition's header says, independent of layout.

    Args:
        node: A function or class definition.

    Returns:
        A text that is equal for two headers that differ only in formatting.
    """
    if isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list, *node.bases, *node.keywords]
        return node.name + "|" + "|".join(ast.dump(part) for part in parts)
    parts = [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]
    return type(node).__name__ + node.name + "|" + "|".join(ast.dump(part) for part in parts)


def _body_key(node: Definition) -> str:
    """Render a definition's own code, without its docstring and nested definitions.

    Args:
        node: A function or class definition.

    Returns:
        A text that is equal for two bodies that differ only in formatting, comments, the
        docstring, or the inside of a nested definition (which is a definition of its own).
    """
    body: list[ast.stmt] = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "|".join(
        f"<{type(part).__name__} {part.name}>" if isinstance(part, DEFINITIONS) else ast.dump(part)
        for part in body
    )


def _and(words: list[str]) -> str:
    """Join words as an English list.

    Args:
        words: One or more words.

    Returns:
        "a", "a and b" or "a, b and c".
    """
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]
