"""What two reviews of the full-screen client found, each as a test.

The keys of a decision must reach the step they were pressed for and no other; a reply that
begins with a letter of the card must be typed; nothing of a step may be hidden without the
card saying so; the layout must follow one resize; the client must come back from a program
it handed the terminal to; and what it stores with the `admin` credential must be compared
with what the attached session reports.
"""

import os
import pty
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest

pytest.importorskip("textual")

from textual.app import SuspendNotSupported

from calu.protocol.client import RequestFailed
from calu.protocol.messages import CLOSE_FELL_BEHIND, CLOSE_TOKEN_REVOKED
from calu.tui import render, scenarios, start
from calu.tui.app import HOLD_S, help_lines
from calu.tui.capabilities import Capabilities
from calu.tui.connection import Link
from calu.tui.difftools import marked_rows, word_spans
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
from calu.tui.model import (
    ALL,
    BLOCK_LIMIT,
    BLOCK_SLACK,
    DIFF_TURNS,
    Change,
    JoinBlock,
    SessionModel,
    StepBlock,
    UserBlock,
)
from calu.tui.screens.checkers import CheckersScreen
from calu.tui.screens.confirm import ConfirmScreen
from calu.tui.screens.detail import DetailScreen
from calu.tui.screens.text import TextScreen
from calu.tui.theme import DARK_PALETTE
from tests.unit.test_tui_app import Running, running

pytestmark = pytest.mark.anyio

LONG = (
    "cd /srv/app && git fetch origin && git reset --hard origin/main && find . -name '*.pyc' "
    "-delete && rm -rf build dist .cache && curl -fsSL https://example.org/install.sh | sh\n"
    "python - <<'PY'\nimport shutil\nshutil.rmtree('/srv/app/logs')\nprint('cleaned')\nPY\n"
    "systemctl --user restart app.service\njournalctl --user -u app.service -n 50"
)
"""A command of nine lines, the first of them longer than the card is wide."""


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


def second(escalation_id: str = "esc-B", command: str = "rm -rf build") -> list[scenarios.Item]:
    """Build one more step that waits, escalated by a checker."""
    return scenarios.escalation(
        escalation_id,
        f"tool-{escalation_id}",
        "Bash",
        {"command": command},
        "The command deletes files the task did not name.",
        "deleted the build directory",
    )


def decided(run: Running) -> list[dict[str, Any]]:
    """Return every `escalation.decide` the client has sent."""
    return run.session.sent("escalation.decide")


# ------------------------------------------------------------------ the card shows the step


def test_a_long_command_is_wrapped_and_what_is_cut_is_counted() -> None:
    model = SessionModel()
    for item in second(command=LONG):
        model.apply(item.method, item.params, 1.0)
    decision = model.decisions["esc-B"]
    view = render.View(palette=DARK_PALETTE, width=74, now=2.0)
    lines = [line.plain for line in render.card_lines(decision, model, view, (1, 1), 8)]
    assert len(lines) <= 8
    assert lines[1].startswith("  cd /srv/app && git fetch origin")
    assert lines[2].startswith("  ↪ "), "a wrapped line is told from a new line of the command"
    assert "https://example.org/install.sh | sh" in "\n".join(lines), "the end of the line"
    hidden = render.card_hidden(decision, model, view, 8)
    assert hidden > 0 and f"  command cut: {hidden} more lines · w shows all" in lines
    assert lines[-2].startswith("  Stopped: "), "the reason keeps one line"
    assert lines[-1].startswith("  a shows all of it (allow from there)")
    tall = [line.plain for line in render.card_lines(decision, model, view, (1, 1), 20)]
    assert render.card_hidden(decision, model, view, 20) == 0
    assert "  journalctl --user -u app.service -n 50" in tall and "cut" not in "\n".join(tall)


async def test_a_cut_command_is_allowed_from_the_detail_not_from_the_card() -> None:
    async with running("simple_turn", size=(80, 24)) as run:
        await run.play(second(command=LONG))
        assert "command cut:" in run.line("card") and "a whole step" in run.line("footer")
        await run.press("a")
        screen = run.app.screen
        assert isinstance(screen, DetailScreen) and not run.app.armed, "a opens, it allows nothing"
        assert "journalctl --user -u app.service -n 50" in screen.text
        assert decided(run) == []
        await run.press("a", "a")
        assert decided(run) == [{"escalation_id": "esc-B", "decision": "allow", "note": None}]
        await run.play(second("esc-C", LONG))
        await run.press("alt+a")
        assert isinstance(run.app.screen, DetailScreen), "the chord does not allow a cut step"
        assert len(decided(run)) == 1


def test_an_edit_on_the_card_shows_its_changed_lines() -> None:
    model = SessionModel()
    edit = {
        "file_path": ".github/workflows/ci.yml",
        "old_string": "      - run: uv run pytest -q\n",
        "new_string": "      - run: uv run pytest -q -m 'not live'\n      - run: ruff check .\n",
    }
    events = scenarios.escalation("e", "t", "Edit", edit, "It changes the CI.", "edited the CI")
    for item in events:
        model.apply(item.method, item.params, 1.0)
    view = render.View(palette=DARK_PALETTE, width=74, now=2.0)
    lines = render.card_lines(model.decisions["e"], model, view, (1, 1), 9)
    plain = [line.plain for line in lines]
    assert plain[1] == "  .github/workflows/ci.yml  +2 -1"
    assert plain[2:5] == [
        "  -      - run: uv run pytest -q",
        "  +      - run: uv run pytest -q -m 'not live'",
        "  +      - run: ruff check .",
    ]
    assert render.card_hidden(model.decisions["e"], model, view, 9) == 0
    many = {"file_path": "big.txt", "content": "\n".join(f"line {n}" for n in range(40))}
    for item in scenarios.escalation("w", "tw", "Write", many, "A new file.", "wrote big.txt"):
        model.apply(item.method, item.params, 1.0)
    cut = [line.plain for line in render.card_lines(model.decisions["w"], model, view, (2, 2), 9)]
    assert cut[1] == "  big.txt  +40 -0" and "  change cut: 36 more lines · w shows all" in cut


