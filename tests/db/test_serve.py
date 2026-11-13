"""`calu serve` against the test database: requests, sessions as tasks, refusals (D208)."""

import os
import signal
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import psycopg
import pytest

from calu import herd, session_requests
from calu.claude import FakeRunner
from calu.credentials import CredentialStore
from calu.db import ConnectionSource, Db, connect
from calu.protocol.client import connect as connect_client
from calu.serve import ServeOptions, Server, os_user
from calu.session_requests import NoServer, SessionRequest
from calu.testing import fake_models
from calu.web.app import create_app
from tests.db.test_attach_discovery import _read_until
from tests.db.test_run import SCRIPT, prepare

pytestmark = [pytest.mark.db, pytest.mark.anyio]

HOST = "serve-test-host"
USER = "serve-test-user"


@asynccontextmanager
async def serving(
    session_url: str, hostname: str = HOST, user: str = USER, **options
) -> AsyncGenerator[Server]:
    """Run a server with fake runners and fake checker models for the duration of the block.

    Args:
        session_url: The session login.
        hostname: The host whose requests the server answers.
        user: The OS user whose requests the server answers.
        **options: Fields of `ServeOptions`.

    Yields:
        The server, subscribed to new requests.
    """
    server = Server(
        ConnectionSource(session_url),
        set(),
        ServeOptions(**options),
        runner_factory=lambda workspace, _config, _hidden: FakeRunner(SCRIPT, workspace),
        checker_models=fake_models(),
        hostname=hostname,
        user=user,
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.run)
        with anyio.fail_after(20):
            await server.listening.wait()
        try:
            yield server
        finally:
            tg.cancel_scope.cancel()


@asynccontextmanager
async def owner_db(db_url: str) -> AsyncGenerator[Db]:
    """Open the owner's connection, which may file requests as an operator may.

    Args:
        db_url: The owner login.

    Yields:
        The wrapped connection.
    """
    conn = await connect(db_url)
    try:
        yield Db(conn)
    finally:
        await conn.close()


def request(directory: Path, **fields) -> SessionRequest:
    """Build a request for the test server's host and user.

    Args:
        directory: The project directory.
        **fields: Fields that differ from a new conversation.

    Returns:
        The request.
    """
    return SessionRequest(hostname=HOST, os_user=USER, directory=str(directory), **fields)


async def ask(db: Db, wanted: SessionRequest) -> SessionRequest:
    """File a request and wait for its outcome.

    Args:
        db: The requester's connection.
        wanted: The request.

    Returns:
        The finished request.
    """
    with anyio.fail_after(30):
        return await session_requests.wait_for(db, await session_requests.file(db, wanted))


async def _ignore(method: str, params: dict) -> None:
    """Drop an event.

    Args:
        method: The event's name.
        params: Its content.
    """


async def test_one_server_runs_sessions_in_two_directories(db_url: str, tmp_path: Path, capsys):
    session_url = await prepare(db_url, tmp_path)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    async with owner_db(db_url) as db, serving(session_url) as server:
        a = await ask(db, request(first, label="test"))
        b = await ask(db, request(second, prompt="have a look"))
        assert (a.state, b.state) == ("started", "started")
        assert a.session_id and b.session_id and a.session_id != b.session_id
        rows = {row.id: row for row in await herd.sessions(db, active_only=True)}
        assert rows[a.session_id].directory == str(first)
        assert rows[b.session_id].directory == str(second)
        assert rows[a.session_id].address != rows[b.session_id].address
        assert len(server.sessions) == 2

        # A client attaches the way `calu attach` does: it mints a token for the session.
        _, token = await herd.mint_token(db, a.session_id, "test")
        async with connect_client(rows[a.session_id].address, token, "test", _ignore) as client:
            snapshot = await client.request("state.get")
            assert snapshot["workspace"] == str(first)
            # `shutdown` ends this session, not the server and not the other session.
            await client.request("shutdown")
        with anyio.fail_after(20):
            while len(server.sessions) > 1:
                await anyio.sleep(0.05)
        assert list(server.sessions) == [b.id]
        # The session with the prompt did not end when its turn was over: it waits.
        still = {row.id: row for row in await herd.sessions(db, active_only=True)}
        assert b.session_id in still and a.session_id not in still
        # No new authentication: a third session from the same server.
        c = await ask(db, request(first))
        assert c.state == "started"
    # Stopping the server stopped the two sessions that were left.
    async with owner_db(db_url) as db:
        assert await db.all("SELECT state FROM sessions WHERE ended_at IS NULL") == []
    out = capsys.readouterr().out
    assert f"[one#{a.id}] [serve] started: session {a.session_id}" in out
    assert "--token=" not in out


