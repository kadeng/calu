"""Spike: calu as root, Claude Code as another uid, through the Agent SDK (D206).

Question: when the process that holds the hooks and the permission callback runs as root and
Claude Code runs under an unprivileged uid without passwd entry, do the PreToolUse and
PostToolUse hooks, `can_use_tool`, the side question and a resume still work, and which
identity (uid, gid, supplementary groups, environment) does a command of Claude Code get?

Two ways to change the user are measured:

- `sdk-user`: `ClaudeAgentOptions(user=...)`, which the SDK hands to the process start.
- `wrapper`: `cli_path` names a script that changes uid, gid and groups with `setpriv` and
  then executes the real `claude`.
- `launcher`: `cli_path` names calu's own launcher `calu-claude` (D207), which was built
  after the first two were measured.

Run as root in a container, never on a workstation (`spikes/spike_other_uid.sh` does that).
Arguments: the mode, and a workspace directory owned by the target uid. Needs
`ANTHROPIC_API_KEY`; uses Haiku. Results are recorded in `spikes/RESULTS.md`.
"""

import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import claude_agent_sdk
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
)

from calu.launch import LAUNCH_ENV, RunAs, claude_path, launcher_path

UID = 1000
GID = 1000
STATE = Path(f"/var/lib/calu/claude/{UID}")
ROOT_ONLY = Path("/etc/calu/secret")
WRAPPER = Path("/usr/local/bin/claude-as-user")

CHECK = """\
echo "id: $(id)"
echo "HOME=$HOME"
echo "inherited=${CALU_SPIKE_INHERITED:-<unset>}"
echo "given=${CALU_SPIKE_GIVEN:-<unset>}"
cat %(secret)s 2>&1 | sed 's/^/root-only file: /'
head -c 40 /proc/%(pid)s/environ 2>&1 | tr '\\0' ' ' | sed 's/^/calu environ: /'; echo
kill -0 %(pid)s 2>&1 | sed 's/^/signal calu: /'; echo "signal exit: $?"
echo made > made.txt
sleep 14
"""


