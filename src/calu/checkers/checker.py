"""Checker agents: LLMs that review one step (command or edit) and return a `CheckDecision`.

A checker request has this layout; each text is its own content block:

    tool definitions (the output tool and the read-only code tools)    <- breakpoint (Caching)
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

A step that is not shown whole cannot be approved. calu cuts a command, diff or input that is
longer than `calu.checkers.render.MAX_CHARS`, and the commit diff and the write-set patch have
limits of their own. The model still judges the visible part (its DENY and ESCALATE stand, and
its summary is kept), but `Checker.run` turns its PASS or ALLOW into ESCALATE (`escalate_if_cut`):
no model has seen the rest, so a human decides.
"""

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, CachePoint, ModelRetry, UsageLimits
from pydantic_ai.capabilities import Caching, Toolset
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.checkers.model import CheckDecision, CheckerEffort, Decision, HonestyAnswer, Step
from calu.checkers.render import render_step_cut
from calu.checkers.rules import Rules
from calu.checkers.tools import CodeTools, StepTimes
from calu.models import MAX_ANSWER_TOKENS, cache_settings, run_bounded

log = logging.getLogger(__name__)

EXTRA_CUT = re.compile(
    r"^\[\.\.\. the diff (is cut here; the summary above is whole|was cut)\]$", re.MULTILINE
)
"""The lines with which calu marks a diff it cut before handing it to a checker as `extra`:
the commit diff (`calu.checkers.changeset.changeset`) and the write-set patch
(`calu.changes`). Every line of a git diff starts with a space, "+", "-", "@", "\\" or a
keyword, never with "[", so a line that **is** one of these notes was written by calu; a diff
that merely contains the words does not match. A test pins the two spellings to their sources.
The session also says so itself (`extra_cut`), from `Changeset.cut` and `ChangeSet.cut`; this
pattern is the second net for a caller that passes the text alone.
"""


class CheckPrompt(list[str | CachePoint]):
    """The user prompt parts of one check, with what the checker cannot see of the step."""

    unseen_chars: int = 0
    """Characters cut from the rendered step (`RenderedStep.unseen_chars`); 0 if it is whole."""


