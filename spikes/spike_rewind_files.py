"""What does `rewind_files` put back -- only what a tool edited, or what a command wrote too?

PLAN.md Q14 asks whether a fork (D36) should also rewind the working tree to its state at the
turn it forks from. A fork rewinds the *conversation*; the files keep whatever the dropped turn
wrote. The Agent SDK offers `enable_file_checkpointing=True` plus
`ClaudeSDKClient.rewind_files(<user message uuid>)`, but the promise only says "tracked files",
and calu's threat model cares exactly about the difference: Claude edits files with Edit/Write
*and* writes them with Bash commands, and a half-reverted tree is worse than one nobody
touched, because it looks consistent and is not.

    uv run python spikes/spike_rewind_files.py

Setup: a throwaway directory, `--tools Bash,Write`, `permission_mode` "bypassPermissions", file
checkpointing on and `replay-user-messages` so that every `UserMessage` carries its uuid. Two
turns, each writing the same two files -- one through the Write tool, one through a shell
redirect -- plus a third file that only the second turn creates. Then a rewind to the uuid of
the second turn's user message, which is the state after turn one.

    tool_file.txt   "v1" again  -> a tool edit is tracked
    bash_file.txt   "v1" again  -> a command's write is tracked too
    bash_new.txt    gone        -> a file a command created is removed again

Whatever does *not* happen is the argument for Q14: those files stay as the dropped turn left
them, and calu would have to say so rather than promise a clean rewind.
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
)

TOOL_FILE = "tool_file.txt"
BASH_FILE = "bash_file.txt"
BASH_NEW = "bash_new.txt"

FIRST = (
    f"Do exactly two things and then stop. "
    f"1. Use the Write tool to create {TOOL_FILE} with the single line: v1 "
    f"2. Use the Bash tool to run exactly: echo v1 > {BASH_FILE}"
)
SECOND = (
    f"Do exactly three things and then stop. "
    f"1. Use the Write tool to overwrite {TOOL_FILE} with the single line: v2 "
    f"2. Use the Bash tool to run exactly: echo v2 > {BASH_FILE} "
    f"3. Use the Bash tool to run exactly: echo new > {BASH_NEW}"
)


def state(workspace: Path) -> dict[str, str]:
    """Read the three files of the spike.

    Args:
        workspace: The throwaway directory.

    Returns:
        File name -> its stripped content, or "<missing>" when it does not exist.
    """
    found: dict[str, str] = {}
    for name in (TOOL_FILE, BASH_FILE, BASH_NEW):
        path = workspace / name
        found[name] = path.read_text(encoding="utf-8").strip() if path.is_file() else "<missing>"
    return found


async def turn(client: ClaudeSDKClient, prompt: str) -> tuple[str | None, list[str]]:
    """Send one prompt and drain the answer.

    Args:
        client: The connected SDK client.
        prompt: What to ask.

    Returns:
        The uuid of the user message this turn started with (None if none carried one), and the
        names of the tools Claude used.
    """
    await client.query(prompt)
    uuid: str | None = None
    tools: list[str] = []
    async for message in client.receive_response():
        if isinstance(message, UserMessage) and message.uuid and uuid is None:
            uuid = message.uuid
        if isinstance(message, AssistantMessage):
            for block in message.content:
                name = getattr(block, "name", None)
                if name:
                    tools.append(str(name))
        if isinstance(message, ResultMessage):
            break
    return uuid, tools


async def main() -> int:
    """Run the two turns, rewind, and print what came back.

    Returns:
        0 if the rewind put every file back, 1 if it left something behind (which is the
        answer Q14 needs either way).
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="haiku",
            tools=["Bash", "Write"],
            allowed_tools=["Bash", "Write"],
            permission_mode="bypassPermissions",
            setting_sources=[],
            enable_file_checkpointing=True,
            extra_args={"replay-user-messages": None},
        )
        async with ClaudeSDKClient(options=options) as client:
            first_uuid, first_tools = await turn(client, FIRST)
            after_first = state(workspace)
            print(f"turn 1 used {first_tools}, uuid {first_uuid}")
            print(f"  files: {after_first}")

            second_uuid, second_tools = await turn(client, SECOND)
            after_second = state(workspace)
            print(f"turn 2 used {second_tools}, uuid {second_uuid}")
            print(f"  files: {after_second}")

            if second_uuid is None:
                print("no uuid for the second turn; the rewind cannot be tried")
                return 1
            await client.rewind_files(second_uuid)
            await asyncio.sleep(1.0)  # the rewind is a control request, not part of a turn
            after_rewind = state(workspace)
            print(f"after rewind to the second turn's uuid:\n  files: {after_rewind}")

    expected = {TOOL_FILE: "v1", BASH_FILE: "v1", BASH_NEW: "<missing>"}
    verdict: dict[str, Any] = {}
    for name, want in expected.items():
        verdict[name] = "put back" if after_rewind[name] == want else "LEFT AS IT WAS"
    print("\nverdict:")
    for name, said in verdict.items():
        print(f"  {name}: {said} (wanted {expected[name]!r}, found {after_rewind[name]!r})")
    return 0 if all(said == "put back" for said in verdict.values()) else 1


sys.exit(asyncio.run(main()))
