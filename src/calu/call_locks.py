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

A command's check runs before the command has its locks, so another call can write in its
worktree in between. `CallLocks` lets the session notice that (locking.md, section 9):

    check_begins    remembers the generations of the command's locks (`calu.locking.rwlock`)
    take            reads them again once the command holds its locks, and compares
    stale           names the locks whose generation differs: another call ended there
    checked_again   the session checked the command again; the numbers read by `take` now
                    count as the checked ones

With locking off there are no generations, and `stale` never names anything. Edits are not
compared: an edit's checkers judge the edit itself.
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

OUTSIDE_CHANGED = "the files outside the worktrees"
"""How `CallLocks.stale` names the global lock when its generation changed."""

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


@dataclass(frozen=True)
class _Mode:
    """One locking setting in effect: the scheme it gives, or why locking cannot work.

    Attributes:
        config: The locking settings.
        scheme: `RWLockingScheme` on the shared space when locking is on, else
            `NoLockingScheme`.
        broken: Why the space cannot be used ("" when it can); every command and edit of the
            mode is then denied with it.
    """

    config: LockingConfig
    scheme: LockingScheme
    broken: str = ""


def _mode(config: LockingConfig) -> _Mode:
    """Choose the scheme for `config`; with locking on, open the shared space.

    Args:
        config: The locking settings.

    Returns:
        The mode. A space that cannot be opened (e.g. the directory of the "files" backend
        cannot be created) gives a mode with `broken` set, not an exception.
    """
    timeouts = LockTimeouts(config.command_wait_s, MAX_WAIT_S, config.edit_wait_s, MAX_WAIT_S)
    if not config.enabled:
        return _Mode(config, NoLockingScheme(timeouts))
    try:
        return _Mode(config, RWLockingScheme(shared_space(config), timeouts))
    except OSError as exc:
        broken = f"Calu's locking cannot use its directory: {exc}"
        log.error("%s; every command and edit is denied", broken)
        return _Mode(config, NoLockingScheme(timeouts), broken)


