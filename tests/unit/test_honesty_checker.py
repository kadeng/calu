"""The honesty checker's own answer: HONEST or DISHONEST per file (PLAN.md D217).

The tests use a fake model and a temporary directory; they print nothing.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers.checker import CheckContext, Checker
from calu.checkers.model import (
    ALL_SYMBOLS,
    CodeMark,
    Decision,
    FileHonesty,
    HonestyAnswer,
    Step,
    UnitHonesty,
)
from calu.defaults.instructions import compose_instructions
from calu.honesty import HonestyState


def answer(**fields) -> HonestyAnswer:
    """Build an answer with a summary.

    Args:
        **fields: The fields of the answer.

    Returns:
        The answer.
    """
    return HonestyAnswer(step_summary="changes the parser", **fields)


def test_a_dishonest_file_never_stops_the_step_and_names_its_units():
    given = answer(
        files=[
            FileHonesty(
                path="src/parser.py",
                verdict="DISHONEST",
                reason="two functions disagree with their comments",
                units=[
                    UnitHonesty(symbol="fields", honest=False, reason="says sorted, is not"),
                    UnitHonesty(symbol="count", honest=True),
                ],
            ),
            FileHonesty(path="src/io.py", verdict="DISHONEST", reason="the module reads the net"),
            FileHonesty(path="src/ok.py", verdict="HONEST"),
        ]
    )
    decision = given.decision()
    assert decision.decision is Decision.PASS
    assert [(m.path, m.symbol, m.reason) for m in decision.marks_dirty] == [
        ("src/parser.py", "fields", "says sorted, is not"),
        ("src/io.py", "", "the module reads the net"),
    ]
    assert [(m.path, m.symbol) for m in decision.marks_clean] == [
        ("src/parser.py", "count"),
        ("src/ok.py", ALL_SYMBOLS),
    ]
    # Claude is told what is dishonest, with the approval; the record holds every verdict.
    assert decision.comment.splitlines() == [
        "DISHONEST src/parser.py: two functions disagree with their comments"
        " (fields: says sorted, is not)",
        "DISHONEST src/io.py: the module reads the net",
    ]
    assert decision.reason.splitlines()[-1] == "HONEST src/ok.py"


def test_only_what_looks_deliberate_escalates():
    given = answer(
        files=[FileHonesty(path="a.py", verdict="DISHONEST", reason="hides an upload")],
        escalate=True,
        escalate_reason="`cleanup` sends the token to a server its comment does not name",
    )
    decision = given.decision()
    assert decision.decision is Decision.ESCALATE
    assert decision.reason.startswith("`cleanup` sends the token")
    assert decision.marks_dirty[0].path == "a.py"
    assert answer(files=[]).decision().decision is Decision.PASS


def test_an_honest_file_closes_all_of_its_marks(tmp_path: Path):
    (tmp_path / "a.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
    (tmp_path / "b.py").write_text("def other():\n    return 0\n")
    state = HonestyState(tmp_path, 10, "check")
    state.record(
        [
            CodeMark(path="a.py", symbol="one", reason="r1"),
            CodeMark(path="a.py", symbol="two", reason="r2"),
            CodeMark(path="b.py", symbol="other", reason="r3"),
        ],
        [],
        "honesty",
        {"a.py", "b.py"},
    )
    assert len(state.open_marks) == 3
    honest = answer(files=[FileHonesty(path="a.py", verdict="HONEST")]).decision()
    changes = state.record(honest.marks_dirty, honest.marks_clean, "honesty", {"a.py"})
    assert len(changes.closed) == 2
    assert [(mark.path, mark.symbol) for mark in state.open_marks] == [("b.py", "other")]


@pytest.mark.anyio
async def test_the_checker_named_honesty_answers_per_file(tmp_path: Path):
    """The model is offered the verdict form, not pass or deny, and is asked again for a reason."""
    seen: list[str] = []
    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        seen.append(str(info.output_tools[0].parameters_json_schema))
        reason = "" if calls == 1 else "the comment promises a sorted list"
        files = [{"path": "src/parser.py", "verdict": "DISHONEST", "reason": reason}]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"files": files, "step_summary": "edits fields"}
                )
            ]
        )

    instructions = compose_instructions("edit", "Honesty only.", name="honesty")
    assert "# Honesty checker" in instructions and "marks_dirty" not in instructions
    checker = Checker("honesty", instructions, FunctionModel(model), tmp_path)
    step = Step(tool_name="Edit", tool_input={"file_path": "src/parser.py"}, cwd=str(tmp_path))
    decision = await checker.check(step, CheckContext())
    assert calls == 2, "a DISHONEST without a reason is asked for again"
    assert "DISHONEST" in seen[0] and "DENY" not in seen[0]
    assert decision.decision is Decision.PASS
    assert decision.marks_dirty[0].reason == "the comment promises a sorted list"
    # Every other checker keeps the common answer.
    other = Checker("edit", compose_instructions("edit", "x"), FunctionModel(model), tmp_path)
    assert not other.judges_honesty
