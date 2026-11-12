"""The full-screen client driven by keys, against a scripted session (`calu.tui.fake`).

Textual's `App.run_test()` runs the real app without a terminal; `Pilot` presses keys. The
clock is controlled by the test and the app draws every change at once (`live=False`), so
nothing here depends on timing.
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import resources

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot

from calu.protocol.messages import CLOSE_FELL_BEHIND, CLOSE_TOKEN_REVOKED
from calu.tui import scenarios
from calu.tui.app import CaluApp, TuiOptions
from calu.tui.fake import FakeSession, fake_connector
from calu.tui.model import ALL, MAIN
from calu.tui.screens.confirm import ConfirmScreen
from calu.tui.screens.detail import DetailScreen
from calu.tui.screens.text import TextScreen
from calu.tui.widgets.chrome import AgentTabs, Line, Lines

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


class Clock:
    """A clock the test moves."""

    def __init__(self) -> None:
        """Start at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Read the clock."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class Running:
    """A running app with its scripted session, for one test."""

    def __init__(
        self, app: CaluApp, pilot: Pilot[None], session: FakeSession, clock: Clock
    ) -> None:
        """Keep the parts together."""
        self.app, self.pilot, self.session, self.clock = app, pilot, session, clock

    async def press(self, *keys: str) -> None:
        """Press keys one after the other and let the app settle."""
        for key in keys:
            await self.pilot.press(key)
            await self.pilot.pause()

    async def type(self, text: str) -> None:
        """Type a text: the first character as a key, the rest inserted if the composer took it.

        Pressing every character as a key costs most of a test's time, and after the first
        one the composer only inserts. Where the first key does something else (it arms a
        decision, or the focus is elsewhere), every character is pressed.
        """
        await self.press("space" if text[0] == " " else text[0])
        composer = self.app.composer
        if self.app.focused is composer and composer.text and not self.app.armed:
            composer.insert(text[1:])
        elif text[1:]:
            await self.pilot.press(*["space" if char == " " else char for char in text[1:]])
        await self.pilot.pause()

    async def play(self, items: list[scenarios.Item]) -> None:
        """Send events live and let the app draw them."""
        await scenarios.play(self.session, items, self.clock.advance)
        await self.pilot.pause()

    def line(self, widget_id: str) -> str:
        """Return the text a one-line or multi-line widget shows."""
        widget = self.app.query_one(f"#{widget_id}")
        assert isinstance(widget, Line | Lines | AgentTabs)
        return widget.text

    @property
    def transcript(self) -> list[str]:
        """The visible lines of the transcript."""
        return self.app.transcript.visible_text()

    @property
    def composer(self) -> str:
        """The text in the composer."""
        return self.app.composer.text


@asynccontextmanager
async def running(
    scenario: str = "",
    size: tuple[int, int] = (120, 36),
    live_events: bool = True,
    session: FakeSession | None = None,
    options: TuiOptions | None = None,
) -> AsyncGenerator[Running]:
    """Run the app on a scenario.

    Args:
        scenario: A key of `scenarios.SCENARIOS`; "" starts with an idle, empty session.
        size: The terminal size.
        live_events: True plays the scenario after attaching (with times); False preloads it
            as history, which the app gets replayed.
        session: A prepared session to attach to instead of a new one.
        options: How the client starts; without them the bell is off.
    """
    clock = Clock()
    session = session or FakeSession()
    items = scenarios.SCENARIOS[scenario]() if scenario else []
    if not live_events:
        scenarios.preload(session, items)
    app = CaluApp(
        fake_connector(session), options or TuiOptions(bell=False), clock=clock, live=False
    )
    async with app.run_test(size=size, notifications=False) as pilot:
        await pilot.pause()
        run = Running(app, pilot, session, clock)
        if live_events:
            await run.play(items)
        yield run


