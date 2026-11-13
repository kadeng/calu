"""The guards in front of the web interface, without a database: login, key, host name, headers.

The pages are tested against a database (`tests/db/test_web.py`) and in a browser
(`tests/browser/test_web_login.py`). Here a three-line application stands behind the
middlewares, so each rule of `TokenGate`, `LocalOnly` and `SecurityHeaders` is checked on its
own.
"""

import base64
import hashlib
import re
from typing import Any

import httpx
import pytest

# The web interface is an optional extra, so a checkout synced without it skips these tests.
pytest.importorskip("starlette", reason="the web interface's packages are missing: uv sync")
pytest.importorskip("jinja2", reason="the web interface's packages are missing: uv sync")

from calu.web.app import (
    CONTENT_SECURITY_POLICY,
    HERE,
    WEB_HOST_NAME,
    LocalOnly,
    SecurityHeaders,
    TokenGate,
)

pytestmark = pytest.mark.anyio

TOKEN = "s3cret-token"


class Recorder:
    """An ASGI application that records what reached it and answers 200."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.seen: list[tuple[str, str, bytes]] = []
        """(scope type, method or "", body) of every request that passed the guards."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Record the request, read its body, answer 200 (or accept the WebSocket)."""
        if scope["type"] == "websocket":
            self.seen.append(("websocket", "", b""))
            await send({"type": "websocket.accept"})
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.seen.append(("http", scope["method"], body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def gated() -> tuple[Recorder, TokenGate, httpx.AsyncClient]:
    """A recorder behind a `TokenGate`, and a client without any login."""
    inner = Recorder()
    gate = TokenGate(inner, TOKEN)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url=f"http://{WEB_HOST_NAME}:8800"
    )
    return inner, gate, client


async def login(client: httpx.AsyncClient) -> tuple[str, str]:
    """Log the client in with the token. Returns (the cookie's value, the key on the page)."""
    page = await client.get(f"/?token={TOKEN}")
    assert page.status_code == 200
    cookie = page.headers["set-cookie"]
    value = cookie.split(";")[0].split("=", 1)[1]
    key = re.search(r'name="calu-key" content="([^"]+)"', page.text)
    assert key is not None, "the page hands the key to login.js"
    return value, key.group(1)


async def test_the_login_hands_out_a_session_cookie_and_a_key_but_never_the_token() -> None:
    """wa0: the cookie held the token itself, so whoever saw the cookie held the login."""
    inner, _gate, client = gated()
    async with client:
        page = await client.get(f"/sessions/abc?token={TOKEN}&detail=full")
        assert page.status_code == 200
        cookie = page.headers["set-cookie"]
        assert cookie.startswith("calu_web_session=")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Domain" not in cookie, "host-only: no other name gets it"
        assert TOKEN not in cookie
        assert TOKEN not in page.text, "the page goes on to the address without the token"
        assert 'name="calu-onward" content="/sessions/abc?detail=full"' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert inner.seen == [], "the login page is the gate's own answer"
        # Two logins are two browsers: each gets its own cookie and its own key.
        again = await client.get(f"/?token={TOKEN}")
        assert again.headers["set-cookie"] != cookie


async def test_the_login_never_goes_on_to_another_host() -> None:
    """`//evil.example/` as a path is an address on another host once it is a link."""
    _inner, _gate, client = gated()
    async with client:
        page = await client.get(f"http://calu.localhost:8800//evil.example/?token={TOKEN}")
        assert 'name="calu-onward" content="/evil.example/"' in page.text


async def test_the_cookie_alone_reads_but_changes_nothing() -> None:
    """wa0: a server on another port of the host name receives the cookie, and only the cookie.

    The cookie is all such a server can replay. It must not be enough to resolve an
    escalation, save a setting or mint a client token -- which are all POSTs.
    """
    inner, _gate, client = gated()
    async with client:
        cookie, key = await login(client)
    stolen = httpx.AsyncClient(
        transport=client._transport,
        base_url=f"http://{WEB_HOST_NAME}:8800",
        cookies={"calu_web_session": cookie},
    )
    async with stolen:
        assert (await stolen.get("/")).status_code == 200
        form = {"decision": "allow", "note": "self-approved"}
        for refused in (
            await stolen.post("/escalations/1/resolve", data=form),
            await stolen.post("/escalations/1/resolve", data={**form, "_key": "guessed"}),
            await stolen.post("/api/escalations/1/resolve", json=form),
            await stolen.post("/api/escalations/1/resolve", json={**form, "_key": key}),
            await stolen.post("/x", content=b"_key=" + key.encode()),  # not a form
            await stolen.post("/x", headers={"X-Calu-Key": "guessed"}),
            await stolen.put("/x"),
            await stolen.delete("/x"),
            await stolen.patch("/x"),
        ):
            assert refused.status_code == 401, refused.request
        assert [entry[:2] for entry in inner.seen] == [("http", "GET")], "no POST got through"

        # The interface's own pages carry the key: as a header (fetch) or as a form field.
        assert (await stolen.post("/x", headers={"X-Calu-Key": key})).status_code == 200
        sent = await stolen.post("/x", data={**form, "_key": key})
        assert sent.status_code == 200
        # The application still reads the whole form, although the gate read it first.
        assert inner.seen[-1][2] in (f"decision=allow&note=self-approved&_key={key}".encode(),)


async def test_without_a_login_nothing_passes() -> None:
    inner, _gate, client = gated()
    async with client:
        assert (await client.get("/")).status_code == 401
        assert "printed" in (await client.get("/")).text, "says how to get in"
        assert (await client.get("/?token=wrong")).status_code == 401
        assert (await client.get("/?token=%C3%A4bc")).status_code == 401, "D168: not a 500"
        assert (await client.post(f"/?token={TOKEN}")).status_code == 401, "a login is a GET"
        client.cookies.set("calu_web_session", "made-up")
        assert (await client.get("/")).status_code == 401
        client.cookies.set("calu_web_session", TOKEN)
        assert (await client.get("/")).status_code == 401, "the token is not a cookie value"
    assert inner.seen == []


async def test_a_form_too_large_to_search_for_the_key_is_refused(monkeypatch) -> None:
    import calu.web.app as app

    monkeypatch.setattr(app, "MAX_FORM_BYTES", 64)
    inner, _gate, client = gated()
    async with client:
        _cookie, key = await login(client)
        big = await client.post("/x", data={"text": "x" * 100, "_key": key})
        assert big.status_code == 413
    assert inner.seen == []


async def test_the_live_websocket_needs_the_cookie_and_the_key() -> None:
    """The live view decides escalations too, so a WebSocket counts as a change."""
    inner, gate, client = gated()
    async with client:
        cookie, key = await login(client)

    async def outcome(cookies: str, subprotocols: list[str]) -> str:
        """Open a WebSocket with these credentials; return the first ASGI message sent."""
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            """Collect what the gate or the application sends."""
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/ws/sessions/x",
            "headers": [(b"host", b"calu.localhost:8800"), (b"cookie", cookies.encode())],
            "subprotocols": subprotocols,
        }
        await gate(scope, None, send)
        return sent[0]["type"]

    own = f"calu_web_session={cookie}"
    assert await outcome(own, []) == "websocket.close", "the cookie alone"
    assert await outcome(own, ["calu"]) == "websocket.close"
    assert await outcome(own, ["calu", "calu.key.guessed"]) == "websocket.close"
    assert await outcome("", ["calu", f"calu.key.{key}"]) == "websocket.close", "the key alone"
    assert inner.seen == []
    assert await outcome(own, ["calu", f"calu.key.{key}"]) == "websocket.accept"