async def test_without_a_server_the_request_is_withdrawn(db_url: str, tmp_path: Path):
    await prepare(db_url, tmp_path)
    async with owner_db(db_url) as db:
        filed = await session_requests.file(db, request(tmp_path))
        with pytest.raises(NoServer):
            await session_requests.wait_for(db, filed, claim_wait_s=0.3)
        again = await session_requests.read(db, filed)
        assert again is not None and again.state == "withdrawn"


async def test_requests_the_server_refuses_say_why(db_url: str, tmp_path: Path):
    session_url = await prepare(db_url, tmp_path)
    conversation = "11111111-2222-4333-8444-555555555555"
    async with owner_db(db_url) as db, serving(session_url, max_sessions=2) as server:
        missing = await ask(db, request(tmp_path / "nowhere"))
        assert missing.state == "failed" and "is not a directory" in missing.message
        bad = await ask(db, request(tmp_path, kind="fork", claude_session_id="abc"))
        assert bad.state == "failed" and "no Claude session id" in bad.message
        one = await ask(db, request(tmp_path, kind="resume", claude_session_id=conversation))
        assert one.state == "started"
        twice = await ask(db, request(tmp_path, kind="resume", claude_session_id=conversation))
        assert twice.state == "failed" and "already runs the conversation" in twice.message
        fork = await ask(db, request(tmp_path, kind="fork", claude_session_id=conversation))
        assert fork.state == "started"
        full = await ask(db, request(tmp_path))
        assert full.state == "failed" and "its limit" in full.message
        assert sorted(server.sessions) == [one.id, fork.id]


async def test_only_recent_requests_for_this_host_and_user_are_claimed(db_url: str, tmp_path: Path):
    session_url = await prepare(db_url, tmp_path)
    async with owner_db(db_url) as db:
        other_host = await session_requests.file(
            db, SessionRequest(hostname="elsewhere", os_user=USER, directory=str(tmp_path))
        )
        other_user = await session_requests.file(
            db, SessionRequest(hostname=HOST, os_user="somebody", directory=str(tmp_path))
        )
        old = await session_requests.file(db, request(tmp_path))
        await db.execute(
            "UPDATE session_requests SET created_at = now() - interval '5 minutes' WHERE id = %s",
            (old,),
        )
        async with serving(session_url) as server:
            mine = await ask(db, request(tmp_path))
            assert mine.state == "started"
            assert list(server.sessions) == [mine.id]
        for left in (other_host, other_user, old):
            row = await session_requests.read(db, left)
            assert row is not None and row.state == "pending"


async def test_a_session_login_cannot_ask_for_a_session(db_url: str, tmp_path: Path):
    """What a running session holds is not enough to start another one, or to read requests."""
    session_url = await prepare(db_url, tmp_path)
    conn = await connect(session_url)
    try:
        db = Db(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await session_requests.file(db, request(tmp_path))
        await conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await db.all("SELECT prompt FROM session_requests")
        await conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await session_requests.withdraw(db, 1)
    finally:
        await conn.close()


async def test_calu_start_as_a_process_gets_a_session_and_attaches(db_url: str, tmp_path: Path):
    """The command a user runs: no address, no token, no password; then `/shutdown`."""
    session_url = await prepare(db_url, tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = tmp_path / "config"
    CredentialStore(config / "calu" / "credentials.json").save("operator", db_url, None)
    environment = dict(os.environ) | {"PYTHONUNBUFFERED": "1", "XDG_CONFIG_HOME": str(config)}
    errors = tmp_path / "start-stderr.log"
    async with serving(session_url, hostname=socket.gethostname(), user=os_user()) as server:
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "start", "--plain", "-p", "have a look"],
                env=environment,
                cwd=str(workspace),
                stderr=log.fileno(),
            )
        seen: list[str] = []
        try:
            with anyio.fail_after(60):
                shown = await _read_until(process, "[attached]", seen, errors)
                assert "[session] " in shown
                assert str(workspace) in shown
                assert process.stdin is not None
                await process.stdin.send(b"/shutdown\n")
                await process.stdin.aclose()
                await process.wait()
            assert process.returncode == 0, errors.read_text(encoding="utf-8", errors="replace")
            with anyio.fail_after(20):
                while server.sessions:
                    await anyio.sleep(0.05)
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()


async def test_calu_start_without_a_server_says_what_to_do(db_url: str, tmp_path: Path):
    await prepare(db_url, tmp_path)
    config = tmp_path / "config"
    CredentialStore(config / "calu" / "credentials.json").save("operator", db_url, None)
    environment = dict(os.environ) | {"XDG_CONFIG_HOME": str(config)}
    done = await anyio.run_process(
        [sys.executable, "-m", "calu", "start", "--no-attach"],
        env=environment,
        cwd=str(tmp_path),
        check=False,
    )
    assert done.returncode != 0
    assert "no `calu serve` answered" in done.stderr.decode()
    async with owner_db(db_url) as db:
        assert await db.all("SELECT state FROM session_requests") == [("withdrawn",)]


