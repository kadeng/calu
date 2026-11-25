"""The `RWLock` backend for the processes of one user on one machine (locking.md §8).

The design: agent/records/notes/locking.md, section 8.

All locks of a space live in one JSON state file, `locks.json`, in the space's directory. A
change of the state works like this:

1. It takes a short `flock` on a separate file, `locks.mutex`. flock belongs to an open file
   description, and every change opens the file anew, so threads exclude each other as well as
   processes.
2. It reads the state, removes the entries of processes that no longer run, applies the change,
   and grants the waiting requests that fit (FIFO, the same rules as `ThreadRWLock`).
3. It writes the new state to a temporary file and renames it over `locks.json`, so a reader
   without the flock always sees a whole state.

A waiting request polls its entry, at first every millisecond and then up to every `POLL_MAX_S`.
A change made by its own process wakes it at once. Every `PRUNE_EVERY_S` it also checks the
entries of its lock, and when one belongs to a process that died, it removes them; so a process
killed while it holds a lock does not block the others for long.

A process is identified by its id, its start time and the boot id (Linux `/proc`). So a reused
process id, or a reboot, cannot keep a dead process's entries alive. Without `/proc` (not
Linux), only the process id is checked.

The holds of a process belong to it: `release` and `withdraw` work from any of its threads, not
from other processes. Create one `FileLockSpace` per process; an object inherited across a
`fork` must not be used in the child. The directory is created with mode 0o700. Every process
that uses it shares the locks; that is the point of the backend.
"""

import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

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

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - Calu runs on Linux (and later macOS)
    fcntl = None  # type: ignore[assignment]

STATE_FILE = "locks.json"
"""The state of every lock of the space."""

MUTEX_FILE = "locks.mutex"
"""The file whose flock guards every change of the state."""

POLL_MIN_S = 0.001
"""The first interval at which a waiting request looks at its entry."""

POLL_MAX_S = 0.02
"""The longest interval at which a waiting request looks at its entry."""

PRUNE_EVERY_S = 0.2
"""How often a waiting request looks for entries of processes that died on its lock."""

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _Process:
    """What identifies a running process across process-id reuse and reboots.

    Attributes:
        pid: The process id.
        start: Its start time (field 22 of `/proc/<pid>/stat`), or "" without `/proc`.
        boot: The boot id (`/proc/sys/kernel/random/boot_id`), or "" without `/proc`.
    """

    pid: int
    start: str
    boot: str


def _start_time(pid: int) -> str | None:
    """Read a process's start time from `/proc`.

    Args:
        pid: The process id.

    Returns:
        Field 22 of `/proc/<pid>/stat`, or None when the process does not exist (or `/proc`
        cannot be read).
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


@cache
def _boot_id() -> str:
    """Read the boot id of the running kernel.

    Returns:
        The boot id, or "" when it cannot be read (not Linux).
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


_me: _Process | None = None


def _this_process() -> _Process:
    """Return the identity of the calling process; computed again after a fork.

    Returns:
        The identity.
    """
    global _me
    pid = os.getpid()
    if _me is None or _me.pid != pid:
        _me = _Process(pid, _start_time(pid) or "", _boot_id())
    return _me


