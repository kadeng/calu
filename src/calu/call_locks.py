"""The locks of one session's tool calls (agent/records/notes/locking.md, section 9).

`CallLocks` connects a session to `calu.locking`. It decides which locks a tool call takes,
issues the call's ticket when its check begins, takes the locks of an approved call in a worker
thread (with a bounded wait that a cancellation ends), and gives them back when the call ends.
With locking off (`LockingConfig.enabled` false) it makes the same calls on `NoLockingScheme`,
which takes no lock and never waits.

    tool                                    locks
    Bash, Monitor (SHELL_TOOLS)             a command in the checkouts of its working directory
                                            and its `cd` targets (`Session.step_checkouts`);
                                            the global lock exclusive when the command checker
                                            says it writes code or configuration outside them
                                            (`mark_writes_outside`), else shared
    Edit, Write, MultiEdit, NotebookEdit    an edit in the checkout of its file, with the file's
                                            lock; a file outside every checkout: an edit outside
    every other tool                        none

The locks live in a space that every session of the process shares (`shared_space`): the
threads of this process ("process"), or a state file that the other Calu processes of the user
share ("files"). A worktree is named by the resolved path of its root.
"""

import logging
import os
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import anyio.to_thread

from calu.checkers.model import Step
from calu.checkers.policy import EDIT_TOOLS, SHELL_TOOLS
from calu.checkouts import Checkout
from calu.config import LockingConfig
from calu.locking import (
    GLOBAL_KEY,
    FileLockSpace,
    Lease,
    LockCancelledError,
    LockError,
    LockingScheme,
    LockSpace,
    LockTimeoutError,
    LockTimeouts,
    NoLockingScheme,
    RWLockingScheme,
    ThreadLockSpace,
    Ticket,
    file_key,
    worktree_key,
)

log = logging.getLogger(__name__)

MAX_WAIT_S = 3600.0
"""The longest wait a call may get, for commands and edits alike (`LockTimeouts` maxima)."""

WAIT_THREADS = 64
"""How many calls of one session may wait for their locks at the same time (worker threads)."""

_spaces: dict[str, LockSpace] = {}
_spaces_mutex = threading.Lock()


