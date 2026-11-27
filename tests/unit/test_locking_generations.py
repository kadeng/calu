"""The generations of `calu.locking` (agent/records/notes/locking.md, sections 4, 8 and 9).

A generation is a number per lock. It advances when a hold is released with `changed=True`.
The backend tests run against the thread backend and the file backend (its state below
`tmp_path`); the scheme's tests use the thread backend. Threads of this process only; one test
starts a short Python process and waits for its end. No output.
"""

import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest

from calu.locking import (
    GLOBAL_KEY,
    FileLockSpace,
    LockMode,
    LockSpace,
    LockTimeoutError,
    LockTimeouts,
    NoLockingScheme,
    RWLockingScheme,
    ThreadLockSpace,
    file_key,
    worktree_key,
)
from calu.locking.scheme import counts_changes

SHARED = LockMode.SHARED
EXCLUSIVE = LockMode.EXCLUSIVE


@pytest.fixture(params=["thread", "file"])
def space(request: pytest.FixtureRequest, tmp_path: Path) -> LockSpace:
    """A fresh lock space of each backend: the threads of one process, or a state file.

    Args:
        request: Gives the backend's name.
        tmp_path: The directory of the file backend's state.

    Returns:
        The space.
    """
    if request.param == "file":
        return FileLockSpace(tmp_path / "locks")
    return ThreadLockSpace()


def scheme() -> RWLockingScheme:
    """Return a scheme on a fresh thread backend with waits of one second.

    Returns:
        The scheme.
    """
    return RWLockingScheme(ThreadLockSpace(), LockTimeouts(1, 1, 1, 1))


# The backends


def test_a_release_with_changed_advances_the_generation_of_its_lock_only(
    space: LockSpace,
) -> None:
    assert space.generations(["a", "b"]) == {"a": 0, "b": 0}
    hold = space.lock("a").acquire(EXCLUSIVE, 0, "one")
    assert hold is not None
    assert space.generations(["a"]) == {"a": 0}  # taking a lock changes nothing
    space.lock("a").release(hold, changed=True)
    assert space.generations(["a", "b"]) == {"a": 1, "b": 0}
    hold = space.lock("a").acquire(SHARED, 0, "two")
    assert hold is not None
    space.lock("a").release(hold)  # the default: nothing changed
    assert space.generations(["a", "b"]) == {"a": 1, "b": 0}


def test_every_shared_hold_released_with_changed_counts(space: LockSpace) -> None:
    lock = space.lock("a")
    first, second = lock.acquire(SHARED, 0, "one"), lock.acquire(SHARED, 0, "two")
    assert first is not None and second is not None
    lock.release(first, changed=True)
    lock.release(second, changed=True)
    assert space.generations(["a"]) == {"a": 2}


def test_a_withdrawn_grant_does_not_advance_the_generation(space: LockSpace) -> None:
    request = space.lock("a").request(EXCLUSIVE, "one")
    request.withdraw()
    assert space.generations(["a"]) == {"a": 0}


def test_reading_a_generation_creates_no_lock_in_the_thread_backend() -> None:
    space = ThreadLockSpace()
    assert space.generations(["never-used"]) == {"never-used": 0}
    assert space.snapshot() == []


def test_two_file_spaces_on_one_directory_share_the_generations(tmp_path: Path) -> None:
    one, two = FileLockSpace(tmp_path / "locks"), FileLockSpace(tmp_path / "locks")
    hold = one.lock("a").acquire(EXCLUSIVE, 0, "one")
    assert hold is not None
    one.lock("a").release(hold, changed=True)
    assert two.generations(["a"]) == {"a": 1}  # as another process would read it
    assert json.loads(one.state_path.read_text())["generations"] == {"a": 1}


def test_a_release_whose_write_failed_advances_the_generation_with_the_next_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = FileLockSpace(tmp_path / "locks")
    hold = space.lock("a").acquire(EXCLUSIVE, 0, "one")
    assert hold is not None

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
        space.lock("a").release(hold, changed=True)
    assert space.generations(["a"]) == {"a": 0}  # not written; the entry still holds the lock
    monkeypatch.undo()
    later = space.lock("a").acquire(EXCLUSIVE, 0, "two")  # the next change of the space
    assert later is not None
    assert space.generations(["a"]) == {"a": 1}
    space.lock("a").release(later)
    assert space.generations(["a"]) == {"a": 1}  # counted once