async def test_the_screen_shows_state_transcript_and_what_waits() -> None:
    async with running("escalation") as run:
        status = run.line("status")
        assert "ESCALATED" in status and "1 needs you" in status and "opus / high" in status
        assert "attached" in status
        text = run.transcript
        assert text[0].startswith("▌ you  Rename the helper in gate.py and run the unit tests.")
        assert (
            "  ┊ thinking" in text
            and "  ┊ I rename it, update both, then run the unit tests." in text
        )
        assert any(row.startswith("  ESCALATE  Bash   force push of main") for row in text)
        card = run.line("card")
        assert card.startswith("DECISION 1 of 1") and "git push --force origin main" in card
        assert run.app.focused is run.app.composer
        assert "a a allow" in run.line("footer")
        panel = run.line("panel-lines")
        assert "NEEDS YOU (1)" in panel and "QUEUE (2)" in panel


async def test_the_users_messages_have_their_own_background() -> None:
    async with running("simple_turn") as run:
        transcript = run.app.transcript
        strip = transcript.render_line(0)
        assert strip.text.startswith("▌ you")
        user_background = {
            segment.style.bgcolor.name
            for segment in strip
            if segment.style and segment.style.bgcolor
        }
        answer_row = next(i for i, row in enumerate(run.transcript) if "I will rename" in row)
        answer_background = {
            segment.style.bgcolor.name
            for segment in transcript.render_line(answer_row)
            if segment.style and segment.style.bgcolor
        }
        tint = run.app.palette.user_bg.lower()
        assert user_background == {tint}
        assert tint not in answer_background


async def test_enter_sends_to_the_next_turn_and_tab_changes_the_send_mode() -> None:
    async with running("escalation") as run:
        assert "to main · next turn" in run.line("composer-head")
        await run.type("also update the changelog")
        await run.press("enter")
        assert run.composer == ""
        assert run.session.sent("chat.send")[-1] == {
            "text": "also update the changelog",
            "delivery": "queued",
            "to": "main",
        }
        await run.press("tab")
        assert "into the running turn" in run.line("composer-head")
        await run.type("stop after the tests")
        await run.press("enter")
        assert run.session.sent("chat.send")[-1]["delivery"] == "inject"
        await run.type("once the other way")
        await run.press("ctrl+g")
        assert run.session.sent("chat.send")[-1] == {
            "text": "once the other way",
            "delivery": "queued",
            "to": "main",
        }
        assert "QUEUE (5)" in run.line("panel-lines")


async def test_a_refused_message_stays_in_the_composer_with_the_reason() -> None:
    async with running("simple_turn") as run:
        run.session.fail["chat.send"] = "no Claude session is running"
        await run.type("hello")
        await run.press("enter")
        assert run.composer == "hello"
        assert run.line("composer-head") == "no Claude session is running"


async def test_ctrl_j_starts_a_new_line_and_up_walks_the_history() -> None:
    async with running("simple_turn") as run:
        await run.type("first")
        await run.press("ctrl+j")
        await run.type("second")
        assert run.composer == "first\nsecond"
        await run.press("enter")
        assert run.session.sent("chat.send")[-1]["text"] == "first\nsecond"
        await run.type("next one")
        await run.press("enter", "up")
        assert run.composer == "next one"
        await run.press("up")
        assert run.composer == "first\nsecond"


async def test_a_pressed_twice_allows_and_a_single_a_is_only_the_start_of_a_text() -> None:
    async with running("escalation") as run:
        await run.press("a")
        assert run.session.sent("escalation.decide") == []
        assert "press a again to allow" in run.line("card")
        await run.type("ll good")
        assert run.composer == "all good", "the first letter is typed with the rest"
        assert run.session.sent("escalation.decide") == []
        assert "a a allow" in run.line("card")
        await run.press("ctrl+u", "a", "a")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "allow", "note": None}
        ]
        assert run.line("card") == "" and "ESCALATED" not in run.line("status")
        assert any(row.startswith("  ALLOWED   Bash") for row in run.transcript)


