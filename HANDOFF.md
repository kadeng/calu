# Handoff

State on 2026-11-09, end of the second implementation session. Read this, then `AGENTS.md`,
`REQUIREMENTS.md` and `PLAN.md` (decisions D1-D22, open questions §11, phases §12).

## Instructions from the owner

- 2026-11-09: "You may implement, make reversible decisions and commit and push at reasonable
  checkpoints. Stop only when you must ask me something." Everything that is only in git counts
  as reversible.
- Work in the Linux copy under `~/projects/stay-in-the-loop/calu`, never in the old Windows copy
  under `/mnt/c`. The `CLAUDE.md` in the parent directory belongs to sitl; for calu, follow
  `AGENTS.md` (which repeats sitl's coding rules, D11).
- Run the checks with `scripts/wsl-test.sh -m 'not live'` after
  `docker compose up -d caludb --wait`.

## Why calu sits inside the stay-in-the-loop directory

calu is the successor of stay-in-the-loop (sitl) and is built by porting sitl's code. On the
owner's machine, calu is cloned as `calu/` inside sitl's working tree, so that a session can
read sitl's source, tests and spike results in the parent directory `..`. calu is still its own
git repository (github.com/kadeng/calu, public, GPL-2.0) and is listed in sitl's `.gitignore`.
Commit calu work in `calu/` only; sitl is a separate, private repository.

## Where things stand

| Phase (`PLAN.md` §12) | State |
|---|---|
| 1 Skeleton | Done |
| Spikes Q1, Q7 | Done, results in `spikes/RESULTS.md`, decisions D15, D16 |
| 2 Core port | Done, including the database store (`calu.store`) |
| 3 Session runner | Done: `calu run`, steps/turns/escalations in the database, operator resolution, sandbox settings, `calu sessions/escalations/resolve` |
| 4 Bus and terminal client | Done: event bus with sequence numbers, JSON-RPC over WebSocket for several clients, `calu attach`, comments (migration 0003) |
| 5 Sandboxing | Settings done (D15); network policy per project, the nested-sandbox case and `calu project add` are open. `calu evals` is done. |
| 6 Herd management | The commands are done (phase 3); the web UI is not started |
| 7 IDE client, single binary | Not started |

Checks: `scripts/wsl-test.sh -m 'not live'` gives ruff format/lint clean, pyright 0 errors,
253 tests passed (unit + database). The database tests need `docker compose up -d caludb --wait`.

### What exists

- **CLI** (`src/calu/cli.py`): `db up|down|migrate`, `setup`, `credential list|set|remove`,
  `run`, `attach`, `sessions`, `escalations`, `resolve`, `evals`. Passwords via getpass, or one
  line each from stdin when stdin is not a terminal (used by tests).
- **`calu run [DIR]`** (`src/calu/run.py`): asks for the session password, decrypts the
  `session` credential, opens two connections (queries, notifications), wipes the credential,
  loads the settings, creates the `sessions` row and starts Claude Code. Events are printed to
  the terminal (the stand-in client until phase 4). `-p/--prompt` sends the first message,
  `--batch` ends the run when Claude is idle again, otherwise further messages are read from
  stdin (`/quit` ends it). `--resume`, `--model`, `--effort`, `--project`, `--thinking`.
- **Session** (`src/calu/session.py`, ported from sitl's `session.py`): tool gate (rules, tool
  policy, checkers, auto-escalation), escalations, narration, chat queue, comments, subagent
  result review, login hint, states of `PLAN.md` §3.1 derived from what runs (escalated >
  checking > tool_call > running > idle). `apply_settings` applies a reloaded configuration from
  the next step on. `snapshot` is what a client gets from `state.get`.
- **Four decisions** (D21): PASS (no objection; Claude Code's auto mode decides as before),
  ESCALATE, ALLOW (run it whatever auto mode says) and DENY. PASS is the normal answer; the
  allow lists and the read-only tool routes produce PASS as well. The PreToolUse hook answers
  PASS without a decision while auto mode is in effect.
- **Checkers may read files** (D22): the checker agent has one tool, `read_file`, bounded by the
  workspace and by the read deny lists (`checkers/read_tool.py`).
- **Bus and protocol** (phase 4): `calu.bus` numbers every event of the process and keeps the
  last 5000 for replay; `calu.protocol.server` serves JSON-RPC over WebSocket to several clients
  (one token per session, `calu.tokens`, migration 0004), `calu.protocol.client` is the client
  side, and `calu attach ADDRESS --token T` is a terminal client. `calu run` prints the address
  and the token; with `--batch` it ends when Claude is done **and** no client is attached.
- **Configuration commands** (`src/calu/admin.py`): `calu project add|list`, `calu config
  get|set|unset KEY` (dotted keys, values parsed as JSON, validated against `Config` before
  the write), `calu checker list|show|set` (instructions from a file or stdin, model, limits,
  enable/disable, a new checker with `--route`) and `calu rules list|add|remove|enable|disable`
  (patterns compiled before they are stored). They need the `admin` credential; every change
  announces itself on `config_changed`, so a running session applies it at its next step.
- **Evaluation** (`src/calu/evals/`, ported from sitl): `calu evals [FILE ...]` runs the
  checkers of the database on the built-in cases, on YAML files and, with `--from-db`, on the
  escalations a checker raised and a human resolved. It reports accuracy, preferred decision,
  critical misses, over-escalations, escalation share and unwanted comments, and exits 1 on a
  critical miss.
- **Store** (`src/calu/store/`): `ConfigStore` loads `Config`, the checker definitions and the
  rules and reloads them on `config_changed`; `DatabaseSessionStore` writes the session, its
  steps with decisions and results, the checker runs, the turns, the chat summary and the
  escalations; `MemorySessionStore` is the in-memory twin for unit tests.
- **Herd** (`src/calu/herd.py`): `sessions`, `escalations`, `resolve` on the database alone,
  with the `admin` credential (an own login for `calu_operator` is Q4).
- **Sandbox** (D15): `SandboxConfig` in `calu/config.py`, passed to Claude Code as a
  `--settings` JSON document (`SdkRunner._options`); `calu.preflight` warns about missing
  `bwrap`/`socat`, a disabled sandbox and missing checker API keys at every start.
- **Schema**: `0001_initial.sql` (tables, roles, audit and version triggers, the escalation
  functions) and `0002_turn_records.sql` (the remaining `TurnRecord` columns, the
  `claude_session_id` index, `calu_cancel_escalation(bigint, text)`).
- **`Db`** (`calu/db/connection.py`): one connection for several tasks, statements serialized
  with a lock and shielded from cancellation (D19). `calu/db/notify.py` holds the channel names,
  `listen`, `Dirty` and `Notifier`.

### Verified by hand (2026-11-09, real Claude Code 2.1.319, haiku, fake checker models)

`spikes/smoke_session.py` runs one real session against a throwaway database on the test server
and prints what it wrote; `--resolve allow|deny` plays the operator. The sandbox is off by
default, because this machine has no `socat`, so the commands Claude runs are not isolated: use
a throwaway directory.

    uv run python spikes/smoke_session.py /tmp/project --resolve deny

Results:

1. "run `ls`, then say what you found": allow-listed command, no model call, one `steps` row
   with `rules_outcome = allow`, turn and answer stored, states starting → idle → running →
   tool_call → running → idle → stopped, `sessions.claude_session_id` set.
2. "create NOTES.md with the Write tool, then run `git push --dry-run`": the edit went to the
   edit checker (ALLOW, one `checker_runs` row), the command to the command checker. Note that
   the default deny list only denies **force** pushes and deletions, not a plain `git push`
   (sitl commit 433aaa4), so this did not escalate.
3. "run `ls`, then create NOTES.md with Write": both steps were recorded as `pass`, so the
   hook's "no decision" answer reaches auto mode and the edit checker ran (D21).
4. "run `sudo -n ls /root`" (the default prompt of the script): the deny rule
   `^(sudo|su|doas)( |$)` matched, so no model call; the session opened escalation 1, went to
   state `escalated` and waited; `calu_resolve_escalation(1, 'denied', ...)` let it continue
   within a second, and Claude reported the denial with the note. This is REQUIREMENTS 12 end to
   end with the real CLI (`can_use_tool`).

### Left out of the port, on purpose

- sitl's **training mode** (`training.py`: the human labels every checker decision while the
  mode is on): not ported, and probably not needed in this form, because `calu evals --from-db`
  gets its labels from the escalations a human resolved anyway (Q9). Its protocol models were
  removed.
- sitl's **event log** (`history.py`): the bus takes over the event buffer and the sequence
  numbers that a reconnecting client needs; nothing of the stream goes into the database (D3).
- sitl's **`tui/`** (a Claude Code channel plugin for interactive users): channels are not used
  (PLAN.md §9, the architecture notes); `calu attach` covers the terminal client.
- `ProjectFiles`/`files()` and `session.history`: the editable project files are database rows
  now, and a client cannot change them through the session protocol, because the session role
  may only read them. They are changed with calu's own commands (still missing, see below).

## Next steps

1. **Q8 (ask the owner):** a client attached to the session decides an escalation in the
   session, which cancels the row with a note, because `calu_session` may not allow or deny
   (D18, §4.1 point 5). Alternatives: the client resolves with its own `calu_operator`
   credentials, or a database function lets `calu_session` record a client decision for its own
   session.
2. **Phase 5**: the network policy per project (Q5), the nested-sandbox case in Docker (Q1).
3. **Phase 6**: a web UI on the herd commands. **Phase 7**: the IDE client (sitl's VSCode
   extension against the new protocol) and the single binary.
4. Smaller gaps: the JSON Schema export (`calu/protocol/schema.py`) is not written to a file by
   any command; `calu attach` has no `/comment`; a client cannot read the step history of an
   earlier session (the bus buffer is per process, and `session.history` was dropped).

## Working rules the owner set

- `REQUIREMENTS.md` contains only requirements the owner stated (reworded, not broken down, not
  extended); change it only on the owner's words. Derived requirements and design go into
  `PLAN.md`.
- `AGENTS.md` stays minimal: onboarding into the project and the code layout; no invented
  conventions (the coding rules there are the owner's sitl rules, D11).
- Commit and push to `main` after each finished unit of work (`gh` is logged in as kadeng).
- Plain technical language in all documents; contract docstrings (enforced by ruff).
- Never copy code from claude-a2a (no license). Mistral: small models only.

## Where to find things

- **sitl** (predecessor, private, github.com/kadeng/stay-in-the-loop): the parent directory
  `..`. Its spike results: `.claude/skills/claude-code-integration/references/spike-results.md`;
  scripts in `spikes/`; its condensed Claude Code / Pydantic AI reference skills in
  `.claude/skills/` (read, do not copy into calu, D10).
- **calu spikes:** `spikes/RESULTS.md` and scripts (CLI 2.1.319). The spike extracted `socat`
  without root under `/tmp/calu-spike-tools` (`spikes/get-socat.sh`); install `socat` properly
  for real use.

## Environment notes (owner's machine)

- WSL2 Ubuntu has uv, Docker, bwrap, Node 24 (nvm) and a logged-in `claude` (2.1.319).
  **`socat` is still not installed**, so the sandbox cannot start and every session with the
  default `claude.sandbox` settings refuses to run (`apt install socat`).
- `~/.config/calu/.env` **does not exist yet**. Live checker tests and real checker models need
  it; the keys are in sitl's git-ignored `.env` (CRLF line endings). Copying them was refused in
  the last session (a sandbox rule), so the owner should create the file:
  `tr -d '\r' < ~/projects/stay-in-the-loop/.env > ~/.config/calu/.env && chmod 600 ...`.
  Live checker model: Nemotron Lightning on Fireworks (`CALU_TEST_CHECKER_MODEL` overrides it).
- `scripts/wsl-test.sh` uses `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`.
- Test database: `docker compose up -d caludb --wait` (127.0.0.1:55432, `calu_owner` /
  `calu-dev`, development only). Tests create and drop a database per test.
- The working tree is on the Linux file system; `.gitattributes` forces LF line endings.

## Left open in sitl (not calu work)

- `tests/unit/test_tui.py::test_channel_drops_messages_without_token` is skipped with a TODO.
- The owner's uncommitted `.idea/stay-in-the-loop.iml` change and untracked `.vscode/` are
  theirs; never commit them.
