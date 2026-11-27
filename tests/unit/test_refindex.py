"""The reference index (`calu.refindex`): the word search over a project's files."""

import subprocess
from pathlib import Path

import pytest

from calu.refindex import ReferenceIndex, SearchIndex, Unit

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
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src/users.py", ".gitignore"], check=True)
    return root


def places(index: ReferenceIndex, unit: Unit) -> list[tuple[str, int]]:
    """Return the unclear places an index reports, as (file, line).

    Args:
        index: The index.
        unit: The unit asked about.

    Returns:
        The places in the order of the answer.
    """
    return [(place.path, place.line) for place in index.referrers(unit).unclear]


def test_the_search_reports_every_code_occurrence_of_the_bare_name(project: Path) -> None:
    """The word search confirms nothing and misses nothing: the call, the method of the same
    name in another class, the name as a string and as a dotted path, the untracked caller
    and the shell script are all unclear. The definition, the comment, the docstrings, a
    string that only mentions the name, a longer identifier, the ignored file and the binary
    file are left out."""
    answer = SearchIndex(project).referrers(Unit("src/users.py", "Users.add_user", line=5))
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
    assert answer.dismissed == 4  # the definition, the comment, the module docstring, the label
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
    answer = SearchIndex(project).referrers(Unit("src/users.py", "Users.add_user", line=5))
    assert not answer.complete and "too large or unreadable" in answer.problem


def test_a_directory_without_git_is_searched_whole(tmp_path: Path) -> None:
    """Outside a git work tree every file below the directory is searched."""
    (tmp_path / "a.py").write_text("add_user()\n", encoding="utf-8")
    found = places(SearchIndex(tmp_path), Unit("b.py", "add_user"))
    assert found == [("a.py", 1)]
