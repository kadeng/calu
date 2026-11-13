"""The login of `calu web` in a real browser, guarded as the command guards it.

The other browser tests run the application open (no token, any loopback name). These run it
the way `calu web` does -- token, `calu.localhost`, the security headers -- because what they
check is the browser's own behaviour: which server it sends the cookie to, whether the pages
still work under the Content-Security-Policy, whether the key reaches the forms, the `fetch`
calls and the live WebSocket.
"""

from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from calu import admin, herd
from calu.db import Db, connect
from calu.setup import setup
from tests.browser.conftest import serve
from tests.db.test_web import populate
from tests.db.test_web_live import gone_quiet, running

# The web interface is an optional extra, so a checkout synced without it skips these
# tests instead of failing to collect them (`uv sync`, docs/TESTING.md).
WEB_EXTRA = "the web interface's packages are missing: uv sync"
pytest.importorskip("starlette", reason=WEB_EXTRA)
pytest.importorskip("jinja2", reason=WEB_EXTRA)

from calu.web.app import WEB_HOST_NAME, create_app  # ruff: ignore[module-import-not-at-top-of-file] (imported after the guard above)

pytestmark = [pytest.mark.browser, pytest.mark.db, pytest.mark.anyio]

TOKEN = "the-token-calu-web-printed"


def named(base: str) -> str:
    """Turn the bound address of a test server into the address `calu web` prints."""
    return base.replace("127.0.0.1", WEB_HOST_NAME)


def complaints(page: Any) -> list[str]:
    """Collect what the browser's console reports as an error from now on.

    A script or a connection that the Content-Security-Policy blocks is reported there, and
    nowhere else: the page just does less.
    """
    found: list[str] = []
    page.on("console", lambda message: found.append(message.text) if message.type == "error" else 0)
    page.on("pageerror", lambda error: found.append(str(error)))
    return found


class OtherServer:
    """A server on another port, as the supervised session could start one. It records the
    `Cookie` header of every request, which is all it can learn from a visiting browser."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.cookies: list[tuple[str, str]] = []
        """(Host header, Cookie header) of every request."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Record the request's cookies and answer with a small page."""
        if scope["type"] != "http":
            return
        headers = {name.decode(): value.decode() for name, value in scope["headers"]}
        self.cookies.append((headers.get("host", ""), headers.get("cookie", "")))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            }
        )
        await send({"type": "http.response.body", "body": b"<h1>the session's dev server</h1>"})


