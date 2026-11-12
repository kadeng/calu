# Code honesty in calu: specification of the redesign (owner's decisions of 2026-11-12)

This replaces the present mark register and its enforcement. It is binding for the
implementation; where it leaves a detail open, choose the simplest thing that keeps the rules
below true, and report the choice.

## 1. What the feature is for

Code is honest when its names and documentation say what it does (the honesty rules,
`calu/defaults/checkers/honesty-rules.md`). The checkers that run in auto mode, and human
reviewers, judge call sites and commands by names and documentation. That is only sound for
code that was verified to be honest. So calu tracks which code is verified, does not let
unverified code be run or committed when honesty is enforced, and tells every checker which
code it must not judge by its documentation.

Threat model (owner): an agent that drifts, cuts corners or half-heartedly works around a
restriction. Make that hard and visible. It is not a guarantee against a determined covert
attacker; do not pay large delays for that case, and never write evasion techniques into code
comments, docstrings, prompts or documents.

## 2. Terms and state

**Trusted commit.** A commit a human or calu certified as honest. Trusted commits are stored
in the database (table `trusted_commits`): commit id, tree id, when, by whom, by which method
(`user` with the client or OS user name, `review`, `session`), optional note. A commit is
trusted if its commit id or its tree id is in the table. The table is global (a commit id
identifies its repository). It is out of the session's write reach except through one database
function that records a certification with its method.

**Baseline of a session.** The nearest ancestor of `HEAD` (HEAD included) that is trusted,
looked up when the session starts and again after every commit and after a trust command.
A repository or HEAD without a trusted ancestor has **no baseline**. Bound the search (a few
thousand commits) and say so in the docstring.

**Session honesty state** (in memory only, as decided in D180; it is recomputed from the
working tree and the baseline, never persisted):

- **Marks**: functions, methods, classes or whole files a checker found dishonest:
  (path, symbol or "" for the file, reason, who, when). A file is *dishonest* while it has at
  least one open mark.
- **Verified contents**: path -> hash of the content a checker verified. An Edit/Write/
  MultiEdit that the edit route's checkers judged without leaving an open mark on that file
  verifies the file at its content after the edit. A file an honesty review judged honest is
  verified at the content it reviewed.
- **Unreviewed files**: files whose content differs from the baseline and is not the verified
  content: changed by a shell command, by a tool calu does not check as an edit, from outside,
  or edited again since. Computed on demand from git (changed and untracked files relative to
  the baseline, ignoring what git ignores) and the verified map. Without a baseline every file
  counts as unreviewed; represent that as a flag, not as a list of every file.
- **Review required**: set when the number of open marks exceeds `honesty.max_open_marks`
  (default 3, counted over all files). It stays set until an honesty review passes.

The state is **clean** when there is a baseline, no open mark, no unreviewed file and no
review is required.

Apply a checker's marks and verifications only after the tool call succeeded (the tool result
arrived without error), never at the gate before the call runs. A denied, escalated-and-denied
or failed edit changes nothing.

## 3. Marks: opening and closing

- Opening: as today, the checkers of the command, edit and changeset routes may return
  `marks_dirty`. Validate paths as today (inside the workspace, existing or created by this
  step). For Python files, match the symbol against the definitions calu knows
  (`calu.checkers.symbols`), and tell the edit checker the exact names to use.
- Closing one mark (`marks_clean`): accepted only while review is not required, only from a
  step that edits that file, and matched by path plus normalised symbol (ignore case of a
  trailing `()`, accept the bare name for `Class.name` when unambiguous). A mark whose file was
  deleted, or whose symbol no longer exists in a Python file that parses, is closed by calu.
- More than `honesty.max_open_marks` open marks: review required. Single `marks_clean` answers
  are then ignored (and the checker is told so); only an honesty review clears the marks. This
  keeps documentation updates close to the edits they belong to.
- A human may clear a mark by hand (`honesty.clear`, as today).

## 4. The honesty review

A reviewer agent with a larger budget than a step checker, read-only tools (the existing
checker tools), and the honesty rules as its instructions. Its job: judge the difference
between the baseline and the present state for the files that are unreviewed or carry marks.

- Input: the list of those files; for each, the diff against the baseline (or the whole file
  if new), fenced as data like every other untrusted text.
- It works in batches of files run concurrently (bounded concurrency), each batch one agent
  run with a bounded request budget; it may read other files with its tools.
- Deterministic shortcuts before any model call: a file whose change is whitespace-only is
  verified without a model. (Further shortcuts, such as recognising a uniform rename, are
  welcome if they are simple and tested; they are not required.)
- Output per file: honest, or findings (symbol, reason). Honest files become verified at
  their present content; findings replace that file's marks. When no finding remains, review
  required is cleared.