def test_the_changed_words_of_a_replaced_line_are_set_apart() -> None:
    assert word_spans("x = check_step(a)", "x = judge_step(a)") == ([(4, 14)], [(4, 14)])
    assert word_spans("alpha beta", "something else entirely here") == ([], [])
    rows = marked_rows("a\nx = check_step(a)\nb\n", "a\nx = judge_step(a)\nb\n", context=1)
    assert [(sign, text) for sign, text, _ in rows[1:]] == [
        (" ", "a"),
        ("-", "x = check_step(a)"),
        ("+", "x = judge_step(a)"),
        (" ", "b"),
    ]
    line = render.diff_row("+", "x = judge_step(a)", rows[3][2], DARK_PALETTE, 80)
    marked = [line.plain[span.start : span.end] for span in line.spans if "underline" in str(span)]
    assert marked == ["judge_step"]


def test_an_expanded_step_shows_its_whole_input_wrapped() -> None:
    model = SessionModel()
    command = "pytest -x " + " ".join(f"tests/unit/very/long/path/test_{n}.py" for n in range(8))
    for item in scenarios.step("t1", "Bash", {"command": command}, "ran some tests"):
        model.apply(item.method, item.params, 1.0)
    step = model.step("t1")
    assert step is not None
    view = render.View(palette=DARK_PALETTE, width=60, expanded=frozenset({step.id}))
    text = "\n".join(line.plain for line in render.step_lines(step, model, view))
    assert "…" not in text.split("ran some tests", 1)[1].split("check", 1)[0]
    assert "test_7.py" in text and "│   ↪ " in text
    many = {"command": "\n".join(f"echo {n}" for n in range(50))}
    for item in scenarios.step("t2", "Bash", many, "echoed"):
        model.apply(item.method, item.params, 1.0)
    long_step = model.step("t2")
    assert long_step is not None
    rows = [line.plain for line in render.input_lines(long_step, view)]
    assert len(rows) == render.INPUT_LINES + 1
    assert rows[-1] == "… 20 more lines · alt+c copies the whole input"


# ------------------------------------------------------------------ layout follows one resize


async def test_one_resize_in_each_direction_changes_the_layout() -> None:
    async with running("escalation", size=(120, 36)) as run:
        side = run.app.query_one("#side")
        assert not run.app.has_class("-narrow") and side.region.width == 36
        await run.pilot.resize_terminal(80, 24)
        await run.pilot.pause()
        assert run.app.has_class("-narrow") and run.app.has_class("-short")
        assert side.region.width == 0 and run.app.transcript.region.width == 80
        assert all(len(row) <= 76 for row in run.line("card").splitlines())
        await run.pilot.resize_terminal(120, 36)
        await run.pilot.pause()
        assert not run.app.has_class("-narrow") and not run.app.has_class("-short")
        assert side.region.width == 36 and side.region.x == 84
        assert run.app.transcript.region.width == 84
        assert "waiting" in run.line("card").splitlines()[0]


# ------------------------------------------------------------------ typing is never a command


async def test_a_reply_that_begins_with_a_card_letter_is_typed_whole() -> None:
    async with running("escalation") as run:
        for reply in ("no, use a PR instead", "why this branch?", "do it on a branch", "all good"):
            await run.type(reply)
            assert run.composer == reply, "the first letter is typed with the rest"
            assert run.app.mode == "chat" and not isinstance(run.app.screen, DetailScreen)
            await run.press("enter")
            assert run.composer == ""
        assert [sent["text"] for sent in run.session.sent("chat.send")] == [
            "no, use a PR instead",
            "why this branch?",
            "do it on a branch",
            "all good",
        ]
        assert decided(run) == [] and run.app.model.decisions["e1"].note == ""


async def test_a_reply_to_a_question_keeps_its_first_letter() -> None:
    async with running("question_multi") as run:
        await run.type("ok 2 is fine")
        assert run.composer == "ok 2 is fine" and run.app.mode == "chat"
        decision = run.app.model.decisions["e1"]
        assert decision.free == ["", "", ""] and decision.answered() == 0


async def test_typing_in_the_detail_returns_and_types() -> None:
    async with running("escalation") as run:
        await run.press("w", "w")
        assert isinstance(run.app.screen, DetailScreen)
        await run.press("h")
        assert not isinstance(run.app.screen, DetailScreen)
        assert run.composer == "h" and run.app.focused is run.app.composer
        assert decided(run) == []


# ------------------------------------------------------------------ a decision is bound to an id


