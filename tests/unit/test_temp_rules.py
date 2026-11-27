"""Temporary rules without a database: durations, the slash command's arguments, the prompt part.

The design is `agent/records/notes/temporary-rules-design.md`; the database side is in
`tests/db/test_temp_rules.py`.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calu.checkers import Step
from calu.checkers.policy import Route
from calu.checkers.rules import Rules
from calu.cli import build_parser
from calu.config import BuiltInRulesConfig
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


# ------------------------------------------------------------------ the built-in rules' switches


def bash(command: str) -> Step:
    """Build a Bash step in /w."""
    return Step(tool_name="Bash", tool_input={"command": command}, cwd="/w")


def read(path: str) -> Step:
    """Build a Read step."""
    return Step(tool_name="Read", tool_input={"file_path": path}, cwd="/w")


def test_each_built_in_rule_asks_until_its_setting_is_switched_off() -> None:
    """`built_in_rules.*`: off, the step goes to the lists and the checkers like any other."""
    steps = {
        "calu_commands": [bash("calu resolve 4 allow"), bash("uv run calu doctor")],
        "calu_container": [bash("docker exec calu-db-postgres-1 psql")],
        "calu_files": [read("/home/u/.config/calu/credentials.json")],
        "worktree_force_remove": [bash("git worktree remove --force wt")],
    }
    for switch, checked in steps.items():
        rules = Rules(Path("/w"))
        for step in checked:
            assert rules.evaluate(step).ask, (switch, step)
        rules.built_in = BuiltInRulesConfig(**{switch: False})
        for step in checked:
            assert not rules.evaluate(step).ask, (switch, step)
        # The other built-in rules still apply.
        for other, more in steps.items():
            if other != switch:
                assert all(rules.evaluate(step).ask for step in more), (switch, other)
    # A deny list entry still applies to a step whose built-in rule is off.
    rules = Rules.from_texts(Path("/w"), {"command-deny": "^calu resolve( |$)\n"})
    rules.built_in = BuiltInRulesConfig(calu_commands=False)
    assert rules.evaluate(bash("calu resolve 4 allow")).ask
    assert not rules.evaluate(bash("calu doctor")).ask


def test_a_rule_s_line_names_its_scope_and_its_end() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    mine = replace(rule("ask  first", now + timedelta(hours=2)), scope="session", conversation="c1")
    until = (now + timedelta(hours=2)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    assert mine.line("c1", now) == f"#1  this session  until {until}  ask first"
    assert mine.line(None, now).startswith("#1  session c1  until ")
    assert "  expired " in mine.line("c1", now + timedelta(hours=3))
    assert rule("x", now + timedelta(hours=2)).line() == f"#1  every project  until {until}  x"
    assert "removed, was until" in rule("x", now, removed=True).line(now=now)
    assert mine.as_json()["expires_at"] == (now + timedelta(hours=2)).isoformat()


def test_the_rule_command_parses_its_scopes_and_duration() -> None:
    parser = build_parser()
    args = parser.parse_args(["rule", "add", "--for", "3d", "--global", "ask", "before", "pushing"])
    assert (args.duration, args.everywhere, args.session, args.words) == (
        "3d",
        True,
        None,
        ["ask", "before", "pushing"],
    )
    assert parser.parse_args(["rule", "add", "--session", "--", "x"]).session == ""
    assert parser.parse_args(["rule", "add", "--session", "ab12", "x"]).session == "ab12"
    assert parser.parse_args(["rule", "list", "--all"]).all
    assert parser.parse_args(["rule", "remove", "12"]).words == ["12"]


def stored(
    text: str,
    *,
    scope: str = "global",
    conversation: str | None = None,
    expires_in: timedelta = timedelta(hours=1),
) -> TemporaryRule:
    """Build a rule as `ConfigStore.load` would hand it to a session."""
    now = datetime.now(UTC)
    return TemporaryRule(
        id=len(text),
        text=text,
        scope=scope,  # type: ignore[arg-type]
        project=None,
        conversation=conversation,
        created_at=now - timedelta(minutes=5),
        expires_at=now + expires_in,
        created_by="cli:test",
        origin="manual",
    )


def test_the_session_s_checkers_read_the_rules_of_its_scope(tmp_path: Path) -> None:
    """Command, edit and changeset checkers get the rules that apply; others do not."""
    from tests.unit.helpers import default_settings, make_session

    settings = default_settings(tmp_path)
    settings = replace(
        settings,
        temporary_rules=(
            stored("Claude may run the database tests."),
            stored("Only for conversation c1.", scope="session", conversation="c1"),
        ),
        config=settings.config.model_copy(
            update={"built_in_rules": BuiltInRulesConfig(calu_commands=False)}
        ),
    )
    session, _rec, _store = make_session(tmp_path, [], settings=settings)

    def prompt(route: Route) -> str:
        return session.checkers[route][0].instructions

    for route in (Route.COMMAND, Route.EDIT, Route.CHANGESET):
        assert "Claude may run the database tests." in prompt(route)
        assert "Only for conversation c1." not in prompt(route)
    assert "Claude may run" not in session.result_checker.instructions
    # The settings' switches reach the session's rules.
    assert session.rules.built_in.calu_commands is False

    session.session_id = "c1"  # the conversation becomes known after the start
    session._follow_temporary_rules()
    assert "Only for conversation c1." in prompt(Route.COMMAND)
