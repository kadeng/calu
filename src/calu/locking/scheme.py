"""Which locks a command or an edit takes, and how long it may wait (agent/records/PLAN.md D274).

The owner's scheme (agent/records/notes/locking.md): inside a worktree, a command takes
the worktree's lock exclusively and an edit takes it shared. So no edit lands while a command
runs there, and two commands of one worktree never run at once. A global lock guards
everything outside the worktrees: an edit there takes it exclusively, every command shared.

    call                              global lock   worktree locks            file lock
    run_command_in_worktree(w)        shared (1)    w: exclusive              -
    run_command_in_worktrees(ws)      shared (1)    each w in ws: exclusive   -
    edit_in_worktree(w)               -             w: shared                 -
    edit_in_worktree(w, path=p)       -             w: shared                 (w, p): exclusive
    edit_outside_worktree()           exclusive     -                         -

    (1) exclusive with writes_outside=True: the command also writes outside its worktrees.

Rules (the owner's decisions of 2026-11-25; the locks themselves are FIFO, `calu.locking.rwlock`):

- **The owner is one operation** (one tool call). Two calls of one agent wait for each other
  like calls of different agents. An owner has at most one lease or pending call at a time; a
  second one is a bug and is refused at once with `LockError` (no reentrancy).
- **Issue order within an agent.** `issue` numbers an agent's calls when the agent issues them
  and returns a `Ticket`. A call with a ticket enters the lock queues only after every earlier
  call of its agent has entered them or was withdrawn (`Ticket.withdraw`: its check was denied,
  it failed, or it takes no lock). A call has entered when its request for its last lock is
  queued. Across agents, the order in which requests reach a lock decides. The order lives in
  the scheme object, so all calls of one agent must go through one scheme (one process).

        agent A issues        #1 edit in W     #2 command in W    #3 edit in W
        checks finish         third            first              second
        enter the queue of W  #1 at once       #2 after #1        #3 after #2
        run                   #1               #2 after #1        #3 after #2

- **Cancellation.** Withdrawing the ticket of a pending call (one that waits for its turn or a
  lock) cancels it: it gives back what it holds and raises `LockCancelledError`, so it can
  never run after the agent's later calls. Once the call returned its lease, withdrawing does
  nothing.
- **No deadlock.** Every operation takes its locks in one total order (`lock_order`: the
  worktree locks sorted by id, the file lock, the global lock), and every wait, also the wait
  for the issue order, ends at the operation's timeout. An operation that runs out of time
  releases what it holds, gives up its turn if it had not entered yet, and raises
  `LockTimeoutError`. The global lock comes last (the owner's choice of 2026-11-25): a command
  that waits for a busy worktree then holds nothing, so an edit outside waits only for the
  commands that run, not for those queued behind them.
  This holds for exceptions the calls themselves raise; asynchronous exceptions such as
  KeyboardInterrupt are outside it (`calu.locking.rwlock`: call from worker threads).
- **Timeouts.** `LockTimeouts` holds a default and a maximum for commands and for edits (both
  types of edit). A call may pass its own timeout; a value above the maximum is cut to it, and
  an infinite, NaN or negative value is refused. No wait is unbounded.

`RWLockingScheme` locks through a `LockSpace` (the backend). `NoLockingScheme` takes the same
calls, checks the same arguments and never waits: the drop-in when the feature is off.
"""

import enum
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType

from calu.locking.rwlock import (
    Hold,
    LockError,
    LockMode,
    LockRequest,
    LockSpace,
    RWLock,
    checked_owner,
    checked_timeout,
)

GLOBAL_KEY = "global"
"""The key of the lock that guards everything outside the worktrees."""

GENERATED_OWNER_PREFIX = "generated:"
"""The prefix of the owners the scheme generates; callers' owners must not start with it."""


def worktree_key(worktree: str) -> str:
    """Return the lock key of a worktree.

    Args:
        worktree: The worktree's id, as the scheme's calls received it.

    Returns:
        `worktree:<id>`.
    """
    return f"worktree:{worktree}"


def file_key(worktree: str, path: str) -> str:
    """Return the lock key of one file in a worktree.

    The worktree id's length is part of the key, so that no two (worktree, path) pairs give
    the same key, whatever characters they contain.

    Args:
        worktree: The worktree's id.
        path: The file's path, as the edit's call received it.

    Returns:
        `file:<length of the id>:<id>:<path>`.
    """
    return f"file:{len(worktree)}:{worktree}:{path}"


def lock_order(key: str) -> tuple[int, str]:
    """Return the sort key that defines the one order in which every operation takes its locks.

    The worktree locks come first (sorted by key), then the file locks, then the global lock.
    A command therefore asks for the global lock only once it has its worktrees, and the call
    enters its agent's issue order when that last request is queued.

    Args:
        key: A key made by `GLOBAL_KEY`, `worktree_key` or `file_key`.

    Returns:
        (group, key).
    """
    if key == GLOBAL_KEY:
        return (2, key)
    return (0 if key.startswith("worktree:") else 1, key)


class OperationKind(enum.Enum):
    """The three types of operation the scheme knows."""

    COMMAND = "command"
    EDIT = "edit in a worktree"
    EDIT_OUTSIDE = "edit outside the worktrees"


