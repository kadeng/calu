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
| 7 Third-party UIs, packaging | Done except what needs a real editor: `calu acp` with ACP's session modes and config options (D42), the wheel as the shipping format (D41, no single binary) |

All seven phases are **implemented**; what is left is listed under "Next steps" and "Open
questions" and is either the owner's decision, a proposal of mine to confirm, or something this
machine cannot do (a real ACP editor, a PyPI account, model API keys, a logged-in `claude`
inside a container). No phase has unbuilt work in it.

Checks: `scripts/wsl-test.sh -m 'not live'` is ruff format/lint clean, pyright 0 errors,
**443 tests pass** (plus 7 browser tests, which skip without `scripts/get-browser.sh`) (unit + database). The database tests need `docker compose up -d caludb
--wait`. 90 commits, ~23k lines of Python in `src` (~25k with the SQL, templates and default
instructions), ~8.9k of tests, 14 migrations, 20 CLI commands, 16 protocol requests, 17 events. CI (`.github/workflows/checks.yml`) runs the same checks, the database and
browser layers and the installed wheel on every push and pull request; the live layer never runs
there.

### What exists

- **Docs**: `README.md` for users, `docs/CONFIGURATION.md` (generated from `calu.config` by
  `calu.configdoc`; `tests/unit/test_configdoc.py` fails when the file and the model
  disagree, or a field has no docstring), `docs/TESTING.md` for the four layers and the
  manual checklist, `PLAN.md` for the decisions, `AGENTS.md` for the rules.
- **CLI** (`src/calu/cli.py`): `db up|down|migrate`, `setup`, `credential list|set|remove|new`,
  `run`, `attach`, `protocol`, `sessions`, `escalations`, `resolve`, `evals`, `project`,
  `config`, `checker`, `rules`. Passwords via getpass, or one line each from stdin when stdin is
  not a terminal (tests use that).
- **`calu run [DIR]`** (`src/calu/run.py`): password → decrypt the `session` credential → two
  connections (queries, notifications) → wipe the credential → load the settings → create the
  `sessions` row → start Claude Code → serve the protocol and print the address and a client
  token. `-p/--prompt`, `--batch` (ends when Claude is done **and** no client is attached),
  `--resume`, `--model`, `--effort`, `--project`, `--thinking`. Reloads the configuration on
  `config_changed`; continues at once on `escalation_resolved`.
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
  per session, `calu.tokens`); `calu.protocol.client`; `calu attach ADDRESS --token T` with
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
- **ACP adapter** (phase 7, D24, `src/calu/acp/`): `calu acp ADDRESS --token T` speaks the Agent
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

1. **Try `calu acp` with a real editor** (Zed or JetBrains Air): the adapter passes an
   end-to-end test against a real session over a real WebSocket with a scripted Claude, but no
   editor has driven it yet, so expect small mismatches in what a client sends. What the editor
   can drive is complete: prompts, the replay of `session/load`, permission requests for
   escalations, and the session mode -- the three auto-escalation switches are offered both as
   ACP's session modes and as its newer boolean config options, and a change made anywhere
   reaches every client (D42).
2. **Publishing** needs two steps only the owner can take (and Q15 asks whether a single
   executable should ship beside the wheel). The wheel itself is verified
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
   both. What `claude.sandbox.weaker_nested_sandbox` changes is still untested, because it needs
   a logged-in `claude` inside a container.
4. Smaller gaps: a client of a session still cannot change what steers the checkers through the
   protocol, by design (the session role may only read it, §12 phase 4) -- except the
   auto-escalation switches, which are session-local and audited (D42, D43). Editing a single
   list entry is done: `calu config add|remove <key> <entry>` (D44).

## Open questions for the owner (`PLAN.md` §11)

- **Q2** Relationship to sitl: replace it or keep both? The technical half is settled by the ACP
  adapter -- any ACP editor drives a calu session with no extension of its own, and what ACP
  does not carry (checker verdicts as structure, the herd view) lives in the web interface. What
  is left is whether sitl keeps living for VSCode users, which ACP does not reach yet.
- **Q3** A database role per session. Recommendation in PLAN: not yet, and not as a role per
  session -- row-level security keyed on a session id the connection sets is the cheaper shape,
  and it should come after the first real unsupervised runs.
- **Q4** Closed. The transport half is loopback plus an SSH tunnel, documented in the README's
  "From another machine"; the operator half is built (D48): `calu credential new operator`, and
  the read-and-resolve commands prefer that credential over `admin`.
- **Q5** Answered as a proposal (D45): a new project gets "proxy" with a starter allow list
  instead of "full". One flag overrules it (`calu project add --network full`); existing
  projects are untouched. The owner should confirm the list and the trade (a task that needs an
  unlisted domain stops until someone adds it).
- **Q9** A training mode: recommended **against**. Every resolved escalation is already a
  labelled case and `calu evals --from-db` uses them; if more labels are wanted, switching an
  auto-escalation category on for a while produces them for the steps the checkers approved,
  with no new tables.
- Answered this session: Q5 (D45, as a proposal to confirm), Q14 (D46, measured: no).
- **Q15** The single executable of REQUIREMENTS 21 and soft goal 2. D41 recommends **against**
  building one and says why; the owner asked for it, so the recommendation needs their word.
  Nothing in the code depends on the answer: `uv tool install` works today and
  `tests/unit/test_packaging.py` pins what a binary would have to carry.
- Answered this session: Q6 (D27), Q8 (D28), Q10 (no plugin mode), Q11 (D23).

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
