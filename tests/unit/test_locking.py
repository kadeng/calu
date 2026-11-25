"""The locking component `calu.locking` on its thread backend (agent/records/PLAN.md D274).

Deterministic tests: threads of this process only, no files, no output. A call that must be
refused waits `BLOCKED` seconds; a call that must get in waits up to 5 s. The randomized load
tests are in `tests/fuzz/` (marker `fuzz`).
"""

import math
import threading
import time
from collections.abc import Callable

import pytest

from calu.locking import (
    GENERATED_OWNER_PREFIX,
    GLOBAL_KEY,
    Hold,
    Lease,
    LockCancelledError,
    LockError,
    LockingScheme,
    LockMode,
    LockTimeoutError,
    LockTimeouts,
    NoLockingScheme,
    Operation,
    OperationKind,
    RWLockingScheme,
    ThreadLockSpace,
    ThreadRWLock,
    TicketState,
    checked_timeout,
    file_key,
    worktree_key,
)

BLOCKED = 0.05
"""Seconds a call waits in a test that expects it not to get its lock."""

SHARED = LockMode.SHARED
EXCLUSIVE = LockMode.EXCLUSIVE


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll `condition` every millisecond until it is true.

    Args:
        condition: What to wait for.
        timeout: Seconds before giving up.

    Raises:
        AssertionError: The condition was still false after `timeout` seconds.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not become true in time")
        time.sleep(0.001)


class Background:
    """Run one function in a daemon thread and keep its result or exception."""

    def __init__(self, function: Callable[[], object]) -> None:
        """Start `function` in a new thread.

        Args:
            function: What the thread runs.
        """
        self.result: object = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(function,), daemon=True)
        self._thread.start()

    def _run(self, function: Callable[[], object]) -> None:
        """Call `function` and store what it returned or raised.

        Args:
            function: What to call.
        """
        try:
            self.result = function()
        except BaseException as exc:
            self.error = exc

    def finish(self, timeout: float = 5.0) -> object:
        """Wait for the thread and return its result.

        Args:
            timeout: Seconds to wait for the thread.

        Returns:
            What the function returned.

        Raises:
            AssertionError: The thread still runs after `timeout` seconds.
            BaseException: Whatever the function raised.
        """
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("the thread did not finish in time")
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def running(self) -> bool:
        """Whether the thread still runs."""
        return self._thread.is_alive()


def lease_of(background: Background) -> Lease:
    """Wait for a background lock call and return its lease.

    Args:
        background: The thread that made the call.

    Returns:
        The lease it got.
    """
    lease = background.finish()
    assert isinstance(lease, Lease)
    return lease


def hold_of(background: Background) -> Hold:
    """Wait for a background lock request and return its hold.

    Args:
        background: The thread that waited.

    Returns:
        The hold it got.
    """
    hold = background.finish()
    assert isinstance(hold, Hold)
    return hold


def waiters(lock: ThreadRWLock) -> int:
    """Return how many requests wait for `lock`.

    Args:
        lock: The lock.

    Returns:
        The number of waiting requests.
    """
    return len(lock.state().waiters)


def scheme(**timeouts: float) -> RWLockingScheme:
    """Return a scheme on a fresh thread backend.

    Args:
        **timeouts: `LockTimeouts` fields; the defaults here are 5 s (default) and 10 s (max).

    Returns:
        The scheme.
    """
    values = {
        "command_default_s": 5.0,
        "command_max_s": 10.0,
        "edit_default_s": 5.0,
        "edit_max_s": 10.0,
    }
    values.update(timeouts)
    return RWLockingScheme(ThreadLockSpace(), LockTimeouts(**values))


def space_of(locking: RWLockingScheme) -> ThreadLockSpace:
    """Return the thread backend of `locking`.

    Args:
        locking: A scheme made by `scheme`.

    Returns:
        Its lock space.
    """
    assert isinstance(locking.space, ThreadLockSpace)
    return locking.space


def holders(locking: RWLockingScheme, key: str) -> tuple[tuple[str, LockMode], ...]:
    """Return the holders of one lock of `locking`'s backend.

    Args:
        locking: A scheme on a thread backend.
        key: The lock's key.

    Returns:
        (owner, mode) per hold; empty when the lock was never used.
    """
    for state in space_of(locking).snapshot():
        if state.key == key:
            return state.holders
    return ()


def queue_of(locking: RWLockingScheme, key: str) -> list[str]:
    """Return the owners waiting for one lock of `locking`'s backend, first in line first.

    Args:
        locking: A scheme on a thread backend.
        key: The lock's key.

    Returns:
        The owners.
    """
    for state in space_of(locking).snapshot():
        if state.key == key:
            return [owner for owner, _mode in state.waiters]
    return []


# The thread backend's lock: FIFO, owners, no reentrancy, bounded waits


def test_shared_holds_coexist() -> None:
    lock = ThreadRWLock("k")
    first = lock.acquire(SHARED, 0, "a")
    second = lock.acquire(SHARED, 0, "b")
    assert first is not None and second is not None
    assert first.token != second.token


