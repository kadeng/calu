# Next step: one calu login ("user") instead of admin, operator and session

Planned on 2026-11-24, not started. Code untouched; this file is the plan. Delete it (and the
pointer in `HANDOFF.md`) when the change is done and recorded as a decision in `PLAN.md`
(next free number after D264).

## The owner's request (2026-11-24, his words)

- "For now, we can subsume all of these under a single "user" role for now (including the
  setup). We could have a separate DB admin role (that's a database user not necessarily a
  calu role) that's capable of changing settings that the regular user can't and work with
  multiple tablespaces (each for a single user). Ensure this is changed, the code and
  documentation is updated and simplified accordingly. When setting up the DB and the DB is
  preexisting, we might want to ask for a DB user. When setting it up locally, the DB password
  and the user password can be identical."
- "In addition, we could have (for example for testing) an empty user password. In this
  scenario we don't ask for a password at all."
- Context that led there: he was asked for an "operator password" by `calu sessions` that he
  never knowingly set, and found admin/operator/session confusing. In every UI (web, IDEs) the
  common case needs deciding and changing settings anyway. I explained (and he accepted) that
  the split between calu's three logins does not guard against Claude: Claude never holds any
  login (store encryption, sandbox, root mode, the web token keep it out), and `operator`
  can approve escalations anyway. See the conversation of 2026-11-24 and D48, D213, D228, D249.

## Decisions taken (the owner did not answer the three questions; these were my
recommendations, announced to him; confirm if in doubt)

1. **Per-user schemas: later, a separate step.** "Tablespaces" in his message means separation
   per user; in PostgreSQL that is a schema (or database) per user, not a tablespace (a storage
   location). This change keeps one user per database, but names things so that a later
   "schema per user" (REQUIREMENTS soft goal 7, PLAN D205 "a login role per machine") fits.
2. **Per-session row-level isolation is dropped for the user's own sessions.** Sessions run
   with the user login; the RLS policies of migrations 0015/0025/0026 stay in the schema (the
   `admin_all` policies of 0033 already let `calu_admin` through) but no calu process logs in
   as `calu_session` any more. Separation between people comes later from per-user schemas.
3. **Existing installations migrate automatically, with one prompt** (the owner's WSL machine,
   backtester1 in root mode with typed passwords, the Cua desktop `calu-uitest`, the rootbox).

## Target model

| Who | Database role | Store entry | Password | Used for |
|---|---|---|---|---|
| the calu user | `calu_admin` (keep the role name in the database; it already reads and writes every table, is a member of `calu_operator`, passes every RLS policy via `admin_all`; renaming a role risks its password, see "Risks") | `user` | the **user password**; may be **empty**: then the entry is stored without encryption-by-password and calu never asks | everything: `run`, `serve`, `start`, `attach`, `sessions`, `escalations`, `resolve`, `token`, `config`, `checker`, `rules`, `honesty`, `web`, `ide`, `acp`, `evals`, `summary`, `project`, the TUI's admin actions |
| the database admin | the owner (`calu_owner` for the local database; for an existing server the DB user given at setup) | `db-admin` (or none: see below) | the **DB password** | migrations (`calu db migrate`, `calu setup`), creating/renewing the user login, `calu db reset`; later: creating per-user schemas |

- Wording in docs and prompts: "user password" (calu) and "database password" (owner). The
  words "admin password", "operator", "session password" disappear from user-facing text.
- **Local database:** `calu setup` asks for ONE password and uses it for both the owner and the
  user login ("the DB password and the user password can be identical"). Whether the owner
  login is stored at all: store it as `db-admin` under the same password (simplest; `calu db
  migrate` and later setups need it). Empty password allowed (testing): both entries without
  password, no prompts ever; PostgreSQL refuses empty passwords for logins, so with an empty
  user password calu generates a random DB password for both roles and stores it in the
  entries (the entries are then protected by the file mode only, like `--no-password` today).
