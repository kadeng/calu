"""The `RWLock` backend for the threads of one process (agent/records/PLAN.md D274).

The tests and the fuzzing harness (`tests/fuzz/locking_harness.py`) use it. Each lock keeps
its requests in a FIFO queue, and each request waits on its own `threading.Event`, so a release
wakes only the requests it grants. A hold is a token, not a thread, so any thread may release
it. Nothing here touches files, processes or the network.
"""

import enum
import threading
from collections import deque
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field

from calu.locking.rwlock import (
    Hold,
    LockError,
    LockMode,
    LockRequest,
    LockSpace,
    LockState,
    RWLock,
    checked_owner,
    checked_timeout,
)


class _Phase(enum.Enum):
    """Where a request is in its life."""

    QUEUED = "queued"  # waits in the queue
    GRANTED = "granted"  # granted; `wait` has not returned the hold yet
    HELD = "held"  # `wait` returned the hold; `release` ends it
    GONE = "gone"  # timed out, withdrawn or released


@dataclass(eq=False)
class _Entry:
    """The lock's record of one request; changed only under the lock's mutex."""

    mode: LockMode
    owner: str
    granted: threading.Event = field(default_factory=threading.Event)
    token: int = 0
    phase: _Phase = _Phase.QUEUED
    waited: bool = False


class ThreadLockRequest(LockRequest):
    """A request in the queue of a `ThreadRWLock`, with the contract of `LockRequest`."""

    def __init__(self, lock: ThreadRWLock, entry: _Entry) -> None:
        """Wrap the lock's record of the request.

        Args:
            lock: The lock whose queue holds the request.
            entry: The lock's record of it.
        """
        self.key = lock.key
        self.mode = entry.mode
        self.owner = entry.owner
        self._lock = lock
        self._entry = entry

    def wait(self, timeout: float) -> Hold | None:
        """Wait at most `timeout` seconds for the grant; block the calling thread meanwhile.

        Called at most once per request. A grant that comes at the moment the time runs out
        counts. When the timeout is invalid, or an exception interrupts the wait, the request is
        withdrawn (a grant given back) before the exception propagates.

        Args:
            timeout: Seconds to wait, finite and >= 0 (`checked_timeout`).

        Returns:
            The hold, or None when the time ran out or the request was withdrawn; the request
            then no longer waits and nothing is held.

        Raises:
            LockError: `wait` was called a second time.
            TypeError: `timeout` is not a number.
            ValueError: `timeout` is negative, NaN or infinite.
        """
        lock, entry = self._lock, self._entry
        with lock._mutex:
            if entry.waited:
                raise LockError(f"a request for the lock {lock.key!r} is waited for once")
            entry.waited = True
        try:
            seconds = checked_timeout(timeout)
            entry.granted.wait(seconds)
        except BaseException:
            self.withdraw()
            raise
        with lock._mutex:
            match entry.phase:
                case _Phase.GRANTED:
                    entry.phase = _Phase.HELD
                    return Hold(lock.key, entry.mode, entry.owner, entry.token)
                case _Phase.QUEUED:
                    lock._leave_queue(entry)
            return None

    def withdraw(self) -> None:
        """Take the request back without waiting for it.

        The request leaves the queue, or its grant is given back if `wait` has not returned it.
        Does nothing when the request is already gone; a thread blocked in `wait` then gets
        None.

        Raises:
            LockError: `wait` returned the hold already; `ThreadRWLock.release` gives it back.
        """
        lock, entry = self._lock, self._entry
        with lock._mutex:
            match entry.phase:
                case _Phase.QUEUED:
                    lock._leave_queue(entry)
                case _Phase.GRANTED:
                    lock._drop_grant(entry)
                case _Phase.HELD:
                    raise LockError(
                        f"the request of {entry.owner!r} for {lock.key!r} was granted; "
                        "release its hold instead"
                    )
                case _Phase.GONE:
                    return
            entry.granted.set()