def test_an_exclusive_hold_excludes_every_other_hold() -> None:
    lock = ThreadRWLock("k")
    assert lock.acquire(EXCLUSIVE, 0, "a") is not None
    assert lock.acquire(SHARED, BLOCKED, "b") is None
    assert lock.acquire(EXCLUSIVE, BLOCKED, "c") is None


def test_a_shared_hold_excludes_an_exclusive_request() -> None:
    lock = ThreadRWLock("k")
    assert lock.acquire(SHARED, 0, "a") is not None
    assert lock.acquire(EXCLUSIVE, BLOCKED, "b") is None


def test_a_waiting_request_gets_the_lock_when_it_is_released() -> None:
    lock = ThreadRWLock("k")
    hold = lock.acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    waiting = Background(lambda: lock.acquire(SHARED, 5, "reader"))
    wait_until(lambda: waiters(lock) == 1)
    lock.release(hold)
    assert waiting.finish() is not None
    assert lock.state().holders == (("reader", SHARED),)


def test_requests_are_granted_in_the_order_they_were_made_not_waited_for() -> None:
    lock = ThreadRWLock("k")
    first = lock.acquire(EXCLUSIVE, 0, "a")
    assert first is not None
    shared = lock.request(SHARED, "b")
    exclusive = lock.request(EXCLUSIVE, "c")
    late_shared = lock.request(SHARED, "d")
    # The waits begin in the opposite order; the queue keeps the order of the requests.
    late_wait = Background(lambda: late_shared.wait(5))
    exclusive_wait = Background(lambda: exclusive.wait(5))
    shared_wait = Background(lambda: shared.wait(5))
    lock.release(first)
    wait_until(lambda: lock.state().holders == (("b", SHARED),))
    assert lock.state().waiters == (("c", EXCLUSIVE), ("d", SHARED))
    lock.release(hold_of(shared_wait))
    exclusive_hold = hold_of(exclusive_wait)
    assert lock.state().holders == (("c", EXCLUSIVE),)
    lock.release(exclusive_hold)
    hold_of(late_wait)
    assert lock.state().holders == (("d", SHARED),)


def test_a_waiting_exclusive_request_keeps_later_shared_requests_out() -> None:
    lock = ThreadRWLock("k")
    reader = lock.acquire(SHARED, 0, "reader")
    assert reader is not None
    writer = Background(lambda: lock.acquire(EXCLUSIVE, 5, "writer"))
    wait_until(lambda: waiters(lock) == 1)
    # Compatible with the current shared hold, but behind the waiting exclusive request.
    assert lock.acquire(SHARED, BLOCKED, "late reader") is None
    lock.release(reader)
    assert writer.finish() is not None
    assert lock.state().holders == (("writer", EXCLUSIVE),)


def test_a_request_that_timed_out_leaves_the_queue() -> None:
    lock = ThreadRWLock("k")
    assert lock.acquire(SHARED, 0, "reader") is not None
    assert lock.acquire(EXCLUSIVE, BLOCKED, "writer") is None
    assert lock.state().waiters == ()
    assert lock.acquire(SHARED, 0, "second reader") is not None


def test_a_timed_out_head_of_the_queue_lets_the_requests_behind_it_in() -> None:
    lock = ThreadRWLock("k")
    assert lock.acquire(SHARED, 0, "reader") is not None
    writer = Background(lambda: lock.acquire(EXCLUSIVE, 0.3, "writer"))
    wait_until(lambda: waiters(lock) == 1)
    second_reader = Background(lambda: lock.acquire(SHARED, 5, "second reader"))
    wait_until(lambda: waiters(lock) == 2)
    assert writer.finish() is None
    assert second_reader.finish() is not None


def test_a_withdrawn_request_leaves_the_queue_or_gives_its_grant_back() -> None:
    lock = ThreadRWLock("k")
    first = lock.acquire(EXCLUSIVE, 0, "a")
    assert first is not None
    queued = lock.request(SHARED, "b")
    queued.withdraw()
    queued.withdraw()
    assert lock.state().waiters == ()
    assert queued.wait(5) is None
    lock.release(first)
    granted = lock.request(EXCLUSIVE, "c")  # granted at once, not yet returned by `wait`
    assert lock.state().holders == (("c", EXCLUSIVE),)
    granted.withdraw()
    assert lock.state().holders == ()
    returned = lock.request(SHARED, "d")
    hold = returned.wait(0)
    assert hold is not None
    with pytest.raises(LockError):
        returned.withdraw()
    with pytest.raises(LockError):
        returned.wait(0)
    lock.release(hold)