async def test_d_pressed_twice_denies_and_escape_takes_the_first_press_back() -> None:
    async with running("escalation") as run:
        await run.press("d", "escape")
        assert run.app.armed == "" and run.session.sent("escalation.decide") == []
        await run.press("d", "d")
        assert run.session.sent("escalation.decide")[0]["decision"] == "deny"


async def test_a_note_goes_to_claude_with_the_decision() -> None:
    async with running("escalation") as run:
        await run.press("n")
        assert "note for Claude with the decision" in run.line("composer-head")
        await run.type("push to a branch instead")
        await run.press("enter")
        assert "note for Claude: push to a branch instead" in run.line("card")
        assert run.session.sent("chat.send") == [], "a note is no chat message"
        await run.press("d", "d")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "deny", "note": "push to a branch instead"}
        ]


async def test_the_chord_keys_decide_while_a_text_is_in_the_composer() -> None:
    async with running("escalation") as run:
        await run.type("a draft")
        await run.press("a")
        assert run.composer == "a drafta" and run.session.sent("escalation.decide") == []
        assert "alt+a allow" in run.line("footer")
        await run.press("alt+a")
        assert run.session.sent("escalation.decide") == [], "a deny rule stopped it: once more"
        assert "Stopped by a deny rule: press a again to allow" in run.line("card")
        await run.press("alt+a")
        assert "send the text in the input as a note with the allow?" in run.line("composer-head")
        await run.press("x")
        assert run.session.sent("escalation.decide") == [], "only y or n answer the question"
        await run.press("n")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "allow", "note": None}
        ]
        assert run.composer == "a drafta", "the draft is kept"


async def test_a_text_in_the_composer_can_go_along_as_the_note() -> None:
    async with running("escalation") as run:
        await run.type("please not on main")
        await run.press("alt+d", "y")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "deny", "note": "please not on main"}
        ]
        assert run.composer == ""


async def test_a_refusal_of_the_session_is_shown_on_the_card() -> None:
    async with running("escalation") as run:
        run.session.fail["escalation.decide"] = "the database is not reachable"
        await run.press("d", "d")
        assert "the session refused: the database is not reachable" in run.line("card")
        assert list(run.app.model.decisions) == ["e1"]


async def test_a_decision_made_by_another_client_closes_the_card() -> None:
    async with running("escalation") as run:
        await run.session.emit("escalation.resolved", {"escalation_id": "e1", "decision": "deny"})
        await run.pilot.pause()
        assert run.line("card") == ""
        assert any(row.startswith("  DENIED    Bash") for row in run.transcript)


async def test_several_decisions_are_answered_in_any_order() -> None:
    async with running("showcase") as run:
        assert run.line("card").startswith("DECISION 1 of 2")
        await run.press("right_square_bracket")
        assert run.line("card").startswith("QUESTION 2 of 2  ·  tests")
        await run.press("1")
        assert "(•) pytest" in run.line("card") and "enter sends the answers" in run.line("card")
        await run.press("enter")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e2", "answer": "pytest"}
        ]
        assert run.line("card").startswith("DECISION 1 of 1  ·  main")


async def test_a_set_of_questions_is_answered_with_digits_an_own_text_and_enter() -> None:
    async with running("question_multi", size=(80, 24)) as run:
        card = run.line("card")
        assert (
            card.startswith("QUESTION 1 of 1")
            and "Which test runner should the new tests use?" in card
        )
        await run.press("enter")
        assert "0 of 3 questions answered" in run.line("composer-head")
        await run.press("1")
        assert "Which checks should run before each commit?" in run.line("card")
        await run.press("1", "3", "right")
        assert "Where do the fixtures of the new tests go?" in run.line("card")
        await run.press("o")
        assert "your own answer to: Fixtures" in run.line("composer-head")
        await run.type("in tests/fixtures")
        await run.press("enter")
        assert "3 of 3 answered" in run.line("card")
        await run.press("left")
        assert "[x] unit tests" in run.line("card") and "[x] lint" in run.line("card")
        await run.press("enter")
        assert run.session.sent("escalation.decide") == [
            {
                "escalation_id": "e1",
                "answer": "Runner: pytest\nChecks: unit tests, lint\nFixtures: in tests/fixtures",
            }
        ]
        assert any(row.startswith("  ANSWERED  Ask") for row in run.transcript)