- **Existing server:** `calu setup --database server` asks for a DB user (the owner, or a role
  that may create roles and own the schema) and its password, migrates, then creates/updates
  the `calu_admin` login with the user password (or a generated one if the user password is
  empty) and stores `user`. Storing `db-admin` there is optional (ask: "keep the database
  admin login on this machine for later migrations? [y/N]"); without it, `calu db migrate`
  asks for the DB user and password again.
- **RDS with IAM:** the user login uses IAM like the session login does today
  (`--session-auth`/`RdsIamAuth`); carry that over to `user`.
- **Root mode (`--sudo`, `/etc/calu`):** same entries in root's store. The place file
  `/var/lib/calu/database.json` (D228, `calu.db.place`) keeps its role: where the database is,
  and with an empty user password the user login every user of the machine may use (today's
  `--no-password` + `ADMIN_ENTRY` path). Keep the existing "nothing writable but by root"
  checks.

## Code: what goes, what changes (paths and functions found on 2026-11-24)

Counts at planning time: `calu_session` 207 refs in src/117 in tests, `calu_operator` 105/20,
`calu_admin` 142/82, entry names `"operator"` 23/58, `"session"` 36/54, `"admin"` 62/124,
`ADMIN_ENTRY` 29/4. 42 migrations, 25 mention the roles.

- `src/calu/db/roles.py`: SESSION/OPERATOR/ADMIN constants and `narrow` (SET ROLE to the
  narrowest role, D249). Keep `ADMIN` (the user role) and `OWNER`; drop `narrow` calls that
  narrow to `calu_operator` (no other login exists); delete `narrow` if nothing needs it.
- `src/calu/setup.py`: `SESSION_ROLE`, `OPERATOR_ROLE`, `ADMIN_ROLE` (390), `admin_login`
  (395), `enable_admin_login` (458) -> becomes "set the user login's password",
  `enable_operator_login` (486) -> delete, `session_can_log_in` (315) -> delete,
  `setup_database` (548) returns a session login today -> returns nothing / the user login.
  New migration 0043: `ALTER ROLE calu_session NOLOGIN; ALTER ROLE calu_operator NOLOGIN;`
  (the roles stay, so old migrations and grants stay valid); make sure `calu_admin` can do
  everything a session does (it has ALL on tables, EXECUTE on functions, `admin_all`
  policies; check `calu_start_session`/`calu_claim_session` and the SECURITY DEFINER
  functions of 0015/0025/0026/0032/0038 for checks like `calu_caller_is_operator()`,
  which `calu_admin` passes as a member of `calu_operator`). tests/db/test_schema.py has
  checks on roles and policies that will need updating.
- `src/calu/cli.py` (biggest part):
  - `_setup` (1539-1795) and `_new_database` (1482), `_setup_server` (1126), `_setup_rds`
    (1165), `_check_setup_options` (1222), `_check_session_options` (1456): rewrite around
    one user password; options `--renew-session`, `--session-auth`, `--session-aws-profile`
    become `--renew-user`/`--user-auth` or go; `--no-password` becomes "empty user password"
    (keep the flag as a shortcut for it); `--set-passwords` -> `--set-password` (one).
  - `ADMIN_ENTRY = "calu_admin"` (1806), `_after_setup` (1810ff), `_store_new_admin` (606),
    `_new_admin_password` (631), `_remember_admin_password` (333), `_typed`, `_admin_secret`
    (387), `_admin_login_for` (436, `--database admin|operator`), `_root_store_hint` (514),
    `load_credential`/`load_target` prompts ("Admin password: ", "Operator password: ",
    "Session password: ") -> "User password: " / "Database password: ".
  - `LOGIN_ENTRIES` (2018), `_new_operator` (1882), `cmd_credential` (1933): `credential
    new operator` goes; `credential set|remove|list` know `user` and `db-admin`.
  - `_session_login` (2212), `_only_root_writes` (2197), `cmd_serve` (2260), `cmd_run`
    (2509): use the `user` entry; an empty password asks nothing.
  - `OPERATOR_FIRST` (2614), `_herd_command` (2623), `_herd_value` (3320),
    `_hold_password_free` (2956), `_password_free_login` (2981), `_hold_for_client` (3036),
    `_login_for_password` (3157), `_shared_admin` (3304), `_revoke` (3255), `_admin_here`
    (3671): collapse to "load the `user` login (or the place file's), ask its password
    unless it is empty".
  - `cmd_web` (4185): `typed_password`/`by_password`/`check_password` (D216): the browser
    signs in with the user password (checked by logging in as `calu_admin`); with an empty
    user password the token is the login, as with `--no-password` now.
  - every `cmd_*` with `--database admin|operator` (6382): drop the option.
- `src/calu/ide/access.py`: `ADMIN_ENTRY` (65), `READ_FREE` (68), `shared_login` (130),
  `Access` (146ff: `place`, `unlock`, `NEEDS_PASSWORD` "admin"/"first", `_only_place`):
  one login, one password ("the one password" of the IDE spec stays, it is now the user
  password). `src/calu/ide/bridge.py` NEEDS_PASSWORD data names the entry.
- VSCode `src/shared/unlock.ts` and JetBrains port (`PromptKind` "first"/"admin", prompt texts
  in `promptText`): one kind of password; the "admin password for changing settings" round
  (`prompt-admin`) goes. Tests: `test/unit/unlock.test.ts`, Kotlin `UnlockTest`.
- `src/calu/tui/adminaccess.py` (`StoreAdmin`, `PasswordNeeded`, `_has_secret`): the admin
  password prompt becomes the user password (or none).
- `src/calu/doctor.py` (credential rows, f0ee891 "a single credentials row for users of root
  calu"), `src/calu/preflight.py` (`self_approval_warnings`: today it warns about a
  password-free admin; now: warn about an empty user password when the sandbox is off and
  calu does not run as root -- that is the self-approval path), `src/calu/web/app.py`
  (`PasswordCheck`, `is_admin_password`), `src/calu/credentials.py` (entry names in docs),
  `src/calu/db/place.py` (`write(..., admin_password=)`).
- Migration of existing installations (in `calu setup` and `calu db migrate`, or a
  `calu credential migrate` run by both): if the store has `admin` (the owner login) and/or
  `calu_admin`/`session`/`operator`: ask the old admin password once (or none if
  password-free), store `db-admin` = the owner login, set `calu_admin`'s password to the user
  password the person types now (offer to reuse the admin password), store `user`, remove
  `session`, `operator`, `calu_admin`, `admin`. Running sessions keep their open connections
  (they logged in as `calu_session`); new sessions use `user`. backtester1 is root mode with
  typed passwords (the owner types them; never write them into a file): test the migration
  there with the owner, it needs the session restart anyway.

## Documentation to rewrite (simplify, owner's writing rules)

- `DOCUMENTATION.md`: "Install and set up" (Step 2 table of two passwords), "Credentials and
  tokens" (The three logins table, the store, `--no-password` table), the glossary row
  "Admin, operator, session (credentials)" (line ~178), the root-mode quick start, `calu web`
  "Signing in", every "operator credential"/"admin password"/"session password" mention
  (~49 + ~104 + many), Troubleshooting rows, the command table's "needs" column (3141ff).
- `README.md` (install block, passwords), `docs/TESTING.md` (database tests, manual checklist
  items naming the passwords), `docs/CONFIGURATION.md` (regenerate if config docstrings
  change), `AGENTS.md` (module table: roles, credentials), `uitest/README.md` and
  `uitest/setup-demo.sh` (the Cua desktop stores `admin`, `calu_admin`, `session` entries),
  `uitest/setup-real.sh`, `uitest/rootbox`, `extensions/vscode` README/spec
  (`agent/records/notes/vscode-extension-spec.md` §8/§10 "the one password").
- `PLAN.md`: a new decision row (D265?) that supersedes D48 (operator login), the session
  login parts of D20/D205/D213/D228/D249 for the user's own sessions; REQUIREMENTS: none
  changed, maybe note the owner's words.

## Tests

- Unit: `tests/unit/test_cli_and_setup.py`, `test_cli_commands.py`, `test_root_client_login.py`,
  `test_credentials.py`, `test_ide_bridge.py`, `test_preflight.py`, `test_web_gate.py`,
  `test_doctor*.py`, TUI admin tests. DB (`CALU_TEST_DSN=postgresql://calu_owner:calu-dev@127.0.0.1:55490/calu`,
  container `calu-prepush-pg`, run sequentially): `tests/db/test_cli_setup.py`,
  `test_schema.py`, `test_roles*.py`, `test_web.py`, `test_ide_bridge_db.py`,
  `test_models_command.py`, session tests that log in as `calu_session`. Root layer:
  `tests/root/` in the rootbox (`docker exec calu-rootbox rootbox-tests`). VSCode
  integration in Docker (`extensions/vscode/scripts/test-in-docker.sh`), Cua desktop
  (`uitest/setup-demo.sh` needs updating first) for VSCode, PyCharm, the web pane.
- New tests: setup of a local database with one password (owner and user equal); with an empty
  password (no prompt anywhere: `calu run`, `sessions`, `config`, `web` signs in by token);
  an existing server with a DB user; the migration of a store with `admin`+`session`+
  `operator`; `calu credential` no longer knows `operator`; preflight warns about an empty
  user password with the sandbox off.

## Risks and checks

- Renaming the database role `calu_admin` -> `calu_user` would be nicer for the docs, but
  `ALTER ROLE ... RENAME` clears an MD5 password and touches every grant/policy name; keep the
  role name, call it "the user login" in text. Decide again only if the owner asks.
- With one login, a leak of the running session's login (process memory, a log) now also
  allows approving escalations and changing settings. calu keeps it out of Claude's reach as
  before (`run.harden_process` makes the process non-dumpable, no login in Claude's
  environment); note this in the decision row.
- An empty user password with the sandbox off in the user's own account lets Claude's
  commands use the login (self-approval). Allowed by the owner ("for testing"); `calu doctor`
  and the start-up warnings must say it plainly.
- The JetBrains plugin and VSCode share the unlock logic; change both and their tests together.
- Keep `calu credential set` able to import a connection string for `user` (remote machines).

## Order of work

1. Read D48, D205, D213, D216, D228, D230, D249 in `PLAN.md` and the setup/credential code above.
2. Migration 0043 + `setup.py` + `roles.py`; DB tests green.
3. `cli.py`: entries, setup, the login choice for every command, `cmd_web`; unit tests green.
4. `ide/access.py`, `tui/adminaccess.py`, `doctor.py`, `preflight.py`, `web/app.py`.
5. VSCode and JetBrains unlock (`unlock.ts`, Kotlin port) and their tests; rebuild the pane
   bundle (`npm run build:pane`).
6. The migration of existing stores; try it on a copy of a store, then in the Cua desktop.
7. Documentation and the PLAN decision; `uitest/setup-demo.sh`.
8. Full test run (unit, DB sequentially, browser, VSCode Docker, rootbox), push, check CI,
   then help the owner migrate backtester1 and his WSL machine (he types the passwords).