@dataclass(frozen=True, slots=True)
class Operation:
    """One command or edit, as far as locking is concerned.

    Attributes:
        kind: The type of operation.
        owner: The operation's label in lock diagnostics and error messages.
        worktrees: The worktree ids: those of a command (sorted, without repeats), the one of
            an edit in a worktree, none for an edit outside.
        path: The edited file, when an edit in a worktree named it.
        writes_outside: For a command: it also writes outside its worktrees.
    """

    kind: OperationKind
    owner: str
    worktrees: tuple[str, ...] = ()
    path: str | None = None
    writes_outside: bool = False

    def describe(self) -> str:
        """Describe the operation in a few words, e.g. "command in worktree wt1".

        Returns:
            The description.
        """
        match self.kind:
            case OperationKind.COMMAND:
                where = ", ".join(self.worktrees) or "no worktree"
                plural = "s" if len(self.worktrees) > 1 else ""
                outside = ", writing outside" if self.writes_outside else ""
                return f"command in worktree{plural} {where}{outside}"
            case OperationKind.EDIT:
                target = f"edit of {self.path}" if self.path is not None else "edit"
                return f"{target} in worktree {self.worktrees[0]}"
            case OperationKind.EDIT_OUTSIDE:
                return "edit outside the worktrees"


@dataclass(frozen=True, slots=True)
class LockTimeouts:
    """How long operations may wait for their locks, in seconds. No wait is unbounded.

    The default of 600 s lets a command at Claude Code's longest Bash timeout (10 minutes)
    finish. The maximum of 3600 s bounds what a call may ask for: a waiting call blocks its
    agent.

    Attributes:
        command_default_s: The wait of a command whose call passes no timeout.
        command_max_s: The longest wait a command's call may ask for.
        edit_default_s: The wait of an edit (in or outside a worktree) that passes no timeout.
        edit_max_s: The longest wait an edit's call may ask for.
    """

    command_default_s: float = 600.0
    command_max_s: float = 3600.0
    edit_default_s: float = 600.0
    edit_max_s: float = 3600.0

    def __post_init__(self) -> None:
        """Check the four values.

        Raises:
            TypeError: A value is not a number.
            ValueError: A value is not finite or not above 0, or a default exceeds its maximum.
        """
        for name in ("command_default_s", "command_max_s", "edit_default_s", "edit_max_s"):
            if checked_timeout(getattr(self, name)) == 0:
                raise ValueError(f"{name} must be above 0 seconds")
        for kind, default, maximum in (
            ("command", self.command_default_s, self.command_max_s),
            ("edit", self.edit_default_s, self.edit_max_s),
        ):
            if default > maximum:
                raise ValueError(
                    f"the default {kind} timeout ({default:g} s) exceeds the maximum "
                    f"({maximum:g} s)"
                )

    def for_operation(self, kind: OperationKind, requested: float | None = None) -> float:
        """Return the wait an operation of `kind` gets.

        Args:
            kind: The operation's type: commands use the command values, both types of edit
                the edit values.
            requested: The call's own timeout in seconds, or None for the default. A value
                above the maximum is cut to the maximum; 0 means "only if free at once".

        Returns:
            Seconds to wait.

        Raises:
            TypeError: `requested` is not a number.
            ValueError: `requested` is negative, NaN or infinite.
        """
        if kind is OperationKind.COMMAND:
            default, maximum = self.command_default_s, self.command_max_s
        else:
            default, maximum = self.edit_default_s, self.edit_max_s
        if requested is None:
            return float(default)
        return min(checked_timeout(requested), float(maximum))


class TicketState(enum.Enum):
    """Where a call is in its agent's issue order."""

    ISSUED = "issued"  # numbered; has not entered the lock queues yet
    ENTERED = "entered"  # its request for its last lock is queued (or it takes no lock)
    WITHDRAWN = "withdrawn"  # denied, failed, cancelled, or takes no lock


class Ticket:
    """The place of one call in its agent's issue order; `LockingScheme.issue` makes it.

    A ticket serves one lock call, or is withdrawn. Only ISSUED tickets hold up the later calls
    of their agent. Used as a context manager, it is withdrawn when the block ends, unless its
    lock call returned a lease.

    Attributes:
        agent: The agent that issued the call.
        number: The call's number in the agent's issue order, from 1.
    """

    def __init__(self, order: _CallOrder, agent: str, number: int) -> None:
        """Create an issued ticket; only `_CallOrder.issue` does.

        Args:
            order: The issue order the ticket belongs to.
            agent: The agent that issued the call.
            number: The call's number.
        """
        self.agent = agent
        self.number = number
        self._order = order
        self._state = TicketState.ISSUED
        self._claimed = False  # a lock call uses it
        self._finished = False  # that lock call returned a lease, or failed
        self._cancelled = False  # withdrawn while that lock call was pending
        self._pending: LockRequest | None = None  # the request that lock call waits for

    @property
    def state(self) -> TicketState:
        """The ticket's state now.

        Returns:
            ISSUED, ENTERED or WITHDRAWN.
        """
        return self._state

    def withdraw(self) -> None:
        """Give up the call's turn; cancel the call when its lock call is pending.

        Withdraw a ticket when its call's check was denied, the call failed before its lock
        call, or it takes no lock: the agent's later calls then need not wait for it. When the
        call's lock call is pending (it waits for its turn or for a lock), the lock call is
        cancelled: it gives back what it holds and raises `LockCancelledError`. Does nothing
        once the lock call returned its lease, or when the ticket was withdrawn already. Any
        thread may call it.
        """
        self._order.withdraw(self)

    def __enter__(self) -> Ticket:
        """Return the ticket itself.

        Returns:
            The ticket.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Withdraw the ticket when the block ends (nothing happens once its call has a lease).

        Args:
            exc_type: The type of the exception that ends the block, or None.
            exc: That exception, or None.
            traceback: Its traceback, or None.
        """
        self.withdraw()

    def __repr__(self) -> str:
        """Describe the ticket for messages and tests; takes no lock.

        Returns:
            E.g. `Ticket('agent-1', 3, issued)`.
        """
        return f"Ticket({self.agent!r}, {self.number}, {self._state.value})"