def test_an_owner_that_asks_again_for_a_lock_is_refused_at_once() -> None:
    lock = ThreadRWLock("k")
    hold = lock.acquire(SHARED, 0, "call-1")
    assert hold is not None
    started = time.monotonic()
    with pytest.raises(LockError):
        lock.request(SHARED, "call-1")
    with pytest.raises(LockError):
        lock.acquire(EXCLUSIVE, 5, "call-1")
    assert time.monotonic() - started < 1
    blocker = lock.acquire(SHARED, 0, "call-2")
    assert blocker is not None
    waiting = lock.request(EXCLUSIVE, "call-3")
    with pytest.raises(LockError):  # also while it only waits
        lock.request(SHARED, "call-3")
    waiting.withdraw()
    lock.release(hold)
    lock.release(blocker)
    assert lock.acquire(SHARED, 0, "call-1") is not None  # after the release it may again


def test_owners_must_be_non_empty_strings() -> None:
    lock = ThreadRWLock("k")
    with pytest.raises(ValueError):
        lock.request(SHARED, "")
    with pytest.raises(TypeError):
        lock.request(SHARED, 3)  # type: ignore[arg-type]
    assert lock.state().waiters == ()


def test_timeouts_must_be_finite_numbers_of_at_least_zero() -> None:
    lock = ThreadRWLock("k")
    for bad in (math.inf, -math.inf, math.nan, -1):
        with pytest.raises(ValueError, match="finite"):
            lock.acquire(SHARED, bad, "a")
    for wrong_type in ("1", True, None):
        with pytest.raises(TypeError):
            lock.acquire(SHARED, wrong_type, "a")  # type: ignore[arg-type]
    assert lock.state().holders == () and lock.state().waiters == ()
    request = lock.request(SHARED, "b")
    with pytest.raises(ValueError):
        request.wait(math.inf)
    assert lock.state().holders == () and lock.state().waiters == ()


def test_a_hold_is_released_once_and_only_on_its_own_lock() -> None:
    lock, other = ThreadRWLock("k"), ThreadRWLock("other")
    hold = lock.acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    with pytest.raises(LockError):
        other.release(hold)
    lock.release(hold)
    with pytest.raises(LockError):
        lock.release(hold)


def test_any_thread_may_release_a_hold() -> None:
    lock = ThreadRWLock("k")
    hold = lock.acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    Background(lambda: lock.release(hold)).finish()
    assert lock.acquire(EXCLUSIVE, 0, "b") is not None


def test_the_snapshot_shows_holders_and_waiters_with_their_owners() -> None:
    space = ThreadLockSpace()
    held = space.lock("a").acquire(EXCLUSIVE, 0, "first")
    assert held is not None
    waiting = Background(lambda: space.lock("a").acquire(SHARED, 5, "second"))
    wait_until(lambda: waiters(space.lock("a")) == 1)
    assert space.lock("b") is space.lock("b")
    states = {state.key: state for state in space.snapshot()}
    assert states["a"].holders == (("first", EXCLUSIVE),)
    assert states["a"].waiters == (("second", SHARED),)
    assert states["b"].holders == () and states["b"].waiters == ()
    space.lock("a").release(held)
    waiting.finish()
    with pytest.raises(ValueError):
        space.lock("")


# Timeouts of the scheme


def test_the_timeout_configuration_refuses_unbounded_or_inconsistent_values() -> None:
    for bad in (
        {"command_max_s": math.inf},
        {"edit_default_s": math.nan},
        {"edit_max_s": 0},
        {"command_default_s": -1},
        {"command_default_s": 20, "command_max_s": 10},
        {"edit_default_s": 20, "edit_max_s": 10},
    ):
        with pytest.raises(ValueError):
            LockTimeouts(**bad)


def test_a_call_waits_the_default_of_its_type_and_at_most_the_maximum() -> None:
    timeouts = LockTimeouts(command_default_s=3, command_max_s=4, edit_default_s=1, edit_max_s=2)
    for locking in (RWLockingScheme(ThreadLockSpace(), timeouts), NoLockingScheme(timeouts)):
        with locking.run_command_in_worktree("wt") as lease:
            assert lease.timeout_s == 3
        with locking.edit_in_worktree("wt") as lease:
            assert lease.timeout_s == 1
        with locking.edit_outside_worktree() as lease:
            assert lease.timeout_s == 1
        with locking.run_command_in_worktree("wt", timeout=99) as lease:
            assert lease.timeout_s == 4
        with locking.edit_in_worktree("wt", timeout=99) as lease:
            assert lease.timeout_s == 2
        with locking.edit_outside_worktree(timeout=0.5) as lease:
            assert lease.timeout_s == pytest.approx(0.5)
        with locking.edit_outside_worktree(timeout=0) as lease:
            assert lease.timeout_s == 0


def test_a_call_refuses_an_unbounded_or_negative_timeout() -> None:
    for locking in (scheme(), NoLockingScheme()):
        for bad in (math.inf, math.nan, -0.1):
            with pytest.raises(ValueError):
                locking.run_command_in_worktree("wt", timeout=bad)
            with pytest.raises(ValueError):
                locking.edit_in_worktree("wt", timeout=bad)
            with pytest.raises(ValueError):
                locking.edit_outside_worktree(timeout=bad)


