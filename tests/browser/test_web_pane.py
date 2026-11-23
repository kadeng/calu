"""The chat pane of `calu web` in a real browser (agent/records/PLAN.md D261), without a database.

The pane's script is the committed bundle (`src/calu/web/static/pane/pane.js`). Each test
serves the pane's routes behind the real `SecurityHeaders` and `TokenGate`, with a bridge per
browser tab; with no database the bridge starts and answers, and the pane shows that no session
is attached. One test opens the pane in its own window, one inside an iframe of a page of
another site, which is the case the token in the address's fragment exists for.
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

TOKEN = "pane-browser-token"


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
            Middleware(TokenGate, token=TOKEN),
        ],
    )


async def wait_for_chat(frame: Any) -> None:
    """Wait until the chat's input is there and the bridge has answered.

    Args:
        frame: The page or the frame that shows the pane.
    """
    await frame.wait_for_selector('[data-testid="input-text"]', timeout=20_000)
    # The bridge's status reaches the view: "Starting calu…" goes away once it runs.
    await frame.wait_for_function(
        "!document.body.innerText.includes('Starting calu')", timeout=20_000
    )


async def test_the_pane_in_its_own_window_connects_and_hides_the_token(
    page: Any, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    project = tmp_path / "project"
    project.mkdir()
    async with serve(pane_app(CredentialStore(tmp_path / "credentials.json"))) as base:
        await page.goto(f"{base}/pane#token={TOKEN}&dir={project}")
        await wait_for_chat(page)
        assert TOKEN not in page.url, "the token is taken out of the address"
        assert "dir=" in page.url
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
        frame_src = f"http://127.0.0.1:{port}/pane#token={TOKEN}"
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
            frame = page.frame_locator("#chat")
            await frame.locator('[data-testid="input-text"]').wait_for(timeout=20_000)
            inner = next(f for f in page.frames if "/pane" in f.url)
            await wait_for_chat(inner)
            assert TOKEN not in inner.url
    assert errors == [], errors


async def test_without_the_token_the_pane_says_how_to_log_in(page: Any, tmp_path: Path) -> None:
    async with serve(pane_app(CredentialStore(tmp_path / "credentials.json"))) as base:
        await page.goto(f"{base}/pane")
        await page.wait_for_function(
            "document.body.innerText.includes('did not accept this pane')", timeout=20_000
        )
