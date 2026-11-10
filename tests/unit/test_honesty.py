"""The honesty register's own logic: which steps a mark blocks, and what a checker is told."""

from datetime import UTC, datetime
from pathlib import Path

from calu.checkers import Step
from calu.honesty import HonestyMark, blocking_marks, command_paths, register_note

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def mark(
    path: str,
    symbol: str = "",
    reason: str = "the docstring lies",
    git_branch: str | None = None,
    opened_by: str | None = None,
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
        opened_by=opened_by,
        closed_at=closed_at,
        closed_by=closed_by,
    )


def workspace(tmp_path: Path) -> Path:
    """Create a project with the files the marks in these tests are on.

    The files have to exist, because `command_paths` only reports paths with a file behind
    them -- otherwise every word of a command would count as a path (D89).

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The project root.
    """
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "vendor" / "src").mkdir(parents=True, exist_ok=True)
    for name in (
        "src/parser.py",
        "src/parser_utils.py",
        "tests/test_parser.py",
        "vendor/src/parser.py",
    ):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def bash(command: str, cwd: str = "") -> Step:
    """Return a Bash step running `command` in `cwd`."""
    return Step(tool_name="Bash", tool_input={"command": command}, cwd=cwd)


def test_a_mark_blocks_a_command_that_names_it(tmp_path: Path):
    """§13.4: a word of the command that resolves to the marked file blocks the step."""
    root = workspace(tmp_path)
    marks = [mark("src/parser.py", "fields")]

    def blocks(command: str, cwd: Path | None = None) -> list[HonestyMark]:
        """Return the marks that `command` would run."""
        return blocking_marks(bash(command, str(cwd or root)), marks, root)

    assert blocks("python src/parser.py --check") == marks
    assert blocks("pytest ./src/parser.py") == marks, "`./` names the same file"
    assert blocks(f"pytest {root}/src/parser.py") == marks, "so does the absolute path"
    assert blocks("pytest src/parser.py::test_fields") == marks, "a pytest node id is the file"
    assert blocks("sed -n 12p src/parser.py:12") == marks, "so is a file:line reference"
    assert blocks("python parser.py", cwd=root / "src") == marks, "resolved against the cwd"

    assert blocks("pytest tests/test_parser.py") == [], "a different file"
    assert blocks("python src/parser_utils.py") == [], "a longer name is another file"
    assert blocks("python vendor/src/parser.py") == [], "the same tail is not the same path"
    assert blocks("cat /etc/hosts") == [], "outside the project"
    # A command that names no marked path is for the checker to judge, not for this layer.
    assert blocks("make test") == []
    assert blocks("python -m myapp") == []
    # An unbalanced quote must not make the layer blind.
    assert blocks('python src/parser.py --name "unclosed') == marks


def test_editing_dirty_code_is_never_blocked(tmp_path: Path):
    """The way out of a dirty mark is an edit, so blocking edits would deadlock (D83)."""
    root = workspace(tmp_path)
    marks = [mark("src/parser.py")]
    edit = Step(
        tool_name="Edit",
        tool_input={"file_path": "src/parser.py", "old_string": "a", "new_string": "b"},
        cwd=str(root),
    )
    read = Step(tool_name="Read", tool_input={"file_path": "src/parser.py"}, cwd=str(root))
    assert blocking_marks(edit, marks, root) == []
    assert blocking_marks(read, marks, root) == []


def test_a_closed_mark_blocks_nothing(tmp_path: Path):
    """Clearing the record is what lets the code run again."""
    root = workspace(tmp_path)
    closed = mark("src/parser.py", closed_at=NOW, closed_by="the honesty checker")
    assert not closed.open
    assert blocking_marks(bash("python src/parser.py", str(root)), [closed], root) == []
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


def test_a_mark_renders_its_spot_its_branch_and_its_author():
    assert mark("src/p.py").label() == "src/p.py"
    assert mark("src/p.py", "fields").label() == "src/p.py:fields"
    assert "[feature/x]" in mark("src/p.py", git_branch="feature/x").line()
    assert mark("src/p.py", "fields").mark().key() == ("src/p.py", "fields")
    # Several checkers may write to the register, so a mark says which one thinks this (D87).
    assert "(honesty)" in mark("src/p.py", opened_by="honesty").line()
    assert "(" not in mark("src/p.py").line().split(":")[0], "no empty parentheses"