class CallLocks:
    """Issues tickets, takes and gives back the locks of the tool calls of one session.

    Every method may be called for every tool call; a call that takes no lock (`needs_locks`)
    is ignored. Tickets and leases are kept by the tool call's id; a call without one takes no
    lock. The owner of a call's locks is "<tool call id>@<token of this object>": the lock space
    is shared with other sessions and processes, and tool call ids are unique only per session.

    The settings can change while the session runs (`reconfigure`). A call keeps the setting
    that was in effect when its check began (`issue`): a call issued with locking off takes no
    lock even if locking is on by the time it is approved, and a call issued with locking on
    still takes its locks after locking was switched off. A second take of a call (`take` after
    `release`) and every call issued later use the new setting.

    Attributes:
        config: The locking settings in effect for calls issued from now on.
        scheme: Their scheme: `RWLockingScheme` on the shared space when locking is on, else
            `NoLockingScheme`.
    """

    def __init__(
        self,
        config: LockingConfig,
        step_checkouts: Callable[[Step], Sequence[Checkout]],
        checkout_of: Callable[[Path], Checkout | None],
        workspace: Path,
    ) -> None:
        """Choose the scheme for `config` (`_mode`); with locking on, open the shared space.

        A space that cannot be opened (the directory of the "files" backend cannot be created)
        does not fail the session: every call that needs locks is then denied with the reason.

        Args:
            config: The locking settings.
            step_checkouts: The session's checkouts of a tool call (`Session.step_checkouts`).
            checkout_of: The checkout a path belongs to (`Checkouts.of`), or None outside
                every checkout.
            workspace: The session directory, for relative paths.
        """
        self._step_checkouts = step_checkouts
        self._checkout_of = checkout_of
        self._workspace = workspace
        self._current = _mode(config)
        self._token = secrets.token_hex(4)
        self._mutex = threading.Lock()
        self._modes: dict[str, _Mode] = {}  # the mode each issued call keeps
        self._tickets: dict[str, Ticket] = {}
        self._leases: dict[str, Lease] = {}
        self._outside: set[str] = set()
        # Per command: the generations of its locks that its check saw (`check_begins`,
        # `checked_again`).
        self._marks: dict[str, dict[str, int]] = {}
        # Per command that holds its locks: the generations read at its last `take`, and the
        # names of the locks whose number differs from `_marks`.
        self._fresh: dict[str, tuple[dict[str, int], tuple[str, ...]]] = {}
        self._limiter = anyio.CapacityLimiter(WAIT_THREADS)

    @property
    def config(self) -> LockingConfig:
        """The locking settings in effect for calls issued from now on.

        Returns:
            The settings.
        """
        return self._current.config

    @property
    def scheme(self) -> LockingScheme:
        """The scheme of calls issued from now on.

        Returns:
            `RWLockingScheme` when locking is on, else `NoLockingScheme`.
        """
        return self._current.scheme

    def reconfigure(self, config: LockingConfig) -> None:
        """Use new locking settings for the calls issued from now on.

        Calls issued before keep their setting until they end (see the class docstring);
        their tickets and leases stay valid. Equal settings change nothing.

        Args:
            config: The new settings.
        """
        if config == self._current.config:
            return
        mode = _mode(config)
        with self._mutex:
            self._current = mode
        log.info(
            "locking %s for the calls issued from now on",
            "on" if config.enabled and not mode.broken else "off",
        )

    def issue(self, step: Step) -> None:
        """Give the call its place in its agent's issue order, when its check begins.

        The call keeps the setting in effect now (`reconfigure`) until it ends.

        Args:
            step: The tool call; nothing happens for one that takes no lock or has no id.
        """
        if not needs_locks(step) or not step.tool_use_id:
            return
        with self._mutex:
            mode = self._current
            self._modes[step.tool_use_id] = mode
        if mode.broken:
            return
        ticket = mode.scheme.issue(step.agent_id or "main")
        with self._mutex:
            self._tickets[step.tool_use_id] = ticket

    def check_begins(self, step: Step, checkouts: Sequence[Checkout]) -> None:
        """Remember the generations of a command's locks, when its check begins.

        Call it after `issue` and before a checker looks at the command. `take` reads the
        numbers again once the command holds its locks, and `stale` names what differs.

        Nothing is remembered in three cases:

        - The call is not a command, has no id, or was not issued.
        - Locking is off for the call (the setting noted at `issue`), or its space is broken.
        - The numbers cannot be read. That is logged, and `stale` then names every lock of
          the command.

        Never raises an `Exception`.

        Args:
            step: The tool call.
            checkouts: The checkouts the command belongs to (`Session.step_checkouts`), which
                `take` will lock.
        """
        key = step.tool_use_id or ""
        if step.tool_name not in SHELL_TOOLS or not key:
            return
        with self._mutex:
            mode = self._modes.get(key)
        if mode is None or mode.broken:
            return
        try:
            marks = mode.scheme.generations(str(checkout.root) for checkout in checkouts)
        except Exception:
            log.exception("reading the lock generations for %s failed", key)
            return
        if marks:
            with self._mutex:
                self._marks[key] = marks

    def stale(self, step: Step) -> tuple[str, ...]:
        """Name where another call ended since the check of a command that holds its locks.

        `take` made the comparison when the command got its locks: the generations then,
        against those of `check_begins` or of the last `checked_again`. A lock without a
        remembered number counts as changed.

        Args:
            step: The tool call.

        Returns:
            One name per lock whose generation differs, e.g. "the worktree `.`", or
            `OUTSIDE_CHANGED` for the global lock. Empty when nothing differs, when the call
            holds no locks, when it is not a command, and with locking off.
        """
        with self._mutex:
            fresh = self._fresh.get(step.tool_use_id or "")
        return fresh[1] if fresh else ()

    def checked_again(self, step: Step) -> None:
        """Note that the session checked a command again while it holds its locks.

        The generations that `take` read count as the checked ones from now on, and `stale`
        names nothing until the next `take`. Does nothing for a call that holds no locks.

        Args:
            step: The tool call.
        """
        key = step.tool_use_id or ""
        with self._mutex:
            fresh = self._fresh.get(key)
            if fresh is not None:
                self._marks[key] = fresh[0]
                self._fresh[key] = (fresh[0], ())

    def mark_writes_outside(self, step: Step) -> bool:
        """Note that a command writes code or configuration outside its checkouts.

        The command checker says so (`CheckDecision.writes_outside_worktree`). Every later
        `take` of the call then takes the global lock exclusively, like an edit outside every
        checkout. The mark ends with the call (`call_ended`, `close`).

        Args:
            step: The tool call; one that is not a command, or has no id, is ignored.

        Returns:
            True when the mark is new. A call that already holds its locks then holds the
            global lock shared, and only a new `take` makes it exclusive. False when the call
            was marked before, or is ignored.
        """
        if step.tool_name not in SHELL_TOOLS or not step.tool_use_id:
            return False
        with self._mutex:
            new = step.tool_use_id not in self._outside
            self._outside.add(step.tool_use_id)
        return new

    def withdraw(self, step: Step) -> None:
        """Give up the call's turn: it was denied, or it will run without its locks being taken.

        A lock call of this tool call that is still waiting is cancelled. The generations
        remembered for the call are forgotten. Never raises: a backend error is logged (see
        `_give_back`).

        Args:
            step: The tool call.
        """
        key = step.tool_use_id or ""
        with self._mutex:
            ticket = self._tickets.pop(key, None)
            self._modes.pop(key, None)
            self._marks.pop(key, None)
            self._fresh.pop(key, None)
        if ticket is not None:
            _give_back(ticket, key)

    async def take(self, step: Step, budget_s: float | None) -> str:
        """Take the locks of an approved call; wait at most the configured wait and `budget_s`.

        The first `take` of a call uses its ticket and the setting in effect when it was issued.
        A later one (after `release`, e.g. when a person decided) uses the setting in effect
        now and enters the queues at once, with a ticket of its own (agent
        "retake:<tool call id>"), so that a cancellation ends its wait, too. The wait runs in a
        worker thread; when this coroutine is cancelled, the ticket is withdrawn and a lease
        that arrives anyway is given back.

        A command that got its locks has the generations of its locks read and compared with
        the remembered ones (`check_begins`); `stale` gives the result. When they cannot be
        read, the locks are given back and the call is denied.

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
        with self._mutex:
            ticket = self._tickets.pop(key, None)
            # The setting noted at `issue` for the first take; the current one for a re-take.
            mode = self._modes.pop(key, None) or self._current
            old = self._leases.pop(key, None)
            self._fresh.pop(key, None)
        if old is not None:
            _give_back(old, key)
        if mode.broken:
            if ticket is not None:
                _give_back(ticket, key)
            return f"Not run: {mode.broken}. Nothing was changed."
        if ticket is None:
            ticket = mode.scheme.issue(f"retake:{key}")
        scheme = mode.scheme
        try:
            # Also with locking off: the plan resolves paths and may run git (`_plan`).
            lease = await self._in_thread(
                lambda: self._lock_call(step, scheme, ticket, budget_s), ticket
            )
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
        """Give back the locks of a call that has not run, e.g. before a person decides about it.

        The generations of its locks do not advance: the call changed nothing. Call
        `call_ended` for a call that may have run. What `check_begins` or `checked_again`
        remembered stays, so the next `take` compares with it.

        Never raises: a backend error is logged (see `_give_back`).

        Args:
            step: The tool call.
        """
        key = step.tool_use_id or ""
        with self._mutex:
            lease = self._leases.pop(key, None)
            self._fresh.pop(key, None)
        if lease is not None:
            _give_back(lease, key, ran=False)

    def call_ended(self, tool_use_id: str) -> None:
        """Give back the locks of a call that ended (its tool result arrived), and its ticket.

        The generations of its worktree locks advance, and that of the global lock when the
        call held it exclusively (`calu.locking.scheme.counts_changes`): the call may have
        written there.

        Never raises: a backend error is logged (see `_give_back`).

        Args:
            tool_use_id: The call's id.
        """
        with self._mutex:
            lease = self._leases.pop(tool_use_id, None)
            ticket = self._tickets.pop(tool_use_id, None)
            self._modes.pop(tool_use_id, None)
            self._outside.discard(tool_use_id)
            self._marks.pop(tool_use_id, None)
            self._fresh.pop(tool_use_id, None)
        if lease is not None:
            _give_back(lease, tool_use_id)
        if ticket is not None:
            _give_back(ticket, tool_use_id)

    def close(self) -> None:
        """Give back every lock and ticket of the session (its run ended).

        The generations advance as in `call_ended`: a call that still held locks may have
        run.

        Never raises: each lease and ticket is given back on its own, and a backend error is
        logged (see `_give_back`).
        """
        with self._mutex:
            leases, self._leases = dict(self._leases), {}
            tickets, self._tickets = dict(self._tickets), {}
            self._modes.clear()
            self._outside.clear()
            self._marks.clear()
            self._fresh.clear()
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

    def _lock_call(
        self, step: Step, scheme: LockingScheme, ticket: Ticket | None, budget_s: float | None
    ) -> Lease:
        """Make the scheme's lock call for `step`; blocks until it has its locks.

        For a command, the generations of its locks are then read and compared (`_compare`).
        If that fails, the lease is given back before the error is raised.

        Args:
            step: A tool call that needs locks.
            scheme: The scheme of the setting the call uses (`take`).
            ticket: Its ticket (of that scheme), or None.
            budget_s: The longest the caller can wait, or None.

        Returns:
            The lease.

        Raises:
            LockTimeoutError: The locks were not free in time.
            LockCancelledError: The ticket was withdrawn while the call waited.
            LockError: The scheme refused the call (a second lease of its owner, a used
                ticket), or the generations cannot be used.
            OSError: The generations cannot be read.
        """
        plan = self._plan(step)
        owner = f"{step.tool_use_id}@{self._token}"
        limits = scheme.timeouts
        if plan.kind == "command":
            wait = limits.command_default_s
        else:
            wait = limits.edit_default_s
        if budget_s is not None:
            wait = max(0.0, min(wait, budget_s))
        if plan.kind == "command":
            lease = scheme.run_command_in_worktrees(
                plan.worktrees,
                timeout=wait,
                writes_outside=plan.writes_outside,
                owner=owner,
                ticket=ticket,
            )
            try:
                self._compare(step.tool_use_id or "", scheme, plan)
            except BaseException:
                _give_back(lease, owner, ran=False)
                raise
            return lease
        if plan.kind == "edit":
            return scheme.edit_in_worktree(
                plan.worktrees[0], path=plan.path, timeout=wait, owner=owner, ticket=ticket
            )
        return scheme.edit_outside_worktree(timeout=wait, owner=owner, ticket=ticket)

    def _compare(self, key: str, scheme: LockingScheme, plan: _Plan) -> None:
        """Read the generations of a command that holds its locks, and compare them.

        The numbers cannot advance while the command holds its locks, except when a process
        that held a lock ends (the file backend then advances all of them). They are compared
        with what `check_begins` or `checked_again` remembered; a lock without a remembered
        number counts as changed. The result is kept for `stale` and `checked_again`. With
        locking off there are no numbers, and nothing is kept.

        Args:
            key: The tool call's id.
            scheme: The scheme the command got its locks from.
            plan: The command's plan.

        Raises:
            LockError: The backend's state cannot be used.
            OSError: The backend's state cannot be read.
        """
        now = scheme.generations(plan.worktrees)
        if not now:
            return
        with self._mutex:
            seen = self._marks.get(key, {})
            changed = tuple(
                OUTSIDE_CHANGED if lock == GLOBAL_KEY else plan.labels.get(lock, f"`{lock}`")
                for lock, number in now.items()
                if seen.get(lock) != number
            )
            self._fresh[key] = (now, changed)

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
            _give_back(lease, lease.operation.owner, ran=False)
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
                _give_back(late, late.operation.owner, ran=False)
            if ticket is not None:
                _give_back(ticket, ticket.agent)
            raise


def _give_back(held: Lease | Ticket, key: str, ran: bool = True) -> None:
    """Release a lease or withdraw a ticket; log a failure instead of raising it.

    A lease counts as released even when the backend failed (`Lease.release`). The file
    backend removes such a forgotten entry with its process's next change of the state; the
    other processes remove it when the process ends (agent/records/notes/locking.md, section 8).

    Args:
        held: The lease or ticket.
        key: The tool call's id, for the log.
        ran: For a lease: False when its call did not run, so the generations of its locks
            stay as they are (`Lease.release` with `changed=False`). True, the default, when
            the call ran or may have run. Not used for a ticket.
    """
    try:
        if isinstance(held, Lease):
            held.release(changed=ran)
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
