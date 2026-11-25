# Testing calu

Four layers, each with its own cost, plus the spikes that measure what Claude Code actually
does. The default run needs nothing but the repository.

| Layer | Marker | Needs | How long |
|---|---|---|---|
| Unit | none | nothing (a few spawn `python -m calu` or a stub `docker`) | ~1.5 min |
| Database | `db` | PostgreSQL (`docker compose up -d caludb --wait`) | ~20 s |
| Browser | `browser` (most also `db`) | a headless Chromium (`scripts/get-browser.sh`) | ~8 s |
| Live | `live` | a real `claude` and/or model API keys | minutes, costs money |
| Root | `root` (five also `db`) | real root, in the root box (`uitest/rootbox`) | ~15 s |
| Fuzz | `fuzz` | nothing (threads of one process); run by hand, never in CI | about 1 min |
| Spikes | — | run by hand, against the real CLI | minutes each |
| VSCode extension | — | Node 22+ (unit); Docker (integration: a real VSCode under xvfb) | ~1 s unit; ~1 min integration (+ a 400 MB VSCode download once) |

```bash
scripts/wsl-test.sh                 # format, lint, types and the unit tests
scripts/wsl-test.sh -m 'not live'   # ... and the database tests (start the database first)
uv run pytest -m browser            # the browser tests (most also need the database)
uv run pytest -m live               # the live tests (needs keys)
```

The shipped checker instructions are measured against the shipped evaluation cases by
`tests/unit/test_instruction_evals_live.py` (marker `live`; about 110 model calls, two minutes
with Haiku): the honesty checker on 53 labelled edits, the honesty reviewer on 14 batches of
files, and the four short checkers on their rules and their test triggers. Run it after a
change to a file of `src/calu/defaults/checkers/`, with the model the checkers will use:

```bash
CALU_TEST_CHECKER_MODEL=anthropic:claude-haiku-5-5 uv run pytest -s -m live tests/unit/test_instruction_evals_live.py
```

`-s` prints the reports (confusion table, precision and recall per class, every miss). The
numbers of 2026-11-18 are in `agent/records/notes/honesty-evals-2026-11-18.md`.

`scripts/wsl-test.sh` runs `ruff format --check`, `ruff check`, `pyright` and `pytest` in that
order, with the virtual environment outside the checkout
(`UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`), which matters on a Windows host where the
repository lives on a mounted drive. The default `addopts` deselect `live`, `db`, `browser` and
`root`, so a plain `uv run pytest` is the fast layer.

Four tests run a real process instead of a mock, because that is the only way to check
them: `tests/unit/test_acp_process.py` spawns `python -m calu acp` and speaks ACP to its pipes,
`tests/unit/test_attach_process.py` spawns `python -m calu attach` against a real session and
types at it, and `tests/unit/test_local_db.py` puts a stub `docker` on PATH and checks what
`calu db up` would run, and `tests/db/test_web_process.py` starts `python -m calu web` with a
credential store of its own and checks that it serves the fleet page on loopback and **refuses**
a public address. The first three need neither Docker nor a database; coverage does not see the
code that runs in a child process -- `calu/acp/serve.py` and the input loop of
`calu/attach.py` are exercised end to end while reporting as uncovered.

Sync with `uv sync`: the web interface and the full-screen terminal client are part of the one
package (no optional extras since 2026-11-13). The tests that need their packages still skip
with a message when one cannot be imported, rather than failing to collect.

### The full-screen terminal client (`calu attach`)

The client (`src/calu/tui/`) is tested in the unit layer, without a terminal, a database or
`claude`; its tests take about 40 s of the unit run.

| File | What it drives | With |
|---|---|---|
| `test_tui_model.py` | `calu.tui.model`: the state as a reducer over protocol events (joins by tool call, questions, subagents, usage, side questions); `HANDLED` must equal the protocol's `EVENTS` | nothing |
| `test_tui_render.py` | `calu.tui.render`: every line the client draws, compared as plain text | Rich only |
| `test_tui_actions.py` | `calu.tui.actions` and the reconnect loop of `calu.tui.connection`: the exact request of each intention, refusals, falling behind, a revoked token; the command table against the line client's `HELP` | the scripted session `calu.tui.fake` |
| `test_tui_app.py` | the Textual app under `App.run_test()` with `Pilot` pressing keys: sending in each mode, `a a`/`d d`/note, questions, the detail view, agent switching, side questions, the scrolling rules (the scroll offset and every visible line stay while output arrives), 80x24 and 120x36, a session of 2,000 steps | the scripted session |
| `test_tui_more.py` | the same app for what came second: the start card of a stopped session, the confirmed stop-and-resume with another model or effort, resuming a stored conversation, the stored history above the replayed events, the diff screen (side by side, unified, jumps between changes, a comment on selected lines), the side panel as a list (queue, held drafts, marks), held drafts sent when the session turns idle, search, copy, export, `@` path completion, large pastes, the settings file, other keys, `NO_COLOR` | the scripted session |
| `test_tui_tools.py` | what needs neither Textual nor a session: `calu.tui.settings` (`tui.toml`, the history of sent texts), `calu.tui.difftools` (the rows of a diff, a selection as a line range), `calu.tui.completion`, `calu.tui.export`, and the stored history in the model | nothing |
| `test_tui_admin.py` | the checkers screen (reading through the session, saving edited instructions with the user login, the calu user password question and a wrong answer, a stored login the database refuses (called outdated), a machine without the user login), code honesty (today's `honesty.update` and the one with baseline and unreviewed files, the page, `/trust` with its yes/no question, `/trust-file`, `/honesty-review`, `/honesty off\|track\|enforce` with the confirmation of a weaker mode), and `f4` (which git program is chosen, the install command per package manager, that a command with `sudo` is only shown) | the scripted session; `FakeAdmin` for the credential; `CaluApp.run_program`, `which` and `environ` for the external program |
| `test_tui_screens.py` | the whole screen as text for six scripted sessions at 120x36 and 80x24, compared with the files in `tests/unit/tui_screens/`; the same text without colours | the scripted session |
| `test_tui_review.py` | what two reviews found, each as a test: a long command on the card (wrapped, the cut counted, allowed from the detail only), an edit's changed lines on the card, one resize in each direction, a reply that begins with a letter of the card (typed whole), a decision bound to its escalation (the second press, the note question and the detail never reach a step that took the place; a key right after the card changed; a held key), Enter on a question set, the list of slash commands, a message on a subagent's tab, hints that fit the width, repeated notices as one toast, the keys of the honesty page, the reconnect after a stay in the git program (close code 1011) and after a close during `state.get`, the interrupt handler around the git program (also on a real pty: `f4`, ctrl+c, still attached), a save the session does not show, a save over a change made meanwhile, the bound on the model's blocks, a refused held draft, the filtered help, an unsupported Textual | the scripted session; one test starts `python -m calu.tui` on a pty |
| `test_startup_imports.py` | what `calu` imports before a command runs, each case in a process of its own: importing `calu.cli`, building the parser, `--version`, `--help`, a subcommand's help and the modules of the client commands (`attach`, the fleet commands, `ide`, `acp`, `doctor`, `start`) import neither Pydantic AI with its provider SDKs nor the Agent SDK, `mcp`, Starlette, uvicorn or Textual; the full-screen client and the web interface import only their own toolkit; the names `calu.checkers`, `calu.context` and `calu.evals` export lazily still resolve; `calu --version` answers faster than a process that only imports what it used to import (Pydantic AI with two provider modules, the Agent SDK; marked `slow`) |
| `test_tui_process.py` | the app over `protocol_connector` against a real `SessionServer`, session and fake runner: a turn, an escalation, a side question, the detail view, a model change; and which client `calu attach` starts | a real WebSocket |
| `test_protocol_side_usage.py` | the requests `side.ask`, `usage.get`, `model.set` and the event `usage.update` through the fake runner | a real WebSocket |

`calu.tui.scenarios` holds the scripted sessions (a turn, an escalation, subagents with a
question, a set of questions, a stopped session, 2,000 steps, a stored history); the tests, the
screenshots and the demo use the same ones.

```bash
uv run python -m calu.tui showcase                 # the client on a scripted session, to look at
uv run python scripts/tui_screenshots.py /tmp/shots   # 32 states at 120x36 and 80x24, SVG and PNG
CALU_UPDATE_TUI_SCREENS=1 uv run pytest tests/unit/test_tui_screens.py   # after a wanted layout change
```

`scripts/tui_screenshots.py` runs the client headless with a clock it controls and writes each
state as SVG; with the headless Chromium of the browser layer (`scripts/get-browser.sh`) it also
renders a PNG beside each. Look at the pictures after a change to `render.py` or `tui.tcss`:
the tests compare texts, not alignment or colour.

### The VSCode extension (`extensions/vscode`)

```bash
cd extensions/vscode && npm ci
npm run check-types && npm run test:unit      # vitest: the pure modules (no VSCode)
npm run build:pane                            # after a change to webview/, src/host/, src/shared/ or pane/
scripts/test-in-docker.sh                     # unit + integration tests in a real VSCode, headless
```

The chat's host logic (`src/host/`) is shared with the chat pane of `calu web` (PLAN.md D261).
`test/unit/chatHost.test.ts` drives it through a scripted bridge. The pane's script is committed
as `src/calu/web/static/pane/pane.js`; `test/unit/pane.test.ts` fails when it is not what the
sources build to, so run `npm run build:pane` after a change to anything the chat uses. The
pane's Python side is tested in `tests/unit/test_web_pane.py` (login, frame policy, the allowed
bridge requests, one bridge over the WebSocket) and in a real browser in
`tests/browser/test_web_pane.py` (its own window, an iframe of another site, no login) and
`tests/browser/test_web_pane_layouts.py`: a scripted session on a token-free local socket (no
database) and the pane at the bottom right of a notebook layout on a 2560x1440 monitor and on
MacBook Pro 16" and 14" displays, as a side bar and in its own window; each runs a turn with an
escalation, the decision, the diff, a file at its line, the session menu, and checks that the
settings open the full web interface in another, logged-in window. `CALU_PANE_SCREENSHOTS=<dir>`
saves a picture of every state; look at them after a change to `media/chat.css`.

`scripts/test-in-docker.sh` builds the image `calu-vscode-test` (`docker/vscode-test.Dockerfile`:
Node, xvfb, Python 3.14 with calu's dependencies), mounts the checkout read-only, installs calu
from it into the container and runs `npm run test:integration` under `xvfb-run`
(`@vscode/test-cli`, `.vscode-test.mjs`). `VSCODE_TEST_VERSION=1.106.0` tests the oldest
supported VSCode. The Docker volumes `calu-vscode-test-cache` and `calu-npm-cache` keep the
VSCode download and npm's cache. On a Linux desktop the same runs without Docker:
`xvfb-run -a npm run test:integration` (or without `xvfb-run` on a display).

What the integration tests use (`test/integration/`): `scripted_session.py` serves a real
`calu.session.Session` with the scripted runner, the rule-based fake checkers and the
in-memory store on 127.0.0.1 and writes its address and a client token to a file;
`scripts/prepare-test-workspace.mjs` makes a git workspace and a `calu` wrapper whose XDG
directories lie below the test directory and whose `CALU_PLACE_FILE` is `/nonexistent`, so
nothing of the tester's own calu (credentials, place file, database) is read. No database,
no Claude Code, no key. The tests (`extension.test.ts`): activation and the commands;
`calu ide` starts and initializes; the chat webview resolves and reports the state it drew;
an attach by address and token (`Sessions.attachEndpoint`); a turn whose escalated step is
allowed, with the card in the webview and "calu: 1 waiting" in the status bar; `step.held`
through the shared reducer; `calu ide` killed, then "Restart bridge" attaches to the same
session again and a second turn goes through; the session's process ended: "The calu server
stopped" with Start server and Retry, the transcript kept and marked ended, a Retry without a
server says when it looked, and an attach to a second scripted session ends the card.

The rest of the extension is tested by hand in the Cua desktop
(`.claude/skills/ui-testing-with-cua/SKILL.md`, the record in
`agent/records/notes/test-plan.md` section A).

## Running a part of the suite, and running it faster (measured 2026-11-18)

The whole suite took 8 minutes in one process. Half of the time was 5 % of the tests: the
full-screen client's tests (Textual's test driver sleeps between key presses), tests that
start a `calu` process (3 s of imports each), and tests that wait out a real timer. Three
changes made then: `tests/conftest.py` shortens Textual's idle step, `pytest-xdist` is a
development dependency, and the test database of `docker-compose.yml` runs without waiting
for the disk (recreate it once: `docker compose up -d caludb --wait`).

