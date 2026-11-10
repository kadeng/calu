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
and the jump palette.

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

## The spikes: what Claude Code actually does

calu's design rests on measured behaviour, not on documentation. Every assumption about Claude
Code has a spike, and `spikes/RESULTS.md` records what each one found, with the CLI version. Run
them by hand; they use throwaway directories and databases.

| Spike | Question it answers | Re-run when |
|---|---|---|
| `spike_q1a_cc_sandbox.sh`, `spike_q1a_precedence.sh` | Does Claude Code's own sandbox isolate Bash, and can project settings switch it off? (D15) | the CLI changes |
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
items 1, 2, 5, 6, 7, 9 and 10 were exercised, and item 4 as part of the network measurement in
`spikes/RESULTS.md`; item 11 was done as an automated test instead (`tests/db/test_run.py`).

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
7. `calu web`: the fleet, a session at all four levels of detail, the triage queue, the settings
   page, Ctrl+K.
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

## Writing tests

- A test that spawns a process must guard its teardown: `terminate()` on a process that has
  already exited raises `ProcessLookupError` and turns a passing test into a flake. Write
  `if process.returncode is None:` around it. This cost two red runs in one day before the
  three process-level tests were made consistent.
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
  with `serve(...)` on a free port in the same event loop.
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
