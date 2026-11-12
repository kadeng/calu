"""What the full-screen client draws, as plain text: `calu.tui.render`."""

import itertools

import pytest

pytest.importorskip("textual")

from calu.tui import render, scenarios
from calu.tui.capabilities import Capabilities, capabilities
from calu.tui.fake import FakeSession
from calu.tui.model import ALL, MAIN, SessionModel, StepBlock, ThinkingBlock, UserBlock
from calu.tui.theme import DARK_PALETTE
from tests.unit.test_tui_model import feed, steps


def view(width: int = 80, **changes: object) -> render.View:
    """Build rendering parameters with the dark palette and a fixed clock."""
    return render.View(palette=DARK_PALETTE, width=width, now=NOW, **changes)  # type: ignore[arg-type]


def plain(lines: list) -> list[str]:
    """Return the text of rendered lines without trailing spaces."""
    return [line.plain.rstrip() for line in lines]


NOW = 1000.0
"""The clock of every rendering in this file."""


def showcase() -> SessionModel:
    """Return a model that has seen the showcase scenario live; `e1` waits for 42 seconds."""
    model = SessionModel()
    feed(model, scenarios.showcase(), start=900.0)
    for escalation_id, waited in (("e1", 42.0), ("e2", 5.0)):
        decision = model.decisions[escalation_id]
        decision.opened_at = NOW - waited
        step = model.step(decision.tool_use_id)
        assert step is not None
        step.opened_at = NOW - waited
    return model


def test_a_step_is_one_line_with_fixed_columns_and_a_line_for_what_claude_was_told() -> None:
    model = showcase()
    read, edit, tests, *_ = steps(model)
    assert plain(render.step_lines(read, model, view())) == [
        "  PASS      Read   read gate.py" + " " * 44 + "0.5s"
    ]
    lines = plain(render.step_lines(edit, model, view()))
    assert lines[0].startswith("  PASS      Edit   renamed check_step to judge_step in gate.py")
    assert lines[0].endswith("+2 -2")
    assert lines[1] == "    note    the docstring of judge_step still names the old function"
    assert plain(render.step_lines(tests, model, view()))[0].endswith("8.7s")


def test_a_denied_step_shows_its_reason_and_a_waiting_step_how_long_it_waits() -> None:
    model = showcase()
    denied = next(step for step in steps(model) if step.verdict == "DENY")
    assert plain(render.step_lines(denied, model, view())) == [
        "  DENY      Bash   read a credentials file",
        "    reason  the task does not need this file",
    ]
    waiting = next(step for step in steps(model) if step.waiting == "open")
    first, second = plain(render.step_lines(waiting, model, view(cursor=waiting.id)))
    assert first.startswith("> ESCALATE  Bash   force push of main to origin")
    assert first.endswith("waiting 0:42")
    assert second.startswith("    reason  The command `git push --force origin main` matches")
    assert len(second) <= 80 and second.endswith("…")


def test_a_long_summary_is_cut_and_never_wrapped() -> None:
    model = SessionModel()
    feed(model, scenarios.step("t1", "Bash", {"command": "x"}, "word " * 60))
    (line,) = render.step_lines(steps(model)[0], model, view(60))
    assert line.cell_len <= 60 and "…" in line.plain


def test_an_expanded_step_shows_input_checks_result_and_the_diff_with_its_indentation() -> None:
    model = showcase()
    edit = steps(model)[1]
    lines = plain(render.step_lines(edit, model, view(100, expanded=frozenset({edit.id}))))
    body = "\n".join(lines)
    assert "    │ file_path: src/gate.py" in lines
    assert "check edit PASS  A rename of one function and its caller, as the user asked." in body
    assert "note to Claude: the docstring of judge_step still names the old function" in body
    assert "    │ -def check_step(step: Step, rules: Rules) -> Verdict:" in lines
    assert "    │ +def judge_step(step: Step, rules: Rules) -> Verdict:" in lines
    assert "    │ +    return judge_step(step, rules) is Verdict.PASS" in lines, "indentation kept"


