"""The second part of the full-screen client, driven by keys against a scripted session.

Starting and resuming, the stored history, the diff screen with comments, the side panel as a
list, held drafts, search, export, path completion, pasted texts, the stored settings and the
screen as text at both sizes. The helpers are those of `test_tui_app`.
"""

import re
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual import events

from calu.tui import scenarios, settings
from calu.tui.app import CaluApp, TuiOptions
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
from calu.tui.model import JoinBlock, StepBlock
from calu.tui.screens.confirm import ConfirmScreen
from calu.tui.screens.diff import DiffScreen
from calu.tui.screens.text import TextScreen
from calu.tui.widgets.chrome import PanelList
from tests.unit.test_tui_app import Clock, Running, running

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


def panel(run: Running) -> PanelList:
    """Return the side panel's list."""
    return run.app.query_one("#panel-lines", PanelList)


# ------------------------------------------------------------------ start, model, effort


async def test_a_stopped_session_shows_a_start_card_and_enter_starts_it() -> None:
    async with running("stopped") as run:
        card = run.line("card")
        assert "SESSION STOPPED" in card and "enter resumes the conversation 5f0c2a9e" in card
        assert "enter start the session" in run.line("footer")
        await run.type("/model sonnet")
        await run.press("enter")
        assert run.session.sent("session.start") == [], "the model is chosen for the start"
        assert "with sonnet / high" in run.line("card")
        await run.type("/effort max")
        await run.press("enter")
        assert "with sonnet / max" in run.line("card")
        await run.press("enter")
        await run.pilot.pause()
        assert run.session.sent("session.start") == [
            {"resume_id": "5f0c2a9e-claude", "model": "sonnet", "effort": "max"}
        ]
        assert "SESSION STOPPED" not in run.line("card")
        assert "sonnet / max" in run.line("status")


async def test_a_failed_start_keeps_the_card_and_the_choice() -> None:
    async with running("stopped") as run:
        run.session.fail["session.start"] = "claude is not signed in"
        await run.type("/effort low")
        await run.press("enter", "enter")
        await run.pilot.pause()
        assert "the start failed: claude is not signed in" in run.line("composer-head")
        assert "SESSION STOPPED" in run.line("card") and "/ low" in run.line("card")


async def test_the_effort_of_a_running_session_changes_by_a_confirmed_stop_and_resume() -> None:
    async with running("simple_turn") as run:
        await run.type("/effort max")
        await run.press("enter")
        screen = run.app.screen
        assert isinstance(screen, ConfirmScreen)
        assert "resume this conversation with opus / max" in screen.title_text
        assert any("background tasks and subagents" in line for line in screen.consequences)
        await run.press("n")
        assert run.session.sent("session.stop") == []
        assert "max stays chosen for the next start" in run.line("composer-head")
        await run.type("/restart")
        await run.press("enter", "y")
        await run.pilot.pause()
        assert run.session.sent("session.stop") == [{}]
        assert run.session.sent("session.start") == [
            {"resume_id": "5f0c2a9e-claude", "effort": "max"}
        ]
        assert "opus / max" in run.line("status")


async def test_restart_takes_model_and_effort_in_any_order() -> None:
    async with running("simple_turn") as run:
        await run.type("/restart high haiku")
        await run.press("enter", "y")
        await run.pilot.pause()
        assert run.session.sent("session.start")[-1] == {
            "resume_id": "5f0c2a9e-claude",
            "model": "haiku",
            "effort": "high",
        }


async def test_a_session_that_cannot_change_the_model_live_offers_the_restart() -> None:
    session = FakeSession()
    snapshot = session.snapshot
    session.snapshot = lambda: {  # type: ignore[method-assign]
        key: value for key, value in snapshot().items() if key != "rate_limits"
    }
    async with running("simple_turn", session=session) as run:
        await run.type("/model sonnet")
        await run.press("enter")
        assert isinstance(run.app.screen, ConfirmScreen) and run.session.sent("model.set") == []
        await run.press("y")
        await run.pilot.pause()
        assert run.session.sent("session.start")[-1]["model"] == "sonnet"


