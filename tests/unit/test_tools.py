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


# ---------------------------------------------------------------- confinement (review cc0)


def outside(tmp_path: Path) -> tuple[Path, Path]:
    """Create a project and, next to it, a directory with a secret the checker must not reach.

    Returns:
        (project root, the directory outside it).
    """
    root = tmp_path / "ws"
    root.mkdir()
    project(root)
    out = tmp_path / "out"
    out.mkdir()
    (out / "secret.txt").write_text("TOPSECRET=1\n", encoding="utf-8")
    return root, out


async def test_read_file_refuses_absolute_paths_and_dot_dot(tmp_path: Path):
    root, out = outside(tmp_path)
    for path in (
        "/etc/hostname",
        str(out / "secret.txt"),
        "../out/secret.txt",
        "src/../../out/secret.txt",
        "src/../README.md",  # stays inside, and is refused all the same: one rule, no parsing
    ):
        answer = await call_tool(root, "read_file", {"path": path})
        assert "cannot read" in answer, path
        assert "TOPSECRET" not in answer and DATA_NOTE not in answer, path


async def test_read_file_refuses_links_that_leave_the_project(tmp_path: Path):
    root, out = outside(tmp_path)
    (root / "link.txt").symlink_to(out / "secret.txt")
    (root / "dirlink").symlink_to(out, target_is_directory=True)
    (root / "hop").symlink_to(root / "link.txt")  # a link to a link
    for path in ("link.txt", "dirlink/secret.txt", "hop"):
        answer = await call_tool(root, "read_file", {"path": path})
        assert "cannot read" in answer and "outside the project" in answer, path
        assert "TOPSECRET" not in answer, path


async def test_a_link_inside_the_project_is_read(tmp_path: Path):
    root, _ = outside(tmp_path)
    (root / "alias.py").symlink_to(root / "src" / "app.py")
    assert "def hello" in await call_tool(root, "read_file", {"path": "alias.py"})


async def test_the_read_deny_list_holds_through_a_link(tmp_path: Path):
    root, _ = outside(tmp_path)
    (root / "notes.txt").symlink_to(root / ".env")
    (root / "cfg").symlink_to(root, target_is_directory=True)
    for path in ("notes.txt", "cfg/.env"):
        answer = await call_tool(root, "read_file", {"path": path})
        assert "cannot read" in answer and "read deny list" in answer, path
        assert "SECRET" not in answer.replace("cannot read", ""), path


async def test_calu_s_own_configuration_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A workspace that contains calu's configuration directory under a name no rule matches
    # (XDG_CONFIG_HOME set): the directory itself is refused, not only its usual name.
    root, _ = outside(tmp_path)
    own = root / "settings" / "calu"
    own.mkdir(parents=True)
    (own / "credentials.json").write_text('{"key": "TOPSECRET"}', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "settings"))
    (root / "alias").symlink_to(own, target_is_directory=True)
    for path in ("settings/calu/credentials.json", "alias/credentials.json"):
        answer = await call_tool(root, "read_file", {"path": path})
        assert "cannot read" in answer and "TOPSECRET" not in answer, path
    assert "no line matches" in await call_tool(root, "grep_files", {"pattern": "TOPSECRET"})
    assert "credentials.json" not in await call_tool(root, "find_files", {"pattern": "**/*"})


async def test_the_search_tools_do_not_go_through_links(tmp_path: Path):
    root, out = outside(tmp_path)
    (root / "link.txt").symlink_to(out / "secret.txt")
    (root / "dirlink").symlink_to(out, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "notes.txt").symlink_to(root / ".env")
    found = await call_tool(root, "find_files", {"pattern": "**/*", "limit": 200})
    assert "secret.txt" not in found and "link.txt" not in found
    assert "dirlink" not in found and "loop/" not in found
    assert "notes.txt" not in found, "a link to a denied file is not listed either"
    assert "no line matches" in await call_tool(root, "grep_files", {"pattern": "TOPSECRET"})
    assert "no line matches" in await call_tool(root, "grep_files", {"pattern": "SECRET=x"})
    # The project's own files are still found, each once (the loop link is not walked).
    hits = await call_tool(root, "grep_files", {"pattern": "def hello"})
    assert hits.count("def hello(name)") == 1


