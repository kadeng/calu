"""Spike: can calu choose Claude Code's session id, also for a fork of a whole conversation?

`calu run` should print the session id when it starts, before the first prompt. Claude Code
reports its id only with the first message of a turn, so calu would have to choose the id
itself: `ClaudeAgentOptions.session_id` (the CLI's `--session-id`). And `calu run --fork ID`
branches a whole conversation: `resume` with `fork_session=True` and no fork point.

Run: `uv run python spikes/spike_session_id.py` (needs a logged-in `claude`; uses Haiku).
It prints the id Claude Code reports for (1) a new session with a chosen id, (2) a fork of it
with a chosen id, (3) a fork of it without one, and whether each fork remembers the original
conversation. Results are recorded in `spikes/RESULTS.md`.
"""

import tempfile
import uuid

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage


async def turn(options: ClaudeAgentOptions, prompt: str) -> tuple[str, str]:
    """Run one turn and return what Claude Code reported.

    Args:
        options: The options of the session.
        prompt: The user message.

    Returns:
        The session id and the answer text of the result message.
    """
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                return message.session_id, str(message.result)
    return "", ""


async def main() -> None:
    """Run the three cases and print what happened."""
    with tempfile.TemporaryDirectory(prefix="calu-spike-") as scratch:
        chosen = str(uuid.uuid4())
        new = ClaudeAgentOptions(model="haiku", cwd=scratch, max_turns=2, session_id=chosen)
        reported, answer = await turn(new, "The secret word is PELICAN. Answer with: noted.")
        print(f"new session:  chosen {chosen}\n              reported {reported} ({answer!r})")

        fork_id = str(uuid.uuid4())
        for label, extra in (("fork, chosen id", {"session_id": fork_id}), ("fork, no id", {})):
            options = ClaudeAgentOptions(
                model="haiku",
                cwd=scratch,
                max_turns=2,
                resume=reported,
                fork_session=True,
                **extra,
            )
            try:
                forked, answer = await turn(options, "What is the secret word? One word.")
            except Exception as error:
                print(f"{label}: failed: {error!r}")
                continue
            print(f"{label}: chosen {extra.get('session_id')}\n  reported {forked} ({answer!r})")


if __name__ == "__main__":
    anyio.run(main)