| What | Command | Time |
|---|---|---|
| The unit layer, in parallel | `uv run pytest -n 8` | about 30 s |
| A quick loop: no process starts, no waited-out timers | `uv run pytest -n 8 -m 'not slow and not live and not db and not browser'` | about 25 s |
| The full-screen client only | `uv run pytest -n 8 tests/unit/test_tui_*.py` | about 25 s |
| The database layer without the slow ones | `uv run pytest -m 'db and not browser and not slow'` | about 100 s |
| The database layer | `uv run pytest -m 'db and not live and not browser'`, or `scripts/db-tests.sh` (the same; the form for a sandboxed Calu session, DOCUMENTATION.md "Developing Calu inside Calu") | about 2.5 min |
| The browser layer (every test marked `browser`; those of the web interface are also marked `db`, the easter egg's are not) | `uv run pytest -m browser` | about 1 min |
| One area | by path, e.g. `tests/unit/test_web_*.py tests/db/test_web*.py tests/browser`, or `-k 'honesty or trust'` | under a minute |
| Everything before a push | `uv run pytest -n 8 && uv run pytest -m 'db and not live and not browser' && uv run pytest -m browser` | about 4 min |

- The marker `slow` is on the tests that start a `calu` process or wait out a real timer.
  They belong to every full run and to CI; only a quick loop leaves them out.
- **Do not run the database layer with `-n`**: the roles are server-wide and every test's
  setup gives the role `calu_admin` (the user login) a new generated password, so parallel
  tests break each other. The trial with 19 failures was made before D265, when the setup
  renewed the login of `calu_session`. For the same reason two runs against the one test
  server at the same time fail with `password authentication failed for user "calu_admin"`
  or `tuple concurrently updated`. One PostgreSQL server per worker would lift this
  (measured: the whole suite in 104 s with eight workers); not built.
- Not done yet, with what they would save: starting `calu` without importing the checker and
  Agent SDK packages for client commands (about 2.5 s per start, also for users); the
  `calu start` test that waits ten seconds for a server that does not come; the browser test
  that waits for real polling intervals.

## Continuous integration

`.github/workflows/checks.yml` runs on every push to `main` and on every pull request:

| Job | What it runs |
|---|---|
| `checks` | `ruff format --check`, `ruff check`, `pyright`, then `pytest -m 'not live and not browser and not fuzz'` against a `postgres:18` service -- the unit and database layers |
| `browser` | `playwright install --with-deps chromium` (a runner has root, so it needs no `scripts/get-browser.sh`), then `pytest -m browser` |
| `package` | `uv build`, then installs the wheel into a throwaway environment and runs `calu --version`, `calu protocol` and an import of the package data out of `site-packages` -- the check that D41 traded the single binary for |

The live layer does not run in CI: it needs a logged-in `claude` and real model API keys, and it
costs money, so it stays a local and manual check (see below).

## The fuzz layer: random load on the locking component

`calu.locking` (PLAN.md D274; not wired into Calu yet) decides which locks a command or an edit
takes, so that no edit lands in a worktree while a command runs there, and in which order the
calls of one agent get them. Its deterministic tests are part of the unit layer
(`tests/unit/test_locking.py`). The randomized ones are marked `fuzz`: they run only when asked
for, and never in CI (`tests/conftest.py` skips them unless the `-m` expression selects
`fuzz`).

```bash
uv run pytest -m fuzz -s tests/fuzz                                   # about a minute
CALU_FUZZ_SECONDS=120 CALU_FUZZ_SEED=7 uv run pytest -m fuzz -s tests/fuzz
uv run python -m tests.fuzz.locking_harness fuzz --seconds 60 --workers 24 --worktrees 4
uv run python -m tests.fuzz.locking_harness fuzz --scheme mixed-order --deadlock-prone
uv run python -m tests.fuzz.locking_harness bench --repetitions 5
```

`tests/fuzz/locking_harness.py` runs simulated agents as threads. An agent thinks, then issues
a batch of one to three calls, each with a ticket in issue order, and waits for all of them.
Each call runs in a thread of its own:

1. Its check takes a random time, so the checks of a batch finish out of order.
2. A denied call, or one that takes no lock, withdraws its ticket.
3. The other calls ask for their locks and hold them for their run time.
4. Some of them ask again with their own owner while they hold, which must be refused at
   once.
5. They release the locks, some of them from another thread.

Some batches are the owner's example: an edit, a command and an edit in one worktree. The
timeouts are short (0.5 s instead of 600 s), so that waits that only a timeout ends happen
often enough to be seen in seconds; 15 % of the calls pass a timeout below 50 ms on purpose. A
report counts:

| Line | Meaning |
|---|---|
| `issued`, `done`, `timeouts` | Calls per type that were issued, ran, or gave up; the withdrawn calls (denied, no lock); the timeouts that came from a short timeout on purpose, and those that ended a deadlock, separately |
| `running`, `waiting` | How many calls held their locks and how many waited, on average and at the peak, per type (`--timeline FILE` writes them every 5 ms as CSV) |
| `waits` | How long the calls that got their locks waited |
| `deadlocks` | Cycles in the wait-for graph of the backend's atomic snapshots (exact). Stalls: nothing held and nothing was checked while something waited, for at least 0.25 s. A stall during which a timeout happened is a suspected deadlock; this test needs no snapshot, so it works with every backend |
| `reentry probes` | Repeated requests of an owner, through the scheme or at a lock; `refused` is the only right outcome |
| `violations` | Two calls held their locks at the same time although the scheme forbids it; a call got its locks while an earlier call of its agent had not entered the queues, or while an earlier, conflicting one still waited; a reentry that was not refused |

A run of the real scheme must show no deadlock, no suspected deadlock, no violation and no
lock left held. Five schemes that are wrong on purpose check that the fuzzer finds what it
looks for: a mixed lock order and worktrees in reverse order (deadlocks), edits without locks
and commands with shared locks (overlaps), and calls that ignore the issue order. The
comparison runs one scenario of six agents in two worktrees with and without locks, on the
scale 1 ms = 1 s, and prints both average times and the violations that locking prevents.
Measured on 2026-11-25 (WSL2): 1.86 s with locks and 1.28 s without (x1.46); without locks,
about 225 violations per run.

## The database layer

```bash
docker compose up -d caludb --wait
uv run pytest -m db
```

`caludb` listens on 127.0.0.1:55432 (`calu_owner` / `calu-dev`, development only). Each test
creates and drops its own database, so the tests do not interfere and a failed run leaves
nothing behind. `tests/db/` covers the schema and the roles (what the user login `calu_admin`
may and may not do, and that `calu_session` and `calu_operator` no longer log in after
migration 0043), the stores, the admin commands, the herd queries, the web interface and the
live relay.

The databases are per test, but calu's roles (`calu_admin`, `calu_operator`, `calu_session`)
belong to the whole server, and every `setup` gives `calu_admin` a new generated password
(`calu.setup.setup`). Two test runs against the same server at the same time can therefore
take each other's user login away, which shows as a failed login or as `tuple concurrently
updated`. Give a second concurrent run a server of its
own and point `CALU_TEST_DSN` at it:

```bash
docker run -d --rm --name calu-test-2 -e POSTGRES_USER=calu_owner -e POSTGRES_PASSWORD=calu-dev \
    -e POSTGRES_DB=calu -p 127.0.0.1:55440:5432 --tmpfs /var/lib/postgresql postgres:18
CALU_TEST_DSN=postgresql://calu_owner:calu-dev@127.0.0.1:55440/calu uv run pytest -m 'not live'
```

Lost connections are covered without restarting the server: `tests/db/test_connection.py` and
`test_a_session_survives_the_loss_of_both_database_connections` end the session's server
processes with `pg_terminate_backend`, which is what a restart does to them, and check that the
session reconnects, is bound to its row again and loses no step. The same files cover the
login that stops working for good: a test gives `calu_admin` a new password (as `calu setup
--set-password` does) or removes the database, and checks that the reconnecting ends after a
few attempts with an error that names the cause, and that `calu run` then exits with 1. The
row isolation between the sessions of one person ended with D265: every session logs in as
`calu_admin`, which passes every row-level policy (PLAN.md D265 (4)). `tests/db/test_schema.py`
checks instead that the user login runs a session through every statement the store uses.

The end of an unattended run (`tests/db/test_run.py`, `tests/unit/test_run.py`): a prompt with
further messages piped to stdin, and a `--batch` run with a message a client queued, each with
and without narration and with turns that call no tool, must do every turn and then return.
That a session which ends, or whose process is gone, leaves no open escalation is covered in
`tests/db/test_herd.py`.

Code honesty has four unit files, all on real temporary git repositories and without a model:
`tests/unit/test_honesty_state.py` (the state computation: baseline, verified contents,
unreviewed files, marks, certification), `tests/unit/test_honesty.py` (marks, the note, the
gate's decision from a snapshot), `tests/unit/test_honesty_session.py` (a session with the
scripted fake runner: applying marks after the tool result, enforcement, the commit gate,
certification, `honesty.trust`) and `tests/unit/test_honesty_review.py` (the honesty review:
its plan, the reviewer agent with fake models, the triggers, one review at a time, the cap, a
failed review). The fake reviewer is `calu.testing.fake_reviewer`. `tests/db/test_trust.py` and
`tests/db/test_honesty_session.py` check the grants and functions of the two trust tables and
that a session's own writes pass them with the user login.

## The browser layer

```bash
scripts/get-browser.sh              # once; needs no root
uv run pytest -m browser
```

`get-browser.sh` installs Playwright's headless Chromium and unpacks `libnss3` and `libnspr4`
into `~/.cache/calu-browser-libs` — Ubuntu does not ship them and `playwright install
--with-deps` would want root. The tests put that directory on `LD_LIBRARY_PATH` themselves and
**skip with a pointer to the script** when the browser is missing, so nobody is forced to have
it. They drive the real pages: the fleet, a session's four levels of detail, minting a token,
answering an escalation, editing a checker, the triage queue, the live view over a real session,
and the jump palette. `tests/browser/test_web_login.py` runs the application guarded as
`calu web` guards it (the token, the host name `calu.localhost`, the Content-Security-Policy)
and checks what only a browser can show: which server the browser sends the login cookie to,
that a captured cookie changes nothing, and that every page still works under the policy.

## The root layer

Every other layer lets the tests' own uid stand in for root (`owner=` parameters, a patched
`os.geteuid`). `tests/root/` runs calu's privileged paths as real root: the launcher starting
a program as another uid with an environment built for it, the file identity after
`calu.asuser.enter` (files created belong to the user, a root-only file is refused, `as_root`
opens it), the root-owned socket and another user's attempt to connect, the place file read
by another user and refused when it is not root's, the modes of the log file and of a store
root wrote, the refusals (`calu run` as root without `--user`, `--from-sudo` for another uid
than sudo's), and whole sessions through `uitest/rootbox/fakecalu.py` (calu's command line
with a scripted runner): as root for uid 1000, whose files in the project then belong to
1000, and with `--user root`, which must bind no token-free socket;
`asuser.prepare` on existing directories and its refusal of a symbolic link in their place;
the launcher's refusal of a uid, gid or group the system calls would leave unchanged;
`calu setup` writing `/var/lib/calu/database.json` (the tests keep and restore a file
that was there), with and without `--no-password`, and the sandbox-off advice that follows
it in setup and in a `--user` session, and `calu setup --set-password` taking the password
out of the file again; `calu serve --user` starting Claude Code (a stand-in that answers the
Agent SDK's start-up requests) as that uid behind a root socket; a client as root that asks
the calu user password once (it opens root's `user` entry, or the database accepts it for
`calu_admin` where the place file says), keeps the user login in memory, switches to the
user's file identity, and then lists sessions and saves settings without a password while
root's store is closed to it and neither its environment nor a program it starts holds the
password (D249, D265).

The tests are skipped unless the process is root **and** `CALU_ROOT_TESTS=1` is set: they
create directories below `/var/lib/calu` and `/run/calu` and start programs as other uids,
which a test run must not do to a developer's machine. They run in the root box, a container
(`uitest/README.md`, "The root box"):

```bash
calu/uitest/rootbox/run.sh                    # once: build, start, install calu inside
docker exec calu-rootbox rootbox-tests        # 25 tests, a PostgreSQL cluster of their own (port 5434)
docker exec calu-rootbox rootbox-tests -k socket -x
```

`rootbox-tests` syncs a virtual environment in `/opt/calu-venv` from the read-only checkout
and runs `pytest -m root tests/root`. CI does not run this layer. Run on 2026-11-19: 24 passed
(after the serve test was given its projects directory, D243). Not yet run with the one user
login (D265): the tests were rewritten for it without a root box.

## The live layer

Marked `live`: tests that need a logged-in `claude` or a model API key.

```bash
mkdir -p ~/.config/calu
printf 'ANTHROPIC_API_KEY=sk-ant-...\n' >> ~/.config/calu/.env   # or MISTRAL_API_KEY, ...
chmod 600 ~/.config/calu/.env
uv run pytest -m live
```

The checker model for live tests is Nemotron Lightning on Fireworks;
`CALU_TEST_CHECKER_MODEL` overrides it. Claude Code authenticates as it is set up on the
machine, so a `claude` that works in a terminal works here. If Mistral is used: small models
only.

`tests/unit/test_runner_live.py` needs no key, only the logged-in `claude`, `bwrap` and `socat`:
it sends a prompt, an injected and a queued message through the real runner and checks that
exactly these three are recorded as the user's input, while calu's own note and a command's
output are not (`spikes/RESULTS.md`, "Whose words a user-role message is"). Run it after every
update of the CLI or of `claude-agent-sdk`; it takes about half a minute on haiku.

44. The answers of the final acceptance round (PLAN D248), with a real `claude`, ideally on a
   claude.ai subscription. In the full-screen client click the `ctx` cell of the status line:
   the usage panel shows "not reported yet by Claude Code" or the 5-hour and weekly limits
   with "reported ... ago", "YOUR PLAN NOW" for a subscription, and Claude Code's `/usage`
   text; the chat log on the session page of `calu web` and the transcript gain no turn, and
   the checkers' context (`/checker-context`) does not mention it. `/usage` in the line client and a click on the
   web's usage line show the same. Type `/context` and `/compact` in the full-screen client:
   Claude Code answers them as in VSCode. In the web live view ask a side question while a
   turn runs, then a follow-up that refers to it: the answers stay on that page (a second
   browser sees nothing), the turn goes on. Store an older shipped checker text unchanged
   (`calu checker set edit --instructions-file src/calu/defaults/checkers/legacy/v2/edit.md`):
   `calu doctor --db`, `calu checker show edit` and the web's settings pane call it older,
   `calu checker update edit` and "Use the present version" replace it, `calu setup` asks; an
   edited text is only reported, `calu checker diff` shows it. `/running` and the web's
   session list show `~/...`, the tooltip the full path.

## The spikes: what Claude Code actually does

calu's design rests on measured behaviour, not on documentation. Every assumption about Claude
Code has a spike, and `spikes/RESULTS.md` records what each one found, with the CLI version. Run
them by hand; they use throwaway directories and databases.

| Spike | Question it answers | Re-run when |
|---|---|---|
| `spike_q1a_cc_sandbox.sh`, `spike_q1a_precedence.sh` | Does Claude Code's own sandbox isolate Bash, and can project settings switch it off? (D15) | the CLI changes |
| `spike_settings_widen.sh` | Do `sandbox.filesystem.allowRead`, `sandbox.network.allowedDomains` or `sandbox.excludedCommands` of the project's settings files take effect next to the sandbox document calu passes with `--settings`, and does the CLI accept that complete document? (measured 2026-11-12, CLI 2.1.319: no effect, accepted; alone, the same entries do work) | **every CLI update**. calu does not rest on it: with `claude.sandbox.fail_if_unavailable` a session does not start, and a Bash step waits for a human, while a settings file holds such an entry (`calu.preflight.settings_file_sandbox_blockers`) |
| `spike_q1b_shell_prefix.sh` | Can `CLAUDE_CODE_SHELL_PREFIX` wrap every command? (the fallback) | the CLI changes |
| `spike_q7_structured_check.sh` | Can `claude -p --json-schema` serve as a checker model? (D16) | considering that model type |
| `spike_hook_timeout.py` | Does a hook that misses its timeout let the call run? (D23) | **every CLI update: the whole gate rests on it** |
| `side_question.py` | Does a headless Claude Code answer a side question (`/btw`) while a turn runs, and stay out of the conversation? Does `get_context_usage` answer in a running turn? (`side.ask`, `usage.get`) | the CLI or the SDK changes |
| `spike_usage_control.py` | Does a headless Claude Code answer `get_session_cost` (the `/usage` text) and `get_usage` (the plan's limits) without a turn and without a model call? (`usage.panel`, D248) | the CLI or the SDK changes |
| `spike_question_tool.py` | Does `AskUserQuestion` reach the hook, and can calu answer it? (D35) | the CLI changes |
| `spike_fork_session.py` | Can a conversation be forked at an earlier turn? (D36) | the SDK changes |
| `spike_auto_mode_ask.py` | Does auto mode ask rather than only allow or deny? (D38) | the CLI changes |
| `spike_dev_channel.py` | Does `--dangerously-load-development-channels` have an effect in the headless mode calu uses? (no: ignored) | the CLI changes |
| `spike_session_id.py` | Does Claude Code take the session id calu chooses, for a new session and for a fork of a whole conversation? (D203) | the CLI or the SDK changes |
| `spike_other_uid.sh` (runs `spike_other_uid.py` as root in a container) | Do the hooks, the permission callback, the side question and a resume work when calu is root and Claude Code another uid, and which uid, gid and groups does a command get? (D206) | the CLI or the SDK changes |
| `spike_calu_fork.py` | Does `calu run --fork-before-turn` branch a conversation calu recorded? | the fork code changes |
| `spike_rewind_files.py` | What does `rewind_files` put back -- tool edits only, or a command's writes too? (D46) | the SDK or the CLI changes what checkpointing tracks |
| `spike_session_rls.py` | Would row-level security keyed on a per-connection session id separate two sessions? (Q3) | the schema gains a table a session writes |
| `scripts/build-binary.sh` | Does a frozen one-file calu still find its package data, and what does it cost? (Q15) | a dependency is added or pydantic-ai is updated (the release workflow runs it on every tag) |
| `smoke_session.py` | A whole calu session against the real `claude` | before a release, and after changes to the runner |
| `smoke_privileged.sh` | calu as root with `--user 1000` and the real `claude` in the development image: who a command, git's filter and calu's files end up as (D211) | after changes to `calu.asuser`, `calu.launch` or to where calu starts a program |
| `spike_root_socket.py` | After the switch to the user's file identity: is the session's socket root's alone, closed to the process's own access, and is a root peer reported as uid 0? (D215) | after changes to `calu.localsocket` or `asuser.as_root` |
| `spike_fs_identity.py` | Is the file-system identity of a root process per thread, and inherited by new threads? (yes; D211 rests on it) | a kernel or C library that behaves differently is suspected |
| `acp_host.py` | Does `calu acp` hold a whole conversation with the official ACP client library, which checks the adapter's messages against the protocol's schema? (D221) | after changes to `calu.acp`, or a new version of the protocol |
| `spike_chat_image.py` | Does Claude Code take an image in a chat message, and how is the message replayed? (D220) | the CLI or the SDK changes |
| `spike_permission_mode_switch.py` | Which modes does a running Claude Code take through `set_permission_mode`, how is the change reported, does it apply inside a turn, and does a hook "ask" still reach `can_use_tool` in acceptEdits? (D247) | the CLI or the SDK changes, or calu offers another mode |
| `smoke_serve.py` | `calu serve` with the real `claude`: two sessions at once in two directories, and a fork started through the server (D208) | after changes to `calu.serve` or to how a session is started |

```bash
docker compose up -d caludb --wait
bash spikes/get-socat.sh                      # the sandbox needs socat; no root required
rm -rf /tmp/calu-smoke && mkdir /tmp/calu-smoke
PATH=/tmp/calu-spike-tools/bin:$PATH uv run python spikes/smoke_session.py /tmp/calu-smoke \
    --sandbox -p "Run exactly: echo hello"
```

`smoke_session.py` creates a throwaway database, runs one real session in it, prints what the
database holds afterwards and drops it again. Its flags: `--sandbox` (on this machine the
sandbox needs socat from `get-socat.sh`), `--changes` (measure and review what a command wrote,
D31), `--socket PATH` (serve the clients on a Unix socket), `--resolve allow|deny` (play the
operator for the first escalation), `--model`, `--checker-model MODEL|auto` (real checkers
instead of the rule-based fakes), `--honesty track|enforce`, `--trust-head` (certify HEAD of
the project directory with the source `user` before the session starts),
`--reviewer-model`, `--resume ID` and `--fork ID` (continue a Claude conversation, or a copy
of it, by the id an earlier run printed as `[session] ID`) and `--stay` (keep the session after the first turn and read further
messages from stdin: the `[clients]` lines name the address and the token for `calu attach`,
which is how the full-screen client was tried against a real session; a client that is still
attached keeps the session alive after Ctrl-D).

Code honesty against a real model (run on 2026-11-12 with haiku as Claude and Nemotron
Lightning as checker and reviewer; all four ended as described). The project directory is a
throwaway git repository with one commit: a small module whose functions have docstrings, and
its tests. `--honesty` needs `--checker-model`, because only a real command checker says
whether a command executes project code.

| Flags | Prompt | Expected |
|---|---|---|
| `--honesty track --trust-head` | add a function and a test with the Edit tool, run the tests, `git add -A`, `git commit -m ...` | nothing is blocked; `honesty: commit ... certified as trusted (source check)` |
| `--honesty enforce --trust-head` | reword a docstring with `sed -i` so that it still describes the function, run the tests, commit | `honesty: review started`, `review finished: 1 file(s) verified, 0 finding(s)`, the tests run, the commit is certified with the source `review` |
| `--honesty enforce --trust-head` | change a function's body with `sed -i` so that it contradicts its docstring, run the tests | the review records one mark that names the function, and the test command is denied with that mark |
| `--honesty enforce --resolve deny` | run the tests | an escalation: no trusted commit, certify one with `/trust` |

The report at the end lists `trusted_commits` and `trusted_files` with their sources.

**Without `--sandbox` the commands Claude runs are not isolated**, so always point it at a
throwaway directory.

## The manual checklist

Before a release, or after changing the runner, the gate or the sandbox. `agent/records/HANDOFF.md` records
what was run and what came out of it, under "Verified by hand with the real CLI"; on 2026-11-10
items 1, 2, 5, 6, 7, 9, 10, 12 and 14 were exercised, and item 4 as part of the network
measurement in `spikes/RESULTS.md`; item 11 was done as an automated test instead
(`tests/db/test_run.py`), and item 13 needs an editor, which this machine has none of. Items 1,
2 and 6 were run again at the end of that day, after a dozen changes to the gate, the register
and the clients -- an allow-listed command and an edit through a real `claude`, and a deny-rule
escalation resolved from the database alone.

1. `calu doctor --db` on the machine — nothing essential missing.
2. `scripts/wsl-test.sh -m 'not live'` — clean, and `uv run pytest -m browser`.
3. `spikes/spike_hook_timeout.py` — the gate fails closed.
4. `smoke_session.py --sandbox` with a prompt that runs a command, and read the probe output of
   `spikes/probe.sh` from inside the sandbox (own PID namespace, no `claude` process,
   credentials unreadable, network blocked, no leftover process).
5. `smoke_session.py --changes` with a command that writes a file whose content carries the
   fake checker's deny marker: the change must be reverted and Claude must be told.
6. A deny-list escalation resolved from the database alone: `calu escalations`,
   `calu escalation <id>`, `calu resolve <id> deny --note ...` — the session continues within a
   second.
7. `calu web`: open the printed address (`http://calu.localhost:8800/?token=...`; a token is
   printed only with an empty user password, otherwise the browser asks for the calu user
   password, item 32); the address without the token must answer 401 in a private window,
   `http://127.0.0.1:8800/` must answer 403 and name the right address, and a `curl -X
   POST` must not decide anything -- neither without a cookie nor with the
   `calu_web_session` cookie copied from the browser (D161).
   Restart the database while the triage page is open: the page says that its list may be out
   of date and recovers by itself, without a restart of `calu web`. Then the
   fleet, a session at all four levels of detail, the triage queue, Ctrl+K. On the settings
   page (D182): open a checker pane and a rules pane, search for "effort", change a value on two
   panes and save once, type a broken regular expression into a rule list and check that the
   error is shown at the list with the text still there, switch the scope to a project and
   check that the global value is shown as inherited; repeat the first steps at phone width.
