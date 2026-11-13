"""Spike: does Claude Code take an image in a user message of the stream, and how is the
message replayed (D220)?

calu sends a chat message with attached images as a user message whose content is a text
block followed by image blocks (`SdkRunner._write`). This sends one such message, with a red
square, to the real Claude Code and prints Claude's answer and the shape of the replayed user
message, which is what calu matches to report the user's input.

Run: `uv run python spikes/spike_chat_image.py` (needs a logged-in `claude` or an API key;
uses Haiku). Results are recorded in `spikes/RESULTS.md`.
"""

import base64
import struct
import tempfile
import zlib
from collections.abc import AsyncIterator
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, UserMessage


def red_square(size: int = 32) -> bytes:
    """Build a PNG of one colour.

    Args:
        size: The edge length in pixels.

    Returns:
        The PNG file's bytes: a red square.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Frame one PNG chunk.

        Args:
            kind: The chunk type.
            data: Its content.

        Returns:
            Length, type, content and checksum.
        """
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def main() -> None:
    """Send the message and print what comes back."""
    data = base64.b64encode(red_square()).decode()
    message: dict[str, Any] = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "Which colour fills this image? Answer with one word."},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": data},
                },
            ],
        },
        "parent_tool_use_id": None,
        "priority": "later",
    }

    async def one() -> AsyncIterator[dict[str, Any]]:
        """Yield the single message.

        Yields:
            The user message.
        """
        yield message

    with tempfile.TemporaryDirectory(prefix="calu-spike-") as scratch:
        options = ClaudeAgentOptions(
            model="haiku", cwd=scratch, max_turns=1, extra_args={"replay-user-messages": None}
        )
        async with ClaudeSDKClient(options) as client:
            await client.query(one())
            async for received in client.receive_response():
                if isinstance(received, UserMessage):
                    content = received.content
                    shape = (
                        "a string"
                        if isinstance(content, str)
                        else [type(block).__name__ for block in content]
                    )
                    print("replayed user message:", shape)
                if isinstance(received, ResultMessage):
                    print("answer:", received.result)


if __name__ == "__main__":
    anyio.run(main)
