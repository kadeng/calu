"""Can calu answer the agent's own question (`AskUserQuestion`)?

The owner's reading is that a question from the agent is an escalation of a kind: Claude wants
something only a human knows, and calu is the only thing between Claude and the human. Orca
renders Claude's `AskUserQuestion` as a card with choices, and opencode has a `question`
permission, so other harnesses do answer it. The open points for calu are:

1. does `AskUserQuestion` reach the PreToolUse hook at all in headless mode, and with what input
   (the question text and its options)?
2. can calu **answer** it, so that Claude acts on the answer? A hook can allow, deny with a
   reason, or ask; none of those is "here is the answer". The candidate is a denial whose reason
   carries the answer, since Claude reads that text as the tool's result.
3. what happens if calu does nothing and lets the call run — does Claude Code ask a terminal
   that is not there, fail, or answer itself?

    uv run python spikes/spike_question_tool.py

Three runs against the real CLI, each with a throwaway directory and the same prompt, which asks
Claude to use the tool and then say what the user chose:

    "allow"  the call runs unchecked: what does Claude Code do with it headless?
    "answer" the hook denies with the answer as its reason
    "ask"    the hook answers "ask", so `can_use_tool` decides (as calu's ESCALATE does)
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage
from claude_agent_sdk.types import PermissionResultDeny, ToolPermissionContext

TOOL = "AskUserQuestion"
ANSWER = "spaces, four of them"
PROMPT = (
    "Use the AskUserQuestion tool to ask me whether this project should indent with tabs or "
    "with spaces. Then tell me in one sentence what I answered. Do not do anything else."
)


async def run(mode: str) -> tuple[list[dict[str, Any]], str]:
    """Run one session and record what the hook saw and what Claude said at the end.

    Args:
        mode: "allow", "answer" or "ask" (see the module docstring).

    Returns:
        The hook inputs for `AskUserQuestion`, and the session's final result text.
    """
    seen: list[dict[str, Any]] = []

    async def pre_tool_use(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Record the call and answer according to `mode`.

        Args:
            input_data: The hook input.
            tool_use_id: Id of the tool call.
            context: SDK hook context (unused).

        Returns:
            A PreToolUse hook output.
        """
        name = str(input_data.get("tool_name"))
        if name == TOOL:
            seen.append(dict(input_data))
        output: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if name != TOOL or mode == "allow":
            output["permissionDecision"] = "allow"
            output["permissionDecisionReason"] = "the spike allows it"
        elif mode == "answer":
            output["permissionDecision"] = "deny"
            output["permissionDecisionReason"] = f"The user answered: {ANSWER}"
        else:
            output["permissionDecision"] = "ask"
            output["permissionDecisionReason"] = "the spike asks the human"
        return {"hookSpecificOutput": output}

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> Any:
        """Stand in for calu's human: deny with the answer as the message.

        Args:
            tool_name: The tool.
            tool_input: Its input.
            context: SDK permission context (unused).

        Returns:
            A denial whose message carries the answer.
        """
        return PermissionResultDeny(message=f"The user answered: {ANSWER}")

    with tempfile.TemporaryDirectory() as tmp:
        options = ClaudeAgentOptions(
            cwd=str(Path(tmp)),
            model="haiku",
            permission_mode="default",
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
            await client.query(PROMPT)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    result = str(message.result)
    return seen, result


async def main() -> int:
    """Run the three variants and print what each one shows.

    Returns:
        0 if the hook saw the question tool at all, 1 if it never did (then calu cannot answer
        it this way and the plan needs another route).
    """
    saw_any = False
    for mode in ("allow", "answer", "ask"):
        print(f"\n=== mode {mode}")
        try:
            seen, result = await run(mode)
        except Exception as exc:  # the CLI may refuse the tool outright
            print(f"  the run failed: {type(exc).__name__}: {exc}")
            continue
        saw_any = saw_any or bool(seen)
        print(f"  the hook saw {TOOL} {len(seen)} time(s)")
        for call in seen:
            print(f"    input: {json.dumps(call.get('tool_input'), indent=2)[:900]}")
            print(f"    keys: {sorted(call)}")
        print(f"  Claude's answer: {result[:400]}")
        print(f"  did it name the answer? {'yes' if ANSWER.split(',')[0] in result else 'no'}")
    return 0 if saw_any else 1


sys.exit(asyncio.run(main()))