class _Cancelled(Exception):
    """Raised inside the scheme when a pending lock call's ticket was withdrawn."""


class _CallOrder:
    """The issue order of every agent of one scheme (see the module docstring).

    All ticket fields change under `_mutex` only. No method builds a message that takes the
    mutex again, so nothing here can block on itself.
    """

    def __init__(self) -> None:
        """Start without agents."""
        self._mutex = threading.Lock()
        self._turns: dict[str, threading.Condition] = {}
        self._issued: dict[str, int] = {}
        self._open: dict[str, set[int]] = {}

    def issue(self, agent: str) -> Ticket:
        """Number the next call of `agent`.

        Args:
            agent: The agent's id.

        Returns:
            The call's ticket, ISSUED.
        """
        with self._mutex:
            number = self._issued.get(agent, 0) + 1
            self._issued[agent] = number
            self._open.setdefault(agent, set()).add(number)
            if agent not in self._turns:
                self._turns[agent] = threading.Condition(self._mutex)
            return Ticket(self, agent, number)

    def claim(self, ticket: Ticket) -> None:
        """Let one lock call use `ticket`.

        Args:
            ticket: A ticket of this order.

        Raises:
            LockError: Another lock call uses the ticket, or it was used or withdrawn already.
        """
        with self._mutex:
            if ticket._claimed:
                raise LockError(
                    f"Ticket({ticket.agent!r}, {ticket.number}) is used by another lock call"
                )
            if ticket._state is not TicketState.ISSUED:
                raise LockError(
                    f"Ticket({ticket.agent!r}, {ticket.number}) was "
                    f"{ticket._state.value} already; a ticket serves one lock call"
                )
            ticket._claimed = True

    def abandon(self, ticket: Ticket) -> None:
        """Withdraw `ticket` if no lock call has claimed it: its call failed before it began.

        Args:
            ticket: A ticket of this order.
        """
        with self._mutex:
            if not ticket._claimed and ticket._state is TicketState.ISSUED:
                self._close(ticket, TicketState.WITHDRAWN)

    def wait_turn(self, ticket: Ticket, deadline: float) -> bool:
        """Wait until every earlier call of the ticket's agent entered or was withdrawn.

        Args:
            ticket: A ticket that the waiting lock call claimed.
            deadline: The latest moment (`time.monotonic`) to wait for.

        Returns:
            True when it is the ticket's turn, False when the deadline passed first.

        Raises:
            _Cancelled: The ticket was withdrawn while the call waited.
        """
        with self._mutex:
            turn = self._turns[ticket.agent]
            while True:
                if ticket._cancelled:
                    raise _Cancelled
                if min(self._open[ticket.agent]) == ticket.number:
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                turn.wait(left)

    def attach(self, ticket: Ticket, request: LockRequest, enter: bool) -> None:
        """Note the request the ticket's call is about to wait for; enter if it is the last.

        Args:
            ticket: A ticket that the lock call claimed.
            request: The queued request.
            enter: True for the request of the call's last lock: the ticket then enters.

        Raises:
            _Cancelled: The ticket was withdrawn meanwhile; the caller withdraws `request`.
        """
        with self._mutex:
            if ticket._cancelled:
                raise _Cancelled
            ticket._pending = request
            if enter:
                self._close(ticket, TicketState.ENTERED)

    def detach(self, ticket: Ticket) -> bool:
        """Forget the request the call waited for, and tell whether it was cancelled meanwhile.

        Args:
            ticket: A ticket that the lock call claimed.

        Returns:
            True when the ticket was withdrawn while the call waited.
        """
        with self._mutex:
            ticket._pending = None
            return ticket._cancelled

    def finish(self, ticket: Ticket, enter: bool) -> None:
        """Record that the ticket's call got all its locks; from now on withdrawing does nothing.

        Args:
            ticket: A ticket that the lock call claimed.
            enter: True when the call takes no lock: the ticket enters now.

        Raises:
            _Cancelled: The ticket was withdrawn before this moment; the call must give back
                its locks.
        """
        with self._mutex:
            ticket._pending = None
            if ticket._cancelled:
                raise _Cancelled
            ticket._finished = True
            if enter:
                self._close(ticket, TicketState.ENTERED)

    def fail(self, ticket: Ticket) -> None:
        """Record that the ticket's lock call failed; its turn is given up if not entered.

        Args:
            ticket: A ticket that the lock call claimed.
        """
        with self._mutex:
            ticket._finished = True
            ticket._pending = None
            if ticket._state is TicketState.ISSUED:
                self._close(ticket, TicketState.WITHDRAWN)

    def withdraw(self, ticket: Ticket) -> None:
        """Withdraw `ticket` and cancel its pending lock call (see `Ticket.withdraw`).

        Args:
            ticket: A ticket of this order.
        """
        with self._mutex:
            if ticket._state is TicketState.ISSUED:
                self._close(ticket, TicketState.WITHDRAWN)
            pending = None
            if ticket._claimed and not ticket._finished:
                ticket._cancelled = True
                pending = ticket._pending
                self._turns[ticket.agent].notify_all()
        if pending is not None:
            # Wakes the waiting call. If its request was granted and returned meanwhile, the
            # call sees the cancellation itself and gives the hold back.
            with suppress(LockError):
                pending.withdraw()

    def open_tickets(self) -> dict[str, tuple[int, ...]]:
        """Return the ISSUED tickets of every agent.

        Returns:
            Agent id -> numbers of its tickets that have neither entered nor been withdrawn;
            agents without such tickets are left out.
        """
        with self._mutex:
            return {agent: tuple(sorted(open_)) for agent, open_ in self._open.items() if open_}

    def _close(self, ticket: Ticket, state: TicketState) -> None:
        """Move an ISSUED ticket to `state` and wake its agent's waiting calls.

        The caller holds `_mutex`.

        Args:
            ticket: An ISSUED ticket of this order.
            state: ENTERED or WITHDRAWN.
        """
        ticket._state = state
        self._open[ticket.agent].discard(ticket.number)
        self._turns[ticket.agent].notify_all()


