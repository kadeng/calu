"""The `RWLock` backend for the processes of one user on one machine (locking.md §8).

The design: agent/records/notes/locking.md, section 8.

All locks of a space live in one JSON state file in the space's directory. A change of the
state works like this:

1. It takes the `flock` of a separate mutex file, and waits for it at most `FLOCK_WAIT_S`
   (another process may be stopped inside its change). flock belongs to an open file
   description, and every change opens the file anew, so threads exclude each other as well as
   processes.
2. It reads the state, removes the entries of processes that ended and the entries this space
   no longer wants (below), applies the change, and grants the waiting requests that fit
   (FIFO, the same rules as `ThreadRWLock`).
3. When the state changed, it writes the new state to a temporary file and renames it over the
   state file, so a reader without the flock always sees a whole state.

A waiting request polls its entry, at first every millisecond and then up to every
`POLL_MAX_S`. A change made by its own process wakes it at once. Every `PRUNE_EVERY_S` it also
checks the entries of its lock, and when one belongs to a process that ended, it removes them;
so a process killed while it holds a lock does not block the others for long.

Process ids can only be compared within one process table: one boot of one kernel, one pid
namespace (the "same" relation of `calu.machine`, D202). The two files are therefore named after
both, `locks.<boot>.<pid namespace>.json` and `.mutex`. Processes in other containers, or on
other machines that share the directory, use other files and do not share the locks with this
process. Within a file, an entry names its process by id and start time, so a reused process id
does not keep a dead process's entries alive. An entry is removed only on proof that its process
ended: no process has the id, a process with another start time has it, or the process ended
and was not reaped yet (a zombie). When `/proc` cannot be read, the entry stays. Without `/proc`
(not Linux) the files are `locks.local.*`, and only the process id is checked.

A space forgets an entry before the change that removes it (release, withdraw, timeout). If that
change fails (e.g. the disk is full), the entry stays in the file, and the next change of the
space removes it: no hold is left that nobody knows of. An entry that a change adds counts only
when the change was written.

The holds of a process belong to it: `release` and `withdraw` work from any of its threads, not
from other processes. Create one `FileLockSpace` per directory and process; an object inherited
across a `fork` must not be used in the child. The directory must belong to the user and must
not be writable by the group or others; a new one is created with mode 0o700. Every process
that uses it shares the locks; that is the point of the backend.
"""

import errno
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
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

POLL_MIN_S = 0.001
"""The first interval at which a waiting request looks at its entry."""

POLL_MAX_S = 0.2
"""The longest interval at which a waiting request looks at its entry."""

PRUNE_EVERY_S = 0.2
"""How often a waiting request looks for entries of processes that ended on its lock."""

FLOCK_WAIT_S = 5.0
"""The longest a change waits for the flock of the mutex file."""

_PROC = Path("/proc/self/stat").exists()
"""Whether this system has a Linux `/proc`."""

_DEAD_STATES = frozenset({"Z", "X", "x"})
"""Process states (field 3 of `/proc/<pid>/stat`) of a process that ended."""

_MODES = frozenset(mode.value for mode in LockMode)

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _Process:
    """What identifies a process of this process table across process-id reuse.

    Attributes:
        pid: The process id.
        start: Its start time (field 22 of `/proc/<pid>/stat`), or "" without `/proc`.
    """

    pid: int
    start: str


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Read a process's state and start time from `/proc`.

    Args:
        pid: The process id.

    Returns:
        (state, start time): fields 3 and 22 of `/proc/<pid>/stat`; None when no process has
        the id.

    Raises:
        OSError: The file exists but cannot be read now (e.g. too many open files), or has an
            unknown format. The caller cannot tell then whether the process runs.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError, ProcessLookupError:
        return None
    fields = text[text.rfind(")") + 2 :].split()
    if len(fields) < 20:
        raise OSError(errno.EINVAL, f"/proc/{pid}/stat has an unknown format")
    return fields[0], fields[19]


