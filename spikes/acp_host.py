"""A real ACP host for `calu acp`: the official client library drives the adapter (D24).

The adapter (`calu acp ADDRESS --token=...`) is started as a child, as an editor starts it,
and spoken to with the client side of the `agent-client-protocol` package, which checks every
message of the agent against the protocol's schema. That is what calu's own tests cannot do:
they speak the protocol as calu understands it.

The host runs a conversation that touches each part an editor uses:

    initialize, session/new
    a prompt with a command                 (streamed text, a tool call and its result)
    a prompt whose command escalates        (the permission request, answered with allow)
    a prompt that is cancelled mid-turn     (session/cancel)
    session/set_mode, session/list, a prompt with an image, session/load

and prints, per step, what arrived and anything that failed. It needs a running calu session
with a real Claude Code (e.g. `spikes/smoke_session.py DIR --stay`), and runs in an
environment that has the package, not in calu's:

    uv run --no-project --with agent-client-protocol python spikes/acp_host.py \\
        CALU_PYTHON ADDRESS TOKEN WORKSPACE

Results are recorded in `spikes/RESULTS.md`.
"""

import asyncio
import base64
import struct
import sys
import traceback
import zlib
from collections import Counter
from typing import Any

from acp import Client, spawn_agent_process
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    FileSystemCapabilities,
    ImageContentBlock,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)


class Host(Client):
    """The editor's side: records what the agent sends and allows what it asks for."""

    def __init__(self) -> None:
        """Start with nothing received."""
        self.kinds: Counter[str] = Counter()
        self.text: list[str] = []
        self.permissions: list[str] = []
        self.tools: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Record one update of the session.

        Args:
            session_id: The session the update belongs to.
            update: The update, already checked against the schema by the library.
            **kwargs: Fields the library passes on.
        """
        kind = type(update).__name__
        self.kinds[kind] += 1
        content = getattr(update, "content", None)
        if kind == "AgentMessageChunk" and getattr(content, "text", None):
            self.text.append(content.text)
        if kind == "ToolCallStart":
            self.tools.append(f"{update.title} [{update.kind}/{update.status}]")

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        """Allow what the agent asks for, and record the question.

        Args:
            session_id: The session.
            tool_call: The tool call the permission is for.
            options: The options the agent offers.
            **kwargs: Fields the library passes on.

        Returns:
            The first option that allows, or the first option.
        """
        offered = [f"{option.option_id}:{option.kind}" for option in options]
        self.permissions.append(f"{tool_call.title} options={offered}")
        chosen = next((o for o in options if str(o.kind).startswith("allow")), options[0])
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=chosen.option_id, outcome="selected")
        )

    def take(self) -> str:
        """Describe what arrived since the last call, and forget it.

        Returns:
            One line: the kinds of updates with their counts, the agent's text, the tool
            calls and the permission questions.
        """
        said = "".join(self.text).strip().replace("\n", " ")
        line = (
            f"updates={dict(self.kinds)} text={said[:160]!r} tools={self.tools} "
            f"permissions={self.permissions}"
        )
        self.kinds.clear()
        self.text.clear()
        self.tools.clear()
        self.permissions.clear()
        return line


async def step(name: str, work: Any, host: Host, timeout_s: float = 150.0) -> Any:
    """Run one step of the conversation and print its outcome.

    Args:
        name: What the step is called in the output.
        work: The awaitable to run.
        host: The host, for what arrived meanwhile.
        timeout_s: How long the step may take.

    Returns:
        What the step returned, or None if it failed or timed out.
    """
    try:
        result = await asyncio.wait_for(work, timeout_s)
    except TimeoutError:
        print(f"-- {name}: TIMEOUT after {timeout_s:.0f} s | {host.take()}", flush=True)
        return None
    except Exception as exc:
        last = traceback.format_exception_only(exc)[-1].strip()
        print(f"-- {name}: FAILED {last[:400]} | {host.take()}", flush=True)
        return None
    shown = result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result
    print(f"-- {name}: ok {str(shown)[:500]} | {host.take()}", flush=True)
    return result


def red_square(size: int = 32) -> str:
    """Build a PNG of one colour.

    Args:
        size: The edge length in pixels.

    Returns:
        The PNG file, a red square, as base64.
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
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