def test_worktree_ids_paths_owners_and_agents_are_checked() -> None:
    for locking in (scheme(), NoLockingScheme()):
        with pytest.raises(TypeError, match="collection"):
            locking.run_command_in_worktrees("wt")
        with pytest.raises(ValueError):
            locking.run_command_in_worktree("")
        with pytest.raises(ValueError):
            locking.edit_in_worktree("wt", path="")
        with pytest.raises(TypeError):
            locking.edit_in_worktree(3)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            locking.edit_outside_worktree(owner="")
        with pytest.raises(ValueError):
            locking.issue("")


# Which locks each operation takes


def test_each_operation_takes_its_locks_in_the_one_order() -> None:
    locking = scheme()
    command = Operation(OperationKind.COMMAND, "c", ("b", "a"))
    assert locking.plan(command) == (
        (worktree_key("a"), EXCLUSIVE),
        (worktree_key("b"), EXCLUSIVE),
        (GLOBAL_KEY, SHARED),
    )
    writing = Operation(OperationKind.COMMAND, "c", ("a",), writes_outside=True)
    assert locking.plan(writing) == ((worktree_key("a"), EXCLUSIVE), (GLOBAL_KEY, EXCLUSIVE))
    assert locking.plan(Operation(OperationKind.COMMAND, "c")) == ((GLOBAL_KEY, SHARED),)
    edit = Operation(OperationKind.EDIT, "e", ("a",), path="x.py")
    assert locking.plan(edit) == ((worktree_key("a"), SHARED), (file_key("a", "x.py"), EXCLUSIVE))
    assert locking.plan(Operation(OperationKind.EDIT, "e", ("a",))) == (
        (worktree_key("a"), SHARED),
    )
    assert locking.plan(Operation(OperationKind.EDIT_OUTSIDE, "o")) == ((GLOBAL_KEY, EXCLUSIVE),)


def test_a_command_names_each_worktree_once_and_in_order() -> None:
    locking = scheme()
    with locking.run_command_in_worktrees(["b", "a", "b"], owner="cmd") as lease:
        assert lease.operation.worktrees == ("a", "b")
        assert lease.locks == (
            (worktree_key("a"), EXCLUSIVE),
            (worktree_key("b"), EXCLUSIVE),
            (GLOBAL_KEY, SHARED),
        )
        assert lease.operation.owner == "cmd"


def test_file_keys_of_different_worktrees_and_paths_never_collide() -> None:
    assert file_key("a:b", "c") != file_key("a", "b:c")


# What waits for what


def test_an_edit_waits_for_a_command_in_its_worktree_but_not_in_another() -> None:
    locking = scheme()
    command = locking.run_command_in_worktree("wt1")
    with pytest.raises(LockTimeoutError):
        locking.edit_in_worktree("wt1", timeout=BLOCKED)
    locking.edit_in_worktree("wt2", timeout=0).release()
    waiting = Background(lambda: locking.edit_in_worktree("wt1", timeout=5))
    wait_until(lambda: waiters(space_of(locking).lock(worktree_key("wt1"))) == 1)
    command.release()
    lease_of(waiting).release()


def test_a_command_waits_for_an_edit_in_its_worktree() -> None:
    locking = scheme()
    edit = locking.edit_in_worktree("wt1")
    with pytest.raises(LockTimeoutError) as raised:
        locking.run_command_in_worktree("wt1", timeout=BLOCKED)
    assert raised.value.key == worktree_key("wt1")
    assert raised.value.mode is EXCLUSIVE
    locking.run_command_in_worktree("wt2", timeout=0).release()
    edit.release()
    locking.run_command_in_worktree("wt1", timeout=0).release()


def test_edits_of_one_worktree_coexist_except_on_one_file() -> None:
    locking = scheme()
    first = locking.edit_in_worktree("wt", path="a.py")
    second = locking.edit_in_worktree("wt", path="b.py", timeout=0)
    unnamed = locking.edit_in_worktree("wt", timeout=0)
    with pytest.raises(LockTimeoutError) as raised:
        locking.edit_in_worktree("wt", path="a.py", timeout=BLOCKED)
    assert raised.value.key == file_key("wt", "a.py")
    for lease in (first, second, unnamed):
        lease.release()


def test_two_commands_never_run_in_one_worktree_at_once() -> None:
    locking = scheme()
    first = locking.run_command_in_worktree("wt1")
    with pytest.raises(LockTimeoutError):
        locking.run_command_in_worktree("wt1", timeout=BLOCKED)
    with pytest.raises(LockTimeoutError):
        locking.run_command_in_worktrees(["wt2", "wt1"], timeout=BLOCKED)
    locking.run_command_in_worktree("wt2", timeout=0).release()
    first.release()


