"""The parts of the full-screen client that need neither Textual nor a session.

Settings and the history of sent texts (`calu.tui.settings`), the rows of a diff
(`calu.tui.difftools`), path completion (`calu.tui.completion`), the Markdown export
(`calu.tui.export`) and the stored history in the model (`SessionModel.load_history`).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calu.tui import completion, scenarios, settings
from calu.tui.difftools import diff_rows, hunk_starts, selection_range, unified_text
from calu.tui.export import transcript_markdown
from calu.tui.model import (
    MAIN,
    JoinBlock,
    SessionModel,
    StepBlock,
    TextBlock,
    TurnEndBlock,
    UserBlock,
)

# ------------------------------------------------------------------ settings


def test_a_missing_settings_file_gives_the_defaults(tmp_path: Path) -> None:
    loaded = settings.load(tmp_path / "tui.toml")
    assert (loaded.theme, loaded.bell, loaded.thinking, loaded.panel_width) == ("", True, True, 36)
    assert loaded.keys == {} and loaded.problems == []


def test_settings_are_read_and_a_wrong_value_is_named_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "tui.toml"
    path.write_text(
        'theme = "calu-light"\nbell = false\npanel_width = 400\nthinking = "yes"\n'
        '[keys]\ndetach = "ctrl+q"\nhelp = 3\n',
        encoding="utf-8",
    )
    loaded = settings.load(path)
    assert loaded.theme == "calu-light" and loaded.bell is False
    assert loaded.panel_width == settings.PANEL_WIDTHS[1], "kept within the allowed widths"
    assert loaded.thinking is True and loaded.keys == {"detach": "ctrl+q"}
    assert loaded.problems == [
        "tui.toml: thinking must be a bool",
        "tui.toml: keys.help must be a text",
    ]


def test_a_file_that_is_no_toml_is_ignored_with_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "tui.toml"
    path.write_text("theme = = =", encoding="utf-8")
    loaded = settings.load(path)
    assert loaded.theme == "" and len(loaded.problems) == 1 and "was not read" in loaded.problems[0]


def test_remember_replaces_one_line_and_keeps_comments_and_the_keys(tmp_path: Path) -> None:
    path = tmp_path / "calu" / "tui.toml"
    assert settings.remember(path, "theme", "calu-light") == ""
    assert path.read_text(encoding="utf-8") == 'theme = "calu-light"\n'
    path.write_text('# mine\ntheme = "x"\n\n[keys]\ndetach = "ctrl+q"\n', encoding="utf-8")
    assert settings.remember(path, "theme", "nord") == ""
    assert settings.remember(path, "bell", False) == ""
    assert path.read_text(encoding="utf-8") == (
        '# mine\ntheme = "nord"\n\nbell = false\n[keys]\ndetach = "ctrl+q"\n'
    )
    loaded = settings.load(path)
    assert (loaded.theme, loaded.bell, loaded.keys) == ("nord", False, {"detach": "ctrl+q"})


def test_the_paths_follow_the_xdg_variables() -> None:
    env = {"XDG_CONFIG_HOME": "/c", "XDG_STATE_HOME": "/s"}
    assert settings.config_path(env) == Path("/c/calu/tui.toml")
    assert settings.history_path(env) == Path("/s/calu/tui-history.jsonl")
    assert settings.config_path({}) == Path.home() / ".config" / "calu" / "tui.toml"


def test_sent_texts_survive_in_the_history_file_and_old_ones_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "state" / "tui-history.jsonl"
    assert settings.load_history(path) == []
    settings.append_history(path, "first\nwith a second line")
    settings.append_history(path, "   ")
    settings.append_history(path, "second")
    assert settings.load_history(path) == ["first\nwith a second line", "second"]
    assert path.stat().st_mode & 0o777 == 0o600
    for number in range(2 * settings.HISTORY_LIMIT):
        settings.append_history(path, f"text {number}")
    kept = settings.load_history(path)
    assert (
        len(kept) == settings.HISTORY_LIMIT and kept[-1] == f"text {2 * settings.HISTORY_LIMIT - 1}"
    )
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 2 * settings.HISTORY_LIMIT


# ------------------------------------------------------------------ diff rows


def test_the_rows_of_a_diff_carry_the_line_numbers_of_both_sides() -> None:
    rows = diff_rows("a\nb\nc\nd\n", "a\nB\nc\nd\ne\n")
    assert [(row.kind, row.before_no, row.after_no, row.text) for row in rows] == [
        ("context", 1, 1, "a"),
        ("removed", 2, None, "b"),
        ("added", None, 2, "B"),
        ("context", 3, 3, "c"),
        ("context", 4, 4, "d"),
        ("added", None, 5, "e"),
    ]
    assert hunk_starts(rows) == [1]
    assert diff_rows("same\n", "same\n") == []


def test_the_split_layout_pairs_a_replaced_line_with_its_replacement() -> None:
    rows = diff_rows("a\nb\nc\n", "a\nB\nB2\nc\n", pair=True)
    assert [(row.kind, row.before, row.after) for row in rows] == [
        ("context", "a", "a"),
        ("changed", "b", "B"),
        ("added", "", "B2"),
        ("context", "c", "c"),
    ]


def test_far_apart_changes_are_separate_hunks_with_a_gap_between() -> None:
    before = "\n".join(f"line {n}" for n in range(1, 31))
    after = before.replace("line 2\n", "LINE 2\n").replace("line 28", "LINE 28")
    rows = diff_rows(before, after)
    gaps = [row.text for row in rows if row.kind == "gap"]
    assert gaps == ["19 unchanged lines"]
    starts = hunk_starts(rows)
    assert (
        len(starts) == 2
        and rows[starts[0]].before == "line 2"
        and rows[starts[1]].before == "line 28"
    )


def test_a_selection_becomes_the_side_and_the_lines_of_a_comment() -> None:
    rows = diff_rows("a\nb\nc\nd\n", "a\nB\nc\nd\ne\n")
    assert selection_range(rows, 2, 3) == ("after", 2, 3)
    assert selection_range(rows, 3, 2) == ("after", 2, 3), "the order of the ends does not matter"
    assert selection_range(rows, 1, 1) == ("before", 2, 2), "only a removed line: the old side"
    gap = diff_rows("\n".join(map(str, range(30))), "\n".join(map(str, range(29))) + "\nX")
    assert gap[0].kind == "gap" and selection_range(gap, 0, 0) is None


def test_the_copied_diff_is_in_the_unified_format() -> None:
    text = unified_text("src/x.py", "a\nb\n", "a\nc\n")
    assert text.splitlines()[:2] == ["--- a/src/x.py", "+++ b/src/x.py"]
    assert "-b" in text.splitlines() and "+c" in text.splitlines()


# ------------------------------------------------------------------ path completion


def test_the_at_word_is_found_only_at_the_cursor_and_after_a_space() -> None:
    assert completion.at_word("look at @src/ga", 15) == (8, "src/ga")
    assert completion.at_word("@", 1) == (0, "")
    assert completion.at_word("mail me@example", 15) is None
    assert completion.at_word("@src and more", 13) is None


def test_project_paths_are_offered_directories_first_and_never_outside(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "gate.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "gates").mkdir()
    (tmp_path / "setup.cfg").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("", encoding="utf-8")
    root = str(tmp_path)
    assert completion.complete_path(root, "s") == ["src/", "setup.cfg"]
    assert completion.complete_path(root, "src/ga") == ["src/gates/", "src/gate.py"]
    assert completion.complete_path(root, "") == ["src/", "setup.cfg"], "no hidden names"
    assert completion.complete_path(root, ".") == [".env"], "never the .git directory"
    assert completion.complete_path(root, "../") == []
    assert completion.complete_path("/no/such/directory", "s") == []
    assert completion.common_prefix(["src/gates/", "src/gate.py"]) == "src/gate"


# ------------------------------------------------------------------ stored history


def test_the_stored_history_becomes_blocks_above_a_line_that_says_so() -> None:
    model = SessionModel()
    change = model.load_history(scenarios.stored_history())
    kinds = [type(block).__name__ for block in model.blocks]
    assert kinds == [
        "UserBlock",
        "StepBlock",
        "TextBlock",
        "TurnEndBlock",
        "UserBlock",
        "StepBlock",
        "StepBlock",
        "TextBlock",
        "TurnEndBlock",
        "JoinBlock",
    ]
    assert change.rebuilt and change.blocks_added == list(range(10))
    steps = [block for block in model.blocks if isinstance(block, StepBlock)]
    assert [(step.tool, step.verdict, step.summary) for step in steps] == [
        ("Grep", "PASS", "searched for the definition of check_step"),
        ("Edit", "PASS", "added a docstring to check_step"),
        ("Bash", "DENY", "read a credentials file"),
    ]
    assert all(step.stored and step.duration is None for step in steps)
    assert isinstance(model.blocks[-1], JoinBlock) and "stored history" in model.blocks[-1].text
    assert model.turn == 2 and model.stored_blocks == 9


def test_what_the_session_replays_is_not_taken_from_the_stored_history_too() -> None:
    model = SessionModel()
    model.load_history(scenarios.stored_history(), replayed_ids=["h3"])
    blocks = model.blocks
    assert [type(block) for block in blocks] == [
        UserBlock,
        StepBlock,
        TextBlock,
        TurnEndBlock,
        UserBlock,
        StepBlock,
        JoinBlock,
    ], "turn 2 is the boundary: its input and the step that was not replayed, no answer"
    whole = SessionModel()
    covered = whole.load_history(
        scenarios.stored_history(), replayed_inputs=["Find out where the step gate decides."]
    )
    assert whole.blocks == [] and not covered, "the replay starts at the first turn"


def test_the_stored_history_is_only_loaded_into_an_empty_model() -> None:
    model = SessionModel()
    model.apply("user.input", {"text": "hello", "source": "user"}, 1.0)
    assert not model.load_history(scenarios.stored_history()) and len(model.blocks) == 1
    broken = SessionModel()
    assert broken.load_history({"steps": "not a list"}).blocks_added == [] and broken.logs


def test_an_escalation_opened_before_the_attach_counts_from_the_sessions_time() -> None:
    model = SessionModel()
    now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    model.clock = lambda: 500.0
    model.wall = lambda: now
    request = {
        "escalation_id": "e1",
        "tool_use_id": "t1",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force"},
        "reason": "a force push",
        "rendered": "git push --force",
        "opened_at": (now - timedelta(seconds=90)).isoformat(),
    }
    model.apply("escalation.request", request, None)
    assert model.decisions["e1"].opened_at == pytest.approx(410.0)
    older = SessionModel()
    older.clock = lambda: 500.0
    older.apply("escalation.request", {**request, "opened_at": None}, None)
    assert older.decisions["e1"].opened_at is None, "a session that does not report the time"


# ------------------------------------------------------------------ export


def test_the_export_holds_messages_texts_and_one_line_per_step() -> None:
    model = SessionModel()
    for item in scenarios.SCENARIOS["escalation"]():
        if item.method:
            model.apply(item.method, item.params, 1.0)
    text = transcript_markdown(model, MAIN)
    assert text.startswith("# calu session")
    assert "## You\n\nRename the helper in gate.py and run the unit tests." in text
    assert "- **PASS** `Edit`: renamed check_step to judge_step in gate.py" in text
    assert "  - note to Claude: the docstring of judge_step still names the old function" in text
    assert "- **DENY** `Bash`: read a credentials file" in text
    assert "  - reason: the task does not need this file" in text
    assert "- **ESCALATE** `Bash`: force push of main to origin" in text
    assert "  ```\n  git push --force origin main\n  ```" in text
    assert "thinking" not in text.lower() or "> " not in text
    with_thinking = transcript_markdown(model, MAIN, thinking=True)
    assert "> The helper is `check_step` in gate.py." in with_thinking