def text(words: str) -> list[Any]:
    """Build a prompt of one text block.

    Args:
        words: The text.

    Returns:
        The prompt.
    """
    return [TextContentBlock(type="text", text=words)]


async def main(python: str, address: str, token: str, workspace: str, only: str = "") -> None:
    """Start the adapter and run the conversation.

    Args:
        python: The interpreter of calu's environment.
        address: The address of the running session.
        token: Its client token.
        workspace: The session's project directory.
        only: "cancel" runs the cancelled turn alone; "" runs everything.
    """
    host = Host()
    command = [python, "-m", "calu", "acp", address, f"--token={token}"]
    async with spawn_agent_process(host, *command, cwd=workspace) as (agent, process):
        capabilities = ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
            terminal=False,
        )
        started = await step(
            "initialize",
            agent.initialize(
                protocol_version=1,
                client_capabilities=capabilities,
                client_info=Implementation(name="acp_host", version="0"),
            ),
            host,
        )
        new = await step("session/new", agent.new_session(cwd=workspace, mcp_servers=[]), host)
        if started is None or new is None:
            return
        session = new.session_id
        if only != "cancel":
            await conversation(agent, host, session)
        await step("prompt, then session/cancel", cancelled(agent, session), host)
        if only == "cancel":
            process.terminate()
            return
        await rest(agent, host, session, new, workspace)
        process.terminate()


async def conversation(agent: Any, host: Host, session: str) -> None:
    """Run the two plain prompts: a command, and a command that escalates.

    Args:
        agent: The connection to the adapter.
        host: The host.
        session: The session id.
    """
    if True:
        await step(
            "prompt: a command",
            agent.prompt(
                session_id=session,
                prompt=text("Run the command: echo hello-acp > acp.txt  Then answer DONE."),
            ),
            host,
        )
        await step(
            "prompt: a command that escalates",
            agent.prompt(
                session_id=session,
                prompt=text("Run exactly this command: echo deploy # calu:escalate"),
            ),
            host,
        )


async def cancelled(agent: Any, session: str) -> Any:
    """Start a slow turn, cancel it after a moment, and return the prompt's answer.

    Args:
        agent: The connection to the adapter.
        session: The session id.

    Returns:
        The answer of the cancelled prompt.
    """
    running = asyncio.ensure_future(
        agent.prompt(
            session_id=session,
            prompt=text(
                "Run this command in the foreground, exactly as written: "
                "python3 -c 'print(sum(i * i for i in range(10**9)))'  "
                "Then tell me the number."
            ),
        )
    )
    await asyncio.sleep(12)
    await agent.cancel(session_id=session)
    return await running


async def rest(agent: Any, host: Host, session: str, new: Any, workspace: str) -> None:
    """Run what follows the cancelled turn: modes, the list, an image, a reload.

    Args:
        agent: The connection to the adapter.
        host: The host.
        session: The session id.
        new: The answer to `session/new`, for the modes.
        workspace: The session's project directory.
    """
    if True:
        modes = getattr(new, "modes", None)
        if modes is not None and modes.available_modes:
            other = next(
                (m.id for m in modes.available_modes if m.id != modes.current_mode_id),
                modes.current_mode_id,
            )
            await step(
                f"session/set_mode {other}",
                agent.set_session_mode(session_id=session, mode_id=other),
                host,
            )
        await step("session/list", agent.list_sessions(cwd=workspace), host)
        await step(
            "prompt with an image",
            agent.prompt(
                session_id=session,
                prompt=[
                    TextContentBlock(
                        type="text", text="Which colour fills this image? Answer with one word."
                    ),
                    ImageContentBlock(type="image", mime_type="image/png", data=red_square()),
                ],
            ),
            host,
        )
        await step("session/load", agent.load_session(cwd=workspace, session_id=session), host)
        await step(
            "prompt after load",
            agent.prompt(session_id=session, prompt=text("Answer with the single word BACK.")),
            host,
        )


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:6]))
