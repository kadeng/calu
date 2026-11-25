"""The locks of a session's tool calls (`calu.call_locks`, PLAN.md D276).

`CallLocks` on its own with stand-in checkouts, and a session with locking on (the fake runner).
Locking uses the "files" backend with its directory below `tmp_path`, so tests do not share
locks. Threads of this process only, no output.
"""

import time
from pathlib import Path

import anyio
import pytest

from calu.call_locks import CallLocks, shared_space
from calu.checkers import Step
from calu.checkouts import Checkout
from calu.config import Config, LockingConfig
from calu.locking import GLOBAL_KEY, LockTimeouts, RWLockingScheme, file_key, worktree_key
from calu.protocol.messages import ChatSendParams, ToolResult, TurnResult
from tests.unit.helpers import make_session

pytestmark = pytest.mark.anyio


def locking(tmp_path: Path, **values: object) -> LockingConfig:
    """Return locking settings with the "files" backend below `tmp_path`.

    Args:
        tmp_path: The test's directory.
        **values: Other `LockingConfig` fields; locking is on unless `enabled` says otherwise.

    Returns:
        The settings.
    """
    settings: dict[str, object] = {
        "enabled": True,
        "backend": "files",
        "directory": str(tmp_path / "locks"),
    }
    settings.update(values)
    return LockingConfig.model_validate(settings)


class Tree:
    """Stand-in checkouts: the main checkout at `root`, a worktree at `root/wt`."""

    def __init__(self, root: Path) -> None:
        """Create both checkouts' directories.

        Args:
            root: The main checkout's root.
        """
        (root / "wt").mkdir(parents=True, exist_ok=True)
        self.main = Checkout(root=root.resolve(), label=".", main=True)
        self.worktree = Checkout(root=(root / "wt").resolve(), label="wt")

    def step_checkouts(self, step: Step) -> list[Checkout]:
        """The checkouts of a command: its working directory's, then the worktree's for `cd wt`.

        Args:
            step: The tool call.

        Returns:
            The checkouts.
        """
        found = [self.of(Path(step.cwd or self.main.root)) or self.main]
        if "cd wt" in str(step.tool_input.get("command", "")) and self.worktree not in found:
            found.append(self.worktree)
        return found

    def of(self, path: Path) -> Checkout | None:
        """The checkout a path belongs to.

        Args:
            path: The path.

        Returns:
            The worktree, the main checkout, or None outside both.
        """
        resolved = path.resolve()
        if resolved.is_relative_to(self.worktree.root):
            return self.worktree
        if resolved.is_relative_to(self.main.root):
            return self.main
        return None


def call_locks(tmp_path: Path, tree: Tree, **values: object) -> CallLocks:
    """Return the call locks of one stand-in session.

    Args:
        tmp_path: The test's directory (the lock directory goes below it).
        tree: The checkouts.
        **values: `LockingConfig` fields.

    Returns:
        The call locks.
    """
    return CallLocks(locking(tmp_path, **values), tree.step_checkouts, tree.of, tree.main.root)


CWD = "/"
"""The working directory of the calls; the stand-in checkouts ignore a directory outside them
and use the main checkout (`Tree.step_checkouts`)."""


def bash(command: str, call: str, agent: str | None = None) -> Step:
    """Return a Bash call in the main checkout.

    Args:
        command: The command.
        call: The tool call's id.
        agent: The subagent's id, or None for the main agent.

    Returns:
        The step.
    """
    return Step(
        cwd=CWD,
        tool_name="Bash",
        tool_input={"command": command},
        tool_use_id=call,
        agent_id=agent,
    )


def edit(path: str, call: str, agent: str | None = None) -> Step:
    """Return an Edit call.

    Args:
        path: The file, absolute or relative to the main checkout.
        call: The tool call's id.
        agent: The subagent's id, or None for the main agent.

    Returns:
        The step.
    """
    return Step(
        cwd=CWD,
        tool_name="Edit",
        tool_input={"file_path": path, "old_string": "a", "new_string": "b"},
        tool_use_id=call,
        agent_id=agent,
    )