async def test_another_stored_conversation_is_resumed_from_the_list() -> None:
    async with running("simple_turn") as run:
        run.session.results["session.list"] = {
            "sessions": [
                {
                    "calu_session_id": "c1",
                    "claude_session_id": "5f0c2a9e-claude",
                    "state": "running",
                    "directory": "/work/myproject",
                    "started_at": "2026-11-12T09:00:00Z",
                    "first_input": "Rename the helper",
                },
                {
                    "calu_session_id": "c0",
                    "claude_session_id": "11aa22bb-older",
                    "state": "ended",
                    "directory": "/work/myproject",
                    "started_at": "2026-11-10T16:30:00Z",
                    "first_input": "Find out where the step gate decides.",
                },
            ]
        }
        await run.type("/sessions")
        await run.press("enter")
        await run.pilot.pause()
        assert type(run.app.screen).__name__ == "CommandPalette"
        await run.type("Find out")
        await run.pilot.pause(0.5)
        run.session.results["session.history"] = scenarios.stored_history()
        await run.press("enter")
        await run.pilot.pause()
        screen = run.app.screen
        assert isinstance(screen, ConfirmScreen) and "11aa22bb" in screen.title_text
        await run.press("y")
        await run.pilot.pause()
        await run.pilot.pause()
        assert run.session.sent("session.stop") == [{}]
        assert run.session.sent("session.start") == [{"resume_id": "11aa22bb-older"}]
        texts = "\n".join(run.app.transcript.all_text())
        assert "Find out where the step gate decides." in texts, "the other conversation is shown"
        assert "Rename the helper in gate.py" not in texts


# ------------------------------------------------------------------ stored history


async def test_the_stored_history_stands_above_the_replayed_events() -> None:
    session = FakeSession()
    session.results["session.history"] = scenarios.stored_history()
    async with running("simple_turn", live_events=False, session=session) as run:
        blocks = run.app.model.blocks
        join = next(index for index, block in enumerate(blocks) if isinstance(block, JoinBlock))
        assert run.app.model.stored_blocks == join == 9
        stored = [block for block in blocks[:join] if isinstance(block, StepBlock)]
        assert [step.tool for step in stored] == ["Grep", "Edit", "Bash"]
        assert session.sent("session.history") == [{"limit": 500}]
        texts = run.app.transcript.all_text()
        assert any("above: stored history" in line for line in texts)
        first_live = next(i for i, line in enumerate(texts) if "Rename the helper" in line)
        assert any("searched for the definition" in line for line in texts[:first_live])
        assert "turn 3" in "\n".join(texts), "the turns go on counting after the stored ones"


async def test_steps_the_session_replays_are_not_shown_twice() -> None:
    session = FakeSession()
    items = scenarios.SCENARIOS["simple_turn"]()
    replayed = [item.params.get("tool_use_id") for item in items if item.method == "tool.use"]
    history = scenarios.stored_history()
    history["steps"][2]["tool_use_id"] = replayed[0]
    session.results["session.history"] = history
    async with running("simple_turn", live_events=False, session=session) as run:
        ids = [b.tool_use_id for b in run.app.model.blocks if isinstance(b, StepBlock)]
        assert ids.count(replayed[0]) == 1 and ids[:2] == ["h1", "h2"]


async def test_a_session_without_a_stored_history_attaches_as_before() -> None:
    session = FakeSession()
    session.fail["session.history"] = "the database cannot be read"
    async with running("simple_turn", live_events=False, session=session) as run:
        assert run.app.model.stored_blocks == 0
        assert any("Rename the helper" in line for line in run.app.transcript.all_text())


# ------------------------------------------------------------------ diff and comments


async def open_diff(run: Running) -> DiffScreen:
    """Put the transcript's cursor on the edit and press D."""
    edit = next(b for b in run.app.model.blocks if isinstance(b, StepBlock) and b.diffs)
    run.app.transcript.focus()
    run.app.transcript.set_cursor(edit.id)
    await run.press("D")
    screen = run.app.screen
    assert isinstance(screen, DiffScreen)
    return screen


async def test_the_diff_is_side_by_side_when_wide_and_unified_when_narrow() -> None:
    async with running("simple_turn") as run:
        screen = await open_diff(run)
        assert screen.split is True
        first = screen.text.splitlines()[0]
        assert "def check_step(" in first and "│" in first and "def judge_step(" in first
        await run.press("s")
        assert screen.split is False
        rows = screen.text.splitlines()
        assert any(row.lstrip("> ").startswith("1      │ -def check_step(") for row in rows)
        assert any("│ +def judge_step(" in row for row in rows)
        await run.press("escape")
        assert not isinstance(run.app.screen, DiffScreen)
    async with running("simple_turn", size=(80, 24)) as run:
        screen = await open_diff(run)
        assert screen.split is False


async def test_brackets_jump_between_the_changes_of_a_file() -> None:
    async with running("simple_turn") as run:
        screen = await open_diff(run)
        first = screen.cursor
        assert screen.rows[first].kind == "changed"
        await run.press("right_square_bracket")
        assert screen.cursor > first and screen.rows[screen.cursor].kind == "changed"
        await run.press("right_square_bracket")
        assert "no further change" in screen.message
        await run.press("left_square_bracket")
        assert screen.cursor == first