def test_the_terminal_names_what_cannot_be_run():
    """D86: a client has to be able to say why a step was refused."""
    from calu.protocol.messages import HonestyMarkInfo, HonestyUpdate
    from calu.run import format_event

    enforced = format_event(
        HonestyUpdate(
            marks=[HonestyMarkInfo(id=1, path="src/parser.py", symbol="fields", reason="r")],
            mode="enforce",
        )
    )
    assert enforced is not None
    assert "src/parser.py:fields" in enforced
    assert "cannot be run" in enforced

    tracked = format_event(
        HonestyUpdate(marks=[HonestyMarkInfo(id=1, path="src/parser.py", reason="r")], mode="track")
    )
    assert tracked is not None and "not blocked" in tracked

    cleared = format_event(HonestyUpdate(marks=[], mode="enforce"))
    assert cleared is not None and "clear again" in cleared


def test_a_checker_is_told_about_its_own_file_first(tmp_path: Path):
    """D88: the checker judging a file is the one that can clear its mark."""
    from calu.honesty import marks_of_step

    root = workspace(tmp_path)
    mine = mark("src/parser.py", "fields", "the docstring promises a sorted list")
    other = mark("docs/api.md", reason="it documents a flag that is gone")
    edit = Step(
        tool_name="Edit",
        tool_input={"file_path": "src/parser.py", "old_string": "a", "new_string": "b"},
        cwd=str(root),
    )

    # The step's own file is found for an edit, and for a command that names it.
    assert marks_of_step(edit, [mine, other], root) == [mine]
    assert marks_of_step(bash("pytest src/parser.py", str(root)), [mine, other], root) == [mine]
    assert marks_of_step(bash("make test", str(root)), [mine, other], root) == []

    note = register_note([mine, other], marks_of_step(edit, [mine, other], root))
    # Its own file comes first, with what to do about it, and the rest follows separately.
    own_at = note.index("src/parser.py:fields")
    rest_at = note.index("docs/api.md")
    assert own_at < rest_at
    assert "This step's own code" in note
    assert "marks_clean" in note, "the checker has to be told how to clear it"
    assert "Other code recorded as dishonest" in note

    # A mark is never named twice: a file that is both the step's own and in the register.
    once = register_note([mine], marks_of_step(edit, [mine], root))
    assert once.count("src/parser.py:fields") == 1
    assert "Other code recorded" not in once

    # With nothing of its own, the note is the plain list the command checker needs.
    plain = register_note([other], marks_of_step(edit, [other], root))
    assert "This step's own code" not in plain
    assert "docs/api.md" in plain


def test_the_paths_of_a_diff_are_read_from_its_headers():
    """D88: a commit names no file, so the changeset checker is told from the diff."""
    from calu.honesty import diff_paths

    diff = """diff --git a/src/parser.py b/src/parser.py
--- a/src/parser.py
+++ b/src/parser.py
@@ -1,2 +1,2 @@
-old
+new
diff --git a/docs/old.md b/docs/new.md
similarity index 90%
rename from docs/old.md
rename to docs/new.md
"""
    # The `b/` side: the file as it will be after the commit, so a rename gives the new name.
    assert diff_paths(diff) == {"src/parser.py", "docs/new.md"}
    assert diff_paths("") == set()
    assert diff_paths("(no diff: the repository has no commit yet)") == set()


def test_a_module_argument_is_read_as_the_files_it_runs(tmp_path: Path):
    """D89: `python -m myapp` runs project code without naming a file."""
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tool.py").write_text("", encoding="utf-8")

    def paths(command: str) -> set[str]:
        """Return the project paths `command` names, as the register sees them."""
        return command_paths(command, tmp_path, tmp_path)

    # A package and a module under a `src/` layout, both only as modules.
    assert "myapp/__init__.py" in paths("python -m myapp")
    assert "src/tool.py" in paths("python3 -m src.tool")
    assert "myapp/__init__.py" in paths("uv run python -m myapp --verbose")
    assert "myapp/__init__.py" in paths("/usr/bin/python3 -m myapp")

    # A module that is not this project's code: an installed dependency names nothing here.
    assert paths("python -m pytest") == set()
    # And a `-m` that belongs to another program is not a module at all.
    assert paths("grep -m 1 pattern file.txt") == set()
    # A node id after `-m pytest` still names its file.
    assert paths("python -m pytest src/tool.py::test_x") == {"src/tool.py"}


def test_a_module_that_is_dirty_blocks_the_command(tmp_path: Path):
    """The point of D89: this is how dirty code is usually run."""
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    marks = [mark("myapp/__init__.py", reason="the module docstring lies")]
    step = Step(tool_name="Bash", tool_input={"command": "python -m myapp"}, cwd=str(tmp_path))
    assert blocking_marks(step, marks, tmp_path) == marks
    # A different module is not this one.
    other = Step(
        tool_name="Bash", tool_input={"command": "python -m something_else"}, cwd=str(tmp_path)
    )
    assert blocking_marks(other, marks, tmp_path) == []