async def test_a_command_locks_its_checkouts_and_an_edit_its_checkout_and_file(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    main, wt = str(tree.main.root), str(tree.worktree.root)
    command = bash("cd wt && make", "c1")
    locks.issue(command)
    assert await locks.take(command, None) == ""
    assert locks.holding() == {
        "c1": (
            (worktree_key(main), "exclusive"),
            (worktree_key(wt), "exclusive"),
            (GLOBAL_KEY, "shared"),
        )
    }
    locks.call_ended("c1")  # an edit in `wt` would wait for the command
    change = edit(str(tree.worktree.root / "x.py"), "e1")
    locks.issue(change)
    assert await locks.take(change, None) == ""
    assert locks.holding() == {
        "e1": (
            (worktree_key(wt), "shared"),
            (file_key(wt, str(tree.worktree.root / "x.py")), "exclusive"),
        )
    }


async def test_an_edit_outside_every_checkout_takes_the_global_lock(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    step = edit(str(tmp_path / "elsewhere.txt"), "e1")
    locks.issue(step)
    assert await locks.take(step, None) == ""
    assert locks.holding() == {"e1": ((GLOBAL_KEY, "exclusive"),)}


async def test_other_tools_take_no_lock(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    step = Step(cwd=CWD, tool_name="Read", tool_input={"file_path": "x.py"}, tool_use_id="r1")
    locks.issue(step)
    assert await locks.take(step, None) == ""
    assert locks.holding() == {}
    assert locks.scheme.open_tickets() == {}


async def test_a_call_that_does_not_get_its_locks_is_denied_and_holds_nothing(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    first = call_locks(tmp_path, tree)
    second = call_locks(tmp_path, tree, command_wait_s=0.1)  # another session, same directory
    running = bash("make", "c1")
    first.issue(running)
    assert await first.take(running, None) == ""
    waiting = bash("make test", "c2")
    second.issue(waiting)
    reason = await second.take(waiting, None)
    assert reason.startswith("Not run:") and "the worktree `.`" in reason
    assert "can be repeated" in reason
    assert second.holding() == {} and second.scheme.open_tickets() == {}
    first.call_ended("c1")
    second.issue(bash("make test", "c3"))
    assert await second.take(bash("make test", "c3"), None) == ""


async def test_the_hook_budget_bounds_the_wait(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    first, second = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    assert await first.take(bash("make", "c1"), None) == ""
    started = time.monotonic()
    reason = await second.take(bash("make", "c2"), 0.1)  # the configured wait is 600 s
    assert reason.startswith("Not run:")
    assert time.monotonic() - started < 5


async def test_a_cancelled_wait_leaves_no_lock_and_no_ticket(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    first, second = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    assert await first.take(bash("make", "c1"), None) == ""
    step = bash("make test", "c2")
    second.issue(step)
    with anyio.move_on_after(0.2):
        await second.take(step, None)
    first.call_ended("c1")
    await anyio.sleep(0.2)
    assert second.holding() == {} and second.scheme.open_tickets() == {}
    space = shared_space(locking(tmp_path))
    assert all(state.holders == () and state.waiters == () for state in space.snapshot() or ())


async def test_calls_of_one_agent_get_their_locks_in_issue_order(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    first, second = bash("make", "c1"), edit(str(tree.main.root / "x.py"), "e2")
    locks.issue(first)
    locks.issue(second)
    granted: list[str] = []

    async def take(step: Step) -> None:
        assert await locks.take(step, None) == ""
        granted.append(step.tool_use_id or "")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(take, second)  # its check finished first
        await anyio.sleep(0.1)
        assert granted == []  # it waits for the earlier command
        tasks.start_soon(take, first)
        await anyio.sleep(0.2)
        assert granted == ["c1"]  # the edit waits for the command's worktree lock
        locks.call_ended("c1")
    assert granted == ["c1", "e2"]


async def test_a_call_gives_its_locks_back_and_takes_them_again(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    step = bash("make", "c1")
    locks.issue(step)
    assert await locks.take(step, None) == ""
    locks.release(step)  # before a person decides about it
    assert locks.holding() == {}
    assert await locks.take(step, None) == ""  # after the person allowed it, without a ticket
    assert "c1" in locks.holding()
    locks.close()
    assert locks.holding() == {}


async def test_with_locking_off_the_same_calls_take_no_lock(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    blocker = call_locks(tmp_path, tree)
    assert await blocker.take(bash("make", "c1"), None) == ""
    off = call_locks(tmp_path, tree, enabled=False)
    step = bash("make test", "c2")
    off.issue(step)
    assert await off.take(step, None) == ""  # the worktree is busy, but nothing is locked
    assert off.holding() == {"c2": ()}
    off.call_ended("c2")
    assert off.holding() == {}


async def test_a_lock_directory_that_cannot_be_used_denies_every_command_and_edit(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    (tmp_path / "not-a-directory").write_text("")
    locks = CallLocks(
        locking(tmp_path, directory=str(tmp_path / "not-a-directory" / "locks")),
        tree.step_checkouts,
        tree.of,
        tree.main.root,
    )
    step = bash("make", "c1")
    locks.issue(step)
    reason = await locks.take(step, None)
    assert reason.startswith("Not run: Calu's locking cannot use its directory")
    read = Step(cwd=CWD, tool_name="Read", tool_input={"file_path": "x"}, tool_use_id="r1")
    assert await locks.take(read, None) == ""


async def test_a_session_gives_back_its_locks_and_denies_a_command_whose_worktree_is_busy(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "a.txt").write_text("old\n", encoding="utf-8")
    config = Config(locking=locking(tmp_path, command_wait_s=0.2))
    turns = [
        [
            {"tool": "Bash", "input": {"command": "make"}, "result": "built"},
            {
                "tool": "Edit",
                "input": {"file_path": "a.txt", "old_string": "old", "new_string": "new"},
                "result": "ok",
            },
            {"text": "Done."},
        ],
        [
            {"tool": "Bash", "input": {"command": "make test"}, "result": "never"},
            {"text": "Blocked."},
        ],
    ]
    session, rec, _store = make_session(workspace, turns, config)
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(1, 1, 1, 1))
    async with anyio.create_task_group() as tasks:
        await session.start(tasks)
        await session.send_chat(ChatSendParams(text="Build it"))
        with anyio.fail_after(10):
            while len(rec.of(TurnResult)) < 1:
                await anyio.sleep(0.02)
        assert session.locks.holding() == {}
        assert [r.is_error for r in rec.of(ToolResult)] == [False, False]
        # Another session's command runs in the same worktree.
        busy = other.run_command_in_worktree(str(session.checkouts.main.root), owner="other")
        await session.send_chat(ChatSendParams(text="Test it"))
        with anyio.fail_after(10):
            while len(rec.of(TurnResult)) < 2:
                await anyio.sleep(0.02)
        busy.release()
        await session.stop()
    denied = rec.of(ToolResult)[-1]
    assert denied.is_error and "Not run: this call waited" in denied.content
    assert session.locks.holding() == {}