def test_an_edit_outside_waits_for_every_command_and_keeps_new_commands_out() -> None:
    locking = scheme()
    command = locking.run_command_in_worktree("wt1")
    with pytest.raises(LockTimeoutError):
        locking.edit_outside_worktree(timeout=BLOCKED)
    waiting = Background(lambda: locking.edit_outside_worktree(timeout=5, owner="outside"))
    wait_until(lambda: waiters(space_of(locking).lock(GLOBAL_KEY)) == 1)
    # A command in another worktree queues behind the waiting edit (FIFO).
    with pytest.raises(LockTimeoutError):
        locking.run_command_in_worktree("wt2", timeout=BLOCKED)
    # Edits inside the worktrees are not affected.
    locking.edit_in_worktree("wt2", timeout=0).release()
    command.release()
    outside = lease_of(waiting)
    with pytest.raises(LockTimeoutError):
        locking.run_command_in_worktree("wt2", timeout=BLOCKED)
    outside.release()
    locking.run_command_in_worktree("wt2", timeout=0).release()


def test_a_command_that_writes_outside_excludes_every_other_command() -> None:
    locking = scheme()
    writer = locking.run_command_in_worktree("wt1", writes_outside=True)
    with pytest.raises(LockTimeoutError):
        locking.run_command_in_worktree("wt2", timeout=BLOCKED)
    with pytest.raises(LockTimeoutError):
        locking.edit_outside_worktree(timeout=BLOCKED)
    locking.edit_in_worktree("wt2", timeout=0).release()
    writer.release()
    locking.run_command_in_worktree("wt2", timeout=0).release()


def test_an_operation_that_timed_out_keeps_none_of_its_locks() -> None:
    locking = scheme()
    outside = locking.edit_outside_worktree(owner="outside")
    # The command gets its worktree and then waits for the global lock, held by the edit.
    with pytest.raises(LockTimeoutError) as raised:
        locking.run_command_in_worktree("wt1", timeout=BLOCKED, owner="command")
    assert raised.value.key == GLOBAL_KEY
    assert raised.value.waited_s >= BLOCKED
    assert holders(locking, worktree_key("wt1")) == ()
    locking.edit_in_worktree("wt1", timeout=0, owner="edit").release()
    outside.release()
    locking.run_command_in_worktree("wt1", timeout=0, owner="command").release()


def test_a_lease_is_released_once_and_at_the_end_of_a_with_block() -> None:
    locking = scheme()
    with locking.run_command_in_worktree("wt") as lease:
        assert not lease.released
    assert lease.released
    lease.release()
    other = locking.run_command_in_worktree("wt", timeout=0)
    Background(other.release).finish()
    other.release()
    assert holders(locking, worktree_key("wt")) == ()


def test_the_lock_space_is_left_empty_after_every_lease_ended() -> None:
    locking = scheme()
    leases = [
        locking.run_command_in_worktree("wt1"),
        locking.edit_in_worktree("wt2", path="x"),
        locking.edit_in_worktree("wt2", path="y"),
    ]
    for lease in leases:
        lease.release()
    for state in space_of(locking).snapshot():
        assert state.holders == () and state.waiters == ()


def test_opposite_orders_of_worktrees_do_not_deadlock() -> None:
    """Two threads ask for the same two worktrees in opposite orders, 200 times; the scheme
    takes them in one order, so both always finish (a deadlock would raise LockTimeoutError)."""
    locking = scheme()
    start = threading.Barrier(2)

    def run(worktrees: list[str], writes_outside: bool) -> None:
        for _ in range(200):
            start.wait(5)
            with locking.run_command_in_worktrees(worktrees, writes_outside=writes_outside):
                time.sleep(0)

    one = Background(lambda: run(["a", "b"], writes_outside=False))
    two = Background(lambda: run(["b", "a"], writes_outside=True))
    one.finish(30)
    two.finish(30)
    for state in space_of(locking).snapshot():
        assert state.holders == () and state.waiters == ()


# Owners and the issue order of an agent's calls (the owner's rules 2-4)


def test_an_edit_a_command_and_an_edit_of_one_agent_run_in_issue_order() -> None:
    """The owner's example: one agent issues an edit, a command and an edit in one worktree, in
    that order and in parallel; their checks finish in reverse order. The command waits for the
    first edit, and the second edit waits for both."""
    locking = scheme()
    first, second, third = (locking.issue("agent") for _ in range(3))
    granted: list[str] = []
    done = {name: threading.Event() for name in ("edit 1", "command", "edit 2")}

    def call(name: str, take: Callable[[], Lease]) -> None:
        lease = take()
        granted.append(name)
        done[name].wait(5)
        lease.release()

    third_call = Background(
        lambda: call("edit 2", lambda: locking.edit_in_worktree("wt", ticket=third, owner="e2"))
    )
    second_call = Background(
        lambda: call(
            "command", lambda: locking.run_command_in_worktree("wt", ticket=second, owner="c")
        )
    )
    time.sleep(BLOCKED)
    assert granted == [] and queue_of(locking, worktree_key("wt")) == []
    first_call = Background(
        lambda: call("edit 1", lambda: locking.edit_in_worktree("wt", ticket=first, owner="e1"))
    )
    wait_until(lambda: granted == ["edit 1"])
    # The command waits for the worktree; it enters its agent's order only when it queues for
    # its last lock (the global one), so the second edit waits for its turn meanwhile.
    wait_until(lambda: queue_of(locking, worktree_key("wt")) == ["c"])
    assert third.state is TicketState.ISSUED
    done["edit 1"].set()
    wait_until(lambda: granted == ["edit 1", "command"])
    time.sleep(BLOCKED)
    assert granted == ["edit 1", "command"]  # the second edit waits for the command
    done["command"].set()
    wait_until(lambda: granted == ["edit 1", "command", "edit 2"])
    done["edit 2"].set()
    for background in (first_call, second_call, third_call):
        background.finish()


