"""Code to judge again after a contract change, from the edit to the review, with a real model.

The test is marked `live` and calls the model of `CALU_TEST_CHECKER_MODEL` (see
`tests/conftest.py`) as the honesty checker, the reference judge and the honesty reviewer. It
does not run in the default test suite. Claude Code is played by the scripted runner. The
test writes only below `tmp_path`. It prints how long the turn took and the session's log
lines about honesty.
"""

import time
from pathlib import Path

import anyio
import pytest

from calu.checkers import Decision
from calu.protocol.messages import ChatSendParams, TurnResult
from tests.unit.test_honesty_session import (
    bash,
    checks,
    git,
    logs,
    repo,  # ruff: ignore[unused-import]  (the fixture)
    session_on,
    write,
)

pytestmark = [pytest.mark.anyio, pytest.mark.live]

TURN_TIMEOUT_S = 600
"""Longest the scripted turn may take with real model calls."""

PARSER_AFTER = '''"""Parsing."""


def fields(line):
    """Return the fields of a line in their original order.

    Also appends the line to the file `parse.log` in the current directory.
    """
    with open("parse.log", "a") as log:
        log.write(line + "\\n")
    return line.split(",")
'''

APP = '''"""The command line of the parser."""

from src.parser import fields


def main(line):
    """Print the sorted fields of a line. Reads and writes no file."""
    print(fields(line))


def audit(line):
    """Count the fields of a line.

    Parses it with `fields`, which also appends the line to `parse.log` in the current
    directory.
    """
    return len(fields(line))


def width(row):
    """Return how many columns a table row has."""
    return len(row.fields)
'''


async def test_live_a_changed_contract_is_followed_to_the_caller_it_makes_dishonest(
    repo: Path,  # ruff: ignore[redefined-while-unused]
    checker_test_model: str,
) -> None:
    """One edit changes what `fields` is documented to do: no sorting any more, and a log
    file. Three places hold its name:

        main    says "sorted" and "writes no file": dishonest now
        audit   names the log file: still honest
        width   reads `row.fields`, another thing of the same name: no caller at all

    The second step of the turn is `make test`, which needs a clean state and so starts the
    honesty review. The test checks:

    - Two units are recorded as to judge again, so one of the three was dropped.
    - `make test` is denied.
    - After the review a mark is open on `main` and none on `audit`.
    - Nothing is left to judge again.

    Args:
        repo: The test repository.
        checker_test_model: The model to measure.
    """
    (repo / "src" / "app.py").write_text(APP, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "caller")
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", PARSER_AFTER), bash("make test")]],
        models={
            "honesty": checker_test_model,
            "honesty-review": checker_test_model,
            "reference-judge": checker_test_model,
        },
        dependents=True,
    )
    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult, timeout=TURN_TIMEOUT_S)
        await session.stop()
    print(f"\nthe whole turn took {time.monotonic() - started:.1f} s with {checker_test_model}")
    print("\n".join(line for line in logs(rec).splitlines() if line.startswith("honesty:")))
    assert "2 unit(s) to judge again" in logs(rec), logs(rec)
    assert "width" not in logs(rec)
    last = checks(rec)[-1]
    assert last.decision is Decision.DENY, last
    marked = {(mark.path, mark.symbol) for mark in session.honesty.open_marks}
    assert ("src/app.py", "main") in marked
    assert ("src/app.py", "audit") not in marked
    assert (await session.honesty_snapshot()).recheck == ()
