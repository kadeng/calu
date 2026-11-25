"""Locks that keep edits away from running commands, per worktree (agent/records/PLAN.md D274).

Not wired into Calu yet: nothing outside this package and its tests imports it.
`calu.locking.scheme` defines which locks commands and edits take and in which order the calls
of an agent get them (`RWLockingScheme`, and `NoLockingScheme` for when locking is off).
`calu.locking.rwlock` defines the FIFO shared/exclusive lock a backend implements;
`calu.locking.threadlocks` is the backend for the threads of one process, and
`calu.locking.filelocks` the one for the processes of one user on one machine.
"""

from calu.locking.filelocks import FileLockRequest, FileLockSpace, FileRWLock
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
from calu.locking.scheme import (
    GENERATED_OWNER_PREFIX,
    GLOBAL_KEY,
    Lease,
    LockCancelledError,
    LockingScheme,
    LockTimeoutError,
    LockTimeouts,
    NoLockingScheme,
    Operation,
    OperationKind,
    RWLockingScheme,
    Ticket,
    TicketState,
    file_key,
    lock_order,
    worktree_key,
)
from calu.locking.threadlocks import ThreadLockRequest, ThreadLockSpace, ThreadRWLock

__all__ = [
    "GENERATED_OWNER_PREFIX",
    "GLOBAL_KEY",
    "FileLockRequest",
    "FileLockSpace",
    "FileRWLock",
    "Hold",
    "Lease",
    "LockCancelledError",
    "LockError",
    "LockMode",
    "LockRequest",
    "LockSpace",
    "LockState",
    "LockTimeoutError",
    "LockTimeouts",
    "LockingScheme",
    "NoLockingScheme",
    "Operation",
    "OperationKind",
    "RWLock",
    "RWLockingScheme",
    "ThreadLockRequest",
    "ThreadLockSpace",
    "ThreadRWLock",
    "Ticket",
    "TicketState",
    "checked_owner",
    "checked_timeout",
    "file_key",
    "lock_order",
    "worktree_key",
]
