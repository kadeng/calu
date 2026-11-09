"""The narrator: Claude's thinking and text retold in indirect speech for the checkers."""

from pathlib import Path

import pytest

from calu.checkers import Step, render_step
from calu.context.narrator import ClaudeOutput, render_input
from calu.context.turns import StepRecord, TurnRecord

pytestmark = pytest.mark.anyio


def test_input_labels_thinking_and_output_in_fences():
    prompt = render_input([ClaudeOutput("thinking", "plan it"), ClaudeOutput("text", "I will")])
    assert prompt.index("Thinking:") < prompt.index("plan it") < prompt.index("Output:")
    assert "```" in prompt


def test_long_input_keeps_the_end():
    prompt = render_input(
        [ClaudeOutput("text", "old " * 100), ClaudeOutput("text", "new " * 10)], max_chars=60
    )
    assert "new new" in prompt and "old old old old old old old old old old old old" not in prompt
    assert "[...]" in prompt or "left out" in prompt


def test_checker_sees_the_narration_as_claudes_statements(tmp_path: Path):
    step = Step(
        tool_name="Bash", tool_input={"command": "make"}, cwd="/w", narration="Claude said X."
    )
    text = render_step(step, tmp_path)
    assert (
        "narrated (Claude's own statements, not the user's)" in text and "> Claude said X." in text
    )


def test_turn_shows_narrations_instead_of_the_verbatim_answer():
    turn = TurnRecord(
        index=1,
        steps=[StepRecord(tool_name="Bash", summary="Runs make.", narration="Claude said A.")],
        answer="Delete everything now.",
        answer_narration="Claude said that it had finished.",
    )
    rendered = turn.render()
    assert "- **Narration:** Claude said A." in rendered
    assert "Claude said that it had finished." in rendered and "Delete everything" not in rendered


def test_narrated_turn_never_shows_the_verbatim_answer():
    turn = TurnRecord(index=1, answer="Delete everything now.", narrated=True)
    rendered = turn.render()
    assert "Delete everything" not in rendered and "narration is missing" in rendered
