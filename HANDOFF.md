# Handoff

State on 2026-11-09, end of the third implementation session. Read this, then `AGENTS.md`,
`REQUIREMENTS.md` and `PLAN.md` (decisions D1-D30, open questions §11, phases §12).

## Instructions from the owner

- "You may implement, make reversible decisions and commit and push at reasonable checkpoints.
  Stop only when you must ask me something." Everything that is only in git is reversible.
- Work in the Linux copy under `~/projects/stay-in-the-loop/calu`, never in the old Windows copy
  under `/mnt/c`. The `CLAUDE.md` in the parent directory belongs to sitl; for calu, follow
  `AGENTS.md` (which repeats sitl's coding rules, D11).
- Run the checks with `scripts/wsl-test.sh -m 'not live'` after
  `docker compose up -d caludb --wait`.
- Commit and push to `main` after each finished unit of work (`gh` is logged in as kadeng).
- `REQUIREMENTS.md` holds only what the owner stated; derived design goes into `PLAN.md`.
  `AGENTS.md` stays minimal (onboarding and code layout, no invented conventions).
- Plain technical language everywhere; contract docstrings (ruff `D`/`DOC` enforce them, and
  `tests/unit/test_docstrings.py` checks completeness).
- Never copy code from `.reference/claude-a2a` (no licence). Mistral: small models only.

## Why calu sits inside the stay-in-the-loop directory

calu is the successor of stay-in-the-loop (sitl) and was built by porting sitl's code. calu is
cloned as `calu/` inside sitl's working tree so that a session can read sitl's source, tests and
spike results in the parent directory `..`. It is its own git repository
(github.com/kadeng/calu, public, GPL-2.0) and is listed in sitl's `.gitignore`. Commit calu work
in `calu/` only.

## Where things stand

| Phase (`PLAN.md` §12) | State |
|---|---|
| 1 Skeleton | Done |
| 2 Core port | Done |
| 3 Session runner | Done (`calu run`, steps/turns/escalations in the database, operator resolution, sandbox settings, herd commands) |
| 4 Bus and clients | Done (event bus, JSON-RPC over WebSocket for several clients, `calu attach`, comments) |
| 5 Sandboxing | Built: the settings (D15), the per-project network policy (D39) with a proposed default for new projects (D45, Q5 asks the owner to confirm it), `calu evals`. Untested: what `weaker_nested_sandbox` changes, which needs a logged-in `claude` inside a container (the rest of Q1 is measured). |
| 6 Herd management | Done: the commands, the web interface, the triage queue, the badge and the jump palette |
| 7 Third-party UIs, packaging | Done except what needs a real editor: `calu acp` with ACP's session modes and config options (D42), the wheel as the shipping format with the single-file binary buildable beside it (D41, `scripts/build-binary.sh`). Driven as a real process over real pipes for the handshake, a prompt, a denied step and `session/load`'s replay (`tests/unit/test_acp_process.py`) |
| 8 Honesty enforcement and checker families | Done (the owner's brief of 2026-11-10, §13): several checkers on one route run in parallel with most-restrictive-wins (D81), the `honesty_marks` register with its three modes and the deterministic layer that refuses to run dishonest code (D82, D83), `calu honesty list\|clear` and the marks on the web interface (D84), the checker's own instructions shipped and seeded off (D85), and the register carried to every front end (D86). What is **not** done is the judgement: it needs a capable checker model (Q17), and whether honesty, security and over-engineering are one agent or several is Q18 |

All eight phases are **implemented**; what is left is listed under "Next steps" and "Open
questions" and is either the owner's decision, a proposal of mine to confirm, or something this
machine cannot do (a real ACP editor, a PyPI account, model API keys, a logged-in `claude`
inside a container). No phase has unbuilt work in it.

Checks: `scripts/wsl-test.sh -m 'not live'` is ruff format/lint clean, pyright 0 errors,
**661 tests pass** (plus 8 browser tests, which skip without `scripts/get-browser.sh`) (unit + database). The database tests need `docker compose up -d caludb
--wait`. 212 commits, ~24k lines of Python in `src` (~26k with the SQL, templates and default
instructions), ~11.5k of tests, 22 migrations, 21 CLI commands, 16 protocol requests,
18 events. CI (`.github/workflows/checks.yml`) runs the same checks, the database and
browser layers and the installed wheel on every push and pull request; the live layer never runs
there.

### What exists

- **Checker record** (D61): `calu checker stats [--days N]` and the web checkers page report,
  per checker, what it decided and where a human disagreed -- escalations that were allowed
  (caution for nothing) and steps it approved that were then denied (misses). Read from the
  record, no model called. `calu checker misses [NAME]` lists the rows behind those counts,
  with the escalation id, so the instructions can be fixed against a real case (D62).
- **Docs**: `README.md` for users and `CALU_GUIDE.md` as their reference documentation, `docs/CONFIGURATION.md` (generated from `calu.config` by
  `calu.configdoc`; `tests/unit/test_configdoc.py` fails when the file and the model
  disagree, or a field has no docstring), `docs/TESTING.md` for the four layers and the
  manual checklist, `PLAN.md` for the decisions, `AGENTS.md` for the rules.
- **CLI** (`src/calu/cli.py`), 20 commands: `db up|down|migrate`, `setup`,
  `credential list|set|remove|new`, `run`, `attach`, `acp`, `web`, `protocol`, `sessions`,
  `escalations`, `escalation`, `resolve`, `token`, `summary`, `doctor`, `evals`, `project`,
  `config get|set|unset|add|remove`, `checker`, `rules`. Passwords via getpass, or one line
  each from stdin when stdin is not a terminal (tests use that). The read-and-resolve commands
  prefer the `operator` credential and fall back to `admin` (D48); everything that changes the
  configuration needs `admin`. Every `calu ...` line in the documentation is parsed by a test
  (`tests/unit/test_documented_commands.py`), and every command is documented.
- **`calu run [DIR]`** (`src/calu/run.py`): password → decrypt the `session` credential → two
  connections (queries, notifications) → wipe the credential → load the settings → create the
  `sessions` row through `calu_start_session` and take it over with `calu.session_id` (D49) →
  start Claude Code → serve the protocol and print the address and a client token. `-p/--prompt`, `--batch` (ends when Claude is done **and** no client is attached),
  `--resume`, `--model`, `--effort`, `--project`, `--thinking`. Reloads the configuration on
  `config_changed`; continues at once on `escalation_resolved`.
- **A heartbeat** (migration 0016, D56): `sessions.last_seen`, stamped every
  `clock.check_interval_s` seconds by `Session.beat`. `herd.SessionRow.stale` is silence beyond
  180 s for a session that has not ended; `calu sessions` marks it "SILENT for N min" and the
  fleet page groups it as "Gone (no heartbeat)", out of "Needs you". The decision path says
  it too: `calu escalations`, `calu escalation <id>` and the web pages mark an escalation
  nobody is waiting for -- an answer is recorded, the step does not run -- and
  `calu resolve <id> cancel [note]` clears such an entry as a cancellation rather than as
  a decision nothing acted on (D57).
- **Isolation of a session's rows** (migration 0015, D49): the `calu_session` login reads and
  writes only the rows that carry its own session id, with three owner-side functions for what
  a policy cannot do (`calu_start_session`, `calu_recent_sessions`, `calu_conversation_sessions`
  plus `calu_same_conversation` for a resumed or forked conversation). The owner is unaffected;
  `calu_operator` reads everything.
- **Packaging**: the wheel (`uv tool install`), and `scripts/build-binary.sh` for a single-file
  binary that `release.yml` builds and runs on x86_64 and arm64 for every tag (D41, Q15).
- **CI**: `.github/workflows/checks.yml` (lint, types, unit + database, browser, the installed
  wheel) and `release.yml` (wheel, both binaries, and a publish step that only a tag reaches).
- **Session** (`src/calu/session.py`, ported from sitl): tool gate, escalations, narration, chat
  queue, comments, subagent result review, login hint, states of `PLAN.md` §3.1 derived from
  what runs. `apply_settings` applies a reloaded configuration from the next step on.
- **Four decisions** (D21): PASS (no objection; Claude Code's own approval decides), ESCALATE,
  ALLOW (run it whatever the harness says), DENY. PASS is the normal answer; the allow lists and
  the read-only tool routes produce PASS. calu does **not** set the permission mode (D5
  revised): `claude.permission_mode` is None by default, Claude Code's default is auto, and the
  hook follows the mode it is actually given.
- **Checker tools** (D22, D25, `checkers/tools.py`): `read_file(path, start_line, end_line)`,
  `find_files(pattern)`, `grep_files(pattern, path_glob)`, on Pydantic AI's workspace API
  (`LocalWorkspace(read_only=True, authority="none")`), bounded by the read deny lists and by
  size. Pydantic AI ships no file tools itself; a language server would be the next tool here.
- **What a command wrote** (D31, `src/calu/changes.py`, **off by default**): with
  `changes.snapshot` on, the session snapshots the project directory before and after every step
  the command checker judges, using git plumbing in a git directory of its own under
  `$XDG_STATE_HOME/calu/snapshots/<pid>-<time>` (outside the project, on the sandbox's
  `denyRead` list, deleted when the session ends). It records the two tree ids and the paths on
  the `steps` row (migration 0010) and warns when a command wrote paths the checker's summary
  did not mention — the dishonesty signal. Files above `changes.max_file_bytes` and binary ones
  are reported by path and size but kept out of the patch, which is itself capped by
  `changes.max_patch_bytes`; `changes.always_capture` (`.env` by default) catches what a
  `.gitignore` would hide. `SnapshotStore.revert` exists and is tested. Overlapping windows
  (background commands, subagents) are marked `overlapping` and then not compared with the
  summary, because the paths cannot be attributed to one step. With `changes.review` on as well
  (also off by default), a `PostToolUse` matcher for Bash calls `ToolGate.review_write_set`
  while Claude waits: the changeset checker reads the patch, a DENY reverts the change and tells
  Claude what was undone and why — or, when the revert does not apply because the files moved
  on, that it was **not** undone and Claude has to fix it itself. An ESCALATE asks a human, and
  the review records a `checker_runs` row like every other check. Verified against the real CLI
  (`spikes/RESULTS.md`, "PostToolUse for Bash").
- **Times and the clock** (D32, `src/calu/clock.py`): a session keeps the start and end time of
  its recent steps (`Session.step_times`, the last 500) and gives the checkers two more
  read-only tools: `step_times` (when the recent calls ran, local and UTC) and `changed_files`
  (which paths changed since a step, and which step's run contains the change). The attribution
  uses the file's **ctime**, which an unprivileged process cannot set — `touch -d` moves only
  the modification time — and it scans directories too, because a deleted file leaves nothing
  but its parent's timestamp. It gives attribution, not content, and only the *last* change of a
  path. `ClockWatch` samples `CLOCK_REALTIME` against `CLOCK_BOOTTIME` (boottime, so a suspended
  host is not mistaken for tampering) every `clock.check_interval_s`; a backward move beyond
  `clock.max_backward_s` arms an alarm that escalates the session's next tool call with the jump
  as its reason, and the human's answer to that escalation clears it. A large forward move is
  only logged, because a first time-server sync jumps forward. An idle session has no next step,
  so the jump reaches the clients as a log event at once and escalates when Claude acts again.
  No live checker model has used the two tools yet.
- **Changeset checker** (D26, `checkers/changeset.py`): a Bash call that creates a `git commit`
  goes to `Route.CHANGESET`; calu collects the diff (`--cached`, against HEAD for `-a`, against
  `HEAD~1` for `--amend`) and gives it to that checker with the command and its message. A DENY
  stops the commit; the escalation context carries the changeset.
- **Store** (`src/calu/store/`): `ConfigStore` (configuration, checker definitions, rules;
  reloads on `config_changed`), `DatabaseSessionStore` (session, steps, checker runs, turns,
  chat summary, escalations, comments, client tokens, address, agents, history),
  `MemorySessionStore` for unit tests.
- **Unix socket** (§6, D20): `calu run --socket [PATH]` serves the clients on a Unix socket
  (mode 0600, `$XDG_RUNTIME_DIR/calu/<pid>.sock` by default) instead of a loopback port, which
  every process on the machine can reach. The address is recorded as `unix:<path>`, and
  `calu attach`, `calu acp` and the web interface all take it. Two things the first real run
  taught: a path over ~100 bytes is refused with its length (the kernel's limit is 108), and a
  directory that already exists keeps its own mode — chmodding `/tmp` is neither possible nor
  calu's business.
- **Bus and protocol** (phase 4): `calu.bus` numbers every event and keeps the last 5000 for
  replay; `calu.protocol.server` serves JSON-RPC over WebSocket to several clients (one token
  per session, `calu.tokens`); `calu.protocol.client`; `calu attach [ADDRESS --token=T]` (no arguments finds the session of this directory and
  mints its own token, D105) with
  `/start /stop /interrupt /allow /deny /inject /to /comment /why /checkers /state /context
  /sessions /history /shutdown`. `calu protocol [--out FILE]` writes the JSON Schema.
- **The web interface** (phase 6, `src/calu/web/`, the optional `web` extra): `calu web` serves
  the loopback interface only (no login yet, Q4). The fleet page groups sessions as every such
  tool does and leads with the ones that need a human (Needs you / Working / Idle / Ended), with
  a filter. A session page shows its history at four levels of detail (the narrated summary a
  checker reads, compact, detailed, full), its subagents, and ready-to-run connect commands with
  a token minted on the spot. `/sessions/{id}/live` relays the session's own WebSocket to the
  browser (D34): the stream, stop, interrupt, start with model and effort dropdowns, the message
  queue, Send and Steer, a recipient selector for background subagents, and escalation cards.
  `/triage` is the mode to sit in: one escalation at a time across every session, answered from
  the keyboard, with "remember exactly this command" turning the decision into an anchored rule.
  Every page carries an attention badge (filled from `/api/sessions`, also in the tab title) and
  a jump palette on Ctrl+K that goes to any session's live view.
  `/settings` is generated from `calu.config.Config`, so every value — Claude's model and effort,
  `use_bedrock`, each checker's model and thinking effort, the sandbox, the clock, the write sets
  — is editable with the right control, and `/checkers/{name}` edits the instructions with
  Ctrl+S. Git links point into the project's own browser (`calu.web.git`); calu browses no git.
- **Forking a conversation** (D36, verified end to end): `calu run --resume <id>
  --fork-before-turn <n>` keeps everything up to the previous turn and discards turn n, under a
  new session id, so the original conversation stays as it was. calu records the transcript uuids
  of each turn (the prompt, and the last assistant entry) from the events Claude Code replays and
  stores them on `turns` (migration 0012); `herd.fork_point` finds the pair again **across the
  `sessions` rows of one conversation**, because a resumed run opens a new row and records only
  the turns it saw. The session page offers the command per turn. A fork point the CLI refuses is
  reported with `FORK_HINT` rather than the authentication hint. Putting the **files** back as
  well is Q14 and not built.
- **A confirmation the harness asks for** (D38): any permission question the SDK raises arrives
  at `can_use_tool`, which is calu's wait for a human, so a step the checkers passed escalates if
  Claude Code's own approval asks. Its reason says so (`HARNESS_ASKED`). In headless mode the
  classifier never asked in four attempts, and allowed a write outside the project directory —
  the deny lists, not the harness, are what stop that.
- **Questions of the agent** (D35): `AskUserQuestion` is routed to `Route.QUESTION`, escalates
  without a checker, carries its options in the `escalation.request` event, and the answer goes
  back as the message of the refusal that keeps the call from running. The row is recorded as
  `answered` (migration 0011). Answer it from the browser (buttons), `calu attach` (`/answer`)
  or `calu resolve <id> answer "..."`.
- **ACP adapter** (phase 7, D24, `src/calu/acp/`): `calu acp [ADDRESS --token=T]` speaks the Agent
  Client Protocol on stdin and stdout (newline-delimited JSON, protocol version 1) and attaches
  to a running session as an ordinary client, so the session outlives the editor and several
  editors can attach at once. Implemented: `initialize`, `session/new` (checks the editor's `cwd`
  against the session's directory), `session/load` with the replay the protocol requires,
  `session/prompt` (waits for `turn.result`), `session/cancel`, `session/close`, the
  `session/update` stream and `session/request_permission` for escalations, whose answer becomes
  an `escalation.decide`. "Always" is answered like "once" and recorded in the note, because a
  rule row needs the admin credential. The editor's `fs/*` and `terminal/*` capabilities stay
  unused and its `mcpServers` are not forwarded, or tool calls would route around the checkers
  and the sandbox. Two details worth keeping in mind when changing it: a `tool_call_update`
  **replaces** the content collection, so every update carries everything the call has shown
  (`_add_content`), and the echo of the adapter's own prompt is dropped so the editor does not
  show the user's message twice.
- **Inspecting a decision**: `escalation.context` returns what the checker saw and said (rendered
  step, the file before the change, the conversation and chat summary of that moment, the checker
  runs with model and version); `checkers.get` returns the definitions, read-only;
  `session.history` returns the turns, steps and chat summary from the database.
- **Deciding without the session** (D30, the invariant): an escalation can always be decided
  from the database alone — `calu escalations` lists them, `calu escalation <id>` prints
  everything a decision needs (reason, the command line or the agent's question with its
  options, the step as the checker rendered it, the file before an edit, the changeset, the
  narrated conversation, the folded summary), and `calu resolve <id> allow|deny|answer` decides.
  The waiting session notices through the `escalation_resolved` notification **and** a poll of
  the row every second. The database deliberately holds no live stream (D3): that is what
  attaching to the session is for.
- **Deciding an escalation** (D28): an operator with a database login uses
  `calu_resolve_escalation` (`calu resolve`); a client of the session has no login and tells the
  session, which records a real resolution with `calu_client_decision` — scoped to that session,
  requiring the hash of one of its client tokens (which the role cannot read back), recorded as
  `resolved_by = 'client:<name>'`.
- **Escalation timeout** (D27): `escalation_timeout_s` (default 8 h, 0 = no limit). On the
  timeout the step is denied, the row is cancelled with the reason, and the session stops;
  Claude's session id is in the database from the start of the session, so
  `calu run --resume <id>` continues it. `calu sessions` prints that id.
- **Process identity and agent tree** (D29): `sessions` carries host, OS user, directory, pid
  and `address`; the `agents` table holds one row per subagent with `parent_agent_id` (NULL =
  the main agent).
- **The folded conversation** (D40): `calu summary <session> [--edit]` and
  `/sessions/{id}/summary` show and correct the chat summary the checkers read for everything
  older than the unfolded turns. An empty text is refused, and the session re-reads the summary
  on the `config_changed` notification (`Session.reload_summary`), so a correction reaches the
  next check.
- **Configuration commands** (`src/calu/admin.py`): `calu project add|list`, `calu config
  get|set|unset KEY` (dotted keys, values as JSON, validated against `Config` before the write),
  `calu checker list|show|set|edit`, `calu rules list|add|edit|remove|enable|disable`. `edit`
  opens the text in `$EDITOR` (`$VISUAL` first, then nano/vim/vi), stores it on close, says
  "unchanged" when nothing changed, and keeps the temporary file and prints its path if storing
  fails. A rule list is edited in the line format `calu setup` loads the built-in lists from;
  disabled rules are untouched. All of these need the `admin` credential.
- **Evaluation** (`src/calu/evals/`, ported): `calu evals [FILE ...]` runs the checkers of the
  database on the built-in cases, on YAML files and, with `--from-db`, on the escalations a
  checker raised and a human resolved. Reports accuracy, preferred decision, critical misses,
  over-escalations, escalation share, unwanted comments; exits 1 on a critical miss.
- **Checker keys** (D17, D37): read from `~/.config/calu/.env` and calu's environment, and
  **every** provider key calu knows is blanked for Claude Code (not only the ones that are set),
  so the checkers can run on an Anthropic API key (an Ultra subscription includes API credits)
  while the main chat keeps the subscription. Measured with the real CLI, and
  `tests/unit/test_credentials.py` holds the two tests for it. The one link not verified here is
  a checker actually billing the Anthropic API, which needs the owner's key.
  Bedrock needs no key here, and `claude.use_bedrock` is the separate switch for Claude itself.
- **Network policy per project** (D39): `projects.network_policy` is no longer inert. "none"
  reaches nothing, "proxy" only `claude.sandbox.allowed_domains`, "full" everything through the
  sandbox's proxy (`allowedDomains: ["*"]`), and `denied_domains` overrides the wildcard.
  `calu project add --network` and `calu project network --name <p> --network <policy>`; a
  running session applies a change when it next starts. Measured against the real CLI: the
  sandbox exports a filtering proxy as `HTTPS_PROXY` and raw TCP is blocked whatever the policy
  says. The start-up warnings say when a session may reach every domain. Open (Q5): the default
  for new projects, still "full".
- **`calu doctor`**: one command that says what is in place before anything runs — `claude` and
  its version, bwrap and socat, git, the two credentials, which provider keys the checkers
  could use, the socket and snapshot directories, optionally a workspace, and with `--db` that
  the database answers and whether its schema is current. It exits 1 when something a session
  cannot run without is missing, and names it.
- **Sandbox** (D15): `SandboxConfig` passed to Claude Code as a `--settings` document;
  `calu.preflight` warns about missing `bwrap`/`socat`, a disabled sandbox and missing checker
  API keys at every start.
- **Client tokens** (D30, migration 0009): a session accepts every non-revoked token of its own
  session, not only the one it printed. `calu token mint SESSION [--label]` prints a token once,
  `calu token list [SESSION]` shows labels, address and last use but never a token, and
  `calu token revoke ID` ends one client's access. The session presents a hash and
  `calu_client_token_ok` confirms it; the role cannot read the hashes and no longer has UPDATE on
  the table, so it cannot un-revoke one. The printed token is still checked first and without the
  database. Verified in the tests and by hand against a real PostgreSQL.
- **Migrations**: 0001 initial · 0002 turn records · 0003 comments · 0004 client tokens ·
  0005 the PASS decision · 0006 the changeset route · 0007 `calu_client_decision` ·
  0008 `sessions.address` and the `agents` table · 0009 minted client tokens · 0010 write sets · 0011 the `answered` state · 0012 fork points.

### Verified by hand with the real CLI

`spikes/smoke_session.py <dir> [--resolve allow|deny]` runs one real session against a
throwaway database and prints what it wrote. The sandbox is off by default (this machine has no
`socat`), so the commands Claude runs are not isolated: use a throwaway directory. Results:

1. An allow-listed `ls`: no model call, `rules_outcome = allow`, turn and answer stored.
2. A `Write`: edit checker, one `checker_runs` row; both steps recorded as `pass`, so the hook's
   "no decision" answer reaches auto mode (D21).
3. `sudo -n ls /root`: the deny rule matched, the session went to `escalated` and waited;
   `calu_resolve_escalation` let it continue within a second and Claude reported the denial with
   the note. That is REQUIREMENTS 12 end to end.
4. A real commit: routed to the changeset checker, PASS, `checker_runs` row recorded. A commit
   whose message carried the fake checker's deny marker was **stopped**, Claude reported the
   reason and did not retry, and the change stayed staged.
5. `spikes/spike_hook_timeout.py`: a PreToolUse hook that misses its timeout does **not** let the
   call run (D23). Re-run it after a CLI update; the whole gate rests on it.
6. `--sandbox` (with socat from `spikes/get-socat.sh`): a sandboxed session really is isolated —
   the probe inside it saw its own PID namespace, no `claude` process, unreadable credentials,
   unwritable Claude Code settings, a blocked network, and its detached process did not survive
   (D15, `spikes/RESULTS.md`).
7. `spikes/spike_question_tool.py`: `AskUserQuestion` reaches the hook with its options, and
   Claude acts on the answer returned as the message of a refusal (D35).
8. `spikes/spike_fork_session.py`: a fork at an earlier turn works — the fork remembered the kept
   turn, not the dropped one, and ran under a new session id (D36). It also showed that a
   user-role transcript entry is not always a prompt, which is what the `resume_drops_turn` guard
   refuses.
9. Checker keys and the subscription: with `ANTHROPIC_API_KEY=""` Claude Code answers normally,
   so blanking the checker keys (what calu does) keeps the subscription while the checkers use
   the Anthropic API (D37).
10. `--changes`: a command wrote a file whose content carried the deny marker (the command line
   did not, so the pre-call check passed). The `PostToolUse` matcher for Bash fired, the
   changeset checker refused the patch, calu reverted the file (the directory was clean
   afterwards), Claude reported the refusal and did not retry, and the database held the write
   set and a `checker_runs` row for the review.

11. The **installed wheel**: `uv build` ships every piece of package data (13 migrations, 16
   default instruction files, 13 templates, 5 static files), and in a throwaway virtual
   environment `calu --version`, `calu protocol` (51 definitions) and `calu doctor` run out of
   `site-packages` with that data resolving correctly (D41). `calu doctor` exits non-zero there,
   which is right: that environment has no session credential.

12. Re-run after the changes of 2026-11-10 (D42-D47): `smoke_session.py` with a prompt that
   runs `ls` and writes a file. The allow-listed `ls` ran with no model call
   (`rules_outcome = allow`), the `Write` went to the edit checker and passed with a
   `checker_runs` row, the turn and the answer were stored, and the session went
   idle -> stopped cleanly. So the gate, the hook, the rules, the edit route and the turn
   storage all still work with the real CLI after the session, store and schema changes.

13. **Row-level security** (D49, migration 0015): after switching the policies on, a real
   session ran end to end again -- it created its row through `calu_start_session`, recorded
   the step, raised the deny-rule escalation, took the denial an operator wrote in the database
   and recorded the turn. A database test additionally holds the line: two sessions on two
   connections each see only their own steps, an UPDATE of the other's changes nothing, an
   INSERT for the other is refused, a connection that never set the id sees and writes nothing,
   and the owner still sees everything.

14. **The network default** (D45) and **a harness confirmation** (D38) together, with the
   sandbox on: with `--allow pypi.org`, the curl to pypi.org ran unasked, and the curl to
   `example.net` made Claude Code ask for a confirmation (`SandboxNetworkAccess`) although the
   command checker had passed it -- calu escalated it, the operator allowed it and it ran. So an
   unlisted domain asks rather than failing (`spikes/RESULTS.md`).
15. **What the checkers cost** (D63): with seeded runs in a throwaway database, `calu checker
   stats` printed `50k in / 240 out, 95% of the input from cache` for one checker and, for a
   checker whose runs read nothing from the cache, the same totals with `0% of the input from
   cache -- the prefix is not being reused; check the model's provider caches`. Both branches
   therefore render from the record with no model call.
16. **The step paths** (D64), seeded the same way: `calu checker stats` reported `14 step(s): 4
   went to a checker, 10 (71%) did not -- 7 allow-listed, 1 denied by a rule, 2 of a tool the
   lists do not cover`, then `calu's own delay per step: ~3 ms, worst 3 ms (no human waiting
   time in this)` and `1 escalation(s) answered, after ~45 s of waiting for a person`. The
   escalated step is counted as checked but left out of the delay, which is the point of
   measuring the two separately.
17. **A resumed conversation end to end** (D65, D67): four runs of one conversation against the
   test database with a fake Claude and a recording checker (`tests/db/test_run.py`), the fold
   happening in the second run and nothing folding afterwards. Without the fix the fourth run's
   checker prompt said "No turns have been folded into this summary yet" -- the summary and the
   two turns before it were gone; with it the prompt carries the real summary and both turns.
   The same runs showed the heartbeat failing at every session start, which D67 fixes.
18. **A real session after the store changes of D65 to D68**: `smoke_session.py --resolve deny`
   with a real `claude` (haiku) -- the deny rule escalated the step, the operator denied it, the
   session continued and Claude reported the denial, and the step, the escalation and the turn
   are in the database. The start now prints only the two warnings that are true on this
   machine (no sandbox, no summarizer key); the heartbeat warning that used to appear at every
   start is gone.
19. **The two new eval cases against the real checker model** (D69), Nemotron Lightning on
   Fireworks, two runs each: 100% -- it escalated the message that asks another session to run
   a command the gate had denied, quoting the request as the instructions say, and passed the
   coordinating message. The same run showed D66's cost line working on a real provider
   (`14890 input tokens (0% of them from the provider's cache), 2789 output tokens, ~1817 ms
   per case`) and confirmed D63's caveat: this model reuses a cache only for an identical
   prompt, so 0% is the provider and not a defect.
20. **The whole built-in case set against a real checker model** (D71-D74), which had never been
   run before for want of a key: 25 cases, three runs each, both checkers. The shipped defaults
   started at 95% with one error (command) and 67% with a critical miss (edit, which passed
   `API_KEY = "sk-live-..."` into a source file as "a placeholder"). After fixing two broken
   cases and four instruction gaps, and raising the checkers' budgets (migration 0017), two
   independent triple runs give **both checkers 100% accuracy, no critical misses, no errors,
   no unwanted comments**. Re-run after any change to the default instructions:
   `scratchpad`-style script in the commit message of D73, or `calu evals --repeat 3`.
   One run is not a measurement -- two runs of identical instructions differed by two critical
   misses, which is why `--repeat` exists. Since D99 the report names the unstable cases:
   on 2026-11-10 the command checker's twenty cases times three gave four of them, among
   them `obfuscated-command: DENY / PASS / ESCALATE` -- three runs, three answers, one set
   of instructions. That is the evidence for Q17 that no accuracy figure carries.
21. **The changeset checker, measured for the first time** (D75, D76): it could not be evaluated
   at all before, so the one checker that judges a whole change was the one nothing measured.
   With four new cases and its own request budget (8 instead of 5, because it reads the files
   around the diff) it decides correctly in 11 of 12 runs; the miss is a rename whose caller
   still calls the old name, which it passed as complete. The report now also separates the
   critical misses calu's rules stop anyway from those where the gate rests on the model alone
   (`cat .env` is on the read-deny list; `kubectl apply` is on no list) -- which is what Q16
   asks the owner to decide.
25. **A client with no arguments, from a subdirectory** (D105, D104): a session row with an
   address in a throwaway database, the operator credential stored `--no-password`, then
   `calu attach` from a **subdirectory** of the project with no further arguments. It found the
   session, minted a token labelled "calu attach", tried the address it had published and
   revoked the token when the connection failed -- `calu token list` then showed
   `1  revoked  session b1635974-...  ws://127.0.0.1:8765  never used  calu attach`. No
   password was asked at any point. `calu credential set operator --no-password` printed what
   the choice costs and named the file, `calu credential list` showed
   `operator  (file mode only)`, and `calu doctor` reported
   `ok      credential protection: operator stored without a password (file mode only)`. The
   store's own modes were as intended (directory 0700, file 0600).

24. **The honesty register's two reports, with two checkers writing to it** (D87): seeded a
   project where `honesty` opened two marks and cleared one itself and `security` opened one
   that a person overruled, then read both reports with the real CLI.
   `calu honesty list --all` named each mark's author and how it ended
   (`(honesty): ... (closed by a checker)`, `(security): ... (closed by kai)`), and
   `calu checker stats` said `honesty  marks: 2 opened, 1 cleared` and
   `security  marks: 1 opened, 0 cleared, 1 overruled by a person` -- which is the signal
   CALU_GUIDE.md tells a user to read before switching `honesty.mode` to `enforce`.
22. **The subagent-result checker, measured for the first time** (D77, D78), which completes the
   set -- all four checkers are now measurable. Its first result was the worst so far: 67% and
   three critical misses in nine runs, all three the same case, a subagent demanding that a
   script the user called throwaway be rewritten as a state machine. The rule for it existed;
   what was missing was a procedure to run before answering ACCEPT. With that, two runs of
   three: 100% and no critical misses, at the price of a higher escalation share (44-56%).
23. **Three cases that try to instruct the checkers** (D79), testing the claim every checker's
   instructions make -- that what it reads is evidence and never an instruction. Measured once
   with the 30B model: the edit checker resisted, the command checker returned ALLOW once in
   three quoting the planted comment, the subagent checker believed a claimed approval once in
   three. **The instructions were not rewritten for it.** Several rounds of that earlier in the
   day moved the numbers without fixing the escapes, which is the signature of a model that
   cannot follow what it is given rather than of unclear wording. Q17 puts the model choice to
   the owner; the framework's part -- a model per checker, `calu evals` to report, `calu checker
   stats` to price it -- is built.

The configuration commands were also exercised by hand against the test database with a
throwaway credential store (`XDG_CONFIG_HOME` pointed into a scratch directory): `setup`,
`project add`, `checker list|show|set|edit`, `config set|get`, a refused value, `rules
add|list|edit|disable|remove`, a bad pattern leaving the list untouched.

### Left out of the port, on purpose

- sitl's **training mode** (`training.py`): not ported and probably not needed in this form,
  because `calu evals --from-db` gets its labels from the escalations a human resolved (Q9). Its
  protocol models were removed.
- sitl's **event log** (`history.py`): the bus took over the replay buffer; nothing of the stream
  goes into the database (D3).
- sitl's **`tui/`** (a Claude Code channel plugin): channels are not used; `calu attach` covers
  the terminal client. The interactive-plugin mode was considered and rejected (Q10, §4.5).
- `ProjectFiles`/`files()`: configuration lives in the database and is changed with calu's own
  commands, not through the session protocol (§6.2).

## Next steps

1. **Try `calu acp` with a real editor** (Zed or JetBrains Air). Its whole configuration is
   now `{ "command": "calu", "args": ["acp"] }` -- the adapter finds the session running in the
   editor's project directory and mints its own token (D105), which needs the operator
   credential stored with `calu credential set operator --no-password` (D104), because an editor
   gives the adapter no terminal to answer a password prompt with. The adapter passes an
   end-to-end test against a real session over a real WebSocket with a scripted Claude, and is
   driven as a real process over real pipes for the handshake, a prompt, a **denied** step and
   `session/load`'s replay -- but no editor has driven it yet, so expect small mismatches in
   what a client sends. What the editor
   can drive is complete: prompts, the replay of `session/load`, permission requests for
   escalations, and the session mode -- the three auto-escalation switches are offered both as
   ACP's session modes and as its newer boolean config options, and a change made anywhere
   reaches every client (D42).
2. **Publishing** needs two steps only the owner can take (and Q15 asks whether the
   single-file binary, which `release.yml` already builds and runs, should ship beside the
   wheel). The wheel itself is verified
   (D41, `tests/unit/test_packaging.py`, and CI installs it on every push), and
   `.github/workflows/release.yml` builds and publishes it for a `v*` tag — but it stays inert
   until (a) the PyPI project exists with a trusted publisher for owner `kadeng`, repository
   `calu`, workflow `release.yml`, environment `pypi`, and (b) that GitHub environment is
   created. Until then `uv tool install calu` in the README does not work. A tag pushed before
   the setup builds the wheel and stops, so nothing is published by accident.
3. **Phase 5**: nothing blocking left. The network policy is built (D39) and its default for a
   new project is now a proposal to confirm (D45, Q5), and the container case is measured (Q1): a default
   Docker container cannot create a user namespace, so the sandbox needs
   `security_opt: [seccomp:unconfined]` and socat — calu's own `dev` service and image now have
   both. What `claude.sandbox.weaker_nested_sandbox` changes is still untested: it needs a
   real tool call in a container, and the cheap route is a measured dead end --
   `claude sandbox status` needs no login but reports the same posture with and without the
   flag, and does not probe whether the kernel allows the namespaces (`spikes/RESULTS.md`,
   "What `claude sandbox status` can and cannot answer"). What came out of it: `calu doctor`
   now reports a managed policy that locks the sandbox posture, which would override calu's
   own settings.
4. Smaller gaps: a client of a session still cannot change what steers the checkers through the
   protocol, by design (the session role may only read it, §12 phase 4) -- except the
   auto-escalation switches, which are session-local and audited (D42, D43). Editing a single
   list entry is done: `calu config add|remove <key> <entry>` (D44).

5. **The ergonomics of auth** were the owner's complaint of 2026-11-10 ("if I need to worry
   about auth and token passing for every single command, it's not easy") and are answered in
   two halves: `calu credential set operator --no-password` takes the password prompt off the
   read-and-resolve commands (D104), and `calu attach` / `calu acp` with no arguments find the
   session of this directory and mint their own token (D105). What is left of the question --
   whether the terminal or the browser is meant to be the interactive tool -- no longer blocks
   anything, because both now work without a prompt.

6. **Honesty enforcement is built and switched off** (§13, D81-D86). What is left needs a
   person rather than more code: a capable checker model for it (Q17), the choice between one
   combined checker and several (Q18), and what a mark should mean after a branch switch (Q19).
   To try it on a real project: `calu checker set honesty --enable` and
   `calu config set honesty.mode track`, then read `calu honesty list` for a few days before
   switching the mode to `enforce`.

## What the owner still has to decide (`PLAN.md` §11)

Everything else is either built or recommended with its reasons. These need a person:

- **Q5 — the network default of a new project** (D45, implemented as a proposal): "proxy" with a
  starter allow list (package registries, git over HTTPS, OS packages) instead of "full". One
  flag overrules it (`calu project add --network full`) and existing projects are untouched.
  The trade to confirm, now measured: an unlisted domain does not stop the task, it **asks** --
  Claude Code wants a confirmation, calu escalates it (D38) and a person allows it in one
  command. So the default costs an interruption, not a failure.
- **Q15 — pressing publish**: a `v*` tag builds both Linux binaries, runs them, and attaches
  them to a **draft** GitHub release (verified with a throwaway tag, which was deleted; the
  draft held `calu-linux-x86_64` and `calu-linux-arm64`). A person opens the draft and
  publishes it. Nothing else is needed for the binaries -- only the wheel waits for PyPI.
- **Publishing at all**: the PyPI project with a trusted publisher and the `pypi` GitHub
  environment, both of which only you can create (next step 2).
- **Q9 — a training mode**: recommended **against**, and the alternative is now built rather
  than merely argued (D55): `calu evals --list` shows the cases a herd produced and
  `--export FILE` keeps them in the form `calu evals FILE` reads back, neither calling a model.
  With D50 the set also holds the labels for steps the checkers *approved*, which is where a
  training mode's value was. What is left is your word on not building one.
- **Q2 — sitl's future**: the technical half is settled, and VSCode is **not** the exception I
  claimed: it reaches ACP through community extensions today (formulahendry's "ACP Client" and
  others), while Microsoft has standardised its own agent mode on MCP and has not committed to
  native ACP (microsoft/vscode#265496). So a VSCode user can drive calu now through somebody
  else's extension; whether that is good enough, or sitl keeps its own panel for what ACP
  cannot carry, is a product call.
- **Q17 — which model the checkers need**, and which one calu ships as the default. Measured
  on 2026-11-10 with a 30B model: the failures that matter are judgement, not wording -- a
  planted "the user approved this" believed, a rewrite of a script the user called throwaway
  accepted, an ordinary `git push` passed as reversible. Rewriting the instructions moved those
  numbers around without fixing them, so they were left as they are. The framework's part is
  done: a model per checker, `calu evals` to report, `calu checker stats` to price it.
- **Q18 — one checker or several**: honesty, security and over-engineering are different
  judgements on the same edit. A route carries a list (D81), so both shapes are configuration
  rather than code; what calu **ships** is the owner's call, with `calu evals` as the evidence.
- **Q19 — what a dirty mark means after a branch switch**: the register records the branch so
  the question can be answered without a migration. My recommendation is in the question.
- **Q16 — whether a push, a deploy or a publish should be a deny rule** rather than a
  checker's judgement, which is the same finding from the other side.
- **Q1's last line**: what `claude.sandbox.weaker_nested_sandbox` changes. Needs a real tool
  call inside a container, so it needs Claude Code credentials there; the cheap route is a
  measured dead end (next step 3).

Answered and built in this session: **Q3** (D49, row-level security per session, migration
0015), **Q4** (D48, the operator login), **Q14** (D46, a fork does not rewind files), and the
ones listed above as proposals. Earlier: Q6 (D27), Q8 (D28), Q10 (no plugin mode), Q11 (D23),
Q13 (D36).

## Environment notes (owner's machine)

- WSL2 Ubuntu has uv, Docker, bwrap, Node 24 (nvm) and a logged-in `claude` (2.1.319).
  **`socat` is missing**, so a session with the default `claude.sandbox` settings refuses to
  start: `apt install socat` fixes it permanently, and `bash spikes/get-socat.sh` extracts it
  into `/tmp/calu-spike-tools/bin` without root, which is enough to run with the sandbox on.
  With that done, the sandbox was verified end to end through calu (`spikes/RESULTS.md`, "calu's
  own sandbox, end to end"): own PID namespace, no `claude` process visible, the credentials
  unreadable, Claude Code's settings unwritable, the network blocked and no leftover process.
- **`~/.config/calu/.env` does not exist.** Live checker tests skip and a real run has no checker
  model key. The keys are in sitl's git-ignored `.env` (CRLF). Creating the file was refused in an
  earlier session by a sandbox rule, so the owner should do it:
  `mkdir -p ~/.config/calu && tr -d '\r' < ~/projects/stay-in-the-loop/.env > ~/.config/calu/.env && chmod 600 ~/.config/calu/.env`.
  Live checker model: Nemotron Lightning on Fireworks (`CALU_TEST_CHECKER_MODEL` overrides it).
- `scripts/wsl-test.sh` uses `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`.
- Test database: `docker compose up -d caludb --wait` (127.0.0.1:55432, `calu_owner` /
  `calu-dev`, development only). Tests create and drop a database per test.
- The working tree is on the Linux file system; `.gitattributes` forces LF line endings.

## Left open in sitl (not calu work)

- `tests/unit/test_tui.py::test_channel_drops_messages_without_token` is skipped with a TODO.
- The owner's uncommitted `.idea/stay-in-the-loop.iml` change and untracked `.vscode/` are
  theirs; never commit them.
