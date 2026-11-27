"""Finding the code to judge again after a contract change (`calu.dependents`)."""

import ast
from pathlib import Path

import pytest

from calu import dependents
from calu.dependents import (
    ParsedFiles,
    contract_changes,
    enclosing,
    enclosing_unit,
    enclosing_units,
    find_dependents,
)
from calu.refindex import SearchIndex

pytestmark = pytest.mark.anyio

STORE = '''class Store:
    def save(self, x):
        """Save x."""
        return x

    def load(self):
        """Load."""
        return 1
'''


def test_only_a_changed_signature_or_doc_comment_is_a_contract_change() -> None:
    """A new docstring and a new parameter change the contract; a change of the body alone,
    and a new definition, do not."""
    after = STORE.replace('"""Save x."""', '"""Save x and send it to the server."""').replace(
        "return 1", "return 2"
    )
    after += "\n\ndef fresh():\n    return 0\n"
    (change,) = contract_changes("store.py", STORE, after)
    assert (change.qualname, change.line, change.cause) == ("Store.save", 2, "store.py:Store.save")
    assert '"""Save x."""' in change.before and "send it to the server" in change.after
    renamed = STORE.replace("def save(self, x)", "def save(self, x, y=0)")
    assert [c.qualname for c in contract_changes("store.py", STORE, renamed)] == ["Store.save"]
    assert contract_changes("store.rs", STORE, after) == []


def test_the_unit_around_a_line_is_the_innermost_definition() -> None:
    """A line in a method belongs to the method, a line in a function nested in an `if` to
    that function, a line at the top level to no unit."""
    tree = ast.parse(
        "x = 1\n"
        "class A:\n"
        "    def f(self):\n"
        "        return 1\n"
        "if True:\n"
        "    def g():\n"
        "        return 2\n"
    )
    assert enclosing(tree, 4) == "A.f"
    assert enclosing(tree, 2) == "A"
    assert enclosing(tree, 7) == "g"
    assert enclosing(tree, 1) == ""
    assert enclosing(None, 4) == ""


TRICKY = '''import os
from a import (
    b,
    c,
)


@decorator(
    1,
)
class Outer:
    """Doc."""

    x = 1

    @property
    def value(self):
        def inner():
            return 1

        return inner()

    class Nested:
        async def run(self):
            import json

            return json

    def same(self):
        return 1

    def same(self, more=None):
        return 2


if os.name:

    def conditional():
        return [n for n in range(3)]

else:
    try:

        def conditional():
            return lambda: 1

    finally:
        pass

tail = 1
'''


@pytest.mark.parametrize(
    "source",
    [TRICKY, Path(dependents.__file__).read_text("utf-8")],
    ids=["tricky", "dependents.py"],
)
def test_many_lines_are_named_as_one_line_is(source: str, tmp_path: Path) -> None:
    """`enclosing_units` reads a table per file. For every line of a file the table gives
    what the walk through the syntax tree gives: decorators, nested classes, definitions in
    `if` and `try` blocks, a name defined twice, and lines past the last definition. A line
    of an `import` statement names no unit, as for `enclosing_unit`."""
    (tmp_path / "m.py").write_text(source, "utf-8")
    count = len(source.splitlines())
    lines = list(range(1, count + 5))
    one_by_one = [enclosing_unit(tmp_path, "m.py", line) for line in lines]
    assert enclosing_units(tmp_path, [("m.py", line) for line in lines]) == one_by_one
    # The second call reuses what the first one parsed.
    parsed: ParsedFiles = {}
    assert enclosing_units(tmp_path, [("m.py", 1)], parsed) == one_by_one[:1]
    (tmp_path / "m.py").write_text("", "utf-8")
    assert enclosing_units(tmp_path, [("m.py", line) for line in lines], parsed) == one_by_one


def test_a_file_that_cannot_be_parsed_names_no_unit(tmp_path: Path) -> None:
    """Another file type, a missing file and invalid Python are at the top level everywhere."""
    (tmp_path / "bad.py").write_text("def f(:\n", "utf-8")
    (tmp_path / "run.sh").write_text("echo hi\n", "utf-8")
    places = [("bad.py", 1), ("run.sh", 1), ("gone.py", 3)]
    assert enclosing_units(tmp_path, places) == ["", "", ""]


async def test_the_dependents_are_the_units_around_the_places_that_use_it(tmp_path: Path) -> None:
    """Every place the index reports counts, decided or not, except an import. The changed
    unit itself is not among them."""
    (tmp_path / "store.py").write_text(STORE, "utf-8")
    (tmp_path / "app.py").write_text(
        "from store import Store\n\n\ndef run(store):\n    return store.save(1)\n\nstore.save(2)\n",
        "utf-8",
    )
    after = STORE.replace('"""Save x."""', '"""Save x remotely."""')
    (change,) = contract_changes("store.py", STORE, after)
    found = await find_dependents(SearchIndex(tmp_path), tmp_path, change)
    assert found.units == (("app.py", ""), ("app.py", "run"))
    assert found.complete
