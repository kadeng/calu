"""Random load on a locking scheme: simulated agents run commands and edits (PLAN.md D274).

`tests/fuzz/test_locking_fuzz.py` uses it (marker `fuzz`; never part of CI), and it runs on its
own as `uv run python -m tests.fuzz.locking_harness --help`. Everything runs in threads of this
process. Nothing is started or contacted, and no file is written except a timeline CSV when
one is asked for. Reports go to stdout and hold counts and timings only.

    agent (one thread per simulated agent), in a loop:
        think ─► issue a batch of 1-3 calls (one ticket each, in order) ─► wait for all of them
    call (one thread per call of the batch):
        its check (random length, so checks finish out of order)
          ├─ denied, or a call that takes no lock ─► withdraw the ticket
          ├─ an invalid timeout ─► the call must fail and withdraw its ticket
          └─ ask for the locks with the ticket (sometimes another thread withdraws the ticket
             0-10 ms later: the call is cancelled if still pending) ─► hold them (sometimes
             probe: ask again with the same owner or the same ticket, which must be refused at
             once) ─► release (sometimes from the releaser thread, as Calu's release would come
             with a tool result)
    ledger:  which calls check, wait and hold; checks each grant against the other holders and
             against the agent's earlier calls (issue order)
    monitor: samples the ledger for the timeline every few milliseconds and, when the backend
             has `snapshot`, looks for a cycle in the wait-for graph

Some batches are the owner's example: an edit, a command and an edit in one worktree, in that
order. The timeouts are short (0.5 s by default, compared with 600 s in the live system), so
that waits that only a timeout ends happen often enough to be seen in a few seconds. A run
counts:

- the timeouts of each operation type, and how many of them ended a deadlock;
- the calls that hold and that wait at each moment (averages, peaks, and the timeline);
- deadlocks: a cycle in the wait-for graph of an atomic snapshot (exact), and stalls, i.e.
  nothing holds and nothing is checked while something waits, for at least `stall_min_s`; a
  stall during which a timeout happened is a suspected deadlock (works with every backend);
- violations: two calls that held their locks at the same time although the scheme forbids it
  (`conflict`); a call granted while an earlier call of its agent had not entered the queues,
  or while an earlier, conflicting call of its agent still waited (issue order);
- probes that were not refused at once with `LockError`;
- after the run: locks that an exclusive request with timeout 0 cannot get (every backend),
  owners the scheme still counts as live, and tickets still ISSUED.

A comparison (`compare`) runs one fixed scenario with `RWLockingScheme` and with
`NoLockingScheme` and reports both times and the overlaps that locking prevented.
"""

import argparse
import csv
import random
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from queue import SimpleQueue

from calu.locking import (
    GLOBAL_KEY,
    FileLockSpace,
    Lease,
    LockCancelledError,
    LockError,
    LockingScheme,
    LockMode,
    LockSpace,
    LockState,
    LockTimeoutError,
    LockTimeouts,
    NoLockingScheme,
    Operation,
    OperationKind,
    RWLockingScheme,
    ThreadLockSpace,
    Ticket,
    TicketState,
    file_key,
    worktree_key,
)

COMMAND = "command"
EDIT = "edit"
EDIT_OUTSIDE = "edit outside"
KINDS = (COMMAND, EDIT, EDIT_OUTSIDE)
"""The three operation types, as the reports name them."""

PLURALS = {COMMAND: "commands", EDIT: "edits", EDIT_OUTSIDE: "edits outside"}
"""The reports' plural of each operation type."""

SHORT_TIMEOUT_S = 0.05
"""A call that passes a timeout below this was given a short one on purpose (`draw_timeout`)."""

LOCK = "lock"
DENIED = "denied"
NO_LOCK = "no lock"
"""What a call's check decides: it takes its locks, it was denied, or it takes no lock."""


@dataclass(frozen=True)
class OpSpec:
    """One simulated call of an agent.

    Attributes:
        kind: COMMAND, EDIT or EDIT_OUTSIDE.
        worktrees: A command's worktrees in the order the agent named them (none for a
            command outside every worktree), an edit's one worktree, none for EDIT_OUTSIDE.
        path: The file of an edit in a worktree, or None.
        writes_outside: A command that also writes outside its worktrees.
        hold_s: How long the call runs once it has its locks.
        check_s: How long its check takes before it asks for the locks.
        outcome: LOCK, DENIED or NO_LOCK.
        timeout_s: The call's own timeout, or None for the configured default.
        release_elsewhere: The releaser thread releases it, not the call's thread.
        probe: None, or "scheme" / "lock" / "ticket": while it holds its locks, ask again with
            the same owner through the scheme or directly at one of its locks, or ask with the
            same ticket under another owner; each must be refused at once with `LockError`.
        cancel_after_s: None, or seconds after the call asks for its locks when another thread
            withdraws its ticket (the call is cancelled if it is still pending then).
        bad_argument: The call passes an invalid timeout; it must fail and withdraw its ticket.
    """

    kind: str
    worktrees: tuple[str, ...] = ()
    path: str | None = None
    writes_outside: bool = False
    hold_s: float = 0.0
    check_s: float = 0.0
    outcome: str = LOCK
    timeout_s: float | None = None
    release_elsewhere: bool = False
    probe: str | None = None
    cancel_after_s: float | None = None
    bad_argument: bool = False


@dataclass(frozen=True)
class Batch:
    """The calls an agent issues together, after thinking.

    Attributes:
        think_s: How long the agent thinks before it issues them.
        calls: The calls, in issue order.
    """

    think_s: float
    calls: tuple[OpSpec, ...]


@dataclass(frozen=True)
class LoadConfig:
    """The shape of a simulated load. The `weight_` fields are relative weights of the mix.

    Attributes:
        workers: Simulated agents (threads).
        worktrees: Worktrees `wt0` ... `wt<n-1>`; agent i lives in `wt<i mod n>`.
        files_per_worktree: Files an edit that names its file chooses from.
        home_bias: Share of an agent's calls in its own worktree.
        weight_command: A command in one worktree.
        weight_command_two_worktrees: A command in two worktrees (in random order).
        weight_command_writing_outside: A command in one worktree that writes outside.
        weight_command_no_worktree: A command outside every worktree.
        weight_edit: An edit in a worktree.
        weight_edit_outside: An edit outside the worktrees.
        edit_path_share: Share of edits in a worktree that name their file.
        batch_weights: Relative weights of batches of 1, 2 and 3 calls.
        pattern_share: Share of batches that are the owner's example (edit, command, edit in
            the agent's worktree).
        command_hold_s: (low, high) of a command's run time.
        edit_hold_s: (low, high) of an edit's run time.
        think_s: (low, high) of the thinking time before a batch.
        check_s: (low, high) of a call's check.
        denied_share: Share of calls whose check is denied.
        no_lock_share: Share of calls that take no lock.
        tiny_timeout_share: Share of calls that pass a timeout of 0 to 20 ms.
        random_timeout_share: Share of calls that pass a timeout of 50 to 500 ms.
        over_max_timeout_share: Share of calls that pass twice the maximum (cut to it).
        release_elsewhere_share: Share of leases the releaser thread releases.
        probe_share: Share of calls that probe a repeated request while they hold.
        cancel_share: Share of calls that another thread cancels 0-10 ms after they ask.
        bad_argument_share: Share of calls that pass an invalid timeout with their ticket.
        timeouts: The scheme's defaults and maxima.
        stall_min_s: The shortest stall that is reported.
        monitor_interval_s: How often the monitor samples.
    """

    workers: int = 12
    worktrees: int = 3
    files_per_worktree: int = 3
    home_bias: float = 0.8
    weight_command: float = 40
    weight_command_two_worktrees: float = 5
    weight_command_writing_outside: float = 5
    weight_command_no_worktree: float = 2
    weight_edit: float = 40
    weight_edit_outside: float = 8
    edit_path_share: float = 0.5
    batch_weights: tuple[float, float, float] = (60, 25, 15)
    pattern_share: float = 0.1
    command_hold_s: tuple[float, float] = (0.0, 0.008)
    edit_hold_s: tuple[float, float] = (0.0, 0.002)
    think_s: tuple[float, float] = (0.0, 0.003)
    check_s: tuple[float, float] = (0.0, 0.004)
    denied_share: float = 0.05
    no_lock_share: float = 0.05
    tiny_timeout_share: float = 0.15
    random_timeout_share: float = 0.10
    over_max_timeout_share: float = 0.05
    release_elsewhere_share: float = 0.2
    probe_share: float = 0.03
    cancel_share: float = 0.04
    bad_argument_share: float = 0.02
    timeouts: LockTimeouts = field(default_factory=lambda: LockTimeouts(0.5, 1.0, 0.5, 1.0))
    stall_min_s: float = 0.25
    monitor_interval_s: float = 0.005