async def test_only_so_many_logins_are_remembered(monkeypatch) -> None:
    import calu.web.app as app

    monkeypatch.setattr(app, "MAX_BROWSERS", 2)
    _inner, gate, client = gated()
    async with client:
        first, _key = await login(client)
        await login(client)
        await login(client)
        assert len(gate.browsers) == 2
        client.cookies.clear()
        client.cookies.set("calu_web_session", first)
        assert (await client.get("/")).status_code == 401, "the oldest login was forgotten"


def scope(kind: str, **headers: str) -> dict[str, Any]:
    """An ASGI scope with these headers."""
    return {
        "type": kind,
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


def test_calu_web_answers_to_its_own_host_name_only() -> None:
    """wa0: under `127.0.0.1` or `localhost` the browser shares the cookie with every port.

    A development server of the supervised session is opened as `127.0.0.1:3000` or
    `localhost:3000`. The interface is not served under those names, so the browser has no
    cookie of it to send there.
    """
    guard = LocalOnly(app=None, host_name=WEB_HOST_NAME)
    assert guard.refusal(scope("http", host="calu.localhost:8800")) is None
    assert guard.refusal(scope("http", host="CALU.localhost:8800")) is None
    for other in ("127.0.0.1:8800", "localhost:8800", "[::1]:8800", "evil.example:8800"):
        refused = guard.refusal(scope("http", host=other))
        assert refused, other
        assert "http://calu.localhost:8800/" in refused, "says where the interface is"
    assert "http://calu.localhost/" in (guard.refusal(scope("http", host="127.0.0.1")) or "")
    # Its own origin is the only one that may change something.
    own = scope("websocket", host="calu.localhost:8800", origin="http://calu.localhost:8800")
    assert guard.refusal(own) is None
    other_port = scope("websocket", host="calu.localhost:8800", origin="http://calu.localhost:3000")
    assert guard.refusal(other_port), "another port of the same name is another origin"


async def test_every_response_carries_the_security_headers() -> None:
    """wa2: even the refusals, so no page of the interface runs a script that was injected."""
    app = SecurityHeaders(LocalOnly(TokenGate(Recorder(), TOKEN), host_name=WEB_HOST_NAME))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://calu.localhost:8800"
    ) as client:
        for answer in (
            await client.get("/"),  # 401 of the gate
            await client.get("/", headers={"Host": "evil.example"}),  # 403 of the host check
            await client.get(f"/?token={TOKEN}"),  # the login page
        ):
            policy = answer.headers["content-security-policy"]
            assert "default-src 'self'" in policy
            assert "frame-ancestors 'none'" in policy and "base-uri 'none'" in policy
            assert "form-action 'self'" in policy
            assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
            assert answer.headers["x-content-type-options"] == "nosniff"
            assert answer.headers["referrer-policy"] == "same-origin"
        # The live WebSocket of this very address is allowed, and a hostile Host is not copied.
        assert (
            "ws://calu.localhost:8800" in (await client.get("/")).headers["content-security-policy"]
        )
        odd = await client.get("/", headers={"Host": "x; script-src *"})
        assert "script-src *" not in odd.headers["content-security-policy"]


def test_the_policy_allows_exactly_the_inline_scripts_of_the_templates() -> None:
    """The hashes are of the template source, so an inline script must not be templated.

    A `{{ ... }}` inside an inline script would change its text per request, the hash would not
    match and the browser would not run it. Such a script belongs in `/static`, with its values
    in `data-` attributes.
    """
    scripts = set()
    for template in (HERE / "templates").glob("*.html"):
        source = template.read_text(encoding="utf-8")
        for attributes, script in re.findall(r"<script([^>]*)>(.*?)</script>", source, re.S):
            if "src=" in attributes:
                assert not script.strip(), f"{template.name}: a script with src has no body"
                continue
            assert not attributes.strip(), f"{template.name}: inline script with attributes"
            assert "{{" not in script and "{%" not in script, template.name
            scripts.add(script)
    assert scripts, "the templates still carry inline scripts; drop this test when they do not"
    for script in scripts:
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in CONTENT_SECURITY_POLICY
    # No inline event handlers or styles either: the policy would silently drop them.
    for template in (HERE / "templates").glob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", source), f"{template.name}: inline handler"
        assert not re.search(r"\sstyle\s*=", source), f"{template.name}: inline style"
