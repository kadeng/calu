"""The chat pane of `calu web` (agent/records/PLAN.md D261): its login, its frame policy, what it
may ask its bridge, and one bridge over the pane's WebSocket. No database: the bridge answers
`ide.initialize` and refuses what the pane may not ask without one.
"""

import functools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("starlette", reason="the web interface's packages are missing: uv sync")
pytest.importorskip("jinja2", reason="the web interface's packages are missing: uv sync")

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from calu.acp.peer import RpcError
from calu.credentials import CredentialStore
from calu.web.app import WEB_HOST_NAME, SecurityHeaders, TokenGate
from calu.web.pane import (
    PANE_RPC_PATH,
    Panes,
    checked_handler,
    frame_ancestors,
    pane_page,
    pane_socket,
    pane_workspace,
)

pytestmark = pytest.mark.anyio

TOKEN = "pane-test-token"


class Recorder:
    """An ASGI application that answers 200 and accepts every WebSocket."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Answer the request."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def test_the_pane_page_and_its_files_need_no_login_but_nothing_else_does() -> None:
    """The page and its static files hold no data; every other page still needs the login."""
    gate = TokenGate(Recorder(), TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url=f"http://{WEB_HOST_NAME}:8800"
    ) as client:
        assert (await client.get("/pane")).status_code == 200
        assert (await client.get("/static/pane/pane.js")).status_code == 200
        assert (await client.get("/static/calu.js")).status_code == 401
        assert (await client.get("/settings")).status_code == 401
        assert (await client.get("/static/pane/../calu.js")).status_code == 401
        assert (await client.post("/pane")).status_code == 401, "only reading is free"


async def test_the_token_opens_the_pane_websocket_and_no_other() -> None:
    """A frame of another site has neither cookie nor key: it sends the token as subprotocol."""
    gate = TokenGate(Recorder(), TOKEN)

    async def outcome(path: str, subprotocols: list[str]) -> str:
        """Open a WebSocket; return the first ASGI message the gate or the app sent."""
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            """Collect what was sent."""
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": path,
            "headers": [(b"host", b"calu.localhost:8800")],
            "subprotocols": subprotocols,
        }
        await gate(scope, None, send)
        return sent[0]["type"]

    assert await outcome(PANE_RPC_PATH, ["calu", f"calu.token.{TOKEN}"]) == "websocket.accept"
    assert await outcome(PANE_RPC_PATH, ["calu", "calu.token.guessed"]) == "websocket.close"
    assert await outcome(PANE_RPC_PATH, ["calu"]) == "websocket.close"
    assert await outcome("/ws/sessions/x", ["calu", f"calu.token.{TOKEN}"]) == "websocket.close"


async def test_only_the_pane_may_be_framed_and_only_by_the_configured_origins() -> None:
    """Every other page keeps `frame-ancestors 'none'` (clickjacking)."""
    app = SecurityHeaders(Recorder(), pane_ancestors=frame_ancestors(None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{WEB_HOST_NAME}:8800"
    ) as client:
        pane = (await client.get("/pane")).headers["content-security-policy"]
        assert "frame-ancestors 'self' http://localhost:* http://127.0.0.1:*;" in pane
        other = (await client.get("/settings")).headers["content-security-policy"]
        assert "frame-ancestors 'none'" in other


async def test_a_pane_another_site_opens_waits_for_a_click() -> None:
    """A site's `window.open` of `/pane#dir=...&session=...` must not connect with the login."""
    from calu.web.pane import pane_page

    async def page(**headers: str) -> str:
        """Ask for the pane's page with these request headers.

        Args:
            **headers: Request headers, `_` for `-`.

        Returns:
            The page's text.
        """
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/pane",
            "query_string": b"",
            "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        }
        return bytes((await pane_page(Request(scope))).body).decode()

    for site in ("cross-site", "same-site"):
        asked = await page(sec_fetch_dest="document", sec_fetch_site=site)
        assert "Open the calu chat?" in asked and "/static/pane-confirm.js" in asked
        assert "/static/pane/pane.js" not in asked
    for headers in (
        {"sec_fetch_dest": "document", "sec_fetch_site": "none"},  # typed or bookmarked
        {"sec_fetch_dest": "document", "sec_fetch_site": "same-origin"},  # the confirm link
        {"sec_fetch_dest": "iframe", "sec_fetch_site": "cross-site"},  # an allowed frame
        {},  # a client without these headers
    ):
        assert "/static/pane/pane.js" in await page(**headers), headers
    script = (Path(__file__).parents[2] / "src/calu/web/static/pane-confirm.js").read_text()
    assert "/pane?confirmed=1${location.hash}" in script, "a navigation, not a fragment change"
    from calu.web.app import _public

    assert _public("/static/pane-confirm.js"), "the page shows its target also without a login"