def test_the_agent_call_names_its_subagent_and_what_it_does() -> None:
    model = showcase()
    call = model.step("a1")
    assert call is not None
    lines = plain(render.step_lines(call, model, view(100)))
    assert lines[1] == "    agent   explore: tool call Grep, 2 steps, last Grep, background"


def test_the_users_message_is_a_block_of_its_own_with_bar_label_and_background() -> None:
    lines = render.user_lines(UserBlock(id=0, text="Rename the helper."), view(40))
    assert plain(lines) == ["▌ you  Rename the helper."]
    assert lines[0].cell_len == 40, "the background fills the whole width"
    backgrounds = {str(span.style) for span in lines[0].spans if "on " in str(span.style)}
    assert backgrounds == {f"on {DARK_PALETTE.user_bg}"}
    relayed = render.user_lines(UserBlock(id=0, text="check docs", relayed_to="explore"), view(40))
    assert plain(relayed) == ["▌ you → explore  check docs"]
    automatic = render.user_lines(UserBlock(id=0, text="task done", source="system"), view(50))
    assert plain(automatic) == ["▌ automatic message  task done"]
    assert f"on {DARK_PALETTE.user_bg}" not in {str(span.style) for span in automatic[0].spans}


def test_a_long_user_message_wraps_inside_its_block() -> None:
    lines = render.user_lines(UserBlock(id=0, text="word " * 30), view(40))
    assert len(lines) > 2 and all(line.plain.startswith("▌ ") for line in lines)
    assert all(line.cell_len == 40 for line in lines)


def test_thinking_is_shown_dimmed_and_collapses_to_one_line() -> None:
    block = ThinkingBlock(id=3, text="The helper has two callers.\nI rename it.")
    shown = render.thinking_lines(block, view())
    assert plain(shown) == [
        "  ┊ thinking",
        "  ┊ The helper has two callers.",
        "  ┊ I rename it.",
    ]
    assert all("italic" in str(span.style) for span in shown[1].spans if span.end > 5)
    collapsed = render.thinking_lines(block, view(thinking_open=False))
    assert plain(collapsed) == ["  ┊ thinking · 2 lines · alt+t shows"]
    reopened = render.thinking_lines(block, view(thinking_open=False, toggled=frozenset({3})))
    assert len(reopened) == 3
    block.streaming = True
    assert plain(render.thinking_lines(block, view()))[0] == "  ┊ thinking…"


def test_claudes_text_is_markdown_without_a_background() -> None:
    model = showcase()
    text = next(b for b in model.blocks if isinstance(b, render.TextBlock))
    lines = render.text_lines(text, model, view())
    assert plain(lines) == ["  I will rename check_step to judge_step and update the callers."]
    assert not any(" on " in f" {span.style}" for span in lines[0].spans)


def test_the_view_of_all_agents_names_the_agent_of_each_step() -> None:
    model = showcase()
    step = steps(model, "a1")[0]
    (line,) = plain(render.step_lines(step, model, view(100, tab=ALL)))
    assert line.startswith("  explore   PASS      Grep   searched the project for check_step")


def test_the_end_of_a_turn_is_a_rule_with_its_cost_and_duration() -> None:
    model = SessionModel()
    feed(model, scenarios.simple_turn())
    (line,) = plain(render.block_lines(model.blocks[-1], model, view(60)))
    assert line.startswith("  ── turn 1 · $0.84 so far · 15s ──") and len(line) <= 60


def test_blank_lines_separate_everything_but_consecutive_steps() -> None:
    model = showcase()
    blocks = model.blocks_of(MAIN)
    for first, second in itertools.pairwise(blocks):
        both_steps = isinstance(first, StepBlock) and isinstance(second, StepBlock)
        assert render.gap_before(first, second) == (not both_steps)
    assert not render.gap_before(None, blocks[0])


