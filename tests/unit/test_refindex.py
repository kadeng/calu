"""The reference index (`calu.refindex`): the word search over a project's files."""

import subprocess
from pathlib import Path

import pytest

from calu.refindex import SearchIndex, Unit
from calu.refindex.search import is_method

MODULE = '''"""Users. add_user is described here."""


class Users:
    def add_user(self, name):
        """Add a user."""
        return name


class Groups:
    def add_user(self, name):
        return name


def setup(users):
    users.add_user("a")  # add_user in a comment
    label = "calls add_user later"
    return getattr(users, "add_user"), "pkg.users.Users.add_user", label
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git repository with a module, a caller, a shell script, an ignored and a binary file.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The project directory.
    """
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "src" / "users.py").write_text(MODULE, encoding="utf-8")
    (root / "src" / "main.py").write_text("users.add_user('b')\nadd_user_later = 1\n", "utf-8")
    (root / "run.sh").write_text("python -c 'add_user()'  # add_user\n", encoding="utf-8")
    (root / "build" / "out.py").write_text("add_user()\n", encoding="utf-8")
    (root / "data.bin").write_bytes(b"\0add_user\0")
    (root / "NOTES.md").write_text("`add_user` adds a user. Call add_user().\n", "utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src/users.py", ".gitignore"], check=True)
    return root


def places(index: SearchIndex, unit: Unit) -> list[tuple[str, int]]:
    """Return the unclear places an index reports, as (file, line).

    Args:
        index: The index.
        unit: The unit asked about.

    Returns:
        The places in the order of the answer.
    """
    return [(place.path, place.line) for place in index.find(unit).unclear]


def test_the_search_reports_every_code_occurrence_of_the_bare_name(project: Path) -> None:
    """The word search confirms nothing and misses nothing: the call, the method of the same
    name in another class, the name as a string and as a dotted path, the untracked caller
    and the shell script are all unclear. The definition, the comment, the docstrings, a
    string that only mentions the name, a longer identifier, the Markdown file, the ignored
    file and the binary file are left out."""
    answer = SearchIndex(project, same_language=False).find(
        Unit("src/users.py", "Users.add_user", line=5)
    )
    assert answer.referrers == ()
    assert [(p.path, p.line) for p in answer.unclear] == [
        ("run.sh", 1),
        ("run.sh", 1),
        ("src/main.py", 1),
        ("src/users.py", 11),
        ("src/users.py", 16),
        ("src/users.py", 18),
        ("src/users.py", 18),
    ]
    # the definition, the comment, the module docstring, the label, two mentions in NOTES.md
    assert answer.dismissed == 6
    assert answer.complete and answer.problem == ""
    assert answer.unclear[4].text.startswith('users.add_user("a")')
    assert answer.unclear[4].column == 10


def test_a_file_the_search_cannot_tokenize_is_searched_as_text(project: Path) -> None:
    """A Python file with a syntax the tokenizer refuses loses nothing: every occurrence is
    reported, comments included."""
    (project / "src" / "broken.py").write_text("x = 'open\nadd_user()  # add_user\n", "utf-8")
    found = places(SearchIndex(project), Unit("src/users.py", "Users.add_user", line=5))
    assert found.count(("src/broken.py", 2)) == 2


def test_a_file_too_large_to_search_makes_the_answer_incomplete(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the size limit is not searched, and the answer says that it is not
    complete, so that nobody reads it as "no further referrer"."""
    monkeypatch.setattr("calu.refindex.search.MAX_FILE_BYTES", 30)
    answer = SearchIndex(project).find(Unit("src/users.py", "Users.add_user", line=5))
    assert not answer.complete and "too large or unreadable" in answer.problem


def test_a_directory_without_git_is_searched_whole(tmp_path: Path) -> None:
    """Outside a git work tree every file below the directory is searched."""
    (tmp_path / "a.py").write_text("add_user()\n", encoding="utf-8")
    found = places(SearchIndex(tmp_path), Unit("b.py", "add_user"))
    assert found == [("a.py", 1)]


def test_by_default_only_files_of_the_units_own_language_are_searched(project: Path) -> None:
    """A Python unit is looked for in Python files only: the shell script that holds the same
    word is not read. Whether another language calls the unit is left for later."""
    found = places(SearchIndex(project), Unit("src/users.py", "Users.add_user", line=5))
    assert ("run.sh", 1) not in found and ("src/main.py", 1) in found


def test_a_method_is_looked_for_only_after_a_dot_or_in_a_string_in_other_files(
    tmp_path: Path,
) -> None:
    """In another file a method is reached as `x.name`, `Class.name` or by its name in a
    string; the bare word there is a variable, a keyword argument or another function, and is
    left out. In the method's own file every use stays, because a class body can use its own
    methods by their bare name."""
    (tmp_path / "store.py").write_text(
        "class Store:\n"
        "    @property\n"
        "    def save(self):\n"
        "        return 1\n"
        "    @save.setter\n"
        "    def save(self, value): ...\n",
        "utf-8",
    )
    (tmp_path / "use.py").write_text(
        "save = 1\n"
        "store.save()\n"
        "run(save=True)\n"
        "Store.save\n"
        "getattr(store, 'save')\n"
        "def save(): ...\n",
        "utf-8",
    )
    answer = SearchIndex(tmp_path).find(Unit("store.py", "Store.save", line=3))
    assert [(p.path, p.line) for p in answer.unclear] == [
        ("store.py", 5),
        ("store.py", 6),
        ("use.py", 2),
        ("use.py", 4),
        ("use.py", 5),
    ]
    function = SearchIndex(tmp_path).find(Unit("use.py", "save", line=6))
    assert ("use.py", 1) in [(p.path, p.line) for p in function.unclear]


def test_only_a_method_directly_in_a_class_counts_as_a_method(tmp_path: Path) -> None:
    """A function nested in a function is called by its bare name, so it is no method."""
    (tmp_path / "m.py").write_text(
        "class A:\n    def f(self):\n        def g(): ...\n        return g()\n", "utf-8"
    )
    assert is_method(tmp_path, Unit("m.py", "A.f"))
    assert not is_method(tmp_path, Unit("m.py", "A.f.g"))
    assert not is_method(tmp_path, Unit("m.py", "A.missing"))
    assert not is_method(tmp_path, Unit("m.py", "f"))
