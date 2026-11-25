"""The file backend of `calu.locking` across real processes (agent/records/notes/locking.md §8).

The tests marked `slow` start Python processes (multiprocessing, "spawn"; each imports calu,
about a second) that share a lock directory below `tmp_path`. The processes write marker and
counter files there and nothing else; the test kills the ones it started on purpose (SIGKILL)
and every other one at its end. No output.
"""

import errno
import fcntl
import multiprocessing
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

from calu.locking import FileLockSpace, LockError, LockMode, LockState, filelocks

SPAWN = multiprocessing.get_context("spawn")
SHARED = LockMode.SHARED
EXCLUSIVE = LockMode.EXCLUSIVE


# Code that runs in the child processes (module level, so that "spawn" can import it)


def count_in_turns(directory: str, counter: str, iterations: int, name: str) -> None:
    """In a child: `iterations` times, take the lock "k" exclusively and add one to a counter.

    The addition is not atomic (read, pause, write), so a lost update shows two holders at once.

    Args:
        directory: The lock directory.
        counter: The counter file.
        iterations: How often to count.
        name: The child's name, for the owners.
    """
    lock = FileLockSpace(directory).lock("k")
    for n in range(iterations):
        hold = lock.acquire(EXCLUSIVE, 60, f"{name}-{n}")
        assert hold is not None
        path = Path(counter)
        value = int(path.read_text())
        time.sleep(0.002)
        path.write_text(str(value + 1))
        lock.release(hold)


def hold_until_killed(directory: str, marker: str, owner: str, mode: str) -> None:
    """In a child: take the lock "k", write `marker`, then sleep until the test kills it.

    Args:
        directory: The lock directory.
        marker: The file written once the lock is held.
        owner: The owner of the request.
        mode: "shared" or "exclusive".
    """
    hold = FileLockSpace(directory).lock("k").acquire(LockMode(mode), 60, owner)
    assert hold is not None
    Path(marker).write_text("held")
    time.sleep(300)


def queue_then_hold(directory: str, owner: str, mode: str, marker_dir: str) -> None:
    """In a child: request the lock "k", report each step with a file, release on request.

    Writes `<owner>.queued` once the request is in the queue and `<owner>.granted` once it is
    granted; releases when the file `<owner>.release` appears (at most 60 s later).

    Args:
        directory: The lock directory.
        owner: The owner of the request.
        mode: "shared" or "exclusive".
        marker_dir: Where the marker files go.
    """
    markers = Path(marker_dir)
    lock = FileLockSpace(directory).lock("k")
    request = lock.request(LockMode(mode), owner)
    (markers / f"{owner}.queued").write_text("queued")
    hold = request.wait(60)
    assert hold is not None
    (markers / f"{owner}.granted").write_text("granted")
    deadline = time.monotonic() + 60
    while not (markers / f"{owner}.release").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    lock.release(hold)