def test_the_status_bar_keeps_state_and_open_decisions_when_it_gets_narrow() -> None:
    model = showcase()
    model.load_snapshot(FakeSession().snapshot() | {"state": "escalated"}, None)
    model.set_connection("attached")
    model.usage.cost = 0.84
    wide = render.status_line(model, view(120)).plain
    for part in ("calu", "ESCALATED", "opus / high", "Checkers decide", "$0.84", "attached"):
        assert part in wide
    narrow = render.status_line(model, view(44)).plain
    assert "ESCALATED" in narrow and "attached" not in narrow and len(narrow) <= 44
    model.decisions = dict(showcase().decisions)
    assert "2 need you" in render.status_line(model, view(50)).plain


def test_usage_limits_and_context_fill_appear_in_the_status_bar_and_the_panel() -> None:
    session = FakeSession()
    model = showcase()
    model.apply(
        "usage.update",
        {"rate_limits": [{"type": "five_hour", "status": "allowed_warning", "utilization": 0.82}]},
        1.0,
    )
    model.set_usage({"context": session.context, "rate_limits": session.rate_limits})
    model.apply(
        "usage.update",
        {"rate_limits": [{"type": "five_hour", "status": "allowed_warning", "utilization": 0.82}]},
        1.0,
    )
    bar = render.status_line(model, view(140)).plain
    assert "ctx 21%" in bar and "5h 82%" in bar
    lines, _ = render.panel_lines(model, view(32), capabilities(session.snapshot()))
    text = plain(lines)
    assert " context  21% of 1000k tokens" in text
    assert " 5h       82% used" in text
    assert any(set(row.strip()) <= {"━", "╌"} and row.strip() for row in text), "a meter is drawn"


def test_a_session_without_usage_figures_says_so_instead_of_showing_a_meter() -> None:
    lines, _ = render.panel_lines(showcase(), view(60), Capabilities())
    text = plain(lines)
    assert " context  not available from this session" in text
    assert " quota    not available from this session" in text
    assert not any("━" in row for row in text)


def test_the_panel_lists_what_needs_the_user_the_agents_and_the_queue() -> None:
    model = showcase()
    lines, targets = render.panel_lines(model, view(34), Capabilities(), selected="e1")
    text = plain(lines)
    assert text[0] == "NEEDS YOU (2)"
    assert text[1].startswith(">1 main Bash") and text[1].endswith("0:42")
    assert text[3].startswith(" 2 tests Question") and text[3].endswith("0:05")
    assert "AGENTS (3)  alt+←/→" in text
    assert ">main         waits for you" in text
    assert " explore      tool call Grep bg" in text
    assert "QUEUE (2)" in text
    assert " next   → main also bump the…" in text or any("also bump" in row for row in text)
    assert "HONESTY (1)  enforce" in text
    assert targets[1] == ("decision", "e1") and ("agent", "a1") in targets.values()


def test_the_decision_card_shows_the_step_the_reason_and_the_keys() -> None:
    model = showcase()
    decision = model.decisions["e1"]
    lines = plain(render.card_lines(decision, model, view(76), (1, 2)))
    assert lines[0].startswith("DECISION 1 of 2  ·  main  ·  Bash") and lines[0].endswith(
        "waiting 0:42"
    )
    assert lines[1] == "  git push --force origin main"
    assert lines[2].startswith("  Stopped: The command")
    assert lines[-1] == "  a,a allow · d,d deny · w,w detail · n,n note · ] next"
    armed = plain(render.card_lines(decision, model, view(76), (1, 2), armed="allow"))
    wide = plain(render.card_lines(decision, model, view(100), (1, 2), armed="allow"))
    assert wide[-1].endswith("press a again to allow · any other key types")
    assert armed[-1] == ("  A deny rule stopped this step · press a again to allow")
    decision.note = "only this once"
    decision.error = "no open escalation e1"
    noted = "\n".join(plain(render.card_lines(decision, model, view(76), (1, 2), max_lines=12)))
    assert "note for Claude: only this once" in noted
    assert "the session refused: no open escalation e1" in noted