def _scope() -> str:
    """Name the process table of the calling process: the kernel's boot and the pid namespace.

    Returns:
        "<16 hex digits of the boot id>.<inode of the pid namespace>", or "local" without
        `/proc`.

    Raises:
        OSError: `/proc` exists, but the boot id or the pid namespace cannot be read, or `/proc`
            shows another pid namespace than the one of this process.
    """
    if not _PROC:
        return "local"
    if os.readlink("/proc/self") != str(os.getpid()):
        raise OSError(errno.EINVAL, "/proc belongs to another pid namespace than this process")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii")
    digits = boot.strip().replace("-", "")[:16]
    if len(digits) != 16 or any(digit not in "0123456789abcdef" for digit in digits):
        raise OSError(errno.EINVAL, f"the boot id {boot.strip()!r} has an unknown format")
    return f"{digits}.{os.stat('/proc/self/ns/pid').st_ino}"


_me: _Process | None = None


def _this_process() -> _Process:
    """Return the identity of the calling process; read again after a fork.

    Returns:
        The identity.

    Raises:
        OSError: `/proc` exists, but this process's start time cannot be read now. Nothing is
            remembered then; the next call reads again.
    """
    global _me
    pid = os.getpid()
    if _me is None or _me.pid != pid:
        start = ""
        if _PROC:
            found = _proc_stat(pid)
            if found is None:
                raise OSError(errno.ESRCH, f"/proc/{pid}/stat of this process is missing")
            start = found[1]
        _me = _Process(pid, start)
    return _me


def _alive(entry: dict[str, Any]) -> bool:
    """Tell whether the process that made `entry` may still run; only proof of its end counts.

    Args:
        entry: An entry of the state, with its process's `pid` and `start`.

    Returns:
        False when no process has the id, a process with another start time has it (the id was
        reused), or the process ended and was not reaped yet (a zombie); True otherwise, also
        when `/proc` cannot be read now.

    Raises:
        OSError: This process's own start time cannot be read (`_this_process`).
    """
    me = _this_process()
    pid = entry["pid"]
    if pid == me.pid:
        return entry["start"] == me.start
    if not _PROC:  # the process id alone
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    try:
        found = _proc_stat(pid)
    except OSError:
        return True
    if found is None:
        return False
    state, start = found
    return start == entry["start"] and state not in _DEAD_STATES


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


def _unchanged(state: dict[str, Any]) -> None:
    """Change nothing: a change that only removes the entries of ended processes.

    Args:
        state: The state.
    """


def _is_state(state: object) -> bool:
    """Tell whether `state` has the form this backend writes.

    Args:
        state: What the state file holds, parsed.

    Returns:
        True for {"version": 1, "seq": int, "locks": {key: [entry, ...]}} with complete entries.
    """
    if not isinstance(state, dict) or state.get("version") != 1:
        return False
    locks = state.get("locks")
    if not isinstance(state.get("seq"), int) or not isinstance(locks, dict):
        return False
    return all(
        isinstance(entries, list)
        and all(
            isinstance(entry, dict)
            and isinstance(entry.get("seq"), int)
            and isinstance(entry.get("owner"), str)
            and entry.get("mode") in _MODES
            and isinstance(entry.get("granted"), bool)
            and isinstance(entry.get("pid"), int)
            and isinstance(entry.get("start"), str)
            and isinstance(entry.get("space"), str)
            for entry in entries
        )
        for entries in locks.values()
    )


def _flock(fd: int, deadline: float) -> None:
    """Take the exclusive flock of `fd`, waiting at most until `deadline`.

    A free flock is taken at once. A busy one is waited for in the kernel's queue by a helper
    thread on a duplicate of `fd` (the same open file description, so the flock is the same),
    while this thread waits until `deadline`. When the time runs out, the helper stays blocked
    until it gets the flock, and then closes its duplicate: once the caller has closed `fd`,
    that gives the flock back at once.

        free ──► taken            busy ──► helper: flock(dup) ──► close(dup) ──► done
                                             caller: wait ≤ deadline ──► taken | TimeoutError

    Args:
        fd: The open mutex file.
        deadline: The latest moment (`time.monotonic`) to wait until.

    Raises:
        TimeoutError: The flock was not free in time; another process holds it (it may be
            stopped inside a change).
        OSError: flock failed for another reason.
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass
    duplicate = os.dup(fd)
    done = threading.Event()
    failed: list[OSError] = []

    def take() -> None:
        """Wait for the flock in the kernel's queue, then close the duplicate."""
        try:
            fcntl.flock(duplicate, fcntl.LOCK_EX)
        except OSError as exc:
            failed.append(exc)
        finally:
            os.close(duplicate)
            done.set()

    threading.Thread(target=take, name="calu-flock", daemon=True).start()
    if not done.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError(
            errno.ETIMEDOUT, "another process holds the flock of the lock state too long"
        )
    if failed:
        raise failed[0]