async def test_w_toggles_the_full_screen_detail_of_what_the_checker_saw() -> None:
    session = FakeSession()
    session.results["escalation.context"] = {
        "recent": "**User:** Rename the helper.",
        "chat_summary": "Nothing folded yet.",
        "checker_runs": [
            {
                "checker_name": "command-checker",
                "checker_version": 7,
                "model": "nemotron",
                "decision": "escalate",
                "reason": "A force push rewrites history.",
                "input_tokens": 4120,
                "output_tokens": 96,
                "duration_ms": 840,
            }
        ],
    }
    async with running("escalation", session=session) as run:
        await run.press("w")
        screen = run.app.screen
        assert isinstance(screen, DetailScreen)
        await run.pilot.pause()
        assert session.sent("escalation.context") == [{"escalation_id": "e1"}]
        for part in (
            "Step waiting for your decision  ·  main  ·  Bash",
            "why it was stopped",
            "matches the deny rule",
            "the step as the checker saw it",
            "git push --force origin main",
            "command-checker v7  nemotron  ESCALATE  4120 in / 96 out tokens, 840 ms",
            "A force push rewrites history.",
            "the conversation as the checker saw it",
            "User: Rename the helper.",
            "Nothing folded yet.",
        ):
            assert part in screen.text, part
        await run.press("w")
        assert not isinstance(run.app.screen, DetailScreen), "the same key returns"
        await run.press("w", "d", "d")
        assert session.sent("escalation.decide")[0]["decision"] == "deny"
        await run.pilot.pause()
        assert not isinstance(run.app.screen, DetailScreen), "it closes with the decision"


async def test_the_detail_of_an_edit_shows_the_whole_change() -> None:
    session = FakeSession()
    async with running("simple_turn", session=session) as run:
        await run.play(
            scenarios.escalation(
                "e9",
                "t9",
                "Edit",
                {"file_path": "src/gate.py", "old_string": "PASS", "new_string": "ASK"},
                "The edit changes what the gate lets through.",
                "changed the verdict of the gate",
                route="edit",
            )
        )
        session.results["escalation.context"] = {"files": {"src/gate.py": scenarios.BEFORE}}
        await run.press("w")
        await run.pilot.pause()
        screen = run.app.screen
        assert isinstance(screen, DetailScreen)
        assert "the change" in screen.text and "diff src/gate.py  +1 -1" in screen.text
        assert "-            return Verdict.ASK" not in screen.text
        assert (
            "+    return Verdict.ASK" in screen.text
            or "+            return Verdict.ASK" in screen.text
        )


async def test_new_output_does_not_move_what_is_read_further_up() -> None:
    async with running(size=(100, 30)) as run:
        await run.play(scenarios.long_session(60))
        transcript = run.app.transcript
        assert transcript.following and transcript.scroll_y == transcript.max_scroll_y
        await run.press("pageup", "pageup")
        offset, visible = transcript.scroll_y, run.transcript
        assert not transcript.following and offset < transcript.max_scroll_y
        assert "scrolled up" in str(run.app.query_one("#new-below").render())
        await run.play(
            [
                *scenarios.user("One more task."),
                *scenarios.thinking("Two files are left."),
                *scenarios.step("x1", "Read", {"file_path": "a.py"}, "read a.py", route="allow"),
                scenarios.Item("assistant.delta", {"message_id": "m9", "text": "Stream"}),
                scenarios.Item("assistant.delta", {"message_id": "m9", "text": "ing more"}),
            ]
        )
        assert transcript.scroll_y == offset, "the scroll offset is unchanged"
        assert run.transcript == visible, "and so is every visible line"
        assert transcript.new_below == 4
        assert "4 new below" in str(run.app.query_one("#new-below").render())
        assert transcript.all_text()[-1] == "  Streaming more ▍", (
            "the output was appended at the end"
        )
        await run.press("end")
        assert transcript.following and transcript.scroll_y == transcript.max_scroll_y
        assert transcript.new_below == 0 and not run.app.query_one("#new-below").has_class("-shown")
        await run.play(scenarios.text("Done."))
        assert transcript.scroll_y == transcript.max_scroll_y, "at the end it follows again"
        assert any("Done." in row for row in run.transcript)


