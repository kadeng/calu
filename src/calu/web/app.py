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
    /escalations          what waits for a human, with allow and deny
    /triage               one escalation at a time, with the queue behind it
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
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
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
    ("working", "Working", frozenset({"running", "checking", "tool_call", "starting"})),
    ("idle", "Idle", frozenset({"idle"})),
    ("ended", "Ended", frozenset({"stopped", "failed"})),
)
"""How the fleet page groups the sessions, in the order it shows them (PLAN.md D33).

Every agent UI that manages many sessions leads with the ones that cannot go on without a
human — Claude Code's agent view groups into "Needs input", "Working", "Completed", Orca into
"Needs You", "Working", "Done", "Idle". calu's escalations are exactly that, so a session that
waits comes first, and a session with an open escalation counts as waiting whatever its state
says.
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


def create_app(db: Db, git_url_template: str = "") -> Starlette:
    """Build the web application on an open database connection.

    The connection is used by every request, and `calu.db.Db` serializes the statements, so
    concurrent requests are safe but not parallel. That is deliberate: one operator's overview
    does not need a pool, and a session's credentials are not available here anyway.

    Args:
        db: The database connection (owner or `calu_operator` rights).
        git_url_template: `web.git_url_template` for commit links to a git browser that is
            neither GitHub, GitLab nor Bitbucket ("" guesses from the project's remote).

    Returns:
        The Starlette application, ready for uvicorn.
    """
    state = _State(db=db, git_url_template=git_url_template)
    routes = [
        Route("/", _page(fleet, state), name="fleet"),
        Route("/sessions/{session_id}", _page(session_page, state), name="session"),
        Route("/sessions/{session_id}/token", _page(mint, state), methods=["POST"], name="mint"),
        Route("/sessions/{session_id}/live", _page(live_page, state), name="live"),
        WebSocketRoute("/ws/sessions/{session_id}", _socket(live_socket, state), name="live_ws"),
        Route("/escalations", _page(escalations, state), name="escalations"),
        Route("/triage", _page(triage, state), name="triage"),
        Route("/api/escalations", _page(waiting_json, state), name="waiting_json"),
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
        Route("/settings", _page(settings_page, state), methods=["GET", "POST"], name="settings"),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    return Starlette(routes=routes)


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
        it; the next live view then mints a fresh one.
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
    """Show one session: its tool calls, its open escalations and how to attach to it.

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
    session. The token is reused for the lifetime of this server and is revocable by hand
    (`live_tokens`).

    Only the requests of `LIVE_METHODS` are forwarded. `shutdown` is not among them: a browser
    should not be able to end the calu process by accident.

    Side effects: mints and revokes a client token; sends messages to the Claude session.

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
    token = state.live_tokens.get(session_id)
    if token is None:
        _id, token = await herd.mint_token(state.db, session_id, "calu web (live view)")
        state.live_tokens[session_id] = token
    try:
        await _relay(websocket, row, token)
    except (OSError, RequestFailed, ConnectionError) as exc:
        await websocket.send_json(
            {
                "type": "closed",
                "message": (
                    f"cannot reach {row.address}: {exc}. The session runs on {row.hostname}; "
                    "run `calu web` on that host, or forward the port with an SSH tunnel."
                ),
            }
        )
    finally:
        with suppress(RuntimeError):
            await websocket.close()


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
        The checkers page.
    """
    return _render(request, "checkers.html", {"checkers": await admin.checkers(state.db)})


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
    rows = await admin.rules(state.db)
    grouped: dict[str, list[admin.RuleRow]] = {}
    for row in rows:
        grouped.setdefault(row.list, []).append(row)
    return _render(request, "rules.html", {"grouped": grouped})


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
        says, because that is what a human has to act on.
    """
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
        (label, command) pairs: a terminal client, an ACP editor, and the herd commands that
        need no token. A placeholder stands in for the token until one is minted.
    """
    address = row.address or "ws://<the session printed its address>"
    shown = token or "<mint a token above>"
    commands = [
        ("Terminal client", f"calu attach {address} --token {shown}"),
        ("ACP editor (Zed, JetBrains Air, Neovim, Goose)", f"calu acp {address} --token {shown}"),
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
