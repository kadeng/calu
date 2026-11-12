# AGENTS.md

calu runs Claude Code sessions unsupervised for long stretches on Linux, checked by LLM checker
agents and humans, with a PostgreSQL database as the central store. It is the successor of
stay-in-the-loop (sitl).

## Status

Implementation in progress; see `HANDOFF.md` for the current state and `PLAN.md` §12 for the
phases.

## Files

| File | Content |
|---|---|
| `README.md` | Description for end users, getting started |
| `CALU_GUIDE.md` | The user documentation: commands, credentials, sandbox, remote access, web interface, ACP, checkers |
| `REQUIREMENTS.md` | The owner's requirements and priorities, only as stated by the owner. Do not add derived requirements; those belong in `PLAN.md`. |
| `PLAN.md` | Design, decisions, threat model, database sketch, CLI, open questions, phases |
| `HANDOFF.md` | State of the work and next steps, for the next session |
| `docs/TESTING.md` | The four test layers, the spikes and the manual checklist |
| `docs/CONFIGURATION.md` | Every setting with its type, default and meaning -- **generated** from `calu.config` by `calu.configdoc`; a test fails when the two disagree |
| `LICENSE` | GPL-2.0, verbatim -- never edited |
| `LICENSE.md` | What the licence covers and what it does not, in plain language (D109); `LICENSE` governs where the two could be read differently |
| `pyproject.toml` | Package, dependencies, ruff, pyright and pytest settings |
| `docker-compose.yml`, `docker/dev.Dockerfile` | Development system `dev` and test database `caludb` |
| `scripts/wsl-test.sh` | Format check, lint, type check and tests in WSL |
| `.github/workflows/checks.yml` | CI: the same checks, the database and browser layers, and the installed wheel |
| `.github/workflows/release.yml` | Builds the wheel for a `v*` tag and publishes it to PyPI; needs a one-time PyPI and environment setup, described at the top of the file |
| `spikes/` | Experiments on Claude Code behaviour, including the question tool (results in `spikes/RESULTS.md`) and `smoke_session.py`, a manual end-to-end run with the real `claude` |

## Code layout

