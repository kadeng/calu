# Testing calu

Four layers, each with its own cost, plus the spikes that measure what Claude Code actually
does. The default run needs nothing but the repository.

| Layer | Marker | Needs | How long |
|---|---|---|---|
| Unit | none | nothing (a few spawn `python -m calu` or a stub `docker`) | ~15 s |
| Database | `db` | PostgreSQL (`docker compose up -d caludb --wait`) | ~20 s |
| Browser | `browser` (and `db`) | a headless Chromium (`scripts/get-browser.sh`) | ~8 s |
| Live | `live` | a real `claude` and/or model API keys | minutes, costs money |
| Spikes | — | run by hand, against the real CLI | minutes each |

```bash
scripts/wsl-test.sh                 # format, lint, types and the unit tests
scripts/wsl-test.sh -m 'not live'   # ... and the database tests (start the database first)
uv run pytest -m 'browser and db'   # the browser tests
uv run pytest -m live               # the live tests (needs keys)
```

`scripts/wsl-test.sh` runs `ruff format --check`, `ruff check`, `pyright` and `pytest` in that
order, with the virtual environment outside the checkout
(`UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`), which matters on a Windows host where the
repository lives on a mounted drive. The default `addopts` deselect `live`, `db` and `browser`,
so a plain `uv run pytest` is the fast layer.

Four tests run a real process instead of a mock, because that is the only way to check
them: `tests/unit/test_acp_process.py` spawns `python -m calu acp` and speaks ACP to its pipes,
`tests/unit/test_attach_process.py` spawns `python -m calu attach` against a real session and
types at it, and `tests/unit/test_local_db.py` puts a stub `docker` on PATH and checks what
`calu db up` would run, and `tests/db/test_web_process.py` starts `python -m calu web` with a
credential store of its own and checks that it serves the fleet page on loopback and **refuses**
a public address. The first three need neither Docker nor a database; coverage does not see the
code that runs in a child process -- `calu/acp/serve.py` and the input loop of
`calu/attach.py` are exercised end to end while reporting as uncovered.

Sync with `uv sync --all-extras`: the web interface is an optional extra (`web`), and the tests
that need it skip without it with a message saying so, rather than failing to collect.

## Continuous integration

`.github/workflows/checks.yml` runs on every push to `main` and on every pull request:

| Job | What it runs |
|---|---|
| `checks` | `ruff format --check`, `ruff check`, `pyright`, then `pytest -m 'not live and not browser'` against a `postgres:18` service -- the unit and database layers |
| `browser` | `playwright install --with-deps chromium` (a runner has root, so it needs no `scripts/get-browser.sh`), then `pytest -m browser` |
| `package` | `uv build`, then installs the wheel into a throwaway environment and runs `calu --version`, `calu protocol` and an import of the package data out of `site-packages` -- the check that D41 traded the single binary for |

The live layer does not run in CI: it needs a logged-in `claude` and real model API keys, and it
costs money, so it stays a local and manual check (see below).

## The database layer

```bash
docker compose up -d caludb --wait
uv run pytest -m db
```

`caludb` listens on 127.0.0.1:55432 (`calu_owner` / `calu-dev`, development only). Each test
creates and drops its own database, so the tests do not interfere and a failed run leaves
nothing behind. `tests/db/` covers the schema and the roles (what `calu_session` may *not* do),
the stores, the admin commands, the herd queries, the web interface and the live relay.

The databases are per test, but the roles `calu_session` and `calu_operator` belong to the whole
server, and every `setup` gives `calu_session` a new password. Two test runs against the same
server at the same time can therefore take each other's session login away, which shows as a
failed login or as `tuple concurrently updated`. Give a second concurrent run a server of its
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
login that stops working for good: a test replaces the session credential (as every `calu setup`
does) or removes the database, and checks that the reconnecting ends after a few attempts with
an error that names the cause, and that `calu run` then exits with 1. Session isolation
(`tests/db/test_schema.py`) includes the attack of a second holder of the `calu_session` login
who names another session's id, and checks with two real `calu_session` logins that the
sessions of one conversation read each other's rows and write only their own.

The end of an unattended run (`tests/db/test_run.py`, `tests/unit/test_run.py`): a prompt with
further messages piped to stdin, and a `--batch` run with a message a client queued, each with
and without narration and with turns that call no tool, must do every turn and then return.
That a session which ends, or whose process is gone, leaves no open escalation is covered in
`tests/db/test_herd.py`.

## The browser layer

