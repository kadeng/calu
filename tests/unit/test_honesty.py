"""The honesty register's own logic: which steps a mark blocks, and what a checker is told."""

from datetime import UTC, datetime
from pathlib import Path

from calu.checkers import Step
from calu.honesty import HonestyMark, blocking_marks, register_note

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def mark(
    path: str,
    symbol: str = "",
    reason: str = "the docstring lies",
    git_branch: str | None = None,
    closed_at: datetime | None = None,
    closed_by: str | None = None,
) -> HonestyMark:
    """Return a mark on `path`, open unless `closed_at` is given."""
    return HonestyMark(
        id=1,
        path=path,
        symbol=symbol,
        reason=reason,
        opened_at=NOW,
        git_branch=git_branch,
        closed_at=closed_at,
        closed_by=closed_by,
    )


WORKSPACE = Path("/project")


def bash(command: str, cwd: str = "/project") -> Step:
    """Return a Bash step running `command` in `cwd`."""
    return Step(tool_name="Bash", tool_input={"command": command}, cwd=cwd)


def test_a_mark_blocks_a_command_that_names_it():
    """§13.4: a word of the command that resolves to the marked file blocks the step."""
    marks = [mark("src/parser.py", "fields")]

    def blocks(command: str, cwd: str = "/project") -> list[HonestyMark]:
        """Return the marks that `command` would run."""
        return blocking_marks(bash(command, cwd), marks, WORKSPACE)

    assert blocks("python src/parser.py --check") == marks
    assert blocks("pytest ./src/parser.py") == marks, "`./` names the same file"
    assert blocks("pytest /project/src/parser.py") == marks, "so does the absolute path"
    assert blocks("pytest src/parser.py::test_fields") == marks, "a pytest node id is the file"
    assert blocks("sed -n 12p src/parser.py:12") == marks, "so is a file:line reference"
    assert blocks("python parser.py", cwd="/project/src") == marks, "resolved against the cwd"

    assert blocks("pytest tests/test_parser.py") == [], "a different file"
    assert blocks("python src/parser_utils.py") == [], "a longer name is another file"
    assert blocks("python vendor/src/parser.py") == [], "the same tail is not the same path"
    assert blocks("cat /etc/hosts") == [], "outside the project"
    # A command that names no marked path is for the checker to judge, not for this layer.
    assert blocks("make test") == []
    assert blocks("python -m myapp") == []
    # An unbalanced quote must not make the layer blind.
    assert blocks('python src/parser.py --name "unclosed') == marks


def test_editing_dirty_code_is_never_blocked():
    """The way out of a dirty mark is an edit, so blocking edits would deadlock (D83)."""
    marks = [mark("src/parser.py")]
    edit = Step(
        tool_name="Edit",
        tool_input={"file_path": "src/parser.py", "old_string": "a", "new_string": "b"},
        cwd="/project",
    )
    read = Step(tool_name="Read", tool_input={"file_path": "src/parser.py"}, cwd="/project")
    assert blocking_marks(edit, marks, WORKSPACE) == []
    assert blocking_marks(read, marks, WORKSPACE) == []


def test_a_closed_mark_blocks_nothing():
    """Clearing the record is what lets the code run again."""
    closed = mark("src/parser.py", closed_at=NOW, closed_by="the honesty checker")
    assert not closed.open
    assert blocking_marks(bash("python src/parser.py"), [closed], WORKSPACE) == []
    assert register_note([closed]) == ""
    assert "closed by the honesty checker" in closed.line()


def test_the_checker_is_told_what_is_dirty():
    """A command that names no path is covered by the checker, so it has to know (§13.4)."""
    assert register_note([]) == ""
    note = register_note([mark("src/parser.py", "fields", "the docstring promises a sorted list")])
    assert "src/parser.py:fields" in note
    assert "the docstring promises a sorted list" in note
    assert "DENY" in note, "the checker has to know what to do with it"

    many = [mark(f"src/m{i}.py", reason="r") for i in range(25)]
    short = register_note(many, limit=3)
    assert "src/m0.py" in short and "src/m24.py" not in short
    assert "and 22 more" in short


def test_a_mark_renders_its_spot_and_its_branch():
    assert mark("src/p.py").label() == "src/p.py"
    assert mark("src/p.py", "fields").label() == "src/p.py:fields"
    assert "[feature/x]" in mark("src/p.py", git_branch="feature/x").line()
    assert mark("src/p.py", "fields").mark().key() == ("src/p.py", "fields")
