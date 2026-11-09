"""Turn records: what happened in one turn of the conversation, in condensed form.

A turn starts with a user input to the main agent and ends with Claude's turn result. It keeps
the user inputs verbatim, a one-line record per tool call (with the checker's step summary when
the call was checked, and the narration of Claude's thinking and text before the call) and
Claude's final answer (narrated if narration is on; see `calu.context.narrator`).
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

MAX_ANSWER_CHARS = 1_500


class StepRecord(BaseModel):
    """One tool call of a turn. `summary` is the checker's step summary, or a short label."""

    tool_name: str
    agent_id: str | None = None
    decision: str | None = None
    summary: str
    comment: str = ""
    """The checker's comment to Claude on this step, if one was sent."""
    narration: str = ""
    """Claude's thinking and text before this step, in indirect speech ("" if none or pending)."""


class TurnRecord(BaseModel):
    """A completed or running turn. `index` counts turns of the session from 1."""

    index: int
    user_inputs: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    answer: str = ""
    answer_narration: str = ""
    """Claude's thinking and text after its last tool call, narrated; replaces `answer` in
    `render` when set."""
    narrated: bool = False
    """True if narration was on for this turn: `render` then never shows `answer` verbatim."""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    def render(self) -> str:
        """Render the turn as Markdown for checker and summarizer prompts.

        Narrations are shown before their step. The final answer is shown narrated if
        `answer_narration` is set; if the turn was `narrated` but the narration is missing
        (pending, failed, or lost on resume), a placeholder replaces it. Turns without narration
        show the answer verbatim, cut at `MAX_ANSWER_CHARS` and marked with "[...]".

        The result is valid CommonMark: labels are bold, blocks are separated by blank lines,
        and user inputs and answers are indented code blocks, so Markdown inside them (e.g. a
        "## ..." line typed by the user) stays literal text and cannot add sections. Example:

            ## Turn 3

            **User:**

                Fix the typo.

            **Steps:**

            - **Narration:** Claude said that it would fix the typo.
            - [Edit, allow] Fixes a typo in README.md.

            **Claude:**

                Done.

        Returns:
            A "## Turn N" section with user inputs, steps and Claude's answer:
            `"".join(self.render_parts())`.
        """
        return "".join(self.render_parts())

    def render_parts(self) -> list[str]:
        """Render the turn as consecutive pieces whose concatenation is `render()`.

        The pieces are: the head (turn heading, user inputs, "**Steps:**"), one piece per step
        (its narration and summary line; every step after the first starts with the newline
        that separates it from the previous one) and the final answer. Appending a step only
        appends a piece and leaves the earlier pieces byte-identical, so a checker prompt that
        puts each piece in its own content block can reuse the provider's prompt cache of the
        previous check (see `calu.checkers.checker`).

        Returns:
            The pieces, in order.
        """
        blocks = [f"## Turn {self.index}"]
        for text in self.user_inputs:
            blocks.append("**User:**\n\n" + _indent(text))
        parts: list[str] = []
        if self.steps:
            parts.append("\n\n".join([*blocks, "**Steps:**\n\n"]))
            for i, s in enumerate(self.steps):
                who = f" (subagent {s.agent_id})" if s.agent_id else ""
                lines: list[str] = []
                if s.narration:
                    lines.append(f"- **Narration{who}:** {_one_line(s.narration)}")
                decision = f", {s.decision}" if s.decision else ""
                comment = f" (**checker comment sent:** {s.comment})" if s.comment else ""
                lines.append(f"- [{s.tool_name}{decision}]{who} {s.summary}{comment}")
                parts.append(("\n" if i else "") + "\n".join(lines))
        else:
            parts.append("\n\n".join(blocks))
        blocks = []
        if self.narrated and not self.answer_narration:
            if self.answer:
                blocks.append("**Claude's final answer:** not shown (its narration is missing).")
        elif self.answer_narration:
            narration = _indent(self.answer_narration)
            blocks.append("**Claude's final answer, narrated:**\n\n" + narration)
        elif self.answer:
            answer = self.answer
            if len(answer) > MAX_ANSWER_CHARS:
                answer = answer[:MAX_ANSWER_CHARS] + " [...]"
            blocks.append("**Claude:**\n\n" + _indent(answer))
        if blocks:
            parts.append("\n\n" + "\n\n".join(blocks))
        return parts


def _indent(text: str) -> str:
    """Indent every line of `text` by four spaces.

    Args:
        text: The text.

    Returns:
        The indented lines joined with newlines (without a trailing newline).
    """
    return "\n".join("    " + line for line in text.splitlines())


def label_for_unchecked(tool_name: str, tool_input: dict) -> str:
    """Return a short label for a tool call that was not checked, e.g. "Read src/a.py".

    Args:
        tool_name: Name of the tool.
        tool_input: Input of the tool call; the first non-empty value of a path-, pattern-,
            URL-, query- or description-like key is used (at most 200 characters).

    Returns:
        "<tool_name> <value>", or only the tool name if no such key has a value.
    """
    for key in ("file_path", "notebook_path", "path", "pattern", "url", "query", "description"):
        value = tool_input.get(key)
        if value:
            return f"{tool_name} {str(value)[:200]}"
    return tool_name


def _one_line(text: str) -> str:
    """Join the lines of `text` with spaces.

    Args:
        text: The text.

    Returns:
        The text on one line, runs of whitespace collapsed.
    """
    return " ".join(text.split())
