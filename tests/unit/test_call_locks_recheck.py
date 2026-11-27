"""Tests of the second check of a command (locking.md section 9, PLAN.md D276).

With locking on, a command's check runs before the command has its locks. When another call
ended in its worktree in between, the command is checked again while it holds its locks.

The first part tests `CallLocks` on its own, with stand-in checkouts. The second part tests a
session with the fake runner. Its command checker reads the file that the command runs.

Locking uses the "files" backend with a directory below `tmp_path`, so these tests share no
locks with other tests. One test uses the "process" backend. Its lock space is shared by every
test of the worker process; its worktree is a path below `tmp_path` that no other test uses.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

import calu.session as session_module
from calu.call_locks import OUTSIDE_CHANGED, CallLocks, shared_space
from calu.checkers import Step
from calu.config import Config, LockingConfig
from calu.locking import GLOBAL_KEY, LockTimeouts, RWLockingScheme
from calu.protocol.messages import CheckResult, Log, ToolResult
from calu.session import Session
from calu.store import MemorySessionStore
from calu.testing import fake_models
from tests.unit.helpers import Recorder, make_session
from tests.unit.test_call_locks import Tree, bash, call_locks, edit, locking

pytestmark = pytest.mark.anyio

MAIN = "the worktree `.`"
"""How `CallLocks.stale` names the main checkout of `Tree`."""

MARKER = "os.environ"
"""The text that makes `reading_checker` deny: the file prints the environment."""


async def checked(locks: CallLocks, tree: Tree, step: Step) -> None:
    """Begin the check of a command as the session does: its ticket, then its generations.

    Args:
        locks: The session's call locks.
        tree: The checkouts.
        step: The command.
    """
    locks.issue(step)
    locks.check_begins(step, tree.step_checkouts(step))


async def ran(locks: CallLocks, step: Step) -> None:
    """Let a call of another session run from its check to its tool result.

    Args:
        locks: That session's call locks.
        step: The call.
    """
    locks.issue(step)
    assert await locks.take(step, None) == ""
    locks.call_ended(step.tool_use_id or "")


# CallLocks: what counts as a change


async def test_a_command_is_not_stale_when_no_call_ended_since_its_check(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    await checked(locks, tree, step)
    assert locks.stale(step) == ()  # it holds no locks yet
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()
    locks.close()


async def test_an_edit_and_a_command_that_ended_in_the_worktree_make_a_command_stale(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)  # two sessions
    others = (edit(str(tree.main.root / "hello.py"), "e1", "sub"), bash("make", "c2"))
    for number, between in enumerate(others):
        step = bash("python hello.py", f"c1-{number}")
        await checked(locks, tree, step)
        await ran(other, between)
        assert await locks.take(step, None) == ""
        assert locks.stale(step) == (MAIN,)
        locks.call_ended(step.tool_use_id or "")


async def test_a_call_that_ended_in_another_worktree_leaves_a_command_fresh(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")  # in the main checkout
    await checked(locks, tree, step)
    await ran(other, edit(str(tree.worktree.root / "x.py"), "e1", "sub"))
    elsewhere = Step(cwd=str(tree.worktree.root), tool_name="Bash", tool_input={"command": "make"})
    elsewhere.tool_use_id = "c2"
    await ran(other, elsewhere)
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()
    locks.call_ended("c1")
    both = bash("cd wt && make", "c3")  # this one locks the worktree as well
    await checked(locks, tree, both)
    await ran(other, edit(str(tree.worktree.root / "x.py"), "e4", "sub"))
    assert await locks.take(both, None) == ""
    assert locks.stale(both) == ("the worktree `wt`",)
    locks.close()


async def test_a_write_outside_the_worktrees_makes_every_command_stale(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    await checked(locks, tree, step)
    await ran(other, edit(str(tmp_path / "elsewhere.txt"), "e1", "sub"))
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == (OUTSIDE_CHANGED,)
    locks.call_ended("c1")
    again = bash("python hello.py", "c2")
    await checked(locks, tree, again)
    installs = Step(cwd=str(tree.worktree.root), tool_name="Bash", tool_input={"command": "x"})
    installs.tool_use_id = "c3"
    other.mark_writes_outside(installs)  # the command checker said so
    await ran(other, installs)
    assert await locks.take(again, None) == ""
    assert locks.stale(again) == (OUTSIDE_CHANGED,)
    locks.close()


async def test_locks_given_back_without_a_run_leave_a_command_fresh(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    await checked(locks, tree, step)
    held = bash("make", "c2")
    other.issue(held)
    assert await other.take(held, None) == ""
    other.release(held)  # a person decides about it: it did not run
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()
    locks.release(step)  # the same for the command itself
    assert locks.stale(step) == ()
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()
    locks.close()
    other.close()


async def test_after_a_second_check_only_a_later_change_makes_the_command_stale(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    await checked(locks, tree, step)
    await ran(other, edit(str(tree.main.root / "hello.py"), "e1", "sub"))
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == (MAIN,)
    locks.checked_again(step)
    assert locks.stale(step) == ()
    locks.release(step)  # e.g. an escalation came up; the command did not run
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()  # nothing ended since the second check
    locks.release(step)
    await ran(other, edit(str(tree.main.root / "hello.py"), "e2", "sub"))
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == (MAIN,)
    locks.close()


async def test_a_command_without_remembered_generations_counts_as_changed(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    locks = call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    locks.issue(step)  # `check_begins` was not called
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == (MAIN, OUTSIDE_CHANGED)
    locks.close()


async def test_an_edit_is_never_stale(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    locks, other = call_locks(tmp_path, tree), call_locks(tmp_path, tree)
    step = edit(str(tree.main.root / "hello.py"), "e1")
    locks.issue(step)
    locks.check_begins(step, [tree.main])
    await ran(other, bash("make", "c2"))
    assert await locks.take(step, None) == ""
    assert locks.stale(step) == ()
    locks.close()


async def test_with_locking_off_nothing_is_remembered_and_nothing_is_stale(
    tmp_path: Path,
) -> None:
    tree = Tree(tmp_path / "repo")
    off, other = call_locks(tmp_path, tree, enabled=False), call_locks(tmp_path, tree)
    step = bash("python hello.py", "c1")
    await checked(off, tree, step)
    assert off._marks == {}
    await ran(other, edit(str(tree.main.root / "hello.py"), "e1", "sub"))
    assert await off.take(step, None) == ""
    assert off.stale(step) == ()
    assert off._fresh == {}
    off.close()


async def test_the_sessions_of_one_process_see_each_others_calls_end(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "repo")
    settings = LockingConfig(enabled=True, backend="process")
    one = CallLocks(settings, tree.step_checkouts, tree.of, tree.main.root)
    two = CallLocks(settings, tree.step_checkouts, tree.of, tree.main.root)
    step = bash("python hello.py", "c1")
    await checked(one, tree, step)
    await ran(two, edit(str(tree.main.root / "hello.py"), "e1", "sub"))
    assert await one.take(step, None) == ""
    assert one.stale(step) == (MAIN,)
    one.close()


# A session: the second check


def reading_checker(
    path: Path,
    seen: list[str],
    during: Callable[[int], Awaitable[None]],
    outside_at: int = 0,
) -> Model:
    """Return a command checker that reads `path`, as a checker reads the file a command runs.

    It answers DENY when the file holds `MARKER`, else PASS. Between reading the file and
    answering it calls `during`, so that a test can let something happen at that moment.

    Args:
        path: The file to read.
        seen: Gets the file's text, once per call of the checker.
        during: Called with the number of the checker's call (from 1) after the file was read.
        outside_at: The number of the call that also answers that the command writes outside
            its worktree; 0 for none.

    Returns:
        A `FunctionModel` named "reading-checker".
    """

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Read the file and decide by its text.

        Args:
            messages: The message history (unused).
            info: Agent information; its first output tool receives the decision.

        Returns:
            A response that calls the output tool.
        """
        text = path.read_text(encoding="utf-8")
        seen.append(text)
        await during(len(seen))
        denied = MARKER in text
        outside = {"writes_outside_worktree": True} if len(seen) == outside_at else {}
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "decision": "DENY" if denied else "PASS",
                        "reason": "hello.py prints every environment variable." if denied else "",
                        "step_summary": "Runs hello.py after ten seconds.",
                        "comment": "",
                        **outside,
                    },
                )
            ]
        )

    return FunctionModel(answer, model_name="reading-checker")


