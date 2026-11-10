# Testing calu

Four layers, each with its own cost, plus the spikes that measure what Claude Code actually
does. The default run needs nothing but the repository.

| Layer | Marker | Needs | How long |
|---|---|---|---|
| Unit | none | nothing | ~10 s |
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

Before a release, or after changing the runner, the gate or the sandbox:

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
9. In a container: `docker compose run --rm dev sh -c 'command -v bwrap socat'` and a real
   `bwrap --unshare-user ...` — the `dev` service needs `security_opt: [seccomp:unconfined]`,
   because Docker's default seccomp profile blocks user namespaces.

## Writing tests

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
- `tests/unit/test_docstrings.py` checks that every public function documents its arguments,
  return value and the exceptions callers must handle; ruff's `D` and `DOC` rules enforce the
  style. A new function without a contract fails the suite, which is the point.
