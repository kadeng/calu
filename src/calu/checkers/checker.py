"""Checker agents: LLMs that review one step (command or edit) and return a `CheckDecision`.

A checker request has this layout; each text is its own content block:

    tool definitions (output tool)                                     <- breakpoint (Caching)
    instructions: the checker's Markdown file                          <- breakpoint (Caching)
    context prefix: project files, CHAT_SUMMARY.md
    completed turns of the session
    running turn: head, step 1, ..., step n-1                          <- CachePoint (read)
                  step n                                               <- CachePoint (write)
    step to check

Caching per checked tool call: the previous check ended its cached prefix after its last step,
which is this check's step n-1; this check repeats all blocks before it byte for byte
(`TurnRecord.render_parts`), so the CachePoint after step n-1 reads that entry and the one after
step n writes the next. Without steps yet, the two CachePoints sit after the prefix and after
the completed turns. Pydantic AI's `Caching(messages=False)` capability adds the breakpoints
after the tool definitions and instructions on Anthropic and Bedrock: four in total, their
maximum. Providers with automatic prefix caching (Mistral, OpenAI) use the stable order, helped
by a per-checker routing key (`calu.models.cache_settings`); hybrid-SSM models such as
Nemotron on Fireworks only reuse a cache for identical prompts. The context comes from the
ContextBuilder (`calu.context`).
"""

import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai import Agent, CachePoint, ModelRetry, UsageLimits
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model

from calu.checkers.model import CheckDecision, Decision, Step
from calu.checkers.render import render_step
from calu.models import MAX_ANSWER_TOKENS, cache_settings, run_bounded

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckContext:
    """Context for one check.

    `prefix` changes rarely (project files, CHAT_SUMMARY.md). `turns` holds the completed
    unfolded turns, `current` the pieces of the running turn (head, then one per step; see
    `TurnRecord.render_parts`). The layout and caching: module docstring.
    """

    prefix: str = ""
    turns: str = ""
    current: tuple[str, ...] = ()

    @property
    def recent(self) -> str:
        """The completed turns and the running turn as one text (for display and training).

        Returns:
            `turns` and the joined `current` pieces, separated by a blank line.
        """
        return "\n\n".join(p for p in (self.turns, "".join(self.current)) if p)


@dataclass(frozen=True)
class CheckOutcome:
    """The result of one checker model call: the decision and what it cost.

    The token counts are None if the call failed (then `decision` is an ESCALATE decision that
    names the error). `duration_ms` is the wall-clock time of the whole call, retries included.
    """

    decision: CheckDecision
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int = 0


def elapsed_ms(started: float) -> int:
    """Return the milliseconds since a `time.monotonic()` reading.

    Args:
        started: The earlier reading.

    Returns:
        The elapsed time in milliseconds, rounded down.
    """
    return int((time.monotonic() - started) * 1000)


def context_parts(context: CheckContext, prefix_heading: str) -> list[str | CachePoint]:
    """Return the context blocks of a checker prompt with (at most two) cache points.

    Layout and the reason for the cache point positions: module docstring. Empty parts are
    left out. Of the candidate positions (after the prefix, after the completed turns, after
    each of the last two steps) only the last two are kept, because the `Caching` capability
    uses two more and providers accept four.

    Args:
        context: The check context.
        prefix_heading: Heading of the prefix block, e.g. "# Project context".

    Returns:
        Texts and `CachePoint`s, to be followed by the item to check.
    """
    parts: list[str | CachePoint] = []
    if context.prefix:
        parts += [f"{prefix_heading}\n\n{context.prefix}", CachePoint()]
    heading = "# Recent steps\n\n"
    if context.turns:
        parts += [heading + context.turns, CachePoint()]
        heading = "\n\n"
    current = list(context.current)
    for i, piece in enumerate(current):
        parts.append((heading if i == 0 else "") + piece)
        if i >= len(current) - 2:
            parts.append(CachePoint())
    points = [i for i, p in enumerate(parts) if isinstance(p, CachePoint)]
    drop = set(points[:-2])
    return [p for i, p in enumerate(parts) if i not in drop]


