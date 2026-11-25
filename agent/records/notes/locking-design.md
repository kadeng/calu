# Locking edits against commands, per checkout (design, not built)

The owner proposed this design on 2026-11-24. Inside a checkout, commands take a read/write
lock exclusively and edits take it shared. Outside the checkouts it is the other way round. This
note works the proposal out and revises it after an adversarial review.

## Status (2026-11-25): the locking component is built, the wiring is not

The owner then asked for the scheme as an isolated component with a pluggable lock, a fake
scheme that does not lock, and a fuzz test (PLAN.md D274). `src/calu/locking/` implements
it; nothing in Calu imports it yet. It differs from the design below in three points:

- **One global lock** for everything outside the worktrees, as the owner proposed. The per-place
  locks of open decision 1 can follow by giving `edit_outside_worktree` a place.
- **Deadlock freedom by lock order, not by all-or-nothing grants.** A pluggable lock per key (a
  file lock or a PostgreSQL advisory lock later) cannot be granted together with other keys by
  one central table. So every operation takes its locks in one total order (global, worktrees
  sorted, file), and every wait has a deadline. With one order, no cycle of waiting operations
  can form; a wait that does last ends at the deadline (`LockTimeoutError`, nothing held).
- **Per-file locks for edits** (review finding 22) and `writes_outside` for commands (finding 7)
  are options of the calls.

The owner then decided the lock semantics (PLAN.md D274): the lock of every backend is FIFO;
the owner of a lock is one tool call, so two calls of one agent wait for each other like calls
of two agents; the calls of one agent enter the lock queues in the order the agent issued them
(`issue` gives a ticket at issue time, `Ticket.withdraw` for a denied call or one without
locks); an owner that asks again for a lock it holds is refused at once with `LockError`. With
these rules the same-agent exception D3 below no longer applies: calls of one agent wait for
each other like calls of different agents, and a later call cannot hold up an earlier one,
because it enters the queues after it.

The fuzz layer (`tests/fuzz/`, docs/TESTING.md) confirms the order. The real scheme showed no
cycle in the wait-for graph and no stall that only a timeout ended, under random load and under
a load of commands across worktrees. Two schemes with a wrong order deadlocked within
seconds, and both detectors saw it; so did schemes that leave out a lock or ignore the issue
order. Still to do: the backend (file locks or PostgreSQL), then the wiring below (assumptions
A1-A7, the hook, the release on the tool result, the path through a person, issuing tickets in
the order Claude issues its calls). Async callers wait in a worker thread; a lease that arrives
after the caller was cancelled must be released.

An adversarial review of the component (2026-11-25, PLAN.md D274) found the lock order and the
thread backend sound, and defects in the ticket layer that are fixed now. What the wiring must
keep in mind:

- Every ticket must end ENTERED or WITHDRAWN on every path: a denied check, an error in the
  check, a cancelled tool call. One ticket left ISSUED holds up its agent until each later call
  times out. `with scheme.issue(agent) as ticket:` withdraws an unused ticket at the end of the
  block; a lock call that fails withdraws its own ticket.
- Tickets follow the order of the tool_use blocks in the stream, not the order in which the
  hooks happen to run.
- A cancelled tool call withdraws its ticket from any thread: a pending lock call then gives
  back what it holds and raises `LockCancelledError`.
- A lease has no finalizer: a lease that is lost keeps its locks for the life of the process.
  Every end of a call (tool result, runner end, session stop) releases it; `release` may be
  called more than once.
- Lock calls run in worker threads (an asynchronous exception in a waiting thread can leave a
  lock taken).
- Open question: the global lock is taken first. A command that waits for its worktree then
  holds the global lock shared, and an edit outside waits behind it; with FIFO, every new
  command in every worktree then queues behind that edit. Taking the global lock last shortens
  those waits (the reviewer measured about half), but a command that waits for the global lock
  then holds its worktree, so edits there wait too. Either way, one long command plus one
  waiting edit outside stops new commands in every worktree until the command ends or the edit
  times out.

Everything below is the design for the wiring.

## Goal

Several agents may work at once: the main agent, foreground and background subagents, and
agents in other worktrees. The design has four goals for them:

- **Isolation.** No edit lands in a checkout while a command runs there, and two commands of
  one checkout never run at once. Outside the checkouts, no write lands in a place while a
  command that uses the place runs.
- **Knowledge.** Every change that a command can execute was known to the check whose approval
  the command runs with. Either the change was in the check's context, or the command was
  checked again with the change in front of the checker.