async def test_the_second_press_does_not_reach_the_step_that_took_the_place() -> None:
    async with running("escalation") as run:
        await run.play(second())
        assert list(run.app.model.decisions) == ["e1", "esc-B"]
        await run.press("a")
        assert (run.app.armed, run.app.armed_for) == ("allow", "e1")
        await run.session.emit("escalation.resolved", {"escalation_id": "e1", "decision": "deny"})
        await run.pilot.pause()
        assert run.app.armed == "", "the first press goes with its step"
        assert run.line("card").startswith("DECISION 1 of 1") and "rm -rf build" in run.line("card")
        assert "a,a allow" in run.line("footer")
        await run.press("a")
        assert decided(run) == [], "this a is a first press for the other step"
        assert (run.app.armed, run.app.armed_for) == ("allow", "esc-B")


async def test_a_key_right_after_the_card_changed_decides_nothing() -> None:
    async with running("escalation") as run:
        await run.play(second())
        await run.press("a")
        await run.session.emit("escalation.resolved", {"escalation_id": "e1", "decision": "deny"})
        await run.pilot.pause()
        for key in ("a", "alt+d", "alt+a"):
            await run.pilot.press(key)  # without the 0.6 s of `Running.press`
            await run.pilot.pause()
            assert not run.app.armed and decided(run) == []
            assert "since a moment ago" in run.line("composer-head")
        await run.press("alt+d")
        assert decided(run) == [{"escalation_id": "esc-B", "decision": "deny", "note": None}]


async def test_the_detail_decides_its_own_step_only() -> None:
    async with running("escalation") as run:
        await run.play(second())
        await run.press("w", "w")
        screen = run.app.screen
        assert isinstance(screen, DetailScreen) and screen.escalation_id == "e1"
        await run.press("d")
        await run.session.emit("escalation.resolved", {"escalation_id": "e1", "decision": "allow"})
        await run.pilot.pause()
        assert not isinstance(run.app.screen, DetailScreen), "it closes with its step"
        run.app.decision_key("d", "e1", detail=True)
        run.app.decide_now("deny", "e1")
        await run.pilot.pause()
        assert decided(run) == [] and "no longer open" in run.line("composer-head")


async def test_enter_on_the_note_question_decides_nothing() -> None:
    async with running("simple_turn") as run:
        await run.play(second("esc-A", "git push"))
        await run.play(second())
        await run.type("use a branch")
        await run.press("alt+a")
        assert run.app.ask is not None and run.app.ask.escalation_id == "esc-A"
        await run.press("enter")
        assert decided(run) == [] and run.app.ask is None, "enter is not a yes and not a no"
        assert run.composer == "use a branch"
        await run.press("alt+a")
        await run.session.emit(
            "escalation.resolved", {"escalation_id": "esc-A", "decision": "deny"}
        )
        await run.pilot.pause()
        assert run.app.ask is None, "the question goes with its step"
        assert "no longer open" in run.line("composer-head")
        for key in ("n", "y", "enter"):
            await run.press(key)
        assert decided(run) == [], "no answer reaches the step that is shown now"
        assert "esc-B" in run.app.model.decisions


async def test_y_sends_the_note_and_n_decides_without_it() -> None:
    async with running("simple_turn") as run:
        await run.play(second("esc-A", "git push"))
        await run.type("use a branch")
        await run.press("alt+d", "y")
        assert decided(run) == [
            {"escalation_id": "esc-A", "decision": "deny", "note": "use a branch"}
        ]
        assert run.composer == ""


async def test_a_held_key_decides_nothing() -> None:
    async with running("escalation") as run:
        waiting: list[tuple[float, Callable[[], None]]] = []
        run.app.defer = lambda delay, work: waiting.append((delay, work))
        await run.press("a", "a")
        assert decided(run) == [] and waiting[0][0] == HOLD_S
        await run.pilot.press("a")  # the key repeats: a third press right after the second
        await run.pilot.pause()
        waiting[0][1]()
        await run.pilot.pause()
        assert decided(run) == [] and "the key repeats" in run.line("composer-head")
        for _ in range(5):
            await run.pilot.press("a")
        await run.pilot.pause()
        assert decided(run) == [] and not run.app.armed and run.composer == ""
        waiting.clear()
        await run.press("a", "a")
        waiting[0][1]()
        await run.pilot.pause()
        assert [sent["decision"] for sent in decided(run)] == ["allow"]


# ------------------------------------------------------------------ questions


async def test_enter_goes_to_the_first_question_without_an_answer() -> None:
    async with running("question_multi", size=(80, 24)) as run:
        decision = run.app.model.decisions["e1"]
        await run.press("right", "right", "1")
        assert decision.answered() == 1 and decision.current == 0
        await run.press("right", "enter")
        assert decision.current == 0, "the first open question is shown"
        assert "1 of 3 answered: this question is still open" in run.line("composer-head")
        assert decided(run) == []
        await run.press("1", "1", "enter")
        assert decided(run) and decided(run)[0]["answer"].startswith("Runner: pytest")


async def test_a_finished_answer_is_sent_by_enter_only_after_a_key_on_the_card() -> None:
    async with running("showcase") as run:
        await run.press("right_square_bracket", "1")
        await run.type("x")
        await run.press("backspace", "enter")
        assert decided(run) == [], "enter after typing asks first"
        assert "Press enter again to send your answer" in run.line("card")
        await run.press("enter")
        assert decided(run) == [{"escalation_id": "e2", "answer": "pytest"}]


async def test_a_digit_just_as_a_question_opens_chooses_nothing() -> None:
    async with running("simple_turn") as run:
        items = scenarios.question(
            "q1",
            "tq",
            [
                {
                    "question": "Which runner?",
                    "header": "Runner",
                    "options": [{"label": "pytest"}, {"label": "unittest"}],
                }
            ],
        )
        await run.play(items)
        await run.pilot.press("2")  # typed at the moment the question appeared
        await run.pilot.press("enter")
        await run.pilot.pause()
        assert run.app.model.decisions["q1"].answered() == 0 and decided(run) == []


