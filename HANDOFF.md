# Handoff

State on 2026-11-09 (implementation started). Read this, then `AGENTS.md`,
`REQUIREMENTS.md` and `PLAN.md`.

## Where things stand

- 2026-11-09: the owner gave the go to implement ("start implementing, commit and push at
  reasonable checkpoints, stop only if you must ask; reversible decisions are yours").
- Phase 1 (skeleton) is done: package `calu`, CLI (`db up/down/migrate`, `setup`,
  `credential`), credential store, schema migration 0001, default checkers and rules loaded by
  `calu setup`, `docker-compose.yml` with `dev` and `caludb`. Decisions D10-D14 in `PLAN.md`.
- Next: phase 2 (port of sitl's core), see "Next steps".
- Decided (details and dates in `PLAN.md` §2): Python 3.14 with uv, first as a uv-installable
  package with a `calu` command (single binary later); clients over WebSocket with JSON-RPC;
  PostgreSQL as the central store, holding all configuration and security settings; the sandbox
  encloses the tool calls, not Claude Code; checkers combined with Claude Code's auto mode;
  Linux only; password-protected start with credentials dropped before Claude starts.
- Open (`PLAN.md` §11): isolation mechanism (Q1), relationship to stay-in-the-loop (Q2), where
  a per-session database role (Q3), remote client authentication (Q4), sandbox network
  policy (Q5), long-unanswered escalations (Q6), checkers on the Anthropic subscription through
  headless Claude Code (Q7).

## Background from the planning discussion

- **Why Python (2026-11-09):** the owner first wanted a single executable and asked about Go or
  Rust with a Pydantic AI-like library. Findings then: no Go port of Pydantic AI was found; Go has
  langchaingo and several young typed-agent libraries; Rust has Rig (Anthropic, OpenAI, Bedrock
  as the `rig-bedrock` crate; Mistral and Fireworks not confirmed; pre-1.0). The Claude Agent SDK
  is official only for Python and TypeScript; community ports of its control protocol exist for
  Go (e.g. panbanda/claude-agent-sdk-go) and Rust (e.g. claude-agent-sdk-rs, rusty_claw), all
  young. The owner then simplified: Python now (reusing sitl's core of about 10,000 lines plus
  3,300 lines of unit tests), uv package first, PyApp or PyInstaller later, a port to Go or Rust
  possible once calu works.
- **Anthropic subscription for checkers:** no model library can call the API with the claude.ai
  subscription; only Claude Code can. Hence the planned model type that runs checks through
  headless Claude Code (`PLAN.md` §3.2, Q7). The owner's requirement of subscription support
  stands in `REQUIREMENTS.md`.
- **Why the sandbox encloses the tool calls (D4):** a Bash command running as the same user as
  Claude Code can open `/proc/<claude-pid>/fd/0` and write forged messages into Claude Code's
  stdin (forged hook answers or user messages), and could change Claude Code's code or settings.
  Yama's ptrace restriction does not prevent this. Separating only calu from Claude Code is
  therefore not enough; tool commands must not reach the Claude Code process or its files. A
  separate Unix user for Claude Code may not be possible where calu will be used, so the
  mechanism stays open (Q1).

## Working rules the owner set

- Implementation is allowed since 2026-11-09 (see above). `REQUIREMENTS.md` stays the owner's:
  change it only on the owner's words.
- `REQUIREMENTS.md` contains only requirements the owner stated (reworded, not broken down, not
  extended). Derived requirements and design go into `PLAN.md`.
- `AGENTS.md` stays minimal: onboarding into the project and the code layout; no invented
  conventions.
- Commit and push to `main` after each finished unit of work (the owner allowed commits in
  calu; `gh` is logged in as kadeng).
- Plain technical language in all documents.

## Where to find things

- **stay-in-the-loop (sitl)**, the predecessor: a private repository
  (github.com/kadeng/stay-in-the-loop). On the owner's machine, calu is cloned inside its working
  tree as `calu/` (ignored by sitl's `.gitignore`), at
  `C:\Users\KaiLo\projects\stay-in-the-loop\calu` (WSL: `/mnt/c/Users/KaiLo/projects/stay-in-the-loop/calu`).
  Code to reuse is listed in `PLAN.md` §9; it lives under `src/stay_in_the_loop/` of sitl.
- **Verified Claude Code behaviour:** sitl's
  `.claude/skills/claude-code-integration/references/spike-results.md` and
  `approval-mechanisms.md`, with the spike scripts in sitl's `spikes/`. The auto-mode results
  behind decision D5 come from `spikes/spike_auto_mode.py` (2026-11-07, bundled CLI 2.1.292,
  model sonnet; haiku has no auto mode).
- **claude-a2a** (github.com/jcwatson11/claude-a2a, cloned in sitl's `.reference/`) has no
  license: never copy code from it into calu.

## Environment notes (owner's machine)

- Host Windows; builds and tests run in WSL2 Ubuntu or Docker. WSL has uv, Node 22 (nvm, only in
  interactive shells: `bash -ic`) and a logged-in `claude`. Docker Desktop may need to be started
  first, and right after its start pulls from Docker Hub can fail once (retry).
- sitl's virtual environment in WSL: `UV_PROJECT_ENVIRONMENT=$HOME/.cache/sitl-venv`.
- API keys (`FIREWORKS_API_KEY`, `MISTRAL_API_KEY`) are in sitl's git-ignored `.env`, which has
  CRLF line endings (strip `\r` before sourcing it in bash).
- Live tests and checks of checker instructions use Nemotron Lightning on Fireworks
  (`fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b`); Mistral only small
  models, never Mistral Medium or Large.

## Next steps

1. Wait for the owner's answers on the open questions they want settled before phase 1, and for
   the go to start implementing.
2. Spikes that inform Q1 and Q7, once allowed: Does Claude Code's own Bash sandbox (bubblewrap)
   give tool commands their own PID namespace, so they cannot open the Claude Code process's
   pipes? Does `CLAUDE_CODE_SHELL_PREFIX` let calu wrap every Bash command? Does `claude -p` with
   a JSON schema give reliable structured checker answers?
3. Phase 1 (`PLAN.md` §12): package skeleton, `calu` CLI, `docker-compose.yml` with `dev` and
   `caludb`, schema and migrations, `calu db up/init`, `calu setup`.

## Left open in sitl (not calu work)

- `tests/unit/test_tui.py::test_channel_drops_messages_without_token` is skipped with a TODO
  (flaky under load, cause unknown; rewrite as an in-process test).
- The owner's uncommitted change to `.idea/stay-in-the-loop.iml` and the untracked `.vscode/`
  are theirs; never commit them.
