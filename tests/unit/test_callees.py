"""What a Python unit uses, read from its syntax (`calu.callees`)."""

from calu.callees import MAX_USED, Callees, UsedUnit, used_units

PROJECT = {
    "src/pkg/__init__.py": "def version():\n    return 1\n",
    "src/pkg/store.py": (
        "def save(row):\n    return row\n\n\n"
        "class Table:\n    @classmethod\n    def make(cls):\n        return cls()\n"
    ),
    "src/pkg/util/text.py": "def strip(line):\n    return line.strip()\n",
    "src/pkg/app.py": """import json
import pkg.store
import pkg.store as st
from pkg import store, version
from pkg.store import Table, save as keep
from pkg.util import text
from .util.text import strip
from . import store as near


def helper(line):
    return line


class Parser:
    def parse(self, line):
        def inner(part):
            return part

        self.check(line)
        self.missing(line)
        line.strip()
        json.dumps(line)
        return inner(helper(line))

    def check(self, line):
        return Parser.parse(self, line)

    @classmethod
    def build(cls):
        return cls.make()

    @classmethod
    def make(cls):
        return Parser()


def absolute(line):
    keep(line)
    pkg.store.save(line)
    st.save(line)
    store.save(line)
    Table.make()
    Table()
    version()
    return text.strip(line)


def relative(line):
    near.save(line)
    return strip(line)


def calls_itself(n):
    return calls_itself(n - 1)
""",
}


def read(path: str) -> str | None:
    """Return a file of the test project.

    Args:
        path: The file's relative path.

    Returns:
        Its text; None if the project has no such file.
    """
    return PROJECT.get(path)


def names(path: str, symbol: str) -> list[str]:
    """List what a unit of the test project uses, as `path:symbol`.

    Args:
        path: The unit's file.
        symbol: The unit's dotted name.

    Returns:
        The used units in the order of their first call.
    """
    return [f"{used.path}:{used.symbol}" for used in used_units(read, path, symbol)]


def test_calls_inside_the_file_are_followed() -> None:
    """A method on `self`, a nested function and a function of the file are found. A method
    the class does not define, a method on another object and the standard library are not."""
    assert names("src/pkg/app.py", "Parser.parse") == [
        "src/pkg/app.py:Parser.check",
        "src/pkg/app.py:Parser.parse.inner",
        "src/pkg/app.py:helper",
    ]


def test_a_class_and_its_methods_are_units() -> None:
    """A call of a class names the class; `Class.method()` and `cls.method()` name the method."""
    assert names("src/pkg/app.py", "Parser.check") == ["src/pkg/app.py:Parser.parse"]
    assert names("src/pkg/app.py", "Parser.build") == ["src/pkg/app.py:Parser.make"]
    assert names("src/pkg/app.py", "Parser.make") == ["src/pkg/app.py:Parser"]


def test_absolute_imports_are_followed_into_the_project() -> None:
    """An imported name, a module with and without an alias, a module imported from its
    package, an imported class and a name of the package's `__init__.py`. The package is
    found below `src/`, where the importing file is."""
    assert names("src/pkg/app.py", "absolute") == [
        "src/pkg/store.py:save",
        "src/pkg/store.py:Table.make",
        "src/pkg/store.py:Table",
        "src/pkg/__init__.py:version",
        "src/pkg/util/text.py:strip",
    ]


def test_relative_imports_are_followed() -> None:
    """`from . import module` and `from .package.module import name`."""
    assert names("src/pkg/app.py", "relative") == [
        "src/pkg/store.py:save",
        "src/pkg/util/text.py:strip",
    ]


def test_a_unit_is_not_listed_as_using_itself() -> None:
    """Recursion is no edge."""
    assert names("src/pkg/app.py", "calls_itself") == []


def test_what_cannot_be_read_gives_nothing() -> None:
    """A file that is missing, not Python or not valid Python, and a name the file does not
    define."""
    assert names("src/pkg/none.py", "f") == []
    assert names("src/pkg/app.py", "gone") == []
    assert used_units({"a.txt": "def f():\n    g()\n"}.get, "a.txt", "f") == []
    assert used_units({"a.py": "def f(:\n"}.get, "a.py", "f") == []


def test_the_list_is_capped() -> None:
    """At most `MAX_USED` units for one unit."""
    many = "".join(f"def g{n}():\n    return {n}\n\n\n" for n in range(MAX_USED + 5))
    calls = "".join(f"    g{n}()\n" for n in range(MAX_USED + 5))
    found = used_units({"a.py": f"{many}def f():\n{calls}"}.get, "a.py", "f")
    assert len(found) == MAX_USED and found[0] == UsedUnit("a.py", "g0")


def test_one_object_reads_each_file_once() -> None:
    """`Callees` answers for many units and asks for a file only the first time."""
    asked: list[str] = []

    def counting(path: str) -> str | None:
        """Note which file is asked for, then return it.

        Args:
            path: The file's relative path.

        Returns:
            Its text, or None.
        """
        asked.append(path)
        return PROJECT.get(path)

    callees = Callees(counting)
    first = callees.of("src/pkg/app.py", "relative")
    again = callees.of("src/pkg/app.py", "absolute")
    assert first and again
    assert asked.count("src/pkg/app.py") == 1 and asked.count("src/pkg/store.py") == 1