# Helpers of the tests


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    """Poll `condition` every 5 ms until it is true.

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
        time.sleep(0.005)


def state(space: FileLockSpace, key: str = "k") -> LockState:
    """Return the state of one lock; an unused lock has no holders and no waiters.

    Args:
        space: The space.
        key: The lock's key.

    Returns:
        The state.
    """
    for lock_state in space.snapshot():
        if lock_state.key == key:
            return lock_state
    return LockState(key, (), ())


@pytest.fixture
def children() -> Iterator[list[BaseProcess]]:
    """Collect the child processes a test starts, and kill the ones still running at its end.

    Yields:
        The list the test appends its processes to.
    """
    started: list[BaseProcess] = []
    yield started
    for process in started:
        if process.is_alive():
            process.kill()
        process.join(10)


def start(children: list[BaseProcess], target: Callable[..., None], *args: object) -> BaseProcess:
    """Start `target(*args)` in a new process and remember it.

    Args:
        children: The test's processes.
        target: A function of this module.
        *args: Its arguments.

    Returns:
        The started process.
    """
    process = SPAWN.Process(target=target, args=args, daemon=True)
    process.start()
    children.append(process)
    return process


# Across processes


@pytest.mark.slow
def test_processes_hold_an_exclusive_lock_one_at_a_time(
    tmp_path: Path, children: list[BaseProcess]
) -> None:
    counter = tmp_path / "counter"
    counter.write_text("0")
    for n in range(4):
        start(children, count_in_turns, str(tmp_path / "locks"), str(counter), 15, f"p{n}")
    for process in children:
        process.join(120)
        assert process.exitcode == 0
    assert counter.read_text() == "60"


@pytest.mark.slow
def test_requests_of_several_processes_are_granted_in_the_order_they_were_made(
    tmp_path: Path, children: list[BaseProcess]
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    markers = tmp_path / "markers"
    markers.mkdir()
    first = space.lock("k").acquire(EXCLUSIVE, 0, "parent")
    assert first is not None
    for owner, mode in (("b", "shared"), ("c", "exclusive"), ("d", "shared")):
        start(children, queue_then_hold, str(space.directory), owner, mode, str(markers))
        wait_until((markers / f"{owner}.queued").exists)
    assert state(space).waiters == (("b", SHARED), ("c", EXCLUSIVE), ("d", SHARED))
    space.lock("k").release(first)
    wait_until((markers / "b.granted").exists)
    # d is shared like b, but stays behind the waiting exclusive request of c (FIFO).
    assert state(space).holders == (("b", SHARED),)
    assert state(space).waiters == (("c", EXCLUSIVE), ("d", SHARED))
    (markers / "b.release").write_text("")
    wait_until((markers / "c.granted").exists)
    assert not (markers / "d.granted").exists()
    (markers / "c.release").write_text("")
    wait_until((markers / "d.granted").exists)
    (markers / "d.release").write_text("")
    for process in children:
        process.join(30)
        assert process.exitcode == 0
    assert state(space) == LockState("k", (), ())


@pytest.mark.slow
def test_the_hold_of_a_killed_process_is_given_up(
    tmp_path: Path, children: list[BaseProcess]
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    marker = tmp_path / "held"
    holder = start(children, hold_until_killed, str(space.directory), str(marker), "a", "exclusive")
    wait_until(marker.exists)
    got: list[object] = []
    waiter = threading.Thread(
        target=lambda: got.append(space.lock("k").acquire(EXCLUSIVE, 20, "b")), daemon=True
    )
    waiter.start()
    wait_until(lambda: state(space).waiters == (("b", EXCLUSIVE),))
    assert holder.pid is not None
    killed = time.monotonic()
    os.kill(holder.pid, signal.SIGKILL)
    holder.join(10)
    waiter.join(10)
    assert got and got[0] is not None
    assert time.monotonic() - killed < 5
    assert state(space).holders == (("b", EXCLUSIVE),)


@pytest.mark.slow
def test_an_owner_is_refused_across_processes(tmp_path: Path, children: list[BaseProcess]) -> None:
    space = FileLockSpace(tmp_path / "locks")
    marker = tmp_path / "held"
    start(children, hold_until_killed, str(space.directory), str(marker), "x", "shared")
    wait_until(marker.exists)
    with pytest.raises(LockError):
        space.lock("k").request(SHARED, "x")
    assert space.lock("k").acquire(SHARED, 0, "y") is not None


# Within this process


def test_entries_of_ended_processes_and_reused_ids_are_not_alive() -> None:
    me = filelocks._this_process()
    alive = {"pid": me.pid, "start": me.start}
    assert filelocks._alive(alive)
    assert not filelocks._alive({**alive, "start": "1"})  # the id now belongs to another process
    assert not filelocks._alive({**alive, "pid": 2**22 + 12345})  # no such process


def test_a_zombie_counts_as_ended_and_a_process_that_cannot_be_read_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen(["sleep", "30"])
    try:
        found = filelocks._proc_stat(child.pid)
        assert found is not None
        entry = {"pid": child.pid, "start": found[1]}
        assert filelocks._alive(entry)
        os.kill(child.pid, signal.SIGKILL)
        wait_until(lambda: (filelocks._proc_stat(child.pid) or ("Z", ""))[0] == "Z")
        assert not filelocks._alive(entry)  # ended, but not reaped yet
    finally:
        child.kill()
        child.wait()
    filelocks._this_process()

    def unreadable(pid: int) -> tuple[str, str] | None:
        """Fail like a read of /proc that hit the limit of open files.

        Args:
            pid: The process id.

        Raises:
            OSError: Always.
        """
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(filelocks, "_proc_stat", unreadable)
    assert filelocks._alive({"pid": 1, "start": "unknown"})


def test_the_files_are_named_after_the_boot_and_the_pid_namespace(tmp_path: Path) -> None:
    space = FileLockSpace(tmp_path / "locks")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")[:16]
    namespace = os.stat("/proc/self/ns/pid").st_ino
    assert space.state_path.name == f"locks.{boot}.{namespace}.json"
    assert space.mutex_path.name == f"locks.{boot}.{namespace}.mutex"


def test_a_state_file_that_is_not_a_lock_state_is_refused(tmp_path: Path) -> None:
    space = FileLockSpace(tmp_path / "locks")
    space.state_path.write_text("not json")
    with pytest.raises(LockError):
        space.lock("k").request(SHARED, "a")
    space.state_path.write_text('{"version": 2}')
    with pytest.raises(LockError):
        space.snapshot()
    space.state_path.write_text('{"version": 1, "seq": 1, "locks": {"k": [{"seq": 1}]}}')
    with pytest.raises(LockError):
        space.snapshot()


def test_the_directory_is_private_and_the_state_empty_after_the_last_release(
    tmp_path: Path,
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    assert space.directory.stat().st_mode & 0o777 == 0o700
    hold = space.lock("k").acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    space.lock("k").release(hold)
    assert space.snapshot() == []


def test_a_directory_that_others_can_write_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o777)
    with pytest.raises(OSError, match="written by the group or others"):
        FileLockSpace(directory)


def test_a_file_left_at_the_temporary_name_is_replaced_not_followed(tmp_path: Path) -> None:
    space = FileLockSpace(tmp_path / "locks")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    (space.directory / f".{space.state_path.name}.{os.getpid()}.tmp").symlink_to(victim)
    hold = space.lock("k").acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    assert victim.read_text() == "keep me\n"
    assert not space.state_path.is_symlink()
    assert state(space).holders == (("a", EXCLUSIVE),)


def test_a_failed_write_leaves_no_hold_that_nobody_knows_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    lock = space.lock("k")
    hold = lock.acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    write = space._write

    def disk_full(text: str) -> None:
        """Fail like a write to a full disk.

        Args:
            text: The state that would be written.

        Raises:
            OSError: Always.
        """
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(space, "_write", disk_full)
    with pytest.raises(OSError):
        lock.release(hold)
    with pytest.raises(LockError):
        lock.release(hold)  # forgotten: it counts as released
    assert state(space).holders == (("a", EXCLUSIVE),)  # but it is still in the file
    with pytest.raises(OSError):
        lock.request(SHARED, "b")
    assert (space._mine, space._held) == (set(), {})  # nothing counts that was not written
    monkeypatch.setattr(space, "_write", write)
    later = lock.acquire(EXCLUSIVE, 0, "b")  # the next change removes the forgotten entry
    assert later is not None
    assert state(space).holders == (("b", EXCLUSIVE),)


def test_a_change_gives_up_when_another_process_keeps_the_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    monkeypatch.setattr(filelocks, "FLOCK_WAIT_S", 0.2)
    fd = os.open(space.mutex_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # as a process stopped inside its change keeps it
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            space.lock("k").request(SHARED, "a")
        assert time.monotonic() - started < 2
    finally:
        os.close(fd)
    assert space.lock("k").acquire(SHARED, 0, "a") is not None


def test_two_spaces_of_one_process_keep_each_others_entries(tmp_path: Path) -> None:
    one, two = FileLockSpace(tmp_path / "locks"), FileLockSpace(tmp_path / "locks")
    hold = one.lock("k").acquire(EXCLUSIVE, 0, "a")
    assert hold is not None
    assert two.lock("k").acquire(EXCLUSIVE, 0, "b") is None
    assert state(two).holders == (("a", EXCLUSIVE),)
    one.lock("k").release(hold)
    assert two.lock("k").acquire(EXCLUSIVE, 0, "b") is not None