def test_a_call_whose_check_finished_first_waits_for_the_earlier_call_of_its_agent() -> None:
    locking = scheme()
    first, second = locking.issue("agent"), locking.issue("agent")
    later = Background(lambda: locking.edit_in_worktree("wt2", ticket=second, owner="later"))
    time.sleep(BLOCKED)
    assert later.running and second.state is TicketState.ISSUED
    with locking.run_command_in_worktree("wt1", ticket=first, owner="earlier"):
        assert first.state is TicketState.ENTERED
        lease_of(later).release()
    assert second.state is TicketState.ENTERED


def test_a_withdrawn_call_lets_the_later_calls_of_its_agent_in() -> None:
    locking = scheme()
    first, second, third = (locking.issue("agent") for _ in range(3))
    waiting = Background(lambda: locking.edit_in_worktree("wt", ticket=third, owner="third"))
    time.sleep(BLOCKED)
    assert waiting.running
    second.withdraw()  # e.g. it takes no lock
    time.sleep(BLOCKED)
    assert waiting.running  # still waits for the first
    first.withdraw()  # e.g. its check was denied
    lease_of(waiting).release()
    assert (first.state, second.state, third.state) == (
        TicketState.WITHDRAWN,
        TicketState.WITHDRAWN,
        TicketState.ENTERED,
    )


def test_a_call_that_timed_out_waiting_for_its_turn_gives_up_its_turn() -> None:
    locking = scheme()
    first, second, third = (locking.issue("agent") for _ in range(3))
    with pytest.raises(LockTimeoutError) as raised:
        locking.edit_in_worktree("wt", ticket=second, timeout=BLOCKED, owner="second")
    assert raised.value.key == "order:agent" and raised.value.mode is None
    assert second.state is TicketState.WITHDRAWN
    waiting = Background(lambda: locking.edit_in_worktree("wt", ticket=third, owner="third"))
    time.sleep(BLOCKED)
    assert waiting.running  # waits for the first, not for the second
    first.withdraw()
    lease_of(waiting).release()


def test_a_call_that_failed_on_its_locks_after_entering_keeps_the_order_moving() -> None:
    locking = scheme()
    blocker = locking.run_command_in_worktree("wt", owner="other agent's command")
    first, second = locking.issue("agent"), locking.issue("agent")
    with pytest.raises(LockTimeoutError):
        locking.edit_in_worktree("wt", ticket=first, timeout=BLOCKED, owner="first")
    assert first.state is TicketState.ENTERED  # it was queued, then gave up
    locking.edit_in_worktree("wt2", ticket=second, timeout=0, owner="second").release()
    blocker.release()


def test_calls_of_different_agents_do_not_wait_for_each_others_order() -> None:
    locking = scheme()
    held_back = locking.issue("a")
    with locking.edit_in_worktree("wt", ticket=locking.issue("b"), timeout=0):
        assert held_back.state is TicketState.ISSUED


def test_a_ticket_is_used_once_and_only_by_its_scheme() -> None:
    locking = scheme()
    ticket = locking.issue("agent")
    locking.edit_in_worktree("wt", ticket=ticket).release()
    with pytest.raises(LockError):
        locking.edit_in_worktree("wt", ticket=ticket)
    withdrawn = locking.issue("agent")
    withdrawn.withdraw()
    with pytest.raises(LockError):
        locking.edit_in_worktree("wt", ticket=withdrawn)
    with pytest.raises(ValueError):
        scheme().edit_in_worktree("wt", ticket=locking.issue("agent"))


def test_an_owner_with_a_lease_is_refused_at_once() -> None:
    locking = scheme()
    lease = locking.edit_in_worktree("wt", owner="call-1")
    later = locking.issue("agent")
    started = time.monotonic()
    with pytest.raises(LockError):
        locking.run_command_in_worktree("wt", owner="call-1", timeout=5, ticket=later)
    assert time.monotonic() - started < 1  # refused, not waited for
    assert later.state is TicketState.WITHDRAWN  # the agent's next calls need not wait for it
    lease.release()
    locking.run_command_in_worktree("wt", owner="call-1", timeout=0).release()


