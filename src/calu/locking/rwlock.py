"""The generic shared/exclusive lock that a locking scheme is built on (agent/records/PLAN.md D274).

An `RWLock` is one lock with two modes. SHARED holds coexist with each other; an EXCLUSIVE hold
excludes every other hold. A `LockSpace` hands out the lock for a key. A backend implements
both: `calu.locking.threadlocks` for the threads of one process now; backends on files or on
PostgreSQL may follow. Every backend must keep this contract (the owner's decisions of
2026-11-25):

- **FIFO.** A request takes its place in the lock's queue when it is made (`RWLock.request`),
  and requests are granted in that order. The shared requests at the front of the queue are
  granted together. A waiting exclusive request keeps later shared requests out, so neither
  commands nor edits starve.
- **Owners.** Every request names its owner: one operation (one tool call), not a thread and
  not an agent. Two operations of one agent are different owners and wait for each other like
  operations of different agents.
- **No reentrancy.** An owner never asks for a lock it holds or already waits for. Such a
  request is a bug and is refused at once with `LockError`.
- **Bounded waits.** `LockRequest.wait` waits at most the timeout it is given and returns a
  `Hold`, or None when the time ran out; the request then no longer waits and nothing is held.
  When the wait itself raises (an invalid timeout, an error of the backend), the request is
  withdrawn first.
- **Release from anywhere.** A hold is released by passing it to `RWLock.release`, from any
  thread. `LockRequest.withdraw` wakes a `wait` blocked in another thread.
- **Generations.** Every lock has a generation: a number that starts at 0 and never goes down.
  `RWLock.release(hold, changed=True)` advances it. The holder says with it that what the lock
  guards may have changed. The number advances in the same step as the release, before the
  next request is granted. `LockSpace.generations` reads the numbers. A caller uses them like
  this: it remembers a generation, takes the lock later, and reads the number again. The same
  number means that no holder in between reported a change. A backend may advance a
  generation more often than holders ask for, never less often.

Limit: call the lock API from worker threads, never from the main thread of a program that
takes Ctrl-C. An asynchronous exception (KeyboardInterrupt, a signal handler that raises) can
strike between any two statements, also inside `with mutex:`, and can leave a lock held or a
mutex taken. No try/except can rule that out in CPython.

Notes for the backends to come. The owner's notes of 2026-11-25 (the danger is reentrancy by
accident):

- PostgreSQL advisory locks are reentrant per database session, so each lease needs a session
  of its own. They already queue in a FIFO-like way: a new request waits behind a conflicting
  waiting one.
- On files, use flock or OFD locks (F_OFD_SETLK), which belong to an open file description;
  never classic POSIX fcntl locks, which belong to the whole process. Linux flock does not
  queue fairly, so a file backend needs a turnstile lock to meet the FIFO rule.

From the review of 2026-11-25:

- `request` must have fixed the place in the queue when it returns: the scheme lets the
  agent's next call go ahead at that moment. A PostgreSQL lock call that is merely sent does not
  say when the server queued it, so that backend must confirm the queued request (e.g. its
  waiting row in `pg_locks`) before `request` returns. flock has no queue at all; the turnstile
  provides it and must survive a waiter that died.
- `withdraw` must wake a `wait` blocked in another thread: on PostgreSQL a query cancel (and a
  grant that arrives just before it must be handed back); on files no blocking `flock` call, but
  `LOCK_NB` tries in the order the turnstile decides.
- A PostgreSQL unlock must run on the session that holds the lock, so that backend keeps one
  session per hold (per owner) and releases on it, whatever thread calls `release`.
- Keys are strings, advisory lock ids 64-bit integers. The mapping must be free of collisions:
  two keys of one plan on one id would break the lock order and re-enter a reentrant lock.
- An error a backend raises in `withdraw` during a clean-up replaces the original exception.
"""

import enum
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


class LockMode(enum.Enum):
    """The two modes of an `RWLock`."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"

    def conflicts_with(self, other: LockMode) -> bool:
        """Tell whether a hold in this mode and a hold in `other` cannot coexist on one lock.

        Args:
            other: The mode of the other hold.

        Returns:
            False when both modes are SHARED, else True.
        """
        return self is LockMode.EXCLUSIVE or other is LockMode.EXCLUSIVE


class LockError(RuntimeError):
    """A lock was used against its contract; it points to a bug in the caller.

    Examples: an owner asked for a lock a second time, a hold was released twice, a ticket was
    used twice.
    """


@dataclass(frozen=True, slots=True)
class Hold:
    """Proof that one request was granted; `RWLock.release` takes it back.

    Attributes:
        key: The key of the lock that granted the hold.
        mode: The mode it was granted in.
        owner: The operation that holds it.
        token: The backend's identifier of this grant, unique per lock while it is held.
    """

    key: str
    mode: LockMode
    owner: str
    token: int


@dataclass(frozen=True, slots=True)
class LockState:
    """The holders and the waiting requests of one lock at one moment, for diagnostics.

    Attributes:
        key: The lock's key.
        holders: (owner, mode) of every grant, also of grants whose `wait` has not returned yet.
        waiters: (owner, mode) of every waiting request, the first in line first.
    """

    key: str
    holders: tuple[tuple[str, LockMode], ...]
    waiters: tuple[tuple[str, LockMode], ...]


def checked_timeout(timeout: float) -> float:
    """Check that `timeout` is a finite number of seconds >= 0 and return it as a float.

    Args:
        timeout: Seconds to wait. 0 means "only if the request is granted at once".

    Returns:
        The timeout as a float.

    Raises:
        TypeError: `timeout` is not an int or a float (a bool counts as neither).
        ValueError: `timeout` is negative, NaN, infinite, or above `threading.TIMEOUT_MAX`
            (about 292 years): no lock wait is unbounded.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError(f"a lock timeout must be a number of seconds, not {timeout!r}")
    try:
        seconds = float(timeout)
    except OverflowError:
        seconds = math.inf
    if not math.isfinite(seconds) or not 0 <= seconds <= threading.TIMEOUT_MAX:
        raise ValueError(
            f"a lock timeout must be finite and from 0 to {threading.TIMEOUT_MAX:g} seconds, "
            f"not {timeout!r}"
        )
    return seconds


