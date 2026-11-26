"""Session names, the turn that still runs, and two editors of one checker's instructions."""

import httpx
import pytest

from calu import admin, herd
from calu.db import Db, connect
from calu.setup import setup
from calu.store import DatabaseSessionStore, StepDecision, StepRow
from calu.web.app import create_app
from tests.db.test_web import IDENTITY, client_for, populate

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_a_session_is_named_from_its_page_and_the_name_is_shown_everywhere(
    db_url: str,
) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        async with await client_for(db) as client:
            renamed = await client.post(
                f"/sessions/{session_id}/name", data={"name": "  payment refactoring "}
            )
            assert renamed.status_code == 200 and "renamed=1" in str(renamed.url)
            assert 'value="payment refactoring"' in renamed.text
            assert "<title>payment refactoring · session · calu</title>" in renamed.text
            row = await herd.session(db, session_id)
            assert row is not None and row.name == "payment refactoring"

            fleet = await client.get("/")
            assert "payment refactoring" in fleet.text
            assert (await client.get("/?q=PAYMENT")).text.count(session_id) >= 1
            assert "No session matches" in (await client.get("/?q=nothing-like-it")).text
            listed = (await client.get("/api/sessions")).json()["sessions"]
            assert [entry["name"] for entry in listed] == ["payment refactoring"]
            live = await client.get(f"/sessions/{session_id}/live")
            assert "<title>payment refactoring · live · calu</title>" in live.text
            assert 'data-title="payment refactoring"' in live.text

            # A refused name changes nothing; "" takes the name away.
            long = await client.post(f"/sessions/{session_id}/name", data={"name": "x" * 81})
            assert long.status_code == 400 and "at most 80 characters" in long.text
            cleared = await client.post(f"/sessions/{session_id}/name", data={"name": ""})
            assert cleared.status_code == 200
            row = await herd.session(db, session_id)
            assert row is not None and row.name == ""
            unknown = "00000000-0000-0000-0000-000000000000"
            missing = await client.post(f"/sessions/{unknown}/name", data={"name": "x"})
            assert missing.status_code == 404
    finally:
        await conn.close()


async def test_the_steps_of_a_turn_that_still_runs_are_on_the_session_page(db_url: str) -> None:
    """A step carries its turn's number at once, the turn is recorded when it ends."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)  # turn 1 is recorded
        store = DatabaseSessionStore(db)
        store.session_id = session_id
        step = await store.add_step(
            StepRow(seq=3, tool_name="Bash", tool_input={"command": "make deploy"}, turn=2)
        )
        await store.decide_step(
            step, StepDecision(decision="allow", reason="", summary="Deploys the build.")
        )
        async with await client_for(db) as client:
            for detail in ("compact", "detailed", "full"):
                page = await client.get(f"/sessions/{session_id}?detail={detail}")
                assert "Steps of the turn that is running" in page.text
                assert "Deploys the build." in page.text
            assert "make deploy" in page.text  # the full level shows the input
            # The page leads to the live view, which the session offers (it has an address).
            assert "Open the live view" in page.text
    finally:
        await conn.close()


async def test_a_stale_page_does_not_overwrite_instructions_saved_meanwhile(
    db_url: str,
) -> None:
    """Two editors of one checker: the second save is refused once, with its text kept."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        before = next(row for row in await admin.checkers(db) if row.name == "edit")
        async with await client_for(db) as client:
            page = await client.get("/settings")
            assert f'name="version:prompt:edit" value="{before.version}"' in page.text
            # Someone else saves first (another tab, `calu checker edit`, an editor).
            await admin.set_checker(db, "edit", instructions_text="The other editor's text.")
            late = await client.post(
                "/settings",
                data={"prompt:edit": "My text.", "version:prompt:edit": str(before.version)},
            )
            assert late.status_code == 400
            assert "changed by someone else meanwhile" in late.text
            assert "My text." in late.text  # kept in the editor
            assert (await admin.instructions(db, "edit")).strip() == "The other editor's text."
            # The page that reports the refusal carries the stored version: saving again is
            # the deliberate overwrite.
            stored = next(row for row in await admin.checkers(db) if row.name == "edit")
            assert f'name="version:prompt:edit" value="{stored.version}"' in late.text
            again = await client.post(
                "/settings",
                data={"prompt:edit": "My text.", "version:prompt:edit": str(stored.version)},
            )
            assert again.status_code == 200
            assert (await admin.instructions(db, "edit")).strip() == "My text."
    finally:
        await conn.close()


async def test_the_page_of_an_ended_session_offers_no_live_view(db_url: str) -> None:
    """The row keeps its address, but nothing listens there any more."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_address("ws://127.0.0.1:1")
        await store.end("stopped")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(db)), base_url="http://127.0.0.1"
        ) as client:
            page = await client.get(f"/sessions/{session_id}")
            assert "there is no live view, only this record" in page.text
            assert "Open the live view" not in page.text
    finally:
        await conn.close()


async def test_the_live_view_of_an_ended_session_goes_on_or_offers_to_continue(
    db_url: str,
) -> None:
    """The owner could resume only from the history page: "live view" showed nothing to do."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        ended = DatabaseSessionStore(db)
        ended_id = await ended.start(IDENTITY)
        await ended.set_claude_session("claude-1", "haiku")
        await ended.set_address("ws://127.0.0.1:1")
        await ended.end("stopped")
        blank = DatabaseSessionStore(db)
        blank_id = await blank.start(IDENTITY)
        await blank.end("stopped")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(db)), base_url="http://127.0.0.1"
        ) as client:
            page = await client.get(f"/sessions/{ended_id}/live")
            assert page.status_code == 200 and "Continue this conversation" in page.text
            assert f'action="http://127.0.0.1/sessions/{ended_id}/request"' in page.text
            nothing = await client.get(f"/sessions/{blank_id}/live")
            assert "nothing to continue" in nothing.text

            # Once another session continues the conversation, the old live view leads there.
            later = DatabaseSessionStore(db)
            later_id = await later.start(IDENTITY)
            await later.set_claude_session("claude-1", "haiku")
            await later.set_address("ws://127.0.0.1:2")
            moved = await client.get(f"/sessions/{ended_id}/live")
            assert moved.status_code == 303
            assert moved.headers["location"].endswith(f"/sessions/{later_id}/live")
    finally:
        await conn.close()


async def test_an_answer_never_redirects_to_another_site(db_url: str) -> None:
    """`back` travels through the browser, so only this interface's own pages are accepted."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, escalation = await populate(db)
        app = create_app(db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            away = await client.post(
                f"/escalations/{escalation}/resolve",
                data={"decision": "deny", "back": "https://example.org/x"},
            )
            assert away.status_code == 303
            assert away.headers["location"] == "http://127.0.0.1/escalations"
            page = f"http://127.0.0.1/sessions/{session_id}?detail=full"
            own = await client.post(
                f"/escalations/{escalation}/resolve", data={"decision": "deny", "back": page}
            )
            assert own.headers["location"] == page
            for back in ("//example.org/x", "/escalations"):
                answer = await client.post(
                    f"/escalations/{escalation}/resolve", data={"decision": "deny", "back": back}
                )
                assert answer.headers["location"].endswith("/escalations")
    finally:
        await conn.close()
