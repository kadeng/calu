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
  An exception that interrupts the wait leaves nothing held and nothing waiting either.
- **Release from anywhere.** A hold is released by passing it to `RWLock.release`, from any
  thread.

Notes for the backends to come (the owner, 2026-11-25). The danger is reentrancy by accident:

- PostgreSQL advisory locks are reentrant per database session, so each lease needs a session
  of its own. They already queue in a FIFO-like way: a new request waits behind a conflicting
  waiting one.
- On files, use flock or OFD locks (F_OFD_SETLK), which belong to an open file description;
  never classic POSIX fcntl locks, which belong to the whole process. Linux flock does not
  queue fairly, so a file backend needs a turnstile lock to meet the FIFO rule.
"""

import enum
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
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
        ValueError: `timeout` is negative, NaN or infinite: no lock wait is unbounded.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError(f"a lock timeout must be a number of seconds, not {timeout!r}")
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"a lock timeout must be finite and at least 0 seconds, not {timeout!r}")
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

    @abstractmethod
    def withdraw(self) -> None:
        """Take the request back without waiting for it.

        The request leaves the queue, or, when it was granted but `wait` has not returned the
        hold yet, the grant is given back. Does nothing when the request is already gone (timed
        out or withdrawn); a thread blocked in `wait` then gets None.

        Raises:
            LockError: `wait` returned the hold already; `RWLock.release` gives it back.
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
    def release(self, hold: Hold) -> None:
        """Give back `hold` and grant the waiting requests that fit now.

        Args:
            hold: A hold that this lock granted, that `wait` returned and that was not
                released yet.

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

    def snapshot(self) -> Sequence[LockState] | None:
        """Return the state of every lock at one moment, or None when the backend cannot.

        This default returns None. A backend that overrides it returns states that were all
        true at the same moment, so that a cycle among them is a real deadlock.

        Returns:
            One state per lock that exists, or None.
        """
        return None
