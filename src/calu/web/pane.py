"""The chat pane of `calu web`: the IDE chat view in a browser tab or an iframe (PLAN.md D261).

The pane is the chat webview of the VSCode extension, built for the browser
(`extensions/vscode/pane/`, bundled into `static/pane/`). It talks to a `calu ide` bridge that
this process runs for it, one per browser tab, over a WebSocket:

    browser tab (/pane)                               calu web
      webview script + host/chatHost.ts                 GET /pane            the page (no data)
        └─ BridgeCore ──WebSocket /pane/rpc──────────►  pane_socket ──► calu.ide.bridge.Bridge
             one JSON-RPC message per text frame                         (workspace = ?dir=)

Login (`TokenGate`): the page and its static files are served without one, because they hold
no data. The WebSocket needs either the browser's login (cookie and key, as every other page) or
the password as the subprotocol `calu.password.<base64url>` (D279). The second is for an iframe
of another site (Jupyter on another port), where the browser sends no SameSite cookie and keeps
no key: the pane asks for the password and keeps it in the page's memory only.

Framing: only `/pane` may be shown in a frame, and only by the origins of
`web.pane_frame_ancestors` (`SecurityHeaders`); every other page keeps `frame-ancestors 'none'`.

What a pane may ask its bridge (`PANE_METHODS`): attaching, starting and renaming sessions,
the session protocol except `shutdown`, the settings it changes (honesty mode, effort), the
temporary rules (`/rule`, `/rules`, `/rule-remove`; never one that lifts the built-in
protections), and reading project files for its file view. Not: writing, deleting or renaming files
(`ide.fs.write*`), attaching to an explicit address with a token, or anything else.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from calu.acp.peer import Peer, RpcError
from calu.acp.serve import DRAIN_S
from calu.ide.access import INVALID_PARAMS
from calu.ide.bridge import METHOD_NOT_FOUND, Bridge
from calu.protocol.client import first_set

if TYPE_CHECKING:
    from calu.credentials import CredentialStore

log = logging.getLogger(__name__)

PANE_PATH = "/pane"
"""Address of the pane's page."""

PANE_RPC_PATH = "/pane/rpc"
"""Address of the pane's WebSocket to its bridge."""

PASSWORD_PROTOCOL = "calu.password."
"""Prefix of the WebSocket subprotocol in which a pane in a frame of another site, which gets
no cookie, sends the password a person typed into it, base64url-encoded (D279); `TokenGate`
checks it for `PANE_RPC_PATH` only, with the back-off of the sign-in page."""

MAX_PANES = 8
"""Bridges that run at the same time; a further pane is told to close one first."""

PANE_METHODS = frozenset(
    {
        "ide.initialize",
        "ide.shutdown",
        "ide.status",
        "ide.unlock",
        "ide.sessions",
        "ide.attach",
        "ide.detach",
        "ide.start",
        "ide.start.cancel",
        "ide.session.rename",
        "ide.settings.get",
        "ide.settings.set",
        "ide.settings.unset",
        "ide.rules.list",
        "ide.rules.add",
        "ide.rules.remove",
        "ide.fs.read",
        "session.call",
    }
)
"""The bridge requests a pane may send (see the module comment)."""

REFUSED_SESSION_METHODS = frozenset({"shutdown"})
"""Session requests a pane may not send through `session.call`: a browser should not be able to
end the session's calu process (the live view refuses it as well)."""

DEFAULT_FRAME_ANCESTORS = ("'self'", "http://localhost:*", "http://127.0.0.1:*")
"""Who may show the pane in a frame when `web.pane_frame_ancestors` is not set: this interface
and pages served from this machine's loopback addresses, such as a local Jupyter."""


def frame_ancestors(sources: list[str] | tuple[str, ...] | None) -> str:
    """Build the CSP `frame-ancestors` sources of the pane's page.

    Args:
        sources: `web.pane_frame_ancestors`; None or empty uses `DEFAULT_FRAME_ANCESTORS`.
            Each entry is a CSP source expression such as "'self'" or "https://lab.example.org";
            entries with a space, ";" or "," are dropped (they would add directives).

    Returns:
        The sources joined by spaces, e.g. "'self' http://localhost:*". "'none'" if every
        entry was dropped.

    Example:
        `frame_ancestors(None)` is "'self' http://localhost:* http://127.0.0.1:*".
    """
    chosen = [s.strip() for s in (sources or DEFAULT_FRAME_ANCESTORS)]
    kept = [s for s in chosen if s and not any(c in s for c in " \t;,\r\n")]
    return " ".join(kept) if kept else "'none'"


def pane_workspace(given: str | None) -> Path:
    """Choose the workspace folder of a pane's bridge.

    The bridge attaches to "the session of the workspace" and starts new sessions there, as
    `calu ide` does in an editor's folder.

    Args:
        given: The `dir` query parameter of the WebSocket address; None or "" for none.

    Returns:
        The resolved directory: `given` if it is an absolute path of an existing directory,
        else this process's working directory.

    Raises:
        ValueError: If `given` is set but not an absolute path of an existing directory.
    """
    if not given:
        return Path.cwd()
    path = Path(given).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"{given!r} is not an absolute path of a directory on this machine")
    return path.resolve()