def test_a_holder_whose_process_ended_advances_every_generation(tmp_path: Path) -> None:
    space = FileLockSpace(tmp_path / "locks")
    hold = space.lock("a").acquire(EXCLUSIVE, 0, "one")
    assert hold is not None
    space.lock("a").release(hold, changed=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    state = json.loads(space.state_path.read_text())
    state["seq"] += 2
    state["locks"]["b"] = [
        {  # a hold of the process that ended: nobody says what it changed
            "seq": state["seq"] - 1,
            "owner": "dead-holder",
            "mode": "exclusive",
            "granted": True,
            "pid": gone.pid,
            "start": "1",
            "space": "another",
        },
        {  # a request of that process that was never granted
            "seq": state["seq"],
            "owner": "dead-waiter",
            "mode": "exclusive",
            "granted": False,
            "pid": gone.pid,
            "start": "1",
            "space": "another",
        },
    ]
    space.state_path.write_text(json.dumps(state))
    assert space.generations(["a", "b", "c"]) == {"a": 1, "b": 0, "c": 0}
    assert space.lock("c").acquire(SHARED, 0, "two") is not None  # a change prunes the entries
    assert space.generations(["a", "b", "c"]) == {"a": 2, "b": 1, "c": 1}
    assert json.loads(space.state_path.read_text())["lost"] == 1  # the granted entry only


def test_a_state_file_without_generations_is_read(tmp_path: Path) -> None:
    space = FileLockSpace(tmp_path / "locks")
    space.state_path.write_text('{"version": 1, "seq": 3, "locks": {}}')
    assert space.generations(["a"]) == {"a": 0}
    hold = space.lock("a").acquire(EXCLUSIVE, 0, "one")
    assert hold is not None
    space.lock("a").release(hold, changed=True)
    assert space.generations(["a"]) == {"a": 1}


# The scheme


def test_which_releases_count() -> None:
    assert counts_changes(worktree_key("w"), SHARED)
    assert counts_changes(worktree_key("w"), EXCLUSIVE)
    assert counts_changes(GLOBAL_KEY, EXCLUSIVE)
    assert not counts_changes(GLOBAL_KEY, SHARED)
    assert not counts_changes(file_key("w", "a.py"), EXCLUSIVE)


def test_an_edit_and_a_command_advance_the_generation_of_their_worktree_only() -> None:
    locking = scheme()
    w1, w2 = worktree_key("w1"), worktree_key("w2")
    start = {w1: 0, w2: 0, GLOBAL_KEY: 0}
    assert locking.generations(["w1", "w2"]) == start
    locking.edit_in_worktree("w1", path="a.py").release()
    assert locking.generations(["w1", "w2"]) == {**start, w1: 1}
    locking.run_command_in_worktree("w1").release()
    assert locking.generations(["w1", "w2"]) == {**start, w1: 2}
    locking.run_command_in_worktrees(["w1", "w2"]).release()
    assert locking.generations(["w1", "w2"]) == {w1: 3, w2: 1, GLOBAL_KEY: 0}
    # The file lock of the edit is not counted.
    assert locking.space.generations([file_key("w1", "a.py")]) == {file_key("w1", "a.py"): 0}


def test_an_edit_outside_and_a_command_that_writes_outside_advance_the_global_generation() -> None:
    locking = scheme()
    locking.edit_outside_worktree().release()
    assert locking.generations([]) == {GLOBAL_KEY: 1}
    locking.run_command_in_worktree("w1", writes_outside=True).release()
    assert locking.generations(["w1"]) == {worktree_key("w1"): 1, GLOBAL_KEY: 2}


def test_a_lease_given_back_without_a_run_advances_nothing() -> None:
    locking = scheme()
    locking.run_command_in_worktree("w1", writes_outside=True).release(changed=False)
    locking.edit_in_worktree("w1").release(changed=False)
    assert locking.generations(["w1"]) == {worktree_key("w1"): 0, GLOBAL_KEY: 0}
    with locking.edit_in_worktree("w1"):
        pass  # the end of a `with` block counts as a run
    assert locking.generations(["w1"]) == {worktree_key("w1"): 1, GLOBAL_KEY: 0}


def test_a_lock_call_that_times_out_advances_nothing() -> None:
    locking = scheme()
    outside = locking.edit_outside_worktree(owner="outside")
    with pytest.raises(LockTimeoutError):
        # It gets the worktree, waits for the global lock in vain, and gives the worktree back.
        locking.run_command_in_worktree("w1", timeout=0.05, owner="command")
    outside.release(changed=False)
    assert locking.generations(["w1"]) == {worktree_key("w1"): 0, GLOBAL_KEY: 0}


def test_schemes_on_one_space_share_the_generations() -> None:
    space = ThreadLockSpace()
    one, two = RWLockingScheme(space), RWLockingScheme(space)  # two sessions of a process
    one.edit_in_worktree("w1").release()
    assert two.generations(["w1"]) == {worktree_key("w1"): 1, GLOBAL_KEY: 0}


def test_the_scheme_without_locks_has_no_generations() -> None:
    off = NoLockingScheme()
    assert off.generations(["w1"]) == {}
    off.run_command_in_worktree("w1").release()
    assert off.generations(["w1"]) == {}
