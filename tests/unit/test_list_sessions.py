"""`calu list-sessions` and `calu run --fork`: the parts that need no database (D203)."""

import argparse

import pytest

from calu import herd
from calu.cli import CliError, build_parser, cmd_list_sessions, cmd_run


def test_an_age_is_said_in_the_largest_unit_that_fits() -> None:
    assert herd.age_text(-5) == "0 s"
    assert herd.age_text(41.9) == "41 s"
    assert herd.age_text(119) == "119 s"
    assert herd.age_text(120) == "2 min"
    assert herd.age_text(7199) == "119 min"
    assert herd.age_text(7200) == "2 h"
    assert herd.age_text(172_799) == "47 h"
    assert herd.age_text(172_800) == "2 d"
    assert herd.age_text(40 * 86_400) == "40 d"


def test_a_conversation_is_one_line_with_age_id_and_the_last_input() -> None:
    row = herd.ConversationRow("0b9c-id", 300.0, "fix the\n  parser\tplease", 4, False)
    assert row.line() == "  5 min  0b9c-id  fix the parser please"
    running = herd.ConversationRow("0b9c-id", 5.0, "", 1, True)
    assert running.line() == "    5 s  0b9c-id  running  (no user input stored)"
    # What somebody pasted must not steer the terminal the list is printed to.
    hostile = herd.ConversationRow("0b9c-id", 5.0, "red \x1b[31m alert \x9b2J\x07 done", 1, False)
    assert "\x1b" not in hostile.line() and "\x9b" not in hostile.line()
    assert hostile.line().endswith("red \ufffd[31m alert \ufffd2J\ufffd done")
    long = herd.ConversationRow("0b9c-id", 5.0, "word " * 80, 1, False)
    assert len(long.line(60)) == 60 and long.line(60).endswith("…")
    assert long.line(60).startswith("    5 s  0b9c-id  word word")


def test_the_count_is_optional_and_five_by_default() -> None:
    parser = build_parser()
    assert parser.parse_args(["list-sessions"]).count == 5
    assert parser.parse_args(["list-sessions", "12"]).count == 12
    assert parser.parse_args(["list-sessions"]).handler is cmd_list_sessions
    with pytest.raises(SystemExit):
        parser.parse_args(["list-sessions", "many"])


def test_a_count_below_one_is_refused_before_any_password_is_asked() -> None:
    store: object = None  # never touched: the count is checked first
    with pytest.raises(CliError, match="1 or more"):
        cmd_list_sessions(argparse.Namespace(count=0), store)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--resume", "other-id"], "exclude each other"),
        (["--fork-before-turn", "3"], "--resume ID --fork-before-turn N"),
    ],
)
def test_fork_takes_neither_resume_nor_a_turn(extra: list[str], message: str) -> None:
    args = build_parser().parse_args(["run", ".", "--fork", "some-id", *extra])
    assert args.fork == "some-id"
    store: object = None  # never touched: the options are checked first
    with pytest.raises(CliError, match=message):
        cmd_run(args, store)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--resume", "my session title"], "--resume takes a Claude session id"),
        (["--fork", "fake-session"], "--fork takes a Claude session id"),
        # 0 was read as "no fork" and continued the original conversation instead.
        (["--resume", "2d01284d-91c7-487b-b082-2f6e8ddd99a6", "--fork-before-turn", "0"], "2 or"),
        (["--fork-before-turn", "1"], "2 or more"),
    ],
)
def test_ids_and_turn_numbers_are_checked_before_any_password_is_asked(
    options: list[str], message: str
) -> None:
    """calu reads a conversation back by its Claude session id and prints that id, so a
    session title is no id for it."""
    store: object = None
    with pytest.raises(CliError, match=message):
        cmd_run(build_parser().parse_args(["run", ".", *options]), store)  # type: ignore[arg-type]