class Scene:
    """A project with `hello.py`, a session on it, and the main agent's command.

    Attributes:
        hello: The file the command runs.
        seen: The text of `hello.py` at each call of the command checker.
        started: Set when the first check has read the file (see `during`).
        go: Lets the first check answer (see `during`).
        session: The session.
        rec: Its events.
        store: Its store.
        command: `sleep 10 && python hello.py` of the main agent, tool call "t1".
    """

    def __init__(
        self,
        tmp_path: Path,
        config: Config,
        during: Callable[[int], Awaitable[None]] | None = None,
        outside_at: int = 0,
    ) -> None:
        """Create the project and the session; call it inside the event loop.

        Args:
            tmp_path: The test's directory.
            config: The session's configuration.
            during: What happens while the command checker runs, between its reading of
                `hello.py` and its answer; it gets the number of the checker's call. None:
                the first call sets `started` and waits for `go`.
            outside_at: The number of the checker's call that also answers that the command
                writes outside its worktree; 0 for none.
        """
        workspace = tmp_path / "project"
        workspace.mkdir()
        self.hello = workspace / "hello.py"
        self.hello.write_text('print("hello")\n', encoding="utf-8")
        self.seen: list[str] = []
        self.started, self.go = anyio.Event(), anyio.Event()
        models = fake_models()
        models["command"] = reading_checker(
            self.hello, self.seen, during or self._stop_first, outside_at
        )
        made: tuple[Session, Recorder, MemorySessionStore] = make_session(
            workspace, [], config, checker_models=models
        )
        self.session, self.rec, self.store = made
        self.command = Step(
            tool_name="Bash",
            tool_input={"command": "sleep 10 && python hello.py"},
            tool_use_id="t1",
            cwd=str(workspace),
        )

    async def _stop_first(self, call: int) -> None:
        """Stop the first check after it read the file: set `started`, wait for `go`.

        Args:
            call: The number of the checker's call; every call but the first goes on at once.
        """
        if call == 1:
            self.started.set()
            await self.go.wait()

    async def subagent_rewrites_hello(self) -> None:
        """Let a subagent's edit of `hello.py` be checked, land and end.

        The file then prints every environment variable.
        """
        change = Step(
            tool_name="Edit",
            tool_input={
                "file_path": str(self.hello),
                "old_string": 'print("hello")',
                "new_string": "import os\nprint(os.environ)",
            },
            tool_use_id="e1",
            cwd=str(self.hello.parent),
            agent_id="sub",
        )
        assert (await self.session.pre_tool_use(change)).verdict.value in ("pass", "allow")
        self.hello.write_text("import os\nprint(os.environ)\n", encoding="utf-8")
        await self.session._on_runner_event(
            ToolResult(tool_use_id="e1", is_error=False, content="ok")
        )

    def checks(self) -> list[CheckResult]:
        """Return the `check.result` events of the command.

        Returns:
            The events, oldest first.
        """
        return [event for event in self.rec.of(CheckResult) if event.tool_use_id == "t1"]

    def logs(self) -> list[str]:
        """Return the messages of the `log` events.

        Returns:
            The messages, oldest first.
        """
        return [event.message for event in self.rec.of(Log)]


