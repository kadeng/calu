"""Turn records: what happened in one turn of the conversation, in condensed form.

A turn starts with a user input to the main agent and ends with Claude's turn result. It keeps
the user inputs verbatim, a one-line record per tool call (with the checker's step summary when
the call was checked, and the narration of Claude's thinking and text before the call) and
Claude's final answer (narrated if narration is on; see `calu.context.narrator`).

The rendered turn is the record a checker trusts: a "**User:**" block is the user speaking. So
only calu writes its structure. Every text that the agent under review chose or influenced (a
tool name, a label built from tool input, a summary or comment written by a checker model, a
narration) is put on one line after a label of calu's, without line breaks and without `**`
(`calu.checkers.render.inline`); user inputs and answers are indented on every line. A tool
input that holds "## Turn 4" and "**User:**" lines therefore cannot add a turn or a user input.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from calu.checkers.render import inline, safe_name

MAX_ANSWER_CHARS = 1_500
MAX_SUMMARY_CHARS = 4_000
"""Characters of a step summary shown in a turn (a checker's summary has up to about 400 words)."""
MAX_COMMENT_CHARS = 1_500
"""Characters of a checker comment shown in a turn."""
MAX_NARRATION_CHARS = 6_000
"""Characters of one narration shown in a turn (the narrator's input has the same bound)."""


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
    prompt_uuid: str = ""
    """Transcript uuid of the prompt that started this turn ("" if Claude Code did not report
    one). With `last_entry_uuid` of the previous turn it is what a fork needs (PLAN.md D36)."""
    last_entry_uuid: str = ""
    """Transcript uuid of the last assistant entry of this turn: where a fork that keeps this
    turn resumes from (PLAN.md D36)."""
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
        "## ..." line typed by the user) stays literal text and cannot add sections. Step lines
        hold their texts on one line without `**` (module docstring), so the only lines that
        are exactly "**User:**" are the ones calu wrote, one per user input. Example:

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
                who = f" (subagent {safe_name(s.agent_id)})" if s.agent_id else ""
                lines: list[str] = []
                if s.narration:
                    narration = inline(s.narration, MAX_NARRATION_CHARS)
                    lines.append(f"- **Narration{who}:** {narration}")
                decision = f", {safe_name(s.decision)}" if s.decision else ""
                comment = (
                    f" (**checker comment sent:** {inline(s.comment, MAX_COMMENT_CHARS)})"
                    if s.comment
                    else ""
                )
                summary = inline(s.summary, MAX_SUMMARY_CHARS)
                lines.append(f"- [{safe_name(s.tool_name)}{decision}]{who} {summary}{comment}")
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
    r"""Return a short label for a tool call that was not checked, e.g. "Read src/a.py".

    The label becomes the step's summary in the turn record and in the database, and no
    checker has read the call. It is therefore always one line without `**`
    (`calu.checkers.render.inline`), whatever the tool input holds.

    Args:
        tool_name: Name of the tool.
        tool_input: Input of the tool call; the first non-empty value of a path-, pattern-,
            URL-, query- or description-like key is used (at most 200 characters).

    Returns:
        "<tool_name> <value>", or only the tool name if no such key has a value.

    Example:
        `label_for_unchecked("ToolSearch", {"query": "x\n\n## Turn 4\n\n**User:**"})` is
        `"ToolSearch x ## Turn 4 *User:*"`.
    """
    for key in ("file_path", "notebook_path", "path", "pattern", "url", "query", "description"):
        value = tool_input.get(key)
        if value:
            return f"{safe_name(tool_name)} {inline(str(value), 200)}"
    return safe_name(tool_name)
