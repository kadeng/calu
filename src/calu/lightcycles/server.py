"""The local web server of `calu lightcycles`: static files on the loopback interface.

    serve(port, ...) --> bind 127.0.0.1 (the asked port, else the usual one, else a free one)
                     --> print the address --> open the browser, if there is one
                     --> uvicorn serves `create_app()` until Ctrl-C

The application has two routes and no state:

    GET /            the page (static/index.html)
    GET /static/*    its scripts and its stylesheet

Every response carries a Content-Security-Policy that allows scripts and styles from this
server only, no inline script, and no request to anywhere else.
"""

import socket
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parent / "static"
"""The directory of the page and its files, inside the package."""

HOST = "127.0.0.1"
"""The only address the server binds: the game is for the person at this machine."""

USUAL_PORT = 8817
"""The port tried first when none is asked for. A browser keeps the save games per address,
so the same port on every start keeps them."""

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
"""The policy of every response: scripts, styles and images from this server only; no inline
script or style; no request from the page to anywhere; the page cannot be framed."""


class SecurityHeaders:
    """Add the browser security headers to every HTTP response.

    Content-Security-Policy   `CONTENT_SECURITY_POLICY`
    X-Content-Type-Options    nosniff
    Referrer-Policy           no-referrer
    Cache-Control             no-store (a new version of calu serves new files at once)
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
        extra = [
            (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"cache-control", b"no-store"),
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


async def _page(request: Request) -> FileResponse:
    """Answer `GET /` with the game's page.

    Args:
        request: The request (unused).

    Returns:
        `static/index.html`.
    """
    return FileResponse(STATIC / "index.html", media_type="text/html")


def create_app() -> Any:
    """Build the application that serves the game.

    Returns:
        The ASGI application, ready for uvicorn: the two routes, wrapped in `SecurityHeaders`.
    """
    app = Starlette(
        routes=[
            Route("/", _page),
            Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
        ]
    )
    return SecurityHeaders(app)


def bind(port: int | None) -> socket.socket:
    """Open the listening socket on the loopback interface.

    Args:
        port: The port to bind, 1 to 65535; None tries `USUAL_PORT` and, if that is taken,
            lets the system choose a free port.

    Returns:
        The bound, listening socket; `getsockname()[1]` is its port. The caller closes it
        or hands it to the server.

    Raises:
        OSError: If the asked port cannot be bound (taken, or not permitted).
        ValueError: If `port` is not None and not 1 to 65535.
    """
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"--port takes a number from 1 to 65535, not {port}")
    candidates = [port] if port is not None else [USUAL_PORT, 0]
    while True:
        candidate = candidates.pop(0)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, candidate))
            # Listening at once: a port another server listens on is refused here, not later.
            listener.listen()
        except OSError:
            listener.close()
            if not candidates:
                raise
            continue
        return listener


def address(port: int, options: dict[str, int]) -> str:
    """Give the address of the page.

    Args:
        port: The server's port.
        options: What the page is to start with (`players`, `bots`, `speed`, `seed`), each
            a whole number; an empty dict gives the plain address.

    Returns:
        E.g. `http://127.0.0.1:8817/?players=1&bots=3`.
    """
    query = f"?{urlencode(options)}" if options else ""
    return f"http://{HOST}:{port}/{query}"


def open_in_browser(url: str) -> bool:
    """Ask the desktop to open an address in the default browser.

    Args:
        url: The address.

    Returns:
        True if a browser was asked to open it; False if there is none or asking failed.
        Never raises: a machine without a browser still gets the printed address.
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _say(line: str) -> None:
    """Print one line and flush it: the address must show at once, also through a pipe.

    Args:
        line: The line.
    """
    print(line, flush=True)


def serve(
    port: int | None = None,
    browser: bool = True,
    options: dict[str, int] | None = None,
    say: Callable[[str], None] = _say,
    open_url: Callable[[str], bool] = open_in_browser,
) -> None:
    """Serve the game until the process is interrupted (Ctrl-C).

    Side effects: binds a port on 127.0.0.1, prints the address, asks the desktop to open
    it, and blocks while uvicorn serves. Writes no file.

    Args:
        port: The port, or None for `USUAL_PORT` or a free one.
        browser: False leaves the browser alone and only prints the address.
        options: What the page is to start with, see `address`.
        say: Prints one line for the user.
        open_url: Opens an address in a browser and says whether that worked.

    Raises:
        OSError: If the asked port cannot be bound.
        ValueError: If the port is out of range.
    """
    import uvicorn

    listener = bind(port)
    url = address(listener.getsockname()[1], options or {})
    say(f"MetaTron for the browser \N{EM DASH} after MetaTron by Kai Londenberg: {url}")
    if browser and not open_url(url):
        say("no browser could be opened here; open the address yourself")
    say("Ctrl-C stops the server")
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False, lifespan="off")
    try:
        uvicorn.Server(config).run(sockets=[listener])
    except KeyboardInterrupt:
        # uvicorn has shut down and raises the interrupt again; Ctrl-C is how this ends.
        pass
    finally:
        listener.close()
