"""The web interface: many sessions at once, their history, and what steers their checkers.

    calu web                      # http://127.0.0.1:8800, needs the `admin` credential

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

**There is no login** (PLAN.md Q4 is open), so the server refuses to bind anything but the
loopback interface: reach it from another machine through an SSH tunnel

    ssh -L 8800:127.0.0.1:8800 the-host

or put a TLS proxy with its own authentication in front of it. Everything the pages can do, the
`admin` credential can do, including changing the rules a session is judged by.
"""

import ipaddress
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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


def create_app(db: Db, git_url_template: str = "", token: str | None = None) -> Starlette:
    """Build the web application on an open database connection.

    The connection is used by every request, and `calu.db.Db` serializes the statements, so
    concurrent requests are safe but not parallel. That is deliberate: one operator's overview
    does not need a pool, and a session's credentials are not available here anyway.

    Args:
        db: The database connection (owner or `calu_operator` rights).
        git_url_template: `web.git_url_template` for commit links to a git browser that is
            neither GitHub, GitLab nor Bitbucket ("" guesses from the project's remote).
        token: The login token every request must carry (`TokenGate`, D161). `calu web`
            always passes one; None leaves the gate out, for in-process tests.

    Returns:
        The Starlette application, ready for uvicorn.
    """
    state = _State(db=db, git_url_template=git_url_template)
    routes = [
        Route("/", _page(fleet, state), name="fleet"),
        Route("/sessions/{session_id}", _page(session_page, state), name="session"),
        Route("/sessions/{session_id}/token", _page(mint, state), methods=["POST"], name="mint"),
        Route("/sessions/{session_id}/live", _page(live_page, state), name="live"),
        Route(
            "/sessions/{session_id}/summary",
            _page(summary_page, state),
            methods=["GET", "POST"],
            name="summary",
        ),
        WebSocketRoute("/ws/sessions/{session_id}", _socket(live_socket, state), name="live_ws"),
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
    middleware = [Middleware(LocalOnly)]
    if token is not None:
        middleware.append(Middleware(TokenGate, token=token))
    return Starlette(routes=routes, middleware=middleware)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
"""Host names a request to the web interface may carry: it binds loopback only (`check_host`)."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that change nothing, and are therefore not checked for their origin."""


TOKEN_COOKIE = "calu_web_token"
"""Cookie that carries the login token of `calu web` after the first visit (D161)."""


class TokenGate:
    """Let through only requests that carry the token `calu web` printed (PLAN.md D161).

    `LocalOnly` keeps other websites out, but not other processes of this user: Claude runs
    as the same user, and a `curl -X POST .../escalations/1/resolve` from its Bash would decide
    its own step. So the interface has a login of the kind a local web tool usually has: a
    random token, printed with the address at start (`http://127.0.0.1:8800/?token=...`).

        GET /?token=<right>         ->  303 to / with an HttpOnly, SameSite=Strict cookie
        request with the cookie     ->  passes
        anything else               ->  401 (HTTP) or close code 1008 (WebSocket)

    The token lives only in the process and the browser; a restart prints a new one.
    """

    def __init__(self, app: Any, token: str) -> None:
        """Wrap the application.

        Args:
            app: The ASGI application.
            token: The secret; a request must carry it as `TOKEN_COOKIE` or `?token=`.
        """
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Pass a request on, exchange a token in the URL for a cookie, or refuse it.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self._has_cookie(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        offered = (query.get("token") or [""])[0]
        if offered and _same(offered, self.token):
            rest = urlencode([(k, v) for k, vs in query.items() if k != "token" for v in vs])
            location = scope.get("path", "/") + (f"?{rest}" if rest else "")
            cookie = f"{TOKEN_COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/"
            await send(
                {
                    "type": "http.response.start",
                    "status": 303,
                    "headers": [
                        (b"location", location.encode("latin-1")),
                        (b"set-cookie", cookie.encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        body = (
            b"calu web: open the address that `calu web` printed when it started; it carries "
            b"the login token (once per browser).\n"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _has_cookie(self, scope: Any) -> bool:
        """Tell whether the request carries the right token cookie.

        Args:
            scope: The ASGI scope.

        Returns:
            True if a `TOKEN_COOKIE` with the token is among its cookies.
        """
        for name, value in scope.get("headers", []):
            if name.lower() != b"cookie":
                continue
            for part in value.decode("latin-1").split(";"):
                key, _, given = part.strip().partition("=")
                if key == TOKEN_COOKIE and _same(given, self.token):
                    return True
        return False


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

    The interface has no login, because it serves loopback only -- but a browser on this
    machine will send requests to 127.0.0.1 on behalf of **any** site it has open. Without this,
    a page anywhere could submit "allow" for escalations 1 to 100 (a form POST is sent
    cross-site without asking, and the ids are small integers), rewrite the settings or the
    checker instructions, or open the live WebSocket and decide steps through it; a page whose
    domain re-resolves to 127.0.0.1 (DNS rebinding) could also read everything shown here.

    Two checks, on every request and WebSocket handshake:

    - The `Host` header must be a loopback name. A rebinding page sends its own domain there.
    - A request that can change something -- every method but GET, HEAD and OPTIONS, and every
      WebSocket -- must come from this interface's own pages: `Sec-Fetch-Site` may not say
      `cross-site` or `same-site` (another port on localhost is "same-site"), and an `Origin`,
      when sent, must be this server's own. Browsers send both on such requests; a client that
      sends neither (curl, `calu`'s own tests) is not a browser acting for someone else.
    """

    def __init__(self, app: Any, extra_hosts: Collection[str] = ()) -> None:
        """Wrap the application.

        Args:
            app: The ASGI application.
            extra_hosts: Further host names to accept, for a test client whose base URL is not
                a loopback name.
        """
        self.app = app
        self.hosts = LOOPBACK_HOSTS | set(extra_hosts)

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
        if name not in self.hosts:
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
        self.minted: dict[str, tuple[int, str]] = {}
        """Session id -> the token id and token of the last `mint`, shown once on the page."""
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
    session_id = request.path_params["session_id"]
    row = await herd.session(state.db, session_id)
    if row is None:
        return HTMLResponse(_not_found(session_id), status_code=404)
    detail = request.query_params.get("detail", "compact")
    if detail not in DETAIL_LEVELS:
        detail = "compact"
    steps = await herd.steps(state.db, session_id, MAX_STEPS)
    runs = await herd.checker_runs(state.db, session_id)
    project = await herd.project_of(state.db, session_id)
    remote = project[1] if project else ""
    minted = state.minted.pop(session_id, None)
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

    The token is shown once on the session page after the redirect and is not stored anywhere
    else: the database holds only its hash (D30).

    Args:
        request: The request; its form may hold a `label`.
        state: The shared state.

    Returns:
        A redirect back to the session page.
    """
    session_id = request.path_params["session_id"]
    form = await request.form()
    label = str(form.get("label") or "calu web")
    try:
        token_id, token = await herd.mint_token(state.db, session_id, label)
    except ValueError:
        return HTMLResponse(_not_found(session_id), status_code=404)
    state.minted[session_id] = (token_id, token)
    return RedirectResponse(request.url_for("session", session_id=session_id), 303)


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
        return HTMLResponse(_not_found(session_id), status_code=404)
    if request.method == "POST":
        form = await request.form()
        try:
            await admin.set_chat_summary(state.db, session_id, str(form.get("summary") or ""))
        except admin.AdminError as exc:
            return HTMLResponse(_bad(str(exc)), status_code=400)
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
    session_id = request.path_params["session_id"]
    row = await herd.session(state.db, session_id)
    if row is None:
        return HTMLResponse(_not_found(session_id), status_code=404)
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
    await websocket.accept()
    session_id = str(websocket.path_params["session_id"])
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
        await websocket.close()
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
    with suppress(RuntimeError):
        await websocket.close()


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
    """Forward events and commands between one browser and one session.

    Args:
        websocket: The browser's connection.
        row: The session to attach to.
        token: The client token minted for this connection.

    Raises:
        OSError: If the session's address cannot be reached.
        calu.protocol.client.RequestFailed: If the session refuses the token.
        ConnectionError: If the session closes the connection.
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
        while True:
            try:
                command = await websocket.receive_json()
            except WebSocketDisconnect, RuntimeError:
                return
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
            await websocket.send_json({"type": "result", "method": method, "result": result})


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
        return HTMLResponse(_not_found(str(escalation_id)), status_code=404)
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
        return HTMLResponse(_bad(f"{decision!r} is neither allow nor deny"), status_code=400)
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
        return HTMLResponse(_not_found(str(exc)), status_code=404)
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


def _not_found(what: str) -> str:
    """Render a small "not found" page.

    Args:
        what: The id that was asked for.

    Returns:
        The HTML.
    """
    return f"<!doctype html><title>not found</title><h1>No such thing: {what}</h1>"


def _bad(why: str) -> str:
    """Render a small "bad request" page.

    Args:
        why: What was wrong.

    Returns:
        The HTML.
    """
    return f"<!doctype html><title>bad request</title><h1>{why}</h1>"
