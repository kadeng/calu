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
    /checkers             the checker definitions and their instructions, editable
    /rules                the allow and deny lists
    /settings             every configuration value, generated from the model

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
        Route(
            "/checkers/{name}",
            _page(checker_page, state),
            methods=["GET", "POST"],
            name="checker",
        ),
        Route("/rules", _page(rules, state), name="rules"),
        Route(
            "/rules/{rule_list}", _page(save_rule_list, state), methods=["POST"], name="save_rules"
        ),
        Route(
            "/rule-sets/{name}",
            _page(add_rule_set, state),
            methods=["POST"],
            name="add_rule_set",
        ),
        Route(
            "/honesty/{mark_id:int}/clear",
            _page(clear_mark, state),
            methods=["POST"],
            name="clear_mark",
        ),
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
        if offered and secrets.compare_digest(offered, self.token):
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
                if key == TOKEN_COOKIE and secrets.compare_digest(given, self.token):
                    return True
        return False


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
    """Show one session: its steps, its escalations, its honesty register, how to attach.

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
            # The honesty register of this session's project: an operator looking at a session
            # whose step was refused is here, not on the rules page (PLAN.md D84, D86).
            "marks": await herd.honesty_marks(state.db, project[0] if project else None),
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


async def checker_page(request: Request, state: _State) -> Response:
    """Show one checker's instructions in an editable form, or store what was edited.

    The same URL serves the form and takes it, so editing the instructions is one page and one
    keystroke (the page submits on Ctrl+S) — this is the page an owner visits most.

    Args:
        request: The request; its path holds the checker's name. A POST holds the new
            `instructions`.
        state: The shared state.

    Returns:
        The checker page, a redirect after a successful save, a 400 page if the instructions are
        empty, or a 404 page if there is no such checker.
    """
    name = str(request.path_params["name"])
    if request.method == "POST":
        return await save_checker(request, state)
    rows = [row for row in await admin.checkers(state.db) if row.name == name]
    if not rows:
        return HTMLResponse(_not_found(name), status_code=404)
    return _render(
        request,
        "checker.html",
        {
            "checker": rows[0],
            "instructions": await admin.instructions(state.db, name),
            "saved": request.query_params.get("saved") == "1",
        },
    )


async def save_checker(request: Request, state: _State) -> Response:
    """Store new instructions for a checker; running sessions pick them up at their next step.

    A changed definition bumps the checker's version and is written to the audit log, as it is
    for `calu checker set` (the database does both).

    Args:
        request: The request; its form holds the `instructions`.
        state: The shared state.

    Returns:
        A redirect back to the checker page, or a 400 page if the instructions are empty.
    """
    name = str(request.path_params["name"])
    form = await request.form()
    instructions = str(form.get("instructions") or "").strip()
    if not instructions:
        return HTMLResponse(_bad("the instructions may not be empty"), status_code=400)
    try:
        await admin.set_checker(state.db, name, instructions_text=instructions)
    except admin.AdminError as exc:
        return HTMLResponse(_bad(str(exc)), status_code=400)
    url = request.url_for("checker", name=name).include_query_params(saved="1")
    return RedirectResponse(url, 303)


async def rules(request: Request, state: _State) -> Response:
    """Show the allow and deny lists, grouped by list.

    Args:
        request: The request.
        state: The shared state.

    Returns:
        The rules page.
    """
    return await _rules_page(request, state)


RULE_GROUPS = (
    ("Commands", "command", "a command"),
    ("File reads", "read", "a path a tool reads"),
    ("File writes", "write", "a path a tool writes"),
)
"""The three kinds of list, each shown as its allow list beside its deny list."""


async def _rules_page(
    request: Request,
    state: _State,
    failed: tuple[str, str, str] | None = None,
) -> Response:
    """Render the rules page: every list of one scope as a text area (PLAN.md D137).

    Args:
        request: The request; `?project=` names the scope, `?saved=` a list just stored.
        state: The shared state.
        failed: (list, the text that was submitted, why it was refused) after a save that did
            not go through, so the page shows the error at that list and keeps what was typed.

    Returns:
        The page; status 400 when `failed` is given.
    """
    project = request.query_params.get("project") or None
    rows = await admin.rules(state.db, project)
    scope = project or "global"
    texts: dict[str, str] = {}
    disabled: dict[str, int] = {}
    inherited: dict[str, int] = {}
    for rule_list in admin.RULE_LISTS:
        own = [r for r in rows if r.list == rule_list and r.scope == scope]
        texts[rule_list] = admin.rule_list_text([r for r in own if r.enabled])
        disabled[rule_list] = sum(1 for r in own if not r.enabled)
        inherited[rule_list] = sum(
            1 for r in rows if r.list == rule_list and r.scope == "global" and project
        )
    error = None
    if failed is not None:
        texts[failed[0]] = failed[1]
        error = {"list": failed[0], "message": failed[2]}
    page = _render(
        request,
        "rules.html",
        {
            "groups": RULE_GROUPS,
            "texts": texts,
            "counts": {name: _pattern_count(text) for name, text in texts.items()},
            "disabled": disabled,
            "inherited": inherited,
            "project": project,
            "projects": await admin.projects(state.db),
            "saved": None if failed else request.query_params.get("saved"),
            "error": error,
            # Ready-made sets that are not in the default lists (D143), offered under the list
            # they would be added to.
            "rule_sets": [
                (name, rule_list, what) for name, (rule_list, what) in admin.RULE_SETS.items()
            ],
            "set_added": request.query_params.get("set_added"),
            # The honesty register blocks steps as the deny lists do, so it belongs on the page
            # about what blocks a step (PLAN.md D82). Clearing one is the override for a mark
            # nobody agrees with (D83).
            "marks": await herd.honesty_marks(state.db),
        },
    )
    if failed is not None:
        page.status_code = 400
    return page


def _pattern_count(text: str) -> int:
    """Count the patterns in the text of a rule list.

    Args:
        text: The list as edited: patterns, `#` comments and blank lines.

    Returns:
        The number of pattern lines.
    """
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


async def save_rule_list(request: Request, state: _State) -> Response:
    """Replace one rule list of one scope with the text of its form (PLAN.md D137).

    The whole list is replaced, as `calu rules edit` does, through
    `calu.admin.replace_rule_list`: every pattern is compiled before anything is deleted, so a
    typo leaves the list as it was -- and the page then shows the error at that list with the
    text still in it, so nothing typed is lost. Disabled rules are not part of the text and are
    kept.

    Args:
        request: The request; its path names the list, `?project=` the scope, and its form
            holds `text`.
        state: The shared state.

    Returns:
        A redirect back to the page with `saved=<list>`, or the page with the error (400).
    """
    rule_list = str(request.path_params["rule_list"])
    project = request.query_params.get("project") or None
    form = await request.form()
    text = str(form.get("text") or "").replace("\r\n", "\n")
    try:
        await admin.replace_rule_list(state.db, rule_list, text, project)
    except admin.AdminError as exc:
        return await _rules_page(request, state, failed=(rule_list, text, str(exc)))
    query = f"?project={project}&saved={rule_list}" if project else f"?saved={rule_list}"
    return RedirectResponse(
        str(request.url_for("rules")) + query + f"#{rule_list}", status_code=303
    )


async def add_rule_set(request: Request, state: _State) -> Response:
    """Append a ready-made rule set to its list in one scope (PLAN.md D143).

    The same as `calu rules add-set NAME`, through `calu.admin.add_rule_set`: patterns the list
    already has, disabled ones included, are skipped, so pressing the button twice adds nothing.

    Args:
        request: The request; its path names the set, `?project=` the scope.
        state: The shared state.

    Returns:
        A redirect back to the page at that list, with `set_added=<count>`; 404 for an unknown
        set or project.
    """
    name = str(request.path_params["name"])
    project = request.query_params.get("project") or None
    try:
        added, _skipped = await admin.add_rule_set(state.db, name, project)
    except admin.AdminError as exc:
        return HTMLResponse(_not_found(str(exc)), status_code=404)
    rule_list = admin.RULE_SETS[name][0]
    query = f"?set_added={len(added)}" + (f"&project={project}" if project else "")
    return RedirectResponse(
        str(request.url_for("rules")) + query + f"#{rule_list}", status_code=303
    )


async def clear_mark(request: Request, state: _State) -> Response:
    """Close one honesty mark by hand and go back to the rules page (PLAN.md D83).

    The override for a mark nobody agrees with: while `honesty.mode` is "enforce" an open mark
    stops every step that would run that code, so somebody has to be able to say "this is fine".
    The row is kept and recorded as closed from the web interface.

    Args:
        request: The request; its path carries the mark id.
        state: The shared state.

    Returns:
        A redirect back to the rules page, or 404 if no open mark has that id.
    """
    mark_id = int(request.path_params["mark_id"])
    cleared = await herd.clear_honesty_mark(state.db, mark_id, "the web interface")
    if cleared is None:
        return HTMLResponse(_not_found(f"open honesty mark {mark_id}"), status_code=404)
    return RedirectResponse(request.url_for("rules"), status_code=303)


async def settings_page(request: Request, state: _State) -> Response:
    """Show and change the configuration of the global scope or of one project.

    The form is generated from `calu.config.Config` (`calu.web.settings`), so every value calu
    knows is editable here with the right control: Claude's model and effort, whether
    `CLAUDE_CODE_USE_BEDROCK=1` is passed, the model and the thinking effort of each checker,
    the sandbox, the context budget, the clock watch and the write sets. Only the values that
    changed are written, each through `calu.admin.set_setting`, which validates the whole
    configuration first and writes the audit log.

    Args:
        request: The request; `?project=NAME` edits that project's scope. A POST holds the form.
        state: The shared state.

    Returns:
        The settings page, a redirect after a save, or a 400 page if a value is refused.
    """
    project = request.query_params.get("project") or None
    values = await admin.settings(state.db, project)
    if request.method == "POST":
        form = await request.form()
        posted = {key: str(value) for key, value in form.multi_items() if key != "project"}
        try:
            changed = web_settings.changes(values, posted)
        except ValueError as exc:
            return HTMLResponse(_bad(f"a value was not valid JSON: {exc}"), status_code=400)
        for key, value in changed.items():
            try:
                await admin.set_setting(state.db, key, value, project)
            except (admin.AdminError, ValueError) as exc:
                # Everything before this key is stored; the page says which one was refused, so
                # the human sees what still has to be fixed.
                return HTMLResponse(_bad(f"{key} was refused: {exc}"), status_code=400)
        url = request.url_for("settings").include_query_params(saved=str(len(changed)))
        if project:
            url = url.include_query_params(project=project)
        return RedirectResponse(url, 303)
    return _render(
        request,
        "settings.html",
        {
            "sections": web_settings.sections(values),
            "projects": await admin.projects(state.db),
            "project": project or "",
            "saved": request.query_params.get("saved"),
        },
    )


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