# ------------------------------------------------------------------ slash commands


async def test_the_slash_list_filters_selects_completes_and_closes() -> None:
    async with running("simple_turn") as run:
        await run.type("/")
        listed = run.line("slash").splitlines()
        assert len(listed) == 9 and listed[0].startswith(" > /allow [id] [note]")
        assert "more: keep typing" in listed[-1] and "↑↓ select" in listed[-1]
        await run.type("tr")
        listed = run.line("slash").splitlines()
        assert [row.split()[0 if row[1] != ">" else 1] for row in listed[:2]] == [
            "/trust",
            "/trust-file",
        ]
        assert "Certify a commit as honest" in listed[0]
        await run.press("down")
        assert run.line("slash").splitlines()[1].startswith(" > /trust-file <path>")
        await run.press("enter")
        assert run.composer == "/trust-file ", "enter completes, it does not run"
        assert run.line("slash") == ""
        await run.press("ctrl+u")
        await run.type("/hon")
        await run.press("escape")
        assert run.line("slash") == "" and run.composer == "/hon", "escape closes the list only"
        await run.type("e")
        assert run.line("slash").splitlines()[0].startswith(" > /honesty")
        await run.press("ctrl+u")
        await run.type("/nosuch")
        assert "no such command" in run.line("slash")
        await run.press("ctrl+u")
        await run.type("/help")
        await run.press("enter")
        assert isinstance(run.app.screen, TextScreen), "a whole command runs on enter"


# ------------------------------------------------------------------ subagents


async def test_a_message_on_the_tab_of_a_foreground_subagent_asks_first() -> None:
    async with running("two_subagents") as run:
        run.app.select_tab("a2")
        assert "tests takes no messages" in run.line("composer-head")
        await run.type("add a test for the empty input")
        await run.press("enter")
        assert run.session.sent("chat.send") == []
        assert run.composer == "add a test for the empty input", "the text is kept"
        head = run.line("composer-head")
        assert "tests takes no messages" in head and "enter again sends this to main" in head
        await run.press("enter")
        assert run.session.sent("chat.send") == [
            {"text": "add a test for the empty input", "delivery": "queued", "to": "main"}
        ]
        assert run.composer == ""


async def test_a_message_to_a_subagent_waits_visibly_and_then_stands_in_its_tab() -> None:
    async with running("two_subagents") as run:
        run.app.select_tab("a1")
        await run.type("also look in docs/")
        await run.press("enter")
        assert run.session.sent("chat.send")[0]["to"] == "a1"
        pending = run.line("pending")
        assert pending.startswith("▌ you → explore · waiting (relayed by main)")
        assert "also look in docs/" in pending
        run.app.select_tab("main")
        assert run.line("pending") == "", "it waits for explore, not for main"
        await run.session.take()
        await run.pilot.pause()
        blocks = [block for block in run.app.model.blocks_of("a1") if isinstance(block, UserBlock)]
        assert [(block.text, block.echo) for block in blocks] == [("also look in docs/", True)]
        run.app.select_tab("a1")
        assert run.line("pending") == ""
        assert any(
            row.startswith("▌ you → explore (relayed by the main agent)  also look in docs/")
            for row in run.transcript
        )
        echoes = [b for b in run.app.model.blocks_of(ALL) if isinstance(b, UserBlock) and b.echo]
        assert echoes == [], "the view of all agents shows the message once"


async def test_the_key_that_switches_agents_stays_on_a_narrow_tab_row() -> None:
    async with running("showcase", size=(80, 24)) as run:
        assert run.app.model.decisions and "alt+←/→" in run.line("tabs")
    async with running("escalation", size=(80, 24)) as run:
        assert "alt+←/→" not in run.line("tabs"), "one tab: nothing to switch to"


# ------------------------------------------------------------------ hints fit the width


def test_hints_are_dropped_whole_by_priority() -> None:
    hints = [(9, "a allow"), (5, "n note"), (8, "w detail"), (5, "] next")]
    assert render.fit_hints(hints, 80) == "a allow · n note · w detail · ] next"
    assert render.fit_hints(hints, 30) == "a allow · n note · w detail"
    assert render.fit_hints(hints, 20) == "a allow · w detail"
    assert render.fit_hints(hints, 3) == "a allow", "the last hint is never cut"


async def test_no_key_line_is_cut_at_eighty_columns() -> None:
    async with running("showcase", size=(80, 24)) as run:
        for key in ("", "a", "escape", "n", "n"):
            if key:
                await run.press(key)
            for widget in ("card", "composer-head", "tabs"):
                assert "…" not in run.line(widget).splitlines()[-1], (key, run.line(widget))
        await run.press("escape", "right_square_bracket")
        assert "…" not in run.line("card").splitlines()[-1]
        await run.press("o", "o")
        assert "…" not in run.line("composer-head")
        await run.press("escape")
        run.app.run_command("hold", "a draft for later")
        await run.pilot.pause()
        assert "lost on detach" in run.line("composer-head"), "a long message wraps, uncut"
        head = run.app.query_one("#composer-head")
        assert head.region.height == 2


def test_the_note_of_the_stored_history_wraps() -> None:
    model = SessionModel()
    model.load_history(scenarios.stored_history())
    last = model.blocks[-1]
    assert isinstance(last, JoinBlock)
    rows = render.block_lines(last, model, render.View(palette=DARK_PALETTE, width=60))
    text = " ".join(row.plain.strip(" ·") for row in rows)
    assert len(rows) == 2 and "without thinking and diffs" in text and "…" not in text