def _abandon(ticket: object) -> None:
    """Withdraw `ticket` if it is a ticket that no lock call has claimed: its call failed early.

    Args:
        ticket: What a lock call received as its ticket; anything but a `Ticket` is ignored.
    """
    if isinstance(ticket, Ticket):
        ticket._order.abandon(ticket)


class LockTimeoutError(TimeoutError):
    """An operation did not get all its locks within its timeout.

    It holds none of them. If its ticket had not entered the queues yet, the ticket was
    withdrawn; a call that timed out on its last lock had entered already, and the agent's
    later calls went ahead of it.

    Attributes:
        operation: The operation that waited.
        key: The lock it was waiting for when the time ran out, or `order:<agent>` when it was
            still waiting for the earlier calls of its agent.
        mode: The mode it asked for on that lock; None for the wait for the issue order.
        timeout_s: Its timeout.
        waited_s: How long it waited in all.
    """

    def __init__(
        self,
        operation: Operation,
        key: str,
        mode: LockMode | None,
        timeout_s: float,
        waited_s: float,
    ) -> None:
        """Describe the timeout.

        Args:
            operation: The operation that waited.
            key: The lock it was waiting for, or `order:<agent>`.
            mode: The mode it asked for, or None for the issue order.
            timeout_s: Its timeout.
            waited_s: How long it waited in all.
        """
        waited_for = (
            f"the lock {key!r} ({mode.value})"
            if mode is not None
            else f"the earlier calls of its agent ({key})"
        )
        super().__init__(
            f"{operation.describe()} ({operation.owner}) waited {timeout_s:g} s for "
            f"{waited_for} in vain"
        )
        self.operation = operation
        self.key = key
        self.mode = mode
        self.timeout_s = timeout_s
        self.waited_s = waited_s


class LockCancelledError(Exception):
    """A pending lock call was cancelled: its ticket was withdrawn while it waited.

    It holds none of its locks. Unlike `LockError`, this is no bug of the caller: the caller
    gave the call up, e.g. because the tool call was cancelled.

    Attributes:
        operation: The operation whose call was cancelled.
        waited_s: How long it had waited.
    """

    def __init__(self, operation: Operation, waited_s: float) -> None:
        """Describe the cancellation.

        Args:
            operation: The operation whose call was cancelled.
            waited_s: How long it had waited.
        """
        super().__init__(
            f"{operation.describe()} ({operation.owner}) was cancelled after {waited_s:.3g} s: "
            "its ticket was withdrawn while it waited"
        )
        self.operation = operation
        self.waited_s = waited_s


def _release_all(holds: Sequence[tuple[RWLock, Hold]], cause: BaseException | None = None) -> None:
    """Release `holds` in reverse order and try every one, even after one failed.

    Args:
        holds: (lock, hold) pairs in the order they were acquired.
        cause: The exception that ends an acquisition, if any. A failed release is then added
            to it as a note instead of being raised.

    Raises:
        Exception: The first release that failed (e.g. `LockError`, or an error of the
            backend), after all were tried; only when `cause` is None.
    """
    first: Exception | None = None
    for lock, hold in reversed(holds):
        try:
            lock.release(hold)
        except Exception as exc:
            if cause is not None:
                cause.add_note(f"releasing the lock {hold.key!r} failed as well: {exc!r}")
            elif first is None:
                first = exc
    if first is not None:
        raise first