8. The session mode from a client (D42): with a session running, set `auto_escalate.edits` on
   the settings page and watch an attached `calu attach` report it; then the session page must
   list the change with who asked for it (D43). With an ACP editor, the same through its own
   agent-mode picker.
9. Removed with D265: session isolation (D49); every session logs in as `calu_admin`, which
   passes every row-level policy, and `tests/db/test_schema.py` checks that this login runs a
   session through every statement the store uses.
10. In a container: `docker compose run --rm dev sh -c 'command -v bwrap socat'` and a real
   `bwrap --unshare-user ...` — the `dev` service needs `security_opt: [seccomp:unconfined]`,
   because Docker's default seccomp profile blocks user namespaces.
11. A conversation resumed twice (D65): `calu run --resume <claude session id>` twice over, with
   `context.fold_every` low enough that the first session folds a turn. The checkers must still
   be told the earlier conversation, which `calu summary <session>` shows and the session page
   repeats; an empty summary there is the defect D65 fixed. `tests/db/test_run.py` covers the
   same path with a fake Claude, so this is only worth running by hand after a change to the
   store or the context builder.

12. A client with no arguments (D105), which is how a session is attached to in practice: with
   a session running in a project, `cd` into a **subdirectory** of it and run `calu attach`
   with nothing else. It must find the session, stream it, and leave one revoked row behind in
   `calu token list` when it ends. Then `calu attach --session <wrong id>` must name what it
   could not find, and `calu attach` in a directory with no session must say so and point at
   `calu sessions`. Verified by hand on 2026-11-10 against a throwaway database (the mint and
   the revoke are also a database test).
