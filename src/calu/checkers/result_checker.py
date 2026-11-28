"""The subagent checker: reviews what Claude tells a subagent and what the subagent hands back.

Two kinds of review, with the same instructions and the same answer (`ResultReview`):

    task    an Agent call before the subagent starts, or Claude's later SendMessage to it
            (`run_task`): the task against the conversation and the earlier decisions
    result  the subagent's final result or handed-back report (`run`): the result against the
            task and the conversation

What a verdict does (the owner's requirement 51):

    verdict     task                                    result
    ACCEPT      the call runs                           Claude gets the result
    ANNOTATE    the call runs, Claude gets the note     Claude gets the result and the note
    DENY        the call is refused; Claude gets the    a handed-back report is refused and
                note as the reason and may write the    Claude gets the note only; a refused
                task again                              report is possible, although the
                                                        instructions do not ask for it
    ESCALATE    a person decides                        a person decides (the exception, e.g.
                                                        a secret Claude should not read)

Request layout (cache points as for the step checkers):

    instructions   <- checkers/subagent-result.md
    user prompt    = [context prefix, CachePoint, recent turns, CachePoint,
                      "# Task to review" (the task) or "# Result to review" (task + result)]

See DOCUMENTATION.md "Checking subagent results" for the failure case it addresses (scope expansion
through reviewer subagents).
"""

import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, CachePoint, ModelRetry, RunContext, UsageLimits
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.checkers.checker import CheckContext, context_parts, elapsed_ms
from calu.checkers.model import CheckerEffort, SubagentResult, SubagentTask
from calu.checkers.render import code
from calu.markdown import fence
from calu.models import MAX_ANSWER_TOKENS, cache_settings, run_bounded

MAX_RESULT_CHARS = 120_000
"""Longer subagent results are cut (from the middle) before they are sent to the checker.
Generous, so that it practically never cuts (PLAN.md D273); a cut escalates with
`context.escalate_on_cut` (`run`)."""

MAX_TASK_CHARS = 60_000
"""Longer tasks Claude gives a subagent are cut (from the middle), as results are."""


def cut_chars(task: str, result: str = "") -> int:
    """Count the characters a review does not show of a task and a result.

    Args:
        task: Claude's text for the subagent.
        result: The subagent's result or report; "" for the review of a task.

    Returns:
        The characters beyond `MAX_TASK_CHARS` and `MAX_RESULT_CHARS`; 0 if both fit.
    """
    return max(0, len(task) - MAX_TASK_CHARS) + max(0, len(result) - MAX_RESULT_CHARS)


class ResultVerdict(StrEnum):
    """Outcome of the review of a task or a result.

    ACCEPT: nothing to remark. ANNOTATE: Claude gets a note with it. DENY: the task is
    refused and Claude gets the note as the reason, so it can write the task again; for a
    result, the report is kept from Claude. ESCALATE: the user decides how to proceed.
    """

    ACCEPT = "ACCEPT"
    ANNOTATE = "ANNOTATE"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


NOTE_IN_SUMMARY = re.compile(r"\n\s*Note:\s*")
"""Where a summary goes over into the note that a model wrote into it (`_require_note`)."""


def _note_is_required(schema: dict[str, Any]) -> None:
    """Name `note` as a required key in the schema the model is shown.

    Validation still accepts an answer without it.

    Args:
        schema: The JSON schema of `ResultReview`; changed in place.
    """
    # Seen on 2026-11-27 with the checker model of the owner's installation: with `note`
    # optional, the model answered {"verdict": "ANNOTATE", "summary": ...} in three attempts out
    # of three, with its remark at the end of the summary or missing. The check failed four
    # times in one session.
    required = schema.setdefault("required", [])
    if "note" not in required:
        required.append("note")