def default_directory() -> Path:
    """Return the directory of the "files" backend when `LockingConfig.directory` is blank.

    Returns:
        `$XDG_STATE_HOME/calu/locks`, or `~/.local/state/calu/locks`; not created here.
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "calu" / "locks"


def shared_space(config: LockingConfig) -> LockSpace:
    """Return the lock space of this process for `config`, created on first use.

    All sessions of the process share it: one `ThreadLockSpace` for the "process" backend, one
    `FileLockSpace` per directory for the "files" backend.

    Args:
        config: The locking settings.

    Returns:
        The space.

    Raises:
        OSError: The directory of the "files" backend cannot be created, belongs to another
            user or can be written by others, or `/proc` cannot be read (`FileLockSpace`).
    """
    if config.backend == "process":
        name = "process"
    else:
        directory = Path(config.directory).expanduser() if config.directory else None
        name = f"files:{(directory or default_directory()).resolve()}"
    with _spaces_mutex:
        space = _spaces.get(name)
        if space is None:
            if name == "process":
                space = ThreadLockSpace()
            else:
                space = FileLockSpace(name.removeprefix("files:"))
            _spaces[name] = space
        return space


def needs_locks(step: Step) -> bool:
    """Tell whether a tool call takes locks at all: a command or an edit.

    Args:
        step: The tool call.

    Returns:
        True for `SHELL_TOOLS` and `EDIT_TOOLS`.
    """
    return step.tool_name in SHELL_TOOLS or step.tool_name in EDIT_TOOLS


@dataclass
class _Plan:
    """Which lock call a tool call makes, and how to name its locks to Claude.

    Attributes:
        kind: "command", "edit" (in a worktree) or "outside" (an edit outside every checkout).
        worktrees: The roots of the checkouts, resolved, as the scheme's worktree ids.
        path: The edited file, resolved, for an edit.
        labels: Lock key -> how a message names it.
        writes_outside: For a command: it writes code or configuration outside its checkouts,
            so it takes the global lock exclusively.
    """

    kind: str
    worktrees: tuple[str, ...] = ()
    path: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    writes_outside: bool = False


class CallLocks:
    """Issues tickets, takes and gives back the locks of the tool calls of one session.

    Every method may be called for every tool call; a call that takes no lock (`needs_locks`)
    is ignored. Tickets and leases are kept by the tool call's id; a call without one takes no
    lock. The owner of a call's locks is "<tool call id>@<token of this object>": the lock space
    is shared with other sessions and processes, and tool call ids are unique only per session.

    Attributes:
        config: The locking settings the session started with.
        scheme: `RWLockingScheme` on the shared space when locking is on, else `NoLockingScheme`.
    """

    def __init__(
        self,
        config: LockingConfig,
        step_checkouts: Callable[[Step], Sequence[Checkout]],
        checkout_of: Callable[[Path], Checkout | None],
        workspace: Path,
    ) -> None:
        """Choose the scheme for `config`; with locking on, open the shared space.

        A space that cannot be opened (the directory of the "files" backend cannot be created)
        does not fail the session: every call that needs locks is then denied with the reason.

        Args:
            config: The locking settings.
            step_checkouts: The session's checkouts of a tool call (`Session.step_checkouts`).
            checkout_of: The checkout a path belongs to (`Checkouts.of`), or None outside
                every checkout.
            workspace: The session directory, for relative paths.
        """
        self.config = config
        self._step_checkouts = step_checkouts
        self._checkout_of = checkout_of
        self._workspace = workspace
        timeouts = LockTimeouts(config.command_wait_s, MAX_WAIT_S, config.edit_wait_s, MAX_WAIT_S)
        self._broken = ""
        self.scheme: LockingScheme = NoLockingScheme(timeouts)
        if config.enabled:
            try:
                self.scheme = RWLockingScheme(shared_space(config), timeouts)
            except OSError as exc:
                self._broken = f"Calu's locking cannot use its directory: {exc}"
                log.error("%s; every command and edit is denied", self._broken)
        self._token = secrets.token_hex(4)
        self._mutex = threading.Lock()
        self._tickets: dict[str, Ticket] = {}
        self._leases: dict[str, Lease] = {}
        self._outside: set[str] = set()
        self._limiter = anyio.CapacityLimiter(WAIT_THREADS)

    def issue(self, step: Step) -> None:
        """Give the call its place in its agent's issue order, when its check begins.

        Args:
            step: The tool call; nothing happens for one that takes no lock or has no id.
        """
        if not needs_locks(step) or not step.tool_use_id or self._broken:
            return
        ticket = self.scheme.issue(step.agent_id or "main")
        with self._mutex:
            self._tickets[step.tool_use_id] = ticket

    def mark_writes_outside(self, step: Step) -> None:
        """Note that a command writes code or configuration outside its checkouts.

        The command checker says so (`CheckDecision.writes_outside_worktree`). Every later
        `take` of the call then takes the global lock exclusively, like an edit outside every
        checkout. The mark ends with the call (`call_ended`, `close`).

        Args:
            step: The tool call; one that is not a command, or has no id, is ignored.
        """
        if step.tool_name in SHELL_TOOLS and step.tool_use_id:
            with self._mutex:
                self._outside.add(step.tool_use_id)

    def withdraw(self, step: Step) -> None:
        """Give up the call's turn: it was denied, or it will run without its locks being taken.

        A lock call of this tool call that is still waiting is cancelled. Never raises: a
        backend error is logged (see `_give_back`).

        Args:
            step: The tool call.
        """
        key = step.tool_use_id or ""
        with self._mutex:
            ticket = self._tickets.pop(key, None)
        if ticket is not None:
            _give_back(ticket, key)

    async def take(self, step: Step, budget_s: float | None) -> str:
        """Take the locks of an approved call; wait at most the configured wait and `budget_s`.

        The first `take` of a call uses its ticket. A later one (after `release`, e.g. when a
        person decided) enters the queues at once, with a ticket of its own (agent
        "retake:<tool call id>"), so that a cancellation ends its wait, too. The wait runs in a
        worker thread; when this coroutine is cancelled, the ticket is withdrawn and a lease
        that arrives anyway is given back.

        Args:
            step: The approved tool call.
            budget_s: The longest the caller can wait (e.g. the PreToolUse hook's time left), or
                None for the configured wait alone.

        Returns:
            "" when the call holds its locks or needs none; else the reason to deny it with: it
            did not get its locks in time, its ticket was withdrawn, or locking does not work.
        """
        key = step.tool_use_id or ""
        if not needs_locks(step) or not key:
            return ""
        if self._broken:
            self.withdraw(step)
            return f"Not run: {self._broken}. Nothing was changed."
        with self._mutex:
            ticket = self._tickets.pop(key, None)
            old = self._leases.pop(key, None)
        if old is not None:
            _give_back(old, key)
        if ticket is None:
            ticket = self.scheme.issue(f"retake:{key}")
        try:
            # Also with locking off: the plan resolves paths and may run git (`_plan`).
            lease = await self._in_thread(lambda: self._lock_call(step, ticket, budget_s), ticket)
        except LockTimeoutError as exc:
            return _busy(exc, self._plan(step).labels)
        except LockCancelledError:
            return "Not run: the call was cancelled while it waited for its locks."
        except (LockError, OSError, ValueError, TypeError) as exc:
            log.exception("taking the locks of %s failed", key)
            return f"Not run: Calu's locking refused this call ({exc}). Nothing was changed."
        with self._mutex:
            self._leases[key] = lease
        return ""

    def release(self, step: Step) -> None:
        """Give back the locks of a call that holds them, e.g. before a person decides about it.

        Never raises: a backend error is logged (see `_give_back`).

        Args:
            step: The tool call.
        """
        key = step.tool_use_id or ""
        with self._mutex:
            lease = self._leases.pop(key, None)
        if lease is not None:
            _give_back(lease, key)

    def call_ended(self, tool_use_id: str) -> None:
        """Give back the locks of a call that ended (its tool result arrived), and its ticket.

        Never raises: a backend error is logged (see `_give_back`).

        Args:
            tool_use_id: The call's id.
        """
        with self._mutex:
            lease = self._leases.pop(tool_use_id, None)
            ticket = self._tickets.pop(tool_use_id, None)
            self._outside.discard(tool_use_id)
        if lease is not None:
            _give_back(lease, tool_use_id)
        if ticket is not None:
            _give_back(ticket, tool_use_id)

    def close(self) -> None:
        """Give back every lock and ticket of the session (its run ended).

        Never raises: each lease and ticket is given back on its own, and a backend error is
        logged (see `_give_back`).
        """
        with self._mutex:
            leases, self._leases = dict(self._leases), {}
            tickets, self._tickets = dict(self._tickets), {}
            self._outside.clear()
        for key, lease in leases.items():
            _give_back(lease, key)
        for key, ticket in tickets.items():
            _give_back(ticket, key)

    def holding(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """Return the calls that hold locks now, for diagnostics and tests.

        Returns:
            Tool call id -> (lock key, mode) of each lock it holds.
        """
        with self._mutex:
            return {
                key: tuple((lock, mode.value) for lock, mode in lease.locks)
                for key, lease in self._leases.items()
            }

    def _plan(self, step: Step) -> _Plan:
        """Decide which lock call a tool call makes (see the module docstring's table).

        Reads the file system (resolves paths, may refresh the worktree registry).

        Args:
            step: A tool call that needs locks.

        Returns:
            The plan.
        """
        if step.tool_name in SHELL_TOOLS:
            checkouts = self._step_checkouts(step)
            roots = tuple(str(checkout.root) for checkout in checkouts)
            labels = {
                worktree_key(str(checkout.root)): f"the worktree `{checkout.label}`"
                for checkout in checkouts
            }
            labels[GLOBAL_KEY] = "the lock of everything outside the worktrees"
            with self._mutex:
                outside = (step.tool_use_id or "") in self._outside
            return _Plan("command", roots, labels=labels, writes_outside=outside)
        raw = step.tool_input.get("file_path") or step.tool_input.get("notebook_path") or ""
        target = Path(step.cwd or self._workspace) / str(raw)
        checkout = self._checkout_of(target)
        if checkout is None:
            return _Plan(
                "outside", labels={GLOBAL_KEY: "the lock of everything outside the worktrees"}
            )
        path = str(target.resolve())
        root = str(checkout.root)
        labels = {
            worktree_key(root): f"the worktree `{checkout.label}`",
            file_key(root, path): f"the file `{raw}`",
        }
        return _Plan("edit", (root,), path, labels)

    def _lock_call(self, step: Step, ticket: Ticket | None, budget_s: float | None) -> Lease:
        """Make the scheme's lock call for `step`; blocks until it has its locks.

        Args:
            step: A tool call that needs locks.
            ticket: Its ticket, or None.
            budget_s: The longest the caller can wait, or None.

        Returns:
            The lease.

        Raises:
            LockTimeoutError: The locks were not free in time.
            LockCancelledError: The ticket was withdrawn while the call waited.
            LockError: The scheme refused the call (a second lease of its owner, a used ticket).
        """
        plan = self._plan(step)
        owner = f"{step.tool_use_id}@{self._token}"
        limits = self.scheme.timeouts
        if plan.kind == "command":
            wait = limits.command_default_s
        else:
            wait = limits.edit_default_s
        if budget_s is not None:
            wait = max(0.0, min(wait, budget_s))
        if plan.kind == "command":
            return self.scheme.run_command_in_worktrees(
                plan.worktrees,
                timeout=wait,
                writes_outside=plan.writes_outside,
                owner=owner,
                ticket=ticket,
            )
        if plan.kind == "edit":
            return self.scheme.edit_in_worktree(
                plan.worktrees[0], path=plan.path, timeout=wait, owner=owner, ticket=ticket
            )
        return self.scheme.edit_outside_worktree(timeout=wait, owner=owner, ticket=ticket)

    async def _in_thread(self, call: Callable[[], Lease], ticket: Ticket | None) -> Lease:
        """Run a blocking lock call in a worker thread; never leave a lease behind.

        When this coroutine is cancelled, the ticket is withdrawn (a waiting lock call then
        ends with `LockCancelledError`) and a lease that arrives anyway is given back: either
        the thread sees the cancellation, or this side sees the lease.

        Args:
            call: The lock call.
            ticket: Its ticket, or None.

        Returns:
            The lease.

        Raises:
            LockTimeoutError: From `call`.
            LockCancelledError: From `call`.
            LockError: From `call`.
        """
        handover = threading.Lock()
        cancelled = False
        arrived: list[Lease] = []

        def run() -> Lease:
            """Make the lock call; hand the lease over, or give it back after a cancellation.

            Returns:
                The lease.

            Raises:
                LockCancelledError: The caller was cancelled before the lease arrived.
            """
            lease = call()
            with handover:
                if not cancelled:
                    arrived.append(lease)
                    return lease
            _give_back(lease, lease.operation.owner)
            raise LockCancelledError(lease.operation, lease.waited_s)

        try:
            return await anyio.to_thread.run_sync(
                run, abandon_on_cancel=True, limiter=self._limiter
            )
        except BaseException:
            with handover:
                cancelled = True
                late = arrived[0] if arrived else None
            if late is not None:
                _give_back(late, late.operation.owner)
            if ticket is not None:
                _give_back(ticket, ticket.agent)
            raise


def _give_back(held: Lease | Ticket, key: str) -> None:
    """Release a lease or withdraw a ticket; log a failure instead of raising it.

    A lease counts as released even when the backend failed (`Lease.release`). The file
    backend removes such a forgotten entry with its process's next change of the state; the
    other processes remove it when the process ends (agent/records/notes/locking.md, section 8).

    Args:
        held: The lease or ticket.
        key: The tool call's id, for the log.
    """
    try:
        if isinstance(held, Lease):
            held.release()
        else:
            held.withdraw()
    except Exception:
        log.exception("giving back the locks of %s failed", key)


def _busy(exc: LockTimeoutError, labels: dict[str, str]) -> str:
    """Say why a call did not get its locks, in words Claude can act on.

    Args:
        exc: The timeout.
        labels: How the call's locks are named (`_Plan.labels`).

    Returns:
        The reason to deny the call with.
    """
    if exc.mode is None:
        what = "its turn after the earlier calls of its agent"
    else:
        what = labels.get(exc.key, f"the lock `{exc.key}`")
    return (
        f"Not run: this call waited {exc.waited_s:.0f} s for {what}, which another call "
        "holds or waits for, and gave up. Nothing ran and nothing was changed; the same call "
        "can be repeated."
    )