async def test_a_command_whose_file_a_subagent_rewrote_is_checked_again_and_denied(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path, Config(locking=locking(tmp_path)))
    results = []

    async def hook() -> None:
        results.append(await scene.session.pre_tool_use(scene.command))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(hook)
        with anyio.fail_after(10):
            await scene.started.wait()  # the first check has read hello.py
        await scene.subagent_rewrites_hello()
        scene.go.set()  # the first check answers PASS for what it read
    assert results[0].verdict.value == "deny"
    assert "checked again" in results[0].reason
    assert "prints every environment variable" in results[0].reason
    # The first check saw the old file, the second the rewritten one.
    assert [MARKER in text for text in scene.seen] == [False, True]
    assert [event.decision.value for event in scene.checks()] == ["PASS", "DENY"]
    assert scene.session.locks.holding() == {}  # the denied command gave its locks back
    assert scene.session.locks.scheme.open_tickets() == {}
    again = [line for line in scene.logs() if "is checked again" in line]
    assert again == [
        "Bash of the main agent: another call ended in the worktree `.` after the check of "
        "this command began; the command is checked again while it holds its locks"
    ]
    # The record: both checks' runs on the step, and the second decision.
    step = next(s for s in scene.store.steps.values() if s.row.tool_use_id == "t1")
    assert [run.decision for run in step.checker_runs] == ["pass", "deny"]
    assert step.decision is not None and step.decision.decision == "deny"
    assert "checked again" in step.decision.reason
    turn = scene.session.context.current
    assert turn is not None
    summaries = [(s.decision, s.summary) for s in turn.steps if s.tool_name == "Bash"]
    assert summaries == [
        ("PASS", "Runs hello.py after ten seconds."),
        (
            "DENY",
            "Checked again after another call ended in the worktree `.`: "
            "Runs hello.py after ten seconds.",
        ),
    ]