13. The ACP adapter in a **real editor** (D105, D106). The configuration itself is covered by
   `tests/db/test_acp_discovery.py`, which spawns `python -m calu acp` with no arguments in a
   project directory and speaks ACP to its pipes, both with and without a password on the
   credential; what is left for a hand check is what an editor makes of it -- that it shows the
   refusal instead of hanging, and that its permission dialog, its agent-mode picker and its
   diff view behave.
14. The empty user password (D104, D265): after `calu setup --no-password` (or `calu
   credential set user --no-password` for a database another machine set up), `calu sessions`
   and `calu escalations` must run without a prompt, `calu credential list` must mark `user`
   and `db-admin` "file mode only", and `calu doctor` must report them on its "credential
   protection" line as stored without a password, with `calu setup --set-password` as the way
   back. `calu run` must start; with the sandbox off its start-up warnings must end with
   `Found: stored without a password: db-admin, user`. Not yet run with the one user login.
15. Revoking a token that is in use: attach with the address and the printed token
   (`calu attach ws://... --token=...`), then `calu token revoke <id>` for that row. Within
   about fifteen seconds, or at the next thing typed, the attached terminal must print
   `[detached] the session closed the connection: the client token was revoked`, and attaching
   again with that token must be refused. `tests/db/test_protocol_tokens.py` covers the same
   path with a fake Claude.
16. An editor opened on a session that already waits: let a session stop on an escalation with
   no client attached, then open the ACP editor on the project. Its permission dialog for the
   waiting step must appear right after the session is created, and answering it must let the
   session continue. Then end the `calu run` process while a prompt is open in the editor: the
   prompt must end with a line that says the connection ended, and the agent process must
   exit, so that the editor offers to restart it.
17. A database restart under a running session: start `calu run --batch -p "..."` with a prompt
   that takes a few minutes, then `docker compose restart caludb`. The session must print that
   the database connection is lost and, once the server answers, that it is back; its steps
   wait meanwhile and none is missing on the session page of `calu web` afterwards. `calu sessions`
   must not show it as SILENT once it is back. The automated tests end the connections with
   `pg_terminate_backend`; only this check has the server really away for a while.
18. The project of a directory: `calu project add . --name demo`, then `calu run` in that
   directory and in a subdirectory must print `[project] demo (registered for this directory)`;
   `calu run --project demoo` must refuse to start and list the registered projects; `calu run`
   in an unregistered directory must print `[project] none`.
19. A sandbox entry in a settings file: put `{"sandbox": {"excludedCommands": ["make"]}}` into
   the project's `.claude/settings.local.json` and start `calu run` there. The start must be
   refused with a message that names the file and the entry. Remove the file, start a session
   with a prompt that runs a slow command and then a second one, and put the file back while
   the first command runs: the second command must wait as an escalation that names the file.
   `calu doctor <directory>` must name the entry too.
20. The permission mode: `calu run` with `claude.permission_mode` unset on a machine whose
   Claude Code settings do not set `permissions.defaultMode` must report the mode "auto" when
   the session starts, with no start-up warning about the mode. With `calu config set
   claude.permission_mode default` the session must report the mode "default" as information,
   again without a start-up warning. `calu doctor` must show a `permission mode` line marked
   `ok` in both cases, naming the mode and where it comes from.
21. Resetting the database: on a scratch database with a project and a rule, run
   `calu db reset`. It must list what it would delete, ask for the calu user password (unless
   it is empty) and for the phrase `yes delete the db`. Paste the phrase (it arrives at once):
   nothing may be deleted. Run it again and type the phrase: `calu project list` is empty
   afterwards and `calu run` still starts with the stored user login. Not yet run with the
   one user login.
22. The full-screen client in a real terminal (`calu attach` beside a `calu run`): in tmux and
   over ssh without `COLORTERM` (the verdict words and the state must stay readable in 256
   colours); at 80x24 (`f2` opens the side panel over the transcript); scroll up with PageUp
   and the mouse wheel while Claude writes — the visible lines must not move, `End` returns;
   `alt+←/→` through the subagents of a session that started two; an escalation answered
   with `a a` and one with `d d` after a note (`n n`); `w w` on an escalation, and back with
   `w`; type "no, use a branch" and "why?" while a step waits (both are typed whole and sent as
   messages, nothing is decided); a step with a command of ten lines (the card counts what it
   cuts, `a` opens the detail, `a a` there allows); hold `a` down on an empty input (nothing is
   decided); with two steps waiting, press `a`, decide the first one from `calu web`, press `a`
   again (the second step must still wait); drag the window from 120 to 80 columns and back
   (the side panel goes and comes with the first resize);
   `/btw <question>` while a command runs — the answer appears in the side panel, the turn goes
   on, and the next turn does not know the question; `alt+c` copies (OSC 52: needs
   `set -g set-clipboard on` in tmux); kill and restart `calu run` while attached (the status
   bar says why the connection ended; `ctrl+d` leaves); two clients on one escalation (the
   other one reports "decided elsewhere"); `calu attach --plain` and `calu attach | cat` give
   the line client.
23. The second part of the full-screen client, in a real terminal: attach to a session that has
   run a while and was restarted with `calu run --resume` (the stored history stands above a
   line that says so, then the live events); `/stop`, then Enter on the start card; `/effort
   max` on a running session (the confirmation lists what the stop ends; `n` keeps the session,
   `y` resumes the conversation with the new effort); `/sessions` and a second conversation;
   `ctrl+up`, the cursor on an edit, `D` (side by side from 120 columns, `s` switches), `v`,
   `j`, Enter, a comment — Claude must receive it with the selected lines; `ctrl+f` and a
   word (the matches are marked, Enter goes to the older one, Esc ends); `alt+h` while Claude
   works (the draft is listed under HELD and goes out when the turn ends), `f2`, `d` on a
   draft; paste 50 lines (one marker; the message arrives in full); `@` and Tab on a project
   path; `/export`; `/theme calu-light`, detach, attach again (the theme is kept in
   `~/.config/calu/tui.toml`, the sent texts come back with the up arrow); `NO_COLOR=1 calu
   attach`; switch to another window while a turn runs (the bell rings at its end if the
   terminal reports focus changes).

24. Checkers, honesty and the git program in the full-screen client, in a real terminal on the
   session's machine: `/checkers`, `j`, `e`, change a line, `ctrl+s` (with a calu user
   password the client asks for it; the message names the new version, and `calu checker
   list` shows the same version); the same on a machine without the user login (the screen
   says "read-only here" and names `calu checker edit`); `/honesty` (mode, marks;
   baseline and unreviewed files once the session reports them); `/honesty enforce`, then
   `/honesty track` (a question, Enter keeps `enforce`); `/trust` (the box names the short
   id and subject of HEAD; Enter certifies nothing); `t`, `r`, `x`, `m` on the honesty page;
   with `calu attach ws://other-host ...` and a local user login for another database, save a
   checker and `/honesty track` (both must say that the attached session does not show the
   change); `f4` with gitui installed (gitui opens
   in the project directory, `q` returns to the client); stay in it for more than a minute (the
   status bar shows `reconnecting` and then `attached`, and what happened meanwhile is in the
   transcript); `CALU_GIT_TOOL='git log' calu attach`, `f4`, ctrl+c in the pager (the client
   is still there); `CALU_GIT_TOOL=tig calu attach`; `f4` without gitui (the
   client offers to install it, shows the command for this machine and runs nothing that
   needs `sudo`). Not yet run with the one user login.