async def test_selected_lines_of_a_diff_are_commented_on() -> None:
    async with running("simple_turn") as run:
        screen = await open_diff(run)
        await run.press("s", "v", "j")
        assert screen.selected() == (screen.cursor - 1, screen.cursor)
        await run.press("enter")
        assert screen.commenting
        await run.press(*"rename", "space", *"it", "enter")
        await run.pilot.pause()
        sent = run.session.sent("comment.add")
        assert len(sent) == 1
        assert {
            k: sent[0][k] for k in ("kind", "path", "side", "start_line", "end_line", "text")
        } == {
            "kind": "diff",
            "path": "src/gate.py",
            "side": "after",
            "start_line": 1,
            "end_line": 1,
            "text": "rename it",
        }
        assert (
            sent[0]["before_text"] == scenarios.BEFORE and sent[0]["after_text"] == scenarios.AFTER
        )
        assert "comment on src/gate.py:1 sent to Claude" in screen.message
        assert not screen.commenting and screen.mark_row is None


async def test_a_refused_comment_says_why_and_keeps_the_selection() -> None:
    async with running("simple_turn") as run:
        run.session.fail["comment.add"] = "no Claude session is running"
        screen = await open_diff(run)
        await run.press("v", "enter", "x", "enter")
        await run.pilot.pause()
        assert "not sent: no Claude session is running" in screen.message
        assert screen.mark_row is not None


async def test_a_tab_without_an_edit_says_that_there_is_no_diff() -> None:
    async with running() as run:
        await run.type("/diff")
        await run.press("enter")
        assert not isinstance(run.app.screen, DiffScreen)
        assert "no step of this tab has changed a file yet" in run.line("composer-head")


# ------------------------------------------------------------------ side panel


async def test_the_side_panel_is_a_list_with_a_cursor() -> None:
    async with running("showcase") as run:
        await run.press("f2")
        assert run.app.focused is panel(run)
        assert panel(run).cursor == ("decision", "e1")
        assert "show on the card" in run.line("footer")
        await run.press("j", "enter")
        assert run.app.selected == "e2" and run.app.focused is run.app.composer
        await run.press("f2")
        while panel(run).cursor is not None and panel(run).cursor[0] != "queue":  # type: ignore[index]
            await run.press("j")
        assert "enter text · c copy · r reuse" in run.line("panel-lines")
        await run.press("enter")
        page = run.app.screen
        assert isinstance(page, TextScreen) and "also bump the version" in page.text
        await run.press("escape")
        await run.press("r")
        assert run.composer.startswith("also bump the version")
        assert "stays queued" in run.line("composer-head")
        assert run.app.focused is run.app.composer


async def test_a_mark_is_cleared_from_the_panel_after_a_yes() -> None:
    async with running("showcase") as run:
        await run.press("f2")
        while panel(run).cursor is not None and panel(run).cursor[0] != "mark":  # type: ignore[index]
            await run.press("j")
        await run.press("x")
        assert "clear the honesty mark on src/gate.py:judge_step?" in run.line("composer-head")
        await run.press("n")
        assert run.session.sent("honesty.clear") == []
        await run.press("f2", "x", "y")
        await run.pilot.pause()
        assert len(run.session.sent("honesty.clear")) == 1
        assert "HONESTY (0)" in run.line("panel-lines")


async def test_on_a_narrow_screen_f2_opens_the_panel_and_escape_closes_it() -> None:
    async with running("showcase", size=(80, 24)) as run:
        await run.press("f2")
        assert run.app.panel_open and run.app.focused is panel(run)
        await run.press("escape")
        assert not run.app.panel_open and run.app.focused is run.app.composer


# ------------------------------------------------------------------ held drafts


async def test_a_held_draft_is_sent_when_the_session_turns_idle() -> None:
    async with running("escalation") as run:
        await run.type("then tag the release")
        await run.press("alt+h")
        assert run.app.held == ["then tag the release"] and run.composer == ""
        assert run.session.sent("chat.send") == []
        assert "HELD (1)  not sent" in run.line("panel-lines")
        await run.press("d", "d")
        await run.play([*scenarios.turn_end(0.1), scenarios.state("idle")])
        await run.pilot.pause()
        assert run.app.held == []
        assert run.session.sent("chat.send") == [
            {"text": "then tag the release", "delivery": "queued", "to": "main"}
        ]


