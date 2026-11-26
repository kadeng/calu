# AGENTS.md

calu runs Claude Code sessions unsupervised for long stretches on Linux, checked by LLM checker
agents and humans, with a PostgreSQL database as the central store. It is the successor of
stay-in-the-loop (sitl).

## Status

Implementation in progress; see `agent/records/HANDOFF.md` for the current state and
`agent/records/PLAN.md` §12 for the phases.

`agent/` holds what the coding agents write and maintain for themselves: the system of records
in `agent/records/` (requirements, plan, handoff, and `notes/` with specs, research, the test
plan and reports) and, when session traces are switched on, the traces in `agent/traces/`.
Everything written for people stays outside it (README, guide, `docs/`, the extensions'
READMEs). This file stays at the top level, where every agent looks first.

## Files

| File | Content |
|---|---|
| `README.md` | Description for end users, getting started |
| `DOCUMENTATION.md` | The user documentation: commands, credentials, sandbox, remote access, web interface, ACP, checkers |
| `agent/records/REQUIREMENTS.md` | The owner's requirements and priorities, only as stated by the owner. Do not add derived requirements; those belong in `agent/records/PLAN.md`. |
| `agent/records/PLAN.md` | Design, decisions, threat model, database sketch, CLI, open questions, phases |
| `agent/records/HANDOFF.md` | State of the work and next steps, for the next session |
| `agent/records/notes/` | Specs, research notes, the test plan (`test-plan.md`), reports, evaluations and their screenshots |
| `docs/TESTING.md` | The four test layers, the spikes and the manual checklist |
| `docs/CONFIGURATION.md` | Every setting with its type, default and meaning -- **generated** from `calu.config` by `calu.configdoc`; a test fails when the two disagree |
| `LICENSE` | A short notice (what the GPL covers, the public-domain exception for the shipped checker instructions, LICENSE.md applies), then the GPL-2.0 text verbatim -- the GPL text itself is never edited |
| `LICENSE.md` | What the licence covers and what it does not, in plain language (D109); its clarifications apply together with the GPL, as the notice in `LICENSE` says |
| `pyproject.toml` | Package, dependencies, ruff, pyright and pytest settings |
| `docker-compose.yml`, `docker/dev.Dockerfile` | Development system `dev` and test database `caludb` |
| `scripts/wsl-test.sh` | Format check, lint, type check and tests in WSL |
| `scripts/db-tests.sh` | The database layer (`uv run pytest -m 'db and not live and not browser'`, extra pytest arguments passed on); the form a sandboxed Calu session can run outside the sandbox (DOCUMENTATION.md, "Developing Calu inside Calu") |
| `.github/workflows/checks.yml` | CI: the same checks, the database and browser layers, and the installed wheel |
| `.github/workflows/release.yml` | Builds the wheel for a `v*` tag and publishes it to PyPI; needs a one-time PyPI and environment setup, described at the top of the file |
| `spikes/` | Experiments on Claude Code behaviour, including the question tool (results in `spikes/RESULTS.md`) and `smoke_session.py`, a manual end-to-end run with the real `claude` |

## Code layout

