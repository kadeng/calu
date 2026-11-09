# Handoff

State on 2026-11-09, end of the second implementation session. Read this, then `AGENTS.md`,
`REQUIREMENTS.md` and `PLAN.md` (decisions D1-D19, open questions §11, phases §12).

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
| 4 Bus and terminal client | Not started |
| 5-7 | Not started (the sandbox settings of phase 5 exist, D15) |

Checks: `scripts/wsl-test.sh -m 'not live'` gives ruff format/lint clean, pyright 0 errors,
211 tests passed (unit + database). The database tests need `docker compose up -d caludb --wait`.

### What exists

- **CLI** (`src/calu/cli.py`): `db up|down|migrate`, `setup`, `credential list|set|remove`,
  `run`, `sessions`, `escalations`, `resolve`. Passwords via getpass, or one line each from
  stdin when stdin is not a terminal (used by tests).
- **`calu run [DIR]`** (`src/calu/run.py`): asks for the session password, decrypts the
  `session` credential, opens two connections (queries, notifications), wipes the credential,
  loads the settings, creates the `sessions` row and starts Claude Code. Events are printed to
  the terminal (the stand-in client until phase 4). `-p/--prompt` sends the first message,
  `--batch` ends the run when Claude is idle again, otherwise further messages are read from
  stdin (`/quit` ends it). `--resume`, `--model`, `--effort`, `--project`, `--thinking`.
- **Session** (`src/calu/session.py`, ported from sitl's `session.py`): tool gate (rules, tool
  policy, checkers, auto-escalation), escalations, narration, chat queue, subagent result
  review, login hint, states of `PLAN.md` §3.1 derived from what runs (escalated > checking >
  tool_call > running > idle). `apply_settings` applies a reloaded configuration from the next
  step on.
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
3. "run `sudo -n ls /root`" (the default prompt of the script): the deny rule
   `^(sudo|su|doas)( |$)` matched, so no model call; the session opened escalation 1, went to
   state `escalated` and waited; `calu_resolve_escalation(1, 'denied', ...)` let it continue
   within a second, and Claude reported the denial with the note. This is REQUIREMENTS 12 end to
   end with the real CLI (`can_use_tool`).

### Left out of the port, on purpose

- sitl's **comment store** (diff and file comments) and **training mode** (human review of
  checker decisions for evaluation data): both were file-based and need tables of their own
  (Q9). The protocol models for them are still in `calu/protocol/messages.py`.
- sitl's **event log** (`history.py`): the bus of phase 4 takes over the event buffer and the
  sequence numbers that a reconnecting client needs.
- `ProjectFiles`/`files()`: the editable project files are database rows now; a client edits
  them through the bus.

## Next steps

1. **Phase 4, bus and terminal client.** A WebSocket JSON-RPC endpoint per session process
   (PLAN.md §6, D2): `state.get`, `events.subscribe` with sequence numbers, the requests of
   sitl's protocol, token issuing, then `calu attach` as a terminal client. The protocol models
   need a pass: remove or rework `files`, the training requests and `session.list/history`
   (which were file-based), add `escalation.decide` over the bus (see Q8), and regenerate the
   JSON Schema (`calu/protocol/schema.py`).
2. **Q8 (ask the owner):** a client attached to the session currently decides an escalation in
   the session, which cancels the row with a note, because `calu_session` may not allow or deny
   (D18, §4.1 point 5). Alternatives: the client resolves with its own `calu_operator`
   credentials, or a database function lets `calu_session` record a client decision for its own
   session. The answer shapes phase 4.
3. **`calu project add`, `calu config set`, `calu checker edit`** (PLAN.md §8): the only way to
   change configuration, rules or checker instructions today is SQL.
4. Phase 5 (network policy per project, Q1 nested sandbox, `socat` on the machine), phase 6
   (web UI), phase 7 (IDE client, single binary).

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