async def test_a_held_draft_can_be_edited_deleted_or_sent_from_the_panel() -> None:
    async with running("escalation") as run:
        for text in ("first draft", "second draft", "third draft"):
            run.app.run_command("hold", text)
        await run.press("f2")
        while panel(run).cursor is not None and panel(run).cursor[0] != "held":  # type: ignore[index]
            await run.press("j")
        await run.press("d")
        assert run.app.held == ["second draft", "third draft"]
        await run.press("s")
        await run.pilot.pause()
        assert run.session.sent("chat.send")[-1]["text"] == "second draft"
        await run.press("enter")
        assert run.composer == "third draft" and run.app.held == []
        assert run.app.focused is run.app.composer


async def test_detaching_with_held_drafts_asks_first() -> None:
    async with running("escalation") as run:
        run.app.run_command("hold", "do not lose me")
        await run.press("ctrl+d")
        assert "1 held draft is not sent and will be lost: detach?" in run.line("composer-head")
        assert run.app.is_running
        await run.press("n")
        assert run.app.held == ["do not lose me"] and run.app.ask is None


# ------------------------------------------------------------------ search, copy, export


async def test_the_transcript_is_searched_as_the_text_is_typed() -> None:
    async with running("simple_turn") as run:
        transcript = run.app.transcript
        await run.press("ctrl+f")
        assert "search the transcript" in run.line("composer-head")
        await run.type("gate")
        assert len(transcript.matches) == 5 and transcript.match == 4
        assert "match 5 of 5" in run.line("composer-head")
        await run.press("enter")
        assert transcript.match == 3
        await run.press("up")
        assert transcript.match == 2
        await run.press("down")
        assert transcript.match == 3
        assert run.session.sent("chat.send") == [], "a search sends nothing"
        await run.type("zzz")
        assert "no line contains this" in run.line("composer-head")
        await run.press("escape")
        assert transcript.needle == "" and run.composer == "" and run.app.mode == "chat"


async def test_a_search_jumps_to_matches_outside_the_view() -> None:
    async with running(size=(100, 30)) as run:
        await run.play(scenarios.long_session(60))
        transcript = run.app.transcript
        await run.type("/search file_3.py")
        await run.press("enter")
        assert len(transcript.matches) == 1
        assert not transcript.following
        assert any("file_3.py" in line for line in run.transcript)


async def test_slash_in_the_transcript_starts_a_search() -> None:
    async with running("simple_turn") as run:
        await run.press("ctrl+up", "slash")
        assert run.app.mode == "search" and run.app.focused is run.app.composer


async def test_copy_takes_the_answer_a_command_or_a_diff() -> None:
    async with running("simple_turn") as run:
        copied: list[str] = []
        run.app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        for what in ("answer", "command", "diff", "nonsense"):
            run.app.run_command("copy", what)
        assert copied[0].startswith("The rename is done.")
        assert copied[1] == "uv run pytest tests/unit -q"
        assert copied[2].startswith("--- a/src/gate.py\n+++ b/src/gate.py")
        assert len(copied) == 3 and "usage: /copy" in run.line("composer-head")


async def test_export_writes_the_transcript_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    async with running("simple_turn") as run:
        await run.type("/export out.md")
        await run.press("enter")
        written = (tmp_path / "out.md").read_text(encoding="utf-8")
        assert "## You" in written and "- **PASS** `Edit`: renamed check_step" in written
        assert f"transcript written to {tmp_path / 'out.md'}" in run.line("composer-head")
        await run.type("/export out.md")
        await run.press("enter")
        assert "exists already" in run.line("composer-head")
        await run.press("ctrl+u")
        await run.type("/export")
        await run.press("enter")
        assert len(list(tmp_path.glob("calu-transcript-*.md"))) == 1


async def test_i_shows_the_task_of_a_subagent_and_what_it_returned() -> None:
    async with running("showcase") as run:
        call = next(
            b
            for b in run.app.model.blocks
            if isinstance(b, StepBlock) and b.tool_use_id in run.app.model.agents
        )
        run.app.transcript.focus()
        run.app.transcript.set_cursor(call.id)
        await run.press("i")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "The task it was given" in page.text and "What it returned" in page.text
        assert str(call.input.get("prompt"))[:20] in page.text


# ------------------------------------------------------------------ composer