def test_an_answered_set_of_questions_is_one_readable_line() -> None:
    model = SessionModel()
    for item in scenarios.question_multi():
        model.apply(item.method, item.params, 1.0)
    decision = model.decisions["e1"]
    decision.picks = [{0}, {0}, {1}]
    decision.own = True
    model.apply(
        "escalation.resolved", {"escalation_id": "e1", "decision": "deny", "note": "x"}, 2.0
    )
    step = model.step(decision.tool_use_id)
    assert step is not None
    rows = [row.plain for row in render.step_lines(step, model, render.View(DARK_PALETTE, 120))]
    assert any("Runner: pytest · Checks: unit tests · Fixtures:" in row for row in rows)


# ------------------------------------------------------------------ toasts


async def test_the_same_notice_is_shown_once_with_its_count() -> None:
    async with running("simple_turn") as run:
        run.app.options.toasts = True
        shown: list[str] = []
        run.app.notify = lambda message, **more: shown.append(message)  # type: ignore[method-assign]
        for _ in range(45):
            await run.session.emit("assistant.delta", {"text": 7})
        await run.pilot.pause()
        assert len(shown) == 1 and shown[0].startswith("assistant.delta: unexpected content")
        first = int(shown[0].rsplit("(", 1)[1].split()[0]) if "times" in shown[0] else 1
        for _ in range(5):
            await run.session.emit("assistant.delta", {"text": 7})
        await run.pilot.pause()
        assert len(shown) == 1, "within the quiet time the notice is only counted"
        run.clock.advance(30.0)
        await run.session.emit("assistant.delta", {"text": 7})
        await run.pilot.pause()
        assert len(shown) == 2 and shown[1].endswith(f"({45 - first + 6} times)")
        assert len([line for line in run.app.model.logs if "assistant.delta" in line.message]) == 51


async def test_an_interrupt_of_this_client_is_not_called_a_decision_elsewhere() -> None:
    async with running("escalation") as run:
        run.app.options.toasts = True
        shown: list[str] = []
        run.app.notify = lambda message, **more: shown.append(message)  # type: ignore[method-assign]
        run.clock.advance(5.0)
        await run.press("escape", "escape")
        await run.pilot.pause()
        assert run.session.sent("interrupt") == [{}]
        assert "cancelled by your interrupt" in shown and not any("elsewhere" in t for t in shown)


# ------------------------------------------------------------------ honesty page


async def test_the_honesty_page_has_keys_for_its_commands() -> None:
    session = FakeSession()
    session.honesty_requests = True
    async with running("simple_turn", session=session) as run:
        mark = {"id": 4, "path": "src/gate.py", "symbol": "judge_step", "reason": "stale docstring"}
        await session.emit(
            "honesty.update", {"mode": "track", "marks": [mark], "unreviewed": ["a.py"]}
        )
        run.app.run_command("honesty")
        await run.pilot.pause()
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "    #4 src/gate.py:judge_step" in page.text
        assert set(page.keys) == {"t", "r", "x", "m"}
        await run.press("t")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.title_text == "Certify this commit as honest?"
        await run.press("y")
        await run.pilot.pause()
        assert session.sent("honesty.trust") == [{}] and run.app.screen is page
        await run.press("r")
        await run.pilot.pause()
        assert session.sent("honesty.review") == [{}]
        await run.press("x")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.title_text == "Clear the honesty mark #4?"
        await run.press("enter")
        assert session.sent("honesty.clear") == [], "enter is no"
        await run.press("x", "y")
        await run.pilot.pause()
        assert session.sent("honesty.clear") == [{"mark_id": 4}]
        assert run.app.screen is page and "MARKED (0)" in page.text


# ------------------------------------------------------------------ the terminal handed over


async def test_the_client_attaches_again_after_a_long_stay_in_the_git_program(
    tmp_path: Path,
) -> None:
    session = FakeSession(workspace=str(tmp_path))
    async with running("simple_turn", session=session) as run:

        def stay(argv: Sequence[str], cwd: str) -> int:
            """Stand for a long stay: the session closes the unanswered connection."""
            session.drop(1011, "keepalive ping timeout")
            session.record("assistant.text", {"text": "written during the stay"})
            return 0

        run.app.run_program = stay
        run.app.which = lambda name: f"/usr/bin/{name}" if name in {"lazygit", "git"} else None
        await run.press("f4")
        with anyio.fail_after(5):
            while session.connects < 2 or run.app.model.connection.status != "attached":
                await run.pilot.pause(0.01)
        assert "attached" in run.line("status") and run.app.link.closed_reason == ""
        assert "  written during the stay" in run.app.transcript.all_text()
        await run.session.emit("assistant.text", {"text": "and a later event"})
        await run.pilot.pause()
        assert "  and a later event" in run.app.transcript.all_text()
        run.clock.advance(60.0)
        session.drop(1011, "keepalive ping timeout")
        with anyio.fail_after(5):
            while run.app.model.connection.status != "detached":
                await run.pilot.pause(0.01)
        assert "keepalive ping timeout" in run.app.link.closed_reason, "no stay: the end"


