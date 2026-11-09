"""Data types shared by the checkers: the step under review and the checker's decision."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Decision(StrEnum):
    """Outcome of a check (PLAN.md D21).

    PASS is the normal answer: the checker has no objection, and Claude Code's auto mode decides
    about the step as it would without calu. ESCALATE pauses the tool call until a human allows
    or denies it. ALLOW and DENY are the deliberate exceptions: ALLOW runs the step whatever auto
    mode would have said, DENY stops it and tells Claude why. Without auto mode, PASS has the
    same effect as ALLOW, because the checkers then decide alone.
    """

    PASS = "PASS"
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"

    @property
    def approves(self) -> bool:
        """Whether the step may run as far as this checker is concerned.

        Returns:
            True for PASS and ALLOW.
        """
        return self in (Decision.PASS, Decision.ALLOW)


class CheckDecision(BaseModel):
    """Structured output of a checker agent.

    Field descriptions are part of the JSON schema the checker model sees.
    """

    decision: Decision = Field(
        description=(
            "PASS if you have no objection and the usual approval of the coding tool may decide "
            "(the normal answer); ESCALATE to pause the step and ask the human; ALLOW to run it "
            "even if the coding tool would refuse; DENY to stop it. Use ALLOW and DENY only "
            "where your instructions clearly call for them."
        )
    )
    reason: str = Field(
        description=(
            "Why this decision was made. For DENY, say what Claude should do instead; Claude "
            "reads this text. For ESCALATE, say what the human should look at. For ALLOW, say "
            "which rule allows it. May be empty for PASS. At most about 100 words."
        )
    )
    comment: str = Field(
        default="",
        description=(
            "Only for PASS and ALLOW: an optional remark to Claude that does not block the "
            "step, e.g. a minor style issue to fix in a later edit, or a reminder to keep "
            "names and API documentation honest. Claude reads it and it is shown in the user's "
            "chat. Leave empty if there is nothing worth saying. At most about 100 words."
        ),
    )
    step_summary: str = Field(
        description=(
            "What the step does and why Claude is doing it: one or two plain sentences for a "
            "small step; for a large edit or command, describe each substantial change "
            "faithfully (up to about 400 words). A command that edits files (sed -i, a "
            "script, a heredoc, ...) must be named as an edit, with the files and the change. "
            "This becomes the record of the step in the context of later checks."
        )
    )


class Step(BaseModel):
    """One tool call by Claude (main agent or subagent) that is about to run.

    `tool_input` is the input as Claude sent it, e.g. `{"command": ...}` for Bash or
    `{"file_path", "old_string", "new_string"}` for Edit. `agent_id` is `None` for the main
    agent, otherwise the id of the subagent that made the call.
    """

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None = None
    agent_id: str | None = None
    cwd: str
    notes: list[str] = Field(default_factory=list)
    """Hints from the user's rules for the checker, e.g. paths outside the allowlists."""
    narration: str = ""
    """Claude's thinking and text before this call, narrated in indirect speech (may be "")."""
