"""The text a client sends as the answer to a set of questions of the agent (PLAN.md D35).

`escalation.decide {answer}` takes one text for the whole `AskUserQuestion` call, also when the
call asked several questions or allowed several options. Every client therefore has to put the
chosen options into one text. This module holds that composition, so that Claude gets the same
shape from each client:

    Runner: pytest
    Scope: unit tests, type check
    Notes: only the files that changed

One line per question: the question's `header` (or `Question N` without one), a colon, and the
chosen option labels joined with ", " or the user's own text. A set of exactly one question is
answered with the bare text, without the header, which is what the web interface and the line
client have always sent.

The module is pure: it imports nothing from calu and performs no I/O.
"""

from collections.abc import Sequence
from typing import Any


def question_title(question: dict[str, Any], index: int) -> str:
    """Return the short name a client shows for one question of a set.

    Args:
        question: The question as `escalation.request.questions` carries it.
        index: Its position in the set, counted from 0.

    Returns:
        The question's `header` if it is a non-empty text, else `Question <index + 1>`.
    """
    header = question.get("header")
    if isinstance(header, str) and header.strip():
        return header.strip()
    return f"Question {index + 1}"


def compose_answer(questions: Sequence[dict[str, Any]], answers: Sequence[Sequence[str]]) -> str:
    """Compose the one answer text for a set of questions.

    Args:
        questions: The questions as `escalation.request.questions` carries them.
        answers: Per question, in the same order, the texts the user chose: option labels,
            or one text of the user's own. Entries beyond the last question are ignored; a
            missing or empty entry leaves that question out of the text.

    Returns:
        For one question: its answers joined with ", ". For several: one line per answered
        question, `<header>: <answers joined with ", ">`. "" if nothing was answered.
    """
    lines: list[tuple[str, str]] = []
    for index, question in enumerate(questions):
        chosen = [text.strip() for text in (answers[index] if index < len(answers) else ())]
        chosen = [text for text in chosen if text]
        if chosen:
            lines.append((question_title(question, index), ", ".join(chosen)))
    if len(questions) == 1:
        return lines[0][1] if lines else ""
    return "\n".join(f"{title}: {text}" for title, text in lines)
