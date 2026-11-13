"""The live view in a real browser: the stream fills, an escalation is answered from its card."""

from pathlib import Path

import pytest

from calu.checkers import CodeMark
from calu.config import HonestyConfig
from tests.db.test_web_live import running

pytestmark = [pytest.mark.browser, pytest.mark.db, pytest.mark.anyio]


async def test_the_live_view_streams_and_answers_an_escalation(
    db_url: str, tmp_path: Path, page
) -> None:
    turns = [
        [
            {"text": "I will deploy."},
            {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"},
        ]
    ]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        # The relay attached and sent the snapshot, so the page knows the state.
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        assert await page.inner_text("#state") in ("idle", "running", "starting")

        await page.fill("#text", "ship it")
        await page.get_by_role("button", name="Send (queued)").click()

        # Claude's text and the tool call arrive as events, without reloading the page.
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('I will deploy.')"
        )
        await page.wait_for_selector(".escalation")
        card = await page.inner_text(".escalation")
        assert "deploy" in card and "calu:escalate" in card
        # D140: the tool call shows its command, not its input as JSON, and the verdict is a
        # badge coloured by the decision -- before, every verdict was orange, a PASS included.
        assert await page.text_content("#stream .tool .tag") == "Bash"
        assert '{"command"' not in await page.inner_text("#stream")
        assert await page.locator("#stream .check .decision.escalate").count() == 1

        # Answering from the card resolves it in the session, so the card goes and the turn ends.
        await page.fill(".escalation input[type=text]", "go ahead")
        await page.get_by_role("button", name="Allow").click()
        await page.wait_for_function("!document.querySelector('.escalation')")
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        stream = await page.inner_text("#stream")
        assert "command checker" in stream or "escalation" in stream
        assert "ship it" in stream  # the user's own message came back as an event


async def test_a_session_that_serves_no_clients_says_so_in_the_browser(
    db_url: str, tmp_path: Path, page
) -> None:
    from calu.db import Db, connect
    from calu.setup import setup
    from calu.store import DatabaseSessionStore, SessionIdentity
    from calu.web.app import create_app
    from tests.browser.conftest import serve

    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        store = DatabaseSessionStore(Db(conn))
        session_id = await store.start(
            SessionIdentity(hostname="elsewhere", os_user="u", directory="/w", pid=1, os="Linux")
        )
        async with serve(create_app(Db(conn))) as base:
            await page.goto(f"{base}/sessions/{session_id}/live")
            await page.wait_for_function(
                "document.getElementById('note').textContent.includes('serves no clients')"
            )
            assert await page.inner_text("#state") == "not attached"
    finally:
        await conn.close()


async def test_a_question_of_the_agent_is_answered_by_clicking_an_option(
    db_url: str, tmp_path: Path, page
) -> None:
    """D35: the options Claude offered become buttons, and the answer is the result."""
    questions = [
        {
            "question": "Should this project indent with tabs or with spaces?",
            "header": "Indentation",
            "options": [
                {"label": "Tabs", "description": "tab characters"},
                {"label": "Spaces", "description": "space characters"},
            ],
            "multiSelect": False,
        }
    ]
    turns = [[{"tool": "AskUserQuestion", "input": {"questions": questions}, "result": "unused"}]]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "set up the project")
        await page.get_by_role("button", name="Send (queued)").click()

        await page.wait_for_selector(".escalation")
        card = await page.inner_text(".escalation")
        assert "tabs or with spaces" in card
        # The options are buttons, and there is no allow or deny: a question needs an answer.
        assert await page.get_by_role("button", name="Spaces").count() == 1
        assert await page.get_by_role("button", name="Allow").count() == 0

        await page.get_by_role("button", name="Spaces").click()
        await page.wait_for_function("!document.querySelector('.escalation')")
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        # Claude received the answer as the result of the call, which never ran.
        stream = await page.inner_text("#stream")
        assert "The user answered your question: Spaces" in stream


async def test_the_live_view_says_when_background_tasks_still_run(
    db_url: str, tmp_path: Path, page
) -> None:
    """D158: a turn can end with background tasks running; Claude Code continues by itself."""
    turns = [
        [{"text": "Started the build in the background."}, {"background_tasks": 1}],
        [{"text": "The build finished."}],
    ]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "build it")
        await page.get_by_role("button", name="Send (queued)").click()
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('The build finished.')"
        )
        stream = await page.inner_text("#stream")
        assert "1 background task(s) still running" in stream
        assert stream.index("still running") < stream.index("The build finished.")


