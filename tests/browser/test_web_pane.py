"""The chat pane of `calu web` in a real browser (agent/records/PLAN.md D261), without a database.

The pane's script is the committed bundle (`src/calu/web/static/pane/pane.js`). Each test
serves the pane's routes behind the real `SecurityHeaders` and `TokenGate`, with a bridge per
browser tab; with no database the bridge starts and answers, and the pane shows that no session
is attached. One test opens the pane in its own window, one inside an iframe of a page of
another site, which gets no cookie of a sign-in: there the pane asks for the password (D279).
"""

import functools
from pathlib import Path
from typing import Any

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

from calu.credentials import CredentialStore  # ruff: ignore[module-import-not-at-top-of-file]
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

PASSWORD = "pane-browser-pässword"


async def is_password(typed: str) -> bool:
    """The password check of the gate: `PASSWORD` is the password.

    Args:
        typed: What the pane sent.

    Returns:
        Whether it is `PASSWORD`.
    """
    return typed == PASSWORD


async def sign_in(frame: Any, password: str, prompt: str = "The password of calu web") -> None:
    """Answer the pane's password dialog.

    Args:
        frame: The page or the frame that shows the pane.
        password: What to type.
        prompt: Text the dialog shows.
    """
    await frame.wait_for_selector(f".pane-detail:has-text('{prompt}')", timeout=20_000)
    await frame.fill(".pane-input", password)
    await frame.press(".pane-input", "Enter")


@pytest.fixture(autouse=True)
def no_machine_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the pane's bridge away from the database of the machine the tests run on.

    The bridge finds a database through the root place file (`calu.db.place.PATH`) too, so a
    developer machine with a root installation would be asked; the tests want none.
    """
    from calu.db import place

    monkeypatch.setattr(place, "PATH", tmp_path / "no-place.json")
    monkeypatch.delenv(place.PATH_ENV, raising=False)


def pane_app(store: CredentialStore) -> Starlette:
    """The pane's routes behind the guards of `calu web` (no database, no other page).

    Args:
        store: The credential store of the pane's bridges.

    Returns:
        The application.
    """
    return Starlette(
        routes=[
            Route(PANE_PATH, pane_page),
            WebSocketRoute(
                PANE_RPC_PATH, functools.partial(pane_socket, store=store, panes=Panes())
            ),
            Mount("/static", StaticFiles(directory=str(HERE / "static"))),
        ],
        middleware=[
            Middleware(SecurityHeaders, pane_ancestors=frame_ancestors(None)),
            Middleware(TokenGate, check_password=is_password),
        ],
    )


async def wait_for_chat(frame: Any) -> None:
    """Wait until the chat's input is there and the bridge has answered.

    Args:
        frame: The page or the frame that shows the pane.
    """
    await frame.wait_for_selector('[data-testid="input-text"]', timeout=20_000)
    # The bridge's status reaches the view: "Starting calu…" goes away once it runs.
    # A locator, not a string predicate: the pane's policy refuses `eval` in a frame.
    await frame.get_by_text("Starting calu").first.wait_for(state="hidden", timeout=20_000)


async def test_the_pane_in_its_own_window_asks_for_the_password_and_connects(
    page: Any, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    project = tmp_path / "project"
    project.mkdir()
    async with serve(pane_app(CredentialStore(tmp_path / "credentials.json"))) as base:
        # An address of the time when `calu web` printed a token: the token leaves it.
        await page.goto(f"{base}/pane#token=old&dir={project}")
        await sign_in(page, PASSWORD)
        await wait_for_chat(page)
        assert "token" not in page.url and "dir=" in page.url
        assert await page.title() == "calu chat"
        # The session list needs the database: the pane says so in a toast instead of failing.
        await page.click('[data-testid="session-dropdown"]')
        await page.wait_for_selector(".pane-toast, .menu", timeout=10_000)
    assert errors == [], errors


async def test_the_pane_runs_in_an_iframe_of_another_site(page: Any, tmp_path: Path) -> None:
    """A page on "localhost" frames the pane served as "127.0.0.1": cross-site, no cookie."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    async with serve(pane_app(CredentialStore(tmp_path / "credentials.json"))) as base:
        port = base.rsplit(":", 1)[1]
        frame_src = f"http://127.0.0.1:{port}/pane"
        outer = Starlette(
            routes=[
                Route(
                    "/",
                    lambda request: HTMLResponse(
                        "<!DOCTYPE html><title>notebook</title>"
                        f'<iframe id="chat" src="{frame_src}" style="width:600px;height:700px">'
                        "</iframe>"
                    ),
                )
            ]
        )
        async with serve(outer) as outer_base:
            await page.goto(outer_base.replace("127.0.0.1", "localhost") + "/")
            await page.frame_locator("#chat").locator(".pane-input").wait_for(timeout=20_000)
            inner = next(f for f in page.frames if "/pane" in f.url)
            await sign_in(inner, "a guess")
            await sign_in(inner, PASSWORD, prompt="did not accept that password")
            await wait_for_chat(inner)
    assert errors == [], errors


async def test_a_cancelled_password_leaves_the_pane_saying_how_to_sign_in(
    page: Any, tmp_path: Path
) -> None:
    async with serve(pane_app(CredentialStore(tmp_path / "credentials.json"))) as base:
        await page.goto(f"{base}/pane")
        await page.wait_for_selector(".pane-input", timeout=20_000)
        await page.press(".pane-input", "Escape")
        await page.wait_for_function(
            "document.body.innerText.includes('did not accept this pane')", timeout=20_000
        )