- **No deadlock.** Every wait ends. The only wait without a bound is a running call's own run.
- **Approvals unchanged.** The locks never change who approves a call. The checkers, auto
  mode's classifier, Claude Code's own ask and deny rules and, for escalations, the person decide
  as they do today.

## Where today's guarantee ends

The checks of all agents run concurrently. A command's check sees the steps approved before
the check began. A step of another agent that is approved later can be applied before the
command runs:

```
A: Edit run.sh   ──check (slow)──────────────────┐approved─► applied
B: bash run.sh   ──check (fast)──┐approved────────────────────────► runs
                                 B's checker did not see A's edit
```

## Places and lock modes

A *place* is a unit that the lock table locks. There are two types of place:

- A **checkout**: the main checkout or a worktree. Its key is its git admin directory: `.git`
  for the main checkout, `.git/worktrees/<id>` for a linked worktree. The key stays the same
  when a worktree moves.
- An **outside place**: state outside the checkouts that commands use. The repository's common
  git directory (configuration and hooks) is one. The user's configuration files in the home
  directory, a shared environment outside the checkouts (for example a venv set by
  `UV_PROJECT_ENVIRONMENT`) and the session's scratch directory are others. Every other
  outside path that a step names is a place too; its key is the path's top directory.

| Step | Checkout locks | Outside-place locks |
|---|---|---|
| Edit, Write, MultiEdit, NotebookEdit inside a checkout | shared on its checkout; exclusive on the file | none |
| Edit or Write outside the checkouts | none | exclusive on its place |
| Command: Bash, and every tool that the rows below do not name | exclusive on its checkouts | shared on the places it names and on the implicit places (common git directory, home configuration, shared environments); exclusive on the places it writes |
| Command on the no-lock list: it runs no project code (`ls`, `cat`, `head`, `wc`, `grep` and `rg` that execute nothing, `find` without `-exec` or `-delete`) | none | none |
| Read, Grep, Glob, WebFetch, WebSearch, TodoWrite, ToolSearch | none | none |
| Tools that start agents or wait for them: Agent, Task, Skill, SendMessage, Calu's own MCP tools | none | none |

The modes are inverted inside a checkout. A command may write and run anything in its checkout,
so commands exclude each other and all edits. An edit excludes only other edits of the same file.
A command is a reader of the outside places, and their writers exclude it.

The places a command writes come from three sources: its redirections, its path arguments, and
a list of commands known to change shared state. The list holds `git config`, `git worktree`,
hook installers and package managers.

The checkouts of a command are the checkouts of five things:

- its working directory;
- every `cd` and `pushd` target;
- every path in its words: arguments, assignment values, and code strings given to an
  interpreter;
- the checkouts nested in those, such as worktrees in `.claude/worktrees`, unless worktrees are
  kept outside the main checkout (A7).

`git` commands are not on the no-lock list, because git runs programs that the repository
configures (fsmonitor, filters, textconv, hooks).

## The life of a call

```
 Claude Code                       Calu: PreToolUse hook                         Calu: stream
 ───────────                       ─────────────────────                         ────────────
 tool_use ───────────────────────► places = classify(step)
                                   snap = versions(places) + instruction version
                                   verdict = check(step)    (in parallel with all other checks)
                                   answer = as today: deny / ask / allow / no decision
                                   deny or ask? ──► return it, no lock
                                                    (after a person allows, can_use_tool
                                                     takes the locks, see below)
                                   wait for all locks: FIFO, all or nothing, deadline
                                     deadline passed ──► deny "busy: not run, retry"
                                   classify again; versions still equal to snap?
                                     no ──► release, delta check, new snap, wait again
                                   record the step; bump the versions of the places it writes
 ◄──── allow / no decision ─────── return answer          (the locks stay held)
 the classifier and the rules decide
   (a person is asked? ──────────► can_use_tool: release, ask, wait for the
                                   locks again, delta check if anything changed)
 the tool runs
 PostToolUse hooks (all of them)
 tool_result ────────────────────────────────────────────────────────────────► log the outcome;
                                                                                release
```

## Pseudocode

