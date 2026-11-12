"""Spike: does `--dangerously-load-development-channels` work in the headless mode calu uses?

A channel is an MCP server that pushes messages into a running Claude Code session; agents on
one machine can talk to each other through one. A channel server that is not on Anthropic's
allow list is only loaded with `--dangerously-load-development-channels server:<name>`, and an
interactive `claude` asks for a confirmation at the start (local development only). The
documentation read on 2026-10-08 said that the flag is ignored with `-p` and through the Agent
SDK. This spike measures it.

Run: `uv run python spikes/spike_dev_channel.py [--no-flag]` (needs a logged-in `claude`;
Haiku). It starts a session through the Agent SDK with a minimal channel server named
`agent-chat` (this file, run with `--serve`) and the flag, lets a turn wait 20 seconds, and
prints every message of the stream that is not an ordinary assistant or tool message, what
Claude Code wrote to stderr, and whether the word the server pushed 6 seconds after its start
reached Claude. With `--no-flag` the same without the flag, as the control.
Results are recorded in `spikes/RESULTS.md`.
"""

import json
import sys
import tempfile
import threading
import time
from typing import Any

SERVER = "agent-chat"
WORD = "MARMOT"


def serve() -> None:
    """Be the channel server: answer the MCP handshake on stdin/stdout, then push one message.

    Speaks newline-delimited JSON-RPC by hand, so that the spike needs no MCP library: it
    answers `initialize` with the capability `experimental["claude/channel"]`, `tools/list`
    with no tools, `ping` with an empty result, and 6 seconds after `notifications/initialized`
    writes one `notifications/claude/channel` whose body names `WORD`.
    """

    def write(message: dict[str, Any]) -> None:
        """Write one JSON-RPC message.

        Args:
            message: The message.
        """
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def push() -> None:
        """Push the channel message after a pause."""
        time.sleep(6)
        write(
            {
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {"content": f"The channel word is {WORD}.", "meta": {"from": "spike"}},
            }
        )

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method, ident = request.get("method"), request.get("id")
        if method == "initialize":
            version = request.get("params", {}).get("protocolVersion", "2025-06-18")
            write(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
                        "serverInfo": {"name": SERVER, "version": "0.0.1"},
                        "instructions": "Messages of other agents arrive as <channel> tags.",
                    },
                }
            )
        elif method == "notifications/initialized":
            threading.Thread(target=push, daemon=True).start()
        elif method == "tools/list":
            write({"jsonrpc": "2.0", "id": ident, "result": {"tools": []}})
        elif ident is not None:
            write({"jsonrpc": "2.0", "id": ident, "result": {}})


async def main(with_flag: bool) -> None:
    """Run one session with the channel server and print what happened.

    Args:
        with_flag: Whether to pass `--dangerously-load-development-channels`.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        UserMessage,
    )

    started = time.monotonic()
    stderr: list[str] = []

    def stamp(text: str) -> None:
        """Print a line with the seconds since the start.

        Args:
            text: The line.
        """
        print(f"[{time.monotonic() - started:6.1f}s] {text}", flush=True)

    with tempfile.TemporaryDirectory(prefix="calu-spike-") as scratch:
        extra: dict[str, str | None] = {"debug-to-stderr": None}
        if with_flag:
            extra["dangerously-load-development-channels"] = f"server:{SERVER}"
        options = ClaudeAgentOptions(
            model="haiku",
            cwd=scratch,
            max_turns=4,
            allowed_tools=["Bash(sleep:*)"],
            mcp_servers={
                SERVER: {"type": "stdio", "command": sys.executable, "args": [__file__, "--serve"]}
            },
            extra_args=extra,
            stderr=stderr.append,
        )
        answer = ""
        async with ClaudeSDKClient(options) as client:
            stamp("connected")
            await client.query(
                "Run the Bash command `sleep 20` in the foreground. Afterwards answer: did a "
                "message from a channel arrive, and if so, which word did it name? If none "
                "arrived, answer exactly: no channel message."
            )
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    answer = str(message.result)
                elif not isinstance(message, (AssistantMessage, UserMessage)):
                    stamp(f"{type(message).__name__}: {str(message)[:400]}")
                elif isinstance(message, UserMessage) and "channel" in str(message.content):
                    stamp(f"UserMessage: {str(message.content)[:400]}")
            # A channel message may start a turn of its own after the first one ended.
            try:
                import anyio

                with anyio.move_on_after(15):
                    async for message in client.receive_response():
                        stamp(f"later {type(message).__name__}: {str(message)[:400]}")
            except Exception as error:
                stamp(f"waiting for a later turn failed: {error!r}")
    print(f"\nflag passed: {with_flag}")
    print(f"answer: {answer!r}")
    print(f"the word reached Claude: {WORD in answer}")
    print("stderr lines that mention channels:")
    for line in stderr:
        if any(word in line.lower() for word in ("channel", SERVER, "development")):
            print("   ", line[:300])


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        import anyio

        anyio.run(main, "--no-flag" not in sys.argv)