| Path | Content |
|---|---|
| `src/calu/cli.py` | The `calu` command |
| `src/calu/credentials.py` | Encrypted credential store (Argon2id, AES-GCM) |
| `src/calu/logins.py` | calu's two logins on a machine (`user`: the role calu_admin, used by everything; `db-admin`: the database owner, for migrations), one user password for both, and every prompt and message about that password (PLAN.md D265) |
| `src/calu/setup.py` | `calu setup`: migrations, default checkers and rules, the user login's password |
| `src/calu/run.py` | `calu run`: credentials, connections, session, protocol server, terminal output |
| `src/calu/launch.py` | The console script `calu-claude`, which starts Claude Code as another user with an environment built for it (D207); a module of its own outside `calu.claude`, so that the launcher imports little |
| `src/calu/logfile.py` | The log of `calu run` and `calu serve`: every record with its traceback in `$XDG_STATE_HOME/calu/log/<command>.log`, one line per warning or error on stderr (D225); the whole request of every refused model call in `<command>.refusals.jsonl` next to it (D289) |
| `src/calu/localsocket.py` | The root-owned Unix socket of a root session: binding it, checking that it is root's alone, connecting, and asking the kernel for the peer's uid (D215) |
| `src/calu/asuser.py` | calu as root: its own file access and the programs it starts in the project run as the unprivileged user (`resolve` reads `--user`, `prepare` creates that user's directories, `enter` switches, `child` gives the arguments for a program start) (D206) |
| `src/calu/serve.py` | `calu serve`: one process that holds the user login and runs many sessions as tasks; it claims requests from the database and runs each as `calu.run` does (D208) |
| `src/calu/projects_dir.py` | The projects directory of `calu serve` (D243): which directories it refuses to serve (`/`, hidden ones, system directories), whether a requested session directory is inside it, and the relay port derived from it |
| `src/calu/session_requests.py` | The rows `calu start` writes and `calu serve` answers: filing, claiming, finishing, waiting, withdrawing |
| `src/calu/relay.py` | The relay of `calu serve` (D242): one loopback port for every session of the server. Its own address lists the sessions and asks the server for new ones (`relay.sessions`, `relay.start`); `/session/<id>` checks the server token and copies messages to and from the session's own port, where the relay authenticates with the session's own token |
| `src/calu/servers.py` | The `servers` rows of `calu serve` processes and their server tokens (hash only): recording a server, checking a token, filing a request through one; minting, listing, revoking |
| `src/calu/attach.py` | `calu attach --plain`: the line-oriented terminal client of a running session (also used when input or output is a pipe) |
| `src/calu/tui/` | `calu attach` on a terminal: the full-screen client (Textual). `model.py` is the state as a reducer over the protocol's events (no Textual import); `connection.py` keeps the connection and reconnects; `actions.py` sends the user's requests; `fake.py` and `scenarios.py` are a scripted session for tests, screenshots and the demo; `settings.py` reads `tui.toml` and keeps the sent texts; `difftools.py`, `completion.py` and `export.py` are the Textual-free parts of the diff screen, of `@` completion and of `/export`; `adminaccess.py` stores what the client changes in the database with the user login (a checker's instructions, `honesty.mode`), never through the session; `gittool.py` chooses the terminal git program `f4` starts and names the install command; `app.py`, `widgets/`, `screens/` and `tui.tcss` render the model |
| `src/calu/protocol/schema.py` | `calu protocol`: JSON Schema of the protocol, for clients in other languages |
| `src/calu/bus.py` | Event bus: sequence numbers, fan-out to the clients, replay buffer |
| `src/calu/tokens.py` | Client tokens (issued per session, stored hashed) |
| `src/calu/session.py` | The session orchestrator: lifecycle (start, run, stop, interrupt), the runner's events, turns and narration, the clients' requests, and the wiring of the components below |
| `src/calu/gate.py` | The decision about one tool call (`StepGate`): rules, routing, commits, running the checkers and combining their verdicts, auto-escalation, the budget; it returns the decision and writes nothing. Every error path ends in ESCALATE |
| `src/calu/escalations.py` | Escalations of a session (`EscalationDesk`): opening a row, waiting for the decision (client, database, timeout), cancelling, and the hold on other steps while one is open or being opened |
| `src/calu/write_sets.py` | What a command wrote (`WriteSets`, D31): the snapshots before and after it, the measured write set, its review by the changeset checkers and the undo of a refused change |
| `src/calu/recorder.py` | The session's writes to the store (`Recorder`): a failed write is reported and counted, and too many in a row stop the session (D59); the `steps` and `checker_runs` rows; the step times (D32) |
| `src/calu/checkouts.py` | The checkouts of the project's repository (D251): the main checkout and every git worktree `git worktree list` names, inside the project or outside it; which checkout a path and a step belong to (the step's working directory and its `cd` targets); the lines that name them for a checker |
| `src/calu/locking/` | Locks that keep edits away from running commands, per worktree (D274); `call_locks.py` connects it to the session (D276), off by default. `scheme.py` says which locks a command or an edit takes, in which order the calls of one agent get them (`issue` gives a `Ticket`; withdrawing it cancels a pending call), and how long they may wait (`RWLockingScheme`; `NoLockingScheme` takes the same calls and never waits, for when the feature is off; `LockTimeouts`: a default and a maximum for commands and for edits, no unbounded wait); `rwlock.py` is the FIFO shared/exclusive lock every backend implements (owner = one tool call, no reentrancy) with notes for the file and PostgreSQL backends; `threadlocks.py` the backend for the threads of one process; `filelocks.py` the backend for the processes of one user on one machine (one state file per boot and pid namespace, bounded flock, entries removed only when their process provably ended; D275) |
| `src/calu/call_locks.py` | The locks of one session's tool calls (D276): which locks a command or an edit takes (the checkouts of its working directory and `cd` targets; the checkout and the file of an edit; the global lock for a file outside every checkout), its ticket when its check begins, its locks before it runs (in a worker thread, cancellable, bounded by the PreToolUse hook's time), and their release with its tool result. With `locking.enabled` off it makes the same calls on `NoLockingScheme`. The lock space is shared by the sessions of the process (`shared_space`) |
| `src/calu/steps.py` | Stateless helpers about one tool call: its paths relative to the project directory, the file an edit targets, the summary of its result |
| `src/calu/run_state.py` | What the session knows about one run (`RunState`) and about one tool call (`PerCall`); a start replaces it, so each run begins from the defaults |
| `src/calu/store/` | The database as the session sees it: configuration in (`config.py`), history and escalations out (`session.py`), in-memory twin for tests (`memory.py`), trusted commits and trusted file contents (`trust.py`) |
| `src/calu/herd.py` | `calu sessions`, `calu list-sessions`, `calu escalations`, `calu resolve`, `calu token` (database only) |
| `src/calu/admin.py` | `calu project`, `calu config`, `calu checker`, `calu rules`: changing what steers the checkers |
| `src/calu/evals/` | `calu evals`: evaluation cases, checker runs and their metrics |
| `src/calu/preflight.py` | Start-up warnings: sandbox tools, checker API keys, write-set settings |
| `src/calu/uninstall.py` | `calu uninstall`: what calu left on this machine (logins, state, the local database, the installation), removed after `remove calu` typed by hand (D267) |
| `src/calu/doctor.py` | `calu doctor`: what is in place and what a session would miss, before anything runs |
| `src/calu/machine.py` | In which process table a process runs (machine id, boot id, pid namespace): what a session records at its start and `calu sessions` compares before it takes a missing pid for a gone process (D202) |
| `src/calu/clock.py` | Watches calu's own clock: a backward jump breaks the attribution of file changes to steps (D32) |
| `src/calu/changes.py` | Git snapshots of the project directory: what a command wrote, as a patch, and how to undo it (D31, optional) |
| `src/calu/paths.py` | Where calu's own secrets and state are on this machine (the XDG directories); imports nothing from calu, so the configuration and the rules both use it |
| `src/calu/config.py` | Session configuration models, built from database values; sandbox settings; checker API keys |
| `src/calu/checkers/` | Rules engine and shell parser, the files a directory search reads (`search.py`), tool routing, step rendering, checker, result checker and changeset checker agents (`changeset.py`), the reviewer agent of the honesty review (`honesty_review.py`: batches, bounded concurrency, a failure verifies nothing), the checkers' read-only code tools (`tools.py`) |
| `src/calu/context/` | Turn records, narrator, context builder with chat summary folding |
| `src/calu/claude/` | Claude Code runner interface, Agent SDK runner (hook, auto mode), scripted fake runner, and `transcripts.py`: the conversations Claude Code keeps itself, for listing and resuming one that was started without calu (D209) |
| `src/calu/protocol/` | Client protocol: messages (Pydantic), JSON-RPC server and client over WebSocket, JSON Schema export |
| `src/calu/web/` | `calu web`: the herd overview (Starlette), the settings form generated from `Config` (`settings.py`), the live relay to a session's own WebSocket, links into a git browser (`git.py`), the chat pane with one `calu ide` bridge per browser tab (`pane.py`, D261; its script is the built `static/pane/`), the "New session…" dialog: a folder, a new one or a git clone below the server's projects directory (`start.py`, D270) |
| `extensions/vscode/src/host/` | The chat host shared by the VSCode extension and the chat pane (D261): the bridge client and password flow over any transport (`bridgeCore.ts`), the session commands (`sessions.ts`), what the chat's messages and the bridge's events do (`chatHost.ts`). No `vscode` import |
| `extensions/vscode/pane/` | The chat pane's browser host: the WebSocket to `calu web`, dialogs, the file and diff views (`webHost.ts`, `dialogs.ts`, `diff.ts`); `npm run build:pane` writes `src/calu/web/static/pane/` |
| `src/calu/acp/` | `calu acp`: the Agent Client Protocol adapter — shapes (`messages.py`), JSON-RPC over newline-delimited JSON (`peer.py`), the translation to calu's protocol (`adapter.py`), the entry point (`serve.py`) |
| `src/calu/ide/` | `calu ide`: the bridge an editor extension starts (JSON-RPC over newline-delimited JSON on stdin and stdout, `agent/records/notes/vscode-extension-spec.md`) — the requests (`bridge.py`), the connection to the attached session with its reconnects (`link.py`), the database login chosen without a prompt (`access.py`), what the editor reads and writes in the database (`database.py`), the model presets (`presets.py`), the entry point (`serve.py`); and the file synchronisation for an editor that works on a copy over SSH (`agent/records/notes/jetbrains-sync-spec.md`): the rules that need no file access (`files.py`: path checks, limits, hash cache, change log, held files), every access to the project's files, confined to the project directory (`filestore.py`), the `ide.fs.*` requests, the polling watcher and the `ide.fs.changed` notifications (`filesync.py`) |
| `src/calu/lightcycles/` | An easter egg, hidden from `calu --help` and not in the user documentation: `calu lightcycles` serves MetaTron for the browser (after MetaTron by Kai Londenberg) on 127.0.0.1. `server.py` is the static file server with its Content-Security-Policy; the game is plain JavaScript in `static/` (`sim.js` one frame of a round, `flow.js` the menus, `water.js`, `render.js`, `main.js`), tested in `tests/browser/test_lightcycles.py` |
| `src/calu/comments/` | User comments on selections in diffs and files |
| `src/calu/models.py` | Model resolution and bounded agent runs (Pydantic AI) |
| `src/calu/lazy.py` | Names a package exports but imports at first use (`calu.checkers`, `calu.context`, `calu.evals`): the modules that need Pydantic AI are not imported by a command that runs no checker; `tests/unit/test_startup_imports.py` holds what `calu.cli` may import |
| `src/calu/model_choice.py` | Which model the checkers get when none is named: the first provider whose credentials are found (D129) |
| `src/calu/honesty.py` | Code honesty: which code was verified to say what it does. Marks and the checks of their paths; the honesty state of a session (`HonestyState`: baseline from the trusted commits, marks, verified contents, unreviewed files, the note for checkers, whether a commit may be certified, the plan of an honesty review). The session applies a step's marks and verifications after its tool result and runs the honesty review; the gate decides from a snapshot |
| `src/calu/configdoc.py` | Walks `calu.config.Config` for `docs/CONFIGURATION.md` and `calu config list` |
| `src/calu/markdown.py` | Code fences that the quoted text cannot close (commands, diffs, file excerpts) |
| `src/calu/controls.py` | Control characters made visible for the terminal clients (`calu attach`, full-screen and line client): escape sequences, carriage returns and bidirectional controls are replaced by signs, so that no text of the session moves the cursor or draws a command in another order than it would run |
| `src/calu/traces/` | Session traces (D232): the conversation appended as Markdown to `<traces.directory>/<date>/<user>/session_NNN.md` inside the project. `render.py` turns the session's events into blocks and creates the file (exclusive open); `redact.py` is the two-stage redaction (known secret values and key shapes, then the redaction agent; a failure withholds the block); `writer.py` is the bounded queue between the session and the file (order kept, a step never waits, a failure never stops the session); `finalize.py` is the final redaction pass before a commit over what the commit adds (D246); `hook.py` installs and removes the opt-in git pre-commit hook that runs it |
| `src/calu/human_diffs.py` | The user's own saves in an IDE editor as diffs (D253, `human_diffs.*`): the unified diff of one save, appended to `<directory>/<date>/<user>/<path>.tmp.diff`; the `.gitignore` of the directory; whether git tracks a file; the cleanup of old `*.tmp.diff` files. The session's `human_diff` answers `human_diffs.add` |
| `src/calu/homes.py` | A session's directory with its user's home written as `~` (another user's of this machine as `~user`), for the session lists of the terminal clients and the web interface (D248); imports nothing from calu |
| `src/calu/files.py` | Files that tool calls refer to (links for clients) |
| `src/calu/testing.py` | Rule-based fake checkers for tests without model calls |
| `src/calu/db/` | Connections (`Db`: one connection, several tasks), migrations (`migrations/NNNN_*.sql`: `0001_baseline.sql`, a cleaned-up schema dump of 2026-11-24, D266, and the migrations after it), notifications (`notify.py`), what is stored about the database and its logins (`target.py`: the three variants of `calu setup`), where root's database is, without a secret, for clients that asked no database (`place.py`, D228), login tokens for RDS with IAM authentication (`rds.py`), calu's roles (`roles.py`, D265), local Docker database with its on-demand start (`local.py`, `local-compose.yml`) |
| `src/calu/defaults/` | Built-in checker instructions and rule lists (loaded into the database), the built-in evaluation cases, and the templates of checker instructions (`templates/`, one Markdown file each, read by `library.py`; loaded only by `calu checker template use`) |
| `tests/unit/` | Tests without database, `claude` or API keys |
| `tests/db/` | Tests against the test database (marker `db`) |
| `tests/browser/` | Tests in a headless browser (marker `browser`; those of the web interface also `db`, the easter egg's not: `-m browser` runs all of them; `scripts/get-browser.sh` installs it without root) |
| `tests/root/` | Tests that need real root (marker `root`; skipped unless root and `CALU_ROOT_TESTS=1`); they run in the root box: `docker exec calu-rootbox rootbox-tests` |
| `tests/fuzz/` | Random load on `calu.locking` (marker `fuzz`; skipped unless `-m fuzz` selects it, never in CI): `locking_harness.py` runs simulated agents as threads, counts timeouts, running and waiting operations, deadlocks and overlaps, and compares a scenario with and without locks (`uv run python -m tests.fuzz.locking_harness --help`) |
| `uitest/rootbox/` | The root box: a container with root, sudo and three users for running the privileged arrangements for real; `fakecalu.py` is calu's command line with a scripted runner (`uitest/README.md`) |

## Commands

Run from the repository root on Linux (on a Windows host: in WSL, with the virtual environment
outside the Windows checkout: `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`).

- `uv run pytest` (unit tests), `uv run pytest -m 'not live'` (also database tests; start the
  database first with `docker compose up -d caludb --wait`), `uv run pytest -m browser`
  (a headless browser; `scripts/get-browser.sh` installs it without root), `uv run pytest -m live`
  (a real `claude` or API keys), `uv run pytest -m fuzz -s tests/fuzz` (random load on the
  locking component, about a minute; never in CI). `docs/TESTING.md` explains the layers, the
  spikes and the manual checklist.
- In a Calu session with Claude Code's sandbox on, run the database tests exactly as
  `scripts/db-tests.sh [pytest args]` (one command, no `./`, no `cd` or chain): only that form
  can reach the test server there.
- A new configuration field needs a docstring **under** it (`ConfigModel` turns those into
  descriptions, which the reference, the web settings form and the JSON schema read) and a
  regenerated `docs/CONFIGURATION.md`; `tests/unit/test_configdoc.py` says how.
- `uv run ruff format .`, `uv run ruff check .`, `uv run pyright`
- `scripts/wsl-test.sh [pytest args]`: all of the above
- Sync with `uv sync`. calu is one package without optional extras: the web interface and the full-screen terminal client are ordinary dependencies.
- CI runs the same checks on every push and pull request, plus the browser layer and the installed wheel (`.github/workflows/checks.yml`). The live layer never runs in CI.
- Every `calu ...` line in the documentation's code blocks is parsed by the real parser in a test (`tests/unit/test_documented_commands.py`), so a renamed flag or a stale example fails the suite rather than a reader's terminal.

## Coding rules (from sitl, agent/records/PLAN.md D11)

- Python first. Other languages only where an integration requires it; keep that layer thin.
- Design by contract: every function and method has a Google-style docstring with its full
  contract before it is implemented: what it does including side effects, `Args:` for every
  parameter, `Returns:`/`Yields:`, `Raises:` for exceptions callers must handle. Ruff's `D` and
  `DOC` rules enforce this for `src/`.
- Plain technical language in code and documents: subject before object, no mannered phrasing;
  prefer diagrams or math notation over long prose about multi-step interactions.
- A remark from calu's checkers or a reviewer, style included, is a reason to look again: check
  whether it is right, then decide with your own judgement. Do not dismiss one as "style only".
  Prose style decides whether the project is taken seriously.
- Tests that need a real `claude` or model API keys are marked `live`; tests that need the
  database are marked `db`. Live checker tests use Nemotron Lightning on Fireworks; if Mistral
  is used, small models only, never Mistral Medium or Large.
