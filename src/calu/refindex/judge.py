"""The reference judge: a model that decides whether a place refers to a unit.

A word search finds many places where a function's name occurs. For some of them no program
can tell whether the place means that function. The judge is a small model with this one
task, like the narrator. It is the last and most expensive stage of the combined index
(`calu.refindex.combined`) and only sees the places the cheaper stages could not decide.

How a question is answered:

1. The judge is shown the unit and the first lines of its definition.
2. It is shown each place with `CONTEXT_LINES` lines before and after it, at most
   `BATCH_PLACES` places per request.
3. It answers "yes", "no" or "unsure" for each place.
4. If it is unsure and says that the whole file would help, it is shown that file once and
   asked again.

A place stays unclear when the judge is unsure, gives no answer for it, the request fails,
or the file is too large to show. The judge never turns a failure into a "no".

Answers are kept and reused (`JudgeCache`):

- A "yes" or "no" depends only on what the judge was shown. So it is stored under the unit's
  file and name plus a hash of the shown lines.
- Line numbers are not part of that hash. Adding lines far above a place does not cause a
  new question.
- The unit's own documentation is not part of it either. Referrers are asked for exactly
  when that documentation changed, and the old answers must still count then.
- "Unsure" is not stored, so such a place is asked about again next time.
- By default the answers live in memory for as long as the `MatchJudge` exists. A store that
  outlasts the session can be passed in.

Safety: everything the judge reads is quoted as data (`calu.markdown.fence`), never given as
an instruction. The judge has no tools.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.defaults.instructions import reference_judge_instructions
from calu.markdown import fence
from calu.models import run_bounded
from calu.refindex.model import Place, Unit

log = logging.getLogger(__name__)

CONTEXT_LINES = 12
"""Lines shown before and after a place (the owner, 2026-11-27)."""

BATCH_PLACES = 10
"""Most places in one request of round 1."""

WHOLE_FILE_BYTES = 200_000
"""A file larger than this is not shown whole; its places stay unclear."""

Answer = Literal["yes", "no", "unsure"]


class JudgeCache(Protocol):
    """Where answers of the judge are kept, by a key that names what it was shown."""

    def get(self, key: str) -> Answer | None:
        """Look an answer up.

        Args:
            key: The key (`MatchJudge._key`).

        Returns:
            "yes" or "no" if that was answered for the key before; None otherwise.
        """
        ...

    def put(self, key: str, answer: Answer) -> None:
        """Keep an answer.

        Args:
            key: The key.
            answer: "yes" or "no"; "unsure" is never kept.
        """
        ...


@dataclass
class MemoryJudgeCache:
    """A `JudgeCache` in memory, with at most `limit` answers; the oldest go first."""

    limit: int = 50_000
    _answers: dict[str, Answer] = field(default_factory=dict, repr=False)

    def get(self, key: str) -> Answer | None:
        """Look an answer up.

        Args:
            key: The key.

        Returns:
            The kept answer; None if there is none.
        """
        return self._answers.get(key)

    def put(self, key: str, answer: Answer) -> None:
        """Keep an answer, dropping the oldest one when the cache is full.

        Args:
            key: The key.
            answer: The answer.
        """
        self._answers.pop(key, None)
        if len(self._answers) >= self.limit:
            del self._answers[next(iter(self._answers))]
        self._answers[key] = answer


class PlaceVerdict(BaseModel):
    """The judge's answer about one place (field descriptions are shown to the model)."""

    number: int = Field(description="The number of the place in the request.")
    refers: Answer = Field(description="yes, no or unsure: does the place refer to the unit?")
    needs_file: bool = Field(
        default=False,
        description="true with unsure, if seeing the whole file would decide it.",
    )
    reason: str = Field(default="", description="A few words: what the name is at this place.")


class JudgeAnswer(BaseModel):
    """The judge's answer to one request."""

    places: list[PlaceVerdict] = Field(description="One entry per place of the request.")