async def test_a_command_is_checked_once_when_nothing_changed(tmp_path: Path) -> None:
    scene = Scene(tmp_path, Config(locking=locking(tmp_path)))
    scene.go.set()
    result = await scene.session.pre_tool_use(scene.command)
    assert result.verdict.value == "pass"
    assert len(scene.seen) == 1 and len(scene.checks()) == 1
    assert "t1" in scene.session.locks.holding()
    assert not [line for line in scene.logs() if "checked again" in line]
    scene.session.locks.close()


async def test_with_locking_off_the_command_is_checked_once_whatever_lands(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path, Config())  # locking is off by default
    results = []

    async def hook() -> None:
        results.append(await scene.session.pre_tool_use(scene.command))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(hook)
        with anyio.fail_after(10):
            await scene.started.wait()
        await scene.subagent_rewrites_hello()
        scene.go.set()
    assert results[0].verdict.value == "pass"  # as before the second check existed
    assert len(scene.seen) == 1 and len(scene.checks()) == 1
    assert scene.session.locks.holding() == {"t1": ()}
    assert not [line for line in scene.logs() if "checked again" in line]
    scene.session.locks.close()


async def test_a_change_in_another_worktree_causes_no_second_check(tmp_path: Path) -> None:
    config = Config(locking=locking(tmp_path))
    scene = Scene(tmp_path, config)
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(5, 5, 5, 5))
    results = []

    async def hook() -> None:
        results.append(await scene.session.pre_tool_use(scene.command))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(hook)
        with anyio.fail_after(10):
            await scene.started.wait()
        # Another session's edit and command end in a worktree the command does not lock.
        elsewhere = str(tmp_path / "another-worktree")
        other.edit_in_worktree(elsewhere, path=f"{elsewhere}/x.py").release()
        other.run_command_in_worktree(elsewhere).release()
        scene.go.set()
    assert results[0].verdict.value == "pass"
    assert len(scene.seen) == 1 and len(scene.checks()) == 1
    scene.session.locks.close()


async def test_a_second_check_that_approves_lets_the_command_run_with_its_locks(
    tmp_path: Path,
) -> None:
    config = Config(locking=locking(tmp_path))
    scene = Scene(tmp_path, config)
    root = str(scene.session.checkouts.main.root)
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(5, 5, 5, 5))
    results = []

    async def hook() -> None:
        results.append(await scene.session.pre_tool_use(scene.command))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(hook)
        with anyio.fail_after(10):
            await scene.started.wait()
        other.run_command_in_worktree(root).release()  # another session's command; no change
        scene.go.set()
    assert results[0].verdict.value == "pass"
    assert len(scene.seen) == 2
    assert [event.decision.value for event in scene.checks()] == ["PASS", "PASS"]
    assert "t1" in scene.session.locks.holding()
    step = next(s for s in scene.store.steps.values() if s.row.tool_use_id == "t1")
    assert len(step.checker_runs) == 2
    assert step.decision is not None and step.decision.decision == "pass"
    scene.session.locks.close()