class Lease:
    """The locks one operation holds, from their grant until `release` or the end of a `with`.

    `release` may be called any number of times, from any thread: the first call releases,
    later calls do nothing. Every end of a call (a tool result, the end of a runner) may
    therefore call it.

    Attributes:
        operation: The operation that holds the locks.
        timeout_s: The timeout it waited with.
        waited_s: How long it waited for all its locks.
    """

    def __init__(
        self,
        operation: Operation,
        holds: Sequence[tuple[RWLock, Hold]],
        timeout_s: float,
        waited_s: float,
        on_release: Callable[[str], None] | None = None,
    ) -> None:
        """Wrap the holds of an operation that got all its locks.

        Args:
            operation: The operation.
            holds: (lock, hold) pairs in the order they were acquired; empty for a scheme
                that does not lock.
            timeout_s: The timeout it waited with.
            waited_s: How long it waited.
            on_release: Called with the owner after the holds were released (the scheme then
                accepts the owner again); None for nothing.
        """
        self.operation = operation
        self.timeout_s = timeout_s
        self.waited_s = waited_s
        self._holds = tuple(holds)
        self._on_release = on_release
        self._mutex = threading.Lock()
        self._released = False

    @property
    def locks(self) -> tuple[tuple[str, LockMode], ...]:
        """The locks the lease holds or held.

        Returns:
            (key, mode) of every lock, in acquisition order; empty for `NoLockingScheme`.
        """
        return tuple((hold.key, hold.mode) for _lock, hold in self._holds)

    @property
    def released(self) -> bool:
        """Whether `release` was called (and has finished).

        Returns:
            True after the first `release`.
        """
        with self._mutex:
            return self._released

    def release(self) -> None:
        """Release every lock of the lease, in reverse order; do nothing when already released.

        When it returns, the locks are free, also for a call that came while another thread
        was still releasing: that call waits for the first one.

        Raises:
            Exception: The first release the backend refused (e.g. `LockError`), after all
                were tried. The lease counts as released even then.
        """
        with self._mutex:
            if self._released:
                return
            self._released = True
            try:
                _release_all(self._holds)
            finally:
                if self._on_release is not None:
                    self._on_release(self.operation.owner)

    def __enter__(self) -> Lease:
        """Return the lease itself; its locks are already held.

        Returns:
            The lease.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lease when the `with` block ends, also when it raised.

        Args:
            exc_type: The type of the exception that ends the block, or None.
            exc: That exception, or None.
            traceback: Its traceback, or None.
        """
        self.release()


def _name(what: str, value: str | os.PathLike[str]) -> str:
    """Return a worktree id, a path or an agent id as a non-empty string.

    Args:
        what: "worktree", "path" or "agent", for the error message.
        value: The id or path.

    Returns:
        The string.

    Raises:
        TypeError: `value` is neither a string nor a path-like object of a string.
        ValueError: `value` is empty.
    """
    text = os.fspath(value) if isinstance(value, str | os.PathLike) else None
    if not isinstance(text, str):
        raise TypeError(f"a {what} must be a string or a path, not {value!r}")
    if not text:
        raise ValueError(f"a {what} must not be empty")
    return text