@dataclass
class MatchJudge:
    """The agent that judges places (see the module docstring)."""

    root: Path
    """The project directory."""
    model: str | Model
    """A Pydantic AI model string or a `Model`."""
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=3, output_tokens_limit=8_000)
    )
    cache: JudgeCache = field(default_factory=MemoryJudgeCache)
    """Where the yes and no answers are kept."""
    _agent: Agent[None, JudgeAnswer] | None = field(default=None, init=False, repr=False)

    @property
    def agent(self) -> Agent[None, JudgeAnswer]:
        """The Pydantic AI agent, built at first use with the fixed instructions.

        Returns:
            The agent.
        """
        if self._agent is None:
            self._agent = Agent(
                output_type=JudgeAnswer,
                instructions=reference_judge_instructions(),
                name="reference-judge",
            )
        return self._agent

    async def judge(self, unit: Unit, places: Sequence[Place]) -> dict[Place, Answer]:
        """Decide for each place whether it refers to `unit`.

        Side effects: model calls for the places without a kept answer; reads the files of
        the places; keeps every yes and no in `cache`.

        Args:
            unit: The unit.
            places: The places to judge.

        Returns:
            An answer for every place. "unsure" where the judge said so, gave no answer, the
            request failed, or the file could not be shown. Never raises for a model error.
        """
        answers: dict[Place, Answer] = dict.fromkeys(places, "unsure")
        wanted: list[Place] = []
        head = self._unit_text(unit)
        excerpts = {place: self._excerpt(place) for place in places}
        keys = {place: self._key(unit, place, excerpts[place]) for place in places}
        open_places: list[Place] = []
        for place in places:
            kept = self.cache.get(keys[place])
            if kept is None:
                open_places.append(place)
            else:
                answers[place] = kept
        for start in range(0, len(open_places), BATCH_PLACES):
            batch = open_places[start : start + BATCH_PLACES]
            shown = [excerpts[place] for place in batch]
            for place, verdict in (await self._ask(head, batch, shown)).items():
                self._answered(answers, keys[place], place, verdict.refers)
                if verdict.refers == "unsure" and verdict.needs_file:
                    wanted.append(place)
        for path in dict.fromkeys(place.path for place in wanted):
            batch = [place for place in wanted if place.path == path]
            whole = self._whole(path, batch)
            if whole is None:
                continue
            for place, verdict in (await self._ask(head, batch, [whole] * len(batch))).items():
                # Kept under the key of the whole file: the excerpt alone did not decide it.
                self._answered(answers, self._key(unit, place, whole), place, verdict.refers)
        return answers

    def _answered(
        self, answers: dict[Place, Answer], key: str, place: Place, answer: Answer
    ) -> None:
        """Record one answer of the judge, and keep a yes or a no in the cache.

        Args:
            answers: The answers of this call; changed in place.
            key: The cache key of what the judge was shown.
            place: The place.
            answer: The judge's answer.
        """
        answers[place] = answer
        if answer != "unsure":
            self.cache.put(key, answer)

    def _key(self, unit: Unit, place: Place, shown: str) -> str:
        """Build the cache key of one answer: the unit, and what the judge was shown.

        The shown lines count without their numbers, so that lines added above the place do
        not change the key; which line is the place is part of it. The unit counts by its
        file and name, not by its text.

        Args:
            unit: The unit.
            place: The place; its file and the column of the name are part of the key.
            shown: The fenced text shown for the place (`_excerpt` or `_whole`).

        Returns:
            A hexadecimal SHA-256.
        """
        lines = [
            line[:2] + line.partition(": ")[2] if line[:2] in (">>", "  ") else line
            for line in shown.splitlines()
        ]
        text = "\n".join([unit.path, unit.qualname, place.path, str(place.column), *lines])
        return hashlib.sha256(text.encode()).hexdigest()

    async def _ask(
        self, head: str, batch: list[Place], shown: list[str]
    ) -> dict[Place, PlaceVerdict]:
        """Send one request about the places of `batch`.

        Args:
            head: The description of the unit.
            batch: The places.
            shown: The text shown for each place; equal texts are sent once.

        Returns:
            The verdicts by place, for the places the judge answered; empty if the request
            failed.
        """
        parts = [head, "# Places"]
        sent: set[str] = set()
        for number, (place, text) in enumerate(zip(batch, shown, strict=True), start=1):
            parts.append(f"## Place {number}: {place.path}, line {place.line}")
            if text not in sent:
                parts.append(text)
                sent.add(text)
            else:
                parts.append("(in the file shown above)")
        try:
            run = await run_bounded(
                self.agent,
                "\n\n".join(parts),
                model=self.model,
                usage_limits=self.usage_limits,
                model_settings=ModelSettings(),
            )
        except Exception:
            log.exception("the reference judge failed")
            return {}
        found: dict[Place, PlaceVerdict] = {}
        for verdict in run.output.places:
            if 1 <= verdict.number <= len(batch):
                found[batch[verdict.number - 1]] = verdict
        return found

    def _lines(self, path: str) -> list[str] | None:
        """Read the lines of one file of the project.

        Args:
            path: The file, relative to the project directory.

        Returns:
            The lines; None if the file cannot be read or is over `WHOLE_FILE_BYTES`.
        """
        try:
            data = (self.root / path).read_bytes()
        except OSError:
            return None
        if len(data) > WHOLE_FILE_BYTES:
            return None
        return data.decode("utf-8", errors="replace").splitlines()

    def _unit_text(self, unit: Unit) -> str:
        """Describe the unit for the judge: its name, its file and the start of its definition.

        Args:
            unit: The unit.

        Returns:
            The text; without the definition if the file or the line is not readable.
        """
        text = f"# The unit\n\n`{unit.qualname}`, defined in `{unit.path}`"
        lines = self._lines(unit.path)
        if lines is None or not 1 <= unit.line <= len(lines):
            return text + "."
        start = "\n".join(lines[unit.line - 1 : unit.line - 1 + CONTEXT_LINES])
        return f"{text}, line {unit.line}. Its definition begins:\n\n{fence(start)}"

    def _excerpt(self, place: Place) -> str:
        """Show a place with `CONTEXT_LINES` lines before and after it.

        Args:
            place: The place.

        Returns:
            The fenced lines, each with its number, the place's line marked with `>>`; the
            place's own text if the file cannot be read.
        """
        lines = self._lines(place.path)
        if lines is None or not 1 <= place.line <= len(lines):
            return fence(f">> {place.line}: {place.text}")
        first = max(1, place.line - CONTEXT_LINES)
        last = min(len(lines), place.line + CONTEXT_LINES)
        return fence(_numbered(lines, first, last, {place.line}))

    def _whole(self, path: str, places: list[Place]) -> str | None:
        """Show a whole file with the lines of the given places marked.

        Args:
            path: The file, relative to the project directory.
            places: The places in it.

        Returns:
            The fenced file; None if it cannot be read or is over `WHOLE_FILE_BYTES`.
        """
        lines = self._lines(path)
        if lines is None:
            return None
        return fence(_numbered(lines, 1, len(lines), {place.line for place in places}))


def _numbered(lines: list[str], first: int, last: int, marked: set[int]) -> str:
    """Render lines with their numbers, the marked ones with `>>`.

    Args:
        lines: The file's lines.
        first: The first line to show (1-based).
        last: The last line to show.
        marked: The line numbers to mark.

    Returns:
        One text line per shown line: ">> 12: code" or "   12: code".
    """
    return "\n".join(
        f"{'>>' if number in marked else '  '} {number}: {lines[number - 1]}"
        for number in range(first, last + 1)
    )
