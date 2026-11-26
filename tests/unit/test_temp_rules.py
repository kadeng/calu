"""Temporary rules without a database: durations, the slash command's arguments, the prompt part.

The design is `agent/records/notes/temporary-rules-design.md`; the database side is in
`tests/db/test_temp_rules.py`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from calu.temp_rules import (
    PROMPT_HEADING,
    RuleError,
    TemporaryRule,
    check_text,
    parse_duration,
    prompt_part,
    split_command,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, timedelta(days=1)),
        ("", timedelta(days=1)),
        ("1m", timedelta(minutes=1)),
        ("30m", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("12H", timedelta(hours=12)),
        ("3d", timedelta(days=3)),
        ("7d", timedelta(days=7)),
        ("168h", timedelta(days=7)),
    ],
)
def test_a_duration_is_minutes_hours_or_days(text: str | None, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("0m", "at least one minute"),
        ("8d", "at most 7 days"),
        ("169h", "at most 7 days"),
        ("30s", "no duration"),
        ("1.5h", "no duration"),
        ("tomorrow", "no duration"),
        ("-1d", "no duration"),
    ],
)
def test_a_duration_outside_one_minute_to_seven_days_is_refused(text: str, why: str) -> None:
    """The owner: never longer than 7 days, nothing smaller than a minute."""
    with pytest.raises(RuleError, match=why):
        parse_duration(text)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("3d Calu may run its tests", ("3d", None, "Calu may run its tests")),
        ("Calu may run its tests", (None, None, "Calu may run its tests")),
        ("--global 2h ask before pushing", ("2h", "global", "ask before pushing")),
        ("1h --project  keep  the spaces", ("1h", "project", "keep  the spaces")),
        ("1h\t--global\task first", ("1h", "global", "ask first")),
        ("2h first line\nsecond line", ("2h", None, "first line\nsecond line")),
        ("--session", (None, "session", "")),
        ("3d 4d text", ("3d", None, "4d text")),
        ("   ", (None, None, "")),
    ],
)
def test_the_rule_command_takes_duration_and_scope_before_the_text(
    arguments: str, expected: tuple[str | None, str | None, str]
) -> None:
    """`/rule [duration] [--session | --project | --global] <text>`, in either order; a second
    duration is part of the text."""
    assert split_command(arguments) == expected


def test_a_rule_needs_a_text_of_at_most_1000_characters() -> None:
    assert check_text("  ask first  ") == "ask first"
    with pytest.raises(RuleError, match="needs a text"):
        check_text(" \n\t ")
    with pytest.raises(RuleError, match="at most 1000"):
        check_text("x" * 1001)


def rule(text: str, expires_at: datetime, removed: bool = False) -> TemporaryRule:
    """Build a stored rule for the prompt tests."""
    return TemporaryRule(
        id=1,
        text=text,
        scope="global",
        project=None,
        conversation=None,
        lifts_builtin=False,
        created_at=expires_at - timedelta(hours=1),
        expires_at=expires_at,
        created_by="cli:test",
        origin="manual",
        removed_at=expires_at if removed else None,
        removed_by="cli:test" if removed else None,
    )


def test_the_prompt_part_lists_only_the_rules_that_count_now() -> None:
    """A rule that expired after the session read it no longer reaches a checker."""
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    rules = [
        rule("Claude may run the database tests.", now + timedelta(hours=2)),
        rule("expired since it was read", now - timedelta(seconds=1)),
        rule("removed", now + timedelta(hours=2), removed=True),
        rule("line\nbreak  inside", now + timedelta(days=1)),
    ]
    part = prompt_part(rules, now)
    assert part.startswith(PROMPT_HEADING + "\n")
    assert "Claude may run the database tests." in part
    assert "expired" not in part and "removed" not in part
    assert "- (until " in part and ") line break inside\n" in part
    assert prompt_part(rules[1:3], now) == ""