FUZZ = LoadConfig()
"""The random load of the fuzz runs: every operation type, short timeouts."""

DEADLOCK_PRONE = replace(
    FUZZ,
    worktrees=2,
    weight_command=40,
    weight_command_two_worktrees=30,
    weight_command_writing_outside=30,
    weight_command_no_worktree=0,
    weight_edit=0,
    weight_edit_outside=0,
    pattern_share=0,
    denied_share=0,
    no_lock_share=0,
    tiny_timeout_share=0,
    random_timeout_share=0,
    over_max_timeout_share=0,
    release_elsewhere_share=0,
    probe_share=0,
    cancel_share=0,
    bad_argument_share=0,
)
"""Only commands, many in two worktrees or writing outside: a wrong lock order deadlocks here."""

BENCH = replace(
    FUZZ,
    workers=6,
    worktrees=2,
    home_bias=0.9,
    weight_command=30,
    weight_command_two_worktrees=0,
    weight_command_writing_outside=2,
    weight_command_no_worktree=0,
    weight_edit=60,
    weight_edit_outside=5,
    edit_path_share=1.0,
    batch_weights=(70, 20, 10),
    pattern_share=0.05,
    command_hold_s=(0.002, 0.06),
    edit_hold_s=(0.0002, 0.001),
    think_s=(0.005, 0.03),
    check_s=(0.001, 0.005),
    denied_share=0.03,
    no_lock_share=0.3,
    tiny_timeout_share=0,
    random_timeout_share=0,
    over_max_timeout_share=0,
    probe_share=0,
    cancel_share=0,
    bad_argument_share=0,
    timeouts=LockTimeouts(30, 60, 30, 60),
)
"""A scenario for timing, on the scale 1 ms = 1 s: an agent thinks 5-30 s, then issues one to
three calls; a check takes 1-5 s, a command runs 2-60 s, an edit 0.2-1 s; 30 % of the calls
(reads) take no lock; three agents share each worktree. Long timeouts, so that nothing times
out."""


def draw_timeout(rng: random.Random, cfg: LoadConfig) -> float | None:
    """Draw the timeout a call passes, by the shares of `cfg`.

    Args:
        rng: The random source.
        cfg: The load.

    Returns:
        Seconds, or None for the configured default.
    """
    roll = rng.random()
    if roll < cfg.tiny_timeout_share:
        return rng.uniform(0.0, 0.02)
    roll -= cfg.tiny_timeout_share
    if roll < cfg.random_timeout_share:
        return rng.uniform(0.05, 0.5)
    roll -= cfg.random_timeout_share
    if roll < cfg.over_max_timeout_share:
        return 2 * max(cfg.timeouts.command_max_s, cfg.timeouts.edit_max_s)
    return None


def _finish(rng: random.Random, cfg: LoadConfig, spec: OpSpec) -> OpSpec:
    """Give `spec` the random properties every call has: check, outcome, timeout, release, probe.

    Args:
        rng: The random source.
        cfg: The load.
        spec: A call with its locks and run time.

    Returns:
        The finished call.
    """
    roll = rng.random()
    outcome = (
        DENIED
        if roll < cfg.denied_share
        else NO_LOCK
        if roll < cfg.denied_share + cfg.no_lock_share
        else LOCK
    )
    probe = None
    if rng.random() < cfg.probe_share:
        probe = rng.choice(["scheme", "lock", "ticket"])
    cancel_after = rng.uniform(0.0, 0.01) if rng.random() < cfg.cancel_share else None
    return replace(
        spec,
        check_s=rng.uniform(*cfg.check_s),
        outcome=outcome,
        timeout_s=draw_timeout(rng, cfg),
        release_elsewhere=rng.random() < cfg.release_elsewhere_share,
        probe=probe,
        cancel_after_s=cancel_after,
        bad_argument=rng.random() < cfg.bad_argument_share,
    )


def random_op(rng: random.Random, cfg: LoadConfig, worker: int) -> OpSpec:
    """Draw one call of agent `worker` from the mix of `cfg`.

    Args:
        rng: The random source.
        cfg: The load.
        worker: The agent's number (decides its home worktree).

    Returns:
        The call.
    """
    names = [f"wt{n}" for n in range(cfg.worktrees)]
    home = names[worker % len(names)]

    def where() -> str:
        return home if rng.random() < cfg.home_bias else rng.choice(names)

    weights = {
        "command": cfg.weight_command,
        "command_two": cfg.weight_command_two_worktrees,
        "command_writing": cfg.weight_command_writing_outside,
        "command_nowhere": cfg.weight_command_no_worktree,
        "edit": cfg.weight_edit,
        "edit_outside": cfg.weight_edit_outside,
    }
    choice = rng.choices(list(weights), weights=list(weights.values()))[0]
    command_hold = rng.uniform(*cfg.command_hold_s)
    edit_hold = rng.uniform(*cfg.edit_hold_s)
    match choice:
        case "command":
            spec = OpSpec(COMMAND, (where(),), hold_s=command_hold)
        case "command_two":
            first = where()
            others = [name for name in names if name != first] or [first]
            spec = OpSpec(COMMAND, (first, rng.choice(others)), hold_s=command_hold)
        case "command_writing":
            spec = OpSpec(COMMAND, (where(),), writes_outside=True, hold_s=command_hold)
        case "command_nowhere":
            spec = OpSpec(COMMAND, (), hold_s=command_hold)
        case "edit":
            named = rng.random() < cfg.edit_path_share
            path = f"f{rng.randrange(cfg.files_per_worktree)}.py" if named else None
            spec = OpSpec(EDIT, (where(),), path=path, hold_s=edit_hold)
        case _:
            spec = OpSpec(EDIT_OUTSIDE, hold_s=edit_hold)
    return _finish(rng, cfg, spec)


