"""Enforces the documentation rule of CLAUDE.md for `src/`: every function and method (also
private and nested ones) has a Google-style docstring that documents every parameter in `Args:`,
the return value in `Returns:` (or `Yields:` for generators) unless it returns None.

Ruff's `D`/`DOC` rules check the format and `Raises:`; this test checks completeness.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

SECTION = re.compile(
    r"^\s*(Args|Arguments|Returns|Yields|Raises|Attributes|Example|Examples|Note)"
    r":\s*$",
    re.MULTILINE,
)


def _sections(doc: str) -> dict[str, str]:
    parts = SECTION.split(doc)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    names = [p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return [n for n in names if n not in ("self", "cls")]


def _returns_value(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if fn.name == "__init__":
        return False
    if fn.returns is not None:
        text = ast.unparse(fn.returns)
        return text not in ("None", "NoReturn", "Never")
    return False


def _is_generator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Yield | ast.YieldFrom):
            return True
    return False


def _problems(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        where = f"{path.relative_to(SRC)}:{node.lineno} {node.name}"
        doc = ast.get_docstring(node)
        if not doc:
            problems.append(f"{where}: no docstring")
            continue
        sections = _sections(doc)
        params = _params(node)
        if params:
            args = sections.get("Args", "") + sections.get("Arguments", "")
            missing = [p for p in params if not re.search(rf"^\s*{re.escape(p)}\b", args, re.M)]
            if missing:
                problems.append(f"{where}: Args missing {missing}")
        if _returns_value(node):
            key = "Yields" if _is_generator(node) else "Returns"
            if key not in sections and not ("Yields" in sections or "Returns" in sections):
                problems.append(f"{where}: no {key} section")
    return problems


def test_all_functions_have_contract_docstrings():
    problems = [p for f in sorted(SRC.rglob("*.py")) for p in _problems(f)]
    assert not problems, "\n" + "\n".join(problems)
