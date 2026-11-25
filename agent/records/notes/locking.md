# Locking edits against commands: the design

This is the reference for Calu's locking (PLAN.md D274). It states what locking guarantees,
how it works, and why each decision was made. The exploration that led here, with the first
adversarial review of the design, is in `locking-design.md`.

Status (2026-11-25): the component `src/calu/locking/` is built and tested, with a backend for
the threads of one process and one for the processes of one user on one machine (section 8).
The wiring into Calu (section 9) comes next.

## 1. What locking guarantees

Calu checks every tool call before it runs, and the checks of different calls run
concurrently. Several agents can work at once: the main agent, foreground and background
subagents, agents in other worktrees. Without locking, an edit of one agent can land between
another agent's command check and that command's run, and the command then executes something
its check never saw.

Locking provides four things:

- **Isolation.** No edit lands in a worktree while a command runs there, and two commands of
  one worktree never run at once. No edit outside the worktrees lands while any command runs.
- **Order.** The calls of one agent enter the lock queues in the order the agent issued them.
- **Liveness.** No deadlock, and every wait has a bound.
- **Unchanged approvals.** Locking never changes who approves a call: the checkers, auto
  mode's classifier, Claude Code's own rules and, for escalations, a person.

Locking does not by itself make a command's check know of an edit that landed after the check
began. That needs a second check when the command gets its locks (section 9, "Not built").

## 2. The scheme

| Call | Worktree locks | File lock | Global lock |
|---|---|---|---|
| `run_command_in_worktree(w)` | `w`: exclusive | - | shared (1) |
| `run_command_in_worktrees(ws)` | each `w` in `ws`: exclusive | - | shared (1) |
| `edit_in_worktree(w)` | `w`: shared | - | - |
| `edit_in_worktree(w, path=p)` | `w`: shared | `(w, p)`: exclusive | - |
| `edit_outside_worktree()` | - | - | exclusive |

(1) Exclusive with `writes_outside=True`: the command also writes outside its worktrees.

Inside a worktree the usual reader/writer roles are inverted. A command may write and run
anything in its worktree, so it excludes every other command and every edit there. An edit
changes one file and runs nothing, so edits share the worktree; only two edits of the same file
exclude each other. Outside the worktrees the roles are the usual ones: commands read and run
what is there, edits write it.

Every operation takes its locks in this order: the worktree locks (sorted by id), the file
lock, the global lock last.

## 3. Decisions and their reasons

