"""The checker's only tool: reading a file of the workspace, bounded by the read deny lists."""

from pathlib import Path

import pytest

from calu.checkers.checker import Checker
from calu.checkers.read_tool import MAX_LINES, ReadRefused, read_file
from tests.unit.helpers import default_rules


def workspace(tmp_path: Path) -> Path:
    """Create a small project with a source file and a secret."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=x\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x01")
    (tmp_path / "nul.txt").write_bytes(b"text\x00more")
    return tmp_path


def test_a_project_file_is_returned_with_line_numbers(tmp_path: Path):
    text = read_file(workspace(tmp_path), None, "src/app.py")
    assert text.splitlines() == ["    1  one", "    2  two", "    3  three"]
    assert read_file(tmp_path, None, "src/app.py", start_line=3) == "    3  three"


def test_long_files_are_cut_and_can_be_read_on(tmp_path: Path):
    (tmp_path / "long.txt").write_text("\n".join(str(i) for i in range(MAX_LINES + 10)))
    text = read_file(tmp_path, None, "long.txt")
    assert text.count("\n") == MAX_LINES  # MAX_LINES lines plus the marker
    assert "10 more lines" in text
    assert "start_line" in text
    assert read_file(tmp_path, None, "long.txt", start_line=MAX_LINES + 1).startswith(
        f"{MAX_LINES + 1:>5}  {MAX_LINES}"
    )


def test_paths_outside_the_workspace_are_refused(tmp_path: Path):
    root = workspace(tmp_path)
    for path in ("../outside.py", "/etc/passwd", "", "src/../../x"):
        with pytest.raises(ReadRefused):
            read_file(root, None, path)
    # The workspace itself and a missing file are not files.
    with pytest.raises(ReadRefused, match="not a file"):
        read_file(root, None, "src")
    with pytest.raises(ReadRefused, match="not a file"):
        read_file(root, None, "src/missing.py")


def test_the_read_deny_lists_bound_the_tool(tmp_path: Path):
    root = workspace(tmp_path)
    rules = default_rules(root)
    with pytest.raises(ReadRefused, match="read deny list"):
        read_file(root, rules, ".env")
    assert "one" in read_file(root, rules, "src/app.py")


def test_binary_files_are_refused(tmp_path: Path):
    root = workspace(tmp_path)
    with pytest.raises(ReadRefused, match="cannot be read as text"):
        read_file(root, None, "binary.bin")
    with pytest.raises(ReadRefused, match="NUL bytes"):
        read_file(root, None, "nul.txt")


def test_the_checker_exposes_the_tool_and_reports_refusals(tmp_path: Path):
    root = workspace(tmp_path)
    checker = Checker("edit", "instructions", "test:model", root, rules=default_rules(root))
    assert checker.agent.name == "edit-checker"
    assert "two" in checker._read_file("src/app.py")
    assert "cannot read .env" in checker._read_file(".env")
    assert "cannot read ../x" in checker._read_file("../x")


@pytest.mark.anyio
async def test_a_checker_model_can_call_the_tool_and_then_decide(tmp_path: Path):
    """Proves the wiring: the tool is offered to the model, runs, and its text comes back."""
    from pydantic_ai import ModelResponse, ToolCallPart
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from calu.checkers import CheckContext, Step

    seen: dict[str, object] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Read a file on the first call, then decide with what it returned."""
        seen["tools"] = sorted(t.name for t in info.function_tools)
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "src/app.py"})])
        seen["result"] = str(messages[-1].parts[0].content)  # type: ignore[union-attr]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "decision": "PASS",
                        "reason": "",
                        "step_summary": "Read the file first.",
                        "comment": "",
                    },
                )
            ]
        )

    root = workspace(tmp_path)
    checker = Checker("edit", "instructions", FunctionModel(model), root, default_rules(root))
    decision = await checker.check(
        Step(tool_name="Write", tool_input={"file_path": "src/app.py"}, cwd=str(root)),
        CheckContext(),
        raise_errors=True,
    )
    assert decision.decision == "PASS"
    assert seen["tools"] == ["read_file"]  # the only tool a checker has
    assert "two" in str(seen["result"])
