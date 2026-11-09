"""Does a PreToolUse hook that does not answer in time let the tool call run?

calu's whole guarantee rests on this: a hook that hangs must stop the step, not wave it
through. The reference says the in-process hook of the Agent SDK fails **closed** (the call is
not run and Claude gets a "hook didn't respond" result, CLI v2.1.210+), while a shell hook of a
plugin fails **open** (the call continues through the normal permission flow). This spike checks
the SDK path with the installed CLI.

    uv run python spikes/spike_hook_timeout.py

Setup: a throwaway directory, `--tools Bash`, `permission_mode` "bypassPermissions" (so that
every fallback path would run the command), and a PreToolUse hook with `timeout=2` that sleeps
20 seconds and would then allow. Claude is asked to run `echo ... > marker.txt`.

    marker.txt exists        -> fail-open: the timeout let the call through
    marker.txt does not      -> fail-closed: the timeout stopped the call

The second run is the control: the same hook without the sleep, so the marker must appear.
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage

MARKER = "marker.txt"
HOOK_TIMEOUT_S = 2
SLEEP_S = 20
PROMPT = f"Run exactly this command with the Bash tool: echo spike > {MARKER}. Then stop."


async def run(slow: bool) -> tuple[bool, float, str]:
    """Run one session whose PreToolUse hook sleeps (or does not) past its timeout.

    Args:
        slow: If True, the hook sleeps `SLEEP_S` seconds before it allows the call.

    Returns:
        Whether the marker file exists afterwards, how long the run took, and the text of the
        session's result message.
    """
    calls: list[str] = []

    async def pre_tool_use(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Answer "allow", after a sleep that exceeds the hook's own timeout.

        Args:
            input_data: The hook input.
            tool_use_id: Id of the tool call.
            context: SDK hook context (unused).

        Returns:
            A hook output that allows the call.
        """
        calls.append(str(input_data.get("tool_name")))
        if slow:
            await asyncio.sleep(SLEEP_S)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "the spike allows everything",
            }
        }

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="haiku",
            tools=["Bash"],
            allowed_tools=["Bash"],
            permission_mode="bypassPermissions",
            setting_sources=[],
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[pre_tool_use],  # type: ignore[list-item]
                        timeout=HOOK_TIMEOUT_S,
                    )
                ]
            },
        )
        started = time.monotonic()
        result = ""
        async with ClaudeSDKClient(options=options) as client:
            await client.query(PROMPT)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    result = str(message.result)
        took = time.monotonic() - started
        exists = (workspace / MARKER).is_file()
    print(f"  hook calls: {calls}")
    return exists, took, result


async def main() -> int:
    """Run the slow hook and the control, and print the verdict.

    Returns:
        0 if the slow hook stopped the call (fail-closed), 1 if it let it through.
    """
    print(f"slow hook (timeout {HOOK_TIMEOUT_S} s, sleeps {SLEEP_S} s):")
    slow_ran, slow_took, slow_result = await run(slow=True)
    print(f"  marker written: {slow_ran}   run took {slow_took:.1f} s")
    print(f"  result: {slow_result[:300]}")
    print("control (the same hook, no sleep):")
    fast_ran, fast_took, fast_result = await run(slow=False)
    print(f"  marker written: {fast_ran}   run took {fast_took:.1f} s")
    print(f"  result: {fast_result[:200]}")
    verdict = (
        "FAIL-OPEN: the hook timeout let the call run"
        if slow_ran
        else "fail-closed: the hook timeout stopped the call"
    )
    print(f"\nverdict: {verdict}")
    if not fast_ran:
        print("warning: the control did not write the marker either; the setup proves nothing")
    return 1 if slow_ran else 0


sys.exit(asyncio.run(main()))
