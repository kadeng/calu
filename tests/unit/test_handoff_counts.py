"""The size claims in `HANDOFF.md` have to be roughly true (D122).

`HANDOFF.md` is the first document a new session reads, and it states how big the project is.
Those numbers had drifted by nearly half ("~11.5k of tests" against 16.6k actual) before anyone
noticed, which is the kind of quiet untruth this project exists to catch -- found in its own
status document.

This test is deliberately loose: it fails when a claim is off by more than a quarter, not when
the project grows by a file. A tight check would fail on every second commit and be removed.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOLERANCE = 0.25
"""How far a stated size may be from the measured one before this test fails."""


def lines_in(directory: str, suffixes: tuple[str, ...]) -> int:
    """Count the lines of the matching files under a directory.

    Args:
        directory: Path relative to the repository root.
        suffixes: File extensions to count, e.g. `(".py",)`.

    Returns:
        The total number of lines.
    """
    total = 0
    for path in (ROOT / directory).rglob("*"):
        if path.suffix in suffixes and "__pycache__" not in path.parts:
            total += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return total


def stated(pattern: str) -> float:
    """Read one number out of `HANDOFF.md`.

    Args:
        pattern: A regular expression with one group holding the number, which may be written
            as "28.5k" or "22".

    Returns:
        The number.

    Raises:
        AssertionError: If the document does not hold that claim any more, which means this
            test is checking something that is no longer written.
    """
    text = (ROOT / "HANDOFF.md").read_text(encoding="utf-8")
    found = re.search(pattern, text)
    assert found, f"HANDOFF.md no longer states {pattern!r}; update or drop this check"
    raw = found.group(1)
    return float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw)


@pytest.mark.parametrize(
    ("claim", "measured"),
    [
        (r"~([\d.]+k) lines of Python in `src`", lambda: lines_in("src", (".py",))),
        (r"~([\d.]+k) of tests", lambda: lines_in("tests", (".py",))),
        (r"(\d+) migrations", lambda: len(list((ROOT / "src/calu/db/migrations").glob("*.sql")))),
    ],
)
def test_a_size_claim_in_the_handoff_is_roughly_true(claim: str, measured) -> None:
    """Compare one claim with what the tree holds.

    Args:
        claim: The pattern that finds the claim.
        measured: Returns the measured value.
    """
    said, real = stated(claim), measured()
    assert real > 0, "the measurement itself found nothing"
    assert abs(said - real) <= TOLERANCE * real, (
        f"HANDOFF.md says {said:g} where the tree holds {real:g} ({claim!r}); update it"
    )


def test_the_handoff_does_not_claim_fewer_tests_than_there_are_test_functions() -> None:
    """The stated number is the collected count, so it can only be higher, never lower."""
    functions = sum(
        len(re.findall(r"(?m)^(?:async )?def test_", path.read_text(encoding="utf-8")))
        for path in (ROOT / "tests").rglob("test_*.py")
    )
    assert functions > 100, f"only {functions} test functions found; the check would prove little"
    assert stated(r"\*\*(\d+) tests pass\*\*") >= functions


def test_the_structural_counts_in_the_handoff_are_exact() -> None:
    """Commands, protocol requests and events are countable, so these are exact, not loose.

    They are also the numbers a reader uses to judge whether the document is current at all: a
    handoff that names 16 protocol requests where there are 19 has been left behind, whatever
    else it says.
    """
    import argparse

    from calu.cli import build_parser
    from calu.protocol import messages
    from calu.protocol.messages import Event

    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert stated(r"(\d+) CLI commands") == len(subparsers.choices)

    from tests.unit.test_web_methods import dispatched_methods

    assert stated(r"(\d+) protocol requests") == len(dispatched_methods())

    events = [
        value
        for value in vars(messages).values()
        if isinstance(value, type) and issubclass(value, Event) and value is not Event
    ]
    assert stated(r"(\d+) events") == len(events)