async def test_a_revoked_token_ends_the_link_also_during_a_stay(tmp_path: Path) -> None:
    session = FakeSession(workspace=str(tmp_path))
    async with running("simple_turn", session=session) as run:

        def stay(argv: Sequence[str], cwd: str) -> int:
            """Stand for a stay during which the token is revoked."""
            session.drop(CLOSE_TOKEN_REVOKED, "the token was revoked")
            return 0

        run.app.run_program = stay
        run.app.which = lambda name: f"/usr/bin/{name}" if name in {"lazygit", "git"} else None
        await run.press("f4")
        with anyio.fail_after(5):
            while run.app.model.connection.status != "detached":
                await run.pilot.pause(0.01)
        assert session.connects == 1 and "revoked" in run.app.link.closed_reason


async def test_running_a_program_keeps_the_interrupt_handler(tmp_path: Path) -> None:
    async with running("simple_turn") as run:
        before = signal.getsignal(signal.SIGINT)
        try:
            code = run.app._run_program([sys.executable, "-c", "pass"], str(tmp_path))
        except SuspendNotSupported:
            code = None  # the headless driver of the tests cannot hand the terminal over
        assert code in {0, None}
        assert signal.getsignal(signal.SIGINT) is before
        seen: list[object] = []

        class Suspended:
            """Stands for `App.suspend` and notes the handler in force during the stay."""

            def __enter__(self) -> None:
                """Enter the stay."""

            def __exit__(self, *error: object) -> None:
                """Leave the stay."""

        run.app.suspend = Suspended  # type: ignore[method-assign,assignment]
        probe = "import signal,sys; sys.exit(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)"
        real_call = subprocess.call

        def call(argv: list[str], cwd: str) -> int:
            """Note the handler of the client while the program runs."""
            seen.append(signal.getsignal(signal.SIGINT))
            return real_call(argv, cwd=cwd)

        subprocess.call = call  # type: ignore[assignment]
        try:
            assert run.app._run_program([sys.executable, "-c", probe], str(tmp_path)) == 0
            assert run.app._run_program(["/no/such/program"], str(tmp_path)) == 127
        finally:
            subprocess.call = real_call
        assert seen and all(handler is not before for handler in seen), "ignored during the stay"
        assert signal.getsignal(signal.SIGINT) is before, "and put back afterwards"


def test_ctrl_c_in_the_git_program_leaves_the_client_attached(tmp_path: Path) -> None:
    """The real client on a terminal: f4, an interrupt inside the program, and it is back."""
    environment = {
        **os.environ,
        "CALU_GIT_TOOL": "sleep 30",
        "TERM": "xterm-256color",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "calu.tui", "simple_turn"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=tmp_path,
        env=environment,
        start_new_session=True,
        preexec_fn=lambda: __import__("fcntl").ioctl(0, __import__("termios").TIOCSCTTY, 0),
    )
    os.close(slave)
    seen = b""

    def read_until(wanted: bytes, seconds: float) -> bool:
        """Read the terminal's output until a text appears or the time is over."""
        nonlocal seen
        end = time.monotonic() + seconds
        while wanted not in seen and time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
        return wanted in seen

    try:
        assert read_until(b"The rename is done", 20), seen[-400:]
        seen = b""
        os.write(master, b"\x1bOS")  # f4
        time.sleep(1.5)  # the program has the terminal now
        os.write(master, b"\x03")  # ctrl+c: an interrupt for the foreground process group
        assert read_until(b"back from sleep", 15), seen[-400:]
        assert process.poll() is None, "the client is still running"
        os.write(master, b"\x04")  # ctrl+d detaches
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)


# ------------------------------------------------------------------ the link


class ClosesWhileAsked(FakeSession):
    """A session that closes the connection while it is asked for its state the second time."""

    def __init__(self, code: int) -> None:
        """Create the session.

        Args:
            code: The close code of that one close.
        """
        super().__init__()
        self.code = code
        self.asked = 0

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        """Answer as usual; on the second `state.get` close the connection instead."""
        if method == "state.get":
            self.asked += 1
            if self.asked == 2:
                self.drop(self.code, "closed while the client asked")
                raise ConnectionError("the connection closed before the response arrived")
        return await super().handle(method, params)


