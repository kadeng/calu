"""Does Claude Code's auto mode ask, and where does the question arrive? (PLAN.md D38)

calu's PASS leaves the decision to Claude Code's own approval (D21): the hook answers with no
decision at all. The owner observed that auto mode does not only allow and deny but can also ask
for a confirmation. If it does that in headless mode, the SDK has only one way to ask — the
`can_use_tool` callback — which is exactly calu's wait for a human, so such a step would
escalate even though no checker objected. This spike checks that.

    uv run python spikes/spike_auto_mode_ask.py

Setup: `permission_mode="auto"`, a PreToolUse hook that returns an empty output (no decision, as
calu does for PASS), and a `can_use_tool` that records what reached it and denies with a marker.
Two prompts: something harmless (`ls`), and something outside the project directory, which is
the kind of step an approval layer tends to want confirmed.

    can_use_tool called      -> auto mode asks, and calu's `Session.ask` already handles it
    never called             -> auto mode only allows and denies in this mode
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
)
from claude_agent_sdk.types import PermissionResultDeny, ToolPermissionContext

OUTSIDE = Path(tempfile.gettempdir()) / "calu-spike-outside.txt"
PROMPTS = {
    "harmless": "Run `ls` with the Bash tool, then say only: listed.",
    "outside the project": (
        f"Use the Bash tool to run exactly: printf 'x\\n' > {OUTSIDE}\nThen say only: written."
    ),
    "a domain never seen before": (
        "Use the WebFetch tool on https://example.com and tell me in one sentence what the page "
        "says. Do not use any other tool."
    ),
    "deleting a directory": (
        "Use the Bash tool to run exactly: mkdir -p doomed && rm -rf doomed\nThen say only: gone."
    ),
}


async def run(prompt: str) -> tuple[list[str], list[str], str]:
    """Run one prompt in auto mode with a hook that decides nothing.

    Args:
        prompt: What to ask Claude to do.

    Returns:
        (the tools the hook saw, the tools `can_use_tool` was asked about, the final text).
    """
    hooked: list[str] = []
    asked: list[str] = []

    async def pre_tool_use(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Answer like calu's PASS: no decision, so the harness decides.

        Args:
            input_data: The hook input.
            tool_use_id: Id of the call.
            context: SDK hook context (unused).

        Returns:
            An output with the event name and no permission decision.
        """
        hooked.append(str(input_data.get("tool_name")))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> Any:
        """Record that the harness asked, and refuse with a marker.

        Args:
            tool_name: The tool.
            tool_input: Its input.
            ctx: SDK permission context.

        Returns:
            A denial carrying the marker.
        """
        asked.append(f"{tool_name} {json.dumps(tool_input)[:120]}")
        return PermissionResultDeny(message="the spike refused it to make the ask visible")

    with tempfile.TemporaryDirectory() as tmp:
        options = ClaudeAgentOptions(
            cwd=tmp,
            model="haiku",
            # WebFetch has to be available for the domain case: a first fetch of a domain is the
            # kind of step an approval layer asks about.
            tools=["Bash", "WebFetch", "Read", "Write"],
            permission_mode="auto",
            setting_sources=[],
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[pre_tool_use], timeout=120)  # type: ignore[list-item]
                ]
            },
        )
        result = ""
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    result = str(message.result)
    return hooked, asked, result


async def main() -> int:
    """Run both prompts and report whether the harness ever asked.

    Returns:
        0 if `can_use_tool` was called at least once, 1 if it never was.
    """
    OUTSIDE.unlink(missing_ok=True)
    ever = False
    for label, prompt in PROMPTS.items():
        print(f"\n=== {label}")
        hooked, asked, result = await run(prompt)
        ever = ever or bool(asked)
        print(f"  the hook saw: {hooked}")
        print(f"  can_use_tool was asked about: {asked or 'nothing'}")
        print(f"  Claude's answer: {result.strip()[:200]!r}")
    print(f"\n  the file outside the project exists: {OUTSIDE.is_file()}")
    OUTSIDE.unlink(missing_ok=True)
    print(
        "\nverdict: "
        + (
            "auto mode asks, and the question arrives at can_use_tool (calu escalates it)"
            if ever
            else "auto mode never asked in this run; it only allowed or denied"
        )
    )
    return 0 if ever else 1


sys.exit(asyncio.run(main()))