def test_the_frame_setting_cannot_add_directives() -> None:
    """An entry with a space, ";" or "," would end the directive and start another."""
    assert frame_ancestors(["https://lab.example.org"]) == "https://lab.example.org"
    assert frame_ancestors(["https://a.org; script-src *", "'self'"]) == "'self'"
    assert frame_ancestors(["a b", "c,d"]) == "'none'"
    assert frame_ancestors([]) == "'self' http://localhost:* http://127.0.0.1:*"


def test_the_workspace_is_an_existing_absolute_directory(tmp_path: Path) -> None:
    assert pane_workspace(str(tmp_path)) == tmp_path.resolve()
    assert pane_workspace(None) == Path.cwd()
    for wrong in ("relative/dir", str(tmp_path / "missing")):
        with pytest.raises(ValueError, match="not an absolute path of a directory"):
            pane_workspace(wrong)


async def test_the_pane_may_not_write_files_end_the_session_or_attach_by_address() -> None:
    asked: list[str] = []

    async def bridge(method: str, params: dict[str, Any]) -> Any:
        """Record what reached the bridge."""
        asked.append(method)
        return {}

    handle = checked_handler(bridge)
    for method in ("ide.fs.write", "ide.fs.delete", "ide.fs.rename", "ide.checker.save"):
        with pytest.raises(RpcError, match="may not ask"):
            await handle(method, {})
    with pytest.raises(RpcError, match="may not ask"):
        await handle("session.call", {"method": "shutdown", "params": {}})
    with pytest.raises(RpcError, match="by session, not by address"):
        await handle("ide.attach", {"session": None, "address": "ws://x", "token": "t"})
    assert asked == []
    await handle("session.call", {"method": "chat.send", "params": {}})
    await handle("ide.fs.read", {"path": "a.py"})
    assert asked == ["session.call", "ide.fs.read"]


def test_one_bridge_answers_over_the_pane_websocket(tmp_path: Path) -> None:
    """The pane's WebSocket carries the bridge's JSON-RPC, one message per frame."""
    store = CredentialStore(tmp_path / "credentials.json")
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("print('hi')\n")
    app = Starlette(
        routes=[
            Route("/pane", pane_page),
            WebSocketRoute(
                PANE_RPC_PATH, functools.partial(pane_socket, store=store, panes=Panes())
            ),
        ]
    )
    with TestClient(app) as client:
        assert '<script src="/static/pane/pane.js"></script>' in client.get("/pane").text
        with client.websocket_connect(
            f"{PANE_RPC_PATH}?dir={project}", subprotocols=["calu"]
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "ide.initialize",
                        "params": {"client": "web", "client_version": "pane"},
                    }
                )
            )
            answer = json.loads(ws.receive_text())
            assert answer["id"] == 1 and answer["result"]["workspace"] == str(project.resolve())
            ws.send_text(
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ide.fs.write", "params": {}})
            )
            refused = json.loads(ws.receive_text())
            assert refused["id"] == 2 and "may not ask" in refused["error"]["message"]
            ws.send_text(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 3, "method": "ide.fs.read", "params": {"path": "a.py"}}
                )
            )
            read = json.loads(ws.receive_text())
            assert read["id"] == 3 and read["result"]["eof"] is True


def test_a_wrong_directory_closes_the_pane(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    app = Starlette(
        routes=[
            WebSocketRoute(
                PANE_RPC_PATH, functools.partial(pane_socket, store=store, panes=Panes())
            )
        ]
    )
    with TestClient(app) as client:
        with client.websocket_connect(f"{PANE_RPC_PATH}?dir=relative") as ws:
            message = ws.receive()
            assert message["type"] == "websocket.close" and message["code"] == 1008