def test_two_calls_of_one_agent_wait_for_each_other_like_calls_of_two_agents() -> None:
    locking = scheme()
    first, second = locking.issue("agent"), locking.issue("agent")
    command = locking.run_command_in_worktree("wt", ticket=first, owner="command")
    with pytest.raises(LockTimeoutError):
        locking.edit_in_worktree("wt", ticket=second, timeout=BLOCKED, owner="edit")
    command.release()


def test_a_release_from_another_thread_lets_the_next_call_of_the_agent_in() -> None:
    locking = scheme()
    first, second = locking.issue("agent"), locking.issue("agent")
    command = locking.run_command_in_worktree("wt", ticket=first, owner="command")
    edit = Background(lambda: locking.edit_in_worktree("wt", ticket=second, owner="edit"))
    wait_until(lambda: queue_of(locking, worktree_key("wt")) == ["edit"])
    Background(command.release).finish()
    lease_of(edit).release()


def test_the_fake_scheme_never_waits_and_keeps_the_owner_rule() -> None:
    fake: LockingScheme = NoLockingScheme(LockTimeouts(1, 2, 1, 2))
    held_back = fake.issue("agent")
    later = fake.issue("agent")
    leases = [
        fake.edit_in_worktree("wt", ticket=later, timeout=0, owner="e"),  # no wait for held_back
        fake.run_command_in_worktree("wt"),
        fake.run_command_in_worktree("wt", writes_outside=True),
        fake.edit_in_worktree("wt", path="a.py"),
        fake.edit_in_worktree("wt", path="a.py"),
        fake.edit_outside_worktree(timeout=0),
    ]
    assert later.state is TicketState.ENTERED and held_back.state is TicketState.ISSUED
    with pytest.raises(LockError):
        fake.edit_in_worktree("wt", owner="e")
    for lease in leases:
        assert lease.locks == () and lease.waited_s == 0
        lease.release()
        lease.release()


# The review of 2026-11-25: tickets on every failure path, cancellation, limits


def test_withdrawing_a_ticket_while_its_call_waits_for_its_turn_cancels_the_call() -> None:
    """Withdrawing the ticket of a call that waits for its turn ends that call at once with
    LockCancelledError; nothing freezes, and the order goes on for every agent."""
    locking = scheme()
    first, second = locking.issue("agent"), locking.issue("agent")
    waiting = Background(lambda: locking.edit_in_worktree("wt", ticket=second, owner="second"))
    time.sleep(BLOCKED)
    assert waiting.running
    started = time.monotonic()
    second.withdraw()
    with pytest.raises(LockCancelledError):
        waiting.finish(2)
    assert time.monotonic() - started < 1
    other = Background(lambda: locking.issue("other agent"))
    other.finish(2)
    first.withdraw()
    assert locking.open_tickets() == {"other agent": (1,)}
    assert locking.live_owners() == frozenset()


@pytest.mark.parametrize(
    "bad_call",
    [
        lambda s, t: s.edit_in_worktree("wt", ticket=t, timeout=-1),
        lambda s, t: s.edit_in_worktree("wt", ticket=t, timeout=math.nan),
        lambda s, t: s.edit_in_worktree("wt", ticket=t, path=""),
        lambda s, t: s.edit_outside_worktree(ticket=t, owner=""),
        lambda s, t: s.run_command_in_worktrees("wt", ticket=t),
        lambda s, t: s.run_command_in_worktree("wt", ticket=t, owner=GENERATED_OWNER_PREFIX + "x"),
    ],
)
def test_a_call_that_fails_on_its_arguments_withdraws_its_ticket(
    bad_call: Callable[[RWLockingScheme, object], Lease],
) -> None:
    locking = scheme()
    ticket, next_ticket = locking.issue("agent"), locking.issue("agent")
    with pytest.raises((TypeError, ValueError)):
        bad_call(locking, ticket)
    assert ticket.state is TicketState.WITHDRAWN
    locking.edit_in_worktree("wt-free", ticket=next_ticket, timeout=0).release()


def test_one_ticket_serves_one_lock_call_at_a_time() -> None:
    locking = scheme()
    blocker = locking.edit_outside_worktree(owner="other agent's edit")
    ticket = locking.issue("agent")
    first_use = Background(
        lambda: locking.run_command_in_worktree("wt", ticket=ticket, owner="first use")
    )
    wait_until(lambda: queue_of(locking, GLOBAL_KEY) == ["first use"])
    started = time.monotonic()
    with pytest.raises(LockError):
        locking.edit_in_worktree("wt2", ticket=ticket, owner="second use")
    assert time.monotonic() - started < 1
    blocker.release()
    lease_of(first_use).release()
    assert locking.live_owners() == frozenset() and locking.open_tickets() == {}