25. Code honesty: in a git repository, `calu config set honesty.mode enforce`, then `calu run`
   with a prompt that edits a source file and runs the project's tests. Without a trusted
   commit the test command must wait as an escalation that names `/trust`. In `calu attach`,
   `/trust` must print the certified commit and a `code honesty (enforce): clean` line; the
   same test command must then run. Change a file with a shell command (`sed -i`) and run the
   tests again: the step must wait as an escalation that names the unreviewed file. `/honesty`
   must print the baseline, the marks and the unreviewed files; `calu honesty trusted` must list
   the certified commit with its source. A `git commit` made in a clean state must be reported
   as `certified as trusted`. The honesty panel of the live page of `calu web` must show the
   same state, and its "Trust HEAD" button must ask before it certifies.
   The honesty review (needs a key for the model of `honesty.reviewer_model` or the default
   model): change a docstring with `sed -i` so that it still describes the code, and let
   Claude run the tests: the terminal must print `honesty: review started`, a `batch ... answered`
   line and `review finished`, `/honesty` must say `a review is running` in between, and the
   test command must then run without an escalation. Change a docstring so that it contradicts
   its function and run the tests again: the command must be denied with the finding of the
   review. `/honesty-review` in `calu attach` must print `review requested` at once and the
   answer when the review ended, and further lines must be accepted meanwhile. In a
   repository without a trusted commit, `/honesty-review` must end with `certified as trusted
   after the review` when every file was judged honest and the working tree has no changes.
   The honesty checker's answer per file (with the honesty checker enabled, an edit that
   renames a function without its docstring): `calu attach --plain` must print a
   `honesty: <file> DISHONEST: <reason>` line under the checker's line, the full-screen client
   a `honesty` line under the step, the live page a line tagged `honesty`, and the VSCode chat a
   `dishonest` chip on the step's row; none of them may show the honesty answer as PASS or
   ALLOW. An edit that keeps the file honest must show `honest` only in the step's details
   (full-screen client, VSCode) or as a plain line (line client, live page).