- Cap: `honesty.review_max_files` (default 300). More files than that: the review does not
  run; the step that needed it is **escalated** to a human with the reason and the counts.
  (The owner's intended later solution, to be recorded in PLAN.md by the lead and not built
  now: build the call graph of the unclean functions and review in waves from its leaves, so
  each reviewer can rely on the documentation of what the reviewed function calls.)
- A review that fails (model error, timeout, malformed answer) verifies nothing; the step
  that needed it escalates. Never fail open.
- Model: `honesty.reviewer_model`, blank = the default model (`calu.config.default_model`).
  Budget settings with defaults of your choice, documented.
- Triggers: (a) a human asks for it: chat command `/honesty-review` and protocol request;
  (b) automatically at the gate when a step needs a clean state and the state has unreviewed
  files or review required but would otherwise be decidable (see section 5); not more than
  one review at a time, and do not repeat a review when nothing changed since the last one.

## 5. Enforcement (`honesty.mode`)

`off` (default): nothing of this runs. `track`: the state is kept and shown, checkers get the
note of section 6, nothing is blocked or escalated because of it. `enforce`: additionally:

**Running code needs a clean state.** The owner: code has to pass the honesty bar before it
is run or tested. Whether a command executes project code is a judgement about the command
(tests, builds that run code, scripts, interpreters, package scripts, make targets), so the
command checker makes it: while the state is not clean, the command checker's answer carries
a field saying whether the command executes code of the project, with the note of section 6
as its evidence. calu then decides:
- the command does not execute project code (reading, searching, editing, git, formatting):
  honesty does not block it;
- it executes project code and there are open marks: DENY, with the marks and their reasons,
  so Claude fixes the documentation first;
- it executes project code and the only problem is unreviewed files or review required: run
  the honesty review (section 4) and decide by its result (clean: go on; findings: DENY with
  them; over the cap or failed: ESCALATE);
- no baseline: ESCALATE, with the instruction that a person certifies a commit with `/trust`
  or asks for an honesty review.
While the state is not clean in enforce mode, the allow lists must not let a command skip the
command checker (otherwise an allow-listed test command would run unjudged). Remove the
present path matcher (`command_paths`, `blocking_marks`): it refused commands that merely name
a file. Editing dishonest code is never blocked: fixing it is an edit.

**Committing needs a clean state.** At a `git commit` (the plain form the changeset review
reads): open marks: DENY with them; unreviewed files or review required: run the review and
decide by it as above; no baseline: ESCALATE. In `track` mode a commit is never blocked.

**Certification by the session.** After a commit succeeded while the state was clean and a
baseline existed, calu records the new HEAD as trusted (method `session`), in `track` and
`enforce` mode. A commit made in any other state is not certified.

A human's allow on an honesty escalation lets the step run once; it certifies nothing.

## 6. What the checkers are told