def random_batch(rng: random.Random, cfg: LoadConfig, worker: int) -> Batch:
    """Draw the next batch of agent `worker`: random calls, or the owner's example.

    Args:
        rng: The random source.
        cfg: The load.
        worker: The agent's number.

    Returns:
        The batch.
    """
    think = rng.uniform(*cfg.think_s)
    if rng.random() < cfg.pattern_share:
        home = f"wt{worker % cfg.worktrees}"
        calls = (
            OpSpec(EDIT, (home,), hold_s=rng.uniform(*cfg.edit_hold_s)),
            OpSpec(COMMAND, (home,), hold_s=rng.uniform(*cfg.command_hold_s)),
            OpSpec(EDIT, (home,), hold_s=rng.uniform(*cfg.edit_hold_s)),
        )
        return Batch(think, tuple(replace(_finish(rng, cfg, c), outcome=LOCK) for c in calls))
    size = rng.choices([1, 2, 3], weights=list(cfg.batch_weights))[0]
    return Batch(think, tuple(random_op(rng, cfg, worker) for _ in range(size)))


def endless(seed: int, cfg: LoadConfig, worker: int) -> Iterator[Batch]:
    """Yield random batches of agent `worker` without end, reproducibly for `seed`.

    Args:
        seed: The run's seed.
        cfg: The load.
        worker: The agent's number.

    Yields:
        One batch after the other.
    """
    rng = random.Random(f"{seed}:{worker}")
    while True:
        yield random_batch(rng, cfg, worker)


def scenario(seed: int, cfg: LoadConfig, batches_per_worker: int) -> list[list[Batch]]:
    """Draw a fixed scenario: `batches_per_worker` batches for each agent.

    Args:
        seed: The seed; equal seeds give equal scenarios.
        cfg: The load.
        batches_per_worker: Batches per agent.

    Returns:
        One list of batches per agent.
    """
    return [
        [random_batch(rng, cfg, worker) for _ in range(batches_per_worker)]
        for worker, rng in ((n, random.Random(f"{seed}:{n}")) for n in range(cfg.workers))
    ]


def take(scheme: LockingScheme, spec: OpSpec, owner: str, ticket: Ticket | None) -> Lease:
    """Ask `scheme` for the locks of `spec`, through the call a real caller would make.

    Args:
        scheme: The locking scheme.
        spec: The call.
        owner: The call's id (the owner of its locks).
        ticket: The call's ticket, or None.

    Returns:
        The lease.

    Raises:
        LockTimeoutError: The locks were not free in time.
        LockError: The owner already has a lease, or the ticket was used.
    """
    if spec.kind == COMMAND:
        if len(spec.worktrees) == 1:
            return scheme.run_command_in_worktree(
                spec.worktrees[0],
                timeout=spec.timeout_s,
                writes_outside=spec.writes_outside,
                owner=owner,
                ticket=ticket,
            )
        return scheme.run_command_in_worktrees(
            spec.worktrees,
            timeout=spec.timeout_s,
            writes_outside=spec.writes_outside,
            owner=owner,
            ticket=ticket,
        )
    if spec.kind == EDIT:
        return scheme.edit_in_worktree(
            spec.worktrees[0], path=spec.path, timeout=spec.timeout_s, owner=owner, ticket=ticket
        )
    return scheme.edit_outside_worktree(timeout=spec.timeout_s, owner=owner, ticket=ticket)


def describe(spec: OpSpec) -> str:
    """Describe `spec` in a few words.

    Args:
        spec: The call.

    Returns:
        E.g. "command in wt0, wt1, writing outside".
    """
    match spec.kind:
        case "command":
            where = ", ".join(spec.worktrees) or "no worktree"
            return f"command in {where}" + (", writing outside" if spec.writes_outside else "")
        case "edit":
            return f"edit of {spec.path or 'a file'} in {spec.worktrees[0]}"
        case _:
            return "edit outside"


def conflict(a: OpSpec, b: OpSpec) -> str | None:
    """Name the rule of the scheme that `a` and `b` break by holding their locks at once.

    Args:
        a: One call.
        b: Another call.

    Returns:
        The rule, or None when they may run at the same time.
    """
    if a.kind == COMMAND and b.kind == COMMAND:
        if a.writes_outside or b.writes_outside:
            return "a command writing outside ran during another command"
        if set(a.worktrees) & set(b.worktrees):
            return "two commands ran in one worktree at once"
        return None
    if COMMAND in (a.kind, b.kind):
        command, other = (a, b) if a.kind == COMMAND else (b, a)
        if other.kind == EDIT_OUTSIDE:
            return "an edit outside ran during a command"
        if other.worktrees[0] in command.worktrees:
            return "an edit ran in a worktree during a command there"
        return None
    if a.kind == EDIT_OUTSIDE and b.kind == EDIT_OUTSIDE:
        return "two edits outside ran at once"
    if a.kind == EDIT and b.kind == EDIT and a.path is not None:
        if (a.worktrees, a.path) == (b.worktrees, b.path):
            return "two edits of one file ran at once"
    return None


@dataclass(eq=False)
class Op:
    """One call in a run.

    Attributes:
        id: Its label, `w<worker>.<batch>.<position>`; also the owner of its locks.
        spec: What it does.
        ticket: Its place in its agent's issue order.
        batch: The calls of its batch, in issue order (this one included).
        position: Its index in `batch`.
        asked: When it began to wait (monotonic clock).
    """

    id: str
    spec: OpSpec
    ticket: Ticket
    batch: list[Op]
    position: int
    asked: float = 0.0


@dataclass(frozen=True)
class Stall:
    """A time in which no call held its locks or was checked while some call waited.

    Attributes:
        start_s: When it began, in seconds from the start of the run.
        length_s: How long it lasted.
        ended_by_timeout: A timeout happened during it: a suspected deadlock.
    """

    start_s: float
    length_s: float
    ended_by_timeout: bool