def _check_private(directory: Path) -> None:
    """Refuse a lock directory that another user could change.

    Args:
        directory: The directory, resolved.

    Raises:
        OSError: `directory` is not a directory (e.g. a symlink), belongs to another user, or
            can be written by the group or others.
    """
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ENOTDIR, f"the lock directory {directory} is not a directory")
    if info.st_uid != os.geteuid():
        raise OSError(errno.EPERM, f"the lock directory {directory} belongs to another user")
    if info.st_mode & 0o022:
        raise OSError(
            errno.EPERM,
            f"the lock directory {directory} can be written by the group or others "
            f"(mode {oct(info.st_mode & 0o777)}; `chmod go-w` it)",
        )


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
        (a grant given back) before the exception propagates. The end of the wait can take up
        to `FLOCK_WAIT_S` longer when another process holds the flock of the state.

        Args:
            timeout: Seconds to wait, finite and >= 0 (`checked_timeout`).

        Returns:
            The hold, or None when the time ran out or the request was withdrawn; the request
            then no longer waits and nothing is held.

        Raises:
            LockError: `wait` was called a second time, or the state file is not a lock state.
            TypeError: `timeout` is not a number.
            ValueError: `timeout` is negative, NaN or infinite.
            OSError: The space's directory cannot be read or written.
        """
        space, key, seq = self._space, self.key, self._seq
        with space._mutex:
            if self._waited:
                raise LockError(f"a request for the lock {key!r} is waited for once")
            self._waited = True
        try:
            deadline = time.monotonic() + checked_timeout(timeout)
            poll = POLL_MIN_S
            next_prune = time.monotonic() + PRUNE_EVERY_S
            while True:
                with space._mutex:
                    if (key, seq) not in space._mine:
                        return None  # withdrawn
                state = space._read()
                entry = _find(state, key, seq)
                if entry is None:
                    return None
                if entry["granted"]:
                    return self._hold()
                now = time.monotonic()
                if now >= deadline:
                    break
                if now >= next_prune:
                    next_prune = now + PRUNE_EVERY_S
                    if not all(_alive(other) for other in state["locks"].get(key, ())):
                        with suppress(TimeoutError):  # the flock is busy: next time
                            space._change(_unchanged, deadline=max(deadline, now + POLL_MAX_S))
                        continue
                wake = space._wake_of(key)
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
        already gone (timed out, withdrawn, released, or removed with a dead process). The space
        forgets the entry first: if the change fails, its next change removes the entry.

        Raises:
            LockError: `wait` returned the hold and it is still held (`FileRWLock.release` gives
                it back), or the state file is not a lock state.
            OSError: The space's directory cannot be read or written.
        """
        space, key, seq = self._space, self.key, self._seq
        with space._mutex:
            if (key, seq) in space._held:
                raise LockError(
                    f"the request of {self.owner!r} for {key!r} was granted; "
                    "release its hold instead"
                )
            wanted = (key, seq) in space._mine
            space._mine.discard((key, seq))
        if not wanted:
            wake = space._wake_of(key)
            with wake:
                wake.notify_all()
            return

        def take_back(state: dict[str, Any]) -> None:
            """Remove the entry if it is still there.

            Args:
                state: The state; changed in place.
            """
            if _find(state, key, seq) is not None:
                _remove(state, key, seq)

        space._change(take_back, key)

    def _hold(self) -> Hold | None:
        """Turn the granted entry into a hold of this process, unless it was withdrawn meanwhile.

        An entry that this space still wants is never removed by anyone else, so it is still
        granted when this runs.

        Returns:
            The hold, or None when another thread withdrew the request first.
        """
        space, key, seq = self._space, self.key, self._seq
        with space._mutex:
            if (key, seq) not in space._mine:
                return None
            hold = Hold(key, self.mode, self.owner, seq)
            space._held[key, seq] = hold
        return hold

    def _leave_or_hold(self) -> Hold | None:
        """At the end of the time: leave the queue, or take the grant that came just now.

        Returns:
            The hold when the entry was granted, else None (the entry is gone).

        Raises:
            LockError: The state file is not a lock state.
            OSError: The space's directory cannot be read or written.
        """
        space, key, seq = self._space, self.key, self._seq
        hold = Hold(key, self.mode, self.owner, seq)

        def leave(state: dict[str, Any]) -> bool:
            """Remove the queued entry, or keep a grant that came just now as a hold.

            Both count only if the change is written (`FileLockSpace._change`).

            Args:
                state: The state; changed in place.

            Returns:
                True when the entry was granted (now a hold of this process), else False.
            """
            entry = _find(state, key, seq)
            if entry is None or (key, seq) not in space._mine:
                return False
            if entry["granted"]:
                space._held[key, seq] = hold
                return True
            space._mine.discard((key, seq))
            _remove(state, key, seq)
            return False

        return hold if space._change(leave, key) else None


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
        """Put a request for `mode` into the queue without waiting for the lock, and return it.

        Its place in the queue is fixed when this returns; it may already be granted. Waits
        only for the flock of the state, at most `FLOCK_WAIT_S`.

        Args:
            mode: SHARED or EXCLUSIVE.
            owner: The operation that asks (`checked_owner`).

        Returns:
            The request; its `wait` gives the hold.

        Raises:
            LockError: `owner` already holds this lock or waits for it, in any process; or the
                state file is not a lock state.
            TypeError: `owner` is not a string.
            ValueError: `owner` is empty.
            OSError: The space's directory cannot be read or written, or the flock of the
                state was not free in time (`TimeoutError`).
        """
        checked_owner(owner)
        space, key = self._space, self.key
        me = _this_process()

        def enqueue(state: dict[str, Any]) -> int:
            """Append the request to the lock's queue and grant what fits.

            The entry counts as this space's only if the change is written.

            Args:
                state: The state; changed in place.

            Returns:
                The new entry's sequence number.

            Raises:
                LockError: `owner` already has an entry on this lock.
            """
            entries = state["locks"].setdefault(key, [])
            if any(entry["owner"] == owner for entry in entries):
                raise LockError(
                    f"{owner!r} already holds the lock {key!r} or waits for it; "
                    "an owner never takes a lock twice"
                )
            state["seq"] += 1
            seq = state["seq"]
            entries.append(
                {
                    "seq": seq,
                    "owner": owner,
                    "mode": mode.value,
                    "granted": False,
                    "pid": me.pid,
                    "start": me.start,
                    "space": space._token,
                }
            )
            _grant_waiting(entries)
            space._mine.add((key, seq))
            return seq

        seq = space._change(enqueue, key)
        return FileLockRequest(space, key, seq, mode, owner)

    def release(self, hold: Hold) -> None:
        """Give back `hold` and grant the waiting requests that fit now.

        The space forgets the hold first: if the change fails, the hold counts as released all
        the same, and the space's next change removes its entry.

        Args:
            hold: A hold that this lock granted to this process, that `wait` returned and that
                was not released yet.

        Raises:
            LockError: `hold` is not a current hold of this lock in this process, or the state
                file is not a lock state.
            OSError: The space's directory cannot be read or written.
        """
        space, key = self._space, self.key
        with space._mutex:
            if hold.key != key or space._held.get((key, hold.token)) != hold:
                raise LockError(f"{hold!r} is not a current hold of the lock {key!r}")
            del space._held[key, hold.token]
            space._mine.discard((key, hold.token))

        def give_back(state: dict[str, Any]) -> None:
            """Remove the held entry, if it is still there, and grant what fits.

            Args:
                state: The state; changed in place.
            """
            if _find(state, key, hold.token) is not None:
                _remove(state, key, hold.token)

        space._change(give_back, key)


