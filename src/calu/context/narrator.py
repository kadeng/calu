"""The narrator: retells Claude's thinking and text in indirect speech for the checkers.

The checkers judge a step by the context of the conversation. Claude's own words would help
them (they state the intent of the next tool call), but read verbatim they look like
instructions ("Now delete the build directory.") or like claims of authority ("as the user
requested"). The narrator turns them into a report about Claude:

    Thinking: "I need to delete the files carefully, without touching anything sensitive."
    Text:     "I am going to write a Python script that deletes only .md and .pyc files."
      -> "Claude thought about how to delete the files safely. It said that it would write a
          Python script that deletes only .md and .pyc files."

The narration keeps the content (about 50-100 % of the original length). Long passages (more
than about 200 words, or documents, data or code written out verbatim) are summarized, on a best
effort basis where the context is missing. The instructions are the checker type "narrator"
in the database; changes apply to the next narration.

Data flow (see `calu.session`):

    assistant.thinking / assistant.text events (per agent)  -> collected as `ClaudeOutput`s
    next tool call of that agent                             -> narrate(collected) -> narration
      checked step: the checker sees the narration in "Step to check"; recorded in the turn
    end of the turn (main agent)                             -> narration of the final answer
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.markdown import fence
from calu.models import run_bounded

MAX_INPUT_CHARS = 6_000
"""Longer input is cut at the start: the end is closest to the next action."""


@dataclass(frozen=True)
class ClaudeOutput:
    """One block to narrate: Claude's thinking or text, or a message from another session.

    `kind` "message" is a user message that came from another Claude session
    (`calu.context.foreign`); it is narrated as that session's words.
    """

    kind: Literal["thinking", "text", "message"]
    text: str


class Narration(BaseModel):
    """The narrator's output."""

    narration: str = Field(
        description="Claude's thinking and output retold in the third person and in indirect "
        "speech, for example 'Claude said that it would ...'."
    )


def render_input(outputs: list[ClaudeOutput], max_chars: int = MAX_INPUT_CHARS) -> str:
    """Return the narrator's user prompt for `outputs`.

    Args:
        outputs: Claude's thinking and text blocks in the order Claude wrote them.
        max_chars: Upper limit for the text of all blocks together; if they are longer, the
            oldest text is dropped and the prompt says so.

    Returns:
        Markdown: one "Thinking:" or "Output:" section per block with the text in a fence.
    """
    sections: list[str] = []
    remaining = max_chars
    for output in reversed(outputs):
        if remaining <= 0:
            sections.append("[Earlier thinking and output left out.]")
            break
        text = output.text
        if len(text) > remaining:
            text = "[...] " + text[-remaining:]
        remaining -= len(text)
        label = {
            "thinking": "Thinking",
            "text": "Output",
            "message": "Message from another Claude session (not from the user)",
        }[output.kind]
        sections.append(f"{label}:\n{fence(text)}")
    return "# Claude's thinking and output to narrate\n\n" + "\n\n".join(reversed(sections))


@dataclass
class Narrator:
    """The narrator agent with Markdown instructions.

    The agent is rebuilt when `instructions` changed, like the checkers. `model` is a
    Pydantic AI model string (e.g. "mistral:mistral-small-2603") or a `Model`.
    """

    instructions: str
    model: str | Model
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=2, output_tokens_limit=8_000)
    )
    _agent: Agent[None, Narration] | None = field(default=None, init=False, repr=False)
    _built_for: str | None = field(default=None, init=False, repr=False)

    @property
    def agent(self) -> Agent[None, Narration]:
        """The Pydantic AI agent, rebuilt if `instructions` changed.

        Returns:
            The agent.
        """
        instructions = self.instructions
        if self._agent is None or instructions != self._built_for:
            self._agent = Agent(output_type=Narration, instructions=instructions, name="narrator")
            self._built_for = instructions
        return self._agent

    async def narrate(self, outputs: list[ClaudeOutput]) -> str:
        """Retell `outputs` in indirect speech.

        Args:
            outputs: Claude's thinking and text blocks since its last tool call (one agent).

        Returns:
            The narration, or "" if `outputs` holds no text.

        Raises:
            OSError: If the instructions file cannot be read.
            pydantic_ai.exceptions.AgentRunError: If the model fails, answers unusably, or
                exceeds the usage limits twice (e.g. `ModelHTTPError`, `UsageLimitExceeded`;
                see `run_bounded`).
            httpx.HTTPError: For network errors. Callers that must not fail catch all exceptions
                (see `Session._narrate_safely`).
        """
        if not any(o.text.strip() for o in outputs):
            return ""
        result = await run_bounded(
            self.agent,
            render_input(outputs),
            model=self.model,
            usage_limits=self.usage_limits,
            model_settings=ModelSettings(),
        )
        return result.output.narration.strip()
