"""The honesty reviewer: an agent that judges whole files or their changes for honesty.

An honesty review (`calu.session.Session.honesty_review`, specification section 4) judges the
files that are unreviewed or carry marks. calu computes which files those are and what the
reviewer is shown (`calu.honesty.HonestyState.review_plan`); this module only asks the model:

    files of the plan
      -> batches (`batches`: at most `batch_files` files and `BATCH_CHARS` characters each)
      -> one agent run per batch, at most `concurrency` at a time, each with `usage_limits`
      -> one `FileReview` per file: honest, or findings (symbol, reason)

The agent has the instructions `system/honesty-review.md` followed by the honesty rules, and
the read-only tools of `calu.checkers.tools`. The files' texts are untrusted data and are put
in code fences that they cannot close.

A review counts only as a whole. If one batch fails (a model error, a run over its limits, an
answer that does not name every file of the batch exactly once or contradicts itself), or the
review takes longer than `timeout_s`, the review has failed and returns no result for any
file, so nothing is verified.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.capabilities import Caching, Toolset
from pydantic_ai.models import Model

from calu.checkers.rules import Rules
from calu.checkers.tools import CodeTools
from calu.honesty import (
    NOTE_PATH_CHARS,
    NOTE_REASON_CHARS,
    FileReview,
    Finding,
    ReviewFile,
    shown,
)
from calu.markdown import fence
from calu.models import MAX_ANSWER_TOKENS, cache_settings, run_bounded

log = logging.getLogger(__name__)

BATCH_CHARS = 120_000
"""The largest number of characters of file texts in one batch. A file whose text alone is
longer gets a batch of its own (`calu.honesty.REVIEW_FILE_BYTES` bounds the text of one file)."""


class ReviewFinding(BaseModel):
    """One spot of a file that the reviewer found dishonest."""

    symbol: str = Field(
        default="",
        description=(
            "The function, method or class, e.g. `fields` or `Parser.fields`. Empty if the "
            "whole file is meant."
        ),
    )
    reason: str = Field(
        description="What disagrees with what, and what would settle it. One or two sentences."
    )


class FileVerdict(BaseModel):
    """The reviewer's answer about one file of a batch."""

    file: int = Field(description="The number of the file, as its heading gives it.")
    honest: bool = Field(
        description=(
            "True if the names and the documentation of the shown code say what it does, by "
            "the honesty rules. False needs at least one finding."
        )
    )
    findings: list[ReviewFinding] = Field(
        default_factory=list[ReviewFinding], description="Empty for an honest file."
    )


class ReviewAnswer(BaseModel):
    """The reviewer's answer about one batch: one verdict per file."""

    files: list[FileVerdict] = Field(description="One entry for every file of the request.")


