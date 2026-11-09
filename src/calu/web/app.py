"""The web interface: many sessions at once, their history, and what steers their checkers.

    calu web                      # http://127.0.0.1:8800, needs the `admin` credential

It is the herd client of PLAN.md §6.2: it works on the database, so it sees the sessions of
every host without a process of its own on them, and it holds the `admin` credential, so it can
also change what steers the checkers. What it deliberately does not do is drive a live session
(chat, interrupt, comments): that belongs to the session's own WebSocket, and the page hands the
user the command to attach to it instead.

Pages:

    /                     every session of every host, newest first, with its open escalations
    /sessions/{id}        one session: its turns and tool calls, and how to attach to it
    /escalations          what waits for a human, with allow and deny
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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from calu import __version__, admin, herd
from calu.db import Db
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
        Route("/escalations", _page(escalations, state), name="escalations"),
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
    rows = await herd.sessions(state.db, active_only=not show_all)
    waiting = await herd.escalations(state.db)
    return _render(
        request,
        "fleet.html",
        {"sessions": rows, "open_escalations": len(waiting), "show_all": show_all},
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
