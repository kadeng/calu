"""The live view: the web server as a client of a real session (PLAN.md D34).

No browser here — this drives the relay WebSocket directly, so the chain under test is
browser command -> web server -> client token minted in the database -> the session's own
protocol -> the Claude session, and the events all the way back.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from websockets.asyncio.client import connect as ws_connect

from calu.bus import Bus
from calu.claude import FakeRunner, FakeScript
from calu.db import Db, connect
from calu.protocol.server import SessionServer
from calu.session import Session
from calu.setup import setup
from calu.store import DatabaseSessionStore, SessionIdentity
from calu.testing import fake_models
from calu.tokens import new_token, token_hash
from tests.browser.conftest import serve
from tests.unit.helpers import default_settings

# The web interface is an optional extra, so a checkout synced without it skips these
# tests instead of failing to collect them (`uv sync --all-extras`, docs/TESTING.md).
WEB_EXTRA = "the web interface needs the `web` extra: uv sync --all-extras"
pytest.importorskip("starlette", reason=WEB_EXTRA)
pytest.importorskip("jinja2", reason=WEB_EXTRA)

from calu.web.app import create_app  # ruff: ignore[module-import-not-at-top-of-file] (imported after the guard above)

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@asynccontextmanager
async def running(
    db_url: str, workspace: Path, turns: list[list[dict[str, Any]]]
) -> AsyncGenerator[tuple[str, str, Session]]:
    """A real session with its protocol server, its row in the database, and the web server.

    Yields (the web server's base URL, the session id, the session).
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        bus = Bus()
        session = Session(
            workspace,
            default_settings(workspace),
            store,
            lambda: FakeRunner(FakeScript(turns=turns), workspace),
            bus.publish,
            checker_models=fake_models(),
            snapshot_root=workspace.parent / "snapshots",
        )
        token = new_token()
        async with anyio.create_task_group() as tasks:
            server = SessionServer(session, bus, token_hash(token), tasks)
            tasks.start_soon(server.listen, "127.0.0.1", 0)
            with anyio.fail_after(10):
                await server.ready.wait()
            await session.start(tasks)
            await store.set_address(server.address)
            session_id = store.session_id or ""
            try:
                async with serve(create_app(db)) as base:
                    yield base, session_id, session
            finally:
                await session.stop()
                tasks.cancel_scope.cancel()
    finally:
        await conn.close()


async def read_until(socket: Any, wanted: str, timeout: float = 20) -> list[dict[str, Any]]:
    """Collect relayed messages until one of `wanted` (a type or an event method) arrives."""
    seen: list[dict[str, Any]] = []
    with anyio.fail_after(timeout):
        while True:
            message = json.loads(await socket.recv())
            seen.append(message)
            if message.get("type") == wanted or message.get("method") == wanted:
                return seen


async def test_the_browser_drives_a_real_session_through_the_relay(
    db_url: str, tmp_path: Path
) -> None:
    turns = [[{"text": "Looking."}, {"tool": "Bash", "input": {"command": "ls"}, "result": "a.py"}]]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        url = base.replace("http://", "ws://") + f"/ws/sessions/{session_id}"
        async with ws_connect(url) as socket:
            # The server attached with a token it minted itself, and sends the state first.
            first = json.loads(await socket.recv())
            assert first["type"] == "snapshot"
            assert first["snapshot"]["workspace"] == str(tmp_path)

            # A message from the browser reaches Claude, and its events come back.
            await socket.send(
                json.dumps({"method": "chat.send", "params": {"text": "look around"}})
            )
            seen = await read_until(socket, "turn.result")
            methods = [m.get("method") for m in seen if m.get("type") == "event"]
            assert "user.input" in methods
            assert "tool.use" in methods
            assert "check.result" in methods
            assert any(m.get("type") == "result" and m["method"] == "chat.send" for m in seen)

            # A method that is not on the list is refused rather than forwarded.
            await socket.send(json.dumps({"method": "shutdown", "params": {}}))
            refused = await read_until(socket, "refused")
            assert refused[-1]["method"] == "shutdown"
            assert "not allowed" in refused[-1]["message"]


async def test_an_escalation_is_answered_from_the_browser(db_url: str, tmp_path: Path) -> None:
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        url = base.replace("http://", "ws://") + f"/ws/sessions/{session_id}"
        async with ws_connect(url) as socket:
            assert json.loads(await socket.recv())["type"] == "snapshot"
            await socket.send(json.dumps({"method": "chat.send", "params": {"text": "ship it"}}))
            seen = await read_until(socket, "escalation.request")
            request = seen[-1]["params"]
            await socket.send(
                json.dumps(
                    {
                        "method": "escalation.decide",
                        "params": {
                            "escalation_id": request["escalation_id"],
                            "decision": "deny",
                            "note": "not from the browser",
                        },
                    }
                )
            )
            after = await read_until(socket, "turn.result")
            resolved = [m["params"] for m in after if m.get("method") == "escalation.resolved"]
            assert resolved and resolved[0]["decision"] == "deny"
            # The session recorded the decision of its client in the database (D28).
            conn = await connect(db_url)
            try:
                row = await Db(conn).one(
                    "SELECT state, resolved_by, note FROM escalations WHERE id = %s",
                    (int(request["escalation_id"]),),
                )
                assert row is not None
                assert row[0] == "denied"
                assert row[1].startswith("client:")
                assert row[2] == "not from the browser"
            finally:
                await conn.close()


async def test_a_session_without_an_address_says_so(db_url: str, tmp_path: Path) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        from calu.store import SessionIdentity

        store = DatabaseSessionStore(db)
        session_id = await store.start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=1, os="Linux")
        )
        async with serve(create_app(db)) as base:
            url = base.replace("http://", "ws://") + f"/ws/sessions/{session_id}"
            async with ws_connect(url) as socket:
                message = json.loads(await socket.recv())
                assert message["type"] == "closed"
                assert "serves no clients" in message["message"]
    finally:
        await conn.close()


async def test_the_live_view_of_a_gone_session_says_the_process_is_gone(
    db_url: str, tmp_path: Path
) -> None:
    """Not "cannot reach the address": the heartbeat already knows why (PLAN.md D56).

    No session runs here: the row carries an address nothing listens on, which is exactly what
    a process that was killed leaves behind.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        session_id = await store.start(
            SessionIdentity(hostname="box", os_user="u", directory=str(tmp_path), pid=1)
        )
        await store.set_address("ws://127.0.0.1:1")
        async with serve(create_app(db)) as base:
            url = base.replace("http://", "ws://") + f"/ws/sessions/{session_id}"

            # While it still beats, an unreachable address is a networking problem.
            async with ws_connect(url) as socket:
                first = json.loads(await socket.recv())
            assert first["type"] == "closed"
            assert "cannot reach" in first["message"] and "SSH tunnel" in first["message"]

            await db.execute(
                "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
                (session_id,),
            )
            async with ws_connect(url) as socket:
                second = json.loads(await socket.recv())
            assert second["type"] == "closed"
            assert "stopped saying it is running" in second["message"]
            assert "SSH tunnel" not in second["message"]
            assert "cancel" in second["message"]
    finally:
        await conn.close()