class Ledger:
    """What the calls of a run do, under one mutex; see the module docstring."""

    def __init__(self, stall_min_s: float) -> None:
        """Start an empty ledger; its clock starts now.

        Args:
            stall_min_s: The shortest stall that is recorded.
        """
        self.stall_min_s = stall_min_s
        self._mutex = threading.Lock()
        self.started = time.monotonic()
        self.ended = self.started
        self._last = self.started
        self.checking: set[str] = set()
        self.waiting: dict[str, Op] = {}
        self.holding: dict[str, Op] = {}
        self.waiting_now: Counter[str] = Counter()
        self.holding_now: Counter[str] = Counter()
        self.waiting_peak: Counter[str] = Counter()
        self.holding_peak: Counter[str] = Counter()
        self.total_waiting_peak = 0
        self.total_holding_peak = 0
        self.waiting_area: dict[str, float] = dict.fromkeys(KINDS, 0.0)
        self.holding_area: dict[str, float] = dict.fromkeys(KINDS, 0.0)
        self.issued: Counter[str] = Counter()
        self.withdrawn: Counter[str] = Counter()
        self.done: Counter[str] = Counter()
        self.timeouts: Counter[str] = Counter()
        self.short_timeouts: Counter[str] = Counter()
        self.deadlock_timeouts: Counter[str] = Counter()
        self.waits: dict[str, list[float]] = {kind: [] for kind in KINDS}
        self.violations: Counter[str] = Counter()
        self.violation_examples: list[str] = []
        self.probes: Counter[str] = Counter()
        self.cancelled: Counter[str] = Counter()
        self.bad_arguments: Counter[str] = Counter()
        self._suspects: dict[str, list[Op]] = {}
        self.stalls: list[Stall] = []
        self._stall_since: float | None = None
        self._stall_timeout = False
        self.cycles: set[frozenset[str]] = set()
        self.cycle_examples: list[str] = []
        self._deadlocked: set[str] = set()
        self.timeline: list[tuple[float, ...]] = []

    def issues(self, op: Op) -> None:
        """Record that an agent issued `op`.

        Args:
            op: The call.
        """
        with self._mutex:
            self.issued[op.spec.kind] += 1

    def checks(self, op: Op) -> None:
        """Record that the check of `op` begins.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self.checking.add(op.id)
            self._changed(now)

    def withdraws(self, op: Op) -> None:
        """Record that `op` was denied or takes no lock; its ticket is withdrawn.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self.checking.discard(op.id)
            self.withdrawn[op.spec.outcome] += 1
            self._changed(now)

    def asks(self, op: Op) -> None:
        """Record that `op` begins to wait for its turn and its locks.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self.checking.discard(op.id)
            op.asked = now
            self.waiting[op.id] = op
            self.waiting_now[op.spec.kind] += 1
            self._changed(now)

    def holds(self, op: Op) -> None:
        """Record that `op` got its locks; count every overlap and every break of issue order.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self._stop_waiting(op)
            self.waits[op.spec.kind].append(now - op.asked)
            for other in self.holding.values():
                rule = conflict(op.spec, other.spec)
                if rule is not None:
                    self._violation(rule, op, other)
            for later in self._suspects.pop(op.id, ()):
                # `op` still waited when `later`, a later conflicting call of its agent, got
                # its locks; now `op` got its locks as well, so it had not left its queue.
                rule = "a call got its locks before an earlier, conflicting call of its agent"
                self._violation(rule, later, op)
            for earlier in op.batch[: op.position]:
                if earlier.ticket.state is TicketState.ISSUED:
                    rule = "a call got its locks before an earlier call of its agent entered"
                    self._violation(rule, op, earlier)
                elif earlier.id in self.waiting and conflict(op.spec, earlier.spec):
                    # Judged when `earlier` ends: a timeout or a cancellation that took it out
                    # of its queue first is no break of the order.
                    self._suspects.setdefault(earlier.id, []).append(op)
            self.holding[op.id] = op
            self.holding_now[op.spec.kind] += 1
            self._changed(now)

    def timed_out(self, op: Op) -> None:
        """Record that `op` gave up at its timeout; it holds nothing.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self._stop_waiting(op)
            self._suspects.pop(op.id, None)
            self.timeouts[op.spec.kind] += 1
            if op.spec.timeout_s is not None and op.spec.timeout_s < SHORT_TIMEOUT_S:
                self.short_timeouts[op.spec.kind] += 1
            if op.id in self._deadlocked:
                self.deadlock_timeouts[op.spec.kind] += 1
            if self._stall_since is not None:
                self._stall_timeout = True
            self._changed(now)

    def lets_go(self, op: Op) -> None:
        """Record that `op` is about to release its locks: it no longer runs.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            del self.holding[op.id]
            self.holding_now[op.spec.kind] -= 1
            self.done[op.spec.kind] += 1
            self._changed(now)

    def cancelled_call(self, op: Op) -> None:
        """Record that `op` was cancelled while it was pending; it holds nothing.

        Args:
            op: The call.
        """
        with self._mutex:
            now = self._advance()
            self._stop_waiting(op)
            self._suspects.pop(op.id, None)
            self.cancelled[op.spec.kind] += 1
            if self._stall_since is not None:
                self._stall_timeout = True
            self._changed(now)

    def failed_on_argument(self, op: Op, refused: bool) -> None:
        """Record the call of `op` with an invalid timeout; it must fail and withdraw its ticket.

        Args:
            op: The call.
            refused: Whether the scheme refused the argument (TypeError or ValueError).
        """
        with self._mutex:
            now = self._advance()
            self.checking.discard(op.id)
            self.bad_arguments[op.spec.kind] += 1
            if not refused:
                self._violation("a call with an invalid timeout was accepted", op, op)
            elif op.ticket.state is not TicketState.WITHDRAWN:
                self._violation("a call that failed on its arguments kept its ticket", op, op)
            self._changed(now)

    def probed(self, probe: str, outcome: str, op: Op) -> None:
        """Record the outcome of a probe of `op`: "refused" is the only right one.

        Args:
            probe: "scheme", "lock" or "ticket".
            outcome: "refused", "granted" or "waited".
            op: The call that asked again with its own owner or ticket.
        """
        with self._mutex:
            self.probes[f"{probe} {outcome}"] += 1
            if outcome != "refused":
                rule = f"a repeated request of an owner was {outcome}, not refused"
                self._violation(rule, op, op)

    def saw_cycle(self, cycle: Sequence[tuple[str, str]]) -> None:
        """Record a deadlock: a cycle of calls that wait for each other.

        Args:
            cycle: (owner, key it waits for) pairs; each waits for the next, the last for the
                first.
        """
        with self._mutex:
            members = frozenset(owner for owner, _key in cycle)
            self._deadlocked.update(members)
            if members not in self.cycles:
                self.cycles.add(members)
                if len(self.cycle_examples) < 3:
                    self.cycle_examples.append(
                        " -> ".join(f"{owner} waits for {key}" for owner, key in cycle)
                    )

    def sample(self) -> None:
        """Append the current numbers of holding and waiting calls to the timeline."""
        with self._mutex:
            now = self._advance()
            self.timeline.append(
                (
                    now - self.started,
                    *(self.holding_now[kind] for kind in KINDS),
                    *(self.waiting_now[kind] for kind in KINDS),
                )
            )

    def close(self) -> None:
        """End the ledger's clock; a stall still going on is recorded."""
        with self._mutex:
            now = self._advance()
            if self._stall_since is not None:
                self._end_stall(now)
            self.ended = now

    def _violation(self, rule: str, op: Op, other: Op) -> None:
        """Count a break of `rule` by `op` (with `other`); the caller holds `_mutex`.

        Args:
            rule: The rule.
            op: The call that broke it.
            other: The other call involved.
        """
        self.violations[rule] += 1
        if len(self.violation_examples) < 5:
            self.violation_examples.append(
                f"{rule}: {op.id} ({describe(op.spec)}) and {other.id} ({describe(other.spec)})"
            )

    def _advance(self) -> float:
        """Add the time since the last change to the areas under the live counts.

        Returns:
            The current time (monotonic clock).
        """
        now = time.monotonic()
        span = now - self._last
        for kind in KINDS:
            self.waiting_area[kind] += self.waiting_now[kind] * span
            self.holding_area[kind] += self.holding_now[kind] * span
        self._last = now
        return now

    def _stop_waiting(self, op: Op) -> None:
        """Take `op` out of the waiting calls.

        Args:
            op: The call.
        """
        del self.waiting[op.id]
        self.waiting_now[op.spec.kind] -= 1

    def _changed(self, now: float) -> None:
        """Update the peaks, and begin or end a stall.

        Args:
            now: The time of the change.
        """
        for kind in KINDS:
            self.waiting_peak[kind] = max(self.waiting_peak[kind], self.waiting_now[kind])
            self.holding_peak[kind] = max(self.holding_peak[kind], self.holding_now[kind])
        self.total_waiting_peak = max(self.total_waiting_peak, len(self.waiting))
        self.total_holding_peak = max(self.total_holding_peak, len(self.holding))
        stalled = not self.holding and not self.checking and bool(self.waiting)
        if stalled and self._stall_since is None:
            self._stall_since = now
            self._stall_timeout = False
        elif not stalled and self._stall_since is not None:
            self._end_stall(now)

    def _end_stall(self, now: float) -> None:
        """Record the stall that ends at `now` if it lasted at least `stall_min_s`.

        Args:
            now: When it ended.
        """
        assert self._stall_since is not None
        length = now - self._stall_since
        if length >= self.stall_min_s:
            self.stalls.append(Stall(self._stall_since - self.started, length, self._stall_timeout))
        self._stall_since = None