@dataclass
class Checker:
    """A checker agent with Markdown system instructions (stored in the database).

    The agent is (re)built lazily whenever `instructions` changed since the last build, so a
    session that assigns new instructions (after a `config_changed` notification) applies them at
    the next check. `model` is a Pydantic AI model string
    such as "mistral:ministral-3b-latest" or a `Model` instance; it is resolved at run time, so
    constructing a `Checker` needs no API key.

    `check` does not raise for model, network or file errors: such failures yield an ESCALATE
    decision whose reason names the error, so a human decides instead.
    """

    name: str
    instructions: str
    model: str | Model
    workspace: Path
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=3, output_tokens_limit=8_000)
    )
    max_tokens: int = MAX_ANSWER_TOKENS
    """Cap on the output tokens of one answer; a cut-off answer is retried once (`run_bounded`)."""
    _agent: Agent[None, CheckDecision] | None = field(default=None, init=False, repr=False)
    _built_for: str | None = field(default=None, init=False, repr=False)
    cache_key: str = field(default_factory=lambda: f"calu-{secrets.token_hex(6)}")
    """Routing hint for providers with automatic prompt caching (one per checker and core run)."""

    @property
    def agent(self) -> Agent[None, CheckDecision]:
        """The Pydantic AI agent, rebuilt if `instructions` changed.

        Returns:
            The agent with the current instructions and the `_require_reason` validator.
        """
        instructions = self.instructions
        if self._agent is None or instructions != self._built_for:
            agent: Agent[None, CheckDecision] = Agent(
                output_type=CheckDecision,
                instructions=instructions,
                name=f"{self.name}-checker",
                capabilities=[Caching(messages=False)],
            )
            agent.output_validator(_require_reason)
            self._agent, self._built_for = agent, instructions
        return self._agent

    def build_prompt(self, step: Step, context: CheckContext) -> list[str | CachePoint]:
        """Return the user prompt parts for checking `step` with `context`.

        Empty context parts and their cache points are left out (layout: module docstring).

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.

        Returns:
            Prompt texts with `CachePoint`s (layout: module docstring).

        Raises:
            ValueError: From `render_step`, for a target path with a NUL character.
            AttributeError: From `render_step`, for malformed MultiEdit input.
            TypeError: From `render_step`, for tool input that is not JSON-serializable.
        """
        parts = context_parts(context, "# Project context")
        parts.append("# Step to check\n\n" + render_step(step, self.workspace))
        return parts

    async def check(
        self, step: Step, context: CheckContext, raise_errors: bool = False
    ) -> CheckDecision:
        """Review `step` with the checker model and return its decision.

        Like `run`, but returns the decision alone.

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.
            raise_errors: If True, failures are raised instead (evaluation counts them as
                errors, not as escalations).

        Returns:
            The model's decision, or the ESCALATE decision described in `run`.

        Raises:
            Exception: Only with `raise_errors`: whatever the model call or rendering raised.
        """
        return (await self.run(step, context, raise_errors)).decision

    async def run(
        self, step: Step, context: CheckContext, raise_errors: bool = False
    ) -> CheckOutcome:
        """Review `step` and return the decision with the cost of the model call.

        By default it never raises (except cancellation): any failure, including instructions
        the model rejects or a step that cannot be rendered, yields an ESCALATE decision whose
        reason names the error, so that a human decides in live operation. The session stores
        the outcome as a `checker_runs` row.

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.
            raise_errors: If True, failures are raised instead (evaluation counts them as
                errors, not as escalations).

        Returns:
            The decision, the token counts (None after a failure) and the duration in
            milliseconds.

        Raises:
            Exception: Only with `raise_errors`: whatever the model call or rendering raised.
        """
        started = time.monotonic()
        try:
            result = await run_bounded(
                self.agent,
                self.build_prompt(step, context),
                model=self.model,
                usage_limits=self.usage_limits,
                model_settings=cache_settings(self.model, f"{self.cache_key}-{self.name}"),
                max_tokens=self.max_tokens,
            )
            usage = result.usage
            log.info(
                "%s checker: %d input tokens (cache read %d, cache write %d), %d output tokens",
                self.name,
                usage.input_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.output_tokens,
            )
        except Exception as exc:
            if raise_errors:
                raise
            return CheckOutcome(
                decision=CheckDecision(
                    decision=Decision.ESCALATE,
                    reason=f"The {self.name} checker failed ({type(exc).__name__}: {exc}).",
                    step_summary=(
                        f"{step.tool_name} call; not summarized because the check failed."
                    ),
                ),
                duration_ms=elapsed_ms(started),
            )
        return CheckOutcome(
            decision=result.output,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=elapsed_ms(started),
        )


def _require_reason(output: CheckDecision) -> CheckDecision:
    """Check that DENY and ESCALATE decisions carry a non-empty reason (output validator).

    Args:
        output: The decision returned by the model.

    Returns:
        `output` unchanged.

    Raises:
        ModelRetry: If a DENY or ESCALATE decision has an empty reason; Pydantic AI then asks
            the model again.
    """
    if output.decision is not Decision.ALLOW and not output.reason.strip():
        raise ModelRetry("A DENY or ESCALATE decision needs a reason.")
    return output
