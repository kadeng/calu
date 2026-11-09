"""The narrator: Claude's thinking and text retold in indirect speech for the checkers."""

from pathlib import Path

import anyio
import pytest

from calu.checkers import Step, render_step
from calu.config import Config, ContextConfig
from calu.context.narrator import ClaudeOutput, render_input
from calu.context.turns import StepRecord, TurnRecord
from calu.protocol.messages import ChatSendParams, TurnResult
from tests.unit.helpers import make_session

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


async def run_turn(tmp_path: Path, config: Config | None = None) -> TurnRecord:
    """Run one turn with text before two steps and return the turn record."""
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {"text": "I will build the project."},
                {"tool": "Bash", "input": {"command": "make"}, "result": "ok"},
                {"text": "I will list the files."},
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
                {"text": "All done."},
            ]
        ],
        config,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        with anyio.fail_after(5):
            while not session.context.turns[0].answer_narration and session.narrator:
                await anyio.sleep(0.01)
        await session.stop()
    return session.context.turns[0]


async def test_session_narrates_before_checked_and_unchecked_steps(tmp_path: Path):
    turn = await run_turn(tmp_path)
    checked, allow_listed = turn.steps
    assert checked.narration == "Narrated: I will build the project."  # before the checker ran
    assert allow_listed.narration == "Narrated: I will list the files."  # in the background
    assert turn.answer_narration == "Narrated: All done."


async def test_checker_context_shows_summary_and_narrated_turns(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {"text": "I will build."},
                {"tool": "Bash", "input": {"command": "make"}, "result": "ok"},
                {"text": "Done."},
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="please build"))
        await rec.wait_for(TurnResult)
        with anyio.fail_after(5):
            while not session.context.turns[0].answer_narration:
                await anyio.sleep(0.01)
        await session.stop()
    context = session.checker_context()
    assert "No turns have been folded" in context.chat_summary
    assert "please build" in context.recent
    assert "- **Narration:** Narrated: I will build." in context.recent
    assert "Narrated: Done." in context.recent


async def test_narration_can_be_turned_off(tmp_path: Path):
    turn = await run_turn(tmp_path, Config(context=ContextConfig(narrate=False)))
    assert all(not s.narration for s in turn.steps) and not turn.answer_narration
    assert "All done." in turn.render()