class FileLockSpace(LockSpace):
    """The locks of every process that uses one directory (see the module docstring).

    Attributes:
        directory: The directory, resolved.
        state_path: The state file of this process table.
        mutex_path: The file whose flock guards every change of the state.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        """Use `directory` for the state; create it with mode 0o700 when it does not exist.

        Args:
            directory: The directory every sharing process names.

        Raises:
            OSError: The directory cannot be created, belongs to another user, or can be
                written by the group or others; or this process's process table cannot be
                identified (`/proc` cannot be read).
            RuntimeError: The platform has no `flock` (not POSIX).
        """
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("the file lock backend needs flock (POSIX)")
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private(self.directory)
        scope = _scope()
        _this_process()
        self.state_path = self.directory / f"locks.{scope}.json"
        self.mutex_path = self.directory / f"locks.{scope}.mutex"
        self._token = secrets.token_hex(8)  # tells this space's entries from other spaces'
        self._mutex = threading.Lock()
        self._wakes: dict[str, threading.Condition] = {}
        self._mine: set[tuple[str, int]] = set()  # entries this space wants
        self._held: dict[tuple[str, int], Hold] = {}  # holds `wait` returned
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
            LockError: The state file is not a lock state.
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
            OSError: The file cannot be read, or is a symlink.
        """
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {"version": 1, "seq": 0, "locks": {}}
        with os.fdopen(fd, encoding="utf-8") as source:
            text = source.read()
        try:
            state = json.loads(text)
        except ValueError as exc:
            raise LockError(f"{self.state_path} is not a lock state ({exc})") from None
        if not _is_state(state):
            raise LockError(f"{self.state_path} is not a lock state of version 1")
        return state

    def _change(
        self,
        change: Callable[[dict[str, Any]], _T],
        key: str | None = None,
        deadline: float | None = None,
    ) -> _T:
        """Change the state under the flock: prune, apply `change`, write it if it changed.

        The pruning removes the entries of processes that ended and the entries of this space
        that it no longer wants (`_mine`). `change` may add to `_mine` and `_held`; when the
        change fails (it raises, or the state cannot be written), both are put back as they
        were, and nothing is written. Afterwards the waiting requests of this process are
        woken: those of `key`, or all of them when `key` is None or the pruning removed an
        entry.

        Args:
            change: Changes the state in place and returns a result.
            key: The only lock whose entries `change` touches, or None.
            deadline: The latest moment (`time.monotonic`) to wait for the flock until; None
                for `FLOCK_WAIT_S` from now.

        Returns:
            What `change` returned.

        Raises:
            LockError: The state file is not a lock state.
            OSError: The space's directory cannot be read or written; `TimeoutError` when the
                flock was not free in time.
        """
        if deadline is None:
            deadline = time.monotonic() + FLOCK_WAIT_S
        with self._mutex:
            mine, held = set(self._mine), dict(self._held)
            fd = os.open(
                self.mutex_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            try:
                _flock(fd, deadline)
                state = self._read()
                before = json.dumps(state, separators=(",", ":"))
                pruned = self._prune(state)
                result = change(state)
                after = json.dumps(state, separators=(",", ":"))
                if after != before:
                    self._write(after)
            except BaseException:
                self._mine, self._held = mine, held  # the change did not happen
                raise
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
        """Remove the entries of ended processes and the unwanted ones of this space; grant then.

        Args:
            state: The state; changed in place.

        Returns:
            Whether an entry was removed.

        Raises:
            OSError: This process's start time cannot be read (`_this_process`).
        """
        me = _this_process()
        removed = False
        for key in list(state["locks"]):
            entries = state["locks"][key]
            kept = [
                entry
                for entry in entries
                if (
                    (key, entry["seq"]) in self._mine
                    if entry["space"] == self._token
                    and entry["pid"] == me.pid
                    and entry["start"] == me.start
                    else _alive(entry)
                )
            ]
            if len(kept) == len(entries):
                continue
            removed = True
            if kept:
                _grant_waiting(kept)
                state["locks"][key] = kept
            else:
                del state["locks"][key]
        return removed

    def _write(self, text: str) -> None:
        """Replace the state file as a whole: write a new temporary file, then rename it.

        A file or symlink left at the temporary file's name is removed first, and never
        followed.

        Args:
            text: The state as JSON.

        Raises:
            OSError: The file cannot be written.
        """
        temporary = self.directory / f".{self.state_path.name}.{os.getpid()}.tmp"
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
            os.replace(temporary, self.state_path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise
