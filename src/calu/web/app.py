"""The web interface: many sessions at once, their history, and what steers their checkers.

    calu web                # http://calu.localhost:8800/?token=..., needs the `admin` credential

It is the herd client of PLAN.md §6.2: it works on the database, so it sees the sessions of
every host without a process of its own on them, and it holds the `admin` credential, so it can
also change what steers the checkers. For the live view of one session it is a client of that
session as well (D34): it mints a client token, connects to the address the session recorded and
relays the event stream to the browser, so the browser needs no token and the session needs no
new protocol.

Pages:

    /                     every session of every host, newest first, with its open escalations
    /sessions/{id}        one session: its turns and tool calls, and how to attach to it
    /sessions/{id}/live   the live view: the stream, the queue, stop, steer, the subagents
    /sessions/{id}/summary  the folded conversation the checkers read, editable
    /escalations          what waits for a human, with allow and deny
    /triage               one escalation at a time, with the queue behind it
    /api/sessions         the fleet as JSON, for the jump palette and the attention badge
    /escalations/{id}     what the checker saw when it escalated the step
    /checkers             what the checkers decided and where a human disagreed
    /settings             everything that steers a session: the configuration, the checker
                          instructions and the allow and deny lists, per scope

**The login is the token `calu web` prints** (`TokenGate`, D161); there are no user accounts
(PLAN.md Q4 is open), so the server refuses to bind anything but the loopback interface: reach
it from another machine through an SSH tunnel

    ssh -L 8800:127.0.0.1:8800 the-host      # then open the printed address at home

or put a TLS proxy with its own authentication in front of it. Everything the pages can do, the
`admin` credential can do, including changing the rules a session is judged by.

Who may do what (the three middlewares, outermost first):

    SecurityHeaders   every response: Content-Security-Policy, nosniff, no referrer elsewhere
    LocalOnly         the Host must be `calu.localhost`; a changing request must come from this
                      interface's own pages
    TokenGate         reading needs the browser's session cookie; changing anything needs the
                      cookie and the browser's key, which no other port can read
"""

import base64
import hashlib
import ipaddress
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import anyio
import psycopg
from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from calu import __version__, admin, herd
from calu.db import Db
from calu.protocol.client import RequestFailed, connect
from calu.web import git
from calu.web import settings as web_settings

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def _prose(text: str) -> Markup:
    """Render a sentence of help text, with `code` spans as code.

    Args:
        text: Plain text in which backticks mark code, as the configuration docstrings use.

    Returns:
        The escaped text with each backtick pair turned into a `<code>` element.
    """
    parts = str(text).split("`")
    if len(parts) % 2 == 0:  # an unpaired backtick: show the text as it is
        return escape(text)
    return Markup("").join(
        Markup("<code>{}</code>").format(part) if index % 2 else escape(part)
        for index, part in enumerate(parts)
    )


TEMPLATES.env.filters["prose"] = _prose
"""The page templates; they ship with the package."""

MAX_STEPS = 300
"""Steps one session page shows (the newest ones)."""

MAX_TURNS = 50
"""Turns one session page shows (the newest ones)."""

LIVE_METHODS = frozenset(
    {
        "state.get",
        "session.start",
        "session.stop",
        "interrupt",
        "chat.send",
        "escalation.decide",
        "escalation.context",
        "context.get",
        "checkers.get",
        "session.history",
        "session.list",
        "auto_escalate.set",
        "honesty.clear",
    }
)
"""What a browser may ask a session to do. `shutdown` is deliberately absent: it would end the
calu process, which is not something a page should do by accident."""

MODELS = ("", "fable", "opus", "sonnet", "haiku")
"""Claude Code's model aliases for the start dropdown; "" keeps what the configuration says."""

EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
"""Reasoning efforts for the start dropdown; "" keeps what the configuration says."""

GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("needs_you", "Needs you", frozenset({"escalated"})),
    ("gone", "Gone (no heartbeat)", frozenset()),
    ("working", "Working", frozenset({"running", "checking", "tool_call", "starting"})),
    ("idle", "Idle", frozenset({"idle"})),
    ("ended", "Ended", frozenset({"stopped", "failed"})),
)
"""How the fleet page groups the sessions, in the order it shows them (PLAN.md D33).

Every agent UI that manages many sessions leads with the ones that cannot go on without a
human — Claude Code's agent view groups into "Needs input", "Working", "Completed", Orca into
"Needs You", "Working", "Done", "Idle". calu's escalations are exactly that, so a session that
waits comes first, and a session with an open escalation counts as waiting whatever its state
says. "Gone" holds the sessions whose process stopped saying it is there (D56): their row still
claims a state, and nothing an operator does for them has any effect, so they are kept out of
"needs you" and named for what they are.
"""

DETAIL_LEVELS = {
    "summary": "Narrated summary (what the checkers see)",
    "compact": "Compact (answers and one line per tool call)",
    "detailed": "Detailed (with Claude's thinking, narrated)",
    "full": "Full (tool inputs, results and checker verdicts)",
}
"""The levels of detail of the chat log, from least to most (the owner's wording).

The four differ in what they show of each turn:

    summary    `TurnRecord.render()`: exactly the Markdown a checker reads
    compact    the user's messages, Claude's answer, one line per step
    detailed   ... plus the narration of each step and the checker's reason
    full       ... plus the tool input, the result summary and the write set

Claude's **raw** thinking is nowhere in this list, because it is not recorded: the database
keeps the narration in indirect speech (D3, and the checkers never see Claude's own words). The
live view of a session shows the real thinking as it streams.
"""


class NotLocal(Exception):
    """The server was asked to bind an address that is not the loopback interface."""


def check_host(host: str) -> None:
    """Refuse to serve anything but the loopback interface (PLAN.md Q4).

    Args:
        host: The address `calu web` was asked to bind.

    Raises:
        NotLocal: If the address is not a loopback address. The web interface has no login yet,
            so binding it to a reachable interface would hand everything the `admin` credential
            can do to whoever finds the port.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise NotLocal(
            f"{host!r} is not an IP address; the web interface serves 127.0.0.1 only, because it "
            "has no login yet (PLAN.md Q4)"
        ) from exc
    if not address.is_loopback:
        raise NotLocal(
            f"the web interface has no login yet, so it refuses to serve {host}. Reach it from "
            "another machine through an SSH tunnel (ssh -L 8800:127.0.0.1:8800 <host>) or put a "
            "TLS proxy with its own authentication in front of it (PLAN.md Q4)."
        )


def create_app(
    db: Db, git_url_template: str = "", token: str | None = None, host_name: str = ""
) -> Starlette:
    """Build the web application on an open database connection.

    The connection is used by every request, and `calu.db.Db` serializes the statements, so
    concurrent requests are safe but not parallel. That is deliberate: one operator's overview
    does not need a pool, and a session's credentials are not available here anyway. `calu web`
    passes a `calu.web.database.ReopeningDb`, which replaces a connection the database closed;
    a database that cannot be reached is answered with status 503 (`_database_down`).

    Args:
        db: The database connection (owner or `calu_operator` rights).
        git_url_template: `web.git_url_template` for commit links to a git browser that is
            neither GitHub, GitLab nor Bitbucket ("" guesses from the project's remote).
        token: The login token (`TokenGate`, D161). `calu web` always passes one; None leaves
            the gate out, for in-process tests.
        host_name: The only host name a request may carry (`LocalOnly`). `calu web` always
            passes `WEB_HOST_NAME`; "" accepts every loopback name, for in-process tests.

    Returns:
        The Starlette application, ready for uvicorn.
    """
    state = _State(db=db, git_url_template=git_url_template)
    # A session id is a UUID, and the routes say so: any other text is answered with a plain
    # 404 by the router and never reaches a query (which would fail on the cast) or a page.
    routes = [
        Route("/", _page(fleet, state), name="fleet"),
        Route("/sessions/{session_id:uuid}", _page(session_page, state), name="session"),
        Route(
            "/sessions/{session_id:uuid}/token", _page(mint, state), methods=["POST"], name="mint"
        ),
        Route("/sessions/{session_id:uuid}/live", _page(live_page, state), name="live"),
        Route(
            "/sessions/{session_id:uuid}/summary",
            _page(summary_page, state),
            methods=["GET", "POST"],
            name="summary",
        ),
        WebSocketRoute(
            "/ws/sessions/{session_id:uuid}", _socket(live_socket, state), name="live_ws"
        ),
        Route("/escalations", _page(escalations, state), name="escalations"),
        Route("/triage", _page(triage, state), name="triage"),
        Route("/api/escalations", _page(waiting_json, state), name="waiting_json"),
        Route("/api/sessions", _page(sessions_json, state), name="sessions_json"),
        Route(
            "/api/escalations/{escalation_id:int}/resolve",
            _page(resolve_json, state),
            methods=["POST"],
            name="resolve_json",
        ),
        Route("/escalations/{escalation_id:int}", _page(escalation_page, state), name="escalation"),
        Route(
            "/escalations/{escalation_id:int}/resolve",
            _page(resolve, state),
            methods=["POST"],
            name="resolve",
        ),
        Route("/checkers", _page(checkers, state), name="checkers"),
        Route("/checkers/{name}", _page(checker_redirect, state), name="checker"),
        Route("/rules", _page(rules_redirect, state), name="rules"),
        Route("/settings", _page(settings_page, state), methods=["GET", "POST"], name="settings"),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    # Outermost first: the headers go on every response, the refusals of the two guards
    # included.
    middleware = [
        Middleware(SecurityHeaders),
        Middleware(LocalOnly, host_name=host_name),
    ]
    if token is not None:
        middleware.append(Middleware(TokenGate, token=token))
    return Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={
            psycopg.OperationalError: _database_down,
            TimeoutError: _database_down,
        },
    )


WEB_HOST_NAME = "calu.localhost"
"""The host name `calu web` is opened under (D161, finding wa0).

Browsers resolve every name under `.localhost` to the loopback interface themselves, so the
name needs no entry in a hosts file. It exists because a cookie belongs to a host name and not
to a port: under `127.0.0.1` or `localhost` the browser would send the interface's cookie to
every development server the supervised session starts on another port. No such server is
opened as `calu.localhost` unless something makes the operator do so, and even then the cookie
alone changes nothing (`TokenGate`).
"""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1", WEB_HOST_NAME})
"""Host names an application built without `host_name` accepts (in-process tests)."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that change nothing, and are therefore not checked for their origin."""

SESSION_COOKIE = "calu_web_session"
"""Cookie that names the browser's login to `calu web` (D161). It is not the token."""

KEY_HEADER = b"x-calu-key"
"""Request header in which a page's `fetch` sends the browser's key."""

KEY_FIELD = "_key"
"""Form field in which a page's form sends the browser's key (`calu.js` adds it)."""

KEY_PROTOCOL = "calu.key."
"""Prefix of the WebSocket subprotocol in which `live.js` sends the browser's key."""

LIVE_PROTOCOL = "calu"
"""The WebSocket subprotocol the live relay answers with when the browser offered any."""

MAX_FORM_BYTES = 8 * 1024 * 1024
"""Largest form body `TokenGate` reads to find the key; a larger one is refused with 413."""

MAX_BROWSERS = 64
"""Browser logins `TokenGate` remembers; the oldest is forgotten when one more logs in."""


def _inline_script_hashes() -> list[str]:
    """Hash the inline scripts of the page templates, for the Content-Security-Policy.

    A few templates carry a short inline `<script>` (the theme is applied before the
    stylesheet loads, some pages reload themselves). The policy allows exactly these by their
    SHA-256 hash, so a script that reaches a page any other way does not run. The hash is of
    the template source, which is what the browser receives as long as the script contains no
    template expression (`tests/unit/test_web_methods.py` checks that).

    Returns:
        One `'sha256-...'` source expression per distinct inline script, sorted.
    """
    found: set[str] = set()
    for template in sorted((HERE / "templates").glob("*.html")):
        source = template.read_text(encoding="utf-8")
        for script in re.findall(r"<script>(.*?)</script>", source, flags=re.DOTALL):
            digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
            found.add(f"'sha256-{digest}'")
    return sorted(found)


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' {' '.join(_inline_script_hashes())}; "
    "connect-src 'self' ws://{host} wss://{host}; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
"""The policy of every response; `{host}` is the request's Host, for the live WebSocket.

Scripts run only from `/static` and from the templates' own inline scripts, so text that
reaches a page unescaped is not executed (finding wa2); the pages cannot be framed, and a form
cannot post anywhere else.
"""


class SecurityHeaders:
    """Add the browser security headers to every HTTP response (finding wa2).

    Content-Security-Policy   `CONTENT_SECURITY_POLICY`
    X-Content-Type-Options    nosniff
    Referrer-Policy           same-origin (no other site learns the address of a page)
    """

    def __init__(self, app: Any) -> None:
        """Wrap the application.

        Args:
            app: The ASGI application.
        """
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Pass the request on and add the headers to the start of its response.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            host = WEB_HOST_NAME  # never copy an odd Host into a header
        policy = CONTENT_SECURITY_POLICY.replace("{host}", host)
        extra = [
            (b"content-security-policy", policy.encode("latin-1")),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
        ]

        async def send_with_headers(message: Any) -> None:
            """Forward one ASGI message, with the headers added to a response start.

            Args:
                message: The ASGI message.
            """
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _header(scope: Any, name: bytes) -> str:
    """Read one request header from an ASGI scope.

    Args:
        scope: The ASGI scope.
        name: The header's name in lower case.

    Returns:
        The value of the first header of that name, or "" if there is none.
    """
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


class TokenGate:
    """Let through only what a logged-in browser of the operator sends (PLAN.md D161).

    `LocalOnly` keeps other websites out, but not other processes of this user: Claude runs
    as the same user, and a `curl -X POST .../escalations/1/resolve` from its Bash would decide
    its own step. So the interface has a login of the kind a local web tool usually has: a
    random token, printed with the address at start (`http://calu.localhost:8800/?token=...`).

    A login gives the browser two secrets, both random and both known only to this process
    and that browser:

        session id   in an HttpOnly, SameSite=Strict cookie. The browser sends a cookie to
                     every port of the host name, so a server on another port can see it.
        key          in the page's `localStorage`, which belongs to one origin, port
                     included. No other port can read it.

    The cookie is therefore only enough to read, and everything that changes something needs
    the key as well:

        GET /...?token=<right>                 ->  200: a page that stores the key and goes on
                                                   to the address without the token; sets the
                                                   cookie
        GET, HEAD, OPTIONS with the cookie     ->  passes
        other methods: cookie and key          ->  passes; the key is the header `X-Calu-Key`
                                                   or the form field `_key`
        WebSocket: cookie and key              ->  passes; the key is the offered subprotocol
                                                   `calu.key.<key>`
        anything else                          ->  401 (HTTP) or close code 1008 (WebSocket)

    The token is never stored in the browser. It and the logins live only in this process; a
    restart prints a new token and forgets every login.
    """

    def __init__(self, app: Any, token: str) -> None:
        """Wrap the application.

        Args:
            app: The ASGI application.
            token: The secret a browser logs in with, as `?token=`.
        """
        self.app = app
        self.token = token
        self.browsers: dict[str, str] = {}
        """Session id -> key, one entry per login, oldest first (at most `MAX_BROWSERS`)."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Pass a request on, log a browser in, or refuse the request.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        key = self._key_of(scope)
        if scope["type"] == "websocket":
            offered = [
                name[len(KEY_PROTOCOL) :]
                for name in scope.get("subprotocols", [])
                if name.startswith(KEY_PROTOCOL)
            ]
            if key is not None and any(_same(given, key) for given in offered):
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        method = scope.get("method", "GET")
        if method == "GET" and await self._login(scope, send):
            return
        if key is None:
            await _plain(
                send,
                401,
                "calu web: open the address that `calu web` printed when it started; it "
                "carries the login token (once per browser).",
            )
            return
        if method in SAFE_METHODS or _same(_header(scope, KEY_HEADER), key):
            await self.app(scope, receive, send)
            return
        # A form of the interface's own pages: the key is a field of the body.
        body = await _form_body(scope, receive)
        if body is None:
            await _plain(send, 413, "calu web: the form is too large.")
            return
        fields = parse_qs(body.decode("latin-1"), keep_blank_values=True)
        if not any(_same(given, key) for given in fields.get(KEY_FIELD, [])):
            await _plain(
                send,
                401,
                "calu web: this request did not carry the key of your browser's login, so "
                "nothing was changed. Open the address that `calu web` printed once more "
                "(the pages need JavaScript and the browser's local storage).",
            )
            return
        await self.app(scope, _replay(body, receive), send)

    async def _login(self, scope: Any, send: Any) -> bool:
        """Log the browser in if the request carries the right `?token=`.

        Side effects: remembers a new session id and key, forgetting the oldest login beyond
        `MAX_BROWSERS`, and answers the request with the page that stores the key.

        Args:
            scope: The ASGI scope of a GET request.
            send: The ASGI send channel.

        Returns:
            True if the token was right and the response was sent; False if the request
            carries no token or a wrong one, and nothing was sent.
        """
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        offered = (query.get("token") or [""])[0]
        if not offered or not _same(offered, self.token):
            return False
        session_id, key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.browsers[session_id] = key
        while len(self.browsers) > MAX_BROWSERS:
            del self.browsers[next(iter(self.browsers))]
        rest = urlencode([(k, v) for k, vs in query.items() if k != "token" for v in vs])
        # One leading slash only: "//host/" would be read as an address on another host.
        onward = "/" + str(scope.get("path", "/")).lstrip("/") + (f"?{rest}" if rest else "")
        page = TEMPLATES.get_template("login.html").render(key=key, onward=onward)
        cookie = f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"set-cookie", cookie.encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": page.encode("utf-8")})
        return True

    def _key_of(self, scope: Any) -> str | None:
        """Find the login the request's cookie names.

        Args:
            scope: The ASGI scope.

        Returns:
            The key of that login, or None if the request carries no `SESSION_COOKIE` of a
            login this process knows.
        """
        for name, value in scope.get("headers", []):
            if name.lower() != b"cookie":
                continue
            for part in value.decode("latin-1").split(";"):
                cookie, _, given = part.strip().partition("=")
                if cookie != SESSION_COOKIE:
                    continue
                for session_id, key in self.browsers.items():
                    if _same(given, session_id):
                        return key
        return None


async def _plain(send: Any, status: int, text: str) -> None:
    """Answer an HTTP request with one line of plain text.

    Args:
        send: The ASGI send channel.
        status: The HTTP status.
        text: The line, without its newline.
    """
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": f"{text}\n".encode()})


async def _form_body(scope: Any, receive: Any) -> bytes | None:
    """Read the whole body of a form request, so that the key can be looked for in it.

    Args:
        scope: The ASGI scope of the request.
        receive: The ASGI receive channel; it is read to the end of the body.

    Returns:
        The body; b"" if the request is not an `application/x-www-form-urlencoded` form or the
        client went away; None if the body is larger than `MAX_FORM_BYTES`.
    """
    kind = _header(scope, b"content-type").split(";")[0].strip().lower()
    if kind != "application/x-www-form-urlencoded":
        return b""
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b""
        body += message.get("body", b"")
        if len(body) > MAX_FORM_BYTES:
            return None
        if not message.get("more_body"):
            return body


def _replay(body: bytes, receive: Any) -> Callable[[], Awaitable[Any]]:
    """Build a receive channel that hands out a body that was already read.

    Args:
        body: The request body.
        receive: The original channel, which is used again once the body was handed out (it
            then reports the disconnect).

    Returns:
        The ASGI receive channel for the application.
    """
    sent = False

    async def replayed() -> Any:
        """Hand out the body once, then whatever the original channel says.

        Returns:
            The next ASGI message.
        """
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replayed


def _same(given: str, token: str) -> bool:
    """Compare a token in constant time; any text, ASCII or not, gives an answer (D168).

    `secrets.compare_digest` raises TypeError on non-ASCII `str`, which turned a stray `?token=`
    into a 500 instead of a 401.

    Args:
        given: What the request carried.
        token: The real token.

    Returns:
        True if they are equal.
    """
    return secrets.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8"))


class LocalOnly:
    """Refuse requests that a page from another site could have caused (PLAN.md D136).

    The interface serves loopback only -- but a browser on this machine will send requests to
    a loopback address on behalf of **any** site it has open, with the login's cookie where
    the site is another port of this host name. Without this,
    a page anywhere could submit "allow" for escalations 1 to 100 (a form POST is sent
    cross-site without asking, and the ids are small integers), rewrite the settings or the
    checker instructions, or open the live WebSocket and decide steps through it; a page whose
    domain re-resolves to 127.0.0.1 (DNS rebinding) could also read everything shown here.

    Two checks, on every request and WebSocket handshake:

    - The `Host` header must name this interface: exactly `host_name` when one is given
      (`calu web` gives `WEB_HOST_NAME`), a loopback name otherwise. A rebinding page sends its
      own domain there, and a bookmark of `127.0.0.1` is told the right address.
    - A request that can change something -- every method but GET, HEAD and OPTIONS, and every
      WebSocket -- must come from this interface's own pages: `Sec-Fetch-Site` may not say
      `cross-site` or `same-site` (another port on localhost is "same-site"), and an `Origin`,
      when sent, must be this server's own. Browsers send both on such requests; a client that
      sends neither (curl, `calu`'s own tests) is not a browser acting for someone else.
    """

    def __init__(self, app: Any, extra_hosts: Collection[str] = (), host_name: str = "") -> None:
        """Wrap the application.

        Args:
            app: The ASGI application.
            extra_hosts: Further host names to accept, for a test client whose base URL is not
                a loopback name.
            host_name: The only host name to accept, next to `extra_hosts`; "" accepts every
                name of `LOOPBACK_HOSTS`.
        """
        self.app = app
        self.host_name = host_name
        self.hosts = (frozenset({host_name}) if host_name else LOOPBACK_HOSTS) | set(extra_hosts)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Pass a request on, or refuse it with 403 (HTTP) or close code 1008 (WebSocket).

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refused = self.refusal(scope)
        if refused is None:
            await self.app(scope, receive, send)
            return
        log.warning("refused a request to %s: %s", scope.get("path"), refused)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = f"refused: {refused}\n".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def refusal(self, scope: Any) -> str | None:
        """Say why a request must be refused, or None when it may pass.

        Args:
            scope: The ASGI scope of the request.

        Returns:
            A short reason for the log and the response, or None.
        """
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        host = headers.get("host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name.lower() not in self.hosts:
            if self.host_name:
                port = host.rsplit(":", 1)[1] if ":" in host and not host.endswith("]") else ""
                where = self.host_name + (f":{port}" if port.isdigit() else "")
                return (
                    f"host {host!r} is not the name of this interface; open http://{where}/ "
                    "(with the ?token=... that `calu web` printed, the first time)"
                )
            return f"host {host!r} is not this machine (the interface serves loopback only)"
        changes = scope["type"] == "websocket" or scope.get("method") not in SAFE_METHODS
        if not changes:
            return None
        if headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return "the request came from another site"
        origin = headers.get("origin")
        if origin is not None:
            parsed = urlparse(origin)
            if origin == "null" or parsed.netloc != host:
                return f"origin {origin!r} is not this interface"
        return None


class _State:
    """What every page needs: the connection and the configuration of the links."""

    def __init__(self, db: Db, git_url_template: str = "") -> None:
        """Keep the connection and the link template.

        Args:
            db: The database connection.
            git_url_template: Template for commit links ("" guesses from the remote).
        """
        self.db = db
        self.git_url_template = git_url_template
        self.live_tokens: dict[str, str] = {}
        """Session id -> the token this server uses for its own live view.

        One token per session and per run of the server, not one per browser connection: minting
        is a database write, and revoking it when a browser disconnects would be a database write
        in a cancelled teardown path, which is exactly what D19 says not to do (a statement
        cancelled halfway leaves the connection unusable for everything else). The token is
        labelled "calu web (live view)", so `calu token list` shows it and an operator can revoke
        it. A refusal is not an error for the reader: `live_socket` then drops the cached
        token, mints a fresh one and connects again (D110).
        """


Page = Callable[[Request, _State], Awaitable[Response]]
"""A page handler: it gets the request and the shared state."""


def _page(handler: Page, state: _State) -> Callable[[Request], Awaitable[Response]]:
    """Bind a page handler to the shared state.

    Args:
        handler: The handler.
        state: The state to pass to it.

    Returns:
        The endpoint Starlette calls.
    """

    async def endpoint(request: Request) -> Response:
        """Call the handler with the state.

        Args:
            request: The request.

        Returns:
            Its response.
        """
        return await handler(request, state)

    return endpoint


Socket = Callable[[WebSocket, _State], Awaitable[None]]
"""A WebSocket handler: it gets the connection and the shared state."""


def _socket(handler: Socket, state: _State) -> Callable[[WebSocket], Awaitable[None]]:
    """Bind a WebSocket handler to the shared state.

    Args:
        handler: The handler.
        state: The state to pass to it.

    Returns:
        The endpoint Starlette calls.
    """

    async def endpoint(websocket: WebSocket) -> None:
        """Call the handler with the state.

        Args:
            websocket: The connection.
        """
        await handler(websocket, state)

    return endpoint


def _render(request: Request, template: str, values: dict[str, Any]) -> Response:
    """Render one page.

    Args:
        request: The request (the templates use it for `url_for`).
        template: File name of the template.
        values: What the template needs, next to `version`.

    Returns:
        The HTML response.
    """
    return TEMPLATES.TemplateResponse(request, template, {"version": __version__, **values})


# ------------------------------------------------------------------ the pages


async def fleet(request: Request, state: _State) -> Response:
    """Show every session of every host, newest first.

    With `?all=1` the sessions that have ended are shown as well.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        The fleet page.
    """
    show_all = request.query_params.get("all") == "1"
    query = (request.query_params.get("q") or "").strip().lower()
    rows = await herd.sessions(state.db, active_only=not show_all)
    if query:
        rows = [row for row in rows if query in _searchable(row)]
    waiting = await herd.escalations(state.db)
    groups = [
        (title, [row for row in rows if _group_of(row) == name]) for name, title, _states in GROUPS
    ]
    return _render(
        request,
        "fleet.html",
        {
            "groups": [(title, found) for title, found in groups if found],
            "sessions": rows,
            "open_escalations": len(waiting),
            "show_all": show_all,
            "query": request.query_params.get("q") or "",
        },
    )


async def session_page(request: Request, state: _State) -> Response:
    """Show one session: its steps, its escalations, how to attach.

    Args:
        request: The request; its path holds the session id.
        state: The shared state.

    Returns:
        The session page, or a 404 page if no session has that id.
    """
    return await _session_view(request, state, minted=None)


async def _session_view(
    request: Request, state: _State, minted: tuple[int, str] | None
) -> Response:
    """Render the page of one session, with a client token if one was just minted.

    Args:
        request: The request; its path holds the session id.
        state: The shared state.
        minted: The id and the text of a token minted by this request, or None.

    Returns:
        The session page, or a 404 page if no session has that id.
    """
    session_id = str(request.path_params["session_id"])
    row = await herd.session(state.db, session_id)
    if row is None:
        return _not_found(request, session_id)
    detail = request.query_params.get("detail", "compact")
    if detail not in DETAIL_LEVELS:
        detail = "compact"
    steps = await herd.steps(state.db, session_id, MAX_STEPS)
    runs = await herd.checker_runs(state.db, session_id)
    project = await herd.project_of(state.db, session_id)
    remote = project[1] if project else ""
    by_turn: dict[int | None, list[herd.StepRow]] = {}
    for step in steps:
        by_turn.setdefault(step.turn, []).append(step)
    return _render(
        request,
        "session.html",
        {
            "session": row,
            "steps": steps,
            "runs": runs,
            "detail": detail,
            "levels": DETAIL_LEVELS,
            "turns": await herd.turns(state.db, session_id, MAX_TURNS),
            "forks": {point.turn: point for point in await herd.fork_points(state.db, session_id)},
            "by_turn": by_turn,
            "agents": await herd.agents(state.db, session_id),
            "setting_changes": await herd.setting_changes(state.db, session_id),
            "checker_names": [row.name for row in await admin.checkers(state.db)],
            "escalations": await herd.escalations(state.db, session_id),
            "tokens": await herd.list_tokens(state.db, session_id),
            "project": project[0] if project else "",
            "remote": remote,
            "project_url": git.project_url(remote),
            "commit_url": lambda commit: git.commit_url(remote, commit, state.git_url_template),
            "minted": minted,
            "connect": _connect(row, minted[1] if minted else ""),
        },
    )


async def mint(request: Request, state: _State) -> Response:
    """Mint a client token for a session, so that a client can attach to it.

    The token is shown once, in the answer to this very request, and is kept nowhere: the
    database holds only its hash (D30). The answer is the session page itself and not a
    redirect to it, so the token is never waiting in this process for a later GET -- a GET
    needs less than this POST does (`TokenGate`), and the token decides escalations.

    Args:
        request: The request; its form may hold a `label`.
        state: The shared state.

    Returns:
        The session page with the token on it (not to be cached), or a 404 page if no session
        has that id.
    """
    session_id = str(request.path_params["session_id"])
    form = await request.form()
    label = str(form.get("label") or "calu web")
    try:
        minted = await herd.mint_token(state.db, session_id, label)
    except ValueError:
        return _not_found(request, session_id)
    page = await _session_view(request, state, minted)
    page.headers["cache-control"] = "no-store"
    return page


async def summary_page(request: Request, state: _State) -> Response:
    """Show and correct the chat summary a session's checkers read (PLAN.md D40).

    The summary is what the checkers see of everything older than the unfolded turns, so a wrong
    one misleads every later check. A running session re-reads it at its next check.

    Args:
        request: The request; its path holds the session id. A POST holds the new `summary`.
        state: The shared state.

    Returns:
        The page, a redirect after a save, a 400 page if the text is empty, or a 404 page if no
        session has that id.
    """
    session_id = str(request.path_params["session_id"])
    row = await herd.session(state.db, session_id)
    if row is None:
        return _not_found(request, session_id)
    if request.method == "POST":
        form = await request.form()
        try:
            await admin.set_chat_summary(state.db, session_id, str(form.get("summary") or ""))
        except admin.AdminError as exc:
            return _bad(request, str(exc))
        url = request.url_for("summary", session_id=session_id).include_query_params(saved="1")
        return RedirectResponse(url, 303)
    return _render(
        request,
        "summary.html",
        {
            "session": row,
            "summary": await admin.chat_summary(state.db, session_id),
            "saved": request.query_params.get("saved") == "1",
        },
    )


async def live_page(request: Request, state: _State) -> Response:
    """Show the live view of one running session.

    The page itself is static; everything in it arrives over the WebSocket of `live_socket`.

    Args:
        request: The request; its path holds the session id.
        state: The shared state.

    Returns:
        The live page, or a 404 page if no session has that id.
    """
    session_id = str(request.path_params["session_id"])
    row = await herd.session(state.db, session_id)
    if row is None:
        return _not_found(request, session_id)
    return _render(
        request,
        "live.html",
        {
            "session": row,
            "socket": request.url_for("live_ws", session_id=session_id),
            "efforts": EFFORTS,
            "models": MODELS,
        },
    )


async def live_socket(websocket: WebSocket, state: _State) -> None:
    """Relay one browser's live view to the session's own WebSocket (PLAN.md D34).

    The server mints a client token for the session (D30), connects to the address the session
    recorded (D29), forwards every event to the browser and every command of the browser to the
    session. The token is reused for the lifetime of this server (`live_tokens`); if the session
    refuses it, because it was revoked meanwhile, the cached token is dropped, a fresh one is
    minted and the connection is tried once more (D110).

    Only the requests of `LIVE_METHODS` are forwarded. `shutdown` is not among them: a browser
    should not be able to end the calu process by accident.

    Side effects: mints and revokes a client token; sends messages to the Claude session.

    A session whose process stopped saying it is there (D56) cannot be reached, and the reader
    is told that rather than being sent to look for a networking problem.

    Args:
        websocket: The browser's connection.
        state: The shared state.
    """
    # A browser that offered subprotocols (the key, `TokenGate`) ends the connection unless
    # the answer names one of them.
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol=LIVE_PROTOCOL if LIVE_PROTOCOL in offered else None)
    session_id = str(websocket.path_params["session_id"])
    try:
        await _live(websocket, state, session_id)
    except (psycopg.OperationalError, TimeoutError) as exc:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json(
                {
                    "type": "closed",
                    "message": f"calu web cannot reach the database ({exc}); reload to try again",
                }
            )
    with suppress(RuntimeError):
        await websocket.close()


async def _live(websocket: WebSocket, state: _State, session_id: str) -> None:
    """Attach one accepted browser connection to a session and relay until either side ends.

    Args:
        websocket: The browser's connection, accepted.
        state: The shared state.
        session_id: Id of the session to attach to.

    Raises:
        psycopg.OperationalError: If the database cannot be reached.
        TimeoutError: If the database does not answer in time.
    """
    row = await herd.session(state.db, session_id)
    if row is None or not row.address:
        await websocket.send_json(
            {
                "type": "closed",
                "message": (
                    "this session serves no clients (it was started without the protocol "
                    "server, or it has ended), so there is nothing live to show"
                ),
            }
        )
        return
    for last_attempt in (False, True):
        token = state.live_tokens.get(session_id)
        if token is None:
            _id, token = await herd.mint_token(state.db, session_id, "calu web (live view)")
            state.live_tokens[session_id] = token
        try:
            await _relay(websocket, row, token)
        except RequestFailed as exc:
            # The token was refused, which is not a networking problem: it was revoked since
            # this server minted it -- by hand (D30) or because the session it belonged to
            # ended (D108). The cached one is dropped and a fresh one minted, which is what
            # `live_tokens` promised; only a second refusal is reported.
            state.live_tokens.pop(session_id, None)
            if not last_attempt:
                continue
            await websocket.send_json(
                {
                    "type": "closed",
                    "message": (
                        f"the session refused this server's client token ({exc}). A fresh one "
                        "was minted and refused as well, so the session is not accepting "
                        "clients; `calu token list` shows its tokens."
                    ),
                }
            )
        except (OSError, ConnectionError) as exc:
            await websocket.send_json({"type": "closed", "message": _unreachable_message(row, exc)})
        break


def _unreachable_message(row: herd.SessionRow, exc: Exception) -> str:
    """Explain why a session's own address could not be reached.

    Args:
        row: The session.
        exc: What the connection attempt raised.

    Returns:
        The line for the browser.
    """
    if row.stale:
        # The heartbeat says the process is gone (D56), so this is not a networking problem
        # and sending the reader to look for one would waste their time.
        return (
            f"this session stopped saying it is running {row.silent_for_s / 60:.0f} minutes "
            f"ago, so there is nothing live to reach ({exc}). Its history is still here, "
            "and an escalation of it can be cleared with `calu resolve <id> cancel`."
        )
    return (
        f"cannot reach {row.address}: {exc}. The session runs on {row.hostname}; "
        "run `calu web` on that host, or forward the port with an SSH tunnel."
    )


async def _relay(websocket: WebSocket, row: herd.SessionRow, token: str) -> None:
    """Forward events and commands between one browser and one session, until one side ends.

        browser closes its connection      ->  returns
        session closes its connection      ->  the browser gets {"type": "closed", ...}, then
                                               returns; it does not wait for the browser's
                                               next command to notice (finding wa4)

    Args:
        websocket: The browser's connection.
        row: The session to attach to.
        token: The client token minted for this connection.

    Raises:
        OSError: If the session's address cannot be reached.
        calu.protocol.client.RequestFailed: If the session refuses the token.
        ConnectionError: If the session closes the connection before it sent its state.
    """
    held: list[dict[str, Any]] = []
    sent_snapshot = False

    async def on_event(method: str, params: dict[str, Any]) -> None:
        """Send one session event to the browser, or hold it until the snapshot is out.

        Attaching replays the events the session has already published, and those arrive while
        the snapshot is still being fetched. A browser needs the snapshot first to know what it
        is applying the events to, so early events wait here.

        Args:
            method: The event method.
            params: Its params.
        """
        message = {"type": "event", "method": method, "params": params}
        if not sent_snapshot:
            held.append(message)
            return
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json(message)

    async with connect(row.address, token, "calu web", on_event) as session:
        snapshot = await session.request("state.get")
        await websocket.send_json({"type": "snapshot", "snapshot": snapshot})
        sent_snapshot = True
        for message in held:
            await websocket.send_json(message)
        held.clear()
        session_ended = False
        async with anyio.create_task_group() as tasks:

            async def watch() -> None:
                """End the relay as soon as the session's connection closes."""
                nonlocal session_ended
                await session.closed.wait()
                session_ended = True
                tasks.cancel_scope.cancel()

            tasks.start_soon(watch)
            if await _commands(websocket, session):
                session_ended = True
            tasks.cancel_scope.cancel()
        if session_ended:
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.send_json(
                    {
                        "type": "closed",
                        "message": (
                            "the session closed the connection: its process ended or was "
                            "restarted. Nothing more arrives here; the session list shows "
                            "whether it runs again."
                        ),
                    }
                )


async def _commands(websocket: WebSocket, session: Any) -> bool:
    """Forward the browser's commands to the session until one of the two connections ends.

    Only the requests of `LIVE_METHODS` are forwarded. A frame that is not a JSON object, a
    method that is not on the list and a request the session refuses are each answered with
    `{"type": "refused", ...}`; none of them ends the relay.

    Args:
        websocket: The browser's connection.
        session: The `calu.protocol.client.ProtocolClient` of the session.

    Returns:
        True if the session closed its connection, False if the browser closed its own.
    """
    while True:
        try:
            try:
                command = await websocket.receive_json()
            except ValueError, KeyError:  # not JSON, or a binary frame
                command = None
            if not isinstance(command, dict):
                await websocket.send_json(
                    {"type": "refused", "method": "", "message": "not a JSON object"}
                )
                continue
            method = str(command.get("method") or "")
            params = command.get("params") or {}
            if method not in LIVE_METHODS:
                await websocket.send_json(
                    {"type": "refused", "method": method, "message": "not allowed from a browser"}
                )
                continue
            try:
                result = await session.request(method, params if isinstance(params, dict) else {})
            except RequestFailed as exc:
                await websocket.send_json(
                    {"type": "refused", "method": method, "message": str(exc)}
                )
                continue
            except ConnectionError:
                return True
            await websocket.send_json({"type": "result", "method": method, "result": result})
        except WebSocketDisconnect, RuntimeError:
            return False


async def escalations(request: Request, state: _State) -> Response:
    """Show what waits for a human, across all sessions.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        The escalations page.
    """
    return _render(request, "escalations.html", {"escalations": await herd.escalations(state.db)})


async def escalation_page(request: Request, state: _State) -> Response:
    """Show one escalation with what the checker saw when it asked.

    Args:
        request: The request; its path holds the escalation id.
        state: The shared state.

    Returns:
        The escalation page, or a 404 page if no escalation has that id.
    """
    escalation_id = int(request.path_params["escalation_id"])
    row = await herd.escalation(state.db, escalation_id)
    if row is None:
        return _not_found(request, str(escalation_id))
    context = await herd.escalation_context(state.db, escalation_id)
    return _render(
        request,
        "escalation.html",
        {
            "escalation": row,
            "rendered": str(context.get("rendered") or row.rendered),
            "recent": str(context.get("recent") or ""),
            "files": context.get("files") or {},
            "changeset": str(context.get("changeset") or ""),
            # What each checker decided, which the escalation's own reason does not say when a
            # category held the step back (D42, D53).
            "runs": await herd.escalation_runs(state.db, escalation_id),
        },
    )


async def resolve(request: Request, state: _State) -> Response:
    """Allow or deny an open escalation; the waiting session continues within a second.

    Args:
        request: The request; its form holds `decision` ("allow" or "deny") and a `note`.
        state: The shared state.

    Returns:
        A redirect to the page the form was submitted from, or a 400 page if the decision is
        not "allow" or "deny".
    """
    escalation_id = int(request.path_params["escalation_id"])
    form = await request.form()
    decision = str(form.get("decision") or "")
    note = str(form.get("note") or "")
    if decision not in ("allow", "deny"):
        return _bad(request, f"{decision!r} is neither allow nor deny")
    resolved = await herd.resolve(state.db, escalation_id, decision, note)
    log.info("escalation %s %s by the web interface (%s)", escalation_id, decision, resolved)
    back = str(form.get("back") or request.url_for("escalations"))
    return RedirectResponse(back, 303)


async def triage(request: Request, state: _State) -> Response:
    """Show the triage mode: one escalation at a time, with the queue behind it.

    This is the mode the owner asked for — sit in front of it and answer what comes, across
    every session and host. The page polls `waiting_json`, so an escalation that opens anywhere
    appears without a reload, with a browser notification if the human allowed those.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        The triage page.
    """
    return _render(request, "triage.html", {"api": request.url_for("waiting_json")})


async def sessions_json(request: Request, state: _State) -> Response:
    """Return the sessions as JSON, for the jump palette and the attention badge.

    Every page carries them (`calu.js`), so one query has to serve both: where each session is,
    what it is doing, whether it waits for a human, and the two links that open it.

    Args:
        request: The request; `?all=1` includes the sessions that have ended.
        state: The shared state.

    Returns:
        `{"sessions": [...], "waiting": n}` with the number of escalations waiting anywhere.
    """
    show_all = request.query_params.get("all") == "1"
    rows = await herd.sessions(state.db, active_only=not show_all)
    waiting = await herd.escalations(state.db)
    return JSONResponse(
        {
            "waiting": len(waiting),
            "sessions": [
                {
                    "id": row.id,
                    "where": f"{row.hostname}:{row.directory}",
                    "state": row.state,
                    "open_escalations": row.open_escalations,
                    "group": _group_of(row),
                    "url": str(request.url_for("session", session_id=row.id)),
                    "live": str(request.url_for("live", session_id=row.id)) if row.address else "",
                }
                for row in rows
            ],
        }
    )


async def waiting_json(request: Request, state: _State) -> Response:
    """Return the open escalations as JSON, newest last, for the triage page.

    The first entry carries its full context (the rendered step and, for a command, the command
    line), because that is the one being decided; the rest are the queue behind it.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        `{"escalations": [...]}` with one entry per open escalation.
    """
    rows = await herd.escalations(state.db)
    found: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        entry: dict[str, Any] = {
            "id": row.id,
            "session_id": row.session_id,
            "where": f"{row.hostname}:{row.directory}",
            "reason": row.reason,
            "created_at": row.created_at.isoformat(),
            "rendered": row.rendered if index == 0 else "",
            "command": "",
            "questions": [],
            "session_url": str(request.url_for("session", session_id=row.session_id)),
            # The queue is worked through fast, so the one entry that cannot be acted on has
            # to say so before a decision is typed (D56, D57).
            "unanswerable": row.unanswerable,
        }
        if index == 0:
            context = await herd.escalation_context(state.db, row.id)
            entry["rendered"] = str(context.get("rendered") or row.rendered)
            tool_input = context.get("tool_input")
            tool_name = str(context.get("tool_name"))
            if tool_name == "Bash" and isinstance(tool_input, dict):
                entry["command"] = str(tool_input.get("command") or "")
            if tool_name == "AskUserQuestion" and isinstance(tool_input, dict):
                asked = tool_input.get("questions")
                entry["questions"] = asked if isinstance(asked, list) else []
        found.append(entry)
    return JSONResponse({"escalations": found})


async def resolve_json(request: Request, state: _State) -> Response:
    """Answer one escalation from the triage page, optionally adding a rule for the command.

    "Remember this command" adds an **exactly anchored** allow or deny rule for the command line
    that was escalated (`^...$` with every regular-expression character escaped), so it can
    never match more than the command a human just judged. A broader pattern is a deliberate
    act and belongs in `calu rules add`.

    Args:
        request: The request; its JSON body holds `decision`, `note` and `remember`, or an
            `answer` for a question of the agent (D35), which is recorded as "answered".
        state: The shared state.

    Returns:
        `{"ok": true}`, or `{"ok": false, "message": ...}` with status 400 if the decision is
        none of them or the escalation is not open any more.
    """
    escalation_id = int(request.path_params["escalation_id"])
    body = await request.json()
    answer = body.get("answer")
    decision = "answer" if answer else str(body.get("decision") or "")
    if decision not in ("allow", "deny", "answer"):
        return JSONResponse({"ok": False, "message": "neither allow nor deny"}, status_code=400)
    note = str(answer or body.get("note") or "")
    if not await herd.resolve(state.db, escalation_id, decision, note):
        return JSONResponse({"ok": False, "message": "it is not open any more"}, status_code=400)
    remembered = ""
    command = str(body.get("remember") or "")
    if command:
        rule_list = "command-allow" if decision == "allow" else "command-deny"
        pattern = f"^{re.escape(command)}$"
        try:
            await admin.add_rule(
                state.db,
                rule_list,
                pattern,
                comment=f"from the triage of escalation {escalation_id}",
            )
            remembered = pattern
        except (admin.AdminError, ValueError) as exc:
            log.warning("could not add the rule for %r: %s", command, exc)
    return JSONResponse({"ok": True, "remembered": remembered})


async def checkers(request: Request, state: _State) -> Response:
    """Show the checker definitions: name, route, model, version.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        The checkers page, with each checker's record beside its definition.
    """
    return _render(
        request,
        "checkers.html",
        {
            "checkers": await admin.checkers(state.db),
            # What they decided and where a human disagreed, read from the record rather than
            # measured by running them again (D61).
            "stats": {row.checker_name: row for row in await herd.checker_stats(state.db)},
            # And the rows behind the counts, because this is the page where the instructions
            # are edited: the case to fix them against is one click away (D62).
            "misses": await herd.checker_misses(state.db, limit=10),
            # How many steps reached a checker at all, and what the gate costs in time (D64).
            "steps": await herd.step_stats(state.db),
        },
    )


async def checker_redirect(request: Request, state: _State) -> Response:
    """Send an old link to a checker's own page to that checker on the settings page.

    Args:
        request: The request; its path holds the checker's name.
        state: The shared state.

    Returns:
        A redirect to the pane of the settings page that edits the checker.
    """
    name = str(request.path_params["name"])
    pane = (
        "honesty" if name == "honesty" else "context" if name == "narrator" else f"checker-{name}"
    )
    return RedirectResponse(f"{request.url_for('settings')}#{pane}", 307)


async def rules_redirect(request: Request, state: _State) -> Response:
    """Send an old link to the rules page to the rule lists on the settings page.

    Args:
        request: The request; `?project=` is kept.
        state: The shared state.

    Returns:
        A redirect to the command rules of the settings page.
    """
    url = request.url_for("settings")
    project = request.query_params.get("project")
    if project:
        url = url.include_query_params(project=project)
    return RedirectResponse(f"{url}#rules-command", 307)


async def settings_page(request: Request, state: _State) -> Response:
    """Show and change everything that steers a session, for the global scope or one project.

    One page holds the configuration values, the checker instructions and the allow and deny
    lists (`calu.web.settings` builds it). A POST writes what the form changed, each value
    through the `calu.admin` function the command line uses, which validates it and writes the
    audit log. A refused value does not stop the others: the page comes back with the error at
    its field and what was typed still in it.

    Args:
        request: The request; `?project=NAME` shows that project's scope. A POST holds the
            form: the fields by name, `_present` for every switch it contains, `_reset` for
            every configuration key to remove from the scope, and `add_set` when a starter rule
            set was asked for.
        state: The shared state.

    Returns:
        The settings page; a redirect to it after a save that stored everything; the page with
        status 400 if a value was refused; 404 for an unknown project.
    """
    project = request.query_params.get("project") or None
    posted: dict[str, str] = {}
    errors: dict[str, str] = {}
    saved_now = 0
    try:
        if request.method == "POST":
            form = await request.form()
            posted = {key: str(value) for key, value in form.multi_items() if key[:1] != "_"}
            result = await web_settings.save(
                state.db,
                project,
                posted,
                present=[str(value) for value in form.getlist("_present")],
                resets=[str(value) for value in form.getlist("_reset")],
            )
            errors, saved_now = result.errors, result.count
            if not errors:
                url = request.url_for("settings").include_query_params(saved=str(saved_now))
                if project:
                    url = url.include_query_params(project=project)
                add_set = posted.get("add_set")
                if add_set:
                    added, _skipped = await admin.add_rule_set(state.db, add_set, project)
                    url = url.include_query_params(set_added=str(len(added)))
                return RedirectResponse(url, 303)
        found = web_settings.panes(await web_settings.load(state.db, project))
    except admin.AdminError as exc:
        return _not_found(request, str(exc))
    web_settings.overlay(found, posted, errors)
    nav: list[tuple[str, list[web_settings.Pane]]] = []
    for pane in found:
        if not nav or nav[-1][0] != pane.section:
            nav.append((pane.section, []))
        nav[-1][1].append(pane)
    page = _render(
        request,
        "settings.html",
        {
            "panes": found,
            "sidebar": nav,
            "projects": await admin.projects(state.db),
            "project": project or "",
            "saved": request.query_params.get("saved"),
            "set_added": request.query_params.get("set_added"),
            "errors": errors,
            "saved_now": saved_now,
            # The pane to show first: the one with the first refused field, if any.
            "open_pane": next(
                (pane.id for pane in found if any(item.error for item in pane.fields)), ""
            ),
        },
    )
    if errors:
        page.status_code = 400
    return page


# ------------------------------------------------------------------ helpers


def _group_of(row: herd.SessionRow) -> str:
    """Decide which group of the fleet page a session belongs to.

    Args:
        row: The session.

    Returns:
        The group's name. A session with an open escalation is in "needs you" whatever its state
        says, because that is what a human has to act on -- unless it has stopped saying it is
        there (D56), in which case it is "gone": answering its escalation would reach nobody,
        and showing it among the sessions that need a decision would waste the operator's time.
    """
    if row.stale:
        return "gone"
    if row.open_escalations:
        return "needs_you"
    for name, _title, states in GROUPS:
        if row.state in states:
            return name
    return "idle"


def _searchable(row: herd.SessionRow) -> str:
    """Return the text a fleet filter matches against.

    Args:
        row: The session.

    Returns:
        Host, user, directory, state, project directory and Claude's session id, lower case.
    """
    return " ".join(
        part.lower()
        for part in (
            row.hostname,
            row.os_user,
            row.directory,
            row.state,
            row.claude_session_id or "",
            row.id,
        )
    )


def _connect(row: herd.SessionRow, token: str) -> list[tuple[str, str]]:
    """Build the commands with which a client attaches to a session.

    The address comes from the session's own row (D29), which is empty for a session that was
    started without the protocol server or by an older calu.

    Args:
        row: The session.
        token: A freshly minted token, or "" if none was minted in this request.

    Returns:
        (label, command) pairs: the two clients as they are started **on** the session's own
        machine, where they need neither the address nor a token (D105), the same two for a
        client elsewhere, and the herd commands that need no token at all. A placeholder stands
        in for the token until one is minted.
    """
    address = row.address or "ws://<the session printed its address>"
    shown = token or "<mint a token above>"
    commands = [
        # On that machine neither the address nor a token is needed (D105); the explicit forms
        # below are for a client elsewhere, through a tunnel. `--token=` and not `--token `,
        # so that a token from before D102, which may start with a dash, works too.
        (f"Terminal client, in {row.directory} on {row.hostname}", "calu attach"),
        ("ACP editor there (Zed, JetBrains Air, Neovim, Goose)", "calu acp"),
        ("Terminal client from elsewhere", f"calu attach {address} --token={shown}"),
        ("ACP editor from elsewhere", f"calu acp {address} --token={shown}"),
        ("Escalations of this session", f"calu escalations {row.id}"),
    ]
    if row.claude_session_id:
        commands.append(("Resume after a stop", f"calu run --resume {row.claude_session_id}"))
    return commands


def _not_found(request: Request, what: str) -> Response:
    """Render the "not found" page.

    Args:
        request: The request.
        what: The id that was asked for, as the request gave it. The template escapes it.

    Returns:
        The page, with status 404.
    """
    return _error(request, 404, "Not found", f"No such thing: {what}")


def _bad(request: Request, why: str) -> Response:
    """Render the "bad request" page.

    Args:
        request: The request.
        why: What was wrong; it may quote the request. The template escapes it.

    Returns:
        The page, with status 400.
    """
    return _error(request, 400, "Bad request", why)


def _error(
    request: Request, status: int, title: str, message: str, retry: bool = False
) -> Response:
    """Render an error page through the template, never by building HTML from text.

    Args:
        request: The request.
        status: The HTTP status of the response.
        title: The page's heading.
        message: One paragraph of plain text. It is shown escaped, so it may hold anything a
            request carried (finding wa2).
        retry: True makes the page ask for itself again after five seconds.

    Returns:
        The page.
    """
    page = _render(request, "error.html", {"title": title, "message": message, "retry": retry})
    page.status_code = status
    return page


async def _database_down(request: Request, exc: Exception) -> Response:
    """Answer a request whose database statement could not be run (finding wa3).

    Registered for `psycopg.OperationalError` and `TimeoutError`. The operator is told that
    the page could not be read, instead of seeing an empty or stale one.
    `calu.web.database.ReopeningDb` opens a new connection for the next request, so the pages
    recover on their own when the database is back.

    Args:
        request: The request.
        exc: What the statement raised.

    Returns:
        Status 503: `{"ok": false, "error": "database unreachable", "message": ...}` for the
        paths under `/api/`, the error page otherwise. The page of a GET asks for itself again
        after five seconds; the page of a request that was to change something does not, and
        says that the change may not have happened.
    """
    log.warning("the database could not be reached for %s: %s", request.url.path, exc)
    reading = request.method in SAFE_METHODS
    message = f"calu web cannot reach the database ({type(exc).__name__}: {exc}). " + (
        "This page tries again every five seconds. Sessions that wait for a decision keep waiting."
        if reading
        else "What you sent may not have taken effect: look at the page again before you repeat it."
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "error": "database unreachable", "message": message}, status_code=503
        )
    return _error(request, 503, "The database cannot be reached", message, retry=reading)
