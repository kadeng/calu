"""Random load on `calu.locking` (marker `fuzz`: runs only on request, never in CI).

    uv run pytest -m fuzz -s tests/fuzz                           # about a minute
    CALU_FUZZ_SECONDS=120 CALU_FUZZ_SEED=7 uv run pytest -m fuzz -s tests/fuzz

Environment (all optional): CALU_FUZZ_SECONDS (each random-load run, default 10),
CALU_FUZZ_SEED (default: a new random seed; every report prints it), CALU_FUZZ_WORKERS,
CALU_FUZZ_WORKTREES, CALU_FUZZ_TIMEOUT_S (the default wait of commands and edits; the maximum
is twice that; default 0.5), CALU_FUZZ_REPETITIONS (runs per scheme in the comparison,
default 3), CALU_FUZZ_TIMELINE (a CSV path for the timeline of the first test).

The tests run threads of this process only and print their reports (counts and timings) to
stdout; `tests/fuzz/locking_harness.py` explains the numbers. They write the timeline CSV when
CALU_FUZZ_TIMELINE names one, and the file backend's state below pytest's `tmp_path`.
"""

import os
import random
from pathlib import Path

import pytest

from calu.locking import FileLockSpace, RWLockingScheme
from tests.fuzz.locking_harness import (
    BENCH,
    DEADLOCK_PRONE,
    FUZZ,
    KeepsOwnersScheme,
    LoadConfig,
    MixedOrderScheme,
    NoEditLocksScheme,
    NoIssueOrderScheme,
    ReversedWorktreesScheme,
    SharedCommandsScheme,
    compare,
    configured,
    fuzz,
)

pytestmark = pytest.mark.fuzz

SEED = int(os.environ.get("CALU_FUZZ_SEED") or random.SystemRandom().randrange(2**32))
"""One seed per test run; every report prints it, CALU_FUZZ_SEED repeats it."""

SECONDS = float(os.environ.get("CALU_FUZZ_SECONDS") or 10)


def load(base: LoadConfig) -> LoadConfig:
    """Return `base` with the values the CALU_FUZZ_* variables set.

    Args:
        base: The load to start from.

    Returns:
        The load.
    """
    return configured(
        base,
        workers=int(os.environ.get("CALU_FUZZ_WORKERS") or 0) or None,
        worktrees=int(os.environ.get("CALU_FUZZ_WORKTREES") or 0) or None,
        timeout_s=float(os.environ.get("CALU_FUZZ_TIMEOUT_S") or 0) or None,
    )


def test_random_load_breaks_no_rule() -> None:
    """All operation types, batches of parallel calls whose checks finish out of order, the
    owner's edit/command/edit batches, withdrawn and cancelled calls, invalid arguments, probes
    of owners and tickets, releases from another thread, short timeouts: no cycle in the
    wait-for graph, no stall that only a timeout ended, no overlap, no break of an agent's issue
    order, every probe refused, no lock, owner or ticket left after the run."""
    report = fuzz(RWLockingScheme, load(FUZZ), SEED, SECONDS, os.environ.get("CALU_FUZZ_TIMELINE"))
    print(f"\n{report.text()}")
    assert report.problems() == []
    ledger = report.ledger
    assert sum(ledger.done.values()) > 100
    for probe in ("scheme", "lock", "ticket"):
        assert ledger.probes[f"{probe} refused"] > 0
    assert sum(ledger.withdrawn.values()) > 0
    assert sum(ledger.cancelled.values()) > 0
    assert sum(ledger.bad_arguments.values()) > 0


def test_random_load_on_the_file_backend_breaks_no_rule(tmp_path: Path) -> None:
    """The same random load, with the state of the locks in a file below `tmp_path` (threads of
    this process share it; tests/unit/test_locking_files.py covers several processes)."""
    report = fuzz(
        RWLockingScheme, load(FUZZ), SEED, SECONDS, space=FileLockSpace(tmp_path / "locks")
    )
    print(f"\n{report.text()}")
    assert report.problems() == []
    assert sum(report.ledger.done.values()) > 100


def test_a_load_of_commands_in_several_worktrees_never_deadlocks() -> None:
    """Commands only, many in two worktrees or writing outside: the load on which the wrong
    lock orders below deadlock at once. A few timeouts from waiting in line can occur in this
    extreme mix (a command that writes outside holds its worktree while it waits for every
    running command); none of them may end a deadlock."""
    report = fuzz(RWLockingScheme, load(DEADLOCK_PRONE), SEED, SECONDS)
    print(f"\n{report.text()}")
    assert report.problems() == []
    assert not report.ledger.cycles
    assert sum(report.ledger.deadlock_timeouts.values()) == 0


@pytest.mark.parametrize("wrong", [MixedOrderScheme, ReversedWorktreesScheme])
def test_the_fuzzer_finds_the_deadlocks_of_a_wrong_lock_order(
    wrong: type[RWLockingScheme],
) -> None:
    """A scheme with an inconsistent lock order deadlocks; the run must show cycles in the
    wait-for graph and timeouts that ended them."""
    report = fuzz(wrong, load(DEADLOCK_PRONE), SEED, min(SECONDS, 5))
    print(f"\n{report.text()}")
    assert report.ledger.cycles
    assert sum(report.ledger.deadlock_timeouts.values()) > 0


@pytest.mark.parametrize("wrong", [NoEditLocksScheme, SharedCommandsScheme, NoIssueOrderScheme])
def test_the_fuzzer_finds_the_violations_of_a_wrong_scheme(wrong: type[RWLockingScheme]) -> None:
    """A scheme that leaves out a lock or weakens one to shared lets calls overlap, and one
    that ignores the issue order lets a call overtake an earlier call of its agent; the run
    must count them."""
    report = fuzz(wrong, load(FUZZ), SEED, min(SECONDS, 5))
    print(f"\n{report.text()}")
    assert sum(report.ledger.violations.values()) > 0


def test_the_fuzzer_finds_owners_that_a_wrong_scheme_never_gives_back() -> None:
    """A scheme that never accepts an owner again: the end-of-run check must list them."""
    report = fuzz(KeepsOwnersScheme, load(FUZZ), SEED, min(SECONDS, 3))
    print(f"\n{report.text()}")
    assert report.live_owners


def test_one_scenario_with_and_without_locking() -> None:
    """The same scenario with `RWLockingScheme` and `NoLockingScheme`: prints both average
    times. With locks nothing is violated or times out; without, overlaps happen."""
    repetitions = int(os.environ.get("CALU_FUZZ_REPETITIONS") or 3)
    comparison = compare(SEED, load(BENCH), batches_per_worker=30, repetitions=repetitions)
    print(f"\n{comparison.text()}")
    for report in comparison.locked:
        assert report.problems() == []
        assert sum(report.ledger.timeouts.values()) == 0
    assert sum(sum(report.ledger.violations.values()) for report in comparison.unlocked) > 0