def test_withdrawing_the_ticket_of_a_call_that_waits_for_a_lock_cancels_it() -> None:
    """The reviewer's case: the call waits for its first lock, its ticket is withdrawn, and
    the agent's next call goes ahead; the cancelled call must never get its lease."""
    locking = scheme()
    blocker = locking.run_command_in_worktree("wt", owner="other agent's command")
    first, second = locking.issue("A"), locking.issue("A")
    command = Background(lambda: locking.run_command_in_worktree("wt", ticket=first, owner="A.1"))
    wait_until(lambda: queue_of(locking, worktree_key("wt")) == ["A.1"])
    assert first.state is TicketState.ISSUED  # its last lock, the global one, is not queued yet
    first.withdraw()
    with pytest.raises(LockCancelledError):
        command.finish(2)
    edit = locking.edit_in_worktree("wt2", ticket=second, timeout=0, owner="A.2")
    assert queue_of(locking, worktree_key("wt")) == []
    edit.release()
    blocker.release()
    for state in space_of(locking).snapshot():
        assert state.holders == () and state.waiters == ()
    assert locking.live_owners() == frozenset()


def test_withdrawing_the_ticket_of_an_entered_call_cancels_its_wait_for_the_last_lock() -> None:
    locking = scheme()
    blocker = locking.run_command_in_worktree("wt", owner="other agent's command")
    ticket = locking.issue("agent")
    edit = Background(lambda: locking.edit_in_worktree("wt", ticket=ticket, owner="edit"))
    wait_until(lambda: queue_of(locking, worktree_key("wt")) == ["edit"])
    assert ticket.state is TicketState.ENTERED
    ticket.withdraw()
    with pytest.raises(LockCancelledError):
        edit.finish(2)
    assert queue_of(locking, worktree_key("wt")) == []
    blocker.release()
    assert locking.live_owners() == frozenset()


def test_withdrawing_a_ticket_after_its_call_got_the_lease_does_nothing() -> None:
    locking = scheme()
    ticket = locking.issue("agent")
    lease = locking.run_command_in_worktree("wt", ticket=ticket, owner="command")
    ticket.withdraw()
    assert ticket.state is TicketState.ENTERED and not lease.released
    assert holders(locking, worktree_key("wt")) == (("command", EXCLUSIVE),)
    lease.release()


def test_a_ticket_used_as_a_context_manager_is_withdrawn_unless_its_call_got_a_lease() -> None:
    locking = scheme()
    with locking.issue("agent") as unused:
        pass  # e.g. the check denied the call
    assert unused.state is TicketState.WITHDRAWN
    with locking.issue("agent") as used:
        lease = locking.edit_in_worktree("wt", ticket=used)
    assert used.state is TicketState.ENTERED and not lease.released
    lease.release()
    assert locking.open_tickets() == {}


def test_huge_timeouts_are_refused_not_overflowed() -> None:
    for huge in (10**400, 1e300, threading.TIMEOUT_MAX * 2):
        with pytest.raises(ValueError):
            checked_timeout(huge)
        with pytest.raises(ValueError):
            LockTimeouts(command_default_s=1, command_max_s=huge)
    assert checked_timeout(threading.TIMEOUT_MAX) == threading.TIMEOUT_MAX


def test_generated_owners_never_collide_with_the_callers_owners() -> None:
    locking = scheme()
    with locking.run_command_in_worktree("wt1", owner="command-1"):
        with locking.run_command_in_worktree("wt2") as generated:
            assert generated.operation.owner.startswith(GENERATED_OWNER_PREFIX)
    with pytest.raises(ValueError):
        locking.edit_outside_worktree(owner=GENERATED_OWNER_PREFIX + "command-1")


def test_the_scheme_reports_live_owners_and_open_tickets() -> None:
    locking = scheme()
    ticket = locking.issue("agent")
    assert locking.open_tickets() == {"agent": (1,)}
    with locking.edit_in_worktree("wt", ticket=ticket, owner="edit"):
        assert locking.live_owners() == frozenset({"edit"})
        assert locking.open_tickets() == {}
    assert locking.live_owners() == frozenset()


def test_an_edit_outside_waits_only_for_the_commands_that_run() -> None:
    """Why the global lock comes last (the owner's choice of 2026-11-25): a command that waits
    for a busy worktree holds nothing, so an edit outside waits for the running command only,
    not also for the command queued behind it."""
    locking = scheme()
    running = locking.run_command_in_worktree("wt1", owner="running")
    queued = Background(lambda: locking.run_command_in_worktree("wt1", owner="queued"))
    wait_until(lambda: queue_of(locking, worktree_key("wt1")) == ["queued"])
    assert holders(locking, GLOBAL_KEY) == (("running", SHARED),)
    outside = Background(lambda: locking.edit_outside_worktree(owner="outside"))
    wait_until(lambda: queue_of(locking, GLOBAL_KEY) == ["outside"])
    running.release()
    # The edit gets the global lock before the queued command, which now holds its worktree
    # and waits for the global lock behind the edit.
    edit = lease_of(outside)
    wait_until(lambda: queue_of(locking, GLOBAL_KEY) == ["queued"])
    assert holders(locking, worktree_key("wt1")) == (("queued", EXCLUSIVE),)
    edit.release()
    lease_of(queued).release()
