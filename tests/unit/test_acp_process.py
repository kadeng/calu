"""`calu acp` as a real process, driven the way an editor drives it.

`tests/unit/test_acp.py` exercises the adapter in process, with the peer wired to memory
streams. What that cannot cover is the entry point itself: that the command parses, that stdin
and stdout really carry the protocol, that **nothing but** protocol lines reach stdout (one
stray line of text breaks a JSON-RPC stream), and that the log goes to stderr instead.

So this starts a real session with its WebSocket server, spawns `python -m calu acp` as a
subprocess, and speaks ACP to its pipes. It also covers `calu.__main__`, which is how the
command is spawned here.

**Coverage does not see any of it**, because the code runs in another process: `calu/acp/serve.py`
still reports about 40% while being exercised end to end. Do not "fix" that by replacing the
subprocess with mocks -- the point of this test is the part mocks cannot check, that the real
process carries the protocol on its real pipes and keeps its log off stdout.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

TIMEOUT_S = 40


class Editor:
    """An ACP client on the pipes of a subprocess."""

    def __init__(self, process: Any) -> None:
        """Wrap the process.

        Args:
            process: The running `calu acp` process.
        """
        self.process = process
        self.id = 0
        self.buffer = b""
        self.lines: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any]) -> int:
        """Send a request and return its id.

        Args:
            method: The ACP method.
            params: Its params.

        Returns:
            The request id to wait for.
        """
        self.id += 1
        message = {"jsonrpc": "2.0", "id": self.id, "method": method, "params": params}
        assert self.process.stdin is not None
        await self.process.stdin.send((json.dumps(message) + "\n").encode())
        return self.id

    async def read_until(self, wanted: Any) -> dict[str, Any]:
        """Read messages until one matches, keeping the others.

        Args:
            wanted: Called with each parsed message; the first one it accepts is returned.

        Returns:
            The matching message.

        Raises:
            AssertionError: If stdout holds a line that is not JSON, or the stream ends first.
        """
        assert self.process.stdout is not None
        while True:
            for raw in self._take_lines():
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:  # pragma: no cover - the failure this guards
                    pytest.fail(f"stdout carried a line that is not protocol: {raw!r}")
                self.lines.append(message)
                if wanted(message):
                    return message
            chunk = await self.process.stdout.receive(4096)
            if not chunk:
                pytest.fail("the adapter closed stdout before answering")
            self.buffer += chunk

    def _take_lines(self) -> list[str]:
        """Split complete lines out of the buffer.

        Returns:
            The complete lines, which are removed from the buffer.
        """
        parts = self.buffer.split(b"\n")
        self.buffer = parts.pop()
        return [part.decode() for part in parts if part.strip()]


async def test_calu_acp_answers_an_editor_and_keeps_stdout_clean(tmp_path: Path) -> None:
    """The handshake, a session, a prompt and the answer, through a real process."""
    turns = [[{"text": "I looked at it."}]]
    async with served(tmp_path, turns) as run:
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        # The same guard as in `test_attach_process.py`: the child's connect must not race the
        # session's socket, which cost one flaky CI run there.
        host, _, port = run.address.removeprefix("ws://").partition(":")
        with anyio.move_on_after(10):
            while True:
                try:
                    probe = await anyio.connect_tcp(host, int(port))
                except OSError:
                    await anyio.sleep(0.05)
                    continue
                await probe.aclose()
                break
        errors = tmp_path / "stderr.log"
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "acp", run.address, "--token", run.token],
                env=environment,
                cwd=str(tmp_path),
                stderr=log.fileno(),
            )
        editor = Editor(process)
        try:
            with anyio.fail_after(TIMEOUT_S):
                first = await editor.send("initialize", {"protocolVersion": 1})
                answer = await editor.read_until(lambda m: m.get("id") == first)
                assert answer["result"]["protocolVersion"] == 1
                assert answer["result"]["agentCapabilities"]["loadSession"] is True

                second = await editor.send("session/new", {"cwd": str(tmp_path), "mcpServers": []})
                created = await editor.read_until(lambda m: m.get("id") == second)
                session_id = created["result"]["sessionId"]
                # The session mode travels with the new session (D42).
                assert created["result"]["modes"]["currentModeId"] == "checkers"

                third = await editor.send(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": "have a look"}]},
                )
                ended = await editor.read_until(lambda m: m.get("id") == third)
                assert ended["result"]["stopReason"] == "end_turn"

            # Claude's answer arrived as an update, not only as the result.
            texts = [
                message["params"]["update"]
                for message in editor.lines
                if message.get("method") == "session/update"
            ]
            assert any(
                update.get("sessionUpdate") == "agent_message_chunk"
                and "I looked at it." in json.dumps(update.get("content"))
                for update in texts
            ), f"no answer among {len(texts)} updates"
        finally:
            # Only if it is still running: `terminate()` on a process that has already exited
            # raises ProcessLookupError, which failed this test twice in one day without
            # anything being wrong with the adapter. The attach test guards it the same way.
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()

        # The log went to stderr; stdout stayed a protocol stream (`read_until` fails the test
        # on any line of stdout that is not JSON).
        said = errors.read_text(encoding="utf-8")
        assert "serving ACP" in said, f"nothing was logged to stderr:\n{said}"
        assert str(tmp_path) in said


async def test_calu_acp_carries_a_denial_and_replays_a_loaded_session(tmp_path: Path) -> None:
    """A denied step and `session/load`, through the real pipes (PLAN.md D24, D65b, D81).

    These are the two paths an editor depends on and that no in-process test covers end to end:
    a refusal has to arrive as a failed tool call with the reason, because an editor shows
    nothing else, and `session/load` has to replay the conversation **before** it returns, which
    the ACP specification requires of an agent that advertises `loadSession`.
    """
    turns = [
        [
            {"text": "I will try the deploy."},
            # The marker makes the rule-based fake checker deny the step (`calu.testing`).
            {"tool": "Bash", "input": {"command": "deploy # calu:deny"}, "result": "refused"},
            {"text": "It was refused, so I stopped."},
        ]
    ]
    async with served(tmp_path, turns) as run:
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        host, _, port = run.address.removeprefix("ws://").partition(":")
        with anyio.move_on_after(10):
            while True:
                try:
                    probe = await anyio.connect_tcp(host, int(port))
                except OSError:
                    await anyio.sleep(0.05)
                    continue
                await probe.aclose()
                break
        errors = tmp_path / "stderr-denial.log"
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "acp", run.address, "--token", run.token],
                env=environment,
                cwd=str(tmp_path),
                stderr=log.fileno(),
            )
        editor = Editor(process)
        try:
            with anyio.fail_after(TIMEOUT_S):
                first = await editor.send("initialize", {"protocolVersion": 1})
                await editor.read_until(lambda m: m.get("id") == first)
                second = await editor.send("session/new", {"cwd": str(tmp_path), "mcpServers": []})
                created = await editor.read_until(lambda m: m.get("id") == second)
                session_id = created["result"]["sessionId"]

                third = await editor.send(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": "deploy it"}]},
                )
                await editor.read_until(lambda m: m.get("id") == third)

                updates = [
                    message["params"]["update"]
                    for message in editor.lines
                    if message.get("method") == "session/update"
                ]
                # The denial reached the editor as a failed call with the checker's reason.
                denials = [
                    update
                    for update in updates
                    if update.get("sessionUpdate") == "tool_call_update"
                    and update.get("status") == "failed"
                ]
                assert denials, f"no failed tool call among {len(updates)} updates"
                said = json.dumps(denials)
                assert "checker: DENY" in said
                assert "calu:deny" in said, "the editor has to see why it was refused"

                # `session/load` replays the conversation before it answers, so every update
                # that belongs to the replay is already on the wire when the result arrives.
                before = len(editor.lines)
                fourth = await editor.send(
                    "session/load",
                    {
                        "sessionId": session_id,
                        "cwd": str(tmp_path),
                        "mcpServers": [],
                    },
                )
                loaded = await editor.read_until(lambda m: m.get("id") == fourth)
                assert "error" not in loaded, loaded
                # Compared by position, not by identity: a replayed chunk is often
                # byte-identical to the one sent during the prompt, so `list.index` would find
                # the earlier copy and compare the wrong thing.
                tail = list(enumerate(editor.lines))[before:]
                result_at = next(
                    position for position, message in tail if message.get("id") == fourth
                )
                replayed = [
                    (position, message)
                    for position, message in tail
                    if message.get("method") == "session/update"
                ]
                assert replayed, "session/load sent no history"
                # The replay carries the conversation: the user's own message and Claude's
                # answer, in the shapes an editor renders as chat.
                shapes = {
                    message["params"]["update"].get("sessionUpdate") for _p, message in replayed
                }
                assert {"user_message_chunk", "agent_message_chunk"} <= shapes, shapes
                text = json.dumps([message for _p, message in replayed])
                assert "deploy it" in text, "the user's own message belongs in the replay"
                assert all(position < result_at for position, _m in replayed), (
                    "the specification requires the replay before the result"
                )
        finally:
            # Only if it is still running: `terminate()` on a process that has already exited
            # raises ProcessLookupError, which failed this test twice in one day without
            # anything being wrong with the adapter. The attach test guards it the same way.
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()