```
LockTable                       # one per Calu process: one mutex, one FIFO queue
  wait(call, {place: mode}, deadline) -> granted | timed_out
      # Grants all places at once or none. A request is granted when it is compatible with
      # the holders and no earlier queued request conflicts with it on one of its places.
  release(call)

on PreToolUse(step):
    places = classify(step)
    snap = snapshot(places)                      # taken together with the check's context
    verdict = check(step, context)
    answer = runner_answer(verdict)              # unchanged: deny / ask / allow / no decision
    if answer in {deny, ask} or not places:
        keep(step, places, snap)                 # can_use_tool uses them after a person allows
        return answer
    return lock_and_validate(step, places, snap, answer, deadline = hook start + LOCK_WAIT_S)

lock_and_validate(step, places, snap, answer, deadline):
    for round in 1, 2, 3, ...:
        if lock_table.wait(step, places, deadline) is timed_out:
            record(step, "postponed: busy")      # no checker said no
            return deny(BUSY_NOTE)               # "Not run: <holder> runs in <checkout>.
                                                 #  Nothing changed. The call can be retried."
        grown = classify(step) - places
        if grown:                                # paths changed during the wait
            release(step); places |= grown; continue
        if snapshot(places) == snap:
            break
        changes = change_log.since(snap, places)
        if round == 1:
            release(step)                        # the delta check runs lock-free
            verdict = delta_check(step, changes)
        else:
            if deadline and time left < CHECK_BOUND_S: release(step); return deny(BUSY_NOTE)
            verdict = delta_check(step, changes, hold=False)  # under the locks; never waits
                                                              # for a person
        snap = snapshot(places)
        if not approves(verdict):
            release(step)
            return runner_answer(verdict)        # deny, or ask: the person decides lock-free
        if round > 1: break                      # nothing in its places changed meanwhile
    record(step); bump(places it writes); change_log.add(step)
    return answer                                # the locks stay held

on can_use_tool(step):                           # Calu's escalation or Claude Code's own ask
    release(step)                                # a person never decides while a call holds locks
    if not person_allows(step): return deny
    return lock_and_validate(step, places, snap, allow, deadline = none)
        # A delta check that escalates here asks the person again and shows the changes
        # since their decision.

on tool_result(call):                            # every end of a call: success, error,
    change_log.outcome(call)                     # denial by the classifier or a rule,
    lock_table.release(call)                     # hook timeout, interrupt
on hook cancelled:              release(call) in `finally`
on runner exit, session stop:   release every call of that runner
```

## Why it cannot deadlock

```
queued request ──waits for──► the holders of its places, and earlier queued requests that conflict
holder         ──waits for──► Claude Code running its call: classifier, rules, tool, hooks (each has a timeout)
                              a delta check: a model call with a deadline, without `_hold`
                              a person answering a network question of its running command
```

- A queued request holds nothing, because the table grants all places at once or none.
- A holder never waits for a lock or for a queued request. A person never decides while the
  step holds locks. The one exception is a network question of a running command, and the
  person's answer takes no lock.
- So the wait-for graph has no cycle, and no lock order is needed. FIFO prevents starvation.
- Every lock wait in the hook has a deadline. When it passes, the call is not run and can be
  retried.
- A holder ends when its call ends. The only hold without a bound is a running call, which is
  the purpose of the isolation. The web UI and the IDEs show which call holds which place.

Two cases remain in which work stalls until a timeout ends it. In the first, a command waits
for a change that another agent is to make in the same checkout: the edit waits for the command,
and the command's timeout or the edit's deadline ends the stall. The second case arises only if
A1 is false.

## Knowledge

- `version[place]` increments when a step that writes gets the place's lock. The step is
  recorded first, so a context built after the increment contains the step.
- A check's snapshot holds the versions of the step's places and the session's instruction
  version. Every user message and every decision note of a person increments the instruction
  version.
- A changed snapshot leads to a **delta check**. The command checker gets the step, how it was
  approved (by a checker, a rule or a person), and the change log of the step's places since
  the snapshot. Each log entry holds the path, the checker's description, the diff for edits and
  the outcome. The checker answers whether the approval still holds. A command that a rule
  approved gets a delta check from the command checker too, because rules read no history.
- The change log is kept per place, and all sessions of the Calu process share it, so a change
  made by another session is known as well. The log is not truncated, within generous limits.
  The running turn and folding can drop steps from the context; the log keeps them.

## Assumptions to verify first (spike)

- **A1.** Within one agent, Claude Code calls the PreToolUse hook of a call that is not
  concurrency-safe only after the previous call's tool_result.
- **A2.** Every tool_use gets exactly one tool_result in the stream. This includes denials by
  auto mode's classifier or by a settings rule, hook timeouts and interrupts.
- **A3.** Claude Code emits the tool_result after all PostToolUse hooks of the call have ended,
  including hooks from settings files.
