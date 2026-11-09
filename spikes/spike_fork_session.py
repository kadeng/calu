"""Can a session be forked at an earlier step, and what does the fork remember? (PLAN.md Q13)

Every other agent UI offers "rewind to here" and "fork from this response"; calu could only
resume a session by its id. The Agent SDK turns out to have the pieces:

    resume=<id>, fork_session=True                      fork at the end of the conversation
    + resume_session_at=<transcript entry uuid>          ... at an earlier point instead
    + resume_drops_turn=<uuid of the prompt to discard>  ... with a guard against the wrong point
    enable_file_checkpointing + rewind_files(uuid)       put the files back as they were
    get_session_messages(id, directory)                  the uuids to choose from

This spike checks the three claims that matter for calu with the installed CLI:

1. does `get_session_messages` give usable uuids for the turns of a finished session?
2. does a fork at the end of turn 1 really not know what turn 2 did — and does it get a new
   session id, leaving the original intact?
3. does `resume_drops_turn` refuse a wrong fork point, with the documented message?

    uv run python spikes/spike_fork_session.py

It writes two files in a throwaway directory through Claude, so nothing of yours is touched.
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
    get_session_messages,
)

FIRST = "Create a file called alpha.txt with the word alpha in it. Say only: done alpha."
SECOND = "Create a file called beta.txt with the word beta in it. Say only: done beta."
ASK = (
    "Which files have you created in this conversation? Answer with their names only, nothing "
    "else. Do not look at the file system; answer from our conversation."
)


def _is_prompt(message: Any) -> bool:
    """Tell whether a transcript entry is a prompt of the user, not a tool result.

    The transcript puts both under `type == "user"`: a tool result is a user-role message whose
    content holds `tool_result` blocks. Picking the wrong one as the dropped turn is what
    `resume_drops_turn` refuses, so this is the distinction a caller has to make.

    Args:
        message: A `SessionMessage`.

    Returns:
        True for a message the human (or the host) sent as a prompt.
    """
    if str(getattr(message, "type", "")) != "user":
        return False
    content = (getattr(message, "message", None) or {}).get("content")
    if isinstance(content, str):
        return True
    blocks = content if isinstance(content, list) else []
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks
    )


def _text(message: Any) -> str:
    """Return a short rendering of a transcript entry, for the printout.

    Args:
        message: A `SessionMessage`.

    Returns:
        At most 60 characters of its first text block.
    """
    content = (getattr(message, "message", None) or {}).get("content")
    if isinstance(content, str):
        return " ".join(content.split())[:60]
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") in ("text", "tool_use", "tool_result"):
            shown = str(block.get("text") or block.get("name") or block.get("type"))
            return " ".join(shown.split())[:60]
    return ""


def options(workspace: Path, **extra: Any) -> ClaudeAgentOptions:
    """Build the options for one run.

    Args:
        workspace: The project directory.
        **extra: Fields to add, e.g. `resume` or `fork_session`.

    Returns:
        The options, with the sandbox off and the transcript kept (a fork needs it).
    """
    return ClaudeAgentOptions(
        cwd=str(workspace),
        model="haiku",
        permission_mode="bypassPermissions",
        setting_sources=[],
        **extra,
    )


async def ask(client: ClaudeSDKClient, prompt: str) -> tuple[str, str, str]:
    """Send one prompt and collect what came back.

    Args:
        client: The connected client.
        prompt: The prompt to send.

    Returns:
        (the session id, the uuid of the last assistant message, the final result text).
    """
    await client.query(prompt)
    last_uuid, session_id, result = "", "", ""
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            last_uuid = str(getattr(message, "uuid", "") or last_uuid)
        if isinstance(message, ResultMessage):
            session_id = str(message.session_id)
            result = str(message.result)
    return session_id, last_uuid, result


async def main() -> int:
    """Run the three checks and print what each one shows.

    Returns:
        0 if a fork at an earlier step worked, 1 if it did not.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        print("=== the original session: two turns, one file each")
        async with ClaudeSDKClient(options(workspace)) as client:
            session_id, first_uuid, first_text = await ask(client, FIRST)
            print(f"  turn 1: {first_text.strip()[:60]!r} (assistant uuid {first_uuid[:8]})")
            _, second_uuid, second_text = await ask(client, SECOND)
            print(f"  turn 2: {second_text.strip()[:60]!r} (assistant uuid {second_uuid[:8]})")
        print(f"  session {session_id}")
        print(f"  files: {sorted(p.name for p in workspace.iterdir() if p.is_file())}")

        print("\n=== the transcript, as get_session_messages reads it")
        messages = get_session_messages(session_id, directory=str(workspace))
        for message in messages:
            kind = str(getattr(message, "type", "?"))
            uuid = str(getattr(message, "uuid", ""))
            print(
                f"  {kind:9} {uuid[:8]}  {'prompt' if _is_prompt(message) else ''} {_text(message)}"
            )
        prompts = [m for m in messages if _is_prompt(m)]
        print(f"  {len(prompts)} real prompt(s) among {len(messages)} entries")
        if len(prompts) < 2 or not first_uuid:
            print("  not enough to fork from; the transcript shape changed")
            return 1
        drops = str(getattr(prompts[1], "uuid", ""))
        # The rule of thumb from the SDK: fork at the *last entry of the kept turn*, which is
        # the entry just before the next prompt — not necessarily the last assistant message.
        index = [str(getattr(m, "uuid", "")) for m in messages].index(drops)
        fork_at = str(getattr(messages[index - 1], "uuid", ""))
        print(f"\n  last entry of turn 1: {fork_at[:8]} (assistant text was {first_uuid[:8]})")
        print(f"  forking there, dropping the turn that starts at {drops[:8]}")

        print("\n=== the fork: does it know about beta.txt?")
        forked = options(
            workspace,
            resume=session_id,
            fork_session=True,
            resume_session_at=fork_at,
            resume_drops_turn=drops,
        )
        try:
            async with ClaudeSDKClient(forked) as client:
                fork_id, _uuid, answer = await ask(client, ASK)
        except Exception as exc:
            print(f"  the fork failed: {type(exc).__name__}: {str(exc)[:300]}")
            return 1
        knows_alpha = "alpha" in answer.lower()
        knows_beta = "beta" in answer.lower()
        print(f"  answer: {answer.strip()[:200]!r}")
        print(f"  new session id: {fork_id != session_id} ({fork_id})")
        print(f"  remembers alpha: {knows_alpha}   remembers beta: {knows_beta}")

        print("\n=== the guard: a fork point that does not match the dropped turn")
        wrong = options(
            workspace,
            resume=session_id,
            fork_session=True,
            resume_session_at=fork_at,
            resume_drops_turn=str(getattr(prompts[0], "uuid", "")),
        )
        try:
            async with ClaudeSDKClient(wrong) as client:
                await ask(client, ASK)
            print("  no refusal: the guard did not fire")
        except Exception as exc:
            text = str(exc)
            marked = "Resume rejected by --resume-drops-turn:" in text
            print(f"  refused: {type(exc).__name__}, documented message: {marked}")
            print(f"    {' '.join(text.split())[:200]}")

        verdict = knows_alpha and not knows_beta
        print(
            f"\nverdict: {'a fork at an earlier step works' if verdict else 'INCONCLUSIVE'}"
            f" (remembers the kept turn: {knows_alpha}, forgot the dropped one: {not knows_beta})"
        )
        return 0 if verdict else 1


sys.exit(asyncio.run(main()))