def escalate_if_cut(decision: CheckDecision, unseen_chars: int, checker: str) -> CheckDecision:
    """Turn an approval of a step that the checker saw only in part into an escalation.

    Args:
        decision: The checker model's decision.
        unseen_chars: Characters of the step that were cut from the prompt; -1 if a part is
            known to be missing but its size is not; 0 if the checker saw everything.
        checker: The checker's name, for the reason.

    Returns:
        `decision` unchanged if nothing was cut or if it does not approve (a DENY or ESCALATE
        on the visible part stands). Otherwise a copy with the decision ESCALATE, a reason that
        says how much was not shown, no comment and no cleared honesty marks: an approval of a
        part is not an approval, and it must not clear a record either.
    """
    if unseen_chars == 0 or not decision.decision.approves:
        return decision
    amount = f"{unseen_chars} characters" if unseen_chars > 0 else "A part"
    said = f" Its reason: {decision.reason.strip()}" if decision.reason.strip() else ""
    return decision.model_copy(
        update={
            "decision": Decision.ESCALATE,
            "reason": (
                f"{amount} of this step were not shown to the {checker} checker, because the "
                "step is longer than what calu puts in front of a checker. Its "
                f"{decision.decision.value} covers the visible part only, so a human has to "
                f"look at the whole step.{said}"
            ),
            "comment": "",
            "marks_clean": [],
        }
    )


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

    The agent has the read-only tools of `calu.checkers.tools` — reading a file or a range of
    its lines, finding files by glob, grepping, and, if `times` is given, the times of the
    session's steps and which step last changed a file — with which it can find out what the
    code actually does, and who did it, when the step alone does not say enough (PLAN.md D22,
    D25, D32). Those tools confine every read to `workspace` after resolving symbolic links
    and apply the read deny lists of `rules` (`CodeTools.confine`); calu enforces this itself
    and relies on no library for it. It has no other tool: no writes, no commands, no network.
    """

    name: str
    instructions: str
    model: str | Model
    workspace: Path
    rules: Rules | None = None
    """The workspace's rules; they bound what `read_file` may read (None allows every file)."""
    times: StepTimes | None = None
    """Where `step_times` and `changed_files` get the session's step times (D32); without it
    those two tools are not offered to the model."""
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=5, output_tokens_limit=24_000)
    )
    max_tokens: int = MAX_ANSWER_TOKENS
    """Cap on the output tokens of one answer; a cut-off answer is retried once (`run_bounded`)."""
    effort: CheckerEffort | None = None
    """How much the model may think (`calu.config.CheckerEffort`); None leaves the provider's
    default. It reaches the provider as Pydantic AI's `thinking` model setting."""
    _agent: Agent[None, Any] | None = field(default=None, init=False, repr=False)
    _built_for: str | None = field(default=None, init=False, repr=False)
    cache_key: str = field(default_factory=lambda: f"calu-{secrets.token_hex(6)}")
    """Routing hint for providers with automatic prompt caching (one per checker and core run)."""

    @property
    def judges_honesty(self) -> bool:
        """Whether this is the honesty checker, which answers per file (PLAN.md D217).

        Returns:
            True for the checker type `HONESTY_CHECKER`: its model answers with an
            `HonestyAnswer` (HONEST or DISHONEST per file), which `run` turns into the
            decision every other checker gives directly.
        """
        return self.name == HONESTY_CHECKER

    @property
    def agent(self) -> Agent[None, Any]:
        """The Pydantic AI agent, rebuilt if `instructions` changed.

        Returns:
            The agent with the current instructions and the validator of its answer:
            `_require_reason` for a `CheckDecision`, `_require_verdict_reason` for the
            honesty checker's `HonestyAnswer`.
        """
        instructions = self.instructions
        if self._agent is None or instructions != self._built_for:
            tools = CodeTools(workspace=self.workspace, rules=self.rules, times=self.times)
            capabilities: list[Any] = [
                Caching(messages=False),
                # The only tools: calu's own read-only ones, which confine every read to
                # the project directory themselves (`CodeTools.confine`).
                Toolset(tools.toolset()),
            ]
            name = f"{self.name}-checker"
            agent: Agent[None, Any]
            if self.judges_honesty:
                judging: Agent[None, HonestyAnswer] = Agent(
                    output_type=HonestyAnswer,
                    instructions=instructions,
                    name=name,
                    capabilities=capabilities,
                )
                judging.output_validator(_require_verdict_reason)
                agent = judging
            else:
                deciding: Agent[None, CheckDecision] = Agent(
                    output_type=CheckDecision,
                    instructions=instructions,
                    name=name,
                    capabilities=capabilities,
                )
                deciding.output_validator(_require_reason)
                agent = deciding
            self._agent, self._built_for = agent, instructions
        return self._agent

    def build_prompt(self, step: Step, context: CheckContext, extra: str = "") -> CheckPrompt:
        """Return the user prompt parts for checking `step` with `context`.

        Empty context parts and their cache points are left out (layout: module docstring).

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.
            extra: A further block after the step, for a checker that judges more than the call
                itself — the changeset checker gets the diff of the commit here (D26). Empty
                for the step checkers.

        Returns:
            Prompt texts with `CachePoint`s (layout: module docstring), and in `unseen_chars`
            the number of characters that were cut from the step.

        Raises:
            ValueError: From `render_step_cut`, for a target path with a NUL character.
            AttributeError: From `render_step_cut`, for malformed MultiEdit input.
            TypeError: From `render_step_cut`, for tool input that is not JSON-serializable.
        """
        rendered = render_step_cut(step, self.workspace)
        parts = CheckPrompt(context_parts(context, "# Project context"))
        parts.unseen_chars = rendered.unseen_chars
        parts.append("# Step to check\n\n" + rendered.text)
        if extra:
            parts.append(extra)
        return parts

    async def check(
        self,
        step: Step,
        context: CheckContext,
        raise_errors: bool = False,
        extra: str = "",
        extra_cut: bool = False,
    ) -> CheckDecision:
        """Review `step` with the checker model and return its decision.

        Like `run`, but returns the decision alone.

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.
            raise_errors: If True, failures are raised instead (evaluation counts them as
                errors, not as escalations).
            extra: A further prompt block after the step (see `build_prompt`).
            extra_cut: True if `extra` leaves out a part of what it describes (see `run`).

        Returns:
            The model's decision, or the ESCALATE decision described in `run`.

        Raises:
            Exception: Only with `raise_errors`: whatever the model call or rendering raised.
        """
        return (await self.run(step, context, raise_errors, extra, extra_cut)).decision

    def settings(self) -> ModelSettings:
        """Build the model settings of one check: the cache routing key and the effort.

        Returns:
            `cache_settings` for this checker, with `thinking` added when `effort` is set:
            "off" asks for no thinking, any other level names it. A provider whose model has no
            thinking mode ignores the setting.
        """
        settings = cache_settings(self.model, f"{self.cache_key}-{self.name}")
        if self.effort is not None:
            settings["thinking"] = False if self.effort == "off" else self.effort
        return settings

    async def run(
        self,
        step: Step,
        context: CheckContext,
        raise_errors: bool = False,
        extra: str = "",
        extra_cut: bool = False,
    ) -> CheckOutcome:
        """Review `step` and return the decision with the cost of the model call.

        A PASS or ALLOW becomes ESCALATE if the checker did not see the whole step
        (`escalate_if_cut`): the rendered command, diff or input was cut
        (`RenderedStep.unseen_chars`), `extra` holds one of calu's cut notes (`EXTRA_CUT`), or
        the caller says so with `extra_cut`.

        By default it never raises (except cancellation): any failure, including instructions
        the model rejects or a step that cannot be rendered, yields an ESCALATE decision whose
        reason names the error, so that a human decides in live operation. The session stores
        the outcome as a `checker_runs` row.

        Args:
            step: The tool call to check.
            context: Context prefix and recent summaries.
            raise_errors: If True, failures are raised instead (evaluation counts them as
                errors, not as escalations).
            extra: A further prompt block after the step (see `build_prompt`).
            extra_cut: True if `extra` leaves out a part of what it describes without one of
                the notes of `EXTRA_CUT`, e.g. a write set whose binary or oversized files are
                not in the patch (`calu.changes.ChangeSet.cut`).

        Returns:
            The decision, the token counts (None after a failure) and the duration in
            milliseconds.

        Raises:
            Exception: Only with `raise_errors`: whatever the model call or rendering raised.
        """
        started = time.monotonic()
        try:
            prompt = self.build_prompt(step, context, extra)
            # One rendering gives both the prompt and the size of the cut: the diff of an edit
            # is computed from the file on disk, which can change between two renderings.
            unseen = (
                prompt.unseen_chars
                if isinstance(prompt, CheckPrompt)
                else render_step_cut(step, self.workspace).unseen_chars
            )
            result = await run_bounded(
                self.agent,
                prompt,
                model=self.model,
                usage_limits=self.usage_limits,
                model_settings=self.settings(),
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
        unseen = unseen or (-1 if extra_cut or EXTRA_CUT.search(extra) else 0)
        answered = result.output
        if isinstance(answered, HonestyAnswer):
            answered = answered.decision()
        return CheckOutcome(
            decision=escalate_if_cut(answered, unseen, self.name),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=elapsed_ms(started),
        )


HONESTY_CHECKER = "honesty"
"""Name of the checker type that judges honesty per file instead of approving a step."""


def _require_verdict_reason(output: HonestyAnswer) -> HonestyAnswer:
    """Ask the honesty checker again when a DISHONEST verdict or an escalation says nothing.

    Args:
        output: The model's answer.

    Returns:
        The answer unchanged.

    Raises:
        ModelRetry: If a DISHONEST file has neither a reason nor a dishonest unit with one,
            or `escalate` is set without `escalate_reason`.
    """
    for entry in output.files:
        said = entry.reason.strip() or any(
            unit.reason.strip() for unit in entry.units if not unit.honest
        )
        if entry.verdict == "DISHONEST" and not said:
            raise ModelRetry(f"Say briefly why {entry.path} is DISHONEST.")
    if output.escalate and not output.escalate_reason.strip():
        raise ModelRetry("Say in escalate_reason what the human should look at.")
    return output


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
    if not output.decision.approves and not output.reason.strip():
        raise ModelRetry("A DENY or ESCALATE decision needs a reason.")
    return output
