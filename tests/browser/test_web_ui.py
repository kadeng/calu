"""The web interface in a real browser: the pages render, the forms work, the links lead on."""

import pytest

from calu.db import Db, connect
from calu.setup import setup
from calu.web.app import create_app
from tests.browser.conftest import serve
from tests.db.test_web import populate

pytestmark = [pytest.mark.browser, pytest.mark.db, pytest.mark.anyio]


async def test_an_operator_can_work_through_a_session_in_the_browser(db_url: str, page) -> None:
    """One pass through what the UI is for: find the session, read it, answer, change a prompt."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _escalation = await populate(db)
        async with serve(create_app(db)) as base:
            # The fleet page shows the session and says that something is waiting.
            await page.goto(base)
            assert "1 waiting" in await page.inner_text("header")
            assert "/w" in await page.inner_text("table")

            # Clicking "open" leads into the session.
            async with page.expect_navigation():
                await page.get_by_role("link", name="history").first.click()
            assert session_id in await page.inner_text("main")
            assert "Runs the test suite." in await page.inner_text("main")

            # The levels of detail show more and more of the same turn.
            await page.get_by_role("link", name="Full (tool inputs").click()
            full = await page.inner_text("main")
            assert "make test" in full and "2 passed" in full
            await page.get_by_role("link", name="Narrated summary").click()
            assert "## Turn 1" in await page.inner_text("main")

            # A token is minted and shown once, with the command to attach with it.
            await page.goto(f"{base}/sessions/{session_id}")
            await page.fill("input[name=label]", "zed on the laptop")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Mint a client token").click()
            shown = await page.inner_text(".token")
            assert "shown once" in shown
            token = shown.split("shown once:")[1].split()[0]
            assert f"calu attach ws://127.0.0.1:8765 --token {token}" in await page.inner_text(
                "main"
            )

            # The escalation is answered from the session page, and the page comes back without it.
            await page.fill("input[name=note]", "not today")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Deny").click()
            assert "Waiting for a decision" not in await page.inner_text("main")
            await page.goto(f"{base}/escalations")
            assert "0 escalations waiting" in await page.inner_text("h1")

            # The checker instructions are one click away from the session and save in place.
            await page.goto(f"{base}/sessions/{session_id}")
            await page.get_by_role("link", name="edit the command instructions").click()
            await page.fill("textarea", "# Command checker\n\nBe brief.")
            async with page.expect_navigation():
                await page.get_by_role("button", name="Save").click()
            assert "saved" in await page.inner_text("main")
            assert "Be brief." in await page.input_value("textarea")
    finally:
        await conn.close()


async def test_the_escalation_detail_page_shows_what_the_checker_saw(db_url: str, page) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        _session_id, escalation = await populate(db)
        async with serve(create_app(db)) as base:
            await page.goto(f"{base}/escalations/{escalation}")
            body = await page.inner_text("main")
            assert "the deny list matched" in body
            assert "sudo ls" in body  # the rendered step
            assert "## Turn 1" in body  # the conversation of that moment
    finally:
        await conn.close()
