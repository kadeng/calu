# Handoff

State on 2026-11-09, end of the first implementation session. Read this, then `AGENTS.md`,
`REQUIREMENTS.md` and `PLAN.md` (decisions D1-D17, open questions §11, phases §12).

## Instructions from the owner (2026-11-09)

- "Start implementing. Commit and push at reasonable checkpoints. Stop only if you must ask
  something. You can make tradeoff decisions. You are authorized to make reversible decisions
  (everything that's just in git is reversible)."
- The next session should run **inside Linux** (e.g. Claude Code started in WSL in the calu
  directory), not on the Windows host. The previous session ran on Windows and called WSL through
  `wsl.exe`, which caused quoting and path-conversion problems (Git Bash rewrote paths; use
  `MSYS_NO_PATHCONV=1` if you ever have to do that again).

## Why calu sits inside the stay-in-the-loop directory

calu is the successor of stay-in-the-loop (sitl) and is built by porting sitl's code. On the
owner's machine, calu is cloned as `calu/` inside sitl's working tree
(`/mnt/c/Users/KaiLo/projects/stay-in-the-loop/calu`) so that a session can read sitl's source,
tests and spike results next to calu. It is still its own git repository
(github.com/kadeng/calu, public, GPL-2.0) and is listed in sitl's `.gitignore`. Commit calu work
in `calu/` only; sitl is a separate, private repository.

## Where things stand

| Phase (`PLAN.md` §12) | State |
|---|---|
| 1 Skeleton | Done (commit a68f8ac) |
| Spikes Q1, Q7 | Done (8d46c0f), results in `spikes/RESULTS.md`, decisions D15, D16 |
| 2 Core port | Done for the modules (6628733); the database loader for configuration is still missing (see next steps) |
| 3 Session runner | Not started; analysis below |
| 4-7 | Not started |

Checks: `scripts/wsl-test.sh -m 'not live'` gives ruff format/lint clean, pyright 0 errors,
144 tests passed (unit + database tests). The database tests need
`docker compose up -d caludb --wait`.

### What exists

- **CLI** (`src/calu/cli.py`): `calu db up [--port]`, `db down`, `db migrate`, `setup
  [--checker-model]`, `credential list|set|remove`. Passwords via getpass, or one line each
  from stdin when stdin is not a terminal (used by tests). Verified end to end against a real
  Docker PostgreSQL (`db up` → `setup` → `migrate` → `down`, wrong password → exit 1).
- **Credential store** (`credentials.py`, D12): `~/.config/calu/credentials.json`, entries
  `admin` (owner `calu_owner`) and `session` (role `calu_session`), Argon2id + AES-256-GCM,
  name as associated data.
- **Schema** (`db/migrations/0001_initial.sql`): tables of `PLAN.md` §7, roles `calu_session`
  and `calu_operator` (cluster-wide, NOLOGIN until `setup`), audit trigger + `config_changed`
  notify on `config`, `rules`, `checker_types`; checker version bump trigger; escalations are
  resolved only via `calu_resolve_escalation(id, 'allowed'|'denied', note)` (operator, notifies
  `escalation_resolved`) and cancelled via `calu_cancel_escalation(id)` (session). Tests prove
  the session role cannot change configuration, resolve escalations or read the audit log.
  Row-level security per session is not done yet.
- **Local DB** (`db/local.py`, `db/local-compose.yml`, D14): compose project `calu-db`, volume
  `calu-db_data`, 127.0.0.1:5433.
- **Setup** (`setup.py`): migrations, default checker types (`command`, `edit`,
  `subagent-result`, `narrator`; model `mistral:mistral-small-2603`) and default rules parsed from
  `defaults/rules/*.txt` (a comment block directly above a pattern becomes its comment), new
  random password for `calu_session`.
- **Ported core** (from sitl `src/stay_in_the_loop/`, package renamed to `calu`):
  `checkers/` (rules, shell, policy, render, checker, result_checker, model),
  `context/` (builder, narrator, turns, foreign), `claude/` (runner, sdk_runner, fake_runner),
  `protocol/` (messages, schema; not client/server), `comments/` (model, format, mapping; not
  store), `models.py`, `markdown.py`, `files.py`, `testing.py` (fake checkers; markers renamed
  `sitl:*` → `calu:*`), `config.py`.
- **Adaptations made in the port:**
  - `Rules` holds lists in memory: `Rules(workspace, lists)`, `Rules.from_rows(workspace,
    [(list, pattern, id)])`, `Rules.from_texts(...)`, `replace(lists)`. List names have no
    `.txt`. Built-in read+write deny: `(^|/)\.config/calu(/|$)`.
  - `Checker`, `ResultChecker`, `Narrator` take `instructions: str` (agent rebuilt when the
    attribute changes) instead of an instructions file path.
  - `ContextBuilder(workspace, config, ..., summary="", on_summary=async callback)`: the chat
    summary lives in memory (`builder.summary`) and is handed to `on_summary` after each fold
    (to be stored in `chat_summaries`).
  - `config.py`: Pydantic models kept; file layout (`ProjectPaths`, `config.toml`,
    `ensure_project_files`) removed; `build_config(layers, checker_rows)` merges global and
    project `config` rows (key `settings`) and checker rows; `load_secrets()` reads
    `~/.config/calu/.env` (D17). `ClaudeConfig.auto_mode` defaults to True (D5).
- **Tests dropped in the port** (they used sitl's `make_session`; bring them back with the new
  session in phase 3): in `test_rules.py` the three session tests at the end; in
  `test_narrator.py` `run_turn`, `test_session_narrates_before_checked_and_unchecked_steps`,
  `test_checker_context_shows_summary_and_narrated_turns`, `test_narration_can_be_turned_off`;
  in `test_result_checker.py` the four session tests; `test_streaming.py`
  `test_fake_runner_streams_words_before_final_text`; `test_files.py`
  `test_session_sends_tool_files_for_bash`. sitl's `test_session.py`, `test_auto_escalate.py`,
  `test_chat_queue.py`, `test_login_hint.py`, `test_review*_fixes.py`, `test_protocol.py`,
  `test_history.py`, `test_comments.py`, `test_training.py`, `test_evals.py` were not ported.
- `tests/conftest.py` lacks sitl's `checker_test_model` fixture and the
  `models.ALLOW_MODEL_REQUESTS` guard; the one `live` test in `test_checkers.py` needs both.
  Port them from sitl's `tests/conftest.py` (it imports `PROVIDER_KEYS_BY_PREFIX` from sitl's
  `preflight.py`, which was not ported; inline the mapping or port `preflight.py`).