class ResultReview(BaseModel):
    """Structured output of the result checker (field descriptions are shown to the model)."""

    model_config = ConfigDict(json_schema_extra=_note_is_required)

    verdict: ResultVerdict = Field(
        description=(
            "ACCEPT, ANNOTATE (a note for Claude), DENY (a task: Claude gets the note as "
            "the reason and may write the task again) or ESCALATE (the user decides)."
        )
    )
    note: str = Field(
        default="",
        description=(
            "Note for Claude: a reminder of what the user asked or accepted, and suggestions "
            "which points look in scope, out of scope, against a tradeoff the user accepted, or "
            "in need of the user's decision. It suggests and does not instruct: Claude decides. "
            "With DENY it says what the task must not ask for or must say. Empty for ACCEPT. "
            "At most about 300 words."
        ),
    )
    summary: str = Field(
        description=(
            "The result and its fit to the task's scope: one plain sentence for a small "
            "result, up to about 200 words for a large one."
        )
    )


@dataclass(frozen=True)
class ResultOutcome:
    """The result of one result-checker model call: the review and what it cost.

    The token counts are None if the call failed (then `review` is an ESCALATE review that
    names the error). `duration_ms` is the wall-clock time of the call, retries included.
    """

    review: ResultReview
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int = 0


def _cut_middle(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Shorten `text` to about `limit` characters by removing its middle.

    Args:
        text: The text.
        limit: Maximum length of the kept parts together.

    Returns:
        `text` unchanged if short enough, else its start and end with a marker in between.
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters omitted ...]\n{text[-half:]}"