| Path | Content |
|---|---|
| `src/calu/cli.py` | The `calu` command |
| `src/calu/credentials.py` | Encrypted credential store (Argon2id, AES-GCM) |
| `src/calu/setup.py` | `calu setup`: migrations, default checkers and rules, session login |
| `src/calu/run.py` | `calu run`: credentials, connections, session, protocol server, terminal output |
| `src/calu/attach.py` | `calu attach --plain`: the line-oriented terminal client of a running session (also used when input or output is a pipe) |
| `src/calu/tui/` | `calu attach` on a terminal: the full-screen client (Textual, the optional `tui` extra). `model.py` is the state as a reducer over the protocol's events (no Textual import); `connection.py` keeps the connection and reconnects; `actions.py` sends the user's requests; `fake.py` and `scenarios.py` are a scripted session for tests, screenshots and the demo; `settings.py` reads `tui.toml` and keeps the sent texts; `difftools.py`, `completion.py` and `export.py` are the Textual-free parts of the diff screen, of `@` completion and of `/export`; `adminaccess.py` stores what the client changes in the database with the local `admin` credential (a checker's instructions, `honesty.mode`), never through the session; `gittool.py` chooses the terminal git program `f4` starts and names the install command; `app.py`, `widgets/`, `screens/` and `tui.tcss` render the model |
| `src/calu/protocol/schema.py` | `calu protocol`: JSON Schema of the protocol, for clients in other languages |
| `src/calu/bus.py` | Event bus: sequence numbers, fan-out to the clients, replay buffer |
| `src/calu/tokens.py` | Client tokens (issued per session, stored hashed) |
| `src/calu/session.py` | The session orchestrator: lifecycle (start, run, stop, interrupt), the runner's events, turns and narration, the clients' requests, and the wiring of the components below |
| `src/calu/gate.py` | The decision about one tool call (`StepGate`): rules, routing, commits, running the checkers and combining their verdicts, auto-escalation, the budget; it returns the decision and writes nothing. Every error path ends in ESCALATE |
| `src/calu/escalations.py` | Escalations of a session (`EscalationDesk`): opening a row, waiting for the decision (client, database, timeout), cancelling, and the hold on other steps while one is open or being opened |
| `src/calu/write_sets.py` | What a command wrote (`WriteSets`, D31): the snapshots before and after it, the measured write set, its review by the changeset checkers and the undo of a refused change |
| `src/calu/recorder.py` | The session's writes to the store (`Recorder`): a failed write is reported and counted, and too many in a row stop the session (D59); the `steps` and `checker_runs` rows; the step times (D32) |
| `src/calu/steps.py` | Stateless helpers about one tool call: its paths relative to the project directory, the file an edit targets, the summary of its result |
| `src/calu/run_state.py` | What the session knows about one run (`RunState`) and about one tool call (`PerCall`); a start replaces it, so each run begins from the defaults |
| `src/calu/store/` | The database as the session sees it: configuration in (`config.py`), history and escalations out (`session.py`), in-memory twin for tests (`memory.py`), trusted commits and trusted file contents (`trust.py`) |
| `src/calu/herd.py` | `calu sessions`, `calu escalations`, `calu resolve`, `calu token` (database only) |
| `src/calu/admin.py` | `calu project`, `calu config`, `calu checker`, `calu rules`: changing what steers the checkers |
| `src/calu/evals/` | `calu evals`: evaluation cases, checker runs and their metrics |
| `src/calu/preflight.py` | Start-up warnings: sandbox tools, checker API keys, write-set settings |
| `src/calu/doctor.py` | `calu doctor`: what is in place and what a session would miss, before anything runs |
| `src/calu/machine.py` | In which process table a process runs (machine id, boot id, pid namespace): what a session records at its start and `calu sessions` compares before it takes a missing pid for a gone process (D202) |
| `src/calu/clock.py` | Watches calu's own clock: a backward jump breaks the attribution of file changes to steps (D32) |
| `src/calu/changes.py` | Git snapshots of the project directory: what a command wrote, as a patch, and how to undo it (D31, optional) |
| `src/calu/paths.py` | Where calu's own secrets and state are on this machine (the XDG directories); imports nothing from calu, so the configuration and the rules both use it |
| `src/calu/config.py` | Session configuration models, built from database values; sandbox settings; checker API keys |
| `src/calu/checkers/` | Rules engine and shell parser, the files a directory search reads (`search.py`), tool routing, step rendering, checker, result checker and changeset checker agents (`changeset.py`), the reviewer agent of the honesty review (`honesty_review.py`: batches, bounded concurrency, a failure verifies nothing), the checkers' read-only code tools (`tools.py`) |
| `src/calu/context/` | Turn records, narrator, context builder with chat summary folding |
| `src/calu/claude/` | Claude Code runner interface, Agent SDK runner (hook, auto mode), scripted fake runner |
| `src/calu/protocol/` | Client protocol: messages (Pydantic), JSON-RPC server and client over WebSocket, JSON Schema export |
| `src/calu/web/` | `calu web`: the herd overview (Starlette, the optional `web` extra), the settings form generated from `Config` (`settings.py`), the live relay to a session's own WebSocket, links into a git browser (`git.py`) |
| `src/calu/acp/` | `calu acp`: the Agent Client Protocol adapter — shapes (`messages.py`), JSON-RPC over newline-delimited JSON (`peer.py`), the translation to calu's protocol (`adapter.py`), the entry point (`serve.py`) |
| `src/calu/comments/` | User comments on selections in diffs and files |
| `src/calu/models.py` | Model resolution and bounded agent runs (Pydantic AI) |
| `src/calu/model_choice.py` | Which model the checkers get when none is named: the first provider whose credentials are found (D129) |
| `src/calu/claude_model.py` | The `claude:` model type: a check answered by the `claude` CLI on this machine's own account, no tools (D117) |
| `src/calu/honesty.py` | Code honesty: which code was verified to say what it does. Marks and the checks of their paths; the honesty state of a session (`HonestyState`: baseline from the trusted commits, marks, verified contents, unreviewed files, the note for checkers, whether a commit may be certified, the plan of an honesty review). The session applies a step's marks and verifications after its tool result and runs the honesty review; the gate decides from a snapshot |
| `src/calu/configdoc.py` | Walks `calu.config.Config` for `docs/CONFIGURATION.md` and `calu config list` |
| `src/calu/markdown.py` | Code fences that the quoted text cannot close (commands, diffs, file excerpts) |
| `src/calu/files.py` | Files that tool calls refer to (links for clients) |
| `src/calu/testing.py` | Rule-based fake checkers for tests without model calls |
| `src/calu/db/` | Connections (`Db`: one connection, several tasks), migrations (`migrations/NNNN_*.sql`), notifications (`notify.py`), local Docker database (`local.py`, `local-compose.yml`) |
| `src/calu/defaults/` | Built-in checker instructions and rule lists (loaded into the database) and the built-in evaluation cases |
| `tests/unit/` | Tests without database, `claude` or API keys |
| `tests/db/` | Tests against the test database (marker `db`) |
| `tests/browser/` | Tests in a headless browser (markers `browser` and `db`; `scripts/get-browser.sh` installs it without root) |

## Commands

Run from the repository root on Linux (on a Windows host: in WSL, with the virtual environment
outside the Windows checkout: `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`).

- `uv run pytest` (unit tests), `uv run pytest -m 'not live'` (also database tests; start the
  database first with `docker compose up -d caludb --wait`), `uv run pytest -m 'browser and db'`
  (a headless browser; `scripts/get-browser.sh` installs it without root), `uv run pytest -m live`
  (a real `claude` or API keys). `docs/TESTING.md` explains the layers, the spikes and the
  manual checklist.
- A new configuration field needs a docstring **under** it (`ConfigModel` turns those into
  descriptions, which the reference, the web settings form and the JSON schema read) and a
  regenerated `docs/CONFIGURATION.md`; `tests/unit/test_configdoc.py` says how.
- `uv run ruff format .`, `uv run ruff check .`, `uv run pyright`
- `scripts/wsl-test.sh [pytest args]`: all of the above
- Sync with `uv sync --all-extras`: the web interface is the optional `web` extra and the full-screen terminal client the optional `tui` extra; their tests skip without them.
- CI runs the same checks on every push and pull request, plus the browser layer and the installed wheel (`.github/workflows/checks.yml`). The live layer never runs in CI.
- Every `calu ...` line in the documentation's code blocks is parsed by the real parser in a test (`tests/unit/test_documented_commands.py`), so a renamed flag or a stale example fails the suite rather than a reader's terminal.

## Coding rules (from sitl, PLAN.md D11)

- Python first. Other languages only where an integration requires it; keep that layer thin.
- Design by contract: every function and method has a Google-style docstring with its full
  contract before it is implemented: what it does including side effects, `Args:` for every
  parameter, `Returns:`/`Yields:`, `Raises:` for exceptions callers must handle. Ruff's `D` and
  `DOC` rules enforce this for `src/`.
- Plain technical language in code and documents: subject before object, no mannered phrasing;
  prefer diagrams or math notation over long prose about multi-step interactions.
- Tests that need a real `claude` or model API keys are marked `live`; tests that need the
  database are marked `db`. Live checker tests use Nemotron Lightning on Fireworks; if Mistral
  is used, small models only, never Mistral Medium or Large.