async def test_the_live_view_shows_the_honesty_state_and_clears_a_mark(
    db_url: str, tmp_path: Path, page
) -> None:
    """The honesty panel shows the state of `honesty.update`, and "Clear" asks the session."""
    (tmp_path / "parser.py").write_text("def fields():\n    return []\n", encoding="utf-8")
    async with running(db_url, tmp_path, [[]]) as (base, session_id, session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        assert await page.locator("#honesty").count() == 0, "honesty.mode is off: no panel"

        session.config = session.config.model_copy(update={"honesty": HonestyConfig(mode="track")})
        session.honesty.record(
            [CodeMark(path="parser.py", symbol="fields", reason="the docstring is missing")],
            [],
            "edit",
        )
        await session.announce_honesty()
        await page.wait_for_selector("#honesty")
        panel = await page.inner_text("#honesty")
        assert "Code honesty (track): not clean" in panel
        assert "no baseline" in panel and "every file unreviewed" in panel
        assert "parser.py:fields -- the docstring is missing" in panel
        assert await page.get_by_role("button", name="Trust HEAD").count() == 1
        assert await page.get_by_role("button", name="Review now").count() == 1

        await page.get_by_role("button", name="Clear").click()
        await page.wait_for_function(
            "document.getElementById('honesty').textContent.includes('0 open mark(s)')"
        )
        assert session.honesty.open_marks == []


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc00000030101009a60e1d50000000049454e44ae426082"
)
"""A valid PNG of one pixel."""


async def test_claudes_text_is_shown_as_markdown_and_a_read_image_as_a_thumbnail(
    db_url: str, tmp_path: Path, page
) -> None:
    """Runs a scripted session and a browser; writes two small files into `tmp_path`.

    Claude's text must become formatted nodes and never markup of the page, and the image
    it reads must appear small under the tool call, served from the project directory.
    """
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("plain", encoding="utf-8")
    text = "## Result\n\nIt is **done**, see `shot.png`.\n\n- one\n- two\n\n<b id='planted'>x</b>"
    turns = [
        [
            {"text": text},
            {"tool": "Read", "input": {"file_path": str(tmp_path / "shot.png")}, "result": "img"},
            {"tool": "Read", "input": {"file_path": str(tmp_path / "notes.txt")}, "result": "t"},
        ]
    ]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "show me")
        await page.get_by_role("button", name="Send (queued)").click()
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        assert await page.inner_text("#stream .md strong") == "done"
        assert await page.inner_text("#stream .md code") == "shot.png"
        assert await page.locator("#stream .md li").count() == 2
        assert await page.locator("#stream .md h4").count() == 1
        # What looks like markup in Claude's text stays text.
        assert await page.locator("#planted").count() == 0
        assert "<b id='planted'>x</b>" in await page.inner_text("#stream .md")
        # One thumbnail: for the image, not for the text file; and it really loaded.
        await page.wait_for_function(
            "[...document.querySelectorAll('#stream .thumb img')]"
            ".some((image) => image.complete && image.naturalWidth === 1)"
        )
        assert await page.locator("#stream .thumb").count() == 1
        link = await page.get_attribute("#stream .thumb", "href")
        assert link is not None and link.startswith(f"/sessions/{session_id}/file?path=")
        # The end of the turn leaves a gap before whatever comes next.
        gap = await page.evaluate(
            "getComputedStyle(document.querySelector('#stream .turn')).marginBottom"
        )
        assert float(gap.removesuffix("px")) > 10


async def test_an_image_is_attached_to_a_message_and_shown_with_it(
    db_url: str, tmp_path: Path, page
) -> None:
    """Runs a scripted session and a browser; writes one small image into `tmp_path`.

    The image chosen in the page must travel with the message and come back in the stream,
    where the page shows it (the content policy has to allow that).
    """
    picture = tmp_path / "shot.png"
    picture.write_bytes(PNG)
    async with running(db_url, tmp_path, [[{"text": "I see one pixel."}]]) as (base, sid, _s):
        await page.goto(f"{base}/sessions/{sid}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.set_input_files("#images", str(picture))
        await page.wait_for_selector("#attached .attached img")
        await page.fill("#text", "what is on this?")
        await page.get_by_role("button", name="Send (queued)").click()
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('I see one pixel.')"
        )
        # Sent: the waiting image is gone from the form and shown under the user's message.
        assert await page.locator("#attached .attached").count() == 0
        await page.wait_for_function(
            "[...document.querySelectorAll('#stream .thumb img')]"
            ".some((image) => image.complete && image.naturalWidth === 1)"
        )
        stream = await page.inner_text("#stream")
        assert "what is on this?" in stream
        # A click enlarges it in place.
        await page.click("#stream .thumb img")
        assert await page.locator("#stream .thumb img.large").count() == 1