def find_cycle(states: Sequence[LockState]) -> list[tuple[str, str]] | None:
    """Find a cycle in the wait-for graph of `states`.

    A waiting request waits for every holder of its lock whose mode conflicts with it, and for
    every request ahead of it in the queue whose mode conflicts with it (FIFO). A call that
    holds one lock and waits for another links the two locks.

    Args:
        states: Lock states taken at one moment (`LockSpace.snapshot`).

    Returns:
        (owner, key it waits for) pairs, each waiting for the next and the last for the
        first; None when there is no cycle.
    """
    edges: dict[str, list[tuple[str, str]]] = {}
    for state in states:
        for position, (owner, mode) in enumerate(state.waiters):
            blockers = [held for held, held_mode in state.holders if mode.conflicts_with(held_mode)]
            blockers += [
                ahead
                for ahead, ahead_mode in state.waiters[:position]
                if mode.conflicts_with(ahead_mode)
            ]
            edges.setdefault(owner, []).extend((blocker, state.key) for blocker in blockers)
    visited: dict[str, bool] = {}  # owner -> still on the current path
    trail: list[tuple[str, str]] = []

    def visit(owner: str) -> list[tuple[str, str]] | None:
        visited[owner] = True
        for blocker, key in edges.get(owner, ()):
            trail.append((owner, key))
            if visited.get(blocker):
                start = next(i for i, (member, _key) in enumerate(trail) if member == blocker)
                return trail[start:]
            if blocker not in visited:
                found = visit(blocker)
                if found is not None:
                    return found
            trail.pop()
        visited[owner] = False
        return None

    for owner in list(edges):
        if owner not in visited:
            found = visit(owner)
            if found is not None:
                return found
    return None