async def test_a_block_that_grows_above_the_view_does_not_move_the_view_either() -> None:
    async with running(size=(100, 30)) as run:
        await run.play(scenarios.long_session(60))
        transcript = run.app.transcript
        await run.press("pageup")
        visible = run.transcript
        first_step = next(b for b in run.app.model.blocks if type(b).__name__ == "StepBlock")
        transcript.toggle(first_step.id)
        await run.pilot.pause()
        assert run.transcript == visible, "a step expanded far above leaves the visible lines"


async def test_the_scroll_keys_work_while_the_composer_keeps_the_focus() -> None:
    async with running(size=(100, 30)) as run:
        await run.play(scenarios.long_session(60))
        transcript = run.app.transcript
        bottom = transcript.scroll_y
        await run.type("half a message")
        await run.press("shift+up", "shift+up", "shift+up")
        assert transcript.scroll_y == bottom - 3
        await run.press("pageup")
        assert transcript.scroll_y < bottom - 10
        await run.press("ctrl+home")
        assert transcript.scroll_y == 0
        await run.press("pagedown")
        assert 0 < transcript.scroll_y < bottom
        await run.press("ctrl+end")
        assert transcript.scroll_y == bottom and transcript.following
        assert run.app.focused is run.app.composer and run.composer == "half a message"
        await run.press("ctrl+u", "home")
        assert transcript.scroll_y == 0, "home and end scroll while the composer is empty"
        await run.press("end")
        assert transcript.following


async def test_alt_arrows_switch_between_the_agents_and_their_transcripts() -> None:
    async with running("showcase") as run:
        assert run.line("tabs").startswith("  main 1!   explore bg *   tests 1! *   all")
        await run.press("alt+right")
        assert run.app.model.selected_tab == "a1"
        first, second = [row for row in run.transcript if row]
        assert first.startswith("  PASS      Grep   searched the project for check_step")
        assert second.startswith("  PASS      Grep   searched the documentation for check_step")
        assert first.endswith("0.5s") and second.endswith("running")
        head = run.line("agent-head")
        assert head.startswith(" explore  ·  Explore  ·  background")
        assert "task  Find every caller of check_step outside src/." in head
        assert "to explore · relayed by the main agent" in run.line("composer-head")
        await run.type("also look in docs/")
        await run.press("enter")
        assert run.session.sent("chat.send") == [
            {"text": "also look in docs/", "delivery": "inject", "to": "a1"}
        ]
        await run.press("alt+right")
        assert run.app.model.selected_tab == "a2"
        assert "tests takes no messages" in run.line("composer-head")
        assert run.line("card").startswith("QUESTION 2 of 2  ·  tests"), "its own question is shown"
        await run.press("alt+right")
        assert run.app.model.selected_tab == ALL
        assert any(row.startswith("  explore   PASS      Grep") for row in run.transcript)
        await run.press("alt+right")
        assert run.app.model.selected_tab == MAIN
        await run.press("alt+left", "alt+left")
        assert run.app.model.selected_tab == "a2"
        await run.press("alt+1")
        assert run.app.model.selected_tab == MAIN and not run.app.query_one(
            "#agent-head"
        ).has_class("-shown")