async def test_an_idle_session_without_a_client_ends_after_the_idle_time(
    db_url: str, tmp_path: Path, capsys
):
    session_url = await prepare(db_url, tmp_path)
    async with owner_db(db_url) as db, serving(session_url, idle_timeout_s=0.4) as server:
        done = await ask(db, request(tmp_path))
        assert done.state == "started"
        session_id = done.session_id or ""
        rows = {row.id: row for row in await herd.sessions(db, active_only=True)}
        _, token = await herd.mint_token(db, session_id, "test")
        # An attached client keeps the session, however long it is idle.
        async with connect_client(rows[session_id].address, token, "test", _ignore):
            await anyio.sleep(1.0)
            assert list(server.sessions) == [done.id]
        with anyio.fail_after(20):
            while server.sessions:
                await anyio.sleep(0.05)
    assert "with no client attached: ending it" in capsys.readouterr().out


async def test_calu_serve_as_a_process_answers_and_stops_on_sigterm(db_url: str, tmp_path: Path):
    """The command itself: the password on stdin, a refused request, SIGTERM.

    Starts `calu serve` with a credential store in `tmp_path`; no Claude Code is started,
    because the one request names a directory that does not exist. On a failure it prints
    what the child wrote to stdout and stderr (the server's own lines, no secrets).
    """
    session_url = await prepare(db_url, tmp_path)
    config = tmp_path / "config"
    CredentialStore(config / "calu" / "credentials.json").save(
        "session", session_url, bytearray(b"a long enough password")
    )
    environment = dict(os.environ) | {"PYTHONUNBUFFERED": "1", "XDG_CONFIG_HOME": str(config)}
    errors = tmp_path / "serve-stderr.log"
    with errors.open("wb") as log:
        process = await anyio.open_process(
            [sys.executable, "-m", "calu", "serve"], env=environment, stderr=log.fileno()
        )
    seen: list[str] = []
    try:
        assert process.stdin is not None
        await process.stdin.send(b"a long enough password\n")
        with anyio.fail_after(60):
            await _read_until(process, "[serve] answering session requests", seen, errors)
            async with owner_db(db_url) as db:
                wanted = SessionRequest(
                    hostname=socket.gethostname(),
                    os_user=os_user(),
                    directory=str(tmp_path / "nowhere"),
                )
                done = await session_requests.wait_for(db, await session_requests.file(db, wanted))
            assert done.state == "failed" and "is not a directory" in done.message
            process.send_signal(signal.SIGTERM)
            await process.wait()
        assert process.returncode == 0, errors.read_text(encoding="utf-8", errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(10):
                await process.wait()


async def test_the_web_interface_asks_the_server_for_a_session(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The buttons of the session page: they write the row `calu start` writes.

    Runs the web application in this process against the test database, with a server that
    uses fake runners; prints nothing.
    """
    session_url = await prepare(db_url, tmp_path)
    async with owner_db(db_url) as db:
        web = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(db, git_url_template="")),
            base_url="http://127.0.0.1",
        )
        host, user = socket.gethostname(), os_user()
        async with web, serving(session_url, host, user) as server:
            # A session's row names the real host and user, so the server answers for those.
            first = await ask(
                db, SessionRequest(hostname=host, os_user=user, directory=str(tmp_path))
            )
            page = await web.get(f"/sessions/{first.session_id}")
            assert "New session in this directory" in page.text
            assert "Fork this conversation" in page.text

            new = await web.post(f"/sessions/{first.session_id}/request", data={"kind": "new"})
            assert new.status_code == 303, new.text
            started = new.headers["location"].split("/sessions/")[1].removesuffix("/live")
            assert started != first.session_id
            rows = {row.id: row for row in await herd.sessions(db, active_only=True)}
            assert rows[started].directory == str(tmp_path)
            assert len(server.sessions) == 2

            fork = await web.post(
                f"/sessions/{first.session_id}/request",
                data={"kind": "fork", "prompt": "go on"},
            )
            assert fork.status_code == 303, fork.text
            (filed,) = await db.all(
                "SELECT kind, claude_session_id, prompt, label FROM session_requests"
                " ORDER BY id DESC LIMIT 1"
            )
            assert filed == (
                "fork",
                rows[first.session_id or ""].claude_session_id,
                "go on",
                "calu web",
            )

            bad = await web.post(f"/sessions/{first.session_id}/request", data={"kind": "x"})
            assert bad.status_code == 400
        # The server is gone: the page says so instead of waiting for ever.
        monkeypatch.setattr(session_requests, "CLAIM_WAIT_S", 0.3)
        web = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(db, git_url_template="")),
            base_url="http://127.0.0.1",
        )
        async with web:
            nobody = await web.post(f"/sessions/{first.session_id}/request", data={"kind": "new"})
        assert nobody.status_code == 503 and "calu serve" in nobody.text
