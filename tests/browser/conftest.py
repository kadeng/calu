"""A real browser and a real HTTP server for the web interface tests.

The tests skip unless the browser is installed: `scripts/get-browser.sh` does that without
root. They are marked `browser` as well as `db`, so the default test run never needs either.
"""

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

LIBS = Path(os.environ.get("CALU_BROWSER_LIBS", Path.home() / ".cache" / "calu-browser-libs"))
"""Where `scripts/get-browser.sh` unpacked libnss3 and libnspr4."""

SETUP = "run scripts/get-browser.sh (it needs no root)"


@pytest.fixture
async def page() -> AsyncIterator[Any]:
    """Open one page in a headless Chromium, or skip if the browser is missing."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - the dev group has it
        pytest.skip(f"playwright is not installed; {SETUP}")
    libs = LIBS / "root" / "usr" / "lib" / "x86_64-linux-gnu"
    environment: dict[str, str | float | bool] = dict(os.environ)
    if libs.is_dir():
        environment["LD_LIBRARY_PATH"] = f"{libs}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(env=environment)
        except Exception as exc:  # the browser or its libraries are missing
            pytest.skip(f"no headless browser ({type(exc).__name__}); {SETUP}")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@asynccontextmanager
async def serve(app: Any) -> AsyncGenerator[str]:
    """Run an ASGI application on a free local port for the duration of the block.

    Args:
        app: The application.

    Yields:
        Its base URL.
    """
    import uvicorn

    # No lifespan: the application has no startup or shutdown handlers, and leaving it on makes
    # the shutdown log a cancelled lifespan receive.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.serve)
        with anyio.fail_after(20):
            while not server.started:
                await anyio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
