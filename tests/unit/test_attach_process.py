"""`calu attach` as a real process against a real session.

The console's commands are covered in process (`tests/unit/test_attach.py`), but the client an
operator actually runs in tmux has never been driven as a process: the connection, the snapshot
it prints on attaching, the loop that reads typed lines, the events arriving while it waits, and
detaching at end of input. All of that lives in `attach()` and in `read_lines`, which a
FakeClient cannot reach.
"""

import os
import sys
from pathlib import Path

import anyio
import pytest

from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

TIMEOUT_S = 40


async def _read_until(process: object, wanted: str, collected: list[str]) -> str:
    """Read the process's stdout until a line contains `wanted`.

    Args:
        process: The running `calu attach` process.
        wanted: The text to wait for.
        collected: Every chunk read so far, appended to.

    Returns:
        Everything read so far.

    Raises:
        AssertionError: If stdout ends first.
    """
    stdout = process.stdout  # type: ignore[attr-defined]
    text = "".join(collected)
    while wanted not in text:
        try:
            chunk = await stdout.receive(4096)
        except anyio.EndOfStream:
            chunk = b""
        if not chunk:
            pytest.fail(f"stdout ended before {wanted!r} appeared; saw:\n{text}")
        collected.append(chunk.decode(errors="replace"))
        text = "".join(collected)
    return text


async def test_attach_prints_the_state_takes_a_prompt_and_detaches(tmp_path: Path) -> None:
    """One session, one prompt, one command, then end of input."""
    turns = [[{"text": "the answer"}]]
    async with served(tmp_path, turns) as run:
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        process = await anyio.open_process(
            [sys.executable, "-m", "calu", "attach", run.address, "--token", run.token],
            env=environment,
            cwd=str(tmp_path),
        )
        seen: list[str] = []
        try:
            with anyio.fail_after(TIMEOUT_S):
                # On attaching it prints the session's state and the command line.
                shown = await _read_until(process, "[attached]", seen)
                # The state line names the state, the workspace and the model, so an operator
                # knows what they attached to before typing anything.
                assert "[state]" in shown
                assert str(tmp_path) in shown

                assert process.stdin is not None
                # A command, not a chat message: the checker definitions come from the session
                # and print one line each.
                await process.stdin.send(b"/checkers\n")
                await _read_until(process, "route command", seen)

                # Starting the session and sending a prompt both go through the protocol, and
                # the answer comes back as an event on the same connection.
                await process.stdin.send(b"/start\n")
                await _read_until(process, "[started]", seen)
                await process.stdin.send(b"have a look\n")
                await _read_until(process, "the answer", seen)

                # The turn's end is printed too, so an operator knows Claude stopped.
                await _read_until(process, "turn ended", seen)

                # End of input detaches, and the process ends by itself.
                await process.stdin.aclose()
                await process.wait()
            assert process.returncode == 0
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()


async def test_attach_reports_a_refused_token_instead_of_a_traceback(tmp_path: Path) -> None:
    """The first thing someone gets wrong is pasting the wrong token."""
    async with served(tmp_path) as run:
        process = await anyio.open_process(
            [sys.executable, "-m", "calu", "attach", run.address, "--token", "not-the-token"],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            cwd=str(tmp_path),
        )
        try:
            with anyio.fail_after(TIMEOUT_S):
                stderr = b""
                assert process.stderr is not None
                while True:
                    try:
                        chunk = await process.stderr.receive(4096)
                    except anyio.EndOfStream:
                        break
                    if not chunk:
                        break
                    stderr += chunk
                await process.wait()
            said = stderr.decode(errors="replace")
            assert "calu:" in said, f"no calu error line in:\n{said}"
            assert "Traceback" not in said
            assert process.returncode == 1
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()
