"""Temporary rules without a database: durations, the slash command's arguments, the prompt part.

The design is `agent/records/notes/temporary-rules-design.md`; the database side is in
`tests/db/test_temp_rules.py`.
"""

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calu.checkers import Step
from calu.checkers.policy import Route
from calu.checkers.rules import (
    ALWAYS_ASKING_SUBCOMMANDS,
    LIFTABLE_SUBCOMMANDS,
    Rules,
    calu_subcommand,
)
from calu.cli import build_parser
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


# ------------------------------------------------------------------ lifting built-in protections


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("calu serve --web", "serve"),
        ("/usr/local/bin/calu run --batch", "run"),
        ("uv run calu doctor", "doctor"),
        ("uvx calu@1.2 attach", "attach"),
        ("python -m calu resolve 4 allow", "resolve"),
        ("python3 -m calu.cli config set x 1", "config"),
        ("calu --opt run resolve", None),  # an option's value could pose as the subcommand
        ("calu --help", None),
        ("calu", None),
        ("calu 'unterminated", None),
        ("ls -la", None),
    ],
)
def test_the_subcommand_is_the_word_right_after_calu_or_unreadable(
    command: str, expected: str | None
) -> None:
    assert calu_subcommand(command) == expected


def bash(command: str) -> Step:
    """Build a Bash step in /w."""
    return Step(tool_name="Bash", tool_input={"command": command}, cwd="/w")


def read(path: str) -> Step:
    """Build a Read step."""
    return Step(tool_name="Read", tool_input={"file_path": path}, cwd="/w")


def test_a_lifting_rule_leaves_calu_commands_and_directories_to_the_checkers() -> None:
    """Option B of the design: the built-in protections step aside, within fixed limits."""
    rules = Rules(Path("/w"))
    asking = [
        "calu serve",
        "uv run calu doctor",
        "calu resolve 4 allow",
        "calu --opt run resolve",
        "calu config set honesty.mode off",
    ]
    for command in asking:
        assert rules.evaluate(bash(command)).ask, command
    assert rules.evaluate(read("/home/u/.config/calu/tui.toml")).ask

    rules.lifted = True
    assert not rules.evaluate(bash("calu serve")).ask
    assert not rules.evaluate(bash("uv run calu doctor")).ask
    for command in asking[2:]:
        reason = rules.evaluate(bash(command)).reason
        assert "runs calu itself" in reason and "temporary rule lifts" in reason, command
    assert not rules.evaluate(read("/home/u/.config/calu/tui.toml")).ask
    for name in ("credentials.json", ".env"):
        assert rules.evaluate(read(f"/home/u/.config/calu/{name}")).ask, name
    # The other built-in pattern stays.
    assert rules.evaluate(bash("git worktree remove --force wt")).ask


def test_every_calu_subcommand_is_either_liftable_or_always_asking() -> None:
    """A new subcommand needs a decision before a lifting rule can let it through."""
    parser = build_parser()
    (subparsers,) = (a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    commands = set(subparsers.choices)
    assert not ALWAYS_ASKING_SUBCOMMANDS & LIFTABLE_SUBCOMMANDS
    assert commands - ALWAYS_ASKING_SUBCOMMANDS - LIFTABLE_SUBCOMMANDS == set()
    assert (ALWAYS_ASKING_SUBCOMMANDS | LIFTABLE_SUBCOMMANDS) - commands == set()
    assert "rule" in ALWAYS_ASKING_SUBCOMMANDS  # a lifting rule must not let Claude add rules


def test_a_rule_s_line_names_its_scope_its_end_and_what_it_lifts() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    mine = replace(rule("ask  first", now + timedelta(hours=2)), scope="session", conversation="c1")
    until = (now + timedelta(hours=2)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    assert mine.line("c1", now) == f"#1  this session  until {until}  ask first"
    assert mine.line(None, now).startswith("#1  session c1  until ")
    assert "  expired " in mine.line("c1", now + timedelta(hours=3))
    lifting = replace(rule("x", now + timedelta(hours=2)), lifts_builtin=True)
    assert f"every project  until {until}  lifts built-in protections  x" in lifting.line()
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
    lifts: bool = False,
) -> TemporaryRule:
    """Build a rule as `ConfigStore.load` would hand it to a session."""
    now = datetime.now(UTC)
    return TemporaryRule(
        id=len(text),
        text=text,
        scope=scope,  # type: ignore[arg-type]
        project=None,
        conversation=conversation,
        lifts_builtin=lifts,
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
            stored("Lifts the protections.", conversation="c1", scope="session", lifts=True),
        ),
    )
    session, _rec, _store = make_session(tmp_path, [], settings=settings)

    def prompt(route: Route) -> str:
        return session.checkers[route][0].instructions

    for route in (Route.COMMAND, Route.EDIT, Route.CHANGESET):
        assert "Claude may run the database tests." in prompt(route)
        assert "Only for conversation c1." not in prompt(route)
    assert "Claude may run" not in session.result_checker.instructions
    assert not session.rules.lifted

    session.session_id = "c1"  # the conversation becomes known after the start
    session._follow_temporary_rules()
    assert "Only for conversation c1." in prompt(Route.COMMAND)
    assert session.rules.lifted
