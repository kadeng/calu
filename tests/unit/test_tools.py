"""The read-only tools of a checker: reading, finding and grepping files of the workspace."""

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers import CheckContext, Checker, Step
from calu.checkers.tools import DATA_NOTE, MAX_LINES
from tests.unit.helpers import default_rules

pytestmark = pytest.mark.anyio


def project(tmp_path: Path) -> Path:
    """Create a small project with sources, a secret and a directory to skip."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n\n\ndef main():\n    hello('world')\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "util.py").write_text("from src.app import hello\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=x\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return tmp_path


async def call_tool(workspace: Path, tool: str, args: dict[str, Any]) -> str:
    """Run one tool of a checker through a model that calls it, and return what it returned."""
    returned: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Call the tool on the first turn, then answer with what it returned."""
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        returned.append(str(messages[-1].parts[0].content))  # type: ignore[union-attr]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"decision": "PASS", "reason": "", "step_summary": "looked", "comment": ""},
                )
            ]
        )

    checker = Checker(
        "edit", "instructions", FunctionModel(model), workspace, default_rules(workspace)
    )
    await checker.check(
        Step(tool_name="Write", tool_input={"file_path": "src/app.py"}, cwd=str(workspace)),
        CheckContext(),
        raise_errors=True,
    )
    return returned[0]


async def test_read_file_returns_numbered_lines_and_frames_them_as_data(tmp_path: Path):
    answer = await call_tool(project(tmp_path), "read_file", {"path": "src/app.py"})
    assert DATA_NOTE in answer
    assert "    1  def hello(name):" in answer
    assert "    6      hello('world')" in answer


async def test_read_file_takes_a_line_range(tmp_path: Path):
    answer = await call_tool(
        project(tmp_path), "read_file", {"path": "src/app.py", "start_line": 5, "end_line": 6}
    )
    assert "    5  def main():" in answer and "    6      hello('world')" in answer
    assert "def hello" not in answer


async def test_read_file_reports_what_it_cannot_read(tmp_path: Path):
    root = project(tmp_path)
    assert "cannot read .env" in await call_tool(root, "read_file", {"path": ".env"})
    assert "read deny list" in await call_tool(root, "read_file", {"path": ".env"})
    assert "cannot read" in await call_tool(root, "read_file", {"path": "../outside.py"})
    assert "cannot read" in await call_tool(root, "read_file", {"path": "src/missing.py"})
    assert "cannot read" in await call_tool(root, "read_file", {"path": ""})


async def test_a_long_file_is_cut_with_a_hint_to_read_on(tmp_path: Path):
    root = project(tmp_path)
    (root / "long.txt").write_text("\n".join(str(i) for i in range(MAX_LINES + 10)))
    answer = await call_tool(root, "read_file", {"path": "long.txt"})
    assert "more lines; call read_file again with start_line" in answer


async def test_find_files_matches_a_glob_and_skips_noise(tmp_path: Path):
    root = project(tmp_path)
    answer = await call_tool(root, "find_files", {"pattern": "src/**/*.py"})
    assert sorted(answer.splitlines()) == ["src/app.py", "src/util.py"]
    answer = await call_tool(root, "find_files", {"pattern": "*.md"})
    assert answer.splitlines() == ["README.md"]
    # .git is not walked, and the read deny lists hide the secret.
    everything = await call_tool(root, "find_files", {"pattern": "**/*", "limit": 100})
    assert ".git/config" not in everything and ".env" not in everything
    assert "no project file matches" in await call_tool(root, "find_files", {"pattern": "*.rs"})


async def test_grep_files_finds_the_callers_of_a_function(tmp_path: Path):
    root = project(tmp_path)
    answer = await call_tool(root, "grep_files", {"pattern": "hello", "path_glob": "src/**/*.py"})
    assert DATA_NOTE in answer
    assert "src/app.py:1: def hello(name):" in answer
    assert "src/util.py:1: from src.app import hello" in answer
    assert "no line matches" in await call_tool(root, "grep_files", {"pattern": "nowhere"})
    assert "not a valid regular expression" in await call_tool(
        root, "grep_files", {"pattern": "^("}
    )


async def test_grep_never_reads_a_denied_file(tmp_path: Path):
    root = project(tmp_path)
    answer = await call_tool(root, "grep_files", {"pattern": "SECRET", "path_glob": "**/*"})
    assert "no line matches" in answer


async def test_a_checker_has_exactly_these_three_tools(tmp_path: Path):
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Record the offered tools and answer at once."""
        seen.extend(sorted(t.name for t in info.function_tools))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"decision": "PASS", "reason": "", "step_summary": "s", "comment": ""},
                )
            ]
        )

    checker = Checker("command", "instructions", FunctionModel(model), tmp_path)
    await checker.check(
        Step(tool_name="Bash", tool_input={"command": "ls"}, cwd=str(tmp_path)),
        CheckContext(),
        raise_errors=True,
    )
    assert seen == ["find_files", "grep_files", "read_file"]
