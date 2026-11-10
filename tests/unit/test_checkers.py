from pathlib import Path

import pytest
from pydantic_ai import CachePoint, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from calu.checkers import (
    CheckContext,
    Checker,
    Decision,
    Route,
    Step,
    ToolPolicy,
    render_step,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def instructions() -> str:
    return "You check commands."


def bash(command: str, cwd: Path) -> Step:
    return Step(tool_name="Bash", tool_input={"command": command}, cwd=str(cwd))


class TestPolicy:
    def test_default_routes(self):
        policy = ToolPolicy()
        assert policy.route("Bash") is Route.COMMAND
        assert policy.route("Edit") is Route.EDIT
        assert policy.route("Read") is Route.ALLOW
        assert policy.route("mcp__x__y") is Route.COMMAND

    def test_overrides_win(self):
        policy = ToolPolicy(overrides={"Read": Route.ESCALATE}, default=Route.ESCALATE)
        assert policy.route("Read") is Route.ESCALATE
        assert policy.route("WebFetch") is Route.ESCALATE

    def test_a_tool_calu_does_not_know_is_checked_rather_than_allowed(self):
        """The gate must fail towards a check: a new Claude Code tool is not a free pass."""
        policy = ToolPolicy()
        for tool in (
            "WebFetch",  # leaves the machine
            "WebSearch",
            "KillShell",  # tools that exist today but are not in the table
            "BashOutput",
            "mcp__github__create_pull_request",
            "SomethingClaudeCodeAddsNextYear",
        ):
            assert policy.route(tool) is Route.COMMAND, f"{tool} would run unchecked"

    def test_nothing_that_writes_or_runs_is_on_the_allow_list(self):
        """The allow list is read-only work plus four documented exceptions."""
        from calu.checkers.policy import READ_ONLY_TOOLS

        allowed_to_write = {
            "TodoWrite",
            "TaskCreate",
            "TaskUpdate",
            "Agent",
            "Task",
            "SendMessage",
            "Skill",
        }
        for tool in READ_ONLY_TOOLS - allowed_to_write:
            assert not any(
                word in tool for word in ("Write", "Edit", "Bash", "Fetch", "Execute", "Run")
            ), f"{tool} looks like it changes something but runs without a check"


class TestRender:
    def test_bash(self, workspace: Path):
        step = Step(
            tool_name="Bash",
            tool_input={"command": "ls -la", "description": "List files"},
            cwd=str(workspace),
        )
        text = render_step(step, workspace)
        assert "`Bash`" in text and "```bash\nls -la\n```" in text
        assert "Claude's description: List files" in text

    def test_edit_is_rendered_as_diff(self, workspace: Path):
        step = Step(
            tool_name="Edit",
            tool_input={
                "file_path": str(workspace / "src" / "a.py"),
                "old_string": "return 1",
                "new_string": "return 2",
            },
            cwd=str(workspace),
        )
        text = render_step(step, workspace)
        assert "File: src/a.py" in text
        assert "-    return 1\n+    return 2\n" in text

    def test_write_new_file(self, workspace: Path):
        step = Step(
            tool_name="Write",
            tool_input={"file_path": "src/new.py", "content": "x = 1\n"},
            cwd=str(workspace),
        )
        text = render_step(step, workspace)
        assert "The file is new" in text and "+x = 1" in text

    def test_edit_outside_workspace_is_marked(self, workspace: Path, tmp_path_factory):
        other = tmp_path_factory.mktemp("other") / "x.txt"
        other.write_text("a\n", encoding="utf-8")
        step = Step(
            tool_name="Edit",
            tool_input={"file_path": str(other), "old_string": "a", "new_string": "b"},
            cwd=str(workspace),
        )
        assert "OUTSIDE THE WORKSPACE" in render_step(step, workspace)

    def test_edit_that_does_not_apply_shows_raw_input(self, workspace: Path):
        step = Step(
            tool_name="Edit",
            tool_input={"file_path": "src/a.py", "old_string": "nope", "new_string": "x"},
            cwd=str(workspace),
        )
        text = render_step(step, workspace)
        assert "does not apply cleanly" in text and '"old_string": "nope"' in text

    def test_multiedit_applies_in_order(self, workspace: Path):
        step = Step(
            tool_name="MultiEdit",
            tool_input={
                "file_path": "src/a.py",
                "edits": [
                    {"old_string": "f()", "new_string": "g()"},
                    {"old_string": "g():", "new_string": "g() -> int:"},
                ],
            },
            cwd=str(workspace),
        )
        assert "+def g() -> int:" in render_step(step, workspace)


class TestChecker:
    async def test_returns_model_decision(self, workspace: Path, instructions: str):
        model = TestModel(
            custom_output_args={"decision": "DENY", "reason": "no", "step_summary": "Deletes /"}
        )
        checker = Checker("command", instructions, model, workspace)
        decision = await checker.check(bash("rm -rf /", workspace), CheckContext())
        assert decision.decision is Decision.DENY
        assert decision.step_summary == "Deletes /"

    async def test_prompt_layout_and_instructions(self, workspace: Path, instructions: str):
        seen: dict = {}

        def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen["instructions"] = info.instructions
            seen["content"] = next(
                p.content for p in messages[-1].parts if isinstance(p, UserPromptPart)
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"decision": "ALLOW", "reason": "", "step_summary": "Lists files."},
                    )
                ]
            )

        checker = Checker("command", instructions, FunctionModel(fake), workspace)
        ctx = CheckContext(prefix="PROJECT", turns="RECENT")
        decision = await checker.check(bash("ls", workspace), ctx)
        assert decision.decision is Decision.ALLOW
        assert seen["instructions"] == "You check commands."
        content = seen["content"]
        assert [type(p) for p in content] == [str, CachePoint, str, CachePoint, str]
        assert "PROJECT" in content[0] and "RECENT" in content[2] and "ls" in content[4]

    async def test_reason_required_for_deny(self, workspace: Path, instructions: str):
        calls = 0

        def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            reason = "" if calls == 1 else "Use trash instead."
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"decision": "DENY", "reason": reason, "step_summary": "s"},
                    )
                ]
            )

        checker = Checker("command", instructions, FunctionModel(fake), workspace)
        decision = await checker.check(bash("rm x", workspace), CheckContext())
        assert calls == 2 and decision.reason == "Use trash instead."

    async def test_failure_escalates(self, workspace: Path, instructions: str):
        def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise RuntimeError("provider down")

        checker = Checker("command", instructions, FunctionModel(broken), workspace)
        decision = await checker.check(bash("ls", workspace), CheckContext())
        assert decision.decision is Decision.ESCALATE
        assert "provider down" in decision.reason

    async def test_instructions_reload_on_change(self, workspace: Path, instructions: str):
        seen: list[str | None] = []

        def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(info.instructions)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"decision": "ALLOW", "reason": "", "step_summary": "s"},
                    )
                ]
            )

        checker = Checker("command", instructions, FunctionModel(fake), workspace)
        await checker.check(bash("ls", workspace), CheckContext())
        checker.instructions = "New rules."
        await checker.check(bash("ls", workspace), CheckContext())
        assert seen == ["You check commands.", "New rules."]


