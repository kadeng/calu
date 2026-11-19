"""Spike: can a host read `/usage` from a headless Claude Code without a turn?

`/usage` sent as chat text runs Claude Code's local command and ends as a turn of its own (a
`result` message with $0 cost), which calu records like any turn. The CLI also answers two
control requests of its stream-json protocol: `get_session_cost` (`{text}`: "the same text
/usage prints in non-interactive mode") and `get_usage` (structured session totals and, when
available, the claude.ai plan's rate-limit utilisation; marked experimental). Neither sends
anything to the model.

Run: `uv run python spikes/spike_usage_control.py` (needs a logged-in `claude`; it sends no
prompt, so it costs nothing). It connects, sends both requests before and without any turn,
and prints the answers and every message Claude Code sent meanwhile. Results are recorded in
`spikes/RESULTS.md`.
"""

import json
import tempfile
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def main() -> None:
    """Run the measurement and print what happened."""
    with tempfile.TemporaryDirectory(prefix="calu-spike-") as scratch:
        options = ClaudeAgentOptions(model="haiku", cwd=scratch, max_turns=1)
        async with ClaudeSDKClient(options) as client:
            query: Any = client._query
            for request in (
                {"subtype": "get_session_cost"},
                {"subtype": "get_usage", "skip_behaviors": True},
            ):
                try:
                    answer = await query._send_control_request(request, timeout=60.0)
                    print(
                        request["subtype"], "->", json.dumps(answer, indent=1, default=str)[:3000]
                    )
                except Exception as exc:  # the SDK raises bare Exception
                    print(request["subtype"], "failed:", exc)
            with anyio.move_on_after(3):
                async for message in client.receive_messages():
                    print("message meanwhile:", type(message).__name__, str(message)[:200])


if __name__ == "__main__":
    anyio.run(main)