class Monitor:
    """A thread that samples the ledger and looks for deadlocks in the backend's snapshots."""

    def __init__(
        self, ledger: Ledger, space: LockSpace | None, interval_s: float, timeline: bool
    ) -> None:
        """Prepare the thread; `start` starts it.

        Args:
            ledger: The run's ledger.
            space: The backend whose snapshots are searched for cycles, or None.
            interval_s: Seconds between two samples.
            timeline: Whether to append each sample to the ledger's timeline.
        """
        self.ledger = ledger
        self.space = space
        self.interval_s = interval_s
        self.timeline = timeline
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="locking-monitor", daemon=True)

    def start(self) -> None:
        """Start sampling."""
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and wait for the thread."""
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        """Sample until `stop`."""
        while not self._stop.wait(self.interval_s):
            if self.timeline:
                self.ledger.sample()
            states = self.space.snapshot() if self.space is not None else None
            if states:
                cycle = find_cycle(states)
                if cycle is not None:
                    self.ledger.saw_cycle(cycle)


def _ms(values: Sequence[float]) -> str:
    """Summarize wait times in milliseconds.

    Args:
        values: Seconds.

    Returns:
        "avg ... p95 ... max ..." or "-" when there are none.
    """
    if not values:
        return "-"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return (
        f"avg {statistics.fmean(ordered) * 1000:.1f} p95 {p95 * 1000:.1f} "
        f"max {ordered[-1] * 1000:.1f}"
    )


@dataclass
class Report:
    """The outcome of one run.

    Attributes:
        title: What ran.
        seed: The seed of the calls.
        workers: Simulated agents.
        worktrees: Worktrees.
        timeouts: The scheme's timeouts.
        ledger: What the calls did.
        leftover: Locks that still had holders or waiters after the run (from a snapshot).
        leaked: Locks that an exclusive request with timeout 0 could not get after the run;
            works with every backend.
        live_owners: Owners the scheme still counted as having a lease after the run.
        open_tickets: Tickets that were still ISSUED after the run.
        hung: Agents or calls that had not finished when the run gave up on them.
        errors: Unexpected exceptions of the agents, the calls and the releaser.
    """

    title: str
    seed: int
    workers: int
    worktrees: int
    timeouts: LockTimeouts
    ledger: Ledger
    leftover: list[str]
    leaked: list[str]
    live_owners: frozenset[str]
    open_tickets: dict[str, tuple[int, ...]]
    hung: int
    errors: list[str]

    @property
    def elapsed_s(self) -> float:
        """How long the run took."""
        return self.ledger.ended - self.ledger.started

    @property
    def suspected_deadlocks(self) -> list[Stall]:
        """The stalls during which a timeout happened."""
        return [stall for stall in self.ledger.stalls if stall.ended_by_timeout]

    def problems(self) -> list[str]:
        """List what the scheme must never do and the run saw.

        Returns:
            One line per problem; empty when the run was clean.
        """
        ledger = self.ledger
        found: list[str] = []
        if ledger.violations:
            found.append(
                f"violations {dict(ledger.violations)}; e.g. {ledger.violation_examples[0]}"
            )
        if ledger.cycles:
            found.append(f"{len(ledger.cycles)} deadlocks; e.g. {ledger.cycle_examples[0]}")
        if self.suspected_deadlocks:
            longest = max(stall.length_s for stall in self.suspected_deadlocks)
            found.append(
                f"{len(self.suspected_deadlocks)} stalls that only a timeout ended "
                f"(suspected deadlocks; the longest {longest:.2f} s)"
            )
        if self.leftover:
            found.append(f"locks left after the run: {'; '.join(self.leftover)}")
        if self.leaked:
            found.append(f"locks no one could take after the run: {', '.join(self.leaked)}")
        if self.live_owners:
            some = ", ".join(sorted(self.live_owners)[:5])
            found.append(f"{len(self.live_owners)} owners left with a lease, e.g. {some}")
        if self.open_tickets:
            found.append(f"tickets left ISSUED after the run: {self.open_tickets}")
        if self.hung:
            found.append(f"{self.hung} agents or calls did not finish")
        if self.errors:
            found.append(f"errors: {'; '.join(self.errors[:3])}")
        return found

    def text(self) -> str:
        """Describe the run in a few lines, for stdout.

        Returns:
            The report.
        """
        ledger = self.ledger
        seconds = max(self.elapsed_s, 1e-9)

        def per_kind(counts: Counter[str]) -> str:
            return ", ".join(f"{PLURALS[kind]} {counts[kind]}" for kind in KINDS)

        def live(area: dict[str, float], now_peak: Counter[str], total_peak: int) -> str:
            parts = ", ".join(
                f"{PLURALS[kind]} avg {area[kind] / seconds:.2f} max {now_peak[kind]}"
                for kind in KINDS
            )
            return f"avg {sum(area.values()) / seconds:.2f} max {total_peak} ({parts})"

        ended_by_grant = [stall for stall in ledger.stalls if not stall.ended_by_timeout]
        t = self.timeouts
        probes = ", ".join(f"{name} {count}" for name, count in sorted(ledger.probes.items()))
        lines = [
            f"{self.title}: seed {self.seed}, {self.workers} agents, {self.worktrees} worktrees, "
            f"{self.elapsed_s:.1f} s, timeouts commands {t.command_default_s:g}/"
            f"{t.command_max_s:g} s, edits {t.edit_default_s:g}/{t.edit_max_s:g} s "
            "(default/max)",
            f"  issued:    {per_kind(ledger.issued)}; withdrawn: denied "
            f"{ledger.withdrawn[DENIED]}, no lock {ledger.withdrawn[NO_LOCK]}; cancelled "
            f"while pending: {sum(ledger.cancelled.values())}; invalid arguments: "
            f"{sum(ledger.bad_arguments.values())}",
            f"  done:      {per_kind(ledger.done)}",
            f"  timeouts:  {per_kind(ledger.timeouts)}",
            f"    of them with a short timeout on purpose (< {SHORT_TIMEOUT_S * 1000:g} ms): "
            f"{per_kind(ledger.short_timeouts)}",
            f"    of them ended a deadlock: {per_kind(ledger.deadlock_timeouts)}",
            "  running:   "
            + live(ledger.holding_area, ledger.holding_peak, ledger.total_holding_peak),
            "  waiting:   "
            + live(ledger.waiting_area, ledger.waiting_peak, ledger.total_waiting_peak),
            "  waits (ms): "
            + "; ".join(f"{PLURALS[kind]} {_ms(ledger.waits[kind])}" for kind in KINDS),
            f"  deadlocks: {len(ledger.cycles)} cycles in the wait-for graph; stalls of at least "
            f"{ledger.stall_min_s:g} s: {len(self.suspected_deadlocks)} ended by a timeout, "
            f"{len(ended_by_grant)} by a grant",
            f"  probes:    {probes or 'none'}",
            f"  violations (overlaps, issue order, reentry, tickets): "
            f"{sum(ledger.violations.values())}",
            f"  after the run: {len(self.leaked)} locks leaked, {len(self.live_owners)} owners "
            f"live, {sum(map(len, self.open_tickets.values()))} tickets open",
        ]
        lines += [f"  PROBLEM:   {problem}" for problem in self.problems()]
        return "\n".join(lines)


def _probe(scheme: LockingScheme, op: Op, lease: Lease, ledger: Ledger) -> None:
    """Ask again while `op` holds `lease`: with its owner, or with its ticket; it must be
    refused at once with `LockError`.

    Args:
        scheme: The scheme under test.
        op: The call that holds its locks.
        lease: Its lease.
        ledger: The run's ledger.
    """
    probe = op.spec.probe or "scheme"
    if probe == "lock":
        if not isinstance(scheme, RWLockingScheme) or not lease.locks:
            return
        key, mode = lease.locks[0]
        try:
            request = scheme.space.lock(key).request(mode, op.id)
        except LockError:
            ledger.probed(probe, "refused", op)
            return
        request.withdraw()
        ledger.probed(probe, "granted", op)
        return
    owner, ticket = (op.id, None) if probe == "scheme" else (f"{op.id}~reuse", op.ticket)
    try:
        extra = take(scheme, replace(op.spec, timeout_s=0.2), owner, ticket)
    except LockError:
        ledger.probed(probe, "refused", op)
    except LockTimeoutError:
        ledger.probed(probe, "waited", op)
    else:
        ledger.probed(probe, "granted", op)
        extra.release()


def _call(
    op: Op, scheme: LockingScheme, ledger: Ledger, handoff: SimpleQueue[tuple[Op, Lease] | None]
) -> None:
    """Run one call: its check, then its locks, its run and its release (see the module docstring).

    Args:
        op: The call.
        scheme: The scheme under test.
        ledger: The run's ledger.
        handoff: Where leases go that the releaser thread releases.
    """
    spec = op.spec
    ledger.checks(op)
    if spec.check_s > 0:
        time.sleep(spec.check_s)
    if spec.outcome != LOCK:
        op.ticket.withdraw()
        ledger.withdraws(op)
        return
    if spec.bad_argument:
        try:
            take(scheme, replace(spec, timeout_s=-1.0), op.id, op.ticket).release()
        except TypeError, ValueError:
            ledger.failed_on_argument(op, refused=True)
        else:
            ledger.failed_on_argument(op, refused=False)
        return
    ledger.asks(op)
    if spec.cancel_after_s is not None:
        canceller = threading.Timer(spec.cancel_after_s, op.ticket.withdraw)
        canceller.daemon = True
        canceller.start()
    try:
        lease = take(scheme, spec, op.id, op.ticket)
    except LockTimeoutError:
        ledger.timed_out(op)
        return
    except LockCancelledError:
        ledger.cancelled_call(op)
        return
    except LockError:
        if spec.cancel_after_s is None or op.ticket.state is not TicketState.WITHDRAWN:
            raise
        ledger.cancelled_call(op)  # withdrawn before the lock call claimed the ticket
        return
    ledger.holds(op)
    if spec.probe is not None:
        _probe(scheme, op, lease, ledger)
    if spec.hold_s > 0:
        time.sleep(spec.hold_s)
    if spec.release_elsewhere:
        handoff.put((op, lease))
    else:
        ledger.lets_go(op)
        lease.release()


def _agent(
    index: int,
    scheme: LockingScheme,
    batches: Iterable[Batch],
    ledger: Ledger,
    handoff: SimpleQueue[tuple[Op, Lease] | None],
    stop: threading.Event,
    spawn: Callable[[str, Callable[..., None], tuple[object, ...]], threading.Thread],
    patience_s: float,
) -> None:
    """Run the batches of one agent until they end or `stop` is set.

    Each batch: think, issue its calls (one ticket each, in order), run every call in a
    thread of its own, and wait for all of them.

    Args:
        index: The agent's number.
        scheme: The scheme under test.
        batches: The agent's batches.
        ledger: The run's ledger.
        handoff: Where leases go that the releaser thread releases.
        stop: Set when the run is over; the current batch still ends.
        spawn: Makes a thread that records an exception of its target as an error.
        patience_s: How long to wait for the calls of a batch.

    Raises:
        TimeoutError: A call of a batch did not finish within `patience_s`.
    """
    agent = f"agent-{index}"
    for number, batch in enumerate(batches):
        if stop.is_set():
            return
        if batch.think_s > 0:
            time.sleep(batch.think_s)
        ops: list[Op] = []
        for position, spec in enumerate(batch.calls):
            op = Op(f"w{index}.{number}.{position}", spec, scheme.issue(agent), ops, position)
            ops.append(op)
            ledger.issues(op)
        threads = [spawn(op.id, _call, (op, scheme, ledger, handoff)) for op in ops]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(patience_s)
            if thread.is_alive():
                raise TimeoutError(f"the call {thread.name} did not finish")


def _release_handed_over(ledger: Ledger, handoff: SimpleQueue[tuple[Op, Lease] | None]) -> None:
    """Release the leases the calls hand over, until None arrives.

    Args:
        ledger: The run's ledger.
        handoff: The leases.
    """
    while (item := handoff.get()) is not None:
        op, lease = item
        ledger.lets_go(op)
        lease.release()


def all_keys(cfg: LoadConfig) -> list[str]:
    """Return the key of every lock a load of `cfg` can use.

    Args:
        cfg: The load.

    Returns:
        The global key, the worktree keys and the file keys.
    """
    keys = [GLOBAL_KEY]
    for n in range(cfg.worktrees):
        keys.append(worktree_key(f"wt{n}"))
        keys += [file_key(f"wt{n}", f"f{f}.py") for f in range(cfg.files_per_worktree)]
    return keys


def run_load(
    scheme: LockingScheme,
    workloads: Sequence[Iterable[Batch]],
    cfg: LoadConfig,
    *,
    title: str,
    seed: int,
    seconds: float | None = None,
    patience_s: float = 300.0,
    timeline_path: str | None = None,
) -> Report:
    """Run one agent thread per workload against `scheme` and report what happened.

    Args:
        scheme: The scheme under test; its backend is searched for deadlocks when it is an
            `RWLockingScheme` whose space has snapshots.
        workloads: The batches of each agent; endless ones need `seconds`.
        cfg: The load (stall threshold, monitor interval, worktrees for the report).
        title: The report's title.
        seed: The seed, for the report.
        seconds: Stop starting batches after this many seconds; None runs the workloads to
            their end.
        patience_s: Seconds after the end (or after `seconds`) before unfinished agents count
            as hung; also the longest wait for the calls of one batch.
        timeline_path: Write the sampled numbers of holding and waiting calls to this CSV file;
            None writes nothing.

    Returns:
        The report.
    """
    ledger = Ledger(cfg.stall_min_s)
    space = scheme.space if isinstance(scheme, RWLockingScheme) else None
    monitor = Monitor(ledger, space, cfg.monitor_interval_s, timeline=timeline_path is not None)
    stop = threading.Event()
    handoff: SimpleQueue[tuple[Op, Lease] | None] = SimpleQueue()
    errors: list[str] = []

    def spawn(name: str, target: Callable[..., None], args: tuple[object, ...]) -> threading.Thread:
        def body() -> None:
            try:
                target(*args)
            except BaseException as exc:
                errors.append(f"{name}: {exc!r}")

        return threading.Thread(target=body, name=name, daemon=True)

    agents = [
        spawn(
            f"agent-{index}",
            _agent,
            (index, scheme, batches, ledger, handoff, stop, spawn, patience_s),
        )
        for index, batches in enumerate(workloads)
    ]
    releaser = spawn("releaser", _release_handed_over, (ledger, handoff))
    monitor.start()
    releaser.start()
    for agent in agents:
        agent.start()
    if seconds is not None:
        time.sleep(seconds)
        stop.set()
    give_up = time.monotonic() + patience_s
    for agent in agents:
        agent.join(max(0.0, give_up - time.monotonic()))
    hung = sum(agent.is_alive() for agent in agents)
    handoff.put(None)
    releaser.join(10)
    monitor.stop()
    ledger.close()
    leftover: list[str] = []
    leaked: list[str] = []
    states = space.snapshot() if space is not None and not hung else None
    for state in states or ():
        if state.holders or state.waiters:
            leftover.append(f"{state.key}: holders {state.holders}, waiters {state.waiters}")
    if space is not None and not hung:
        for key in all_keys(cfg):
            hold = space.lock(key).acquire(LockMode.EXCLUSIVE, 0, "leak check")
            if hold is None:
                leaked.append(key)
            else:
                space.lock(key).release(hold)
    if timeline_path is not None:
        with open(timeline_path, "w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(
                ["t_s", *(f"running_{k}" for k in KINDS), *(f"waiting_{k}" for k in KINDS)]
            )
            writer.writerows(ledger.timeline)
    return Report(
        title=title,
        seed=seed,
        workers=len(workloads),
        worktrees=cfg.worktrees,
        timeouts=scheme.timeouts,
        ledger=ledger,
        leftover=leftover,
        leaked=leaked,
        live_owners=scheme.live_owners(),
        open_tickets=scheme.open_tickets(),
        hung=hung,
        errors=errors,
    )


def fuzz(
    scheme_class: type[RWLockingScheme],
    cfg: LoadConfig,
    seed: int,
    seconds: float,
    timeline_path: str | None = None,
    space: LockSpace | None = None,
) -> Report:
    """Put random load on a scheme of `scheme_class` for `seconds`.

    Args:
        scheme_class: `RWLockingScheme` or one of the wrong schemes below.
        cfg: The load.
        seed: The seed of the calls.
        seconds: How long batches start.
        timeline_path: Where to write the timeline CSV, or None.
        space: The backend, fresh and unused; None for a new `ThreadLockSpace`.

    Returns:
        The report.
    """
    backend = space if space is not None else ThreadLockSpace()
    scheme = scheme_class(backend, cfg.timeouts)
    workloads = [endless(seed, cfg, worker) for worker in range(cfg.workers)]
    return run_load(
        scheme,
        workloads,
        cfg,
        title=f"{scheme_class.__name__} on {type(backend).__name__}, random load",
        seed=seed,
        seconds=seconds,
        patience_s=30 + 4 * max(cfg.timeouts.command_max_s, cfg.timeouts.edit_max_s),
        timeline_path=timeline_path,
    )


@dataclass
class Comparison:
    """One scenario run with and without locks, several times each.

    Attributes:
        seed: The scenario's seed.
        batches_per_worker: Batches per agent.
        locked: The runs with `RWLockingScheme`.
        unlocked: The runs with `NoLockingScheme`.
    """

    seed: int
    batches_per_worker: int
    locked: list[Report]
    unlocked: list[Report]

    def text(self) -> str:
        """Describe the comparison in a few lines, for stdout.

        Returns:
            The report.
        """
        locked = statistics.fmean(report.elapsed_s for report in self.locked)
        unlocked = statistics.fmean(report.elapsed_s for report in self.unlocked)
        overlaps = statistics.fmean(
            sum(report.ledger.violations.values()) for report in self.unlocked
        )
        waits = {
            kind: [wait for report in self.locked for wait in report.ledger.waits[kind]]
            for kind in KINDS
        }
        timeouts = sum(sum(report.ledger.timeouts.values()) for report in self.locked)
        first = self.locked[0]
        return "\n".join(
            [
                f"Scenario: seed {self.seed}, {first.workers} agents, {first.worktrees} "
                f"worktrees, {self.batches_per_worker} batches each, {len(self.locked)} runs "
                "with and without locking",
                f"  without locking: {unlocked:.3f} s on average; violations that locking "
                f"prevents: {overlaps:.1f} per run",
                f"  with locking:    {locked:.3f} s on average (x{locked / unlocked:.2f}); "
                f"timeouts: {timeouts}",
                "  waits with locking (ms): "
                + "; ".join(f"{PLURALS[kind]} {_ms(waits[kind])}" for kind in KINDS),
            ]
        )


def compare(seed: int, cfg: LoadConfig, batches_per_worker: int, repetitions: int) -> Comparison:
    """Run one scenario `repetitions` times with locks and without, alternately.

    Args:
        seed: The scenario's seed.
        cfg: The load.
        batches_per_worker: Batches per agent.
        repetitions: Runs per scheme.

    Returns:
        The comparison.
    """
    workloads = scenario(seed, cfg, batches_per_worker)
    locked: list[Report] = []
    unlocked: list[Report] = []
    for _ in range(repetitions):
        for scheme, runs in (
            (RWLockingScheme(ThreadLockSpace(), cfg.timeouts), locked),
            (NoLockingScheme(cfg.timeouts), unlocked),
        ):
            title = f"{type(scheme).__name__}, scenario"
            runs.append(run_load(scheme, workloads, cfg, title=title, seed=seed))
    return Comparison(seed, batches_per_worker, locked, unlocked)


# Schemes that are wrong on purpose: the fuzzer must find their faults.


class MixedOrderScheme(RWLockingScheme):
    """WRONG on purpose: a command that writes outside takes the global lock before its
    worktree locks, while every other operation takes the global lock last."""

    def plan(self, operation: Operation) -> tuple[tuple[str, LockMode], ...]:
        """Return the locks in the wrong order for commands that write outside.

        Args:
            operation: The operation.

        Returns:
            (key, mode) pairs.
        """
        steps = super().plan(operation)
        if operation.kind is OperationKind.COMMAND and operation.writes_outside:
            return steps[-1:] + steps[:-1]
        return steps


class ReversedWorktreesScheme(RWLockingScheme):
    """WRONG on purpose: every second command takes its worktree locks in reverse order."""

    def plan(self, operation: Operation) -> tuple[tuple[str, LockMode], ...]:
        """Return the worktree locks reversed for owners with an odd character sum.

        Args:
            operation: The operation.

        Returns:
            (key, mode) pairs.
        """
        steps = super().plan(operation)
        if operation.kind is OperationKind.COMMAND and sum(map(ord, operation.owner)) % 2:
            return tuple(reversed(steps[:-1])) + steps[-1:]
        return steps


class NoEditLocksScheme(RWLockingScheme):
    """WRONG on purpose: edits in a worktree take no lock."""

    def plan(self, operation: Operation) -> tuple[tuple[str, LockMode], ...]:
        """Return no locks for edits in a worktree.

        Args:
            operation: The operation.

        Returns:
            (key, mode) pairs.
        """
        return () if operation.kind is OperationKind.EDIT else super().plan(operation)


class SharedCommandsScheme(RWLockingScheme):
    """WRONG on purpose: commands take their worktree locks shared."""

    def plan(self, operation: Operation) -> tuple[tuple[str, LockMode], ...]:
        """Return shared worktree locks for commands.

        Args:
            operation: The operation.

        Returns:
            (key, mode) pairs.
        """
        steps = super().plan(operation)
        if operation.kind is not OperationKind.COMMAND:
            return steps
        return tuple((key, mode if key == GLOBAL_KEY else LockMode.SHARED) for key, mode in steps)


class NoIssueOrderScheme(RWLockingScheme):
    """WRONG on purpose: a call enters the lock queues without waiting for the earlier calls of
    its agent."""

    def _await_turn(self, ticket: Ticket, deadline: float) -> bool:
        """Return at once, whatever the agent's earlier calls do.

        Args:
            ticket: The call's ticket.
            deadline: Ignored.

        Returns:
            True.
        """
        return True


class KeepsOwnersScheme(RWLockingScheme):
    """WRONG on purpose: it never accepts an owner again, as if leases leaked their owners."""

    def _unclaim(self, owner: str) -> None:
        """Do nothing.

        Args:
            owner: Ignored.
        """


SCHEMES: dict[str, type[RWLockingScheme]] = {
    "rw": RWLockingScheme,
    "mixed-order": MixedOrderScheme,
    "reversed-worktrees": ReversedWorktreesScheme,
    "no-edit-locks": NoEditLocksScheme,
    "shared-commands": SharedCommandsScheme,
    "no-issue-order": NoIssueOrderScheme,
    "keeps-owners": KeepsOwnersScheme,
}
"""The schemes the command line can run; only `rw` is right."""


def configured(
    base: LoadConfig,
    *,
    workers: int | None = None,
    worktrees: int | None = None,
    timeout_s: float | None = None,
) -> LoadConfig:
    """Return `base` with the given values replaced.

    Args:
        base: The load to start from.
        workers: Simulated agents, or None to keep.
        worktrees: Worktrees, or None to keep.
        timeout_s: The default timeout of commands and edits (the maximum becomes twice that),
            or None to keep.

    Returns:
        The load.
    """
    changes: dict[str, object] = {}
    if workers:
        changes["workers"] = workers
    if worktrees:
        changes["worktrees"] = worktrees
    if timeout_s:
        changes["timeouts"] = LockTimeouts(timeout_s, 2 * timeout_s, timeout_s, 2 * timeout_s)
    return replace(base, **changes)  # type: ignore[arg-type]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harness from the command line; print the reports to stdout.

    Args:
        argv: The arguments; None for `sys.argv[1:]`.

    Returns:
        1 when a fuzz run of the right scheme found a problem, else 0.
    """
    parser = argparse.ArgumentParser(
        prog="python -m tests.fuzz.locking_harness",
        description="Random load on calu.locking with simulated agents (threads only).",
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    run = modes.add_parser("fuzz", help="random load for some seconds")
    run.add_argument("--seconds", type=float, default=10.0)
    run.add_argument("--seed", type=int)
    run.add_argument("--workers", type=int)
    run.add_argument("--worktrees", type=int)
    run.add_argument(
        "--timeout", type=float, help="default wait in seconds (the maximum is twice that)"
    )
    run.add_argument(
        "--deadlock-prone",
        action="store_true",
        help="only commands, many in two worktrees or writing outside",
    )
    run.add_argument(
        "--scheme",
        choices=sorted(SCHEMES),
        default="rw",
        help="rw is the real scheme; the others are wrong on purpose",
    )
    run.add_argument(
        "--timeline", metavar="CSV", help="write the numbers of holding and waiting calls"
    )
    run.add_argument(
        "--files",
        metavar="DIR",
        help="use the file backend with its state in DIR (a new, empty directory)",
    )
    bench = modes.add_parser("bench", help="one scenario with and without locking")
    bench.add_argument("--seed", type=int)
    bench.add_argument("--workers", type=int)
    bench.add_argument("--worktrees", type=int)
    bench.add_argument("--batches", type=int, default=30, help="batches per agent")
    bench.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args(argv)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    if args.mode == "bench":
        cfg = configured(BENCH, workers=args.workers, worktrees=args.worktrees)
        print(compare(seed, cfg, args.batches, args.repetitions).text())
        return 0
    base = DEADLOCK_PRONE if args.deadlock_prone else FUZZ
    cfg = configured(base, workers=args.workers, worktrees=args.worktrees, timeout_s=args.timeout)
    space = FileLockSpace(args.files) if args.files else None
    report = fuzz(SCHEMES[args.scheme], cfg, seed, args.seconds, args.timeline, space)
    print(report.text())
    return 1 if args.scheme == "rw" and report.problems() else 0


if __name__ == "__main__":
    sys.exit(main())
