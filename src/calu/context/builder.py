"""The context that checkers see, and the folding of old turns into the chat summary.

    cached prefix = project files (AGENTS.md, CLAUDE.md, ...) + skill list + chat summary
    recent        = unfolded turns + the running turn

When a turn ends and at least `fold_every` turns are unfolded, the oldest `fold_count` turns are
folded: a summarizer agent merges them into the narrative part of the chat summary, and their
user inputs are appended verbatim to its "User inputs" section. Folding runs in the background;
checks continue with the old context until it is done. The summary is kept in memory
(`ContextBuilder.summary`) and handed to `on_summary` after each fold, so the session can store
it in the database (table `chat_summaries`).

Chat summary layout (an operator may edit both parts):

    # Chat summary
    <narrative>
    ## User inputs (verbatim)
    <one block per folded user input>
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
from pydantic_ai import Agent
from pydantic_ai.models import Model

from calu.checkers.checker import CheckContext
from calu.config import ContextConfig
from calu.context.turns import StepRecord, TurnRecord

log = logging.getLogger(__name__)

INPUTS_HEADING = "## User inputs (verbatim)"

SUMMARIZER_INSTRUCTIONS = """\
You maintain the running summary of a coding session between a user and the coding agent Claude
Code. You get the current summary and the records of the turns to add. Return the new summary as
Markdown without a top-level heading. Keep it concise (at most about 600 words), in plain technical
language. Keep: the goals and decisions of the user, the current state of the work, files that were
created or changed and why, open problems, and rules the user stated. Drop details that no longer
matter. Do not invent facts."""


def split_summary(text: str) -> tuple[str, str]:
    """Split the chat summary into (narrative, verbatim inputs section body).

    The "# Chat summary" heading is removed.

    Args:
        text: The chat summary text.

    Returns:
        Narrative and body of the inputs section, both stripped; the body is "" if the file has
        no inputs section.
    """
    body = re.sub(r"^# Chat summary\s*\n", "", text, count=1)
    if INPUTS_HEADING in body:
        narrative, inputs = body.split(INPUTS_HEADING, 1)
        return narrative.strip(), inputs.strip()
    return body.strip(), ""


def join_summary(narrative: str, inputs: str) -> str:
    """Build the chat summary from its two parts (inverse of `split_summary`).

    Args:
        narrative: The narrative summary.
        inputs: Body of the verbatim inputs section; the section is omitted if it is blank.

    Returns:
        The file content, ending with a newline.
    """
    text = f"# Chat summary\n\n{narrative.strip()}\n"
    if inputs.strip():
        text += f"\n{INPUTS_HEADING}\n\n{inputs.strip()}\n"
    return text


class ContextBuilder:
    """Collects turn records and builds `CheckContext`s. See the module docstring.

    `summarizer_model` is a Pydantic AI model string or `Model`; it is only used for folding.
    `on_turn_end` (optional) is called with each completed turn, e.g. to persist it.
    `on_summary` (optional) is awaited with the new chat summary after each fold.
    """

    def __init__(
        self,
        workspace: Path,
        config: ContextConfig,
        summarizer_model: str | Model | None = None,
        on_turn_end: Callable[[TurnRecord], None] | None = None,
        summary: str = "",
        on_summary: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Create a builder with no turns; turn indexes start at 1.

        Args:
            workspace: The workspace root (project files and skills are read from it).
            config: Context settings.
            summarizer_model: Model for folding; None uses `config.summarizer_model`.
            on_turn_end: Called with each completed turn, or None.
            summary: The chat summary so far (e.g. loaded from the database); "" if none.
            on_summary: Awaited with the new summary after each fold, or None.
        """
        self.workspace = workspace
        self.summary = summary
        """The chat summary; an operator edit may replace it at any time."""
        self.on_summary = on_summary
        self.config = config
        self.summarizer_model = summarizer_model or config.summarizer_model
        self.on_turn_end = on_turn_end
        self.turns: list[TurnRecord] = []  # completed, unfolded
        self.current: TurnRecord | None = None
        self._next_index = 1
        self._fold_lock = anyio.Lock()
        self._summarizer: Agent[None, str] | None = None

    # ------------------------------------------------------------------ recording

    def user_input(self, text: str) -> None:
        """Record a user input; starts a turn if none is running.

        Args:
            text: The user input, kept verbatim.
        """
        self._ensure_turn().user_inputs.append(text)

    def step(self, record: StepRecord) -> None:
        """Record a tool call of the running turn (a turn is started if needed).

        Args:
            record: The tool call record.
        """
        self._ensure_turn().steps.append(record)

    def end_turn(self, answer: str) -> TurnRecord | None:
        """Close the running turn with Claude's `answer` and add it to the unfolded turns.

        Calls `on_turn_end` with the closed turn; exceptions from it propagate.

        Args:
            answer: Claude's final answer of the turn.

        Returns:
            The closed turn, or None if no turn was running.
        """
        turn, self.current = self.current, None
        if turn is None:
            return None
        turn.answer = answer
        turn.ended_at = datetime.now(UTC)
        self.turns.append(turn)
        if self.on_turn_end is not None:
            self.on_turn_end(turn)
        return turn

    def restore(self, turns: list[TurnRecord]) -> None:
        """Replace the unfolded turns with the latest of `turns` (resuming a stored session).

        At most `fold_every - 1` turns are kept, so the next completed turn may trigger a fold;
        older ones are assumed to be covered by the chat summary. The next turn index continues
        after the highest restored index.

        Args:
            turns: Completed turns of the session, oldest first.
        """
        self.turns = list(turns[-(self.config.fold_every - 1) :])
        self.current = None
        self._next_index = max((t.index for t in turns), default=0) + 1

    def needs_fold(self) -> bool:
        """Tell whether enough turns are unfolded to fold.

        Returns:
            True if the number of unfolded turns reached `fold_every`.
        """
        return len(self.turns) >= self.config.fold_every

    def _ensure_turn(self) -> TurnRecord:
        """Return the running turn; start a new one with the next index if none is running.

        Returns:
            The running turn.
        """
        if self.current is None:
            self.current = TurnRecord(index=self._next_index)
            self._next_index += 1
        return self.current

    # ------------------------------------------------------------------ context

    def prefix(self) -> str:
        """Return the cached part of the context (files that change rarely).

        Reads the configured project files that exist and the skill list; adds the summary.
        Unreadable files count as empty.

        Returns:
            The parts joined with blank lines; "" if there are none.
        """
        parts: list[str] = []
        for name in self.config.files:
            path = self.workspace / name
            if path.is_file():
                parts.append(f"## File {name}\n\n{_read(path)}")
        skills = self._skills()
        if skills:
            parts.append("## Skills available to Claude\n\n" + skills)
        if self.summary:
            parts.append(self.summary)
        return "\n\n".join(parts)

    def recent_parts(self) -> tuple[str, list[str]]:
        """Return the completed unfolded turns and the pieces of the running turn.

        The completed turns are limited to `ContextConfig.max_recent_chars` minus the size of
        the running turn: the oldest turns are dropped first (they are still kept for folding),
        and a note says how many. A running turn longer than the limit drops its oldest steps
        behind a note. The running turn's pieces come from `TurnRecord.render_parts`, so a new
        step only appends a piece (prompt caching per checked step, see `Checker`).

        Returns:
            `(turns, current)`: the completed turns rendered as one text ("" if none) and the
            running turn's pieces ([] if no turn runs).
        """
        limit = self.config.max_recent_chars
        current = self.current.render_parts() if self.current else []
        if sum(map(len, current)) > limit and len(current) > 1:
            head, steps = current[0], current[1:]
            kept: list[str] = []
            for piece in reversed(steps):
                if sum(map(len, kept)) + len(piece) > limit - len(head):
                    break
                kept.insert(0, piece)
            omitted = len(steps) - len(kept)
            current = [head + f"[{omitted} earlier steps of this turn omitted]", *kept]
        budget = limit - sum(map(len, current))
        rendered = [t.render() for t in self.turns]
        dropped = 0
        while rendered and sum(len(r) + 2 for r in rendered) > budget:
            rendered.pop(0)
            dropped += 1
        turns = "\n\n".join(rendered)
        if dropped:
            turns = f"[{dropped} older turns omitted]" + (f"\n\n{turns}" if turns else "")
        return turns, current

    def recent(self) -> str:
        """Return the unfolded turns and the running turn, rendered.

        The result is limited to `ContextConfig.max_recent_chars` characters: the oldest turns
        are dropped first (they are still kept for folding), and a note says how many.

        Returns:
            The rendered turns, oldest first (see `recent_parts` for the limits).
        """
        turns, current = self.recent_parts()
        return "\n\n".join(p for p in (turns, "".join(current)) if p)

    def check_context(self) -> CheckContext:
        """Build the context for one check from `prefix()` and `recent()`.

        Returns:
            The check context.
        """
        turns, current = self.recent_parts()
        return CheckContext(prefix=self.prefix(), turns=turns, current=tuple(current))

    def _skills(self) -> str:
        """List the skills in the configured skill directories (`<dir>/*/SKILL.md`).

        Returns:
            One line "- <skill dir name>: <frontmatter description>" per skill; "" if none.
        """
        lines = []
        for d in self.config.skill_dirs:
            for skill_md in sorted((self.workspace / d).glob("*/SKILL.md")):
                desc = _frontmatter_value(_read(skill_md), "description")
                lines.append(f"- {skill_md.parent.name}: {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ folding

    @property
    def summarizer(self) -> Agent[None, str]:
        """The summarizer agent, created on first use.

        Returns:
            An agent with `SUMMARIZER_INSTRUCTIONS`; the model is passed per run.
        """
        if self._summarizer is None:
            self._summarizer = Agent(
                output_type=str, instructions=SUMMARIZER_INSTRUCTIONS, name="summarizer"
            )
        return self._summarizer

    async def fold(self) -> bool:
        """Fold the oldest `fold_count` turns into the chat summary if `needs_fold()`.

        The summary is read before and compared again after the (slow) summarizer call; if it
        was replaced meanwhile, nothing changes and the turns stay unfolded for the next try.
        On success, `summary` is replaced and `on_summary` is awaited with it.
        Concurrent calls are serialized. Does not raise (except cancellation): errors are
        logged.

        Returns:
            True if the turns were folded; False if no fold was needed or it failed.
        """
        async with self._fold_lock:
            if not self.needs_fold():
                return False
            batch = self.turns[: self.config.fold_count]
            current = self.summary
            narrative, inputs = split_summary(current)
            prompt = (
                f"# Current summary\n\n{narrative or '(empty)'}\n\n# Turns to add\n\n"
                + "\n\n".join(t.render() for t in batch)
            )
            try:
                result = await self.summarizer.run(prompt, model=self.summarizer_model)
                if self.summary != current:
                    log.info("chat summary changed during folding; folding later")
                    return False
                new_inputs = "\n\n".join(
                    f"Turn {t.index}:\n{_indent(text)}" for t in batch for text in t.user_inputs
                )
                all_inputs = "\n\n".join(p for p in (inputs, new_inputs) if p)
                self.summary = join_summary(result.output, all_inputs)
                if self.on_summary is not None:
                    await self.on_summary(self.summary)
            except Exception:  # folding is best effort
                log.exception("folding turns into the chat summary failed")
                return False
            del self.turns[: len(batch)]
            return True


def _read(path: Path) -> str:
    """Read a UTF-8 text file; return "" if it cannot be read or decoded.

    Args:
        path: The file.

    Returns:
        The content, or "".
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return ""


def _indent(text: str) -> str:
    """Indent every line of `text` by four spaces.

    Args:
        text: The text.

    Returns:
        The indented lines joined with newlines (without a trailing newline).
    """
    return "\n".join("    " + line for line in text.splitlines())


def _frontmatter_value(text: str, key: str) -> str:
    """Return the value of `key` in a Markdown file's YAML frontmatter, or "".

    Only simple one-line `key: value` entries are recognized; surrounding quotes are removed.

    Args:
        text: The Markdown file content.
        key: The frontmatter key, e.g. "description".

    Returns:
        The value, or "" if there is no frontmatter or no such key.
    """
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return ""
    for line in match.group(1).splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""