async def test_an_agent_is_found_by_name_and_tab_reaches_a_background_subagent() -> None:
    async with running("showcase") as run:
        await run.type("/agent expl")
        await run.press("enter")
        assert run.app.model.selected_tab == "a1"
        await run.press("alt+1", "tab", "tab")
        assert "to explore · relayed by the main agent" in run.line("composer-head")
        await run.press("tab")
        assert "to main · next turn" in run.line("composer-head")
        await run.session.emit(
            "agents.update",
            {"agents": [scenarios.agent_info("a1", "explore", "Explore", True, "completed")]},
        )
        await run.press("shift+tab")
        assert "into the running turn" in run.line("composer-head"), "a finished agent is no target"


async def test_a_side_question_is_answered_in_the_side_panel_and_sends_no_chat_message() -> None:
    async with running("escalation") as run:
        run.session.side_answers["Which callers?"] = "Two, both in `src/gate.py`."
        await run.press("f3")
        assert "side question" in run.line("composer-head")
        assert "Not part of the conversation" in run.line("side-lines").replace("\n", " ")
        events_before = len(run.session.events)
        await run.type("Which callers?")
        await run.press("enter")
        await run.pilot.pause()
        assert run.session.sent("side.ask") == [{"question": "Which callers?", "history": []}]
        side = run.line("side-lines")
        assert "▌ you  Which callers?" in side and "  Two, both in src/gate.py." in side
        assert run.session.sent("chat.send") == [] and len(run.session.events) == events_before
        assert run.line("card").startswith("DECISION 1 of 1"), "the open decision is still there"
        await run.press("escape")
        assert "to main" in run.line("composer-head") and not run.app.side_open
        await run.type("/btw And the tests?")
        await run.press("enter")
        await run.pilot.pause()
        assert run.session.sent("side.ask")[-1] == {
            "question": "And the tests?",
            "history": [{"question": "Which callers?", "answer": "Two, both in `src/gate.py`."}],
        }
        assert run.app.side_open and "And the tests?" in run.line("side-lines")


async def test_a_session_without_side_questions_says_so() -> None:
    session = FakeSession()
    snapshot = session.snapshot
    session.snapshot = lambda: {k: v for k, v in snapshot().items() if k != "rate_limits"}  # type: ignore[method-assign]
    async with running("simple_turn", session=session) as run:
        await run.press("f3")
        assert not run.app.side_open
        head = run.line("composer-head")
        assert head == (
            "/btw: not available from this session: every message enters the main conversation"
        )
        assert session.sent("side.ask") == []
        assert "quota    not available" in run.line("panel-lines")


async def test_typing_while_the_transcript_has_the_focus_lands_in_the_composer() -> None:
    async with running("simple_turn") as run:
        await run.press("ctrl+up")
        assert run.app.focused is run.app.transcript
        assert "j k move" in run.line("footer")
        await run.press("k", "k", "k")
        cursor = run.app.transcript.cursor
        assert cursor is not None and any(row.startswith("> PASS") for row in run.transcript)
        await run.press("enter")
        assert any(
            row.startswith("    │ diff src/gate.py") for row in run.app.transcript.all_text()
        )
        await run.press("x")
        assert run.app.focused is run.app.composer and run.composer == "x"
        await run.press("ctrl+up", "escape")
        assert run.app.focused is run.app.composer


async def test_thinking_is_shown_and_alt_t_collapses_it() -> None:
    async with running() as run:
        await run.play(
            [
                *scenarios.user("Think first."),
                scenarios.Item(
                    "assistant.delta", {"message_id": "m1", "text": "Step one.", "thinking": True}
                ),
            ]
        )
        assert "  ┊ thinking…" in run.transcript and "  ┊ Step one." in run.transcript
        await run.play(
            [
                scenarios.Item(
                    "assistant.thinking", {"message_id": "m1", "text": "Step one.\nStep two."}
                )
            ]
        )
        assert "  ┊ thinking" in run.transcript and "  ┊ Step two." in run.transcript
        await run.press("alt+t")
        assert "  ┊ thinking · 2 lines · alt+t shows" in run.transcript
        assert "  ┊ Step two." not in run.transcript
        await run.press("alt+t")
        assert "  ┊ Step two." in run.transcript