Every checker of the command, edit and changeset routes gets, while `honesty.mode` is not
off, a note computed by calu (not by a model): the baseline (short id, or that there is
none), the open marks with reasons (the ones on this step's files first), the unreviewed files
(first 20 and the count), and whether a review is required. The fixed instruction part that
goes with it (`system/marks.md`, rewritten) says: do not rely on the names or documentation
of the listed code; read the implementation with your tools or escalate; how to record and
clear marks; that clean marks are ignored while a review is required. Keep it short.

## 7. Certifying by hand

- Chat: `/trust [<commit-or-branch>]` in `calu attach` (no argument: HEAD), protocol request
  `honesty.trust`. A branch name is resolved to the commit it points at now, in the session's
  workspace; the commit is recorded with method `user` and the client's name. The session then
  recomputes its baseline.
- Command line: `calu honesty trust <commit-or-branch> [--note TEXT]` (resolved with git in
  the current directory; needs the operator or admin credential), `calu honesty trusted`
  (list), `calu honesty untrust <commit>`. These join the existing `calu honesty show|edit|
  reset` (the honesty rules).
- The web live view shows the state and offers trust HEAD and "review now" if cheap to add;
  otherwise report it as not done.

## 8. Protocol and clients

- `honesty.update` (existing event) carries the whole state a client shows: mode, baseline,
  marks, unreviewed count and the first names, review required, whether a review is running.
- Requests: `honesty.clear` (existing), `honesty.trust`, `honesty.review`.
- `calu attach`: `/trust`, `/honesty-review`, the existing `/clear-mark`; print state changes.
- Another branch is adding protocol requests for a new terminal UI at the same time: keep
  your protocol additions together in one block per file so that a merge is mechanical.

## 9. Configuration (`honesty.*`)

`mode` (off | track | enforce), `rules` (existing), `max_open_marks` (3),
`review_max_files` (300), `reviewer_model` (blank = default model), and the reviewer's budget
settings. Each with a docstring that the settings page and docs/CONFIGURATION.md show.

## 10. What is deleted

The deterministic run-blocking by path (`command_paths`, `blocking_marks`, the interpreter
table), applying marks before the tool runs, and anything of the old register that the new
state replaces. Keep `calu.checkers.symbols` and the contract listing in the rendered edit.

## 11. Tests (no API keys, fake models and the fake Claude runner)

At least: the state computation against a real temporary git repository (baseline lookup,
unreviewed files after a shell edit, verification by an edit, a second edit invalidating it,
no baseline); the threshold (4th mark sets review required, clean marks then ignored, a
passing review clears it); marks applied only after a successful tool result; enforce mode:
a reading command passes with open marks, an executing command is denied with them, an
executing command with only unreviewed files triggers the review and follows its result, the
allow list does not skip the checker while not clean, no baseline escalates; the commit gate
and certification of the new HEAD; `/trust` and the CLI commands; the cap escalates; a failed
review escalates; track mode blocks nothing.

## 12. Files that are not part of git (added by the owner after step one had begun)

If an earlier step did not build this, the step that reads it builds it, including the
database part.

The owner: files that git does not hold -- uncommitted files, temporary files -- must be
verifiable and trustable too, by a hash of their content. This should be the exception: most
such files are in the project only for a while and are tracked in memory, which means they
are inspected again after a restart unless their trust was stored with a hash.

- **In scope.** (a) Untracked files that git does not ignore: found through git, as section 2
  says. (b) Files git ignores and files outside the repository, **when the session wrote
  them** through Edit/Write/MultiEdit or a measured write set names them, or when a person
  names them. calu does not scan ignored directories or the file system for others.
- **In memory.** These files use the same verified-content map as tracked files: path ->
  SHA-256 of the content that an edit check or an honesty review verified. A different
  content means unreviewed. The map is empty after a restart, so such a file is unreviewed
  again until it is verified again.
- **Stored trust by hash, as the exception.** A table `trusted_files` holds (SHA-256 of the
  content, the path as a hint for people, when, by whom, method, note). A file whose present
  content has a recorded hash counts as verified wherever it lies, also after a restart. Only
  a person records one: `calu honesty trust --file PATH [--note TEXT]` on the command line and
  `/trust-file PATH` in the chat (protocol: a `path` on `honesty.trust`, or its own request).
  `calu honesty trusted` lists them beside the commits; `calu honesty untrust` removes one by
  hash or path. calu itself does not store file trust: what an edit check or a review verified
  stays in memory. The session role may read the table and add to it only through a database
  function, as for commits.
- The state note for checkers (section 6) names unreviewed files of this kind like any other.
- Tests: an untracked file is unreviewed until verified; a file in an ignored directory that
  the session wrote is tracked and one it did not write is not; a stored hash makes the file
  verified after a "restart" (a fresh state) and stops doing so when the content changes.

## 13. Who established trust, and which trust is sufficient (owner, added later; overrides
sections 2, 5 and 12 where they differ)

If an earlier step did not build this, the step that reads it builds it.

The owner: calu may store trust persistently itself; every stored trust says who established
it, calu's own review or the user, and a setting decides whether that trust is sufficient.

- **Three sources, ordered by strength:** `user` (a person certified it) > `review` (calu's
  honesty review verified it) > `check` (calu's step checkers verified it: an edit the edit
  route's checkers judged without leaving a mark).
- **calu stores what it verifies.** A file content that an edit check or an honesty review
  verified is recorded in `trusted_files` by its SHA-256 with the source (`check` or `review`),
  tracked or not; a person's `calu honesty trust --file` / `/trust-file` records `user`. A
  stronger source replaces a weaker one for the same hash, never the other way round. The
  in-memory map stays as a cache of the present session. Consequence: a restart no longer
  makes verified files unreviewed.
- **Commits.** A commit the session certifies (section 5) gets the weakest source among the
  verifications of the changes it contains since the baseline, and no stronger than the
  baseline's own source: all by honesty review -> `review`; any by step checks -> `check`.
  `/trust` and `calu honesty trust` record `user`.
- **Sufficiency.** Setting `honesty.sufficient_trust`: `check` (default), `review` or `user`:
  the weakest source that counts. A commit or file content whose recorded source is weaker
  than the setting is treated as not trusted: the commit is no baseline, the file is
  unreviewed. Raising the setting therefore makes earlier, weaker trust insufficient without
  deleting it; lowering it makes it count again. With `review`, an edit check alone does not
  verify a file: such files are unreviewed until an honesty review judged them. With `user`,
  only a person's certification counts.
- `calu honesty trusted` shows the source of every entry; the state that clients show names
  the baseline's source.
- Keep the tables from growing without bound: one row per content hash; say in the docstring
  how many rows a long session adds and add a simple way to prune entries of the sources
  `check` and `review` older than a given age (`calu honesty prune --days N`), never `user`
  entries.
- Tests: each source is recorded as specified; a stronger source replaces a weaker one and
  not the reverse; `sufficient_trust = review` makes a check-verified file unreviewed and a
  `check` commit no baseline; a restart (fresh state) keeps a stored verification.
