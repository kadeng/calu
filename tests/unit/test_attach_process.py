"""`calu attach` as a real process against a real session.

The console's commands are covered in process (`tests/unit/test_attach.py`), but the client an
operator actually runs in tmux has never been driven as a process: the connection, the snapshot
it prints on attaching, the loop that reads typed lines, the events arriving while it waits, and
detaching at end of input. All of that lives in `attach()` and in `read_lines`, which a
FakeClient cannot reach.
"""

import os
import re
import sys
from pathlib import Path

import anyio
import pytest

from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

TIMEOUT_S = 40


async def _wait_until_connectable(address: str) -> None:
    """Wait until the session's address really accepts a connection.

    `SessionServer.ready` is set once the socket is bound, which should be enough -- but this
    test failed once in CI with an empty stdout, the symptom of a child whose connect was
    refused, and a flaky test is worth less than no test. Connecting from here first turns any
    remaining window into a wait instead of a failure, and if the port never accepts, the
    message says so about the harness rather than about `calu attach`.

    Args:
        address: The `ws://host:port` address the session recorded.

    Raises:
        AssertionError: If the port does not accept within ten seconds.
    """
    host, _, port = address.removeprefix("ws://").partition(":")
    with anyio.move_on_after(10):
        while True:
            try:
                stream = await anyio.connect_tcp(host, int(port))
            except OSError:
                await anyio.sleep(0.05)
                continue
            await stream.aclose()
            return
    pytest.fail(f"the session's own address {address} never accepted a connection")


async def _read_until(
    process: object, wanted: str, collected: list[str], errors: Path | None = None
) -> str:
    """Read the process's stdout until a line contains `wanted`.

    Args:
        process: The running `calu attach` process.
        wanted: The text to wait for.
        collected: Every chunk read so far, appended to.
        errors: File the process's stderr was redirected to; its content goes into the failure
            message, because a process that ends before printing says why only there.

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
            said = ""
            if errors is not None and errors.exists():
                said = errors.read_text(encoding="utf-8", errors="replace")
            code = getattr(process, "returncode", None)
            pytest.fail(
                f"stdout ended before {wanted!r} appeared (exit {code}).\n"
                f"stdout was:\n{text or '(empty)'}\nstderr was:\n{said or '(empty)'}"
            )
        collected.append(chunk.decode(errors="replace"))
        text = "".join(collected)
    return text


async def test_attach_prints_the_state_takes_a_prompt_and_detaches(tmp_path: Path) -> None:
    """One session, one prompt, one command, then end of input."""
    turns = [[{"text": "the answer"}]]
    async with served(tmp_path, turns) as run:
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        await _wait_until_connectable(run.address)
        errors = tmp_path / "attach-stderr.log"
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "attach", run.address, f"--token={run.token}"],
                env=environment,
                cwd=str(tmp_path),
                stderr=log.fileno(),
            )
        seen: list[str] = []
        try:
            with anyio.fail_after(TIMEOUT_S):
                # On attaching it prints the session's state and the command line.
                shown = await _read_until(process, "[attached]", seen, errors)
                # The state line names the state, the workspace and the model, so an operator
                # knows what they attached to before typing anything.
                assert "[state]" in shown
                assert str(tmp_path) in shown

                assert process.stdin is not None
                # A command, not a chat message: the checker definitions come from the session
                # and print one line each.
                await process.stdin.send(b"/checkers\n")
                await _read_until(process, "route command", seen, errors)

                # Starting the session and sending a prompt both go through the protocol, and
                # the answer comes back as an event on the same connection.
                await process.stdin.send(b"/start\n")
                await _read_until(process, "[started]", seen, errors)
                await process.stdin.send(b"have a look\n")
                await _read_until(process, "the answer", seen, errors)

                # The turn's end is printed too, so an operator knows Claude stopped.
                await _read_until(process, "turn ended", seen, errors)

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


async def test_attach_answers_an_escalation_and_the_session_continues(tmp_path: Path) -> None:
    """The loop the README showcases, through the real pipes.

    `calu attach` is where an operator answers a step that waits, and the console's commands are
    covered in process -- but not the loop as a whole: the escalation arriving unasked while the
    client idles, `/why` fetching what the checker saw, `/allow` resolving it, and Claude going
    on afterwards. A client that prints the question but cannot answer it would pass every
    in-process test and be useless.
    """
    turns = [
        [
            {"text": "I will deploy."},
            # The marker makes the fake checker escalate the step (`calu.testing`).
            {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "shipped"},
            {"text": "Deployed after you allowed it."},
        ]
    ]
    async with served(tmp_path, turns) as run:
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        await _wait_until_connectable(run.address)
        errors = tmp_path / "attach-escalation-stderr.log"
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "attach", run.address, f"--token={run.token}"],
                env=environment,
                cwd=str(tmp_path),
                stderr=log.fileno(),
            )
        seen: list[str] = []
        try:
            with anyio.fail_after(TIMEOUT_S):
                await _read_until(process, "[attached]", seen, errors)
                assert process.stdin is not None
                await process.stdin.send(b"/start\n")
                await _read_until(process, "[started]", seen, errors)
                await process.stdin.send(b"deploy it\n")

                # The escalation arrives by itself, with the id to answer and how to answer it.
                shown = await _read_until(process, "escalation", seen, errors)
                ids = re.findall(r"escalation (\d+)", shown)
                assert ids, f"the escalation was printed without an id:\n{shown}"
                escalation_id = ids[0]

                # `/why` shows what the checker saw, which is what the decision rests on.
                await process.stdin.send(f"/why {escalation_id}\n".encode())
                await _read_until(process, "calu:escalate", seen, errors)

                # `/allow` resolves it and Claude goes on: the answer reaches the session, the
                # step runs, and the turn ends.
                await process.stdin.send(f"/allow {escalation_id} go ahead\n".encode())
                await _read_until(process, "Deployed after you allowed it.", seen, errors)
                await _read_until(process, "turn ended", seen, errors)

                await process.stdin.aclose()
                await process.wait()
            assert process.returncode == 0
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()
