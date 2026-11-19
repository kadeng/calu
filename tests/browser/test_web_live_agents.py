"""The live view with several subagents, several open decisions and a set of questions."""

from pathlib import Path

import pytest

from tests.db.test_web_live import running

pytestmark = [pytest.mark.browser, pytest.mark.db, pytest.mark.anyio]


def _agent(description: str, command: str) -> dict:
    """Build a scripted subagent that says a line and runs one command."""
    return {
        "agent": {
            "description": description,
            "prompt": description,
            "subagent_type": "general-purpose",
            "actions": [
                {"text": f"Starting: {description}."},
                {"tool": "Bash", "input": {"command": command}, "result": "ok"},
            ],
            "result": f"{description}: done.",
        }
    }


async def test_subagents_are_told_apart_and_two_open_cards_are_answered_on_their_own(
    db_url: str, tmp_path: Path, page
) -> None:
    """A decision of one subagent and a question of the main agent are open together.

    (Two decisions open together only by a race or after the hold's time limit: an open
    decision holds every other checked step. A question holds nothing and is not held.)
    """
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    question = {
        "question": "Which index?",
        "header": "Index",
        "multiSelect": False,
        "options": [{"label": "test"}, {"label": "production"}],
    }
    turns = [
        [
            {
                "parallel": [
                    # The question first: an open decision would hold the question's call.
                    [{"sleep": 0.5}, _agent("Publish the package", "twine upload # calu:escalate")],
                    [{"tool": "AskUserQuestion", "input": {"questions": [question]}}],
                    [_agent("Run the tests", "pytest -q")],
                ]
            },
            {"text": "All reported."},
        ]
    ]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "go")
        await page.get_by_role("button", name="Send (queued)").click()

        # Two cards, each saying whose it is and which of the two it is.
        await page.wait_for_function("document.querySelectorAll('.escalation').length === 2")
        heads = " | ".join(await page.locator(".escalation-head").all_inner_texts())
        assert "1 of 2" in heads and "2 of 2" in heads
        assert "subagent #" in heads and "Publish the package" in heads
        assert "a question of the main agent" in heads
        waiting = await page.inner_text("#waiting")
        assert "1 decision and 1 question wait for you" in waiting
        assert "approves no other step that needs a check" in waiting
        assert (await page.title()).startswith("(2) ")
        # The agent list says who waits, and every line of a subagent carries its label.
        waits = await page.locator("#agents button.agent.waits").all_inner_texts()
        assert any("waits for your decision" in chip for chip in waits)
        assert any("asks you a question" in chip for chip in waits)
        assert await page.locator("#stream .line .who").count() >= 2

        # A note typed into one card survives the answer given in the other card.
        publish = page.locator(".escalation", has_text="twine upload")
        await publish.locator("input[type=text]").fill("only to the test index")
        await page.locator(".escalation .questions").get_by_role("button", name="test").click()
        await page.wait_for_function("document.querySelectorAll('.escalation').length === 1")
        assert await publish.locator("input[type=text]").input_value() == "only to the test index"
        assert (await page.title()).startswith("(1) ")

        # Following one subagent hides the lines of the others.
        await publish.get_by_role("button", name="show only this subagent").click()
        shown = await page.locator("#stream .line:not([hidden])").all_inner_texts()
        assert shown and all("Publish the package" in text for text in shown)
        assert "only subagent" in await page.inner_text("#following")
        await page.get_by_role("button", name="show every agent").click()
        assert await page.locator("#stream .line[hidden]").count() == 0

        await publish.get_by_role("button", name="Allow").click()
        await page.wait_for_function("!document.querySelector('.escalation')")
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        assert not (await page.title()).startswith("(")
        # Nothing runs any more: only the controls that can act are enabled.
        # One look at the page for the state and its controls: after the turn the session can
        # leave "idle" once more for a moment (work that ends after the turn's result), so
        # a state read in one call and a button read in the next may not belong together
        # (seen once in CI, on the line that read the buttons).
        await page.wait_for_function(
            "document.getElementById('state').textContent === 'idle'"
            " && document.getElementById('interrupt').disabled"
            " && document.getElementById('start').disabled"
            " && !document.getElementById('send').disabled"
        )
        assert errors == []


async def test_a_set_of_questions_is_answered_with_one_composed_text(
    db_url: str, tmp_path: Path, page
) -> None:
    questions = [
        {
            "question": "Which checks?",
            "header": "Checks",
            "multiSelect": True,
            "options": [{"label": "ruff"}, {"label": "pyright"}, {"label": "pytest"}],
        },
        {
            "question": "Which greeting?",
            "header": "Greeting",
            "multiSelect": False,
            "options": [{"label": "Hello"}, {"label": "Hi"}],
        },
    ]
    turns = [[{"tool": "AskUserQuestion", "input": {"questions": questions}}, {"text": "Thanks."}]]
    async with running(db_url, tmp_path, turns) as (base, session_id, session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "ask me")
        await page.get_by_role("button", name="Send (queued)").click()
        await page.wait_for_selector(".escalation .questions")
        assert "holds nothing back" in await page.inner_text("#waiting")

        # Nothing is sent before every question has an answer.
        await page.get_by_role("button", name="ruff").click()
        await page.get_by_role("button", name="pytest").click()
        await page.get_by_role("button", name="Send the answer").click()
        assert "question 2 of 2 has no answer yet" in await page.inner_text(".questions")
        await page.get_by_role("button", name="Hello").click()
        await page.get_by_role("button", name="Hi").click()  # one choice: Hi replaces Hello
        await page.get_by_role("button", name="Send the answer").click()
        await page.wait_for_function("!document.querySelector('.escalation')")
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        stream = await page.inner_text("#stream")
        assert "Checks: ruff, pytest" in stream and "Greeting: Hi" in stream
        assert "the question was answered" in stream
        assert await page.locator("#stream .result.failed").count() == 0
        del session


async def test_a_reader_who_scrolled_back_is_not_pulled_to_the_end(
    db_url: str, tmp_path: Path, page
) -> None:
    lines = [{"tool": "Bash", "input": {"command": f"echo {n}"}, "result": "ok"} for n in range(60)]
    turns = [lines, [{"text": "A later answer."}]]
    async with running(db_url, tmp_path, turns) as (base, session_id, _session):
        await page.goto(f"{base}/sessions/{session_id}/live")
        await page.wait_for_function("document.getElementById('note').textContent === 'attached'")
        await page.fill("#text", "one")
        await page.keyboard.press("Control+Enter")
        await page.wait_for_function(
            "document.getElementById('stream').textContent.includes('turn ended')"
        )
        await page.evaluate("() => { document.getElementById('stream').scrollTop = 0; }")
        await page.wait_for_function("document.getElementById('stream').scrollTop === 0")
        await page.wait_for_timeout(100)  # the scroll event has been handled
        await page.fill("#text", "two")
        await page.keyboard.press("Control+Enter")
        await page.wait_for_selector("#latest:not([hidden])")
        assert await page.evaluate("() => document.getElementById('stream').scrollTop") == 0
        assert "jump to latest" in await page.inner_text("#latest")
        await page.click("#latest")
        await page.wait_for_function(
            "() => { const s = document.getElementById('stream');"
            " return s.scrollHeight - s.scrollTop - s.clientHeight < 5; }"
        )
        assert await page.is_hidden("#latest")
