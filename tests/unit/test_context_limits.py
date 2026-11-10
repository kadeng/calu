"""What the context builder drops when a checker's context grows too long.

The unfolded turns and the running turn are capped at `ContextConfig.max_recent_chars`. What
gets dropped decides what a checker judges a step on, so the two truncations say so in the text
rather than leaving a shorter context that looks complete: the earlier steps of the running
turn, and the oldest completed turns.
"""

import re
from pathlib import Path

from calu.config import ContextConfig
from calu.context import ContextBuilder, StepRecord


def builder(workspace: Path, limit: int) -> ContextBuilder:
    """Build a context builder with a small character budget.

    Args:
        workspace: A directory to read project files from (none need exist).
        limit: The budget in characters.

    Returns:
        The builder.
    """
    return ContextBuilder(workspace, ContextConfig(max_recent_chars=limit, narrate=False))


def step(summary: str) -> StepRecord:
    """Build a step record with a given summary.

    Args:
        summary: The checker's one-line summary of the step.

    Returns:
        The record.
    """
    return StepRecord(tool_name="Bash", decision="pass", summary=summary)


def test_the_running_turn_keeps_its_head_and_its_newest_steps(tmp_path: Path) -> None:
    """A long turn is cut in the middle, not at the front: the user's words stay."""
    context = builder(tmp_path, limit=1_200)
    context.user_input("the question that started this turn")
    for index in range(40):
        context.step(step(f"step number {index} with a summary long enough to count {'x' * 40}"))
    recent = context.recent()

    # The user's input is in, and so are the last steps.
    assert "the question that started this turn" in recent
    assert "step number 39" in recent
    # The earliest steps are gone, and the text says how many.
    assert "step number 0 " not in recent
    found = re.search(r"\[(\d+) earlier steps? of this turn omitted\]", recent)
    assert found, f"no marker in:\n{recent[:400]}"
    assert int(found.group(1)) > 20
    assert len(recent) <= 1_200 + 200  # the marker itself may push a little over


def test_the_oldest_completed_turns_are_dropped_and_counted(tmp_path: Path) -> None:
    """Older turns go first, because the newest turn is the one a step belongs to."""
    context = builder(tmp_path, limit=1_500)
    for index in range(30):
        context.user_input(f"question {index}")
        context.step(step(f"a step of turn {index} {'y' * 60}"))
        context.end_turn(f"answer {index}")
    recent = context.recent()

    assert "question 29" in recent and "answer 29" in recent
    assert "question 0" not in recent
    found = re.search(r"\[(\d+) older turns? omitted\]", recent)
    assert found, f"no marker in:\n{recent[:400]}"
    assert int(found.group(1)) > 10


def test_a_short_context_is_left_whole(tmp_path: Path) -> None:
    """The markers must only appear when something was really dropped."""
    context = builder(tmp_path, limit=100_000)
    context.user_input("one question")
    context.step(step("one step"))
    context.end_turn("one answer")
    context.user_input("the running one")
    recent = context.recent()
    assert "omitted" not in recent
    assert "one question" in recent and "the running one" in recent


def test_a_turn_longer_than_the_whole_budget_still_names_the_step(tmp_path: Path) -> None:
    """The limit cannot be met by dropping everything: the running turn survives.

    1000 is the smallest budget the configuration allows (`ContextConfig` validates it), and a
    single user input longer than that is enough to exceed it.
    """
    context = builder(tmp_path, limit=1_000)
    context.user_input("q" * 4_000)
    context.step(step("the only step"))
    recent = context.recent()
    # The step the checker is about to judge is in the context, even though the head alone
    # exceeds the budget -- a turn that shows none of its steps is worse than a long one.
    assert "the only step" in recent
    assert "omitted" not in recent

    # With more steps than fit, the newest one stays and the rest are counted, in the singular
    # when there is one of them.
    context.step(step("the newest step"))
    recent = context.recent()
    assert "the newest step" in recent
    assert "[1 earlier step of this turn omitted]" in recent