async def test_at_completes_a_path_of_the_project(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "gate.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("", encoding="utf-8")
    async with running(session=FakeSession(workspace=str(tmp_path))) as run:
        await run.type("look at @s")
        assert "src/  setup.cfg" in run.line("composer-head")
        await run.press("tab")
        assert run.composer == "look at @s", "two paths, nothing in common beyond the s"
        await run.type("r")
        await run.press("tab")
        assert run.composer == "look at @src/"
        await run.press("tab")
        assert run.composer == "look at @src/gate.py "
        assert run.app.target == "main" and run.app.delivery == "queued", "tab completed only"


async def test_at_says_when_the_project_is_not_on_this_machine() -> None:
    async with running() as run:
        await run.type("see @src")
        assert "only where the project directory is on this machine" in run.line("composer-head")


async def test_a_large_paste_is_one_marker_and_is_sent_in_full() -> None:
    async with running() as run:
        pasted = "\n".join(f"line {number}" for number in range(1, 41))
        await run.type("log: ")
        run.app.post_message(events.Paste(pasted))
        await run.pilot.pause()
        assert run.composer == "log: [pasted 40 lines #1]"
        await run.press("enter")
        await run.pilot.pause()
        assert run.session.sent("chat.send")[0]["text"] == f"log: {pasted}"


async def test_a_paste_marker_can_be_expanded_and_a_small_paste_is_plain_text() -> None:
    async with running() as run:
        run.app.post_message(events.Paste("one\ntwo"))
        await run.pilot.pause()
        assert run.composer == "one\ntwo"
        run.app.composer.set_text("")
        run.app.post_message(events.Paste("x\n" * 9))
        await run.pilot.pause()
        assert run.composer == "[pasted 10 lines #1]"
        await run.press("alt+e")
        assert run.composer == "x\n" * 9


async def test_sent_texts_are_kept_between_runs(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    options = TuiOptions(bell=False, history_path=path)
    async with running(options=options) as run:
        await run.type("the first message")
        await run.press("enter")
        await run.pilot.pause()
    assert settings.load_history(path) == ["the first message"]
    async with running(options=TuiOptions(bell=False, history_path=path)) as run:
        await run.press("up")
        assert run.composer == "the first message"


# ------------------------------------------------------------------ settings


async def test_theme_bell_and_thinking_are_stored_in_the_settings_file(tmp_path: Path) -> None:
    path = tmp_path / "tui.toml"
    async with running("simple_turn", options=TuiOptions(bell=True, settings_path=path)) as run:
        await run.type("/theme calu-light")
        await run.press("enter")
        await run.type("/bell")
        await run.press("enter")
        assert "bell for a new waiting step: off" in run.line("composer-head")
        await run.press("alt+t")
    stored = settings.load(path)
    assert (stored.theme, stored.bell, stored.thinking) == ("calu-light", False, False)


async def test_keys_of_the_settings_file_replace_the_default_binding() -> None:
    options = TuiOptions(bell=False, keys={"agent-next": "ctrl+n", "nonsense": "x"})
    async with running("showcase", options=options) as run:
        assert "no binding is called nonsense" in run.line("composer-head")
        before = run.app.model.selected_tab
        await run.press("ctrl+n")
        assert run.app.model.selected_tab != before
        await run.press("alt+right")
        assert run.app.model.selected_tab != "main" or before != "main"


async def test_the_panel_width_comes_from_the_settings() -> None:
    async with running("showcase", options=TuiOptions(bell=False, panel_width=50)) as run:
        assert run.app.query_one("#side").outer_size.width == 50


async def test_toasts_can_be_switched_off() -> None:
    async with running("escalation", options=TuiOptions(bell=False, toasts=False)) as run:
        seen: list[str] = []
        run.app.notify = lambda message, **_: seen.append(message)  # type: ignore[method-assign,assignment]
        await run.session.emit("log", {"level": "warning", "message": "the checker was slow"})
        await run.pilot.pause()
        assert seen == [] and run.app.model.logs[-1].message == "the checker was slow"


async def test_no_color_draws_the_screen_without_colours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    session = FakeSession()
    clock = Clock()
    app = CaluApp(
        fake_connector(session),
        TuiOptions(bell=False),
        clock=clock,
        live=False,
        admin=FakeAdmin(session),
    )
    monkeypatch.delenv("NO_COLOR")
    async with app.run_test(size=(120, 36), notifications=False) as pilot:
        await pilot.pause()
        await scenarios.play(session, scenarios.SCENARIOS["showcase"](), clock.advance)
        await pilot.pause()
        picture = app.export_screenshot()
        colours = set(re.findall(r"fill: ?(#[0-9a-fA-F]{6})", picture))
        assert colours, "the export names its colours"
        spread = {
            colour: max(channels) - min(channels)
            for colour in colours
            if (channels := [int(colour[i : i + 2], 16) for i in (1, 3, 5)])
        }
        assert max(spread.values()) <= 8, f"every colour is a grey: {spread}"
        assert any("ESCALATE" in line for line in app.transcript.visible_text())