def checked_handler(handle: Any) -> Any:
    """Wrap a bridge's request handler with the pane's allowlist.

    Args:
        handle: `Bridge.handle`.

    Returns:
        An async handler with the same contract that refuses, with `METHOD_NOT_FOUND`, every
        method outside `PANE_METHODS` and every `session.call` of a method in
        `REFUSED_SESSION_METHODS`, and with `INVALID_PARAMS` an `ide.attach` that names an
        address or a token.
    """

    async def handler(method: str, params: dict[str, Any]) -> Any:
        """Answer one request of the pane, or refuse it.

        Args:
            method: The request method.
            params: Its params.

        Returns:
            The bridge's result.

        Raises:
            RpcError: For a refused request, or what the bridge raises.
        """
        if method not in PANE_METHODS:
            raise RpcError(METHOD_NOT_FOUND, f"the chat pane may not ask {method!r}")
        if method == "session.call" and params.get("method") in REFUSED_SESSION_METHODS:
            raise RpcError(METHOD_NOT_FOUND, f"the chat pane may not ask {params.get('method')!r}")
        if method == "ide.attach" and (params.get("address") or params.get("token")):
            raise RpcError(INVALID_PARAMS, "the chat pane attaches by session, not by address")
        return await handle(method, params)

    return handler


class Panes:
    """Counts the running pane bridges, so that a browser cannot start any number of them."""

    def __init__(self, limit: int = MAX_PANES) -> None:
        """Start with none.

        Args:
            limit: How many may run at the same time.
        """
        self.limit = limit
        self.running = 0


CONFIRM_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>calu chat</title>
<link href="/static/pane/theme.css" rel="stylesheet">
<link href="/static/pane/pane.css" rel="stylesheet">
</head>
<body>
<main>
<h1>Open the calu chat?</h1>
<p>Another web site opened this page. The chat connects to Calu only when you open it here.</p>
<p id="target"></p>
<p><a id="go" href="/pane?confirmed=1">Open the chat</a></p>
</main>
<script src="/static/pane-confirm.js"></script>
</body>
</html>
"""
"""What `pane_page` serves to a top-level visit that another site started: the folder and the
session it asks for, and a link that loads the pane from this origin."""


async def pane_page(request: Request) -> Response:
    """Serve the pane's page: the stylesheets, an empty `#app` and the script.

    The page holds no data; everything arrives over `PANE_RPC_PATH`. `SecurityHeaders` gives it
    the `frame-ancestors` of `web.pane_frame_ancestors`.

    A top-level visit that another web site started (`Sec-Fetch-Dest: document` and
    `Sec-Fetch-Site: cross-site` or `same-site`, e.g. `window.open` with a `#dir=...&session=...`
    of its choosing) gets `CONFIRM_PAGE` instead: the pane would connect with the browser's
    login at once and start its work there (attach, status checks, file watching). A frame in
    an application that `web.pane_frame_ancestors` allows, an address the person opened or
    typed, and a link of this interface get the pane.

    Args:
        request: The request; its `Sec-Fetch-*` headers are read.

    Returns:
        The HTML page.
    """
    headers = request.headers
    if headers.get("sec-fetch-dest") == "document" and headers.get("sec-fetch-site") in (
        "cross-site",
        "same-site",
    ):
        return HTMLResponse(CONFIRM_PAGE, headers={"cache-control": "no-store"})
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>calu chat</title>
<link href="/static/pane/theme.css" rel="stylesheet">
<link href="/static/pane/chat.css" rel="stylesheet">
<link href="/static/pane/pane.css" rel="stylesheet">
</head>
<body>
<div id="app"></div>
<script src="/static/pane/pane.js"></script>
</body>
</html>
"""
    return HTMLResponse(page, headers={"cache-control": "no-store"})


async def pane_socket(websocket: WebSocket, store: CredentialStore | None, panes: Panes) -> None:
    """Run one bridge for one browser tab until either side ends.

    The WebSocket carries one JSON-RPC message per text frame in both directions, the lines of
    `calu ide` without their newline. At the end the bridge detaches its session (which keeps
    running) and revokes a minted token, as `calu ide` does when its editor goes away.

    Args:
        websocket: The browser's connection; `TokenGate` has checked its login.
        store: The credential store of this process; None (in-process tests) refuses the pane.
        panes: The count of running pane bridges.
    """
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="calu" if "calu" in offered else None)
    try:
        workspace = pane_workspace(websocket.query_params.get("dir"))
    except ValueError as exc:
        await websocket.close(code=1008, reason=str(exc)[:120])
        return
    if store is None:
        await websocket.close(code=1011, reason="this calu web has no credential store")
        return
    if panes.running >= panes.limit:
        await websocket.close(code=1013, reason=f"{panes.limit} chat panes are open; close one")
        return
    panes.running += 1
    try:
        await _serve(websocket, workspace, store)
    finally:
        panes.running -= 1
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


async def _serve(websocket: WebSocket, workspace: Path, store: CredentialStore) -> None:
    """Connect a bridge to the WebSocket (the body of `calu.ide.bridge.serve`, with the allowlist).

    Args:
        websocket: The accepted connection.
        workspace: The bridge's workspace folder.
        store: The credential store.
    """

    async def read_line() -> str:
        """Read the next message of the browser.

        Returns:
            The message with a newline, or "" when the browser closed the connection.
        """
        try:
            return await websocket.receive_text() + "\n"
        except WebSocketDisconnect, RuntimeError, KeyError:
            return ""

    async def write_line(line: str) -> None:
        """Send one message to the browser; a closed connection drops it.

        Args:
            line: The JSON message, without a newline.
        """
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_text(line)

    async with anyio.create_task_group() as tasks:
        bridge = Bridge(workspace, store, tasks)
        peer = Peer(read_line, write_line, checked_handler(bridge.handle))
        peer.after_response = bridge.after_response
        bridge.peer = peer
        tasks.start_soon(peer.serve)
        await first_set(peer.closed, bridge.shutdown)
        with anyio.move_on_after(DRAIN_S):
            await peer.idle()
        await bridge.close()
        tasks.cancel_scope.cancel()