async def test_a_second_check_that_finds_a_write_outside_takes_the_global_lock_exclusively(
    tmp_path: Path,
) -> None:
    config = Config(locking=locking(tmp_path))
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(5, 5, 5, 5))
    root: list[str] = []

    async def during(call: int) -> None:
        """During the first check, another session's command ends in the worktree.

        Args:
            call: The number of the checker's call.
        """
        if call == 1:
            other.run_command_in_worktree(root[0]).release()

    scene = Scene(tmp_path, config, during, outside_at=2)
    root.append(str(scene.session.checkouts.main.root))
    assert (await scene.session.pre_tool_use(scene.command)).verdict.value == "pass"
    # The locks were given back and taken again; nothing ended in between, so no third check.
    assert len(scene.seen) == 2
    assert scene.session.locks.holding()["t1"][-1] == (GLOBAL_KEY, "exclusive")
    scene.session.locks.close()


async def test_a_hook_cancelled_during_the_second_check_leaves_no_lock(tmp_path: Path) -> None:
    config = Config(locking=locking(tmp_path))
    space = shared_space(config.locking)
    other = RWLockingScheme(space, LockTimeouts(5, 5, 5, 5))
    root: list[str] = []
    second = anyio.Event()

    async def during(call: int) -> None:
        """Let a command end in the worktree during the first check; never end the second.

        Args:
            call: The number of the checker's call.
        """
        if call == 1:
            other.run_command_in_worktree(root[0]).release()
        else:
            second.set()
            await anyio.sleep_forever()

    scene = Scene(tmp_path, config, during)
    root.append(str(scene.session.checkouts.main.root))
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tasks:

            async def hook() -> None:
                await scene.session.pre_tool_use(scene.command)

            tasks.start_soon(hook)
            with anyio.fail_after(10):
                await second.wait()
            assert "t1" in scene.session.locks.holding()  # the second check runs under the locks
            scope.cancel()  # an interrupt cancels the hook
    assert scene.session.locks.holding() == {}
    assert all(state.holders == () and state.waiters == () for state in space.snapshot() or ())
    assert scene.session.locks.scheme.open_tickets() == {}


async def test_a_second_check_without_time_left_asks_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(locking=locking(tmp_path))
    scene = Scene(tmp_path, config)
    root = str(scene.session.checkouts.main.root)
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(5, 5, 5, 5))
    results = []

    async def hook() -> None:
        results.append(await scene.session.pre_tool_use(scene.command))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(hook)
        with anyio.fail_after(10):
            await scene.started.wait()
        other.edit_in_worktree(root, path=str(scene.hello)).release()
        # The hook's time is used up when the first check answers.
        monkeypatch.setattr(session_module, "HOOK_TIMEOUT_S", 0.0)
        scene.go.set()
    assert results[0].verdict.value == "ask"
    assert "The second check did not finish" in results[0].reason
    assert len(scene.seen) == 1  # no second check ran, and the command does not run unchecked
    assert scene.session.locks.holding() == {}
    scene.session.locks.close()


async def test_a_command_a_person_allowed_runs_without_a_second_check_and_the_log_says_so(
    tmp_path: Path,
) -> None:
    config = Config(locking=locking(tmp_path))
    scene = Scene(tmp_path, config)
    root = str(scene.session.checkouts.main.root)
    other = RWLockingScheme(shared_space(config.locking), LockTimeouts(5, 5, 5, 5))
    scene.go.set()
    command = scene.command
    assert (await scene.session.pre_tool_use(command)).verdict.value == "pass"
    answers = []

    async def asked() -> None:
        answers.append(await scene.session.ask(command))  # Claude Code asks about the call

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(asked)
        with anyio.fail_after(10):
            while not scene.session.desk.escalations:
                await anyio.sleep(0.01)
        assert scene.session.locks.holding() == {}  # no locks while a person decides
        other.edit_in_worktree(root, path=str(scene.hello)).release()
        escalation_id = next(iter(scene.session.desk.escalations))
        await scene.session.decide_escalation(escalation_id, "allow", "", "tester")
    assert answers[0].allow
    assert len(scene.seen) == 1
    warned = [line for line in scene.logs() if "runs without another check" in line]
    assert warned == [
        "Bash of the main agent: another call ended in the worktree `.` after the last check "
        "of this command. A person allowed the command, so it runs without another check"
    ]
    scene.session.locks.close()