async def test_the_side_panel_is_a_column_when_wide_and_opens_over_the_transcript_when_narrow() -> (
    None
):
    async with running("showcase", size=(120, 36)) as run:
        side = run.app.query_one("#side")
        assert side.display and side.region.width == 36 and side.region.x == 84
        assert "alt+←/→ switches" in run.line("tabs")
    async with running("showcase", size=(80, 24)) as run:
        side = run.app.query_one("#side")
        assert not side.display
        assert "2 queued · 1 mark · f2 panel" in run.line("tabs")
        assert run.app.transcript.region.width == 80
        assert run.line("card").startswith("DECISION 1 of 2")
        await run.press("f2")
        assert side.display and side.region.right == 80
        assert run.app.transcript.region.width == 80, "the panel lies over the transcript"
        assert "NEEDS YOU (2)" in run.line("panel-lines")
        await run.press("escape")
        assert not side.display


async def test_escape_twice_interrupts_a_running_turn() -> None:
    async with running("escalation") as run:
        await run.press("escape")
        assert "esc again to interrupt" in run.line("footer")
        assert run.session.sent("interrupt") == []
        await run.press("escape")
        await run.pilot.pause()
        assert run.session.sent("interrupt") == [{}]
        assert run.app.model.state == "idle" and run.line("card") == ""
        await run.press("escape", "escape")
        assert len(run.session.sent("interrupt")) == 1, "an idle session has nothing to interrupt"


async def test_two_escapes_far_apart_do_not_interrupt() -> None:
    async with running("escalation") as run:
        await run.press("escape")
        run.clock.advance(5.0)
        await run.press("escape")
        assert run.session.sent("interrupt") == []


async def test_slash_commands_do_what_the_line_client_does() -> None:
    async with running("escalation") as run:
        await run.type("/al")
        assert "/allow" in run.line("composer-head") and "tab completes" in run.line(
            "composer-head"
        )
        await run.press("tab")
        assert run.composer == "/allow "
        await run.press("ctrl+u")
        await run.type("/deny e1 not on main")
        await run.press("enter")
        assert run.session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "deny", "note": "not on main"}
        ]
        await run.type("/inject look at the tests first")
        await run.press("enter")
        assert run.session.sent("chat.send")[-1] == {
            "text": "look at the tests first",
            "delivery": "inject",
            "to": "main",
        }
        await run.type("/level edits")
        await run.press("enter")
        await run.pilot.pause()
        assert "Confirm edits" in run.line("status")
        await run.type("/model sonnet")
        await run.press("enter")
        await run.pilot.pause()
        assert run.session.sent("model.set") == [{"model": "sonnet"}]
        assert "sonnet / high" in run.line("status")
        await run.type("/nonsense")
        await run.press("enter")
        assert "no command /nonsense" in run.line("composer-head") and run.composer == "/nonsense"
        await run.press("ctrl+u")
        await run.type("/shutdown")
        await run.press("enter")
        assert isinstance(run.app.screen, ConfirmScreen) and run.session.sent("shutdown") == []
        await run.press("s", "h", "enter")
        assert isinstance(run.app.screen, ConfirmScreen), "the whole word is needed"
        await run.press("escape")
        assert not isinstance(run.app.screen, ConfirmScreen)
        assert run.session.sent("shutdown") == []
        await run.type("/shutdown")
        await run.press("enter")
        await run.press(*"shutdown", "enter")
        await run.pilot.pause()
        assert run.session.sent("shutdown") == [{}]