@pytest.mark.live
async def test_live_checker_returns_structured_decision(workspace: Path, checker_test_model: str):
    """Wiring test with a real model: only the structure of the result is asserted."""
    instructions = (
        Path(__file__).resolve().parents[2] / "src/calu/defaults/checkers/command.md"
    ).read_text(encoding="utf-8")
    checker = Checker("command", instructions, checker_test_model, workspace)
    decision = await checker.check(bash("ls -la src", workspace), CheckContext(prefix="A project."))
    assert decision.decision in set(Decision)
    assert decision.step_summary.strip()
    assert "checker failed" not in decision.reason


def test_cache_points_reuse_the_previous_check(tmp_path: Path):
    """Each check repeats the previous check's blocks and cache point (per-step caching)."""
    from calu.checkers.checker import context_parts
    from calu.context.turns import StepRecord, TurnRecord

    turn = TurnRecord(index=2, user_inputs=["go"])
    turn.steps.append(StepRecord(tool_name="Bash", summary="Runs make."))
    first = context_parts(
        CheckContext(prefix="P", turns="T1", current=tuple(turn.render_parts())), "# Project"
    )
    turn.steps.append(StepRecord(tool_name="Edit", summary="Edits a.py."))
    second = context_parts(
        CheckContext(prefix="P", turns="T1", current=tuple(turn.render_parts())), "# Project"
    )
    assert sum(isinstance(p, CachePoint) for p in second) == 2  # + 2 from Caching = 4

    # The first check's text blocks up to its last cache point recur unchanged in the second
    # check, and the second check has a cache point at the same place (it reads that entry).
    def texts(parts: list) -> list[str]:
        return [p for p in parts if isinstance(p, str)]

    last = max(i for i, p in enumerate(first) if isinstance(p, CachePoint))
    cached = texts(first[: last + 1])
    assert texts(second)[: len(cached)] == cached
    seen = 0
    for i, part in enumerate(second):
        seen += isinstance(part, str)
        if seen == len(cached):
            assert isinstance(second[i + 1], CachePoint)
            break
    assert "".join(turn.render_parts()) == turn.render()


def test_the_guide_names_every_tool_a_checker_has(tmp_path: Path) -> None:
    """CALU_GUIDE.md claims a checker has exactly five tools, all reads (D123).

    That claim is part of the security argument -- "a checker cannot edit your code, run a
    command or send your source anywhere" -- so it must not be able to go stale quietly. Adding
    a tool to `calu.checkers.tools` fails this test until the guide says so.
    """
    from calu.checkers.tools import CodeTools
    from calu.context.turns import StepRecord

    guide = (Path(__file__).resolve().parents[2] / "CALU_GUIDE.md").read_text(encoding="utf-8")

    plain = CodeTools(tmp_path).toolset()
    with_times = CodeTools(tmp_path, times=lambda: [StepRecord(tool_name="Bash")]).toolset()
    read_only = {"read_file", "find_files", "grep_files"}
    extra = {"step_times", "changed_files"}

    assert set(plain.tools) == read_only, (
        "a checker without the step times has the three reading tools and nothing else"
    )
    assert set(with_times.tools) == read_only | extra

    for name in read_only | extra:
        assert f"`{name}`" in guide, f"the guide does not name the tool {name}"
    # And the count in the sentence is the count in the code.
    assert f"exactly {len(read_only | extra)} tools" in guide.replace("five", "5"), (
        "the guide's tool count and the toolset disagree"
    )