def prepare(workspace: Path) -> None:
    """Create the state directory, a file only root may read, the check script and the wrapper.

    Args:
        workspace: The project directory, owned by the target uid.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    os.chown(STATE, UID, GID)
    STATE.chmod(0o700)
    ROOT_ONLY.parent.mkdir(parents=True, exist_ok=True)
    ROOT_ONLY.write_text("only root reads this\n")
    ROOT_ONLY.chmod(0o600)
    check = workspace / "check.sh"
    check.write_text(CHECK % {"secret": ROOT_ONLY, "pid": os.getpid()})
    os.chown(check, UID, GID)
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    WRAPPER.write_text(
        f'#!/bin/sh\nexec setpriv --reuid {UID} --regid {GID} --clear-groups "{bundled}" "$@"\n'
    )
    WRAPPER.chmod(0o755)


def owner(path: Path) -> str:
    """Describe who owns a file.

    Args:
        path: The file.

    Returns:
        `uid:gid mode`, or `missing`.
    """
    if not path.exists():
        return "missing"
    status = path.stat()
    return f"{status.st_uid}:{status.st_gid} {stat.filemode(status.st_mode)}"


async def main(mode: str, workspace: Path) -> None:
    """Run one turn with a side question, then a resume, and print what happened.

    Args:
        mode: `sdk-user`, `sdk-user-str`, `wrapper` or `launcher`.
        workspace: The project directory, owned by the target uid.
    """
    started = time.monotonic()
    seen: dict[str, int] = {"pre": 0, "post": 0, "ask": 0}
    running = anyio.Event()

    def stamp(text: str) -> None:
        """Print a line with the seconds since the start.

        Args:
            text: The line.
        """
        print(f"[{time.monotonic() - started:6.1f}s] {text}", flush=True)

    async def pre(input_data: Any, tool_use_id: str | None, context: Any) -> dict:
        """Count a PreToolUse call and say which uid the callback runs as.

        Args:
            input_data: The hook's input.
            tool_use_id: Unused.
            context: Unused.

        Returns:
            An empty hook output.
        """
        seen["pre"] += 1
        stamp(f"PreToolUse {input_data.get('tool_name')} in uid {os.getuid()}")
        return {}

    async def post(input_data: Any, tool_use_id: str | None, context: Any) -> dict:
        """Count a PostToolUse call.

        Args:
            input_data: The hook's input.
            tool_use_id: Unused.
            context: Unused.

        Returns:
            An empty hook output.
        """
        seen["post"] += 1
        stamp(f"PostToolUse {input_data.get('tool_name')}")
        return {}

    async def may(tool: str, tool_input: dict[str, Any], context: Any) -> PermissionResultAllow:
        """Allow a tool call and count the question.

        Args:
            tool: The tool's name.
            tool_input: Its input.
            context: Unused.

        Returns:
            The permission.
        """
        seen["ask"] += 1
        stamp(f"can_use_tool {tool} in uid {os.getuid()}: {str(tool_input)[:80]}")
        running.set()
        return PermissionResultAllow()

    os.environ["CALU_SPIKE_INHERITED"] = "from-calu's-own-environment"
    environment = {
        "HOME": str(STATE),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "CALU_SPIKE_GIVEN": "from-options",
    }
    if mode == "launcher":
        run_as = RunAs(uid=UID, gid=GID, home=str(STATE))
        environment[LAUNCH_ENV] = run_as.spec(claude_path(None)).dump()
    hooks: Any = {
        "PreToolUse": [HookMatcher(hooks=[pre])],
        "PostToolUse": [HookMatcher(hooks=[post])],
    }

    def options(resume: str | None) -> ClaudeAgentOptions:
        """Build the options of one client.

        Args:
            resume: The conversation to resume, or None for a new one.

        Returns:
            The options for the mode.
        """
        how: dict[str, Any] = {
            "sdk-user": {"user": UID},
            "sdk-user-str": {"user": str(UID)},
            "wrapper": {"cli_path": str(WRAPPER)},
            "launcher": {"cli_path": str(launcher_path())},
        }[mode]
        return ClaudeAgentOptions(
            model="haiku",
            cwd=str(workspace),
            max_turns=6,
            hooks=hooks,
            can_use_tool=may,
            env=environment,
            resume=resume,
            setting_sources=[],
            stderr=lambda line: stamp(f"stderr: {line[:200]}"),
            **how,
        )

    session = None
    async with ClaudeSDKClient(options(None)) as client:
        await client.query(
            "The secret word is PELICAN. Run this Bash command in the foreground, exactly as "
            "written: sh ./check.sh > out.txt 2>&1   Then answer with the single word finished."
        )

        async def ask() -> None:
            """Ask a side question while the command runs."""
            with anyio.move_on_after(60):
                await running.wait()
            await anyio.sleep(5)
            query: Any = client._query
            answer = await query._send_control_request(
                {"subtype": "side_question", "question": "What is the secret word?"},
                timeout=120.0,
            )
            stamp(f"side answer: {str(answer)[:120]}")

        async with anyio.create_task_group() as tg:
            tg.start_soon(ask)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    session = message.session_id
                    stamp(f"result: {str(message.result)[:80]} (session {session})")

    stamp(f"callbacks: {seen}")
    out = workspace / "out.txt"
    print(out.read_text() if out.exists() else "out.txt is missing", flush=True)
    stamp(f"out.txt {owner(out)}; made.txt {owner(workspace / 'made.txt')}")
    stored = sorted(str(p.relative_to(STATE)) for p in STATE.rglob("*.jsonl"))
    stamp(f"state directory {owner(STATE)}; conversations: {stored}")

    async with ClaudeSDKClient(options(session)) as client:
        await client.query("What was the secret word? Answer with the word only.")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                stamp(f"resumed: {str(message.result)[:80]}")


if __name__ == "__main__":
    target = Path(sys.argv[2])
    prepare(target)
    anyio.run(main, sys.argv[1], target)
