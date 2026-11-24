"""The chat pane in the window layouts it is meant for, with a session attached (PLAN.md D261).

A scripted session (no database, no Claude Code) serves its protocol on a token-free local
socket, where the pane's bridge finds it as the session of the project folder. The pane is
shown in an iframe of a page of another site, laid out like a notebook application:

    ┌──────────────────────┬──────────────────────┐
    │                      │      other pane      │   2/3 of the height
    │      notebook        ├──────────────────────┤
    │                      │   calu chat pane     │   1/3 of the height
    └──────────────────────┴──────────────────────┘

at the sizes of a 2560x1440 monitor and of MacBook Pro 16" and 14" displays (their default
scaled resolutions), as a narrow side bar, and in its own window. Each layout runs a turn that
escalates, decides it, opens the session menu, a file and a diff, and follows the settings
link, which must open the full web interface in another window, not inside the frame.

`CALU_PANE_SCREENSHOTS=<dir>` saves a picture of each state for a person to look at.
"""

import functools
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.browser.conftest import serve

WEB_EXTRA = "the web interface's packages are missing: uv sync"
pytest.importorskip("starlette", reason=WEB_EXTRA)
pytest.importorskip("jinja2", reason=WEB_EXTRA)

from starlette.applications import Starlette  # ruff: ignore[module-import-not-at-top-of-file] (after the guard above)
from starlette.middleware import Middleware  # ruff: ignore[module-import-not-at-top-of-file]
from starlette.responses import HTMLResponse  # ruff: ignore[module-import-not-at-top-of-file]
from starlette.routing import Mount, Route, WebSocketRoute  # ruff: ignore[module-import-not-at-top-of-file]
from starlette.staticfiles import StaticFiles  # ruff: ignore[module-import-not-at-top-of-file]

from calu import localsocket  # ruff: ignore[module-import-not-at-top-of-file]
from calu.bus import Bus  # ruff: ignore[module-import-not-at-top-of-file]
from calu.checkers.rules import Rules  # ruff: ignore[module-import-not-at-top-of-file]
from calu.claude import FakeRunner, FakeScript  # ruff: ignore[module-import-not-at-top-of-file]
from calu.config import CheckerConfig, Config  # ruff: ignore[module-import-not-at-top-of-file]
from calu.credentials import CredentialStore  # ruff: ignore[module-import-not-at-top-of-file]
from calu.defaults import default_instructions  # ruff: ignore[module-import-not-at-top-of-file]
from calu.protocol.server import SessionServer  # ruff: ignore[module-import-not-at-top-of-file]
from calu.session import Session  # ruff: ignore[module-import-not-at-top-of-file]
from calu.setup import CHECKER_DEFAULTS, recommended_rules  # ruff: ignore[module-import-not-at-top-of-file]
from calu.store import CheckerDefinition, MemorySessionStore, ProjectSettings  # ruff: ignore[module-import-not-at-top-of-file]
from calu.testing import fake_models  # ruff: ignore[module-import-not-at-top-of-file]
from calu.web.app import HERE, SecurityHeaders, TokenGate  # ruff: ignore[module-import-not-at-top-of-file]
from calu.web.pane import (  # ruff: ignore[module-import-not-at-top-of-file]
    PANE_PATH,
    PANE_RPC_PATH,
    Panes,
    frame_ancestors,
    pane_page,
    pane_socket,
)

pytestmark = [pytest.mark.browser, pytest.mark.anyio]

TOKEN = "pane-layout-token"

GREETER = "def greet(name):\n    return 'Hello, ' + name\n\n\nprint(greet('world'))\n"

TURNS = [
    [
        {"text": "I look at the project first. The entry point is greeter.py:1."},
        {
            "tool": "Bash",
            "input": {"command": "ls", "description": "List the files"},
            "result": "greeter.py",
        },
        {
            "tool": "Edit",
            "input": {
                "file_path": "greeter.py",
                "old_string": "'Hello, ' + name",
                "new_string": "f'Hello, {name}!'",
            },
            "writes": {"greeter.py": GREETER.replace("'Hello, ' + name", "f'Hello, {name}!'")},
        },
        {
            "tool": "Bash",
            "input": {"command": "git push origin main  # calu:escalate", "description": "Push"},
            "result": "pushed",
        },
        {"text": "Done: greeter.py now uses an f-string, and the change is pushed."},
    ],
    *[[{"text": "Echo."}]] * 10,
]