class ReviewFailed(Exception):
    """One batch of an honesty review gave no usable answer; the message says why."""


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of one honesty review by the model."""

    results: dict[str, FileReview] = field(default_factory=dict[str, FileReview])
    """Path -> the verdict, for every file that was handed in. Empty if `problem` is set."""
    problem: str = ""
    """"" if every batch answered; otherwise one sentence saying why the review failed."""
    input_tokens: int = 0
    output_tokens: int = 0


def batches(
    files: Sequence[ReviewFile], batch_files: int, batch_chars: int = BATCH_CHARS
) -> list[list[ReviewFile]]:
    """Split the files of a review into batches, in the given order.

    Args:
        files: The files for the reviewer.
        batch_files: The largest number of files in one batch; at least 1.
        batch_chars: The largest sum of the lengths of the files' texts in one batch.

    Returns:
        The batches. A batch is closed when the next file would make it larger than either
        limit; a file whose text alone is longer than `batch_chars` is a batch of its own.
    """
    found: list[list[ReviewFile]] = []
    current: list[ReviewFile] = []
    size = 0
    for file in files:
        if current and (len(current) >= batch_files or size + len(file.text) > batch_chars):
            found.append(current)
            current, size = [], 0
        current.append(file)
        size += len(file.text)
    if current:
        found.append(current)
    return found


def batch_prompt(batch: Sequence[ReviewFile], baseline: str) -> str:
    """Render the request for one batch.

    Args:
        batch: The files of the batch.
        baseline: The short id of the baseline commit, or "" without a baseline.

    Returns:
        A heading, one line about the baseline, and per file: "## File N: path", whether the
        whole file or its change since the baseline follows, the open marks on it, and the
        text in a code fence. Paths, symbols and reasons are rendered on one line (`shown`).
    """
    parts = [
        "# Files to review",
        (
            f"The baseline is the trusted commit {baseline}. Its code is verified."
            if baseline
            else "There is no baseline: no code of this project is verified yet."
        )
        + f" Answer for each of the {len(batch)} file(s) below.",
    ]
    for number, file in enumerate(batch, start=1):
        lines = [f"## File {number}: {shown(file.path, NOTE_PATH_CHARS)}"]
        if file.marks:
            spots = "; ".join(
                (shown(mark.symbol, NOTE_PATH_CHARS) or "the whole file")
                + f" ({shown(mark.reason, NOTE_REASON_CHARS)})"
                for mark in file.marks
            )
            lines.append(f"Recorded as dishonest before this review: {spots}.")
        lines.append(
            "The whole file:"
            if file.whole
            else "The change of the file since the baseline, as a unified diff:"
        )
        lines.append(fence(file.text, "" if file.whole else "diff"))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def verdicts(batch: Sequence[ReviewFile], answer: ReviewAnswer) -> dict[str, FileReview]:
    """Turn the reviewer's answer about a batch into the result per file.

    Args:
        batch: The files of the batch, in the order of their numbers.
        answer: The model's answer.

    Returns:
        Path -> `FileReview` with the SHA-256 and the mark ids of the plan, and the findings.

    Raises:
        ReviewFailed: If the answer does not name every file of the batch exactly once, calls
            a file honest and names findings for it, calls it dishonest without a finding, or
            has a finding without a reason.
    """
    numbers = sorted(verdict.file for verdict in answer.files)
    if numbers != list(range(1, len(batch) + 1)):
        raise ReviewFailed(
            f"the answer names the files {numbers} of a batch of {len(batch)} file(s)"
        )
    found: dict[str, FileReview] = {}
    for verdict in answer.files:
        file = batch[verdict.file - 1]
        if verdict.honest == bool(verdict.findings):
            raise ReviewFailed(
                f"the answer about file {verdict.file} says honest={verdict.honest} with "
                f"{len(verdict.findings)} finding(s)"
            )
        if any(not finding.reason.strip() for finding in verdict.findings):
            raise ReviewFailed(f"a finding about file {verdict.file} has no reason")
        found[file.path] = FileReview(
            file.sha256,
            tuple(
                Finding(finding.symbol.strip(), finding.reason.strip())
                for finding in verdict.findings
            ),
            marks=tuple(mark.id for mark in file.marks),
        )
    return found


@dataclass
class HonestyReviewer:
    """The reviewer agent of an honesty review, with its budget.

    `model` is resolved at run time, so constructing a reviewer needs no API key. The agent's
    only tools are calu's read-only code tools, which confine every read to `workspace` and
    apply the read deny lists of `rules` (`calu.checkers.tools.CodeTools`).
    """

    instructions: str
    model: str | Model
    workspace: Path
    rules: Rules | None = None
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=30, output_tokens_limit=60_000)
    )
    """The budget of one batch (`honesty.reviewer_request_limit`,
    `honesty.reviewer_output_tokens_limit`)."""
    max_tokens: int = MAX_ANSWER_TOKENS
    """Cap on the output tokens of one answer."""
    batch_files: int = 8
    concurrency: int = 4
    timeout_s: float = 600.0
    cache_key: str = field(default_factory=lambda: f"calu-{secrets.token_hex(6)}")
    """Routing hint for providers with automatic prompt caching."""

    def agent(self) -> Agent[None, ReviewAnswer]:
        """Build the Pydantic AI agent of one review.

        Returns:
            An agent with the instructions, the output type `ReviewAnswer` and the read-only
            code tools.
        """
        tools = CodeTools(workspace=self.workspace, rules=self.rules)
        return Agent(
            output_type=ReviewAnswer,
            instructions=self.instructions,
            name="honesty-reviewer",
            capabilities=[Caching(messages=False), Toolset(tools.toolset())],
        )

    async def review(
        self,
        files: Sequence[ReviewFile],
        baseline: str = "",
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> ReviewOutcome:
        """Judge the files, in batches that run at the same time.

        Side effects: model calls; the model's tool calls read files of the workspace.

        Args:
            files: The files of the plan that need the model. Empty returns at once.
            baseline: The short id of the baseline commit, or "".
            progress: Called after each batch that answered, with the number of finished
                batches and the number of batches. An exception it raises fails the review.

        Returns:
            With `problem` "": one result for every file. Otherwise no result at all and the
            reason: a batch failed (the other batches are cancelled) or the review took
            longer than `timeout_s`. Never raises, except for cancellation from outside.
        """
        groups = batches(files, self.batch_files)
        if not groups:
            return ReviewOutcome()
        agent = self.agent()
        settings = cache_settings(self.model, f"{self.cache_key}-honesty-review")
        results: dict[str, FileReview] = {}
        problems: list[str] = []
        tokens = [0, 0]
        done = 0
        limiter = anyio.Semaphore(max(1, self.concurrency))

        async def run(number: int, batch: Sequence[ReviewFile], scope: anyio.CancelScope) -> None:
            """Review one batch and keep its result; on a failure stop the other batches.

            Args:
                number: The batch's number, from 1, for the problem text.
                batch: Its files.
                scope: The scope around all batches.
            """
            nonlocal done
            async with limiter:
                try:
                    result = await run_bounded(
                        agent,
                        batch_prompt(batch, baseline),
                        model=self.model,
                        usage_limits=self.usage_limits,
                        model_settings=settings,
                        max_tokens=self.max_tokens,
                    )
                    results.update(verdicts(batch, result.output))
                    tokens[0] += result.usage.input_tokens
                    tokens[1] += result.usage.output_tokens
                    done += 1
                    if progress is not None:
                        await progress(done, len(groups))
                except Exception as exc:
                    log.warning("honesty review: batch %d failed", number, exc_info=True)
                    problems.append(
                        f"batch {number} of {len(groups)} failed ({type(exc).__name__}: {exc})"
                    )
                    scope.cancel()

        with anyio.move_on_after(self.timeout_s) as timeout:
            async with anyio.create_task_group() as group:
                for number, batch in enumerate(groups, start=1):
                    group.start_soon(run, number, batch, group.cancel_scope)
        if problems:
            return ReviewOutcome(problem=f"The honesty review failed: {problems[0]}.")
        if timeout.cancelled_caught or len(results) != len(files):
            return ReviewOutcome(
                problem=(
                    f"The honesty review failed: it took longer than {self.timeout_s:g} "
                    "seconds (honesty.review_timeout_s)."
                )
            )
        return ReviewOutcome(results=results, input_tokens=tokens[0], output_tokens=tokens[1])