## Next steps

1. **Finish phase 2:** a store module (e.g. `src/calu/store.py`) that loads, for a project,
   `Config` (`build_config` from `config` rows + `checker_types`), the checker instructions by
   name, and `Rules.from_rows` from `rules` (global + project, ordered by `position`); and that
   reloads them on `config_changed`. Database tests for it. Port the conftest pieces above.
2. **Phase 3, session runner.** Recommended approach: port sitl's `session.py` (1,600 lines;
   the orchestrator: ToolGate `pre_tool_use`/`_decide`/`ask`, escalations, narration, queue,
   subagent result review, login hint, state events) and replace its file-based parts with an
   injected store interface (database implementation + in-memory implementation for unit
   tests), so sitl's `test_session.py` and the dropped tests can be ported with few changes.
   File-based parts to replace: `ProjectPaths`/`ensure_project_files`, `CommentStore`,
   `EventLog` (`history.py`), `TrainingDesk` (`training.py`, decisions log), summary switching
   between Claude sessions (`_switch_summary`, `_claim_summary`, `_restore_context`),
   `files()`/`checker_context()` (file paths for the editor; replace by database content).
   Then: steps, checker runs, turns, escalations and session state written to the database;
   escalation waits for `escalation_resolved` (LISTEN) as well as local decisions; `calu run`
   with the password prompt, credential decrypt, connect, wipe, then start Claude Code (D8);
   SdkRunner passes the sandbox settings of D15 via `--settings` and checks for `bwrap`/`socat`.
   States in the DB are those of `PLAN.md` §3.1 (sitl uses starting/idle/busy/waiting/stopped;
   map or migrate).
3. Phase 4 (WebSocket JSON-RPC bus, `calu attach`), then 5-7 as in `PLAN.md` §12. D16 (checker
   model type through `claude -p`) can be added to `models.py` at any point.

## Background from the planning discussion

- **Why Python (2026-11-09):** the owner first wanted a single executable and asked about Go or
  Rust. No Go port of Pydantic AI exists; Rust has Rig (pre-1.0); the Claude Agent SDK is
  official only for Python and TypeScript. The owner chose Python now (reusing sitl's core), uv
  package first, PyApp or PyInstaller later, a port to Go or Rust possible once calu works.
- **Anthropic subscription for checkers:** only Claude Code can use the claude.ai subscription;
  hence D16.
- **Why the sandbox encloses the tool calls (D4):** a Bash command running as the same user as
  Claude Code can open `/proc/<claude-pid>/fd/0` and forge messages into Claude Code's stdin;
  confirmed by the control run in `spikes/RESULTS.md`.

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
  `..` of this repository on the owner's machine. sitl's spike results:
  `.claude/skills/claude-code-integration/references/spike-results.md`; scripts in `spikes/`;
  its condensed Claude Code / Pydantic AI reference skills in `.claude/skills/` (read, do not
  copy into calu, D10).
- **calu spikes:** `spikes/RESULTS.md` and scripts (CLI 2.1.319). The spike extracted `socat`
  without root under `/tmp/calu-spike-tools` (`spikes/get-socat.sh`); install `socat` properly
  for real use.

## Environment notes (owner's machine)

- WSL2 Ubuntu has uv, Docker, bwrap, Node 22 (nvm, interactive shells only), a logged-in
  `claude` (2.1.319). No `socat` installed system-wide.
- Virtual environment outside the Windows checkout: `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`
  (set by `scripts/wsl-test.sh`).
- Test database: `docker compose up -d caludb --wait` (127.0.0.1:55432, `calu_owner` /
  `calu-dev`, development only). Tests create and drop a database per test.
- The working tree is on a Windows drive with `core.autocrlf=true`; `.gitattributes` forces LF.
- Checker API keys for live tests are in sitl's git-ignored `.env` (CRLF line endings); calu
  reads keys from `~/.config/calu/.env`. Live checker model: Nemotron Lightning on Fireworks.

## Left open in sitl (not calu work)

- `tests/unit/test_tui.py::test_channel_drops_messages_without_token` is skipped with a TODO.
- The owner's uncommitted `.idea/stay-in-the-loop.iml` change and untracked `.vscode/` are
  theirs; never commit them.