class ThreadRWLock(RWLock):
    """An `RWLock` for the threads of one process, with the contract of `calu.locking.rwlock`."""

    def __init__(self, key: str) -> None:
        """Create a free lock.

        Args:
            key: The key the lock space knows the lock by.
        """
        self.key = key
        self._mutex = threading.Lock()
        self._queue: deque[_Entry] = deque()
        self._holders: dict[int, _Entry] = {}
        self._owners: set[str] = set()
        self._exclusive = False
        self._next_token = 1

    def request(self, mode: LockMode, owner: str) -> ThreadLockRequest:
        """Put a request for `mode` into the queue at once, without waiting, and return it.

        Its place in the queue is fixed now; it may already be granted when this returns.

        Args:
            mode: SHARED or EXCLUSIVE.
            owner: The operation that asks (`checked_owner`).

        Returns:
            The request; its `wait` gives the hold.

        Raises:
            LockError: `owner` already holds this lock or waits for it.
            TypeError: `owner` is not a string.
            ValueError: `owner` is empty.
        """
        checked_owner(owner)
        entry = _Entry(mode, owner)
        with self._mutex:
            if owner in self._owners:
                raise LockError(
                    f"{owner!r} already holds the lock {self.key!r} or waits for it; "
                    "an owner never takes a lock twice"
                )
            self._owners.add(owner)
            self._queue.append(entry)
            self._grant_waiting()
        return ThreadLockRequest(self, entry)

    def release(self, hold: Hold) -> None:
        """Give back `hold` and grant the waiting requests that fit now.

        Args:
            hold: A hold that this lock granted, that `wait` returned and that was not
                released yet.

        Raises:
            LockError: `hold` is not a current hold of this lock.
        """
        with self._mutex:
            entry = self._holders.get(hold.token) if hold.key == self.key else None
            if (
                entry is None
                or entry.phase is not _Phase.HELD
                or entry.mode is not hold.mode
                or entry.owner != hold.owner
            ):
                raise LockError(f"{hold!r} is not a current hold of the lock {self.key!r}")
            self._drop_grant(entry)

    def state(self) -> LockState:
        """Return the lock's holders and waiting requests now.

        Returns:
            The state, with the owners of the requests.
        """
        with self._mutex:
            return self._state()

    def _state(self) -> LockState:
        """Return the state; the caller holds `_mutex`.

        Returns:
            The state, with the owners of the requests.
        """
        return LockState(
            key=self.key,
            holders=tuple((entry.owner, entry.mode) for entry in self._holders.values()),
            waiters=tuple((entry.owner, entry.mode) for entry in self._queue),
        )

    def _grant_waiting(self) -> None:
        """Grant requests from the front of the queue while they fit; the caller holds `_mutex`.

        An exclusive request fits when nothing is held; a shared request when no exclusive
        hold exists. The first request that does not fit stops the granting, so requests behind
        it wait as well (FIFO). Each granted request gets a token and its event fires.
        """
        while self._queue:
            entry = self._queue[0]
            if entry.mode is LockMode.EXCLUSIVE:
                if self._holders:
                    return
                self._exclusive = True
            elif self._exclusive:
                return
            self._queue.popleft()
            entry.token = self._next_token
            self._next_token += 1
            entry.phase = _Phase.GRANTED
            self._holders[entry.token] = entry
            entry.granted.set()

    def _leave_queue(self, entry: _Entry) -> None:
        """Remove a request that was not granted; the caller holds `_mutex`.

        Leaving the queue may let the requests behind it in, so they are granted if they fit.

        Args:
            entry: A queued request of this lock.
        """
        self._queue.remove(entry)
        entry.phase = _Phase.GONE
        self._owners.discard(entry.owner)
        self._grant_waiting()

    def _drop_grant(self, entry: _Entry) -> None:
        """End a grant and grant the waiting requests that fit; the caller holds `_mutex`.

        The grant was released, or withdrawn before `wait` returned it.

        Args:
            entry: A granted request of this lock.
        """
        del self._holders[entry.token]
        entry.phase = _Phase.GONE
        self._owners.discard(entry.owner)
        if entry.mode is LockMode.EXCLUSIVE:
            self._exclusive = False
        self._grant_waiting()


class ThreadLockSpace(LockSpace):
    """The `ThreadRWLock`s of one process, created on first use and kept for the space's life."""

    def __init__(self) -> None:
        """Create a space without locks."""
        self._mutex = threading.Lock()
        self._locks: dict[str, ThreadRWLock] = {}

    def lock(self, key: str) -> ThreadRWLock:
        """Return the lock for `key`, created free on first use.

        Args:
            key: A non-empty string; equal keys give the same lock.

        Returns:
            The lock.

        Raises:
            ValueError: `key` is empty.
        """
        if not key:
            raise ValueError("a lock key must not be empty")
        with self._mutex:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = ThreadRWLock(key)
            return lock

    def snapshot(self) -> Sequence[LockState]:
        """Return the state of every lock, all taken at one moment.

        Holds the internal mutex of every lock (in key order) while it reads them, so that
        no grant or release happens in between; a cycle among the states is a real deadlock.

        Returns:
            One state per lock that exists, sorted by key.
        """
        with self._mutex:
            locks = sorted(self._locks.values(), key=lambda lock: lock.key)
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock._mutex)
            return [lock._state() for lock in locks]
