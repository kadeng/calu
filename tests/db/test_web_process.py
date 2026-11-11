"""`calu web` as a real process, with its own credential store.

The pages are tested in process (`tests/db/test_web.py`) and in a browser
(`tests/browser/`). What only a process can show is the command around them: the credential it
asks for, the server it starts, the port it binds -- and that it **refuses** a non-loopback
address, which is the only thing between "no login yet" and a herd overview on the open
network.
"""

import os
import socket
import sys
from pathlib import Path

import anyio
import httpx
import pytest

from calu.db import Db, connect
from calu.setup import setup

pytestmark = [pytest.mark.db, pytest.mark.anyio]

START_TIMEOUT_S = 40


async def test_calu_web_serves_the_fleet_and_refuses_a_public_address(
    db_url: str, tmp_path: Path
) -> None:
    """The command starts the server on loopback and will not bind anything else."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        await Db(conn).execute("SELECT 1")
    finally:
        await conn.close()

    home = tmp_path / "config"
    home.mkdir()
    environment = {**os.environ, "XDG_CONFIG_HOME": str(home), "PYTHONUNBUFFERED": "1"}
    password = "web-process-test"

    # Store the admin credential the way a person does: the connection string, then a password.
    setting = await anyio.open_process(
        [sys.executable, "-m", "calu", "credential", "set", "admin"],
        env=environment,
        stdin=-1,
    )
    assert setting.stdin is not None
    await setting.stdin.send(f"{db_url}\n{password}\n{password}\n".encode())
    await setting.stdin.aclose()
    await setting.wait()
    assert setting.returncode == 0, "the credential was not stored"

    # A public address is refused before anything is served.
    public = await anyio.open_process(
        [sys.executable, "-m", "calu", "web", "--host", "0.0.0.0", "--port", "8899"],
        env=environment,
        stdin=-1,
        stderr=-1,
    )
    assert public.stdin is not None
    await public.stdin.send(f"{password}\n".encode())
    await public.stdin.aclose()
    with anyio.fail_after(START_TIMEOUT_S):
        said = b""
        assert public.stderr is not None
        while True:
            try:
                chunk = await public.stderr.receive(4096)
            except anyio.EndOfStream:
                break
            if not chunk:
                break
            said += chunk
        await public.wait()
    assert public.returncode == 1
    refusal = said.decode(errors="replace")
    assert "refuses to serve 0.0.0.0" in refusal
    # And it says how to reach it from elsewhere instead of only saying no.
    assert "ssh -L" in refusal

    # On loopback it serves the fleet page.
    # A free port, not a fixed one: with 8897 the test once read the answers of another
    # `calu web` that happened to hold that port, and failed on content it never served.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = await anyio.open_process(
        [sys.executable, "-m", "calu", "web", "--port", str(port)],
        env=environment,
        stdin=-1,
    )
    assert server.stdin is not None
    await server.stdin.send(f"{password}\n".encode())
    try:
        with anyio.fail_after(START_TIMEOUT_S):
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        answer = await client.get(f"http://127.0.0.1:{port}/", timeout=2)
                        break
                    except httpx.HTTPError:
                        await anyio.sleep(0.3)
            assert answer.status_code == 200
            assert "calu" in answer.text
            # An empty database is the first thing a new user sees, so the page has to say
            # what to do rather than showing an empty table.
            assert "project directory" in answer.text
            # And it refreshes itself, which is what makes it usable as a watch window.
            assert "location.reload" in answer.text
    finally:
        # Only if it is still running: `terminate()` on an exited process raises
        # ProcessLookupError, which turns a passing test into a flake (see
        # `tests/unit/test_acp_process.py`).
        if server.returncode is None:
            server.terminate()
            with anyio.move_on_after(10):
                await server.wait()