async def test_a_close_while_the_attach_asks_for_the_state_is_followed_by_a_reconnect() -> None:
    session = ClosesWhileAsked(CLOSE_FELL_BEHIND)
    scenarios.preload(session, scenarios.escalation_scenario())
    model = SessionModel()
    link = Link(fake_connector(session), model, lambda change: None, lambda: 10.0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(link.run)
        with anyio.fail_after(5):
            while model.connection.status != "attached":
                await anyio.sleep(0.01)
            session.drop(CLOSE_FELL_BEHIND, "fell behind")  # the next attach asks a second time
            session.record("assistant.text", {"text": "written meanwhile"})
            while session.connects < 3 or model.connection.status != "attached":
                await anyio.sleep(0.01)
        assert link.closed_reason == "" and link.error is None
        assert getattr(model.blocks[-1], "text", "") == "written meanwhile"
        assert [getattr(b, "text", "") for b in model.blocks].count("written meanwhile") == 1
        link.detach()


async def test_a_close_for_good_while_the_attach_asks_ends_the_link() -> None:
    session = ClosesWhileAsked(1001)
    model = SessionModel()
    link = Link(fake_connector(session), model, lambda change: None, lambda: 10.0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(link.run)
        with anyio.fail_after(5):
            while model.connection.status != "attached":
                await anyio.sleep(0.01)
            session.drop(CLOSE_FELL_BEHIND, "fell behind")
            while model.connection.status != "detached":
                await anyio.sleep(0.01)
        assert link.closed_reason == "closed while the client asked"


async def test_falling_behind_during_the_first_attach_is_not_a_failed_attach() -> None:
    session = ClosesWhileAsked(CLOSE_FELL_BEHIND)
    session.asked = 1  # the very first `state.get` meets the close
    scenarios.preload(session, scenarios.simple_turn())
    model = SessionModel()
    link = Link(fake_connector(session), model, lambda change: None, lambda: 10.0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(link.run)
        with anyio.fail_after(5):
            while model.connection.status != "attached":
                await anyio.sleep(0.01)
        assert link.error is None and session.connects == 2
        texts = [getattr(block, "text", "") for block in model.blocks]
        assert len(texts) == len(set(texts)) or texts.count("") > 1, "no event was applied twice"
        link.detach()


# ------------------------------------------------------------------ the admin credential


async def test_a_save_that_the_session_does_not_show_is_reported_as_such() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    access.other_database = True
    async with running("simple_turn", session=session, admin=access) as run:
        run.app.run_command("checkers")
        await run.pilot.pause()
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e")
        screen.query_one("#checkers-editor").insert("One more rule.\n")  # type: ignore[attr-defined]
        await run.press("ctrl+s")
        await run.pilot.pause()
        assert access.saved and screen.message_error
        assert "in the database of this machine's admin credential" in screen.message
        assert "the attached session reads version" in screen.message
        assert "saved:" not in screen.message


async def test_a_save_over_a_change_made_meanwhile_is_refused() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    async with running("simple_turn", session=session, admin=access) as run:
        run.app.run_command("checkers")
        await run.pilot.pause()
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e")
        listed = session.results["checkers.get"]["checkers"]
        listed[0]["version"] += 1  # somebody saved in calu web meanwhile
        listed[0]["instructions"] = "the other change"
        screen.query_one("#checkers-editor").insert("Mine.\n")  # type: ignore[attr-defined]
        await run.press("ctrl+s")
        await run.pilot.pause()
        assert access.saved == [] and listed[0]["instructions"] == "the other change"
        assert "changed meanwhile to version" in screen.message and screen.editing


async def test_a_mode_the_session_does_not_announce_is_not_reported_as_in_force() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    access.other_database = True
    async with running("simple_turn", session=session, admin=access) as run:
        run.app.run_command("honesty", "enforce")
        with anyio.fail_after(5):
            while "honesty.mode" not in run.line("composer-head"):
                await run.pilot.pause(0.05)
        head = run.line("composer-head")
        assert access.modes == [("enforce", "myproject")]
        assert "stored honesty.mode = enforce" in head and "still reports off" in head
        assert run.app.message_error


# ------------------------------------------------------------------ memory and speed


def test_the_model_keeps_a_bounded_number_of_blocks() -> None:
    model = SessionModel()
    changes: list[Change] = []
    edit = ("src/a.py", "old line\n", "new line\n")
    for number in range(BLOCK_LIMIT + 3 * BLOCK_SLACK):
        if number % 100 == 0:
            model.apply("user.input", {"text": f"task {number}", "source": "user"}, 1.0)
        tool = {"tool_use_id": f"t{number}", "name": "Edit", "input": {"file_path": "src/a.py"}}
        changes.append(model.apply("tool.use", tool, 1.0))
        diff = {"tool_use_id": f"t{number}", "path": edit[0], "before": edit[1], "after": edit[2]}
        model.apply("edit.diff", diff, 1.0)
        if number % 100 == 99:
            model.apply("turn.result", {"subtype": "success", "num_turns": 1}, 2.0)
    assert len(model.blocks.kept) <= BLOCK_LIMIT + BLOCK_SLACK
    assert model.blocks.base > 0 and any(change.trimmed for change in changes)
    first = model.blocks.kept[0]
    assert isinstance(first, JoinBlock) and "/history" in first.text
    assert all(block.id == model.blocks.base + index for index, block in enumerate(model.blocks))
    assert model.block(0) is None and model.step("t0") is None
    assert len(model._steps) <= len(model.blocks.kept)
    assert model._by_agent["main"][0] == model.blocks.base
    newest = model.step(f"t{BLOCK_LIMIT + 3 * BLOCK_SLACK - 1}")
    assert newest is not None and newest.diffs[0].kept and newest.diffs[0].after == "new line\n"
    old = next(
        block
        for block in model.blocks
        if isinstance(block, StepBlock) and block.turn < model.turn - DIFF_TURNS - 1
    )
    assert not old.diffs[0].kept and old.diffs[0].before == "" and old.diffs[0].counts() == (1, 1)
    lines = render.diff_lines(old.diffs[0], render.View(DARK_PALETTE, 100))
    assert "no longer kept" in lines[1].plain


async def test_a_very_long_session_stays_bounded_and_a_tab_change_stays_fast() -> None:
    session = FakeSession()
    scenarios.preload(session, scenarios.long_session(4000))
    async with running(session=session, size=(120, 36)) as run:
        model = run.app.model
        assert len(model.blocks) == 5200 and len(model.blocks.kept) <= BLOCK_LIMIT + BLOCK_SLACK
        assert "file_3999.py" in "\n".join(run.transcript)
        await run.press("ctrl+home")
        assert "older blocks that this view no longer keeps" in run.transcript[0]
        await run.press("ctrl+end")
        await run.play(second())
        run.app.select_tab("main")
        await run.pilot.pause()
        started = time.perf_counter()
        for _ in range(4):
            run.app.transcript.rebuild()
        elapsed = (time.perf_counter() - started) / 4
        assert elapsed < 0.15, f"laying out a tab again took {elapsed:.2f} s"
        await run.play(scenarios.long_session(700)[1:])
        assert len(model.blocks.kept) <= BLOCK_LIMIT + BLOCK_SLACK
        assert run.line("card").startswith("DECISION 1 of 1"), "the open step stays decidable"
        await run.press("d", "d")
        assert decided(run) == [{"escalation_id": "esc-B", "decision": "deny", "note": None}]


# ------------------------------------------------------------------ smaller things


async def test_a_held_draft_that_is_refused_is_held_again() -> None:
    async with running("simple_turn") as run:
        run.app.run_command("hold", "first draft")
        run.app.run_command("hold", "second draft")
        await run.type("something else")
        run.session.fail["chat.send"] = "the queue is full"
        await run.press("f2")
        run.app.on_panel_list_act(
            type("Act", (), {"kind": "held", "key": "0", "pressed": "s"})()  # type: ignore[arg-type]
        )
        await run.pilot.pause()
        assert run.app.held == ["first draft", "second draft"], "back at the front"
        assert run.composer == "something else" and "held again" in run.line("composer-head")


async def test_enter_in_a_stopped_session_starts_it_and_sends_the_text() -> None:
    async with running("stopped") as run:
        assert "enter start the session" in run.line("footer")
        await run.type("go on with the tests")
        assert "enter start the session and send this" in run.line("footer")
        assert "enter starts the session and sends this" in run.line("composer-head")
        await run.press("enter")
        await run.pilot.pause()
        asked = [name for name, _ in run.session.requests]
        assert "session.start" in asked and "session.stop" not in asked
        assert run.session.sent("chat.send")[0]["text"] == "go on with the tests"
        assert run.composer == ""


async def test_the_model_list_marks_the_model_in_use() -> None:
    async with running("simple_turn") as run:
        offered: list[list[tuple[Any, ...]]] = []
        run.app.search_commands = lambda entries, **more: offered.append(list(entries))  # type: ignore[method-assign]
        run.app.run_command("model")
        run.app.run_command("effort")
        assert [entry[0] for entry in offered[0] if "(now)" in entry[0]] == ["opus  (now)"]
        assert [entry[0] for entry in offered[1] if "(now)" in entry[0]] == ["high  (now)"]


def test_the_help_fits_eighty_columns_and_can_be_filtered() -> None:
    caps = Capabilities()
    lines = [line.plain for line in help_lines(DARK_PALETTE, caps, 74)]
    keyed = [line for line in lines if line.startswith("  a,a · alt+a")]
    assert keyed and keyed[0].index("Allow the step") <= 26, "the key column is not half the page"
    found = [line.plain for line in help_lines(DARK_PALETTE, caps, 74, "lazygit")]
    assert any(line.startswith("  f4") for line in found)
    assert not any("Allow the step" in line for line in found)
    assert any("CALU_GIT_TOOL" in line for line in found)
    nothing = [line.plain for line in help_lines(DARK_PALETTE, caps, 74, "zzzz")]
    assert nothing == ["nothing in the help contains 'zzzz'"]


async def test_slash_filters_the_help_page() -> None:
    async with running("simple_turn") as run:
        await run.press("f1")
        page = run.app.screen
        assert isinstance(page, TextScreen) and "Allow the step that waits" in page.text
        await run.press("slash", "t", "r", "u", "s", "t", "enter")
        assert "Certify a commit as honest" in page.text
        assert "Allow the step that waits" not in page.text
        await run.press("escape")
        assert run.app.screen is page and "Allow the step that waits" in page.text
        await run.press("escape")
        assert run.app.screen is not page


def test_an_unsupported_textual_falls_back_to_the_line_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert start.problem() is None and start.available()
    monkeypatch.setattr(start.importlib.metadata, "version", lambda name: "7.5.0")
    assert not start.available()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("CALU_ATTACH", raising=False)
    assert not start.wants_full_screen(plain=False)
    said = capsys.readouterr().err
    assert "needs textual >= 8.2 and < 9 (found 7.5.0" in said and "line client" in said
    monkeypatch.setattr(start.importlib.metadata, "version", lambda name: "9.0.1")
    assert not start.available()


async def test_a_refused_request_keeps_its_exception_type() -> None:
    # `ClosesWhileAsked` relies on the scripted session refusing with `RequestFailed`.
    with pytest.raises(RequestFailed):
        await FakeSession().handle("no.such.method", {})


def test_a_subagents_text_has_its_name_in_the_gutter_of_the_view_of_all() -> None:
    model = SessionModel()
    for item in scenarios.two_subagents():
        model.apply(item.method, item.params, 1.0)
    text = next(
        block for block in model.blocks if block.agent_id == "a2" and hasattr(block, "streaming")
    )
    rows = [
        row.plain for row in render.block_lines(text, model, render.View(DARK_PALETTE, 70, tab=ALL))
    ]
    assert rows[0].startswith("  tests     The existing tests cover")
    assert all(row.startswith("            ") for row in rows[1:])


def test_a_code_block_stands_on_its_own_background() -> None:
    lines = render.markdown_lines("Run:\n\n```\nuv run pytest\n```\n", 40, DARK_PALETTE)
    code = next(line for line in lines if "uv run pytest" in line.plain)
    assert len(code.plain) == 40, "padded to the width, so the block is a rectangle"
    styles = {str(span.style) for span in code.spans}
    assert any(DARK_PALETTE.surface.lower() in style.lower() for style in styles)