@dataclass
class ResultChecker:
    """A result checker with Markdown instructions (the agent is rebuilt when they change).

    `review` never raises for model or network errors: those yield an ESCALATE review whose
    note names the error, so the user decides.
    """

    instructions: str
    model: str | Model
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=3, output_tokens_limit=8_000)
    )
    max_tokens: int = MAX_ANSWER_TOKENS
    """Cap on the output tokens of one answer; a cut-off answer is retried once (`run_bounded`)."""
    effort: CheckerEffort | None = None
    """How much the model may think, as for a step checker (`Checker.effort`); None leaves the
    provider's default. It was missing here, so `subagent_checker.effort` was a setting that
    reached nothing (agent/records/PLAN.md D135)."""
    _agent: Agent[None, ResultReview] | None = field(default=None, init=False, repr=False)
    _built_for: str | None = field(default=None, init=False, repr=False)
    cache_key: str = field(default_factory=lambda: f"calu-result-{secrets.token_hex(6)}")
    """Routing hint for providers with automatic prompt caching (see `models.cache_settings`)."""

    @property
    def agent(self) -> Agent[None, ResultReview]:
        """The Pydantic AI agent, rebuilt if `instructions` changed.

        Returns:
            The agent.
        """
        instructions = self.instructions
        if self._agent is None or instructions != self._built_for:
            agent: Agent[None, ResultReview] = Agent(
                capabilities=[Caching(messages=False)],
                output_type=ResultReview,
                instructions=instructions,
                name="subagent-result-checker",
            )
            agent.output_validator(_require_note)
            self._agent, self._built_for = agent, instructions
        return self._agent

    def build_prompt(self, result: SubagentResult, context: CheckContext) -> list[str | CachePoint]:
        """Return the user prompt parts for reviewing `result` with `context`.

        Args:
            result: The subagent's task and result.
            context: Cached project/conversation context and recent turns.

        Returns:
            Prompt parts with cache points between context and result.
        """
        parts = context_parts(context, "# Project and conversation context")
        kind = "background subagent" if result.background else "subagent"
        # Claude wrote the description: one line in a code span, so it cannot add a heading or
        # a user turn to the prompt (`calu.checkers.render`, rule 1).
        description = code(result.description, 300) if result.description else "no description"
        done, what = (
            ("handed back its report", "Its report")
            if result.handback
            else ("finished", "Its final result")
        )
        parts.append(
            f"# Result to review\n\nA {kind} ({description}) {done}.\n\n"
            f"Task Claude gave it:\n{fence(_cut_middle(result.prompt, MAX_TASK_CHARS))}\n\n"
            f"{what}:\n{fence(_cut_middle(result.result))}"
        )
        return parts

    def build_task_prompt(
        self, task: SubagentTask, context: CheckContext
    ) -> list[str | CachePoint]:
        """Return the user prompt parts for reviewing a task Claude gives a subagent.

        Args:
            task: The task that starts a subagent, or Claude's later message to one.
            context: Cached project/conversation context and recent turns.

        Returns:
            Prompt parts with cache points between context and task.
        """
        parts = context_parts(context, "# Project and conversation context")
        # Claude wrote the description: one line in a code span (`calu.checkers.render`).
        description = code(task.description, 300) if task.description else "no description"
        if task.kind == "message":
            what = f"Claude sends a further message to its subagent ({description})."
        else:
            kind = "background subagent" if task.background else "subagent"
            what = f"Claude starts a {kind} ({description}). The subagent has not run yet."
        before = (
            f"Claude's thinking and text before this call, narrated:\n{fence(task.narration)}\n\n"
            if task.narration.strip()
            else ""
        )
        parts.append(
            f"# Task to review\n\n{what}\n\n{before}"
            f"Claude's text for the subagent:\n{fence(_cut_middle(task.prompt, MAX_TASK_CHARS))}"
        )
        return parts

    def settings(self) -> ModelSettings:
        """Build the model settings of one review: the cache routing key and the effort.

        Returns:
            `cache_settings` for this checker, with `thinking` added when `effort` is set:
            "off" asks for no thinking, any other level names it -- the same rule as
            `Checker.settings`, so one setting means one thing for every checker.
        """
        settings = cache_settings(self.model, self.cache_key)
        if self.effort is not None:
            settings["thinking"] = False if self.effort == "off" else self.effort
        return settings

    async def review(self, result: SubagentResult, context: CheckContext) -> ResultReview:
        """Review `result` (see the class docstring for error handling).

        Args:
            result: The subagent's task and result.
            context: Cached project/conversation context and recent turns.

        Returns:
            The review; `run` returns the same review with the cost of the call.
        """
        return (await self.run(result, context)).review

    async def run(
        self, result: SubagentResult, context: CheckContext, escalate_cut: bool = True
    ) -> ResultOutcome:
        """Review `result` and return the review with the cost of the model call.

        Never raises for model or network errors (see the class docstring); the session stores
        the outcome as a `checker_runs` row.

        Args:
            result: The subagent's task and result.
            context: Cached project/conversation context and recent turns.
            escalate_cut: `context.escalate_on_cut`: a review that could not show all of the task
                or the result (`cut_chars`) ends in ESCALATE whatever the model answered.

        Returns:
            The review, the token counts (None after a failure) and the duration in
            milliseconds.
        """
        outcome = await self._run(self.build_prompt(result, context), "result")
        cut = cut_chars(result.prompt, result.result)
        return _escalated_for_cut(outcome, cut, "task or result") if escalate_cut else outcome

    async def run_task(
        self, task: SubagentTask, context: CheckContext, escalate_cut: bool = True
    ) -> ResultOutcome:
        """Review a task Claude gives a subagent, before the subagent gets it.

        Never raises for model or network errors (see the class docstring).

        Args:
            task: The task that starts a subagent, or Claude's later message to one.
            context: Cached project/conversation context and recent turns.
            escalate_cut: `context.escalate_on_cut`, as for `run`.

        Returns:
            The review (its `summary` retells the task), the token counts (None after a
            failure) and the duration in milliseconds.
        """
        outcome = await self._run(self.build_task_prompt(task, context), "task")
        cut = cut_chars(task.prompt)
        return _escalated_for_cut(outcome, cut, "task") if escalate_cut else outcome

    async def _run(self, prompt: list[str | CachePoint], what: str) -> ResultOutcome:
        """Run one review and return it with its cost; never raises for model errors.

        Args:
            prompt: The user prompt parts.
            what: "task" or "result", for the review that replaces a failed call.

        Returns:
            The review, the token counts (None after a failure) and the duration in
            milliseconds.
        """
        started = time.monotonic()
        try:
            run = await run_bounded(
                self.agent,
                prompt,
                model=self.model,
                usage_limits=self.usage_limits,
                model_settings=self.settings(),
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # every failure must reach a human
            return ResultOutcome(
                review=ResultReview(
                    verdict=ResultVerdict.ESCALATE,
                    note=f"The subagent checker failed ({type(exc).__name__}: {exc}).",
                    summary=f"Subagent {what} not reviewed because the check failed.",
                ),
                duration_ms=elapsed_ms(started),
            )
        return ResultOutcome(
            review=run.output,
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            cache_read_tokens=run.usage.cache_read_tokens,
            duration_ms=elapsed_ms(started),
        )


def _escalated_for_cut(outcome: ResultOutcome, cut: int, what: str) -> ResultOutcome:
    """Turn a review that could not show everything into an escalation (D273).

    Args:
        outcome: The model's review.
        cut: Characters the review did not show (`cut_chars`); 0 leaves `outcome` as it is.
        what: What was cut, for the note ("task", "task or result").

    Returns:
        `outcome` unchanged when nothing was cut or it escalates already; otherwise the same
        cost with an ESCALATE review whose note names the cut and the model's own verdict.
    """
    review = outcome.review
    if not cut or review.verdict is ResultVerdict.ESCALATE:
        return outcome
    note = (
        f"The subagent's {what} was {cut} characters longer than the checker reads; it saw "
        f"the start and the end only (context.escalate_on_cut). The checker answered "
        f"{review.verdict.value}" + (f": {review.note}" if review.note else ".")
    )
    escalated = review.model_copy(update={"verdict": ResultVerdict.ESCALATE, "note": note})
    return replace(outcome, review=escalated)


def _require_note(ctx: RunContext[Any], output: ResultReview) -> ResultReview:
    """Output validator: ANNOTATE, DENY and ESCALATE need a non-empty note.

    A model that leaves `note` out often writes it into the summary, after a line that
    begins "Note:" (seen on 2026-11-27, see `_note_is_required`). Such a summary is split
    there: the text after it becomes the note.

    An ESCALATE without a note takes the summary as its note: a small model often writes
    what the user has to decide into the summary, and asking it again fails the check as often
    as it helps. The person who decides then reads that text. An ANNOTATE or DENY without a
    note is asked for again. On the last attempt an ANNOTATE without a note counts as ACCEPT:
    it has nothing to tell Claude, and a check that fails for it makes a person decide about
    an ordinary task or result.

    Args:
        ctx: The run's context; `retry` and `max_retries` say whether this is the last attempt.
        output: The model's review.

    Returns:
        The review; with the note taken out of the summary where the model wrote it there;
        for an ESCALATE without a note, a copy whose note is the summary; for an ANNOTATE
        without a note on the last attempt, a copy with the verdict ACCEPT.

    Raises:
        ModelRetry: If an ANNOTATE or DENY has no note and attempts are left, if a DENY
            has none on the last attempt, or if an ESCALATE has neither note nor summary.
    """
    if output.verdict is ResultVerdict.ACCEPT or output.note.strip():
        return output
    found = NOTE_IN_SUMMARY.search(output.summary)
    if found and output.summary[: found.start()].strip() and output.summary[found.end() :].strip():
        return output.model_copy(
            update={
                "summary": output.summary[: found.start()].strip(),
                "note": output.summary[found.end() :].strip(),
            }
        )
    if output.verdict is ResultVerdict.ESCALATE and output.summary.strip():
        return output.model_copy(update={"note": output.summary.strip()})
    if output.verdict is ResultVerdict.ANNOTATE and ctx.retry >= ctx.max_retries:
        return output.model_copy(update={"verdict": ResultVerdict.ACCEPT})
    raise ModelRetry("ANNOTATE, DENY and ESCALATE need a note.")
