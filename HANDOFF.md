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
| 5 Sandboxing | Settings done (D15); open: network policy per project (Q5), nested sandbox in Docker (Q1). `calu evals` done. |
| 6 Herd management | Commands done; **web UI not started** |
| 7 Third-party UIs, packaging | `calu acp` done (not yet driven by a real editor); open: single binary, ACP session modes |

Checks: `scripts/wsl-test.sh -m 'not live'` is ruff format/lint clean, pyright 0 errors,
**294 tests pass** (unit + database). The database tests need `docker compose up -d caludb
--wait`. 46 commits, ~16k lines of source, ~5.5k of tests, 8 migrations, 14 CLI commands,
16 protocol requests.

### What exists

- **CLI** (`src/calu/cli.py`): `db up|down|migrate`, `setup`, `credential list|set|remove`,
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
- **Changeset checker** (D26, `checkers/changeset.py`): a Bash call that creates a `git commit`
  goes to `Route.CHANGESET`; calu collects the diff (`--cached`, against HEAD for `-a`, against
  `HEAD~1` for `--amend`) and gives it to that checker with the command and its message. A DENY
  stops the commit; the escalation context carries the changeset.
- **Store** (`src/calu/store/`): `ConfigStore` (configuration, checker definitions, rules;
  reloads on `config_changed`), `DatabaseSessionStore` (session, steps, checker runs, turns,
  chat summary, escalations, comments, client tokens, address, agents, history),
  `MemorySessionStore` for unit tests.
- **Bus and protocol** (phase 4): `calu.bus` numbers every event and keeps the last 5000 for
  replay; `calu.protocol.server` serves JSON-RPC over WebSocket to several clients (one token
  per session, `calu.tokens`); `calu.protocol.client`; `calu attach ADDRESS --token T` with
  `/start /stop /interrupt /allow /deny /inject /to /comment /why /checkers /state /context
  /sessions /history /shutdown`. `calu protocol [--out FILE]` writes the JSON Schema.
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
  0008 `sessions.address` and the `agents` table · 0009 minted client tokens.

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
   editor has driven it yet, so expect small mismatches in what a client sends. Open inside the
   phase: ACP's session modes (`availableModes`, `session/set_mode`), which would carry
   `AutoEscalate`, and the single-binary packaging.
2. **Times, ctime attribution and the clock watch** (D32, decided 2026-11-09, nothing implemented
   yet, and cheap): `steps` already records created, decided and finished times, so what is missing
   is the host's timezone, two read-only checker tools — `step_times` and `changed_files` (ctime in
   a window, directories included, because a delete leaves no inode) — and a task that samples
   `CLOCK_REALTIME` against `CLOCK_BOOTTIME` and escalates when the wall clock moves backwards by
   more than `clock.max_backward_s`. It gives attribution without content.
3. **The write set of a Bash step** (D31, decided 2026-11-09, nothing implemented yet, **optional
   and off by default**): snapshot the project directory before and after every Bash step with git
   plumbing in a git directory of calu's own (`$XDG_STATE_HOME/calu/snapshots/<session>`, outside
   the project, on the sandbox's `denyRead` list, used with `--work-tree`). It closes the gap that
   `Edit`/`Write` reach the hook with their content while a command that edits files is only
   *described* by the command checker. First use: compare the declared paths with the written ones
   (no model call, and a mismatch is a dishonesty signal); then the real diff to a checker after
   the call; then `git apply -R` as the revert on a post-hoc DENY. Needs only the `git` binary.
   Include the size and type policy from the start. The overlay alternative was measured and
   dropped (see D31).
4. **Phase 6**: the web UI on the herd commands plus the configuration writes (§6.2 says which
   side each feature talks to).
5. **Phase 5**: the network policy per project (Q5), the nested-sandbox case in Docker (Q1).
6. Smaller gaps: `calu config set` cannot set a value inside a list; the chat summary can only be
   read, not edited, through a client; the protocol server binds TCP only (a Unix socket is
   mentioned in §6 and `sessions.address` already allows for one).

## Open questions for the owner (`PLAN.md` §11)

- **Q2** Relationship to sitl: replace it or keep both?
- **Q3** A database role per session (would allow row-level security, and narrow D28 further).
- **Q4** How clients authenticate remotely (tokens, operator accounts, TLS) — see next step 2.
- **Q5** The sandbox's network policy and its default.
- **Q9** Where a training mode would belong, if one is wanted at all.
- Answered this session: Q6 (D27), Q8 (D28), Q10 (no plugin mode), Q11 (D23).

## Environment notes (owner's machine)

- WSL2 Ubuntu has uv, Docker, bwrap, Node 24 (nvm) and a logged-in `claude` (2.1.319).
  **`socat` is still missing**, so a session with the default `claude.sandbox` settings refuses
  to start (`apt install socat`).
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