```bash
scripts/get-browser.sh              # once; needs no root
uv run pytest -m 'browser and db'
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
| `spike_question_tool.py` | Does `AskUserQuestion` reach the hook, and can calu answer it? (D35) | the CLI changes |
| `spike_fork_session.py` | Can a conversation be forked at an earlier turn? (D36) | the SDK changes |
| `spike_auto_mode_ask.py` | Does auto mode ask rather than only allow or deny? (D38) | the CLI changes |
| `spike_calu_fork.py` | Does `calu run --fork-before-turn` branch a conversation calu recorded? | the fork code changes |
| `spike_rewind_files.py` | What does `rewind_files` put back -- tool edits only, or a command's writes too? (D46) | the SDK or the CLI changes what checkpointing tracks |
| `spike_session_rls.py` | Would row-level security keyed on a per-connection session id separate two sessions? (Q3) | the schema gains a table a session writes |
| `scripts/build-binary.sh` | Does a frozen one-file calu still find its package data, and what does it cost? (Q15) | a dependency is added or pydantic-ai is updated (the release workflow runs it on every tag) |
| `smoke_session.py` | A whole calu session against the real `claude` | before a release, and after changes to the runner |

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
operator for the first escalation) and `--model`.

**Without `--sandbox` the commands Claude runs are not isolated**, so always point it at a
throwaway directory.

## The manual checklist

Before a release, or after changing the runner, the gate or the sandbox. `HANDOFF.md` records
what was run and what came out of it, under "Verified by hand with the real CLI"; on 2026-11-10
items 1, 2, 5, 6, 7, 9, 10, 12 and 14 were exercised, and item 4 as part of the network
measurement in `spikes/RESULTS.md`; item 11 was done as an automated test instead
(`tests/db/test_run.py`), and item 13 needs an editor, which this machine has none of. Items 1,
2 and 6 were run again at the end of that day, after a dozen changes to the gate, the register
and the clients -- an allow-listed command and an edit through a real `claude`, and a deny-rule
escalation resolved from the database alone.

1. `calu doctor --db` on the machine — nothing essential missing.
2. `scripts/wsl-test.sh -m 'not live'` — clean, and `uv run pytest -m 'browser and db'`.
3. `spikes/spike_hook_timeout.py` — the gate fails closed.
4. `smoke_session.py --sandbox` with a prompt that runs a command, and read the probe output of
   `spikes/probe.sh` from inside the sandbox (own PID namespace, no `claude` process,
   credentials unreadable, network blocked, no leftover process).
5. `smoke_session.py --changes` with a command that writes a file whose content carries the
   fake checker's deny marker: the change must be reverted and Claude must be told.
6. A deny-list escalation resolved from the database alone: `calu escalations`,
   `calu escalation <id>`, `calu resolve <id> deny --note ...` — the session continues within a
   second.
7. `calu web`: open the printed address (`http://calu.localhost:8800/?token=...`); the address
   without the token must answer 401 in a private window, `http://127.0.0.1:8800/` must answer
   403 and name the right address, and a `curl -X POST` must not decide anything -- neither
   without a cookie nor with the `calu_web_session` cookie copied from the browser (D161).
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
9. Session isolation (D49): `uv run pytest -m db tests/db/test_schema.py` covers it, and by
   hand after any change to the schema or the store, `smoke_session.py --resolve deny` must
   still record its step, its escalation and its turn -- a session that cannot write says so at
   once (an `InsufficientPrivilege` on its first step), which is the failure mode to recognise.
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
14. The password-free credential (D104): `calu credential set operator --no-password`, then
   `calu sessions` and `calu escalations` must run without a prompt, `calu credential list`
   must mark it "file mode only", and `calu doctor` must report it -- as a note for `operator`,
   as a problem if `admin` or `session` is stored that way. Verified by hand on 2026-11-10.
   `calu run` with a `session` entry stored that way must refuse to start.
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
   Claude Code settings do not set `permissions.defaultMode` must print, among the start-up
   warnings, that auto mode's classifier is NOT active; with `calu config set
   claude.permission_mode auto` the session must report the mode "auto" when it starts.
   `calu doctor` must show an `auto mode` line in both cases.
21. Resetting the database: on a scratch database with a project and a rule, run
   `calu db reset`. It must list what it would delete, ask for the admin password and for the
   phrase `yes delete the db`. Paste the phrase (it arrives at once): nothing may be deleted.
   Run it again and type the phrase: `calu project list` is empty afterwards and `calu run`
   still starts with the stored session credential.

## Measuring the checkers

`calu evals` runs the case set against the checkers of a database; `uv run python
spikes/run_builtin_cases.py` runs it against what calu **ships**, which is what a change to
`src/calu/defaults/checkers/*.md` has to be measured with (PLAN.md D73). It needs a key for a
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
  that mostly failed behind a plausible-looking percentage (PLAN.md D97).
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