LAYOUTS = {
    # name: (viewport width, height, device scale factor, where the pane sits)
    "monitor-2560x1440": (2560, 1300, 1, "corner"),
    "macbook-pro-16": (1728, 1000, 2, "corner"),
    "macbook-pro-14": (1512, 860, 2, "corner"),
    "side-bar": (1920, 1000, 1, "side"),
    "own-window": (1280, 800, 1, "window"),
}
"""The browser's inner size (the screen minus the browser's own bars) and how the pane is shown.
MacBook Pro 16" and 14" use their default scaled resolutions (1728x1117 and 1512x982 points)."""

OUTER = """<!DOCTYPE html>
<html><head><title>notebook</title><style>
  html, body {{ margin: 0; height: 100%; font-family: sans-serif; }}
  .app {{ display: grid; height: 100vh; {columns} }}
  .notebook {{ background: #f3f3f3; padding: 12px; border-right: 1px solid #ccc; }}
  .right {{ display: grid; grid-template-rows: {rows}; min-height: 0; }}
  .other {{ background: #fafafa; padding: 12px; border-bottom: 1px solid #ccc; }}
  iframe {{ border: 0; width: 100%; height: 100%; display: block; }}
</style></head><body><div class="app">
  <div class="notebook">Notebook: In [1]: import pandas as pd</div>
  <div class="right"><div class="other">Other pane (plots, a terminal)</div>
  <iframe id="chat" src="{src}" title="calu chat"></iframe></div>
</div></body></html>
"""


def settings(workspace: Path) -> ProjectSettings:
    """The settings of a fresh `calu setup`, for a session without a database.

    Args:
        workspace: The project directory.

    Returns:
        The enabled built-in checkers with the fake model, the recommended rules, defaults.
    """
    checkers = {
        name: CheckerDefinition(
            name=name,
            route=route,
            instructions=default_instructions(name),
            model="test:model",
            request_limit=request_limit or CheckerConfig.model_fields["request_limit"].default,
            output_tokens_limit=CheckerConfig.model_fields["output_tokens_limit"].default,
            max_tokens=CheckerConfig.model_fields["max_tokens"].default,
            version=1,
        )
        for name, route, _, request_limit, enabled in CHECKER_DEFAULTS
        if enabled
    }
    rules = Rules.from_rows(
        workspace, ((r.list, r.pattern, r.position) for r in recommended_rules())
    )
    return ProjectSettings(config=Config(), checkers=checkers, rules=rules)


@asynccontextmanager
async def scripted_session(project: Path) -> AsyncGenerator[str]:
    """Run a scripted session of `project` on a token-free local socket (`--insecure`).

    Args:
        project: The project directory.

    Yields:
        The session's id; the session runs until the block ends.
    """
    bus = Bus()
    script = FakeScript(turns=[list(turn) for turn in TURNS])
    store = MemorySessionStore()
    session = Session(
        project,
        settings(project),
        store,
        lambda: FakeRunner(script, project),
        bus.publish,
        checker_models=fake_models(),
        snapshot_root=project.parent / "snapshots",
    )
    async with anyio.create_task_group() as tasks:
        await session.start(tasks)
        server = SessionServer(session, bus, "unused", tasks, insecure=True)
        path = localsocket.path_of(str(store.session_id), localsocket.user_root())
        listener = localsocket.bind(path, owner=os.geteuid())
        tasks.start_soon(server.listen_local, listener, path)
        try:
            yield str(store.session_id)
        finally:
            await session.stop()
            tasks.cancel_scope.cancel()


def pane_app(store: CredentialStore) -> Starlette:
    """The pane behind the guards of `calu web`, and a stand-in for the full web interface.

    Args:
        store: The credential store of the pane's bridges.

    Returns:
        The application; `/settings` answers "full web interface: settings".
    """
    return Starlette(
        routes=[
            Route(PANE_PATH, pane_page),
            WebSocketRoute(
                PANE_RPC_PATH, functools.partial(pane_socket, store=store, panes=Panes())
            ),
            Route(
                "/settings",
                lambda request: HTMLResponse("<title>settings</title>full web interface: settings"),
            ),
            Mount("/static", StaticFiles(directory=str(HERE / "static"))),
        ],
        middleware=[
            Middleware(SecurityHeaders, pane_ancestors=frame_ancestors(None)),
            Middleware(TokenGate, token=TOKEN),
        ],
    )