- **A4.** A PreToolUse callback that waits delays no other call. This includes calls of the same
  agent that Claude Code runs concurrently.
- **A5.** The model retries a call after a "busy: not run" denial. Calu records the denial as a
  postponement, so it never counts as a checker's denial.
- **A6.** The stream task processes a tool_result promptly even while hooks wait, so the next
  call of the same agent waits for the release for milliseconds at most.
- **A7.** Claude Code's WorktreeCreate hook (a shell hook in the settings files) can place agent
  worktrees outside the main checkout. Main-checkout commands then need no locks of nested
  worktrees.

## Performance

- One agent: no waits, because its calls are serial (A1). The cost per call is a few dictionary
  operations under one mutex, without I/O.
- Several agents in one checkout: edits run in parallel. Commands run one at a time and never
  during an edit, so a waiting call waits for the remaining time of the running command. A delta
  check costs one checker call (a few seconds). It runs only when a change in the same places
  was granted between a command's check and its start.
- Agents in separate worktrees: no waits, with two exceptions. Writers of outside places wait,
  and these writers are rare (package installs into a shared environment, git configuration).
  Main-checkout commands wait when worktrees are nested in the main checkout.
- A shared environment outside the checkouts makes each command that changes it wait for all
  commands that use it. Per-worktree environments avoid this.

## Boundaries

- The locks cover a call while it runs. Processes that keep running after their call
  (background shells, servers, containers) are outside them. While Calu knows of such a process
  in a checkout, the edit checks there are told that the edited file may run at once.
- A command's places come from its words, its working directory and the implicit places. The
  sandbox and the deny rules, not the locks, bound what a command reaches beyond them.
- A command that changes shared state outside the checkouts takes the exclusive lock when its
  words name the target or when the command is on the writer list.
- Hooks configured in Claude Code's settings run programs that no Calu check sees. This is
  true today, independent of the locks.
- The locks hold within one Calu process. Calu does not lock agents of other Calu processes,
  the person's own edits outside the IDE sync, or other programs. Calu's own writes (the IDE
  sync, trace finalization) take the locks of an edit.
- A network question of a running command keeps the command's checkout locked until the person
  answers.

## Review of the first version (2026-11-24)

An adversarial review found no cycle among lock holders that only execute, and 22 problems. The
concrete cases are in the conversation of that day and are not recorded here. The design above
adopts these rules from the review:

- A lock wait never answers "ask". An "ask" sends the call to `can_use_tool` without auto mode's
  classifier and in place of Claude Code's own ask rules (spike-results.md), so the approvals
  would change whenever agents contend for a lock.
- The release comes with the call's tool_result. Every end of a call produces one. A PostToolUse
  hook is missing when a call does not run, and it runs concurrently with hooks from the
  settings files.
- No watchdog releases a lock while its call may still run. Waits have deadlines instead.
- A delta check never outlasts the hook's budget, and a step that holds locks never waits for a
  person.
- Escalated steps take the locks after the person's decision and are validated like all others.
- Skill and every tool that waits for agents take no lock.
- A checkout is keyed by its git admin directory. The change log is shared across sessions.
- Delta checks get the change log without truncation, plus the instruction version. Rule
  approvals get a delta check from the command checker.
- `git` is not on the no-lock list. Places are re-classified after the wait. An edit of a file
  with more than one hard link takes the exclusive lock of every place, because Calu does not
  know where the other links are.
- Outside places have one lock each instead of one global lock. With a single global lock, an
  edit of a scratch file waits for the longest-running command in every worktree, and all new
  commands queue behind it.
- Write sets (D31) take "before" after the grant and "after" before the release. The undo runs
  under the locks and only if the files are unchanged since "after".

## Open decisions

1. Outside the checkouts: one lock per place (recommended) or one lock for everything.
2. Nested worktrees: either main-checkout commands lock them, which serializes the main agent's
   commands with the worktree agents, or worktrees move outside the main checkout if A7 holds
   (recommended if it holds).
3. Shared environments outside the checkouts: lock them as above, which costs parallelism, or
   recommend per-worktree environments in the documentation.

## Build order

1. A spike for A1-A7.
2. Checkout locks with versions, the change log, delta checks, the release on the tool_result,
   the busy denial, and the path through a person (escalations and Claude Code's own asks).
   This covers agents that share a checkout, the common case of the main agent with
   subagents.
3. Write sets under the locks (D31).
4. Outside places (decisions 1 and 3).
5. Nested worktrees (decision 2).