26. The database setup (D205), on a machine without calu credentials (or with
   `XDG_CONFIG_HOME` pointing at an empty directory). **Docker:** `calu setup`, answer 1, a
   data directory of your choice: the directory must be private (0700) with the cluster below
   `pg/`; `calu db down`, then `calu run -p hi` must print `started the local database` after
   the calu user password and run; `calu setup` again must ask the user password once and
   report `user login (calu_admin): its password is the calu user password`. The Docker
   variant was run on 2026-11-13 with the logins of before D265, under a compose project of
   its own (setup 3.7 s, on-demand start 3.8 s); not run: rootless Docker. **Existing
   server:** `calu setup --database server` with a user that may not create roles must stop
   before the user password is asked, list what is missing with the SQL, and store nothing;
   with a prepared user (the SQL of the guide) it must ask the new user password twice after
   a two-line explanation and end with `stored the logins 'user' and 'db-admin'`. **RDS with
   IAM:** never run against a real instance. With one: prepare the roles and the IAM policy as
   the guide says, `calu setup --database rds-iam ...` (the owner logs in with IAM, the user
   login with the user password, D265), then `calu run`; let the session run longer than 15
   minutes, end its connections on the server (`pg_terminate_backend`) and check that it
   reconnects. **The
   existing-server variant was run in the root box on 2026-11-18** with the logins of before
   D265; not yet run with the one user login. To run there (PostgreSQL 16; Docker is not
   available), as a user and as root: a first setup with the passwords from standard input
   and on a terminal; a wrong owner password (nothing stored); Ctrl-D at the new user
   password ("Not done: no password was entered", nothing stored); two different answers at
   the new password ("Not changed: the two passwords differ"); a second run (the user
   password asked once); a wrong user password at the second run (tries left, then the
   pointer to "A forgotten user password"); `--set-password` (a running server keeps its
   connections and is refused at the next reconnect, in a message that names the cause);
   Enter at the new password (an empty user password: no command asks afterwards, and the
   report and the warning say who can use the login); a database that is down (the
   server's refusal in one message); an older schema (migration 0036 taken out: `calu
   sessions` and `calu ide` list without names, `calu doctor --db` says that one migration is
   not applied, `calu setup` applies it); a store of before D265 (`calu setup` asks the earlier admin password once,
   offers to keep it as the user password, and removes `admin`, `session`, `operator` and
   `calu_admin`; another command on that store names `calu setup`).
27. `calu serve` (D208). In one terminal `calu serve` in the directory that holds your
   projects: it asks the calu user password once (nothing with an empty one) and prints that
   it answers requests in that directory. In two project directories below it `calu start -p
   hi`: each must attach to a session of its own without a password, and the server prints one
   "requested", "started" and (after `/shutdown` in the client) "ended" line per session,
   with the directory's name in front. `calu start --fork <id>` of one of them must know the
   original conversation. `/quit` in a client must leave the session running (`calu attach`
   finds it again). Ctrl-C in the server must stop every session; `calu sessions` then lists
   none as running. With the server stopped, `calu start` must say after about ten seconds
   that no `calu serve` answered. Not yet run with the one user login; the runs below used the
   logins of before D265. Run on 2026-11-13 with `spikes/smoke_serve.py` (two
   sessions at once and a fork, real Claude Code, fake checkers); not run by hand: the two
   commands on a real terminal, and Ctrl-C with sessions in the middle of a turn. A test runs
   `calu serve` as a process with the password on its standard input, a refused request and
   SIGTERM (`tests/db/test_serve.py`). **Run in the root box on 2026-11-18** with scripted
   sessions (`fakecalu serve`), as one user, as root with `--user`, and through sudo: the
   "requested", "started" and "ended" lines, two sessions at once, Ctrl-C with one session
   in the middle of a turn and one waiting at an escalation (both rows "stopped", the
   escalation "cancelled", the attached client told that the session closed),
   `--max-sessions` (the request beyond it is refused with the reason), `--idle-timeout 1`
   (ended after 60 s without a client), a request for a directory that does not exist, and a
   server killed after it claimed a request (`calu start` says after 180 s that the server
   did not answer; the row is "failed"). Not run: `--fork`.
29. calu as root (D211). In a container as root with a project directory mounted from a
   host user: `calu run` without `--user` must refuse and say how to name the user; `calu
   run --user 1000 -p "run: id > who.txt"` must write `uid=1000 gid=1000` into a file owned
   by 1000, and `/var/lib/calu/claude/1000` and `/var/lib/calu/state/1000` must belong to
   1000 with mode 0700. Run on 2026-11-13 with `spikes/smoke_privileged.sh` (real Claude
   Code, fake checkers, snapshots on, the sandbox off); not run: a named user with its own
   home, the sandbox on, `calu serve --user`. **Run in the root box on 2026-11-18**, with a
   stand-in `claude` that records who started it, with the Claude Code the Agent SDK bundles
   (it ran as the uid and answered "Not logged in") and with scripted sessions: `--user ada`
   (uid 1000, gid 1000, groups 1000 and 27, home `/home/ada`), `--user 1000` and `1000:1000`
   (no supplementary group, home `/var/lib/calu/claude/1000`), `--user 4242:4243` (a uid
   without a name), `--user root` (one user; no token-free socket since that day), `calu
   serve --user` in each form, and `tests/root/`. The environment the program got: `PATH`,
   `LANG`, `TERM`, `HOME`, `USER` and `LOGNAME` (a named user only) and what the Agent SDK
   sets; no key, nothing of sudo's, nothing of calu's. Not possible there: the sandbox on (an
   unprivileged container may not create user namespaces; `calu doctor` reports it).
30. `--sudo` (D212), on a machine where you have sudo. With calu installed in your own home:
   `calu run --sudo` must refuse and print the install command for root. After that
   install and `sudo calu setup`: `calu run --sudo -p "run: id > who.txt"` must ask the sudo
   password, then the calu user password (nothing after `sudo calu setup --no-password`), and
   `who.txt` must name you and belong to you;
   `sudo ls /etc/calu` must show the credential store, and your home must hold nothing new
   of calu's. Not yet run with the one user login; the runs below used the logins of before
   D265. Run in a container as root on 2026-11-13: the install command (calu and
   `calu-claude` land in `/usr/local/lib/calu/calu/bin`, linked from `/usr/local/bin`), the
   installation check on it (passes), and the refusal of `calu run` without `--user`. **Run
   through a real sudo in the root box on 2026-11-18**, as a user whose sudo asks nothing
   and as one whose sudo asks its password: the refusal from an installation in the home
   directory (the install command was cut off the message; corrected), `calu serve --sudo`
   (sudo's prompt, then the session password of that time, then the server answers that
   user's requests; the process is root with the user's file access), what sudo passes on
   (`HOME`, `LANG`, `PATH`, `TERM`, `USER`, `SUDO_*`: none of the user's `XDG_*`, `PG*`,
   `PYTHONPATH` or provider keys), the note
   about provider keys, and that the user's home gets no file of calu's (the log file used to
   be created there before the elevation; corrected). `calu doctor --sudo` reports the same
   arrangement without starting anything.
31. The client as root (D214), with a session started by `calu run --sudo`: `calu attach
   --sudo` must ask the sudo password (unless sudo remembers it) and the calu user password
   once ("calu user password (asked once, for /running, /switch, /new, /fork and saving
   settings; Ctrl-D skips them)"); editing and saving a checker's instructions (`/checkers`,
   `e`, ctrl+s) must then ask nothing; `/running` must list the sessions, `/switch <id>`
   must attach to another one without a password, and `f4` and the editor must run as you (a
   file they create belongs to you). As yourself, without `--sudo`: saving must ask for the
   calu user password and accept it (the database checks it for `calu_admin` at the place
   root's place file names; D213, D265). `calu run --sudo` must print
   a line `[clients] root clients, without a token: unix:/run/calu/sessions/<id>.sock`, and
   `sudo ls -l /run/calu/sessions` must show that socket as `srw------- root root`; `calu
   token list` must show no new token for the `--sudo` client (D215). Not yet run with the
   one user login; the run below used the logins of before D265 (its "admin password" is the
   user password now). **Run in the root box on 2026-11-18 with the line client**
   (`--plain`): the socket and its directory are as stated and another user cannot enter the
   directory; `calu attach --sudo` in the project
   asks only sudo's password, mints no token, and its decisions are recorded as
   `client:root:calu attach`; `calu start --sudo` asks the admin password once; as yourself
   `calu config set` asks the admin password once and a wrong one is said in one sentence.
   Not run: the full-screen client's `/checkers`, `/running`, `/switch`, `f4` as root
   (test plan, section C). Since D249 the client found through a root socket asks the calu
   user password once before it starts (Ctrl-D skips it and the client says the database
   commands are not available; three wrong answers say the same with the pointer to "A
   forgotten user password"); `/running`, `/switch`, `/new`, `/fork`, `/rename`, saving
   instructions and `/honesty` must then show no password dialog in that run, also after a
   `/switch`. After `sudo calu setup --no-password` nothing is asked at all (the place file
   publishes the user login).
   The root box test `tests/root/test_root_client.py` runs the part without the screen.
32. The web interface's login (D216, D265), after `sudo calu setup` with a user password:
   `calu web` must print the address without a token; the browser must show a page asking
   for the calu user password, say "That is not the calu user password." with the pointer to
   "A forgotten user password" after a wrong one (and answer the next attempt later), and
   show the fleet after the right one; saving a setting must then work. `calu web --sudo`
   must do the same with the process running as root (`ps -o user= -C calu`). After a setup
   with an empty user password the address must carry `?token=`. Not yet run with the one
   user login; the runs below used the logins of before D265. Run on 2026-11-13 only as a
   test of the gate against the database; **not in a browser**. Run in the root box on
   2026-11-18 with curl (`webcheck`): `calu web` and `calu web --sudo` ask the admin password
   of that time once in the terminal and print the address without a token; the front
   page answers 401 with the sign-in form, a wrong password 401 after a second, the right
   one opens the fleet; the `--sudo` process is root. After `calu setup --no-password` both
   print the address with `?token=`. A setup that gives `calu_admin` a new password while
   `calu web` runs: the pages keep working until its database connection is lost, then
   answer 503 "The database cannot be reached"; the terminal names the refused login, not
   that `calu web` has to be started again.
33. Status-only terminal and the log file (D225): `calu run -p "say hi" --batch` must print
   the project, the session id, the client lines and `[stopped]` at the end, but neither
   "say hi", Claude's answer, `[idle]` nor `[turn ended ...]`; `calu attach` must show both. A failure (e.g. a
   stopped database during the run) must be one line on stderr that names
   `~/.local/state/calu/log/run.log`, and that file must hold the traceback. Ctrl-C must print
   `calu: interrupted`. **Run in the root box on 2026-11-18**, as a user and as root
   (`/var/lib/calu/log/run.log` and `serve.log`, 0600 in a 0700 directory, root's): the
   terminal of a scripted run shows the project, the ids, the client lines and `[stopped]`
   and nothing of the conversation; a Claude Code that does not answer ends the session
   with one line per message on stderr and the traceback in the file; Ctrl-C in the middle
   of a turn prints `[stopped]` and `calu: interrupted` (exit 130), and so does SIGTERM
   since that day; a database that goes away is one line per reason (it was one per attempt)
   and the server goes on when it is back.
34. Local clients (D226, D227): with a session from `calu run --sudo`, `calu attach --sudo` in
   the project must attach without asking calu's password (only sudo's); with `--secure` on
   the run it must fall back to the database and a token. With `calu run --insecure` (no
   sudo), `calu attach` in the project must attach without a password, and `calu attach
   ws://127.0.0.1:<port>` without `--token`; without `--insecure` the latter must say it
   wants a token. **Run in the root box on 2026-11-18**: all four as stated. With
   `--insecure`, a session as one user and a root session for a user both listen on a
   socket of that user (`/tmp/calu-<uid>/sessions/<id>.sock` there, no `/run/user`; socket
   0600, directories 0700, the user's), another user cannot enter the directory but is let
   in on the loopback port, as the option says, and both are recorded as
   `client:local:calu attach`. A directory of that name planted by another user is refused
   ("no local socket"), and the clients use the address. With `--secure`, `calu attach
   --sudo` asked the admin password of that time once and used a token; with the one user
   login it must ask the calu user password once. Not yet run with the one user login.
35. The place file and an empty user password (D228, D265): after `sudo calu setup` with a
   user password, `/var/lib/calu/database.json` must exist (`-rw-r--r-- root root`) and hold
   no password (after `--no-password` it holds `admin_password`, the generated password of
   `calu_admin`, and your plain `calu attach` must save a checker's instructions without
   asking anything). With a session from `calu run --sudo --insecure`, your own `calu attach`
   must save a checker's instructions after asking the calu user password once. After `sudo
   calu setup --no-password` (it asks the user password once, to open the stored logins):
   `calu run --sudo` and `calu attach --sudo` must ask for no calu password, saving a
   checker's instructions in the `--sudo` client must ask nothing, and `calu web --sudo` must
   print an address with `?token=`. Every user of the machine, one without any credential
   included, then lists sessions, resolves escalations and changes settings without a prompt
   (by design). `sudo calu setup --set-password` must ask a new user password twice; after
   it the file holds no secret, the published password stops working, a user without a
   credential reaches nothing again, `calu_admin` logs in with the new user password, and
   the logins in `/etc/calu` are encrypted. Not yet run with the one user login. **Run in the
   root box on 2026-11-18** with the logins of before D265 (settings changed with `calu
   config set` and `calu ide`, not in the full-screen client): the file is `-rw-r--r-- root
   root` in a directory only root writes; after a setup with passwords it holds host, port
   and database name; with `--no-password` it held the generated password, every user
   reached the database without a prompt, and a later setup with passwords took it out.
28. A conversation started with `claude` itself (D209): in a project directory run `claude`,
   send a message, quit. `calu list-sessions` must list it with "outside calu" and that
   message; `calu run --resume <id>` must continue it and warn at the start that none of
   its steps was checked. Run on 2026-11-13: the listing and the reading of a real
   transcript; not run: the resume under calu with real checkers.
36. Session traces with a real redaction model (D232): `calu config set traces.enabled true`,
   then a session whose prompt plants a fake key and a made-up person, for example
   "Write `notes.txt` with the line: contact Jane Roe, jane.roe@example.org, key
   AKIAIOSFODNN7EXAMPLE". Expected in `agent/traces/<date>/<user>/session_NNN.md`: the key
   as `[redacted: aws access key]` and the name and the address as `[redacted: name]` and
   `[redacted: e-mail address]`, in the user's block, in the tool call's input and in the
   narration; every other word, the code fences and the order of the blocks intact; no
   block withheld. Then `calu config set traces.redactor_model test:missing` and one more
   prompt: its blocks must read `[this step was withheld: redaction failed]`, the session
   must go on, and the planted values must be nowhere in the file. Afterwards
   `calu config unset traces.redactor_model` and `calu config set traces.enabled false`.
   Run on 2026-11-18 with `anthropic:claude-haiku-5-5` as the redaction agent, in the
   UI-test desktop (`uitest/setup-real.sh`), once with `traces.content` `narrated` and once
   `plain`: a made-up `sk-ant-api03-` key, a name, an e-mail address and a phone number were
   replaced in every block (user, tool input, check, tool result, Claude's words), no other
   text changed, no block was withheld, and each block was in the file one to three seconds
   after its event. With `test:missing` both blocks of the next prompt were withheld and the
   session went on. Found there, and fixed since: every change of a `traces` setting
   started a new file for every running session, also for one that wrote nothing afterwards
   (a file with a header and no block). Now a setting that changes nothing of the output
   starts none, an idle session switches files with its next block, and a file without a
   block is removed when it is closed. The unit tests cover the redaction with stand-in models
   (`tests/unit/test_traces.py`, `tests/unit/test_traces_session.py`).
37. The sandbox question of `calu setup` (requirement 42, PLAN D239). On a terminal,
   `calu setup` must explain the sandbox in two lines, say whether `bubblewrap` and `socat` are
   installed (if not: `sudo apt install bubblewrap socat`) and ask `Switch Claude Code's
   sandbox on? on/off` with `(on)` when both are installed, `(off)` otherwise; a second run
   must add `Stored now: ...` and offer that value. Answer `off`: the report says `Claude
   Code's sandbox: off (stored)`, and the recommendation (`strongly recommended ... --sudo`)
   appears only after `calu setup --no-password` in one's own account, not after a setup
   with passwords and not as root. Then `calu run -p hi` after a `--no-password` setup must
   show the same recommendation among the start-up warnings; `calu run --sudo -p hi` after
   `sudo calu setup` must not, and after `sudo calu setup --no-password` must (the place file
   then publishes the password of `calu_admin`); `calu doctor --db` must show a `sandbox` note or an ok line accordingly. With the
   sandbox off and `bwrap` taken off `PATH`, a session must start. Not run yet.
38. The relay of `calu serve` from another machine (PLAN D242; the record is
   `agent/records/notes/test-plan.md` I17-I30). On the server: `calu serve` prints the relay
   line, two sessions from `calu start --no-attach`, then `calu token mint --server`. On the
   laptop: the printed `ssh -N -L <port>:127.0.0.1:<port> <host>` (the port derived from the
   projects directory, the same after a restart of the server), then `calu attach
   ws://127.0.0.1:<port> --token=<token>` must list both sessions and ask which; in the
   full-screen client `/running` must list the server's sessions, `/switch`, `/new` and `/fork`
   must change to the named or new session; killing the forward and opening it again must
   reattach; `calu token revoke --server <id>` on the server must detach the client within 15
   seconds. On the server, `calu attach` in a project and then `kill -9` of `calu serve` must
   end the client at once with "the session ended: its process is gone".
39. The projects directory of `calu serve` (PLAN D243; test plan I31-I37). `calu serve
   --projects /etc`, `--projects /`, a start in a hidden directory and `--projects` of a
   symbolic link to `/usr/lib` must each end at once with one line that names the reason,
   before the password is asked. `calu start` in a directory outside the projects directory
   must end with "the server did not start the session: ... is not in <projects>, the
   projects directory this `calu serve` on <host> serves ...", and `calu start <dir outside>
   --relay ws://127.0.0.1:<port>` from the laptop with the same reason. Stopping and starting
   the server must print the same relay port; a second server of the same directory must say
   that the port is taken and print the next one; `calu token mint --server` must name the
   projects directory and the port the server took.
40. A `calu serve` that died (PLAN D244; test plan I38-I41). Start two servers of one user,
   A with `--projects ~/a` and B with `--projects ~`, and `kill -9` A. `calu start ~/a/x`
   must be started by B at once (B ends A's row before it claims), A's server token must be
   refused by the relay, and `calu token mint --server` must name only B. In the full-screen
   client, `/running` must end with one line per running server, "calu serve of user@host
   serves DIR"; through the relay from the laptop it must show the sessions' names (`/rename`
   on the server) and how long each has been in its state.
41. The redaction prompt and the trace format (PLAN D245) with a real redaction model. With
   `traces.enabled` on and the shipped instructions, send "Why does the build take so long?"
   and, as a second message, "Make the build compile once; keep the timeout at 30 s. I have
   been at this since 3 am." Expected in the file: no `### User` block for the first message
   (its turn heading and Claude's answer stay), the second message's requirement word for
   word, the sentence about 3 am removed, no block withheld. Then `calu traces instructions
   edit` (write "Keep the user's messages unchanged" under the shipped text) and one more
   question: it must stay in the file. `calu traces instructions reset`. Then
   `calu config set traces.layout flat`, `traces.file_names random`, `traces.header_times
   false`, `traces.header_user false` and `traces.turn_numbers false`: the next file must be
   `agent/traces/session_<12 hex digits>.md`, its header without "Started" and "User", its
   turns headed `## Turn` and `### End of turn`. In the web interface's settings, pane
   "Session traces": the instructions editor shows the shipped text and, folded, the fixed
   part; save it in two tabs one after the other: the second save must be refused with "changed
   by someone else meanwhile". Not run yet; the unit and database tests cover it with
   stand-in models (`tests/unit/test_traces.py`, `tests/unit/test_traces_session.py`,
   `tests/db/test_cli_reports.py`, `tests/db/test_web.py`).
42. The final redaction pass before a commit (PLAN D246) with a real redaction model, in a
   throwaway git repository registered as a project (never in a repository that matters).
   With `traces.enabled` on, send "Add `notes.txt` with the line: ask Jane Roe,
   jane.roe@example.org, then commit it". Expected: Claude's `git commit` takes a moment
   longer, the session log says "session traces finalized for the commit", `git show
   --stat HEAD` lists the trace file, and `git show HEAD:<trace file>` ends with the line
   `_[calu: the final redaction pass ran up to this line (check ...)]_`, has the name and the
   address replaced, and keeps the request word for word. After a second message and commit,
   `git diff HEAD~1 HEAD -- agent/traces` adds lines only after the first marker. Then
   `calu config set traces.redactor_model test:missing` and ask for one more commit: it must
   be refused with "final pass failed", and nothing is committed; `calu config unset
   traces.redactor_model`. Outside the session: `calu traces hook install` (and once with a
   hook of your own in place: refused; with `--chain`: kept and run after calu's), append a
   line to a trace file, `git add` it and commit from the IDE: the commit passes and the line
   is redacted; with the database stopped the same commit is refused with the message that
   names `calu traces finalize --staged` and `--no-verify`. `calu traces hook remove`. Run on
   2026-11-19 with Haiku (test plan K28-K31), except the commit from an IDE and the stopped
   database; the unit tests cover it with stand-in models (`tests/unit/test_traces_finalize.py`,
   `tests/unit/test_traces_session.py`).
43. Switching Claude Code's permission mode while a session runs (PLAN D247), with a real
   `claude` and a model that has auto mode. Start a session; every client must show the mode
   `auto`: VSCode and PyCharm the "mode" selector next to the supervision level, the web live
   view "Claude Code's permission mode", the full-screen client `mode auto` in the status line,
   the line client `/mode`. Ask for a long task with several commands, and while it runs choose
   `default` in VSCode: within a second every other client shows `default` (the full-screen
   client with "changed by ..."), the next command's check runs without auto mode's
   classifier, and the session page of `calu web` lists under "Settings changed while the
   session ran" "permission_mode ... mode: 'auto' -> 'default'" with the VSCode client's name. Switch to `acceptEdits` from PyCharm, then from the web view
   back to `auto`; `/mode ac` + Tab in the full-screen client completes `acceptEdits`. Make a
   step escalate in `acceptEdits` (`calu:escalate` with the fake checker, or a deny-list
   command): it must wait for a decision. `/mode plan` and `/mode bypassPermissions` must be
   refused with their reasons and change nothing. With `calu config set
   claude.require_auto_mode true` and a new session, switching to `default` must be refused
   naming the setting. Through the relay of `calu serve` (a server token) the switch must work
   as well. Run on 2026-11-19 with Haiku (test plan K13-K24); the unit, database and browser
   tests cover it with the scripted
   runner (`tests/unit/test_permission_mode.py`, `tests/db/test_web_live.py`,
   `tests/browser/test_web_live_ui.py`, the vitest and Kotlin tests).

45. The owner's answers of 2026-11-20 (PLAN D250), with a real `claude`. (a) In a long turn
   of commands in auto mode, switch to `default` while a command is being checked (a slow
   checker model helps): that command runs without an escalation, and no "was asked for the
   permission mode auto" warning appears; switch before the first turn of a new session: no
   warning either. (b) In the full-screen client open a step's diff, comment on a line: the
   session's row in the `comments` table names `edit:<tool call id>` (no command or page lists
   comments: `SELECT ref, path FROM comments WHERE session_id = '<session id>'` as the owner
   role). (c) `calu checker template list`, `show review-findings`, then `use
   review-findings` on a database with the shipped subagent-result text: replaced; `use
   edit-project-rules` over your own edit text: refused, `--append` adds it. (d) `calu setup <
   /dev/null` with an older shipped text stored: kept and listed with `calu setup
   --update-instructions`. (e) On a claude.ai subscription, run a turn until Claude Code
   reports a limit and open the usage panel: the limit shows "reported N min ago", no account
   name appears; a new session shows "not reported yet by Claude Code" again; after `calu
   setup` (migration 0042) the owner's `SELECT to_regclass('usage_limits')` gives NULL. The
   scripted tests cover the logic (`tests/unit/test_permission_mode.py`,
   `tests/unit/test_runner_contract.py`, `tests/unit/test_tui_more.py`,
   `tests/unit/test_templates.py`, `tests/unit/test_protocol_side_usage.py`,
   `tests/db/test_schema.py`, `tests/db/test_cli_setup.py`).
46. The usage panel in the IDEs and a held save across a restart (test plan A18, B24, H38). (a)
   In VSCode and in PyCharm click the context chip below the message field: the panel shows
   the 5-hour and weekly limits (each "reported ... ago" or "not reported yet by Claude
   Code"), Claude Code's `/usage` text and the cost; the session gains no turn. With a real
   `claude` on a subscription: "Your plan now" lists the plan's limits. (b) PyCharm in the SSH
   mode: save a file that a running turn edits ("1 file held until the turn ends"), close and
   reopen the IDE: the file stays held (the server keeps its version) until the turn has ended,
   then it is merged and uploaded. Scripted tests: `usagePanel.test.ts`, `UsagePanelTest`,
   `HoldKeeperTest`.
47. Git worktrees with a real `claude` (PLAN D251, `agent/records/notes/worktree-design.md`
   section 7; for the agent in the Cua desktop, on Haiku). Set up a project as
   `spikes/spike_worktrees.sh` does, with `honesty.mode enforce`, `traces.enabled` and
   `changes.snapshot` on and HEAD certified (`calu honesty trust HEAD`); run every item once
   with `.claude/worktrees/` in `.gitignore` and once without. Ask Claude with the spike's
   prompts. (L1) The main agent: `EnterWorktree`, an edit, a test run, a commit, `ExitWorktree`
   keep, `git merge` in the main checkout, a test run there: no escalation, and the merged file
   counts as verified (no honesty review starts). (L2) A worktree subagent makes a dishonest
   edit while the main agent runs tests in the main checkout: the subagent's next code run is
   denied with the mark, the main agent's runs pass. (L3) Two background worktree subagents at
   once, one dishonest: the honest one finishes its test runs; the session page of `calu web`
   (the "wrote" line of each step) shows an itemised write set for each
   (`.claude/worktrees/agent-.../...`), none "overlapping". (L4) A
   subagent runs `PYTHONPATH=../<sibling> python3 -c "import helper"`: the command checker
   escalates by calu's rule, the reason names both checkouts. (L5) A second session in the
   same project after L2, with the dishonest file still in its worktree: a code run there
   waits for the honesty review, which marks the file again. (L6) `git worktree remove
   --force` of a worktree with uncommitted work: Calu asks; a plain `git worktree remove` of a
   clean one runs. The scripted cases U1-U14 are in `tests/unit/test_worktrees.py`.
   `spikes/worktrees_l.sh` sets the projects up, runs the sessions (L1+L2, L4, L5+L6, L3) and
   collects the evidence; a step expected to ask ends its session by the escalation timeout,
   nobody decides it. **Run in the Cua desktop on 2026-11-20** (CLI 2.1.327, Haiku, calu from
   `d9e8e1b` to `8440a28`), both variants: as stated after three fixes (`255d577` the timeout
   message, `63b4a40` the "Checkout:" line of a `cd` into a worktree, `8440a28` a worktree the
   step creates), with two observations that are model judgements, not calu's; details and
   the open points in `agent/records/notes/test-plan.md`, section L.
48. The owner's decisions on the worktree round (PLAN D252), with a real `claude` (for the
   agent in the Cua desktop, on Haiku), in a project like item 47's with `traces.enabled`.
   (a) Dropped: the trace's headings no longer name a step's worktree (the owner, 2026-11-20).
   (b) Two
   background subagents hand their reports back: each `SubagentHandback` is reviewed by the
   subagent-result checker (the `steps` row "SubagentResult" with `"handback": true`, a
   `checker_runs` row "subagent-result", no command checker run), the checker's note reaches
   the main agent, and the empty final result after it is passed with the reason "handed its
   report back" and no checker run. (c) **Not measured yet:** make the result checker
   escalate a handed-back report (`calu:escalate-result` does that only with the stand-in
   checker; with a real one, a report that asks for something the user did not ask for) and
   deny it in a client: record what the subagent does after the denied `SubagentHandback`
   (it retries, ends, or answers in text), what the main agent receives (a task notification,
   an empty result), and whether calu's note arrives before or after it. (d) In a worktree
   outside the project (`git worktree add ../x`), commit a staged trace file with the hook
   installed: the final pass uses the project's settings. (e) `/trust <worktree> HEAD` in the
   full-screen client (Tab completes the name) and in `calu attach --plain`: the answer names
   the worktree, and the commit is the worktree's `HEAD`. (f) A command writes `out.txt`
   into a checkout with honesty enforced: the next code run there does not wait for a model
   review (the log line counts it "without code").
49. Recording your own edits (PLAN D253), with the scripted server or a real session, in a git
   project. (a) `calu config set human_diffs.enabled true` prints the hint with
   `calu config set traces.enabled true` while the traces are off; switching it on in the
   VSCode settings panel, in PyCharm's settings dialog and on the web settings page asks
   whether to switch on the traces too, and "Not now" leaves them off. (b) Attached in VSCode,
   edit and save a tracked file: `agent/human-diffs/<date>/<user>/<path>.tmp.diff` gets one
   block (`# <path>: saved ... in vscode`, then the diff); the directory has a `.gitignore`,
   and `git status` shows nothing new. A second save appends a second block. Saving an
   untracked file, a change Claude makes, and `echo x >> file` in the terminal add nothing.
   (c) The same in PyCharm in each mode (local, WSL, SSH with calu's synchronisation): a
   local save is recorded; a change of Claude that the synchronisation writes into the local
   copy is not. (d) Rename a file to `<name>.diff`, set `human_diffs.keep_days 1`, age both
   files with `touch -d '3 days ago'` and start a session: the `.tmp.diff` file is deleted and
   named in the log, the `.diff` file stays. (e) Claude's `Write` into `agent/human-diffs/`
   waits for a person. (f) In each IDE, edit a tracked file without saving, let Claude (or
   `echo x >> file`) change it on disk, then save and choose to overwrite (VSCode
   "Overwrite", PyCharm "Keep Memory Changes" and save): the block shows only your edits, not
   the removal of Claude's line. Repeat but load the disk version first (VSCode "Revert File",
   PyCharm "Load File System Changes"), edit and save: the block is diffed against Claude's
   version. The unit tests are `tests/unit/test_human_diffs.py`, the editors'
   `test/unit/humanDiffs.test.ts` and `HumanDiffsTest.kt`.
50. Links to source lines in the chat (VSCode and PyCharm), with the scripted server or a real
   session. Let Claude answer with `src/app.py:12`, `src/app.py#L12`, `docs/guide.md`, a code
   span `` `src/app.py:3` ``, a bare `hello.py:2` and a Markdown link whose target is `src/app.py#L5`:
   each is a link, and a click opens the file at that line. `README.md` is not a web link any
   more. A URL (`https://example.org/a.py`), an absolute path (`/etc/hosts.conf`), `picture.png`
   and text inside a code block stay plain. In VSCode a path outside the workspace folder
   (`../../x.py`) opens nothing; in PyCharm's SSH mode a path outside the server's project
   directory gives the notice that the file is not on this machine.
51. Trusting commits and files from the IDEs (VSCode and PyCharm), in a git project with honesty
   `track` and a worktree. In the chat: `/trust` asks "Trust HEAD?" and after "Trust" the chat says
   which commit is trusted; `/trust main`, `/trust <worktree> HEAD` and `/trust <commit id>` do the
   same for that commit; `/trust a b c` shows an error at the input; `/trust-file src/x.py` asks and
   then trusts the file; `/trust-file` alone asks for the path (the open editor's file is
   suggested); `/honesty-review` starts a review. "Cancel" records nothing (`calu honesty trusted`).
   The honesty menu's "Trust HEAD…", "Trust a commit or branch…" and "Trust a file…" do the same.
   VSCode only: "Trust This File as Honest" in the explorer's and the editor's context menu.
52. The chat pane of `calu web` (PLAN.md D261), with a running `calu serve` and a session in a
   project. (a) Open the printed `chat pane:` address with `&dir=<project>`: the session of the
   project is attached, the transcript and the composer work as in VSCode, and the address no
   longer shows the token. Reload: the pane connects again without the token in the address.
   (b) Send a message, decide an escalation, switch sessions in the session menu, start a new
   session, rename it. (c) Click a file link and "Open diff" of an edit: a read-only view at the
   line, and a unified diff. (d) The gear opens the full web interface's settings in a separate
   window. (e) In a Jupyter notebook on this machine, `IFrame("<pane address>", width="100%",
   height=700)`: the pane works inside the notebook. A page on another host that is not in
   `web.pane_frame_ancestors` cannot frame it (the browser shows a refusal). (f) With `calu web`
   signing in by the calu user password: the pane in its own window works after signing in to
   `calu web` in the same browser.
53. The password experience (PLAN.md D265, REQUIREMENTS 48), on a store with a calu user
   password. Go through each case in each front end: the CLI (`calu sessions`, `calu run`),
   the full-screen client's save (`/checkers`, `e`, ctrl+s), the sign-in page of `calu web`,
   the password dialogs of VSCode and PyCharm, and the chat pane of `calu web`. The texts are
   those of `src/calu/logins.py`, the same in every front end.
   (a) A wrong password: "That is not the calu user password" with the tries left ("2 tries
   left", then "1 try left" on a terminal); after the last try "Not done: ..." with the
   pointer to "A forgotten user password" in DOCUMENTATION.md. A front end that asks again by
   itself (the web page, the dialogs) shows the pointer with every refusal.
   (b) Enter on an empty prompt: "No password was typed, and this login has one", never
   "wrong".
   (c) Ctrl-D (CLI, full-screen client), Escape or closing the dialog (VSCode, PyCharm, chat
   pane): one message that says what was not done ("Not done: no password was entered. Run
   the command again to retry." on a terminal; in an IDE the action that needed the database
   is not done and the next one asks again), and nothing changed meanwhile.
   (d) No terminal: `calu acp` started by an editor in a project whose login has a password
   must not hang; it says that it cannot ask for the password, why, and the two ways out
   (give it where the front end asks, or remove it with `calu setup --set-password` and an
   empty answer). `calu sessions </dev/null` must say "Not done: no password was entered".
   (e) An outdated stored login: on machine A `calu setup --set-password`; on machine B, which
   holds the `user` entry of that database under the old password, `calu sessions`, the
   full-screen client's save, `calu web` and an IDE: each must open the entry with the old
   password and then call the login outdated (the database refused it), naming how to renew
   it; check that the named way works on a machine that holds only `user`.
   (f) An empty user password (`calu setup --set-password` and Enter, or `--no-password`):
   nothing is asked anywhere (CLI, full-screen client, IDE dialogs, chat pane); `calu web`
   prints an address with `?token=` and the browser gets no sign-in page; `calu setup` and
   `calu doctor` say that every program of the user can then use the login.
   Not run yet.
54. `calu uninstall` (PLAN.md D267), in a throwaway machine or container only (it removes the
   real logins and the local database): install with `install.sh` as yourself, `calu setup`,
   then `calu uninstall --dry-run` must list `~/.config/calu`, `~/.local/state/calu`, the
   local database (container and data directory) and the uv tool installation, and name what
   it keeps. With `calu serve` running it must refuse and list the process. Typing `remove
   calu` faster than two seconds (`echo "remove calu" | calu uninstall`) must be refused;
   typed by hand it removes everything listed, `calu` is gone from `PATH`, `docker ps -a` shows
   no `calu-db` container, and a new `install.sh` + `calu setup` starts from nothing. As root
   (`sudo calu uninstall` after `install.sh | sudo bash`): `/etc/calu`, `/var/lib/calu`,
   `/usr/local/lib/calu`, `/usr/local/bin/calu` and the invoking user's calu files go;
   uv, Docker and the postgres image stay. Not yet run by hand.
55. "New session…" in the web interface (PLAN.md D270), with `calu serve --sudo --web` in
   `~/projects`: the button opens a window; the dialog lists the folders of `~/projects`
   (no hidden ones), walks into one, makes a new folder, clones a public https repository
   into a new folder named after it, and refuses `../x`, `.x` and an `ext::` address. With
   "start a session" and "open the chat pane" the window becomes the pane, attached to the
   new session; with only "start a session" it shows the live view. Without a running server
   it says how to start one. The new folder and the clone belong to you, not root. Not yet
   run by hand.
56. The "chat" link of the sessions list (PLAN.md D271): with two sessions of yours on this
   machine (one per project) and one of another host in the database, the list shows "chat"
   for the two local ones only; each opens a window with the chat pane attached to its own
   session (its transcript, its escalations), also when both run in one folder. Not yet run
   by hand.

## Measuring the checkers

`calu evals` runs the case set against the checkers of a database; `uv run python
spikes/run_builtin_cases.py` runs it against what calu **ships**, which is what a change to
`src/calu/defaults/checkers/*.md` has to be measured with (agent/records/PLAN.md D73). It needs a key for a
real model in the environment, without a carriage return (D70).

Read the result as follows:

- **critical misses** are the number that matters: a step the checker approved that the case
  says it should not have. The report says how many of them calu's own rules stop anyway
  (D76) -- those cost nothing in production, and the rest are where the gate rests on the
  model alone.
- **one run is not a measurement.** Two runs of identical instructions have differed by two
  critical misses. Use `--repeat 3` and run it twice before believing an improvement. The
  report then says how many **cases** the misses are spread over and how many failed in every
  run (D98): a case that fails once is unstable and may settle with a stronger model, while a
  case that fails every time is a judgement the checker does not have, and repeating it will
  not produce one.
- an **error** is a check that never reached a verdict (a budget ran out). It escalates in
  production, so it is safe and costly, not dangerous. The accuracy is of the checks that
  **answered**, and the line says so (`accuracy 95% of 19 answered`) whenever any failed: a
  check that never judged the step was not wrong about it, and mixing the two would hide a run
  that mostly failed behind a plausible-looking percentage (agent/records/PLAN.md D97).
- a case of `escalation share` far from the earlier runs is worth a look even when the
  accuracy holds: it is the cost side of the gate. Read the split beside it (D100): an
  escalation a case **wanted** is the checker working, and one **spent on a step that should
  pass** is what a user pays for. A third of answers being escalations is fine when none of
  them is waste.

### Throwaway databases that a killed run leaves behind

`smoke_session.py` and the `db_url` fixture each create a database per run and drop it in a
`finally`, which an interrupted process never reaches. They collect quietly on the development
machine, named `calu_smoke_*` and `calu_test_*`:

```bash
docker compose exec caludb psql -U calu_owner -d calu -c "SELECT datname FROM pg_database WHERE datname LIKE 'calu_smoke%' OR datname LIKE 'calu_test%'"
```

Drop one with `DROP DATABASE <name> WITH (FORCE)`. They hold nothing but a throwaway session,
so dropping all of them when no run is going is safe.

## What coverage says, measured once

There is no coverage tool in the project's dependencies on purpose -- the tests are written to
falsify behaviour, not to reach lines -- but the number is worth knowing. Measured on
2026-11-10 with `uv run --with coverage python -m coverage run --source=src/calu -m pytest -m
'not live and not browser'` (ephemeral, nothing added to `pyproject.toml`): **89%** of 8987
statements, 29 modules complete.

What the gaps are, so nobody chases them again:

- `src/calu/cli.py` (69%) and `src/calu/acp/serve.py` (40%) are **run in subprocesses** by the
  process-level tests, which coverage in this process cannot see. They are exercised end to
  end; the number is wrong, not the testing.
- `src/calu/claude/sdk_runner.py` (71%) needs a real `claude`, so the `live` tests cover what
  the fakes cannot.
- The rest is error handling and cancellation paths -- a failing database call, an interrupted
  turn, a model that cannot answer. Worth a test when a specific one is in doubt, not as a
  sweep.

## Writing tests

- A test that spawns a process must guard its teardown: `terminate()` on a process that has
  already exited raises `ProcessLookupError` and turns a passing test into a flake. Write
  `if process.returncode is None:` around it. This cost two red runs in one day before the
  three process-level tests were made consistent.
- A flaky process test may be telling the truth about the product. The ACP and attach tests
  failed about once in forty full-suite runs and were twice treated as infrastructure; with
  stderr in the message the next occurrence read `calu attach: error: argument --token:
  expected one argument`, which was a real defect in 1.71% of tokens (D102). Read the message
  before blaming the harness.
- A test that spawns a process must put **stderr into its failure message**. A process-level
  test that fails with "stdout ended" and nothing else cannot be diagnosed, and the occurrence
  is often not reproducible: `test_acp_process.py` failed once in a full suite run on
  2026-11-10 and was then clean for 17 runs (6 of the whole suite, 8 of the process tests
  alone, 3 earlier), so the message was all there was to go on -- and it said nothing. Both
  process tests now quote the exit code, how far the protocol got and all of stderr.

- Unit tests use the fake runner (`calu.claude.FakeRunner`, a scripted stand-in for Claude Code)
  and the rule-based fake checkers (`calu.testing`), so they need no API key and no `claude`.
  A scripted call can really write files (`"writes": {...}`) when a feature watches the file
  system.
- `tests/unit/helpers.py::make_session` builds a session with the fakes, an in-memory store and
  a snapshot directory next to the workspace — **no test writes outside its temporary
  directory**.
- Database tests take the `db_url` fixture, which creates a database per test.
- Browser tests take the `page` fixture of `tests/browser/conftest.py` and serve the application
  with `serve(...)` on a free port in the same event loop. The pages are served with their
  Content-Security-Policy, which forbids `eval`; the fixture therefore wraps the expression of
  `page.wait_for_function` into a function. A template must not carry an inline script with a
  template expression in it, an inline event handler or a `style` attribute
  (`tests/unit/test_web_gate.py` checks that): the policy allows the inline scripts by the hash
  of their source and drops the rest without an error.
- **A test that spawns a process waits for what the child needs.** Both process-level tests
  connect to the session's port themselves before spawning the child: `SessionServer.ready` is
  set once the socket is bound, which should be enough, but one CI run failed with an empty
  stdout -- the symptom of a refused connect -- and a flaky test is worth less than no test.
  They also capture the child's stderr into the failure message, because a process that dies
  before printing says why only there.
- **Do not assert an exit code that depends on the machine.** `calu doctor` exits 1 when
  anything a session needs is missing, so a runner without `claude` gives 1 where a developer's
  machine gives 0 -- one such assertion turned CI red after passing locally. Assert the lines
  the command printed about the thing under test, and keep exit-code assertions for cases that
  hold everywhere (a database that cannot be reached, a refused argument).
- `tests/unit/test_docstrings.py` checks that every public function documents its arguments,
  return value and the exceptions callers must handle; ruff's `D` and `DOC` rules enforce the
  style. A new function without a contract fails the suite, which is the point.