@pytest.fixture
async def browser_context() -> AsyncIterator[Any]:
    """Open a headless Chromium and hand out contexts of a chosen size, or skip without one.

    Yields:
        `make(**options)`: a new browser context (Playwright's `new_context` options).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - the dev group has it
        pytest.skip("playwright is not installed; run scripts/get-browser.sh")
    libs = Path(os.environ.get("CALU_BROWSER_LIBS", Path.home() / ".cache" / "calu-browser-libs"))
    lib = libs / "root" / "usr" / "lib" / "x86_64-linux-gnu"
    environment: dict[str, str | float | bool] = dict(os.environ)
    if lib.is_dir():
        environment["LD_LIBRARY_PATH"] = f"{lib}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(env=environment)
        except Exception as exc:  # the browser or its libraries are missing
            pytest.skip(f"no headless browser ({type(exc).__name__}); run scripts/get-browser.sh")
        try:
            yield browser.new_context
        finally:
            await browser.close()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """A project with a file, its own runtime directory and no database of this machine.

    Returns:
        The project directory.
    """
    from calu.db import place

    monkeypatch.setattr(place, "PATH", tmp_path / "no-place.json")
    monkeypatch.delenv(place.PATH_ENV, raising=False)
    # A short path of its own: a Unix socket path may have at most 107 bytes, and pytest's
    # temporary paths of parallel workers are longer.
    runtime = Path(tempfile.mkdtemp(prefix="calu-pane-"))
    request.addfinalizer(lambda: shutil.rmtree(runtime, ignore_errors=True))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    project = tmp_path / "project"
    project.mkdir()
    (project / "greeter.py").write_text(GREETER)
    return project


class Shots:
    """Saves screenshots when `CALU_PANE_SCREENSHOTS` names a directory."""

    def __init__(self, layout: str) -> None:
        """Remember the layout's name.

        Args:
            layout: Prefix of the file names.
        """
        directory = os.environ.get("CALU_PANE_SCREENSHOTS")
        self.directory = Path(directory) if directory else None
        self.layout = layout

    async def take(self, page: Any, state: str) -> None:
        """Save the whole window.

        Args:
            page: The Playwright page.
            state: Suffix of the file name.
        """
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(self.directory / f"{self.layout}-{state}.png"))


async def fits(frame: Any, selector: str) -> None:
    """Check that an element is inside the pane's visible area (nothing cut off).

    Args:
        frame: The pane's frame.
        selector: The element.
    """
    box = await frame.evaluate(
        """(selector) => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                    width: innerWidth, height: innerHeight,
                    scroll: document.documentElement.scrollWidth};
        }""",
        selector,
    )
    assert box["top"] >= 0 and box["bottom"] <= box["height"] + 1, (selector, box)
    assert box["left"] >= 0 and box["right"] <= box["width"] + 1, (selector, box)
    assert box["scroll"] <= box["width"] + 1, f"the pane scrolls sideways: {box}"


@pytest.mark.parametrize("layout", list(LAYOUTS))
async def test_the_pane_works_in_each_layout(
    browser_context: Any, world: Path, layout: str
) -> None:
    width, height, scale, where = LAYOUTS[layout]
    context = await browser_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    shots = Shots(layout)
    async with (
        scripted_session(world),
        serve(pane_app(CredentialStore(world.parent / "c.json"))) as base,
        # Last, so that the outer page's server stops first.
        AsyncExitStack() as stack,
    ):
        port = base.rsplit(":", 1)[1]
        src = f"http://127.0.0.1:{port}/pane#token={TOKEN}&dir={world}"
        if where == "window":
            await page.goto(src)
            frame: Any = page
        else:
            columns = (
                "grid-template-columns: 1fr 1fr;"
                if where == "corner"
                else "grid-template-columns: 1fr 420px;"
            )
            rows = "2fr 1fr" if where == "corner" else "0fr 1fr"
            outer = Starlette(
                routes=[
                    Route(
                        "/",
                        lambda request: HTMLResponse(
                            OUTER.format(columns=columns, rows=rows, src=src)
                        ),
                    )
                ]
            )
            outer_base = await stack.enter_async_context(serve(outer))
            await page.goto(outer_base.replace("127.0.0.1", "localhost") + "/")
            await page.locator("#chat").wait_for()
            frame = next(f for f in page.frames if "/pane" in f.url)
        await frame.wait_for_selector('[data-testid="input-text"]', timeout=20_000)
        # The bridge finds the session of the project folder by its local socket.
        await frame.wait_for_function(
            "() => document.body.innerText.includes('project')", timeout=20_000
        )
        await shots.take(page, "1-attached")
        await fits(frame, '[data-testid="input-text"]')

        await frame.fill(
            '[data-testid="input-text"]', "Please use an f-string in greeter.py and push."
        )
        await frame.press('[data-testid="input-text"]', "Enter")
        # The push escalates: the decision card is there, with its buttons inside the pane.
        allow = frame.get_by_role("button", name="Allow").first
        await allow.wait_for(timeout=20_000)
        await allow.scroll_into_view_if_needed()
        await shots.take(page, "2-escalated")
        box = await allow.bounding_box()
        assert box is not None and box["height"] >= 16 and box["width"] >= 30
        await allow.click()
        await frame.wait_for_function(
            "() => document.body.innerText.includes('now uses an f-string')", timeout=20_000
        )
        await shots.take(page, "3-decided")

        # A click on the edit's row shows its diff, in a dialog that fits the pane.
        await frame.get_by_text("Edit", exact=True).first.click()
        dialog = frame.locator(".pane-dialog")
        await dialog.wait_for(timeout=10_000)
        text = await dialog.inner_text()
        assert "+    return f'Hello, {name}!'" in text, text
        await shots.take(page, "4-diff")
        await fits(frame, ".pane-dialog")
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="detached")

        # The source reference in Claude's text opens the file at its line.
        link = frame.locator("a", has_text="greeter.py:1").first
        if await link.count():
            await link.click()
            await frame.locator(".pane-dialog").wait_for(timeout=10_000)
            assert "Hello" in await frame.locator(".pane-dialog").inner_text()
            await shots.take(page, "5-file")
            await fits(frame, ".pane-dialog")
            await page.keyboard.press("Escape")

        # The session menu fits as well.
        await frame.click('[data-testid="session-dropdown"]')
        await anyio.sleep(0.5)
        await shots.take(page, "6-session-menu")
        await page.keyboard.press("Escape")

        # Settings open the full web interface in another window, never inside the frame.
        async with context.expect_page() as opened:
            await frame.click('[data-testid="header-settings"]')
        settings_page = await opened.value
        # The pane passes its token on: the window logs in and goes on to /settings.
        await settings_page.wait_for_url("**/settings", timeout=10_000)
        await settings_page.wait_for_load_state()
        assert TOKEN not in settings_page.url
        assert "full web interface" in await settings_page.inner_text("body")
        assert "/pane" in frame.url, "the pane stays where it is"
    assert errors == [], errors


async def test_a_sessions_list_link_attaches_the_pane_to_its_session(
    browser_context: Any, world: Path
) -> None:
    """D271: the "chat" link of `calu web`'s sessions list names the session
    (`#session=<id>`). The pane attaches to that session first, also when the folder in the
    address is another one: the workspace it then shows is the session's own."""
    context = await browser_context(viewport={"width": 900, "height": 700})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    elsewhere = world.parent / "elsewhere"
    elsewhere.mkdir()
    async with (
        scripted_session(world) as session_id,
        serve(pane_app(CredentialStore(world.parent / "c.json"))) as base,
    ):
        await page.goto(f"{base}/pane#token={TOKEN}&dir={elsewhere}&session={session_id}")
        await page.wait_for_selector('[data-testid="input-text"]', timeout=20_000)
        await page.wait_for_function(
            "() => document.body.innerText.includes('project')", timeout=20_000
        )
        assert f"session={session_id}" in page.url, "the session stays in the address"
    assert errors == [], errors
