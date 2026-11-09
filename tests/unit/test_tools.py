"""The read-only tools of a checker: reading, finding and grepping files of the workspace."""

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers import CheckContext, Checker, Step
from calu.checkers.tools import DATA_NOTE, MAX_LINES, StepTime
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


async def call_tool(
    workspace: Path, tool: str, args: dict[str, Any], times: Sequence[StepTime] | None = None
) -> str:
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
        "edit",
        "instructions",
        FunctionModel(model),
        workspace,
        default_rules(workspace),
        times=(lambda: times) if times is not None else None,
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


async def test_step_times_reports_the_calls_with_their_times(tmp_path: Path):
    now = datetime.now(UTC)
    steps = [
        StepTime(
            seq=1,
            tool_name="Bash",
            summary="Runs the tests.",
            started_at=now - timedelta(seconds=30),
            finished_at=now - timedelta(seconds=28, milliseconds=500),
        ),
        StepTime(seq=2, tool_name="Edit", started_at=now - timedelta(seconds=2), agent_id="tool-7"),
    ]
    answer = await call_tool(project(tmp_path), "step_times", {}, times=steps)
    assert "2 steps so far" in answer
    assert "step 1 (Bash)" in answer and "Runs the tests." in answer
    assert "(1.5 s)" in answer
    assert "step 2 (Edit, subagent tool-7)" in answer and "still running" in answer
    assert "local (" in answer  # the local time, with UTC next to it


async def test_step_times_says_so_when_nothing_has_run(tmp_path: Path):
    answer = await call_tool(project(tmp_path), "step_times", {}, times=[])
    assert "no steps have run" in answer


async def test_changed_files_attributes_a_change_to_the_step_that_was_running(tmp_path: Path):
    root = project(tmp_path)
    session_start = datetime.now(UTC) - timedelta(seconds=30)
    early = StepTime(
        seq=1,
        tool_name="Bash",
        summary="Installs.",
        started_at=session_start,
        finished_at=session_start + timedelta(seconds=1),
    )
    await anyio.sleep(0.02)
    running = StepTime(seq=2, tool_name="Bash", summary="Formats.", started_at=datetime.now(UTC))
    await anyio.sleep(0.02)
    (root / "src" / "app.py").write_text("def hello(name):\n    return 'changed'\n")
    (root / "src" / "new.py").write_text("print('new')\n")
    # mtime can be backdated by anyone; ctime cannot, so the change is still attributed.
    os.utime(root / "src" / "app.py", (0, 0))

    answer = await call_tool(root, "changed_files", {}, times=[early, running])
    assert "src/app.py" in answer and "src/new.py" in answer
    assert answer.count("in step 2 (Bash)") >= 2
    assert "src/" in answer  # the directory changed too, because a file was created in it
    assert ".env" not in answer  # the read deny list covers it
    assert ".git" not in answer

    # A window that began after the writes reports nothing.
    later = StepTime(seq=3, tool_name="Bash", started_at=datetime.now(UTC) + timedelta(seconds=1))
    nothing = await call_tool(root, "changed_files", {"since_step": 3}, times=[early, later])
    assert "no project file matching" in nothing

    # The glob narrows it, and an unknown step number falls back to the whole session.
    only_new = await call_tool(
        root, "changed_files", {"path_glob": "**/new.py", "since_step": 99}, times=[early, running]
    )
    assert "src/new.py" in only_new and "app.py" not in only_new


async def test_changed_files_needs_a_step_to_compare_with(tmp_path: Path):
    answer = await call_tool(project(tmp_path), "changed_files", {}, times=[])
    assert "nothing to attribute" in answer