def checked_owner(owner: str) -> str:
    """Check that `owner` names an operation: a non-empty string.

    Args:
        owner: The owner's label, e.g. a tool call's id.

    Returns:
        `owner`.

    Raises:
        TypeError: `owner` is not a string.
        ValueError: `owner` is empty.
    """
    if not isinstance(owner, str):
        raise TypeError(f"a lock owner must be a string, not {owner!r}")
    if not owner:
        raise ValueError("a lock owner must not be empty")
    return owner


class LockRequest(ABC):
    """One request in a lock's queue; its place in the queue was fixed when it was made.

    Attributes:
        key: The key of the lock.
        mode: The requested mode.
        owner: The operation that asked.
    """

    key: str
    mode: LockMode
    owner: str

    @abstractmethod
    def wait(self, timeout: float) -> Hold | None:
        """Wait at most `timeout` seconds for the grant; block the calling thread meanwhile.

        Called at most once per request. A grant that comes at the moment the time runs out
        counts. When the timeout is invalid, or the wait itself raises, the request is withdrawn
        (a grant given back) before the exception propagates.

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

    @abstractmethod
    def withdraw(self) -> None:
        """Take the request back without waiting for it.

        The request leaves the queue, or, when it was granted but `wait` has not returned the
        hold yet, the grant is given back; a thread blocked in `wait` then gets None. Does
        nothing when the request is already gone (timed out, withdrawn, or its hold released).

        Raises:
            LockError: `wait` returned the hold and it is still held; `RWLock.release` gives it
                back.
        """


class RWLock(ABC):
    """One shared/exclusive lock with the contract of the module docstring.

    Attributes:
        key: The key the lock space knows the lock by.
    """

    key: str

    @abstractmethod
    def request(self, mode: LockMode, owner: str) -> LockRequest:
        """Put a request for `mode` into the queue at once, without waiting, and return it.

        Its place in the queue is fixed now; it may already be granted when this returns.

        Args:
            mode: SHARED or EXCLUSIVE.
            owner: The operation that asks (`checked_owner`), e.g. a tool call's id.

        Returns:
            The request; its `wait` gives the hold.

        Raises:
            LockError: `owner` already holds this lock or waits for it.
            TypeError: `owner` is not a string.
            ValueError: `owner` is empty.
        """

    @abstractmethod
    def release(self, hold: Hold, changed: bool = False) -> None:
        """Give back `hold` and grant the waiting requests that fit now.

        Args:
            hold: A hold that this lock granted, that `wait` returned and that was not
                released yet.
            changed: True when what the lock guards may have changed while the hold was held.
                The lock's generation then advances before the next request is granted.

        Raises:
            LockError: `hold` is not a current hold of this lock.
        """

    def acquire(self, mode: LockMode, timeout: float, owner: str) -> Hold | None:
        """Request `mode` and wait at most `timeout` seconds for it: `request` and `wait` in one.

        Args:
            mode: SHARED or EXCLUSIVE.
            timeout: Seconds to wait, finite and >= 0.
            owner: The operation that asks.

        Returns:
            The hold, or None when the time ran out (nothing is held then).

        Raises:
            LockError: `owner` already holds this lock or waits for it.
            TypeError: `timeout` is not a number, or `owner` is not a string.
            ValueError: `timeout` is negative, NaN or infinite, or `owner` is empty.
        """
        checked_timeout(timeout)
        return self.request(mode, owner).wait(timeout)


class LockSpace(ABC):
    """The locks of one locking scheme, one per key; a backend implements it."""

    @abstractmethod
    def lock(self, key: str) -> RWLock:
        """Return the lock for `key`, created free on first use.

        Args:
            key: A non-empty string; equal keys give the same lock.

        Returns:
            The lock.

        Raises:
            ValueError: `key` is empty.
        """

    @abstractmethod
    def generations(self, keys: Iterable[str]) -> dict[str, int]:
        """Return the generation of each lock in `keys` (see the module docstring).

        Args:
            keys: Lock keys. A key whose lock was never used has the generation 0.

        Returns:
            Key -> generation, one entry per key.
        """

    def snapshot(self) -> Sequence[LockState] | None:
        """Return the state of every lock at one moment, or None when the backend cannot.

        This default returns None. A backend that overrides it returns states that were all
        true at the same moment, so that a cycle among them is a real deadlock.

        Returns:
            One state per lock that exists, or None.
        """
        return None