def _alive(entry: dict[str, Any]) -> bool:
    """Tell whether the process that made `entry` still runs.

    Args:
        entry: An entry of the state, with its process's `pid`, `start` and `boot`.

    Returns:
        False when the entry is from another boot, or its process ended (or its id now belongs
        to another process); True otherwise.
    """
    me = _this_process()
    if entry["boot"] != me.boot:
        return False
    if entry["pid"] == me.pid:
        return entry["start"] == me.start
    if me.boot:
        return _start_time(entry["pid"]) == entry["start"]
    try:  # without /proc: the process id alone
        os.kill(entry["pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _grant_waiting(entries: list[dict[str, Any]]) -> None:
    """Grant queued entries of one lock in FIFO order while they fit (the rules of `ThreadRWLock`).

    An exclusive entry fits when nothing is granted; a shared entry when no exclusive entry is
    granted. The first entry that does not fit stops the granting.

    Args:
        entries: The lock's entries in the order of their sequence numbers; changed in place.
    """
    granted = [entry for entry in entries if entry["granted"]]
    exclusive = any(entry["mode"] == LockMode.EXCLUSIVE.value for entry in granted)
    for entry in entries:
        if entry["granted"]:
            continue
        if entry["mode"] == LockMode.EXCLUSIVE.value:
            if granted:
                return
            exclusive = True
        elif exclusive:
            return
        entry["granted"] = True
        granted.append(entry)


def _find(state: dict[str, Any], key: str, seq: int) -> dict[str, Any] | None:
    """Return the entry with sequence number `seq` of the lock `key`.

    Args:
        state: The state.
        key: The lock's key.
        seq: The entry's sequence number.

    Returns:
        The entry, or None when it is not there (withdrawn, timed out, released or pruned).
    """
    for entry in state["locks"].get(key, ()):
        if entry["seq"] == seq:
            return entry
    return None


def _remove(state: dict[str, Any], key: str, seq: int) -> None:
    """Remove an entry and grant what fits now; drop the lock when it has no entries left.

    Args:
        state: The state; changed in place.
        key: The lock's key.
        seq: The entry's sequence number.
    """
    entries = [entry for entry in state["locks"].get(key, ()) if entry["seq"] != seq]
    if entries:
        _grant_waiting(entries)
        state["locks"][key] = entries
    else:
        state["locks"].pop(key, None)


class FileLockRequest(LockRequest):
    """A request in the state file of a `FileLockSpace`, with the contract of `LockRequest`."""

    def __init__(self, space: FileLockSpace, key: str, seq: int, mode: LockMode, owner: str):
        """Wrap an entry that `FileRWLock.request` put into the state.

        Args:
            space: The space whose state holds the entry.
            key: The lock's key.
            seq: The entry's sequence number.
            mode: The requested mode.
            owner: The operation that asked.
        """
        self.key = key
        self.mode = mode
        self.owner = owner
        self._space = space
        self._seq = seq
        self._waited = False

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
            OSError: The space's directory cannot be read or written.
        """
        space = self._space
        with space._mutex:
            if self._waited:
                raise LockError(f"a request for the lock {self.key!r} is waited for once")
            self._waited = True
        try:
            deadline = time.monotonic() + checked_timeout(timeout)
            poll = POLL_MIN_S
            next_prune = time.monotonic() + PRUNE_EVERY_S
            while True:
                state = space._read()
                entry = _find(state, self.key, self._seq)
                if entry is None:
                    return None
                if entry["granted"]:
                    return self._hold()
                now = time.monotonic()
                if now >= deadline:
                    break
                if now >= next_prune:
                    next_prune = now + PRUNE_EVERY_S
                    if not all(_alive(other) for other in state["locks"].get(self.key, ())):
                        space._change(lambda state: None)  # removes them, grants what fits
                        continue
                wake = space._wake_of(self.key)
                with wake:
                    wake.wait(min(poll, deadline - now))
                poll = min(poll * 2, POLL_MAX_S)
            return self._leave_or_hold()
        except BaseException:
            self.withdraw()
            raise

    def withdraw(self) -> None:
        """Take the request back without waiting for it.

        The entry leaves the queue, or its grant is given back if `wait` has not returned it; a
        thread of this process blocked in `wait` then gets None. Does nothing when the entry is
        already gone (timed out, withdrawn, released, or removed with a dead process).

        Raises:
            LockError: `wait` returned the hold and it is still held; `FileRWLock.release`
                gives it back.
            OSError: The space's directory cannot be read or written.
        """
        key, seq = self.key, self._seq

        def take_back(state: dict[str, Any]) -> None:
            if (key, seq) in self._space._held:
                raise LockError(
                    f"the request of {self.owner!r} for {key!r} was granted; "
                    "release its hold instead"
                )
            if _find(state, key, seq) is not None:
                _remove(state, key, seq)

        self._space._change(take_back, key)

    def _hold(self) -> Hold | None:
        """Turn the granted entry into a hold of this process, unless it was withdrawn meanwhile.

        Returns:
            The hold, or None when another thread withdrew the request first.
        """
        space = self._space
        with space._mutex:
            entry = _find(space._read(), self.key, self._seq)
            if entry is None or not entry["granted"]:
                return None
            space._held.add((self.key, self._seq))
        return Hold(self.key, self.mode, self.owner, self._seq)

    def _leave_or_hold(self) -> Hold | None:
        """At the end of the time: leave the queue, or take the grant that came just now.

        Returns:
            The hold when the entry was granted, else None (the entry is gone).
        """
        key, seq = self.key, self._seq

        def leave(state: dict[str, Any]) -> bool:
            entry = _find(state, key, seq)
            if entry is None:
                return False
            if entry["granted"]:
                self._space._held.add((key, seq))
                return True
            _remove(state, key, seq)
            return False

        granted = self._space._change(leave, key)
        return Hold(key, self.mode, self.owner, seq) if granted else None


class FileRWLock(RWLock):
    """One lock of a `FileLockSpace`, with the contract of `calu.locking.rwlock`."""

    def __init__(self, space: FileLockSpace, key: str) -> None:
        """Make the handle of the lock `key`; the state file holds the lock itself.

        Args:
            space: The space.
            key: The lock's key.
        """
        self.key = key
        self._space = space

    def request(self, mode: LockMode, owner: str) -> FileLockRequest:
        """Put a request for `mode` into the queue at once, without waiting, and return it.

        Its place in the queue is fixed when this returns; it may already be granted.

        Args:
            mode: SHARED or EXCLUSIVE.
            owner: The operation that asks (`checked_owner`).

        Returns:
            The request; its `wait` gives the hold.

        Raises:
            LockError: `owner` already holds this lock or waits for it, in any process.
            TypeError: `owner` is not a string.
            ValueError: `owner` is empty.
            OSError: The space's directory cannot be read or written.
        """
        checked_owner(owner)
        me = _this_process()
        key = self.key

        def enqueue(state: dict[str, Any]) -> int:
            entries = state["locks"].setdefault(key, [])
            if any(entry["owner"] == owner for entry in entries):
                if not entries:
                    state["locks"].pop(key, None)
                raise LockError(
                    f"{owner!r} already holds the lock {key!r} or waits for it; "
                    "an owner never takes a lock twice"
                )
            state["seq"] += 1
            entry = {
                "seq": state["seq"],
                "owner": owner,
                "mode": mode.value,
                "granted": False,
                "pid": me.pid,
                "start": me.start,
                "boot": me.boot,
            }
            entries.append(entry)
            _grant_waiting(entries)
            return state["seq"]

        seq = self._space._change(enqueue, key)
        return FileLockRequest(self._space, key, seq, mode, owner)

    def release(self, hold: Hold) -> None:
        """Give back `hold` and grant the waiting requests that fit now.

        Args:
            hold: A hold that this lock granted to this process, that `wait` returned and that
                was not released yet.

        Raises:
            LockError: `hold` is not a current hold of this lock in this process.
            OSError: The space's directory cannot be read or written.
        """
        space, key = self._space, self.key

        def give_back(state: dict[str, Any]) -> None:
            entry = _find(state, key, hold.token) if hold.key == key else None
            if (
                entry is None
                or (key, hold.token) not in space._held
                or entry["owner"] != hold.owner
                or entry["mode"] != hold.mode.value
            ):
                raise LockError(f"{hold!r} is not a current hold of the lock {key!r}")
            space._held.discard((key, hold.token))
            _remove(state, key, hold.token)

        space._change(give_back, key)


class FileLockSpace(LockSpace):
    """The locks of every process that uses one directory (see the module docstring)."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        """Use `directory` for the state; create it with mode 0o700 when it does not exist.

        Args:
            directory: The directory every sharing process names.

        Raises:
            OSError: The directory cannot be created.
            RuntimeError: The platform has no `flock` (not POSIX).
        """
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("the file lock backend needs flock (POSIX)")
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._state_path = self.directory / STATE_FILE
        self._mutex_path = self.directory / MUTEX_FILE
        self._mutex = threading.Lock()
        self._wakes: dict[str, threading.Condition] = {}
        self._held: set[tuple[str, int]] = set()
        self._locks: dict[str, FileRWLock] = {}

    def lock(self, key: str) -> FileRWLock:
        """Return the handle of the lock `key`.

        Args:
            key: A non-empty string; equal keys give the same lock, in every process.

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
                lock = self._locks[key] = FileRWLock(self, key)
            return lock

    def snapshot(self) -> Sequence[LockState]:
        """Return the state of every lock, as one read of the state file shows it.

        The file is replaced as a whole, so the states were all true at one moment.

        Returns:
            One state per lock that has entries, sorted by key.

        Raises:
            OSError: The space's directory cannot be read.
        """
        state = self._read()
        states = []
        for key in sorted(state["locks"]):
            entries = state["locks"][key]
            states.append(
                LockState(
                    key=key,
                    holders=tuple(
                        (entry["owner"], LockMode(entry["mode"]))
                        for entry in entries
                        if entry["granted"]
                    ),
                    waiters=tuple(
                        (entry["owner"], LockMode(entry["mode"]))
                        for entry in entries
                        if not entry["granted"]
                    ),
                )
            )
        return states

    def _read(self) -> dict[str, Any]:
        """Read the state file; an absent file is the empty state.

        Returns:
            The state.

        Raises:
            LockError: The file is not a state of this backend.
            OSError: The file cannot be read.
        """
        try:
            text = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "seq": 0, "locks": {}}
        try:
            state = json.loads(text)
        except ValueError as exc:
            raise LockError(f"{self._state_path} is not a lock state ({exc})") from None
        if not isinstance(state, dict) or state.get("version") != 1:
            raise LockError(f"{self._state_path} is not a lock state of version 1")
        return state

    def _change(self, change: Callable[[dict[str, Any]], _T], key: str | None = None) -> _T:
        """Change the state under the flock: prune dead processes, apply `change`, write it.

        When `change` raises, nothing is written (the pruning is repeated by the next change).
        Afterwards the waiting requests of this process are woken: those of `key`, or all of
        them when `key` is None or the pruning removed an entry.

        Args:
            change: Changes the state in place and returns a result.
            key: The only lock whose entries `change` touches, or None.

        Returns:
            What `change` returned.

        Raises:
            OSError: The space's directory cannot be read or written.
        """
        with self._mutex:
            fd = os.open(self._mutex_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                state = self._read()
                pruned = self._prune(state)
                result = change(state)
                self._write(state)
            finally:
                os.close(fd)  # also ends the flock
            wakes = list(self._wakes.values()) if key is None or pruned else [self._wake_of(key)]
        for wake in wakes:
            with wake:
                wake.notify_all()
        return result

    def _wake_of(self, key: str) -> threading.Condition:
        """Return the condition on which this process's waiting requests for `key` wait.

        Args:
            key: The lock's key.

        Returns:
            The condition, created on first use.
        """
        wake = self._wakes.get(key)
        if wake is None:
            wake = self._wakes.setdefault(key, threading.Condition())
        return wake

    def _prune(self, state: dict[str, Any]) -> bool:
        """Remove the entries of processes that no longer run, and grant what fits then.

        Args:
            state: The state; changed in place.

        Returns:
            Whether an entry was removed.
        """
        removed = False
        for key in list(state["locks"]):
            entries = state["locks"][key]
            alive = [entry for entry in entries if _alive(entry)]
            if len(alive) == len(entries):
                continue
            removed = True
            if alive:
                _grant_waiting(alive)
                state["locks"][key] = alive
            else:
                del state["locks"][key]
        return removed

    def _write(self, state: dict[str, Any]) -> None:
        """Replace the state file as a whole: write a temporary file, then rename it.

        Args:
            state: The state.

        Raises:
            OSError: The file cannot be written.
        """
        temporary = self.directory / f".{STATE_FILE}.{os.getpid()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, separators=(",", ":"))
        os.replace(temporary, self._state_path)