| # | Decision | Reason |
|---|---|---|
| 1 | Commands take their worktree exclusively, edits shared (the owner, 2026-11-24) | A command can run any file of its worktree, so no edit may land while it runs; edits of different files do not interfere, so they need not wait for each other |
| 2 | One global lock for everything outside the worktrees (the owner) | Simple, and outside edits are rare. Locks per outside place (scratch directory, shared environment, git configuration) were considered; they can follow by giving `edit_outside_worktree` a place |
| 3 | An edit that names its file takes that file's lock exclusively | Two shared edits of one file could lose an update |
| 4 | A command that writes outside says so (`writes_outside`) and takes the global lock exclusively | Its writes outside could change what other commands run; Calu cannot see such writes, so the caller declares them |
| 5 | Deadlock freedom by one total lock order plus bounded waits | A pluggable lock per key (files, PostgreSQL) cannot be granted together with other keys by one central table. One order rules out cycles; deadlines end any wait |
| 6 | The global lock comes last (the owner, 2026-11-25) | With it first, a command that waits for a busy worktree already holds the global lock, so an edit outside waits for every command queued there, and new commands everywhere queue behind that edit. With it last, a waiting command holds nothing. Measured: section 7 |
| 7 | FIFO for every backend (the owner) | Requests are granted in arrival order, shared ones at the front together, and a waiting exclusive request keeps later shared ones out: neither commands nor edits starve |
| 8 | The owner of a lock is one operation, one tool call (the owner) | Two calls of one agent must wait for each other like calls of different agents; a thread or an agent as owner would let them pass each other |
| 9 | Issue order within an agent, through tickets (the owner) | A later call whose check finished first must not overtake an earlier call of the same agent. `issue(agent)` numbers a call when it is issued; a call enters the queues after the agent's earlier calls entered or were withdrawn. A call has entered when its request for its last lock is queued |
| 10 | No reentrancy: a second request of an owner is refused at once with `LockError` (the owner) | An owner never needs a lock it holds; such a request is a bug, and waiting on it would deadlock until a timeout |
| 11 | Two-step lock API: `request` takes the place in the queue, `wait` waits for the grant | Decision 9 needs the moment at which a request is queued, before it is granted |
| 12 | Withdrawing the ticket of a pending call cancels it (`LockCancelledError`) | A cancelled call must never run after the agent's later calls. It is a separate error because the caller gave the call up, which is no bug |
| 13 | A default and a maximum wait for commands and for edits; a call may pass its own wait, which is cut to the maximum; no infinite wait (the owner) | A waiting call blocks its agent, so every wait needs a bound. Defaults 600 s (one command at Claude Code's longest Bash timeout can finish), maxima 3600 s |
| 14 | `NoLockingScheme`: the same calls and checks, no locks (the owner) | Locking costs time (section 7). When it is off, Calu still makes every locking call, and the no-op scheme answers it |
| 15 | Lock calls only from worker threads | An asynchronous exception (KeyboardInterrupt, a signal handler that raises) can strike inside any `with mutex:` and leave a lock or a mutex taken; no code can rule that out in CPython |
| 16 | Generated owners start with `generated:`, which callers may not use | A caller's id such as `command-1` must never collide with a generated one |

## 4. The component

```
src/calu/locking/
  rwlock.py       the contract every backend keeps: RWLock (request, release, acquire),
                  LockRequest (wait, withdraw), LockSpace (lock, snapshot), Hold, LockState,
                  checked_timeout, checked_owner, LockError; notes for future backends
  threadlocks.py  ThreadRWLock, ThreadLockSpace: the backend for the threads of one process
  scheme.py       LockingScheme (the calls, issue, live_owners, open_tickets), RWLockingScheme
                  (plan, the lock order), NoLockingScheme, LockTimeouts, Lease, Ticket,
                  LockTimeoutError, LockCancelledError
```

A caller:

```python
with scheme.issue(agent_id) as ticket:        # when the agent issues the call
    ...                                       # the call's check
    if the check allowed the call and it takes locks:
        lease = scheme.edit_in_worktree(worktree, ticket=ticket, owner=tool_use_id)
# Leaving the block withdraws the ticket unless the call got its lease.
...                                           # the call runs
lease.release()                               # on its tool result, from any thread
```

Another thread cancels a pending lock call with `ticket.withdraw()`. `live_owners()` and
`open_tickets()` report what is pending.

The lock contract (`rwlock.py`): a request takes its place in the queue when it is made;
grants follow the queue (FIFO); every request names its owner; an owner never asks twice;
`wait` waits at most its timeout, and a timeout, an invalid timeout or an exception in the wait
withdraws the request; a hold is released from any thread; `withdraw` wakes a `wait` blocked in
another thread.

## 5. Guarantees and their limits

The guarantees of section 1 hold for calls that go through the scheme while they run. Their
limits:

- A call's locks cover the call while it runs. A process that keeps running after its call
  (a background shell, a server, a container) is outside them.
- A command's worktrees are those its caller names. The sandbox and the deny rules, not the
  locks, bound what a command reaches beyond them.
- Writes outside the worktrees by a command are covered when the caller declares them
  (`writes_outside`).
- The locks cover the processes that share one lock space: the threads of one process with the
  thread backend; the processes of one user on one machine with the file backend (section 8).
  Changes made by people or by programs outside Calu are not covered.
- One long command plus one waiting edit outside stops new commands in every worktree until the
  command ends or the edit times out; this follows from FIFO on the global lock.
- Lock calls run in worker threads (decision 15).

## 6. Calls of one agent in parallel

If Claude Code runs several calls of one agent at once (to be verified: the assumption A1 of
`locking-design.md` says it runs a call that is not concurrency-safe only after the previous
one ended), the issue order applies. The owner's example: an agent issues an edit, a command
and an edit in one worktree, in that order and in parallel. The command waits for the first
edit, and the second edit waits for both. With the global lock last, the command enters the
agent's order only once it has its worktree and queues for the global lock; until then the
agent's later calls wait for their turn.

## 7. Tests and measurements

- `tests/unit/test_locking.py`: deterministic tests of the backend and the scheme, including
  the owner's edit/command/edit example with checks that finish in reverse order, every
  failure path of a ticket, cancellation, and the reason for the lock order.
- `tests/fuzz/` (marker `fuzz`, never in CI; docs/TESTING.md): simulated agents issue batches
  of parallel calls whose checks finish out of order, with withdrawn and cancelled calls,
  invalid arguments, probes of owners and tickets, releases from other threads and short
  timeouts. It counts timeouts per operation type and the calls that run and wait, and looks
  for deadlocks (cycles in the wait-for graph, and stalls that only a timeout ended), overlaps,
  breaks of the issue order, and locks, owners or tickets left after the run. Six schemes that
  are wrong on purpose show that it finds what it looks for.
- An adversarial review (2026-11-25) found the lock order and the backend sound and five defects
  in the ticket layer, all fixed (PLAN.md D274).

The cost of locking, one scenario run with and without it (six agents in two worktrees, 1 ms
standing for 1 s): 1.34 to 1.47 times as long with locks; without them about 180 to 225
overlaps or breaks of the issue order per run.

The lock order, same scenarios, average wait in milliseconds (p95 in brackets):

| Scenario | Order | Commands | Edits | Edits outside |
|---|---|---|---|---|
| 6 agents, 2 worktrees, 5 % edits outside | global first | 33 (116) | 27 (98) | 41 (86) |
| | global last | 25 (81) | 28 (96) | 28 (54) |
| 8 agents, 2 worktrees, 12 % edits outside | global first | 126 (286) | 41 (191) | 148 (305) |
| | global last | 71 (183) | 67 (194) | 61 (193) |

With one agent per worktree both orders take the same time. Global first only wins when many
commands write outside: such a command then holds its worktree while it waits for every
running command.

## 8. Backends

- **Threads of one process** (`ThreadLockSpace`): one `calu serve` runs all its sessions in one
  process, so this backend covers them.
- **Processes of one user on one machine** (`FileLockSpace`, PLAN.md D275): two `calu run`, or
  `calu serve` and `calu run`, on the same checkout. Files, not PostgreSQL, for these reasons:
  - A process that dies is recognised by its process id (with its start time and the boot id),
    and its holds and requests are removed; no server has to notice a closed connection.
  - No database round trip per lock call, and locking works while the database is away.
  - It is tested across real processes in the unit layer, without a database.
  - The checkouts it protects are local to the machine anyway.

  How it works: one state file (`locks.json`) holds the FIFO queue of every lock. A short
  `flock` on a separate file guards each change; flock belongs to an open file description, and
  every change opens the file anew, so threads exclude each other as well as processes. The new
  state is written to a temporary file and renamed, so a reader always sees a whole state.
  Grants follow the same rules as the thread backend (the same algorithm). A waiting request
  polls its entry, from every millisecond up to every 20 ms, and a change of its own lock in its
  own process wakes it at once. Every change removes the entries of processes that no longer run;
  a waiting request looks for them on its lock every 0.2 s, so a killed holder is gone within a
  fraction of a second. The holds of a process belong to it: its threads release them, other
  processes cannot. The directory is private to the user (mode 0o700).

  Costs: a change takes about 0.2 ms, a read 25 us. Under the fuzzer's load (twelve agents, very
  short holds) the file backend handles about 200 calls per second, the thread backend about
  1000; real tool calls take seconds, so the difference does not show. Waking only the waiters of
  the changed lock, not all of them, doubled the file backend's throughput.

  Tested: the lock contract runs against both backends; across real processes, four processes
  that count in turns lose no update, requests of several processes are granted in FIFO order,
  the hold of a process killed with SIGKILL is given up within seconds, and an owner is refused
  across processes. The fuzzer runs on the file backend as well.
- **PostgreSQL** (not planned): its advisory locks are reentrant per session, so each lease
  would need a session of its own; a sent lock call does not say when the server queued it, so
  `request` would have to confirm the queued row in `pg_locks`; an unlock must run on the
  holding session.

## 9. Wiring into Calu (next)

The owner's decisions of 2026-11-25: locking is off by default and switched on by
configuration, as are the default waits for edits and commands; Calu always makes the locking
calls, and when locking is off the no-op scheme answers them.

- The PreToolUse hook issues a ticket per call (per agent), and after the check of an approved
  call takes its locks, with a wait bounded by the hook's time left. A call that does not get
  its locks is denied with a note that nothing ran and the call can be repeated.
- A call that goes to a person takes its locks after the person allowed it, and releases them
  before a person decides on a call that already holds them.
- The locks are released with the call's tool result, and all of them at the end of a run or a
  session.
- Commands lock the checkouts of their working directory and `cd` targets
  (`calu.checkouts`); edits lock the checkout of their file, or the global lock when the file
  is outside every checkout; other tools take no lock.

Not built: the second check of a command whose worktree changed between its check and its
locks; the handling of processes that keep running after their call.