def test_the_question_card_shows_tabs_options_and_what_is_chosen() -> None:
    model = SessionModel()
    feed(model, scenarios.question_multi())
    decision = model.decisions["e1"]
    decision.picks[0] = {0}
    decision.current = 1
    decision.picks[1] = {0, 2}
    lines = plain(render.card_lines(decision, model, view(76), (1, 1), max_lines=12))
    assert lines[0].startswith("QUESTION 1 of 1  ·  main")
    assert lines[1] == "   Runner ✓   Checks ✓   Fixtures    2 of 3 answered"
    assert lines[2] == "  Which checks should run before each commit?  (several may be chosen)"
    assert lines[3:6] == [
        "   1 [x] unit tests  uv run pytest",
        "   2 [ ] type check  uv run pyright",
        "   3 [x] lint  uv run ruff check .",
    ]
    decision.picks[2] = {1}
    done = plain(render.card_lines(decision, model, view(76), (1, 1), max_lines=12))
    assert done[-1].startswith("  enter sends: Runner: pytest · Checks: unit tests, li…")


def test_the_agent_tabs_carry_badges_for_decisions_and_unread_output() -> None:
    model = showcase()
    line, spans = render.tabs_line(model, view(100), "alt+←/→ switches")
    assert line.plain.startswith("  main 1!   explore bg *   tests 1! *   all")
    assert line.plain.endswith("alt+←/→ switches")
    assert [key for _, _, key in spans] == [MAIN, "a1", "a2", ALL]
    start, end, _ = spans[1]
    assert line.plain[start:end] == " explore bg * "


def test_a_subagents_tab_is_headed_by_who_it_is_and_what_it_was_asked() -> None:
    model = showcase()
    head, task = plain(render.agent_head(model.agents["a1"], model, view(90)))
    assert head == " explore  ·  Explore  ·  background  ·  tool call Grep"
    assert task == " task  Find every caller of check_step outside src/."
    # Seen in a real terminal: a longer head wrapped onto a second line and pushed the task down.
    narrow, _ = plain(render.agent_head(model.agents["a1"], model, view(40)))
    assert len(narrow) <= 40 and narrow.startswith(" explore  ·  Explore") and narrow.endswith("…")


def test_side_questions_are_drawn_as_question_and_answer_and_say_what_they_are() -> None:
    model = SessionModel()
    text = " ".join(plain(render.side_lines(model, view(40))))
    assert "Not part of the conversation" in text and "/btw <question>" in text
    exchange, _ = model.ask_side("Which callers?")
    assert "  asking…" in plain(render.side_lines(model, view(40)))
    exchange.pending, exchange.answer = False, "Two, both in `gate.py`."
    lines = plain(render.side_lines(model, view(40)))
    assert "▌ you  Which callers?" in lines and "  Two, both in gate.py." in lines
    exchange.answer, exchange.error = "", "no Claude session is running"
    assert "  no answer: no Claude session is running" in plain(render.side_lines(model, view(60)))


def test_small_formats() -> None:
    assert render.clock_text(42) == "0:42" and render.clock_text(3725) == "1:02:05"
    assert render.duration_text(0.24) == "0.2s" and render.duration_text(72) == "1m12s"
    assert render.cost_text(0.836) == "$0.84" and render.cost_text(None) == ""
    assert render.tool_label("AskUserQuestion") == "Ask"
    assert render.tool_label("mcp__github__create_issue") == "creat…"
    assert render.cut("a  b\nc", 10) == "a b c" and render.cut("abcdef", 4) == "abc…"
    assert render.clip("    indented line", 12) == "    indente…"
    assert render.meter(0.5, 10) == "━━━━━╌╌╌╌╌"
    assert isinstance(steps(showcase())[0], StepBlock)