async def test_a_path_swapped_for_a_link_after_the_check_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The race the open has to survive: `confine` saw a regular file, and before the open the
    # agent replaced a directory of the path by a link that leads out of the project.
    from calu.checkers.tools import CodeTools, ReadRefused

    root, out = outside(tmp_path)
    (out / "app.py").write_text("TOPSECRET\n", encoding="utf-8")
    tools = CodeTools(workspace=root, rules=default_rules(root))
    real = tools.confine("src/app.py")

    def swapped(path: str) -> Path:
        """Answer like the check did, then swap `src` for a link to the outside directory."""
        (root / "src").rename(root / "src.bak")
        (root / "src").symlink_to(out, target_is_directory=True)
        return real

    monkeypatch.setattr(tools, "confine", swapped)
    with pytest.raises(ReadRefused, match="outside the project"):
        tools.read_lines("src/app.py")


async def test_special_files_and_large_files_are_not_read(tmp_path: Path):
    from calu.checkers.tools import MAX_FILE_BYTES

    root, _ = outside(tmp_path)
    os.mkfifo(root / "pipe")
    (root / "big.log").write_text("x" * (MAX_FILE_BYTES + 1), encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\xff")
    with anyio.fail_after(20):  # a FIFO without a writer must not block the read
        assert "not a regular file" in await call_tool(root, "read_file", {"path": "pipe"})
    assert "are not read" in await call_tool(root, "read_file", {"path": "big.log"})
    assert "cannot read" in await call_tool(root, "read_file", {"path": "blob.bin"})
    answer = await call_tool(root, "grep_files", {"pattern": "x"})
    assert "1 files over" in answer and "were not searched" in answer


# ---------------------------------------------------------------- bounded search (review cc4)


async def test_a_backtracking_pattern_is_stopped_and_the_event_loop_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import calu.checkers.tools as tools_module

    monkeypatch.setattr(tools_module, "GREP_TIMEOUT_S", 1.0)
    root, _ = outside(tmp_path)
    (root / "min.js").write_text("a" * 60 + "!\n", encoding="utf-8")
    ticks = 0
    answer = ""

    async def tick() -> None:
        """Count how often the event loop gets to run while the search is going on."""
        nonlocal ticks
        while True:
            await anyio.sleep(0.05)
            ticks += 1

    async with anyio.create_task_group() as group:
        group.start_soon(tick)
        with anyio.fail_after(30):
            answer = await call_tool(root, "grep_files", {"pattern": "(a+)+$"})
        group.cancel_scope.cancel()
    assert "was stopped after 1 s" in answer
    assert ticks >= 5, "the event loop ran while the pattern was being matched"


async def test_grep_matches_only_the_start_of_a_very_long_line(tmp_path: Path):
    from calu.checkers.tools import MAX_GREP_LINE_CHARS

    root, _ = outside(tmp_path)
    (root / "bundle.js").write_text(
        "head " + "x" * MAX_GREP_LINE_CHARS + " NEEDLE\n", encoding="utf-8"
    )
    assert "bundle.js:1" in await call_tool(root, "grep_files", {"pattern": "^head"})
    assert "no line matches" in await call_tool(root, "grep_files", {"pattern": "NEEDLE"})


def test_a_glob_with_many_stars_is_matched_without_backtracking():
    import time

    from calu.checkers.tools import _matches

    started = time.monotonic()
    assert not _matches("a" * 200 + "/" + "a" * 250, "**/" + "*a" * 30 + "*b")
    assert time.monotonic() - started < 2
    assert _matches("src/a/b.py", "src/**/*.py") and _matches("src/b.py", "src/**/*.py")
    assert _matches("src/b.py", "*.py") and not _matches("src/b.py", "*.md")
    assert _matches("a/b/c.txt", "**/*") and _matches("c.txt", "**/*")
    assert not _matches("src/b.py", "lib/**/*.py") and not _matches("src/b.py", "")
