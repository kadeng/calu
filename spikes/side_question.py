"""Spike: does a headless Claude Code answer a side question while a turn runs?

Claude Code's `/btw` asks a question beside the running work: the answer is made from the
conversation so far, with no tools, and neither the question nor the answer enters the
conversation. The CLI offers the same to an SDK host as the control request `side_question`
(`{question, history?}` -> `{response, synthetic}`); the Python Agent SDK has no method for it
yet, so this spike sends the control request itself.

Run: `uv run python spikes/side_question.py` (needs a logged-in `claude`; uses Haiku).
It starts a turn whose Bash command takes 25 seconds, asks a side question while the command runs,
asks for the context usage as well, and prints when each answer arrived and whether the next
turn knows about the side question. Results are recorded in `spikes/RESULTS.md`.
"""

import tempfile
import time
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def main() -> None:
    """Run the measurement and print what happened."""
    started = time.monotonic()

    def stamp(text: str) -> None:
        """Print a line with the seconds since the start.

        Args:
            text: The line.
        """
        print(f"[{time.monotonic() - started:6.1f}s] {text}", flush=True)

    with tempfile.TemporaryDirectory(prefix="calu-spike-") as scratch:
        options = ClaudeAgentOptions(
            model="haiku", cwd=scratch, allowed_tools=["Bash(python3 -c:*)"], max_turns=6
        )
        async with ClaudeSDKClient(options) as client:
            await client.query(
                "The secret word is PELICAN. Run this Bash command in the foreground, exactly "
                "as written: python3 -c 'import time; time.sleep(25)'  Then answer with the "
                "single word finished."
            )

            async def ask() -> None:
                """Ask the side question and the context usage while the turn runs."""
                await anyio.sleep(12)
                query: Any = client._query
                stamp("side question sent")
                answer = await query._send_control_request(
                    {"subtype": "side_question", "question": "What is the secret word?"},
                    timeout=120.0,
                )
                stamp(f"side answer: {answer!r}")
                usage = await client.get_context_usage()
                stamp(
                    f"context: {usage.get('totalTokens')} of {usage.get('maxTokens')} tokens,"
                    f" {usage.get('percentage')} %, model {usage.get('model')}"
                )

            async with anyio.create_task_group() as tg:
                tg.start_soon(ask)
                async for message in client.receive_response():
                    stamp(f"{type(message).__name__}: {str(message)[:300]}")
            await client.query("Did I ask you a question beside the task? Answer yes or no.")
            async for message in client.receive_response():
                stamp(f"{type(message).__name__}: {str(message)[:300]}")


if __name__ == "__main__":
    anyio.run(main)