async def test_pages_show_usage_help_and_the_checkers_context() -> None:
    async with running("simple_turn") as run:
        assert "ctx 21%" in run.line("status"), "asked once after attaching"
        run.session.rate_limits = [{"type": "five_hour", "status": "allowed", "utilization": 0.4}]
        await run.type("/usage")
        await run.press("enter")
        await run.pilot.pause()
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "context  21% of 1000k tokens" in page.text and "5h       40% used" in page.text
        assert "claude-opus-5" in page.text
        await run.press("escape")
        assert not isinstance(run.app.screen, TextScreen)
        await run.press("f1")
        page = run.app.screen
        assert isinstance(page, TextScreen) and "/btw" in page.text
        assert (
            "Withdraw or edit a queued message" in page.text and "cannot be withdrawn" in page.text
        )
        await run.press("q")
        run.session.results["context.get"] = {"chat_summary": "S", "recent": "**User:** hi"}
        await run.type("/context")
        await run.press("enter")
        await run.pilot.pause()
        page = run.app.screen
        assert isinstance(page, TextScreen) and "**User:** hi" in page.text


async def test_the_palette_lists_the_commands() -> None:
    async with running("simple_turn") as run:
        await run.press("ctrl+p")
        await run.pilot.pause()
        assert type(run.app.screen).__name__ == "CommandPalette"
        await run.type("supervision")
        await run.pilot.pause(0.5)
        await run.press("enter")
        await run.pilot.pause(0.3)
        await run.type("Confirm edits")
        await run.pilot.pause(0.5)
        await run.press("enter")
        await run.pilot.pause(0.3)
        assert run.session.sent("auto_escalate.set")[-1]["edits"] is True


async def test_a_history_that_was_replayed_is_drawn_once_and_can_be_answered() -> None:
    async with running("showcase", live_events=False) as run:
        assert "2 need you" in run.line("status")
        assert "waiting since before this client attached" in run.line("card")
        assert any("waits for you" in row for row in run.transcript)
        await run.press("d", "d")
        assert run.session.sent("escalation.decide")[0]["escalation_id"] == "e1"


async def test_the_connection_state_is_shown_and_sending_is_off_while_detached() -> None:
    async with running("escalation") as run:
        run.session.drop(CLOSE_FELL_BEHIND, "fell behind")
        run.session.record("assistant.text", {"text": "written meanwhile"})
        await run.pilot.pause(0.2)
        assert run.session.connects == 2 and "attached" in run.line("status")
        assert any("written meanwhile" in row for row in run.app.transcript.all_text())
        run.session.drop(CLOSE_TOKEN_REVOKED, "the client token was revoked")
        await run.pilot.pause(0.2)
        assert "detached: the client token was revoked" in run.line("status")
        assert "not attached" in run.line("composer-head")
        await run.type("still here")
        await run.press("enter")
        assert run.composer == "still here" and "not sent" in run.line("composer-head")


async def test_a_long_session_costs_the_same_per_step_as_a_short_one() -> None:
    session = FakeSession()
    scenarios.preload(session, scenarios.long_session(2000))
    async with running(session=session, size=(120, 36)) as run:
        transcript = run.app.transcript
        assert len(run.app.model.blocks) == 2600
        assert len(list(run.app.query("*"))) < 40, "no widget per block"
        assert transcript.following and "file_1999.py" in "\n".join(run.transcript)
        started = time.perf_counter()
        for number in range(50):
            await scenarios.play(
                session,
                scenarios.step(f"late{number}", "Bash", {"command": "true"}, f"late step {number}"),
            )
        await run.pilot.pause()
        elapsed = time.perf_counter() - started
        assert "late step 49" in "\n".join(run.transcript)
        assert elapsed < 5.0, f"50 steps after 2,000 took {elapsed:.1f} s"
        await run.press("ctrl+home")
        assert run.transcript[0].startswith("▌ you  Task 1")


def test_the_stylesheet_ships_with_the_package() -> None:
    sheet = resources.files("calu.tui").joinpath("tui.tcss")
    assert sheet.is_file() and "#transcript" in sheet.read_text(encoding="utf-8")
