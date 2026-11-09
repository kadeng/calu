"""Files that tool calls refer to: command arguments and skill files (`tool.files`)."""

from pathlib import Path

import pytest

from calu.checkers.rules import Rules
from calu.files import command_files, is_text_file, skill_file
from calu.protocol.messages import FileLink

pytestmark = pytest.mark.anyio


def make_workspace(tmp_path: Path) -> tuple[Path, Rules]:
    ws = tmp_path / "w"
    (ws / "docs").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "docs/PLAN.md").write_text("x")
    (ws / "DESIGN.md").write_text("x")
    (ws / "run.log").write_text("x")
    (ws / "app.ts").write_text("x")
    (ws / "tests/test_a.py").write_text("x")
    (ws / "image.png").write_bytes(b"x")
    (ws / "id.pem").write_text("secret")
    (ws / "Makefile").write_text("x")
    return ws, Rules.from_texts(ws, {"read-deny": r"\.pem$"})


def test_links_existing_text_files_relative_and_absolute(tmp_path: Path):
    ws, rules = make_workspace(tmp_path)
    command = f"wc -l {ws}/DESIGN.md; git -C {ws} log --oneline -3 && tail run.log app.ts"
    assert command_files(command, ws, rules) == [
        FileLink(raw=f"{ws}/DESIGN.md", path=str(ws / "DESIGN.md")),
        FileLink(raw="run.log", path=str(ws / "run.log")),
        FileLink(raw="app.ts", path=str(ws / "app.ts")),
    ]


def test_follows_cd_and_pytest_node_ids_and_lists_each_file_once(tmp_path: Path):
    ws, rules = make_workspace(tmp_path)
    links = command_files(
        "cd docs && head PLAN.md PLAN.md; uv run pytest tests/test_a.py::test_x -q", ws, rules
    )
    assert links == [
        FileLink(raw="PLAN.md", path=str(ws / "docs/PLAN.md")),
        FileLink(raw="tests/test_a.py", path=str(ws / "tests/test_a.py")),
    ]


def test_skips_missing_binary_directories_and_read_denied_files(tmp_path: Path):
    ws, rules = make_workspace(tmp_path)
    links = command_files("cat missing.md image.png docs id.pem Makefile", ws, rules)
    assert links == [FileLink(raw="Makefile", path=str(ws / "Makefile"))]
    assert not is_text_file(ws / "docs")


def test_skill_file_links_skill_md(tmp_path: Path):
    skill = tmp_path / ".claude/skills/greet"
    skill.mkdir(parents=True)
    assert skill_file(str(skill)) is None
    (skill / "SKILL.md").write_text("---\nname: greet\n---\n")
    assert skill_file(f"{skill}  ") == FileLink(raw="greet", path=str(skill / "SKILL.md"))
