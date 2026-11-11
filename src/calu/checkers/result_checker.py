"""The subagent result checker: reviews a subagent's final result against the conversation.

Request layout (cache points as for the step checkers):

    instructions   <- checkers/subagent-result.md
    user prompt    = [context prefix, CachePoint, recent turns, CachePoint,
                      subagent task (Claude's prompt) + subagent result]

See CALU_GUIDE.md "Checking subagent results" for the failure case it addresses (scope expansion
through reviewer subagents).
"""

import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_ai import Agent, CachePoint, ModelRetry, UsageLimits
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.checkers.checker import CheckContext, context_parts, elapsed_ms
from calu.checkers.model import CheckerEffort
from calu.markdown import fence
from calu.models import MAX_ANSWER_TOKENS, cache_settings, run_bounded

MAX_RESULT_CHARS = 30_000
"""Longer subagent results are cut (from the middle) before they are sent to the checker."""


class ResultVerdict(StrEnum):
    """Outcome of a result review.

    ACCEPT: the result fits the task's scope. ANNOTATE: Claude gets a triage note.
    ESCALATE: the user decides how to proceed.
    """

    ACCEPT = "ACCEPT"
    ANNOTATE = "ANNOTATE"
    ESCALATE = "ESCALATE"


class ResultReview(BaseModel):
    """Structured output of the result checker (field descriptions are shown to the model)."""

    verdict: ResultVerdict = Field(
        description="ACCEPT, ANNOTATE (triage note for Claude) or ESCALATE (the user decides)."
    )
    note: str = Field(
        default="",
        description=(
            "Triage note for Claude: which points of the result are in scope, out of scope, "
            "contradict a tradeoff the user accepted, or need the user's decision. Empty for "
            "ACCEPT. At most about 300 words."
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


@dataclass(frozen=True)
class SubagentResult:
    """The final result of a subagent and the task it was given.

    `agent_id` is the protocol agent id (id of the Agent tool call); `prompt` is the task text
    Claude gave the subagent; `background` tells whether it ran in the background.
    """

    agent_id: str | None
    description: str
    prompt: str
    result: str
    background: bool = False


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
    reached nothing (PLAN.md D135)."""
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
        description = result.description or "no description"
        parts.append(
            f"# Result to review\n\nA {kind} ({description}) finished.\n\n"
            f"Task Claude gave it:\n{fence(_cut_middle(result.prompt, 10_000))}\n\n"
            f"Its final result:\n{fence(_cut_middle(result.result))}"
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

    async def run(self, result: SubagentResult, context: CheckContext) -> ResultOutcome:
        """Review `result` and return the review with the cost of the model call.

        Never raises for model or network errors (see the class docstring); the session stores
        the outcome as a `checker_runs` row.

        Args:
            result: The subagent's task and result.
            context: Cached project/conversation context and recent turns.

        Returns:
            The review, the token counts (None after a failure) and the duration in
            milliseconds.
        """
        started = time.monotonic()
        try:
            run = await run_bounded(
                self.agent,
                self.build_prompt(result, context),
                model=self.model,
                usage_limits=self.usage_limits,
                model_settings=self.settings(),
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # every failure must reach a human
            return ResultOutcome(
                review=ResultReview(
                    verdict=ResultVerdict.ESCALATE,
                    note=f"The subagent result checker failed ({type(exc).__name__}: {exc}).",
                    summary="Subagent result not reviewed because the check failed.",
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


def _require_note(output: ResultReview) -> ResultReview:
    """Output validator: ANNOTATE and ESCALATE need a non-empty note.

    Args:
        output: The model's review.

    Returns:
        The unchanged review.

    Raises:
        ModelRetry: If the note is missing.
    """
    if output.verdict is not ResultVerdict.ACCEPT and not output.note.strip():
        raise ModelRetry("ANNOTATE and ESCALATE need a triage note.")
    return output
