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
from calu.db.target import Target
from calu.setup import set_user_password, setup, user_login

pytestmark = [pytest.mark.db, pytest.mark.anyio, pytest.mark.slow]

START_TIMEOUT_S = 40


async def test_calu_web_serves_the_fleet_and_refuses_a_public_address(
    db_url: str, tmp_path: Path
) -> None:
    """The command starts the server on loopback and will not bind anything else; a browser
    signs in with the calu user password (D216, D265).

    Gives the server-wide role `calu_admin` the password "web-process-test"; stores the user
    login in a credential store below `tmp_path`. One wrong password makes the server wait
    about a second, which is the gate's delay.
    """
    password = "web-process-test"
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        await set_user_password(conn, password)
        await Db(conn).execute("SELECT 1")
    finally:
        await conn.close()
    user_url = user_login(Target.parse(db_url), password).libpq_string()

    home = tmp_path / "config"
    home.mkdir()
    environment = {**os.environ, "XDG_CONFIG_HOME": str(home), "PYTHONUNBUFFERED": "1"}

    # Store the user login the way a person does: the connection string, then the user
    # password, which protects the entry and is the password of `calu_admin`.
    setting = await anyio.open_process(
        [sys.executable, "-m", "calu", "credential", "set", "user"],
        env=environment,
        stdin=-1,
    )
    assert setting.stdin is not None
    await setting.stdin.send(f"{user_url}\n{password}\n{password}\n".encode())
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
            # The address it prints carries no token: the browser is asked the user password.
            assert server.stdout is not None
            printed = b""
            while b"sign in with" not in printed or not printed.endswith(b"\n"):
                printed += await server.stdout.receive(4096)
            line = next(x for x in printed.decode().splitlines() if "sign in with" in x)
            assert "?token=" not in printed.decode()
            # Not the bound address: the interface answers to its own host name only, so the
            # browser keeps its login apart from every other server on loopback.
            assert line.split()[3] == f"http://calu.localhost:{port}/"
            # This client does what a browser does by itself: `calu.localhost` is the loopback
            # interface. It connects there and names the host in the request.
            named = {"Host": f"calu.localhost:{port}"}
            local = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(follow_redirects=True) as client:
                while True:
                    try:
                        asked = await client.get(f"{local}/", headers=named, timeout=2)
                        break
                    except httpx.HTTPError:
                        await anyio.sleep(0.3)
                assert asked.status_code == 401 and "calu user password" in asked.text
                bare = await client.get(f"{local}/", timeout=5)
                assert bare.status_code == 403, "not under the bare loopback address"
                wrong = await client.post(
                    f"{local}/_login", headers=named, data={"password": "a guess", "onward": "/"}
                )
                assert wrong.status_code == 401 and "not the calu user password" in wrong.text
                login = await client.post(
                    f"{local}/_login", headers=named, data={"password": password, "onward": "/"}
                )
                assert login.status_code == 200 and "calu-key" in login.text
                # The login set the cookie, so the fleet page is served now.
                answer = await client.get(f"{local}/", headers=named, timeout=5)
            assert answer.status_code == 200
            assert "calu" in answer.text
            # An empty database is the first thing a new user sees, so the page has to say
            # what to do rather than showing an empty table.
            assert "project directory" in answer.text
            # And it refreshes itself, which is what makes it usable as a watch window.
            assert "data-reload-after" in answer.text
    finally:
        # Only if it is still running: `terminate()` on an exited process raises
        # ProcessLookupError, which turns a passing test into a flake (see
        # `tests/unit/test_acp_process.py`).
        if server.returncode is None:
            server.terminate()
            with anyio.move_on_after(10):
                await server.wait()


async def test_calu_serve_with_web_runs_the_web_interface_as_its_child(
    db_url: str, tmp_path: Path
) -> None:
    """D268: `calu serve --web` asks the user password once, starts `calu web` as its child
    with the same login (a browser signs in with that password), and the child ends with the
    server: after SIGTERM to the server, and after SIGKILL, which only the kernel's
    parent-death signal can pass on.

    Gives the server-wide role `calu_admin` the password "serve-web-test"; stores the user
    login in a credential store below `tmp_path`; starts the server in a projects directory
    below `tmp_path` (no session is started, so no `claude` is needed).
    """
    import signal

    password = "serve-web-test"
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        await set_user_password(conn, password)
    finally:
        await conn.close()
    user_url = user_login(Target.parse(db_url), password).libpq_string()
    home = tmp_path / "config"
    home.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PYTHONUNBUFFERED": "1",
    }
    setting = await anyio.open_process(
        [sys.executable, "-m", "calu", "credential", "set", "user"], env=environment, stdin=-1
    )
    assert setting.stdin is not None
    await setting.stdin.send(f"{user_url}\n{password}\n{password}\n".encode())
    await setting.stdin.aclose()
    await setting.wait()
    assert setting.returncode == 0

    for stop in (signal.SIGTERM, signal.SIGKILL):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "calu",
                "serve",
                "--projects",
                str(projects),
                "--no-relay",
                "--web",
                "--web-port",
                str(port),
            ],
            env=environment,
            stdin=-1,
        )
        assert server.stdin is not None and server.stdout is not None
        await server.stdin.send(f"{password}\n".encode())
        try:
            with anyio.fail_after(START_TIMEOUT_S):
                printed = b""
                while b"sign in with" not in printed or not printed.endswith(b"\n"):
                    printed += await server.stdout.receive(4096)
            text = printed.decode()
            started = next(x for x in text.splitlines() if x.startswith("started calu web"))
            child = int(started.split("(pid ")[1].split(")")[0])
            assert f"http://calu.localhost:{port}/" in text and "?token=" not in text
            named = {"Host": f"calu.localhost:{port}"}
            async with httpx.AsyncClient() as client:
                with anyio.fail_after(START_TIMEOUT_S):
                    while True:  # the address is printed just before the port is bound
                        try:
                            login = await client.post(
                                f"http://127.0.0.1:{port}/_login",
                                headers=named,
                                data={"password": password, "onward": "/"},
                                timeout=10,
                            )
                            break
                        except httpx.ConnectError:
                            await anyio.sleep(0.3)
            assert login.status_code == 200, "the child signs a browser in with the password"
            server.send_signal(stop)
            with anyio.fail_after(20):
                await server.wait()
                while Path(f"/proc/{child}").exists():
                    status = Path(f"/proc/{child}/stat").read_text().split(") ")[-1][0]
                    if status == "Z":  # ended; its parent (gone) cannot reap it
                        break
                    await anyio.sleep(0.2)
        finally:
            if server.returncode is None:
                server.kill()
                await server.wait()
