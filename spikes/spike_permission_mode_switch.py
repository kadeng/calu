"""How does a live change of Claude Code's permission mode behave? (agent/records/PLAN.md D247)

calu lets a client switch the permission mode of a running session (`mode.set`) through the
Agent SDK's `set_permission_mode`. This spike measures what the decision rests on:

1. Which modes a running Claude Code accepts, and what it answers for one it refuses.
2. Whether a message reports the new mode, or only the next hook input does.
3. Whether a change made while a tool call waits in the hook applies to the next call of the
   same turn.
4. Whether a hook "ask" still reaches `can_use_tool` in "acceptEdits" (calu's ESCALATE is a
   hook "ask"; if the mode approved the edit at its own step, an escalated edit would run
   without the human).

Plan mode is not measured here any more: in the first run (CLI 2.1.319, haiku) Claude Code
wrote its plan through the Write tool to a file under `~/.claude/plans` of the real home
directory, outside the throwaway directory, and asked no permission for it.

    uv run python spikes/spike_permission_mode_switch.py [model]

Setup: a throwaway directory, `setting_sources=[]`, tools Write and Bash, a PreToolUse hook
that records the `permission_mode` of its input and answers "ask" for files named `ask*` and
"allow" otherwise, and a `can_use_tool` that records the call and denies it. Default model
haiku (it has no auto mode, which is also measured).
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage
from claude_agent_sdk.types import PermissionResultDeny, ToolPermissionContext


async def main() -> int:
    """Run the measurements and print what was seen.

    Returns:
        0 always; the output is the result.
    """
    model = sys.argv[1] if len(sys.argv) > 1 else "haiku"
    seen: list[str] = []
    asked: list[str] = []
    hold = asyncio.Event()
    hold.set()
    in_hook = asyncio.Event()

    async def pre_tool_use(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        """Record the mode, wait while `hold` is clear, answer ask or allow by file name.

        Args:
            input_data: The hook input.
            tool_use_id: Id of the call.
            context: SDK hook context (unused).

        Returns:
            The hook output.
        """
        path = str((input_data.get("tool_input") or {}).get("file_path") or "")
        seen.append(
            f"{input_data.get('tool_name')} {Path(path).name} mode={input_data.get('permission_mode')}"
        )
        in_hook.set()
        await hold.wait()
        decision = "ask" if Path(path).name.startswith("ask") else "allow"
        return {
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}
        }

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> Any:
        """Record that the harness asked, and refuse.

        Args:
            name: The tool.
            tool_input: Its input.
            ctx: SDK permission context.

        Returns:
            A denial.
        """
        asked.append(f"{name} {Path(str(tool_input.get('file_path') or '')).name}")
        return PermissionResultDeny(message="refused by the spike")

    with tempfile.TemporaryDirectory() as tmp:
        options = ClaudeAgentOptions(
            cwd=tmp,
            model=model,
            tools=["Write", "Bash"],
            permission_mode="default",
            setting_sources=[],
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use], timeout=300)]},  # type: ignore[list-item]
        )
        async with ClaudeSDKClient(options=options) as client:

            async def turn(prompt: str) -> None:
                """Send one prompt and print the messages that carry a mode.

                Args:
                    prompt: The prompt.
                """
                await client.query(prompt)
                async for msg in client.receive_response():
                    data = getattr(msg, "data", None)
                    if isinstance(data, dict) and "permissionMode" in data:
                        print(
                            f"    message {type(msg).__name__} {data.get('subtype')}: permissionMode={data['permissionMode']}"
                        )
                    if isinstance(msg, ResultMessage):
                        print(f"    result: {str(msg.result)[:100]!r}")

            print("1. modes a running Claude Code accepts")
            for mode in [
                "acceptEdits",
                "plan",
                "auto",
                "dontAsk",
                "bypassPermissions",
                "nonsense",
                "default",
            ]:
                try:
                    await client.set_permission_mode(mode)  # type: ignore[arg-type]
                    print(f"   {mode}: accepted")
                except Exception as exc:
                    print(f"   {mode}: refused: {type(exc).__name__}: {exc}")
            await client.set_permission_mode("default")

            print("2. a mode set between turns, seen by the next hook input (auto with this model)")
            seen.clear()
            await client.set_permission_mode("auto")
            await turn("Use the Write tool to create a1.txt containing x. Then say only: done.")
            print(f"    hook saw: {seen}")

            print("3. a change while the hook waits applies to the next call of the turn?")
            await client.set_permission_mode("default")
            seen.clear()
            hold.clear()
            in_hook.clear()
            task = asyncio.create_task(
                turn(
                    "Use the Write tool to create b1.txt containing x, and after that has "
                    "finished, create b2.txt containing y (two separate Write calls, one after "
                    "the other). Then say only: done."
                )
            )
            await in_hook.wait()
            await client.set_permission_mode("acceptEdits")
            print("    set acceptEdits while the hook waited: returned")
            hold.set()
            await task
            print(f"    hook saw: {seen}")

            print("4. hook 'ask' on a Write in acceptEdits: does can_use_tool get it?")
            seen.clear()
            asked.clear()
            await turn("Use the Write tool to create ask1.txt containing x. Then say only: done.")
            print(
                f"    hook saw: {seen}; can_use_tool: {asked}; file exists: {(Path(tmp) / 'ask1.txt').exists()}"
            )

            print(json.dumps({"files": sorted(p.name for p in Path(tmp).iterdir())}))
    return 0


sys.exit(asyncio.run(main()))