async def test_the_login_works_in_a_browser_and_no_other_port_can_use_it(db_url: str, page) -> None:
    """wa0, end to end: log in, let another port see what a browser sends it, replay that."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, first = await populate(db)
        _other_session, second = await populate(db)
        app = create_app(db, token=TOKEN, host_name=WEB_HOST_NAME)
        other = OtherServer()
        async with serve(app) as bound, serve(other) as other_bound:
            base = named(bound)

            # Under the bare loopback address the interface is not served, token or not.
            refused = await page.goto(f"{bound}/?token={TOKEN}")
            assert refused is not None and refused.status == 403
            assert base in await page.inner_text("body"), "it says where the interface is"

            # The printed address logs in and goes on to the page without the token.
            said = complaints(page)
            await page.goto(f"{base}/?token={TOKEN}&all=1")
            await page.wait_for_url(f"{base}/?all=1")
            assert "2 waiting" in await page.inner_text("header")
            cookies = await page.context.cookies()
            mine = [c for c in cookies if c["name"] == "calu_web_session"]
            assert len(mine) == 1 and mine[0]["httpOnly"] and mine[0]["sameSite"] == "Strict"
            assert mine[0]["domain"] == WEB_HOST_NAME, "host-only, for this name"
            assert TOKEN not in str(cookies), "the token is not kept in the browser"
            assert await page.evaluate("document.cookie") == "", "no script can read the cookie"

            # The operator opens the session's dev server in the same browser, as one does.
            await page.goto(f"{other_bound}/")
            await page.goto(other_bound.replace("127.0.0.1", "localhost") + "/")
            assert [cookie for _host, cookie in other.cookies if cookie] == [], (
                "under 127.0.0.1 and localhost the other port gets no cookie of calu web"
            )
            # Only a link that names calu's own host on the other port makes the browser send
            # the cookie there. Cookies are per host, not per port; no setting changes that.
            await page.goto(f"{named(other_bound)}/")
            captured = [cookie for host, cookie in other.cookies if WEB_HOST_NAME in host][-1]
            assert captured.startswith("calu_web_session="), "this is what the other server sees"
            # The key is in the storage of calu web's origin, port included: not readable here.
            assert await page.evaluate("localStorage.getItem('calu-key')") is None

            # The other server replays the cookie, as `curl -b` would.
            port = bound.rsplit(":", 1)[1]
            async with httpx.AsyncClient(
                base_url=bound, headers={"Host": f"{WEB_HOST_NAME}:{port}", "Cookie": captured}
            ) as thief:
                allow = {"decision": "allow", "note": "self-approved"}
                for attempt in (
                    await thief.post(f"/escalations/{first}/resolve", data=allow),
                    await thief.post(f"/api/escalations/{first}/resolve", json=allow),
                    await thief.post(f"/sessions/{session_id}/token", data={"label": "mine"}),
                ):
                    assert attempt.status_code == 401
            for escalation in (first, second):
                still = await herd.escalation(db, escalation)
                assert still is not None and still.state == "open", "nothing was decided"
            assert await herd.list_tokens(db, session_id) == []

            # The operator's own pages still work, each in its own way of sending the key.
            # A form: mint a client token.
            await page.goto(f"{base}/sessions/{session_id}")
            await page.fill("input[name=label]", "zed")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Mint a client token").click()
            assert "shown once" in await page.inner_text(".token")
            # A form: deny the first escalation.
            await page.goto(f"{base}/escalations/{first}")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Deny").click()
            denied = await herd.escalation(db, first)
            assert denied is not None and denied.state == "denied"
            # fetch: allow the second one in the triage.
            await page.goto(f"{base}/triage")
            await page.wait_for_selector("#current .escalation")
            await page.get_by_role("button", name="Allow (a)").click()
            await page.wait_for_function(
                "document.getElementById('current').textContent.includes('Nothing is waiting')"
            )
            allowed = await herd.escalation(db, second)
            assert allowed is not None and allowed.state == "allowed"
            # The settings page runs its inline script (allowed by its hash) and its own file.
            await page.goto(f"{base}/settings")
            assert await page.evaluate("document.documentElement.classList.contains('st-js')")
            # And its form, which settings.js submits, carries the key like every other form.
            await page.goto(f"{base}/settings#escalation")
            await page.fill("[name=store_failures_before_stop]", "4")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Save changes").click()
            assert "1 change saved" in await page.inner_text(".st-flash")
            assert await admin.settings(db) == {"store_failures_before_stop": 4}
            assert said == [], "nothing was blocked by the Content-Security-Policy"
    finally:
        await conn.close()


async def test_the_live_view_attaches_with_the_key(db_url: str, tmp_path: Path, page) -> None:
    """The live WebSocket carries the key as a subprotocol, and the policy lets it connect."""
    turns = [[{"text": "Looking around."}]]
    async with running(db_url, tmp_path, turns, token=TOKEN, host_name=WEB_HOST_NAME) as (
        bound,
        session_id,
        _session,
    ):
        base = named(bound)
        said = complaints(page)
        await page.goto(f"{base}/sessions/{session_id}/live?token={TOKEN}")
        await page.wait_for_url(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "look around")
        await page.get_by_role("button", name="Send (queued)").click()
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('Looking around.')"
        )
        assert said == []

        # Without the key the same browser, cookie and all, is not attached: the live view
        # decides escalations, so it counts as a change.
        await page.evaluate("localStorage.removeItem('calu-key')")
        await page.reload()
        await page.wait_for_function(
            "document.getElementById('note').textContent.includes('trying again')"
        )
        assert await page.inner_text("#state") == "not attached"


async def test_the_live_page_says_when_the_session_hangs_up(db_url: str, page) -> None:
    """wa4: the page kept saying "attached" for a session whose process had ended."""
    async with gone_quiet(db_url) as (db, session_id, hang_up), serve(create_app(db)) as base:
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        assert await page.inner_text("#state") == "running"
        hang_up.set()
        await page.wait_for_function(
            "document.getElementById('note').textContent"
            ".includes('the session closed the connection')"
        )
        assert await page.inner_text("#state") == "not attached"


async def test_the_live_page_attaches_again_when_its_own_connection_drops(
    db_url: str, page
) -> None:
    """wa4: a dropped connection to calu web (a restart, a tunnel) left the page dead."""
    sockets: list[Any] = []

    async def through(route: Any) -> None:
        """Pass the page's WebSocket on to the server and keep a handle on it."""
        route.connect_to_server()
        sockets.append(route)

    async with gone_quiet(db_url) as (db, session_id, _hang_up), serve(create_app(db)) as base:
        await page.route_web_socket("**/ws/sessions/**", through)
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await sockets[0].close()
        await page.wait_for_function(
            "document.getElementById('note').textContent.includes('trying again')"
        )
        assert await page.inner_text("#state") == "not attached"
        # After the first pause (1 s) it attaches again, with a fresh snapshot.
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        assert len(sockets) == 2
        assert await page.inner_text("#state") == "running"


async def test_the_triage_says_when_its_list_may_be_out_of_date(db_url: str, page) -> None:
    """wa3: a poll that failed left the last queue on screen without a word."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await populate(db)
        async with serve(create_app(db)) as base:
            await page.goto(f"{base}/triage")
            await page.wait_for_selector("#current .escalation")
            assert await page.inner_text("#stale") == ""

            async def down(route: Any) -> None:
                """Answer as calu web does when it cannot reach the database."""
                await route.fulfill(
                    status=503,
                    content_type="application/json",
                    body='{"ok": false, "error": "database unreachable", "message": "down"}',
                )

            await page.route("**/api/**", down)
            await page.wait_for_function(
                "document.getElementById('stale').textContent.includes('may be out of date')"
            )
            await page.wait_for_function(
                "document.getElementById('attention').textContent === 'not connected'"
            )
            # The escalation is still shown, and answering it says that it was not answered.
            messages: list[str] = []

            async def note(dialog: Any) -> None:
                """Record the alert and close it."""
                messages.append(dialog.message)
                await dialog.accept()

            page.on("dialog", note)
            await page.get_by_role("button", name="Allow (a)").click()
            with anyio.fail_after(10):
                while not messages:
                    await anyio.sleep(0.05)
            assert await page.locator("#current .escalation").count() == 1
            assert messages and "not answered" in messages[0]

            await page.unroute("**/api/**")
            await page.wait_for_function("document.getElementById('stale').textContent === ''")
            await page.wait_for_function(
                "document.getElementById('attention').textContent.includes('1 waiting')"
            )
    finally:
        await conn.close()