class LockingScheme(ABC):
    """The calls a command or an edit makes before it runs; the module docstring has the rules.

    Each lock call blocks until the operation has its locks, then returns a `Lease`, or raises
    `LockTimeoutError` when its timeout runs out, or `LockCancelledError` when its ticket is
    withdrawn meanwhile. Worktree ids and paths are opaque: equal strings mean the same worktree
    or file, so callers pass stable ids (e.g. the resolved git admin directory of a worktree).
    Whatever a lock call raises, its ticket ends ENTERED or WITHDRAWN. A typical caller:

        with scheme.issue(agent_id) as ticket:     # when the agent issues the call
            ...                                    # the call's check
            if the check allowed the call and it takes locks:
                lease = scheme.edit_in_worktree(worktree, ticket=ticket, owner=tool_use_id)
        # Leaving the block withdraws the ticket unless the call got its lease.
        ...                                        # the call runs
        lease.release()                            # on its tool result, from any thread

    Another thread cancels a pending lock call with `ticket.withdraw()`.

    Attributes:
        timeouts: The default and maximum waits.
    """

    def __init__(self, timeouts: LockTimeouts | None = None) -> None:
        """Create the scheme.

        Args:
            timeouts: The default and maximum waits; None for `LockTimeouts()`.
        """
        self.timeouts = timeouts if timeouts is not None else LockTimeouts()
        self._order = _CallOrder()
        self._owner_mutex = threading.Lock()
        self._owner_count = 0
        self._live: set[str] = set()

    def issue(self, agent: str) -> Ticket:
        """Number a call of `agent` when the agent issues it, before its check.

        Issue a ticket for every call that may take locks, in the order the agent issued
        them, and pass it to the call's lock call, or withdraw it (`Ticket.withdraw`).

        Args:
            agent: The agent's id, e.g. "main" or a subagent's id; one order per id.

        Returns:
            The call's ticket.

        Raises:
            TypeError: `agent` is not a string.
            ValueError: `agent` is empty.
        """
        return self._order.issue(_name("agent", agent))

    def live_owners(self) -> frozenset[str]:
        """Return the owners that have a lease or a pending lock call now, for diagnostics.

        Returns:
            The owners.
        """
        with self._owner_mutex:
            return frozenset(self._live)

    def open_tickets(self) -> dict[str, tuple[int, ...]]:
        """Return the tickets that hold up their agents' later calls now, for diagnostics.

        Returns:
            Agent id -> numbers of its ISSUED tickets; agents without them are left out.
        """
        return self._order.open_tickets()

    def run_command_in_worktree(
        self,
        worktree: str | os.PathLike[str],
        *,
        timeout: float | None = None,
        writes_outside: bool = False,
        owner: str | None = None,
        ticket: Ticket | None = None,
    ) -> Lease:
        """Wait until a command may run in `worktree`, and hold that until the lease ends.

        Args:
            worktree: The worktree's id.
            timeout: Seconds to wait; None for the configured command default, a value above
                the configured maximum is cut to it.
            writes_outside: True when the command also writes outside its worktrees; it then
                excludes every other command and every edit outside.
            owner: The operation's id (e.g. the tool call's id); None for a generated one.
            ticket: The call's place in its agent's issue order (`issue`), or None to enter
                the lock queues at once.

        Returns:
            The lease of the command's locks.

        Raises:
            LockTimeoutError: The locks were not free within the timeout; none is held.
            LockCancelledError: `ticket` was withdrawn while the call waited; none is held.
            LockError: `owner` already has a lease or a pending call, or `ticket` serves
                another lock call or was used or withdrawn already.
            TypeError: `worktree`, `owner`, `ticket` or `timeout` has the wrong type.
            ValueError: `worktree` or `owner` is empty or reserved, `timeout` is negative, NaN
                or infinite, or `ticket` belongs to another scheme.
        """
        return self.run_command_in_worktrees(
            [worktree], timeout=timeout, writes_outside=writes_outside, owner=owner, ticket=ticket
        )

    def run_command_in_worktrees(
        self,
        worktrees: Iterable[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        writes_outside: bool = False,
        owner: str | None = None,
        ticket: Ticket | None = None,
    ) -> Lease:
        """Wait until a command may run in all `worktrees`, and hold that until the lease ends.

        A command that reaches several worktrees (e.g. through `cd`) names all of them. An
        empty collection means a command outside every worktree: it takes the global lock only.

        Args:
            worktrees: The worktree ids; repeats are ignored.
            timeout: Seconds to wait; None for the configured command default, a value above
                the configured maximum is cut to it.
            writes_outside: True when the command also writes outside its worktrees; it then
                excludes every other command and every edit outside.
            owner: The operation's id (e.g. the tool call's id); None for a generated one.
            ticket: The call's place in its agent's issue order (`issue`), or None to enter
                the lock queues at once.

        Returns:
            The lease of the command's locks.

        Raises:
            LockTimeoutError: The locks were not free within the timeout; none is held.
            LockCancelledError: `ticket` was withdrawn while the call waited; none is held.
            LockError: `owner` already has a lease or a pending call, or `ticket` serves
                another lock call or was used or withdrawn already.
            TypeError: `worktrees` is a single string or path instead of a collection, or an
                id, `owner`, `ticket` or `timeout` has the wrong type.
            ValueError: An id or `owner` is empty or reserved, `timeout` is negative, NaN or
                infinite, or `ticket` belongs to another scheme.
        """
        try:
            if isinstance(worktrees, str | os.PathLike):
                raise TypeError(
                    "run_command_in_worktrees takes a collection of worktrees; "
                    "use run_command_in_worktree for one"
                )
            ids = tuple(sorted({_name("worktree", worktree) for worktree in worktrees}))
            timeout_s = self.timeouts.for_operation(OperationKind.COMMAND, timeout)
            operation = Operation(
                kind=OperationKind.COMMAND,
                owner=self._owner(owner, OperationKind.COMMAND),
                worktrees=ids,
                writes_outside=bool(writes_outside),
            )
        except BaseException:
            _abandon(ticket)
            raise
        return self._lease(operation, timeout_s, ticket)

    def edit_in_worktree(
        self,
        worktree: str | os.PathLike[str],
        *,
        path: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        owner: str | None = None,
        ticket: Ticket | None = None,
    ) -> Lease:
        """Wait until an edit may land in `worktree`, and hold that until the lease ends.

        Args:
            worktree: The worktree's id.
            path: The edited file; two edits of the same path then never run at once. None
                when the caller does not name it.
            timeout: Seconds to wait; None for the configured edit default, a value above the
                configured maximum is cut to it.
            owner: The operation's id (e.g. the tool call's id); None for a generated one.
            ticket: The call's place in its agent's issue order (`issue`), or None to enter
                the lock queues at once.

        Returns:
            The lease of the edit's locks.

        Raises:
            LockTimeoutError: The locks were not free within the timeout; none is held.
            LockCancelledError: `ticket` was withdrawn while the call waited; none is held.
            LockError: `owner` already has a lease or a pending call, or `ticket` serves
                another lock call or was used or withdrawn already.
            TypeError: `worktree`, `path`, `owner`, `ticket` or `timeout` has the wrong type.
            ValueError: `worktree`, `path` or `owner` is empty, `owner` is reserved, `timeout`
                is negative, NaN or infinite, or `ticket` belongs to another scheme.
        """
        try:
            worktree_id = _name("worktree", worktree)
            file = _name("path", path) if path is not None else None
            timeout_s = self.timeouts.for_operation(OperationKind.EDIT, timeout)
            operation = Operation(
                kind=OperationKind.EDIT,
                owner=self._owner(owner, OperationKind.EDIT),
                worktrees=(worktree_id,),
                path=file,
            )
        except BaseException:
            _abandon(ticket)
            raise
        return self._lease(operation, timeout_s, ticket)

    def edit_outside_worktree(
        self,
        *,
        timeout: float | None = None,
        owner: str | None = None,
        ticket: Ticket | None = None,
    ) -> Lease:
        """Wait until an edit outside every worktree may land, and hold that until the lease ends.

        Args:
            timeout: Seconds to wait; None for the configured edit default, a value above the
                configured maximum is cut to it.
            owner: The operation's id (e.g. the tool call's id); None for a generated one.
            ticket: The call's place in its agent's issue order (`issue`), or None to enter
                the lock queues at once.

        Returns:
            The lease of the edit's lock.

        Raises:
            LockTimeoutError: The lock was not free within the timeout.
            LockCancelledError: `ticket` was withdrawn while the call waited; none is held.
            LockError: `owner` already has a lease or a pending call, or `ticket` serves
                another lock call or was used or withdrawn already.
            TypeError: `owner`, `ticket` or `timeout` has the wrong type.
            ValueError: `owner` is empty or reserved, `timeout` is negative, NaN or infinite,
                or `ticket` belongs to another scheme.
        """
        try:
            timeout_s = self.timeouts.for_operation(OperationKind.EDIT_OUTSIDE, timeout)
            operation = Operation(
                kind=OperationKind.EDIT_OUTSIDE,
                owner=self._owner(owner, OperationKind.EDIT_OUTSIDE),
            )
        except BaseException:
            _abandon(ticket)
            raise
        return self._lease(operation, timeout_s, ticket)

    def _owner(self, owner: str | None, kind: OperationKind) -> str:
        """Return `owner` checked, or a new owner such as `generated:command-7` when it is None.

        Args:
            owner: The caller's id of the operation, or None.
            kind: The operation's type, for the generated owner.

        Returns:
            The owner.

        Raises:
            TypeError: `owner` is not a string.
            ValueError: `owner` is empty, or starts with `GENERATED_OWNER_PREFIX`.
        """
        if owner is not None:
            checked_owner(owner)
            if owner.startswith(GENERATED_OWNER_PREFIX):
                raise ValueError(
                    f"owners that start with {GENERATED_OWNER_PREFIX!r} are reserved for the "
                    f"ones the scheme generates, not {owner!r}"
                )
            return owner
        with self._owner_mutex:
            self._owner_count += 1
            return f"{GENERATED_OWNER_PREFIX}{kind.name.lower()}-{self._owner_count}"

    def _lease(self, operation: Operation, timeout_s: float, ticket: Ticket | None) -> Lease:
        """Claim the ticket and the owner, take the locks (`_take`), and clean up on failure.

        On any failure the owner is accepted again and a ticket that had not entered is
        withdrawn, so the agent's later calls need not wait for this one.

        Args:
            operation: The checked operation.
            timeout_s: The checked timeout.
            ticket: The call's ticket, or None.

        Returns:
            The lease.

        Raises:
            LockTimeoutError: The locks were not free in time; none is held.
            LockCancelledError: The ticket was withdrawn while the call waited.
            LockError: The owner already has a lease or a pending call, or the ticket serves
                another lock call or was used or withdrawn already.
            TypeError: The ticket is not a `Ticket`.
            ValueError: The ticket belongs to another scheme.
        """
        if ticket is not None:
            if not isinstance(ticket, Ticket):
                raise TypeError(f"a ticket must be a Ticket from issue(), not {ticket!r}")
            if ticket._order is not self._order:
                _abandon(ticket)
                raise ValueError(f"{ticket!r} was issued by another locking scheme")
            self._order.claim(ticket)
        with self._owner_mutex:
            refused = operation.owner in self._live
            if not refused:
                self._live.add(operation.owner)
        if refused:
            if ticket is not None:
                self._order.fail(ticket)
            raise LockError(
                f"{operation.owner!r} already has a lease or a pending call; "
                "an owner never takes a lock twice"
            )
        try:
            return self._take(operation, timeout_s, ticket)
        except BaseException:
            self._unclaim(operation.owner)
            if ticket is not None:
                self._order.fail(ticket)
            raise

    def _unclaim(self, owner: str) -> None:
        """Accept `owner` again: its lease ended or its call failed.

        Args:
            owner: The operation's id.
        """
        with self._owner_mutex:
            self._live.discard(owner)

    @abstractmethod
    def _take(self, operation: Operation, timeout_s: float, ticket: Ticket | None) -> Lease:
        """Wait at most `timeout_s` for the turn and the locks of `operation`; return the lease.

        The lease must call `_unclaim` when it is released; a claimed ticket must end ENTERED
        (`_CallOrder.attach` or `finish`) when the call returns its lease.

        Args:
            operation: The checked operation, its owner claimed.
            timeout_s: The checked timeout.
            ticket: The call's ticket, claimed for it, or None.

        Returns:
            The lease.

        Raises:
            LockTimeoutError: The turn or the locks did not come in time; none is held.
            LockCancelledError: The ticket was withdrawn while the call waited; none is held.
        """


class RWLockingScheme(LockingScheme):
    """The scheme with real locks, taken from a `LockSpace` (the backend).

    Attributes:
        space: The backend's locks.
        timeouts: The default and maximum waits.
    """

    def __init__(self, space: LockSpace, timeouts: LockTimeouts | None = None) -> None:
        """Create the scheme on `space`.

        Args:
            space: The backend's locks.
            timeouts: The default and maximum waits; None for `LockTimeouts()`.
        """
        super().__init__(timeouts)
        self.space = space

    def plan(self, operation: Operation) -> tuple[tuple[str, LockMode], ...]:
        """Return the locks `operation` takes, in the order it takes them (`lock_order`).

        Args:
            operation: The operation.

        Returns:
            (key, mode) pairs as in the module docstring's table.
        """
        steps: list[tuple[str, LockMode]] = []
        match operation.kind:
            case OperationKind.COMMAND:
                outside = LockMode.EXCLUSIVE if operation.writes_outside else LockMode.SHARED
                steps.append((GLOBAL_KEY, outside))
                steps.extend((worktree_key(w), LockMode.EXCLUSIVE) for w in operation.worktrees)
            case OperationKind.EDIT:
                worktree = operation.worktrees[0]
                steps.append((worktree_key(worktree), LockMode.SHARED))
                if operation.path is not None:
                    steps.append((file_key(worktree, operation.path), LockMode.EXCLUSIVE))
            case OperationKind.EDIT_OUTSIDE:
                steps.append((GLOBAL_KEY, LockMode.EXCLUSIVE))
        return tuple(sorted(steps, key=lambda step: lock_order(step[0])))

    def _await_turn(self, ticket: Ticket, deadline: float) -> bool:
        """Wait until the earlier calls of the ticket's agent entered or were withdrawn.

        Args:
            ticket: The call's claimed ticket.
            deadline: The latest moment (`time.monotonic`) to wait for.

        Returns:
            True when it is the call's turn, False when the deadline passed first.

        Raises:
            _Cancelled: The ticket was withdrawn while the call waited.
        """
        return self._order.wait_turn(ticket, deadline)

    def _take(self, operation: Operation, timeout_s: float, ticket: Ticket | None) -> Lease:
        """Wait for the turn, then take the locks of `plan(operation)` within one deadline.

        Each lock is requested (its place in the queue is fixed) and then waited for, with the
        time left until the deadline. The ticket enters when the last request is queued. After
        each wait the call checks whether its ticket was withdrawn meanwhile. When a lock does
        not come in time, or the call is cancelled, the locks already held are released.

        Args:
            operation: The checked operation, its owner claimed.
            timeout_s: The checked timeout.
            ticket: The call's ticket, claimed for it, or None.

        Returns:
            The lease of all its locks.

        Raises:
            LockTimeoutError: The turn or a lock did not come in time; none is held.
            LockCancelledError: The ticket was withdrawn while the call waited; none is held.
        """
        started = time.monotonic()
        deadline = started + timeout_s
        holds: list[tuple[RWLock, Hold]] = []
        try:
            if ticket is not None and not self._await_turn(ticket, deadline):
                waited = time.monotonic() - started
                raise LockTimeoutError(operation, f"order:{ticket.agent}", None, timeout_s, waited)
            plan = self.plan(operation)
            for index, (key, mode) in enumerate(plan):
                lock = self.space.lock(key)
                request = lock.request(mode, operation.owner)
                try:
                    if ticket is not None:
                        self._order.attach(ticket, request, enter=index == len(plan) - 1)
                    hold = request.wait(max(0.0, deadline - time.monotonic()))
                except BaseException:
                    request.withdraw()
                    raise
                if hold is not None:
                    holds.append((lock, hold))
                if ticket is not None and self._order.detach(ticket):
                    cancelled = LockCancelledError(operation, time.monotonic() - started)
                    _release_all(holds, cancelled)
                    holds.clear()
                    raise cancelled
                if hold is None:
                    waited = time.monotonic() - started
                    raise LockTimeoutError(operation, key, mode, timeout_s, waited)
            if ticket is not None:
                self._order.finish(ticket, enter=not plan)
            return Lease(operation, holds, timeout_s, time.monotonic() - started, self._unclaim)
        except _Cancelled:
            cancelled = LockCancelledError(operation, time.monotonic() - started)
            _release_all(holds, cancelled)
            raise cancelled from None
        except BaseException as exc:
            _release_all(holds, exc)
            raise


class NoLockingScheme(LockingScheme):
    """The drop-in scheme for when locking is off: same calls and argument checks, no locks.

    Its leases hold nothing; every call returns at once. A ticket enters at once, without
    waiting for the agent's earlier calls. The owner rule still holds: a second lease of an
    owner is refused with `LockError`, as with `RWLockingScheme`.

    Attributes:
        timeouts: The default and maximum waits (checked, never waited).
    """

    def _take(self, operation: Operation, timeout_s: float, ticket: Ticket | None) -> Lease:
        """Return a lease without locks at once; the ticket enters.

        Args:
            operation: The checked operation, its owner claimed.
            timeout_s: The checked timeout, kept on the lease.
            ticket: The call's ticket, claimed for it, or None.

        Returns:
            The empty lease.

        Raises:
            LockCancelledError: The ticket was withdrawn before the call finished.
        """
        if ticket is not None:
            try:
                self._order.finish(ticket, enter=True)
            except _Cancelled:
                raise LockCancelledError(operation, 0.0) from None
        return Lease(operation, (), timeout_s, 0.0, self._unclaim)
