# calu: implementation plan

Status: phases 1-4 and 7 done (2026-11-09; see §12 and `HANDOFF.md`). This file holds the decisions,
the architecture and the order of work, derived from the owner's requirements in
[REQUIREMENTS.md](REQUIREMENTS.md). Decisions carry a date; open questions are listed in §11.

## 1. Goal

calu runs Claude Code sessions **unsupervised for long stretches** (e.g. overnight) on Linux
machines, under the control of LLM **checker agents** and humans:

- Claude Code works in its **auto mode**. Every tool call is additionally checked by calu's
  checkers and rules; a call runs only if both approve (§2, D5).
- Checkers can allow, deny or **escalate** a call. An escalation pauses the session until a human
  resolves it, from any connected client or directly in the database.
- calu runs as a background process (e.g. in tmux), independent of any IDE. Several user
  interfaces can attach to the same session at the same time.
- All sessions report to one **PostgreSQL database**. The database holds the configuration, the
  checker definitions, the full step history and the open escalations, so that one person can
  watch and unblock many sessions ("herd management").
- Nothing that steers or bypasses the checkers is within reach of Claude Code or of the commands
  it runs.

calu is the successor of stay-in-the-loop (sitl), which runs inside VSCode for one user. calu reuses sitl's core (§9). Whether sitl continues
alongside calu is decided later (§11).

## 2. Decisions

| # | Decision (date) |
|---|---|
| D1 | **Python 3.14, managed with uv** (2026-11-09). First deliverable: a package installable with `uv tool install`, providing one `calu` command. A single-file binary (PyApp or PyInstaller) was considered and dropped (D41); a port to Go or Rust stays possible once calu works. |
| D2 | **Clients connect over WebSocket with JSON-RPC** (2026-11-09), the front-end-neutral protocol of sitl extended for many clients (§6). |
| D3 | **PostgreSQL is the central store** (2026-11-09): configuration, security settings, checker definitions, session and step history, escalations. No token-level streaming goes into the database. |
| D4 | **The sandbox boundary encloses the tool calls, not Claude Code** (2026-11-09). calu and the Claude Code process form the trusted zone; every command Claude runs is executed in a sandbox that cannot reach or modify the trusted zone. The mechanism is open (§4.4). |
| D5 | **Checkers AND Claude Code's auto mode** (2026-11-09, verified in sitl's `spikes/spike_auto_mode.py`; revised 2026-11-09 on the owner's instruction): running in the auto mode is the intended way to use calu, because a step then needs the checkers **and** the auto mode classifier, but calu does **not** set the permission mode. Claude Code's own default is the auto mode, so choosing another mode is a deliberate act of the user, in Claude Code's settings or in `claude.permission_mode` (None by default = leave it alone). The hook adapts to whatever mode is in effect: it reads the mode from its own input, answers a PASS with no decision while that mode is "auto" (so the classifier decides as well), and otherwise answers "allow", so the checkers decide alone. DENY answers "deny", ESCALATE answers "ask" and goes to calu's `can_use_tool`. |
| D6 | **All configuration and security settings live in the database** (2026-11-09). Securing the database and the calu process secures everything that could let an agent bypass the checkers. Nothing that steers the checkers is read from files the agent can write. |
| D7 | **Linux only** (2026-11-09): the machine where Claude Code and calu run. Clients may run anywhere. |
| D8 | **Password-protected start, credentials dropped before Claude starts** (2026-11-09, details in §5). |
| D9 | Claude Code authenticates as it is set up on the machine (claude.ai login, AWS Bedrock, Vertex, ...); calu does no billing check (taken over from sitl). |
| D10 | **sitl code is ported into calu** (2026-11-09) and published under calu's GPL-2.0, with its unit tests. sitl's condensed third-party documentation (`.claude/skills/`) is not copied; calu's documents link to the original sources instead. |
| D11 | **sitl's coding rules apply to calu** (2026-11-09): Python first, contract docstrings in Google style enforced by ruff (`D`, `DOC`), plain technical language. They are the owner's rules from sitl's `CLAUDE.md` and are listed in `AGENTS.md`. |
| D12 | **Credential store** (2026-11-09, answers the first half of Q3): the database credentials (connection URLs) are kept encrypted in `~/.config/calu/credentials.json` (mode 0600). Each entry is encrypted with AES-256-GCM under a key derived from a password with Argon2id. Entries: `admin` (database owner, for `db init/migrate`, `setup`, configuration changes) and `session` (role `calu_session`, for `calu run`). The sandbox (D4) hides `~/.config/calu`. A per-session database role stays open (Q3). |
| D13 | **Database access with psycopg 3 and plain SQL migrations** (2026-11-09): no ORM. Migrations are numbered SQL files in the package (`calu/db/migrations/`), applied in order and recorded in `schema_migrations`. psycopg's async connections support `LISTEN/NOTIFY` directly. |
| D14 | **`calu db up` creates the local database owner** (2026-11-09): on the first start it generates the PostgreSQL owner password, stores it as the `admin` credential under an admin password the user chooses, and starts the container with it. The password is never written in plain text. |
| D15 | **Isolation of tool calls: Claude Code's own sandbox, configured by calu** (2026-11-09, answers Q1 on the evidence of `spikes/RESULTS.md`, CLI 2.1.319): calu passes the sandbox settings through `--settings` (`enabled`, `failIfUnavailable: true`, `allowUnsandboxedCommands: false`, `filesystem.denyRead` for secrets including `~/.config/calu`, Claude's credentials, `~/.ssh`, cloud credentials and the Docker socket, `allowedDomains` per project) and loads only the setting sources it chooses. Sandboxed commands get their own PID namespace (no access to the Claude Code process), cannot write `~/.claude`, `.claude/settings*.json` or `.git` hooks/config, and leftover processes die. Requires `bwrap` and `socat`. Fallback: `CLAUDE_CODE_SHELL_PREFIX` with a bubblewrap wrapper (verified to route every Bash command, including background ones). Edit/Write/Read still run in the Claude Code process and stay covered by the hook and the deny lists. |
| D16 | **Checkers on the Anthropic subscription through `claude -p --output-format json --json-schema`** (2026-11-09, answers Q7): schema-valid `structured_output` in 6 of 6 runs, 2.5-4.4 s per check with haiku; run with `--tools ""`, `--no-session-persistence` and stdin closed. |
| D17 | **Checker API keys stay out of the database** (2026-11-09): calu reads them from its environment and `~/.config/calu/.env` and hides them from Claude Code (as in sitl). |
| D18 | Replaced by D28 (2026-11-09). Originally: **a decision that reaches the session from a client is recorded as a cancellation with a note**. Only `calu_operator` may call `calu_resolve_escalation`; the session role may not, so that a session which somehow lost its credential cannot approve its own steps (§4.1 point 5). A client attached to the session process therefore decides in the session, which then cancels the row with a note that holds the decision and the client. The audit trail of such a decision is weaker than that of an operator's resolution; whether clients should instead resolve escalations with their own operator credentials is Q8. |
| D24 | **Third-party user interfaces are served through ACP, not through a plugin per editor** (2026-11-09, §6.1): a `calu acp` adapter speaks the Agent Client Protocol on stdio, is spawned by the editor and attaches to a running calu session over calu's own protocol. One adapter reaches JetBrains Air, Zed, Neovim, Goose and every other ACP client, and ACP's baseline `session/request_permission` is exactly calu's escalation. ACP is the floor: calu's own WebSocket protocol stays for what ACP cannot carry (the checker verdicts as structure, the herd overview, editing the rules). The interactive-plugin mode of §4.5 is **not** built (Q10 answered: no). |
| D23 | **The tool gate stays an in-process hook of the Agent SDK** (2026-11-09, verified in `spikes/RESULTS.md`, "Hook timeouts"): a hook that does not answer within its timeout stops the step there — the call is not run even with `permission_mode: "bypassPermissions"`, and Claude retries it once and then reports it. A shell hook of a plugin does the opposite (its timeout discards the output and the call continues through the normal permission flow), which is why the gate does not move into a plugin (§4.5). The PreToolUse timeout is 900 s, just above the checker deadline of 840 s, because no human wait happens in that hook; the two hooks that review a subagent's result may wait for a human and keep 24 hours. |
| D21 | **Four decisions: PASS, ALLOW, DENY, ESCALATE** (2026-11-09, the owner's wording). PASS is the normal answer: the checker has no objection and Claude Code's auto mode decides as before (the hook returns no decision). ESCALATE asks a human. ALLOW and DENY are deliberate exceptions: ALLOW lets the step run whatever auto mode would say (the hook answers "allow"), DENY stops it. Without auto mode (a model that has none, or a user who chose another mode), PASS has the same effect as ALLOW, because then the checkers decide alone. The allow lists of the rules produce PASS, not ALLOW: they say "no check needed", not "run this whatever the harness thinks". This replaces the three-valued decision of sitl, where ALLOW already meant "no decision" under auto mode (D5). |
| D22 | **Checkers may read files** (2026-11-09): a checker agent gets read-only tools for the files of the workspace, so it can find out what the code actually does instead of guessing or escalating. It gets nothing else: no writes, no commands, no network. |
| D29 | **The database identifies the process and the agent tree** (2026-11-09, the owner's requirement): many calu processes wrap many Claude sessions, each with its own subagents, so a row has to say where the process is and who is whose parent. `sessions` carries the host, the OS user, the project directory, the **pid** and now the **address** the process listens on ("ws://host:port" or a Unix socket path; migration 0008), next to Claude's own session id. The `agents` table holds one row per subagent of a session with `parent_agent_id` (NULL = the session's main agent, as a NULL `steps.agent_id` already means), its `sdk_agent_id`, description, type, background flag and status. The session fills it from the Agent tool call that starts a subagent (the call's id is the subagent's id, its caller is the parent) and from the runner's `agents.update` events. |
| D30 | **Every escalation can be decided from the database alone; the session's WebSocket is the convenient path, not the only one** (2026-11-09, corrected 2026-11-10 on the owner's instruction). The invariant: an operator who can reach the database can see why a step was escalated and decide it **without connecting to the session process** — which may run on another host, be unreachable, or have ended. `escalations.reason` and `escalations.context` therefore hold what a decision needs: the rendered step, the tool input (the command line, paths relative to the workspace), the file an edit would change, the changeset of a commit, the questions of an `AskUserQuestion` call with their options (D35), the conversation as the checkers see it (narrated, D3) and the folded summary. `calu escalations` lists them, `calu escalation <id>` prints all of it, `calu resolve <id> allow|deny|answer` decides, and the waiting session notices through the `escalation_resolved` notification **and** a poll of the row every second, so a lost notification only costs a second (D27's wait uses both). What the database does **not** hold is the live stream — token-by-token text, thinking, tool results as they arrive (D3); that is what attaching to the session is for. A client that *is* attached decides through the session instead, and the session records it for the client (D28), because a client holds no database login. A direct database login also covers what no single session can answer — the sessions and escalations of other hosts, and every **write** to the configuration, the rules and the checker instructions (§6.2). A client needs the session's address (D29) and a client token, and since migration 0009 a session accepts **every non-revoked token of its own session**, not only the one it printed: `calu token mint SESSION` creates one, `calu token list` shows them without the tokens themselves, `calu token revoke ID` ends one. So an editor, a local command or a remote interface keeps a token in its own configuration and survives a restart of the session. The session role may not read `client_tokens.token_sha256` (D28), so it presents the hash of the token a client sent and `calu_client_token_ok` answers whether it belongs to that session and is not revoked, stamping `last_used_at`; the role can therefore confirm a hash it was given but not enumerate the tokens, and it no longer needs UPDATE on the table, so it cannot un-revoke one. The printed token is checked first and without the database, so attaching still works while the database is unreachable. |
| D28 | **A session records the decisions of its own clients** (2026-11-09, the owner's answer to Q8, replaces D18): a client of a session process — `calu attach`, the ACP adapter, an IDE panel — has no database login; it authenticates to the session with a client token, and the session writes its decision to the database. `calu_client_decision` (migration 0007) resolves only an open escalation **of that session**, requires the SHA-256 of one of its client tokens, and records `resolved_by = 'client:<name>'`. The token hash is a capability the session process holds in memory: the SELECT privilege of `calu_session` on `client_tokens` is narrowed to the id, so the database credential alone cannot forge a decision. `calu_resolve_escalation` (any escalation, `resolved_by` = the operator) stays operator-only and is what a herd interface with its own login uses. What the agent still cannot do (§4.1 point 5): approve a step by reaching the database, because it has neither the credential nor the token — and an agent that owns the calu process could answer in-process anyway, which no database rule can prevent. Narrowing this further (one database role per session, row-level security) is Q3. |
| D27 | **An escalation nobody answers stops the session, which stays resumable** (2026-11-09, the owner's decision on Q6): `escalation_timeout_s` (default 8 h, 0 waits without a limit) bounds the wait. On the timeout the step is denied, the escalation row is cancelled with the reason, and the session stops, so the machine and the Claude session are free again. Claude Code's own session id is therefore written to the `sessions` row **as soon as Claude Code reports it** (at the start, not only after the first turn), so that `calu run --resume <id>` takes the conversation up again and `session.history` finds it; `calu sessions` prints it. |
| D26 | **A changeset checker reviews the whole change before a commit** (2026-11-09, the owner's wording): a step checker judges one edit with limited context, which is the right altitude for style, documentation and local design and the wrong one for "is this change correct and complete?". The commit is the moment the change exists, so a Bash call that creates a `git commit` is routed to `Route.CHANGESET` instead of the command checker (the deny lists still run first). calu collects the diff itself — `git diff --cached`, or against HEAD for `-a` and against `HEAD~1` for `--amend` — and gives it to the changeset checker next to the commit command and its message. A DENY undoes nothing: it stops the commit and tells Claude what to fix first. The checker type is `changeset` (route `changeset`, migration 0006); a database without it, or an administrator who disables it, leaves commits to the command checker. Verified with the real CLI: a PASS let a commit through and recorded a `checker_runs` row, a DENY stopped it and Claude reported the reason without retrying. |
| D25 | **The checkers' tools stand on Pydantic AI's workspace API** (2026-11-09): the checker agents get the `LocalWorkspace` capability with `read_only=True` and `authority="none"`, so the library pins the run to the project directory, refuses writes and commands, and marks the directory's files as carrying no authority (the agent under review writes them). calu writes only the tool functions that the library has no opinion about — `read_file(path, start_line, end_line)`, `find_files(pattern)`, `grep_files(pattern, path_glob)` in `calu.checkers.tools` — and adds what the library does not know: the read deny lists, bounds on every answer, and the framing that a file's content is data. Pydantic AI 2.66 ships no file tools of its own (`common_tools` are network searches, `native_tools.FileSearchTool` is provider-hosted), so there is nothing to reuse there; a language server belongs in the same toolset, through `pydantic_ai.capabilities.MCP` or as further functions. |
| D20 | **One token per session for its clients** (2026-11-09): `calu run` issues a client token, prints it once and stores only its SHA-256 (`client_tokens`). A client sends it in `initialize`; every other request is refused before that. The endpoint binds 127.0.0.1 by default, so remote access goes through an SSH tunnel or a TLS proxy until Q4 is answered. `--socket [PATH]` serves a **Unix socket** instead (mode 0600, under `$XDG_RUNTIME_DIR` by default): the token still applies, and the file system becomes a second layer, since a loopback port is reachable by every process of every user on the machine while a 0600 socket is not. A path longer than about 100 bytes is refused with its length, because the kernel's limit is 108 and `AF_UNIX path too long` explains nothing. |
| D19 | **One database connection per session, serialized and shielded** (2026-11-09): `calu run` opens two connections before it wipes the credentials (one for queries, one for notifications) and cannot open more later, because reconnecting would need the password again (§5). psycopg allows one statement at a time per connection, and a session writes from several tasks at once, so every statement goes through `calu.db.Db`: a lock serializes them, and each statement is shielded from cancellation (bounded by a timeout), because a task cancelled in the middle of a statement leaves the connection unusable for every other task. |
| D47 | **The configuration documents itself, and a test keeps it honest** (2026-11-10): `Config` has 54 settable keys and the README named about ten of them, so most of what a user can set was discoverable only by reading the model. The docstring **under** a field is now its description (`ConfigModel` sets `use_attribute_docstrings`), every field has one, and `calu.configdoc` generates `docs/CONFIGURATION.md` from the model: key, type as the user has to type it, default, and what it does. `tests/unit/test_configdoc.py` regenerates the file and fails on a difference, and fails as well when a field has no docstring or is missing from the reference -- so a new setting cannot be added without documenting it, and the reference cannot describe a setting that no longer exists. The same descriptions reach two places that already looked for them: the web interface's settings form renders `info.description` next to each control (it was simply always empty), and the JSON schema of the protocol now carries them for non-Python clients. `hidden_env` is deliberately left out of the reference, because listing it as an ordinary value invites someone to put a key in it; it stays documented where it belongs, next to the checker keys. |
| D46 | **A fork rewinds the conversation, not the files** (2026-11-10, answers Q14, measured in `spikes/RESULTS.md`, "What `rewind_files` puts back"): the SDK's `enable_file_checkpointing` plus `rewind_files` tracks only what Claude Code's own edit tools changed. Measured with the real CLI: a file the Write tool overwrote came back, a file a Bash command overwrote did **not**, and a file a Bash command created was **not** removed -- with no error to say so. For calu that is the worst of the three possible states: a tree where some files are back and some are not looks consistent to the human who reviews the fork, and is not. So a fork stays what D36 built, a branch of the conversation, and both the CLI's help and the web interface's fork card say that the files are left alone. If file rewinding is wanted later, it belongs on D31's snapshots -- a git scratch index that measures and reverts everything a step wrote, commands included -- and not on Claude Code's checkpointing; that would make `changes.enabled` a precondition of the feature. |
| D45 | **Proposed default for a new project: the "proxy" policy with a starter allow list** (2026-11-10, answers Q5 as a proposal the owner can overrule with one flag). A new project used to get "full": every domain through the sandbox's proxy, which is the reach a command has without calu at all. The careful alternative had a trap -- "proxy" keeps the configured `allowed_domains`, which is empty by default, so a bare "proxy" default would have meant the same as "none" and every package install in a new project would have failed. The default therefore has two halves: `projects.network_policy` defaults to "proxy" (migration 0014, for rows that do not name one; existing projects keep what they have), and `calu project add` **writes** `config.DEFAULT_ALLOWED_DOMAINS` into the new project's settings -- package registries for Python, JavaScript, Rust and Go, git over HTTPS, Debian and Ubuntu packages. It is written rather than applied at load time so that `calu config get --project` shows it and `calu config add|remove claude.sandbox.allowed_domains` edits it (D44); the list is not the default of `SandboxConfig.allowed_domains`, because a session whose project says nothing, or that runs without a project, must still reach nothing. `--network full` restores the old behaviour and seeds nothing. Claude Code itself runs outside the sandbox (D4), so its API traffic is not affected. The start-up warnings now also say when a command can reach **nothing**: that is the safe end, but it explains every failing download, so a session stuck on an install says why instead of leaving it to be guessed. |
| D44 | **A list setting is edited one entry at a time, relative to what the session really uses** (2026-11-10, closes a gap the handoff listed): `calu config set claude.sandbox.deny_read '[...]'` replaces a whole list, which is wrong for the lists that matter -- the sandbox's read deny list and its allowed domains -- because a user who adds one path means "as well as the others", and the others are usually the built-in defaults rather than stored values. `calu config add|remove <key> <entry>` starts from the **effective** value (`admin.effective_value`: the stored value where there is one, else the default of `Config`), changes one entry and writes the result back through `set_setting`, so the defaults survive and the result is validated like any other change. Adding an entry twice changes nothing; removing an entry that only the defaults hold is allowed, because that is what "remove it" means; a key that is not a list, or not a key at all, is refused with what it is instead. The missing VALUE is caught before the admin password is asked, as for the other commands. |
| D43 | **A change of a running session's settings is on the record, with the name of whoever asked** (2026-11-10, found while building D42): `auto_escalate.set` is the one setting a client may change through the protocol, and switching a category *off* weakens the session -- steps the checkers approved stop waiting for a human. Nothing recorded that. The audit log is fed by triggers on `config`, `rules` and `checker_types`, and a session-local change touches none of them, so an operator reading the history afterwards could not tell that every edit had been waiting for a human until somebody switched it off. Migration 0013 gives `audit_log` the session the entry belongs to (NULL for the project-wide changes the triggers record) and adds `calu_session_setting_change`, which a session calls for itself; `calu_session` still may not read or write `audit_log` directly, so it can neither forge an entry for another session nor for the project's configuration. `Session.set_auto_escalate` writes the entry **before** it changes the setting and refuses the change if the write fails, because a change nobody can see afterwards is worse than a refused request; a request that changes nothing is not recorded. A settings reload records itself as the actor. `herd.setting_changes` reads them back and the web session page shows them, so the record is visible where the session is reviewed rather than only in SQL. |
| D42 | **ACP's session mode is calu's auto-escalation setting, offered through both of ACP's APIs** (2026-11-10, phase 7): an editor shows a picker for how much the agent may do on its own, and calu's equivalent is `AutoEscalate` -- three independent switches that make every command, every edit or every subagent result wait for a human whatever the checker decided (D21, D38). ACP has two APIs for this and they fit the three switches differently. **Session config options** (`configOptions`, `session/set_config_option`, `config_option_update`) have a boolean option type, which is an exact fit -- one option per switch -- but an agent may send one only to a client that advertised `session.configOptions.boolean`. The older **session modes** (`modes`, `session/set_mode`, `current_mode_update`), which config options supersede and which the specification says will be removed, are one value out of a list, so the eight combinations have to be flattened: `calu.acp.modes.LADDER` is four modes of growing strictness (`checkers`, `edits`, `writes`, `everything`), each a superset of the one before. A combination that is not on the ladder is reachable with `calu config set` and the web interface, so a running session need not be on it; the mode is then reported as `custom`, which is advertised only while it is the current mode and is refused as a target. calu answers **both** APIs and keeps them in sync, as the specification asks during the transition, because a client may understand only one. A client never changes the setting locally: `session/set_mode` and `session/set_config_option` become `auto_escalate.set` on calu's own protocol, the session applies it and announces `auto_escalate.changed` to **every** attached client, and each adapter turns that back into its ACP notifications -- so a change made in one editor, in `calu config set` or in the web interface reaches the others instead of leaving them showing a mode the session does not use. |
| D41 | **Recommendation, the owner's to accept (Q15): no single-file binary; the wheel installed with `uv tool install` is the shipping format** (2026-11-10). D1 left a PyApp or PyInstaller build "for later". REQUIREMENTS 21 and soft goal 2 ask for one, so this is a recommendation against it and not a decision taken on my own. A frozen binary removes the Python interpreter, which is the cheapest of calu's three prerequisites: a session also needs a reachable PostgreSQL and the `claude` CLI (Node), and neither can be frozen into calu's own executable. Against that it costs a build per platform and libc (CI matrix, glibc vs musl), and it puts the package data calu reads at runtime -- migrations, default checker instructions, web templates and static files -- behind a frozen-import layer that `importlib.resources` reaches differently in each freezer. The installed wheel was verified by hand instead: `uv build` produces 138 entries including all 13 migrations, 16 default instruction files, 13 templates and 5 static files, and in a throwaway virtual environment `calu --version`, `calu protocol` (51 definitions) and `calu doctor` run from `site-packages` with that package data resolving correctly. `calu doctor` covers what a binary was meant to make unnecessary: it names every missing prerequisite instead of failing late. A port to Go or Rust (D1) would bring a single binary with it. Nothing in the code depends on the recommendation being accepted: if the owner wants the binary, it is a build to add, and `tests/unit/test_packaging.py` already pins what it would have to carry. |
| D40 | **The folded conversation is editable, because it is what the checkers read** (2026-11-10): everything older than the unfolded turns reaches a checker only as the chat summary the summarizer model wrote, so a wrong summary misleads every later check — and there was no way to correct it. `calu summary <session> [--edit]` shows it and edits it in `$EDITOR`, and the web interface has the same form (`/sessions/{id}/summary`); both write through `admin.set_chat_summary`, which refuses an empty text (that would hide the folded turns silently) and sends `config_changed`. A running session re-reads the summary on that notification, next to the settings, the rules and the checker instructions (`Session.reload_summary`), so a correction reaches the next check. The number of covered turns is left as it is, so the next folding continues where the summarizer stopped. This is sitl's "CHAT_SUMMARY.md is a file you can edit" in calu's shape: a row an operator can reach from anywhere, not a file in the project that the agent could rewrite. |
| D39 | **A project's network policy shapes the sandbox its commands run in** (2026-11-10, the mechanism behind Q5, measured in `spikes/RESULTS.md`, "The sandbox's network policy"): `projects.network_policy` existed from the first migration and nothing applied it — an inert column whose default said "full" while the sandbox blocked everything. It is now read by `ConfigStore.load` and applied to `claude.sandbox` through `SandboxConfig.with_policy`: "none" clears the allow list (nothing is reachable), "proxy" keeps the configured `allowed_domains`, and "full" sets the wildcard `["*"]`; `denied_domains` is kept in every case, because it overrides the wildcard and is how "everything except these" is written. The measurements that made this possible: the sandbox runs a filtering HTTP proxy and exports it as `HTTPS_PROXY`, raw TCP is blocked whatever the policy says (so a program that ignores the proxy variables reaches nothing), and the lists are domain lists, not address lists. `calu project add --network` sets the policy and `calu project network --name <p> --network <policy>` changes it; a running session applies a change when it next starts, because the sandbox is built when Claude Code starts. The start-up warnings now say when a session may reach every domain. Still the owner's to decide (Q5): the **default** for new projects, which stays "full". |
| D38 | **A confirmation the harness asks for is an escalation, whatever the checkers said** (2026-11-10, the owner's observation, measured in `spikes/RESULTS.md`, "Does auto mode ask"): Claude Code's auto mode does not only allow and deny, it can ask. calu's PASS leaves the decision to it (D21), so such a step would stall if calu ignored the question. It does not: the SDK's only channel for a permission question is `can_use_tool`, which calu implements as the wait for a human, so the step escalates even though no checker objected. The reason of that escalation is `HARNESS_ASKED` and says exactly that, because a human who reads "approval required" would look for a checker verdict that does not exist. Two measurements belong with this: in **headless** mode the classifier never asked in four attempts — it allowed `ls`, a write **outside** the project directory, `rm -rf` of a directory and a first fetch of an unseen domain — so the harness layer is more permissive there than the confirmation dialog of the interactive terminal, and calu's own deny lists are what actually stop such a step. That is an argument for the rules, not against the second opinion: it is the same conclusion as D5, from the other side. |
| D36 | **A session can be forked at an earlier turn, and calu records the uuids it needs** (2026-11-10, answers Q13, measured in `spikes/RESULTS.md`, "Forking a session at an earlier step"): the Agent SDK takes `resume` with `fork_session=True` to branch at the end, `resume_session_at=<transcript entry uuid>` to branch earlier, and `resume_drops_turn=<uuid of the prompt whose turn is discarded>` as a guard that refuses a wrong point with a documented message. The spike forked a two-turn session after turn 1: the fork remembered the kept turn, did not know the dropped one, and ran under a new session id, leaving the original intact. Two findings shape the implementation: a user-role transcript entry is **not always a prompt** (tool results share that type, and picking one is what the guard refuses), and the fork point is the **last entry of the kept turn**, which is the entry before the next prompt. calu therefore records, per turn, the uuid of its prompt and the uuid of the turn's last assistant entry (migration 0012), because the runner already passes `replay-user-messages` and sees `UserMessage`; that is more reliable than counting prompts in the transcript afterwards, since calu's own injected messages are prompts too. `calu run --resume <id> --fork-before-turn <n>` starts the fork, and the session page offers the command for each turn, as it already does for attaching — the web server does not start sessions itself (it holds no session credential, and the session may be on another host). Putting the **files** back as they were at that turn is a second step: it needs `enable_file_checkpointing` and `ClaudeSDKClient.rewind_files(uuid)`, which the spike did not try (Q14). |
| D37 | **The checkers may run on an Anthropic API key while the main chat keeps the subscription** (2026-11-10, the owner's setup: an Ultra subscription includes API credits): this works with what calu already does and needed only measuring. `anthropic` is in `PROVIDER_KEYS_BY_PREFIX`, so `ANTHROPIC_API_KEY` counts as a checker key: calu reads it from `~/.config/calu/.env`, gives it to the checker models, and **blanks it** for Claude Code (`SdkRunner._claude_env`). Measured with the real CLI: with `ANTHROPIC_API_KEY=""` in its environment Claude Code answers normally, so the subscription stays in effect; a non-empty invalid key did not switch it to the API either, but calu does not rely on that. So a checker model of `anthropic:claude-haiku-4-5` next to `claude.model = "opus"` is a supported combination, as is Bedrock for the checkers (`bedrock:...`, which needs no key here) or for Claude Code (`claude.use_bedrock`). The start-up warnings already name a configured provider whose key is missing, including this one. |
| D35 | **A question from the agent is an escalation, and its answer travels back as the message of a refusal** (2026-11-09, the owner's reading, verified in `spikes/RESULTS.md`, "The agent's own question"): `AskUserQuestion` reaches the PreToolUse hook with the whole question in its input (`questions[].question`, `header`, `options[].label`/`description`, `multiSelect`), so a client can show the real choices. Claude acts on the text that comes back in a refusal: in the spike its final sentence named the answer that `can_use_tool` had put in its denial message. Answering from the **hook** instead made Claude ask a second time, so the channel is the one a human decision already uses (`Session.ask`). calu therefore routes the tool to `Route.QUESTION`, which escalates without a checker (there is nothing to judge — only the human knows the answer), stores the questions in the escalation's context and in the `escalation.request` event, and returns the chosen answer as the message. Such an escalation ended neither in an allow nor in a deny, so `escalations.state` gains **`answered`** (migration 0011) and both resolution functions accept it; `calu escalations` and the web interface show it as what it is. |
| D33 | **What the web interface takes from the other agent UIs, and what it refuses** (2026-11-09, researched on the owner's instruction: Claude Code on the web and its `claude agents` view, Orca (onorca.dev), opencode `opencode web`, Goose). The pattern all of them converge on is **grouping by what needs a human**: Claude Code's agent view groups sessions into Pinned / Ready for review / Needs input / Working / Completed with counts in the header and filters like `s:blocked`; Orca has a board with "Needs You / Working / Done / Idle", status glyphs, a bell with an unread count and a phone companion. calu adopts that ordering (D34), because its own escalations are exactly "needs you". Also adopted: a **message queue** a human can see, reorder and take back while the agent works, with Orca's distinction between queueing and "Steer" (which calu already has as `chat.send delivery="inject"`); **stop and interrupt** from the UI; a **turn-level list of changed files** rather than a diff viewer of calu's own; **answering a question** of the agent as a card with numbered choices (Orca renders `AskUserQuestion` that way, opencode has a `question` permission and an environment flag to enable the question tool for non-CLI clients, which confirms the owner's reading that a question is an escalation of a kind); and a **settings dialog** over the values that already live in the database. Refused: Orca's launch defaults (`--dangerously-skip-permissions` for Claude, `--yolo` for Gemini) — calu exists to do the opposite, and that contrast belongs in the README; a git browser of calu's own (links into the project's own browser instead, `calu.web.git`); a worktree per task; and "always approve" as a session-local memory, because calu already has a better home for it — a rule row that an operator can read and revoke. Still open and needing a spike before it is promised: **fork a conversation** and **rewind to an earlier step** (Claude Code has "Rewind to here" and `/fork`, Orca "Fork from this response", opencode `/undo` and `POST /session/:id/fork`). calu can resume a Claude session by its id (D27), but resuming it *at a chosen message* is not something the Agent SDK is known to offer, so Q13 asks it. |
| D34 | **The web interface is a client of both sides** (2026-11-09, §6.2 applied to phase 6): the pages that read the fleet, the history and the configuration talk to the database with the `admin` credential, and the live view of one session talks to **that session's own WebSocket**, because only the session process holds the live state and can hold a tool call. The server does both: it mints a client token for the session it watches (D30), connects to the address the session recorded (D29) and relays the event stream to the browser over a WebSocket of its own, so the browser never needs a token and the session needs no new protocol. What that unlocks is what the other tools do from their UIs: stop, interrupt, a visible message queue, "steer" into the running turn, the live thinking as it streams, and a conversation with a **background subagent** (`chat.send {to: <agent id>}`, which the protocol already carries). The fleet page groups the sessions as D33 says and leads with what waits for a human. |
| D31 | **The write set of a step is detected with a git snapshot in calu's own state directory** (2026-11-09, the owner's decision): `Edit`, `Write` and `NotebookEdit` reach the hook with their exact content, but a Bash command that edits files (`sed -i`, a formatter, codegen, a script) is only *described* by the command checker, so calu never saw what it actually wrote. calu therefore snapshots the project directory before and after every Bash step with git plumbing: a git directory of its own (`$XDG_STATE_HOME/calu/snapshots/<session>`, **outside** the project and on the sandbox's `denyRead` list), used with `--work-tree <project>`; `git add -A` plus `git write-tree` gives a tree object per snapshot, `git diff-tree -r --name-status` the write set, `git diff` the patch a checker reads, and `git apply -R` an exact revert whose pre-state cannot be lost, because both trees stay in the object store. Measured on this machine (git 2.43): 107 ms for the first snapshot of a 236-file tree, 2 ms for later ones, 2.5 MB of objects. An external git directory is chosen over the project's own `.git` because it leaves the user's repository without unreachable objects, works for a project directory that is not a git repository at all, keeps the pre-state and the revert source out of the agent's reach, and lets calu decide what is captured: in-tree `.gitignore` files are still honoured (`node_modules` is skipped), while `git add -f -- <path>` force-captures a configured list of sensitive paths that `.gitignore` would otherwise hide (`.env` and the like). Only the `git` binary is needed, which calu already calls (`checkers/changeset.py`), so there is nothing to install and nothing to add to the dependencies; `calu.preflight` checks for it. Three uses, in this order: (1) compare the paths the command checker *declared* with the paths actually written — a mismatch is the dishonesty signal the step checkers cannot otherwise see, and the comparison needs no model call; (2) give the real diff to a checker after the call (PostToolUse, where the subagent-result review already waits for a human if it must); (3) revert on a post-hoc DENY, so the answer is "reverted, and here is why" instead of asking the agent to undo its own write. Limits that go with it: a post-hoc check is strictly weaker than the gate, because side effects (a `curl | sh`, a network call) cannot be reverted, only files; content-identical rewrites are invisible (intended); file modes (the exec bit) and symlinks are covered, ownership and extended attributes are not; and a before/after window is only sound while one step is in flight, so with concurrent background commands or subagents the write set is recorded for the turn rather than the step. Because hashing is the cost, a size and type policy belongs in the first version: a byte cap per step, and big or binary files recorded as path, size and "binary" without content, so no checker prompt ever carries them. The snapshot is **optional and off by default** (`changes.snapshot`, 2026-11-09, the owner's decision): it costs disk and hashing time in every session, while the cheap attribution of D32 needs nothing, so a project turns it on when it wants content and a revert. Built 2026-11-09: `calu.changes.SnapshotStore` (snapshot, write set with sizes and a bounded patch, revert), the configuration section `changes`, migration 0010 (`steps.snapshot_before`, `steps.snapshot_after`, `steps.wrote`) and the session's window around every step the command checker judges — it records the write set and warns about paths the checker's summary did not mention. The review after the call is built too (`changes.review`, off by default and needing `snapshot`): a `PostToolUse` matcher for Bash calls `ToolGate.review_write_set`, the changeset checker reads the patch, and a DENY reverts the change with `git apply --reverse` and tells Claude what was undone and why — or, if the revert does not apply because the files moved on, that it was **not** undone and Claude has to fix it. An ESCALATE asks a human and a denial reverts in the same way. The review records a `checker_runs` row like every other check. Verified against the real CLI (`spikes/RESULTS.md`, "PostToolUse for Bash"). An **overlay upperdir** was measured as the alternative (exact write set, quarantine, bounded writes) and **dropped** (2026-11-09, the owner's decision): it needs the mount namespace, which would displace D15 and mean rebuilding Claude Code's sandbox, its merge is a file-system-semantics job where a bug writes a wrong working tree, a rename of a directory out of the lower layer fails with EIO, and copy-up amplifies space (a 1-byte write into a 200 MB file copied 200 MB). |
| D32 | **Steps carry trustworthy times, and a checker attributes a file change to a step by its ctime** (2026-11-09, the owner's design): `steps` already records when a tool call was created, decided and finished, so the cheap way to tell *which step changed a file* is the file's **ctime**, which an unprivileged process cannot forge — `touch -d 2020-01-01` moves mtime and leaves ctime at the current time (measured 2026-11-09), so `find -newercm` sees a backdated file that `-newer` misses. The checkers therefore get two read-only tools next to the file tools of D25: `step_times` (the recent tool calls with their local and UTC times) and `changed_files` (paths whose ctime falls in a window, with the step that window belongs to). Directories are scanned as well, because a deleted file leaves no inode and shows only in its parent's timestamps. What this gives and what it does not: it is **attribution without content** — it says a step touched a path, not what it wrote, which is why D31's snapshot stays the option for content and revert; ctime records only the **last** change, so a later write erases the evidence of an earlier one (the later write is itself attributed, so hiding a change means hiding it inside a step where a change is plausible, which is exactly what the declared-against-written comparison of D31 catches); overlapping steps (background commands, subagents) widen a window to "one of these steps"; mmap writes may have their timestamp flushed after the step ends; and the filesystem must keep a real ctime with fine granularity, which ext4, xfs and btrfs do and 9p, FUSE and NFS mounts may not. Attribution rests on the clock, so calu watches its own: a task samples `CLOCK_REALTIME` against `CLOCK_BOOTTIME` (boottime, not monotonic, so that a suspended or hibernated host does not look like tampering) and **escalates** when the wall clock moves backwards by more than `clock.max_backward_s` (default 1 s, which is far above the slew of a time-server sync), with the current step if there is one and otherwise as an escalation without a step. A large forward jump is recorded as a warning event, not an escalation, because a first time-server sync legitimately jumps forward. Setting the clock needs CAP_SYS_TIME, which the agent does not have, so this is defence in depth and also catches an administrator or a broken sync silently corrupting the attribution. calu never trusts a time the agent reports; only its own two clocks and the filesystem. |

## 3. Architecture

```
            ┌──────────── clients (any machine) ─────────────┐
            │ TUI (calu attach) · IDE plugin · web UI · herd │
            └──────────────┬─────────────────────────────────┘
                           │ WebSocket, JSON-RPC (authenticated)
┌──────────────────────────┼─────────── trusted zone (Linux host) ──────────────────────────┐
│  calu session process    ▼                                                                 │
│   ├─ bus: state snapshot + event stream to all subscribed clients                          │
│   ├─ session runner: Claude Agent SDK, PreToolUse hook, can_use_tool, auto mode (D5)       │
│   ├─ rules engine (allow/deny lists), checkers, narrator, context builder                  │
│   └─ store: writes steps, checker results, escalations; LISTEN for resolutions ────────────┼──► PostgreSQL
│                           │ stdin/stdout (stream-json control protocol)                    │    (local docker
│  Claude Code process ◄────┘                                                                │     container or
│   │  Edit/Write/Read run here, after the hook allowed them                                 │     server)
│   ▼                                                                                        │
│  ┌──────────── sandbox (D4) ─────────────────────────────┐                                 │
│  │ Bash commands and everything they start               │                                 │
│  │ project dir writable; calu, Claude Code install and   │                                 │
│  │ config, other dirs hidden or read-only                │                                 │
│  └───────────────────────────────────────────────────────┘                                 │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

One **calu session process** runs one Claude Code session (main agent and its subagents). It
is started by `calu run` in a terminal (typically inside tmux) and keeps running when all
clients detach. Several sessions on one host are several processes.

The **database** is shared by all hosts and sessions. Herd tools (CLI, web UI) can work on the
database alone: list sessions, read their history, resolve escalations. A resolution in the
database reaches the waiting session process through `LISTEN/NOTIFY`.

### 3.1 Session states

```
starting ─► idle ◄──► running ─► checking ─► tool_call ─► running
                        │           │
                        │           └─► escalated ─(resolved)─► tool_call | running
                        └─► stopped | failed
```

`running`: Claude works (model turn). `checking`: a checker or the narrator runs for a tool call.
`tool_call`: the tool executes. `escalated`: waits for a human decision. The current state and
its start time are kept in the database (session row) and on the bus.

### 3.2 Checker models

Checkers, narrator and summarizer use Pydantic AI (as in sitl): structured output with
validation and retry, prompt caching, usage limits, fake models for tests. Model strings are
Pydantic AI's (`bedrock:...`, `anthropic:...`, `mistral:...`, `fireworks:...`), stored in the
database per checker type.

- **AWS Bedrock** and the **Anthropic API**: through Pydantic AI's providers.
- **Anthropic subscription:** an API client cannot use the claude.ai subscription; only Claude
  Code can. calu therefore gets a model type that runs the check through Claude Code itself in
  headless mode (`claude -p` with a JSON schema for the structured answer, to verify in a spike),
  on the subscription of the machine. Expected cost: 1-3 s CLI start per check.
- **Fireworks.ai and Mistral:** through Pydantic AI's providers (as in sitl).

## 4. Threat model and isolation

### 4.1 What must hold

The agent (Claude, every command it runs, and anything a prompt injection makes it do) must not:

1. read or change calu's configuration, checker instructions, rules or security settings;
2. read calu's memory, environment or files, in particular the database credential;
3. stop, signal or trace the calu process or the Claude Code process;
4. change how Claude Code reports tool calls to calu, or what calu's answers say: this rules
   out any write access to Claude Code's installation, its configuration files that enable or
   disable hooks and permissions, and its stdin/stdout pipes;
5. approve its own escalations (through the database, the WebSocket bus or a client);
6. start processes that outlive the tool call or the session and act unchecked (`nohup`, `at`,
   `cron`, user systemd units, daemons).

### 4.2 Trust zones (D4)

- **Trusted:** the calu process, the Claude Code process, the database and its credentials.
  Claude Code itself is trusted code; it is not the agent. The agent acts only through tool
  calls, and those are checked before they run.
- **Untrusted:** everything a tool call executes. Since tool commands that run in the same user
  space as Claude Code could modify Claude Code (finding 2026-11-09), they run in a sandbox.

| Tool | Executed by | Protection |
|---|---|---|
| Bash (and everything it starts, also in the background) | the sandbox | sandbox: own PID and mount namespace, writable project dir, calu and Claude Code installation/config hidden or read-only, no access to the Claude Code process |
| Edit, Write, NotebookEdit | the Claude Code process | checked by the hook before they run; write deny lists cover Claude Code and calu files, `.claude/`, `.git/`; Claude Code's own deny rules as a second layer |
| Read, Grep, Glob | the Claude Code process | read deny lists (credentials, keys); reading is otherwise allowed |
| MCP servers, plugins | started by Claude Code | only what calu configures (`--strict-mcp-config`); counted as trusted code |
| Subagents | the same Claude Code process | same hook, same checks |

### 4.3 Remaining risks (accepted or mitigated by checkers)

- The agent can write project files that checkers read as context (AGENTS.md, CLAUDE.md, ...).
  Mitigation: those files go to the edit checker; their changes are recorded per step.
- Access to the Docker socket gives root-equivalent control (including the local database
  container and its environment). The sandbox must hide `/var/run/docker.sock` and
  `~/.config/calu` (D12, D14).
- Network access from the sandbox (exfiltration, downloads). Optional network policy per project
  (none, allow list through a proxy, full).
- The checkers and the auto mode classifier can be wrong; long unsupervised runs rely on both,
  plus the deny lists for known dangerous commands.

### 4.4 Isolation mechanism (open)

The requirement is fixed (4.1, 4.2); the mechanism is decided after spikes. Options, which may
be combined:

1. **Claude Code's own sandbox for Bash** (bubblewrap on Linux, filesystem and network rules
   from settings). To verify: does it put commands in their own PID namespace with a fresh
   `/proc`, so that they cannot open the Claude Code process's pipes? Can calu set its rules
   without the agent being able to change them?
2. **calu wraps every Bash command** (e.g. through `CLAUDE_CODE_SHELL_PREFIX`, to verify) in a
   bubblewrap call: own PID and mount namespace, selected directories hidden or read-only, the
   project directory writable. Light isolation without root.
3. **Separate Unix user** for tool commands (sudo rule), possibly combined with 1 or 2. Not
   always possible on the target machines.
4. **Container per session** for the tool commands. Strongest, heaviest.

cgroups (a systemd scope per session) are used in every option for resource limits and to kill
all leftover processes at the end of a tool call or session.

## 4.5 Would a Claude Code plugin carry calu's configuration? (2026-11-09)

A plugin may contain `hooks/hooks.json`, an MCP server (`.mcp.json`), skills, commands, agents,
a `bin/` directory that is added to the Bash tool's PATH, and a `settings.json` of which **only**
`agent` and `subagentStatusLine` take effect. It is loaded with `--plugin-dir`, `--plugin-url`,
a marketplace install, or, in the SDK, `plugins=[{"type": "local", "path": ...}]`.

What calu configures on Claude Code today, and whether a plugin could carry it:

| calu needs | As a plugin? |
|---|---|
| PreToolUse hook for every tool | Yes, as a `command` hook. But a shell hook **fails open**: on its timeout the call continues through the normal permission flow, while the in-process SDK hook fails closed — verified, D23. It also starts a process per tool call and has to reach the calu process over IPC. |
| PostToolUse (Agent/Task) and SubagentStop for the subagent result review | Yes, with the same caveats. |
| The wait for a human (`can_use_tool`, no time limit) | Not as a hook. A plugin MCP server plus `--permission-prompt-tool mcp__plugin_calu_calu__ask` is the equivalent for the CLI (tool timeout about 28 h). In the SDK, `can_use_tool` stays the better path. |
| `permission_mode: "auto"` (D5) | No: a CLI flag or an SDK option; a plugin's `settings.json` cannot set it. |
| The sandbox settings (D15: `--settings` with `sandbox.enabled`, `failIfUnavailable`, `filesystem.denyRead`, ...) | **No.** A plugin's `settings.json` carries only `agent` and `subagentStatusLine`. These settings have to stay on `--settings`. |
| Hiding the checker API keys from Claude Code | No: the parent process sets the environment. |
| `replay-user-messages`, `forward-subagent-text`, `thinking-display` | No: CLI flags. |
| Slash commands for the operator (`/calu-escalations`, `/calu-context`) | Yes, and only a plugin can do it. Interactive sessions only. |
| A `calu` shim on the Bash tool's PATH | Yes (`bin/`), of minor value. |

**Conclusion.** A plugin does not make `calu run` simpler: in headless mode calu passes the
hooks as in-process callbacks, which cannot fail open, needs no file on disk and no IPC, and the
two settings that a user would most want to shorten (auto mode and the sandbox) cannot live in a
plugin at all. "Use this plugin" can therefore not replace what `calu run` does.

A plugin is the right vehicle for a **different mode**, which calu does not have yet: a user who
runs `claude` interactively and wants calu's checks in that session. There, a plugin plus
`--permission-prompt-tool` is close to the whole configuration, except the sandbox settings. That
mode needs a hook entry point (`calu hook ...`) that talks to a running calu process, and an MCP
server for the escalation wait; the session process would then serve a second kind of client.
Q10 asks whether that mode is wanted.

Security note: a plugin is a directory of files. The agent must not be able to edit it, or it
could switch the checks off (§4.1 point 4). Claude Code's sandbox already denies writes to
`~/.claude`, and a plugin inside the project would need a write deny rule. In-process callbacks
have no such surface.

## 5. Credentials and start

```
calu run  ──► asks for the password ──► Argon2id(password, salt) ──► key
                                                     │
          encrypted DB credential (D12) ─────decrypt─┘──► connect to PostgreSQL
          verify role, load configuration, create the session row
          overwrite password, key and credential in memory (bytearray)
          start Claude Code (its environment never contains calu secrets)
```

- The database connection stays open for the session. If it breaks, calu cannot reconnect
  without the password: the session pauses (state `escalated`, reason "database connection
  lost") until an operator re-enters the password (`calu attach` or the console). A per-session
  database role with limited rights is the alternative (Q3).
- Database roles: `calu_admin` (configuration, checker definitions, security settings),
  `calu_session` (writes the rows of its own session, reads configuration; row-level security),
  `calu_operator` (reads everything, resolves escalations). Clients authenticate to the session
  process with tokens that the process issues and stores hashed in the database.
- Python cannot guarantee that secrets leave no copies in memory (immutable strings, libpq
  copies). The real protection is that untrusted code cannot read the calu process at all (4.1).

## 6. Client protocol

- One WebSocket endpoint per session process (default: local only). `calu run` binds
  `127.0.0.1` on a port the system chooses, or with `--socket [PATH]` a **Unix socket** —
  `$XDG_RUNTIME_DIR/calu/<pid>.sock` by default, mode 0600, so the file system decides who may
  connect instead of every process on the machine being able to reach a loopback port. The
  address is recorded in `sessions.address` either way (`ws://host:port` or `unix:<path>`,
  D29), and `calu attach`, `calu acp` and the web interface take both. Remote access goes
  through an SSH tunnel or a TLS proxy. JSON-RPC 2.0 messages.
- Requests (from sitl's protocol): `session.start/stop/interrupt`, `chat.send` (queued or
  inject), `escalation.decide`, `context.get`, `comment.add`, plus `state.get`,
  `events.subscribe`, `session.list`, `session.history`, `escalation.context` and
  `checkers.get`. Everything that **changes** the project's configuration is deliberately absent
  (§6.2); the one setting a client may change is `auto_escalate.set`, because it belongs to the
  running session rather than to the project -- it holds back approved steps and is forgotten on
  restart -- and an editor needs it to offer ACP's session mode (D42).
- Events: sitl's event set (`assistant.*`, `tool.use`, `tool.result`, `check.result`,
  `escalation.request/resolved`, `chat.queue`, `tool.files`, ...) plus
  `auto_escalate.changed` (D42), each with a sequence number
  (`Event.seq`, counted per calu process). A client that connects gets a snapshot (`state.get`)
  and then the events after its sequence number (`events.subscribe`); several clients see the
  same stream. The replay buffer holds the last 5000 events of the process and nothing is
  written to the database (D3), so a client that was away longer is told that its replay has a
  gap and takes a fresh snapshot.
- The protocol is defined by Pydantic models; a JSON Schema is generated for non-Python clients.

### 6.1 ACP for third-party user interfaces (2026-11-09)

The **Agent Client Protocol** (ACP, agentclientprotocol.com, draft v1 as read on 2026-11-09)
standardizes how an editor talks to a coding agent: JSON-RPC 2.0, the editor is the *client*,
the agent is the *server*. It is developed by Zed and JetBrains, which is the reason to bet on
it rather than on one editor's plugin API:

- **JetBrains Air** (and the Air plugin for the IntelliJ IDEs) runs agents locally and talks to
  them over ACP, so that an agent needs no integration written for JetBrains; it discovers
  agents through an **ACP agent registry** that a user installs from, and already lists Codex,
  Claude Agent, Gemini CLI, Junie, Copilot, OpenCode, Pi and Cline.
- **Zed** created ACP and runs Claude Code through an ACP adapter today.
- **Neovim** reaches it through agentic.nvim, avante.nvim and CodeCompanion; **Goose** speaks it
  in both directions (`goose acp`).

So one ACP implementation gives calu all of these at once, and a place in the registry, without
writing a plugin for any single editor (D24). ACP is the floor, not the ceiling: what it has no
slot for — the checker verdicts as structure, the herd overview, editing the rules — stays with
calu's own protocol (§6), which calu-aware clients use.

**Where ACP and calu fit together well.**

| ACP | calu |
|---|---|
| `session/request_permission` (a **baseline** agent → client request: tool call, `options` with `allow_once` / `allow_always` / `reject_once` / `reject_always`, answer `selected`/`cancelled`) | ESCALATE: exactly calu's "freeze time and ask a human". `allow_always` and `reject_always` can be stored as a rule row (`calu rules add`), because the spec leaves the scope of "always" to the agent. |
| `session/update` with `tool_call` / `tool_call_update` (`toolCallId`, `title`, `kind`, `status` pending → in_progress → completed/failed, `content` incl. a `diff` variant with `oldText`/`newText`, `locations`) | `tool.use`, `tool.result`, `edit.diff`; the checker's step summary is the `title`, its verdict and comment go into a `tool_call_update` |
| `agent_message_chunk`, `agent_thought_chunk`, `user_message_chunk` | `assistant.text`, `assistant.thinking`, `assistant.delta`, `user.input` |
| `authenticate` with `authMethods` | the client token of D20 |
| `session/new {cwd, mcpServers}` | the workspace of the session |
| `session/load` (MUST replay the whole conversation as `session/update`, then answer) and `session/resume` (reconnect **without** replay) | `events.subscribe {after_seq: 0}` and `events.subscribe {after_seq: N}` — calu's bus already has both modes |
| session config options (`configOptions`, `session/set_config_option`, `config_option_update`) and the older session modes (`availableModes`, `currentModeId`, `session/set_mode`, `current_mode_update`) they supersede | the auto-escalation categories of `AutoEscalate`, as three boolean options and as a ladder of four modes (D42) |

**Where they do not fit, and what follows.**

1. **Transport.** ACP defines only stdio, and "the client launches the agent as a subprocess"
   (an HTTP/WebSocket transport is an open RFD). calu is the opposite: a background process that
   outlives every client, with several clients attached at once (REQUIREMENTS 7, 9). So calu
   does **not** become an ACP agent; it gets a thin adapter, `calu acp`, which the editor spawns
   and which attaches to a running session over calu's own WebSocket protocol and translates in
   both directions. Each editor spawns its own adapter; the session keeps running in tmux. This
   is how Zed runs Claude Code, so the shape is established. If the HTTP transport lands, the
   session process can serve ACP directly and the adapter becomes optional.
2. **Several clients are not specified in ACP.** It says `session/load` allows "sharing sessions
   between different Client instances" and nothing about two clients on one live session. With
   the adapter it works anyway, because calu's bus does the fan-out — with one rough edge: a
   permission request cannot be retracted by the agent, so if one client decides first, the
   other's dialog stays open and its late answer is discarded (calu's `decide_escalation`
   already refuses a decided escalation).
3. **The client's `fs/*` and `terminal/*` capabilities must not be used.** Claude Code does its
   own file and command work inside the sandbox; letting the editor read, write or run things on
   the agent's behalf would route tool calls around both the checkers and the sandbox.
4. **`mcpServers` from `session/new` must not be forwarded unchecked** to Claude Code: an editor
   could otherwise add tools to the trusted zone (§4.2). The adapter ignores them, or forwards
   only what the project's configuration allows.
5. **ACP has no notion of a checker.** A verdict, a reason and a comment have to travel as tool
   call content, which a calu-aware client would rather receive as structure. ACP is therefore
   the common denominator for third-party user interfaces, not a replacement for calu's own
   protocol (§6), which the web UI and the herd overview keep using.
6. **The draft is moving.** `session/resume`, `session/list` and the other session management
   methods — the ones calu would want most — are marked UNSTABLE, and `session/load` requires
   replaying the whole conversation, which needs the step and turn history from the database
   (the `session.history` request that §6 dropped with sitl's file-based event log).

### 6.2 Two kinds of calu client: session clients and herd clients (2026-11-09)

ACP is the floor (§6.1). calu's own user interfaces go beyond it, and the features that do fall
into two groups that talk to **different** sides of calu, because the rights differ:

| Feature | Talks to | Why |
|---|---|---|
| Chat, events, interrupt, comments | the session process, over §6's protocol | the session owns the live state, which is not in the database (D3): the token-by-token stream, the thinking, a tool call it is holding |
| Deciding an escalation or answering a question | **either**: the database (`calu resolve`, the web interface) or the session a client is attached to (D28, D30) | a decision must never depend on reaching the session process; the database carries the context a decision needs, and the session picks the decision up by notification and by polling |
| **Inspecting what a checker saw** for an escalation (`escalation.context`): the rendered step, the file before the change, the conversation and the chat summary of that moment, the checker runs with model and version | the session process | the row is written by the session, and `calu_session` may read `escalations` and `checker_runs` |
| **Reading** the checker definitions and their instructions (`checkers.get`) | the session process | `calu_session` has SELECT on `checker_types` |
| **Editing** the checker instructions, the rules, the configuration | the database, with the `admin` credential (`calu.admin`) | a session must not be able to change the rules it is judged by (§4.1 point 1); its role has no write access, and that is deliberate |
| Escalations and sessions of **other** hosts and sessions, resolving them | the database, with `calu_operator` (`calu.herd`) | herd management works without any session process, from any machine that reaches the database (REQUIREMENTS 13) |

So a calu user interface is two clients in one window: a session client for the session it
watches, and a database client for the fleet and for the configuration. A web interface runs
both on its server side; `calu attach` is only the first (it shows the checker definitions and
the escalation context, and it changes nothing), and the configuration commands are only the
second.

Both ways of deciding an escalation end as a real resolution in the database (D28): a herd
client with its own `calu_operator` login calls `calu_resolve_escalation` directly, and a
session client — which has only a client token — tells the session, which calls
`calu_client_decision` for it. Either way `escalation_resolved` fires, so every other attached
client learns of the decision, and `resolved_by` says who made it.

## 7. Database (first sketch)

| Table | Content |
|---|---|
| `hosts` | hostname, OS, first and last seen |
| `projects` | name, git remote, default checker set, network policy (applied to the sandbox, D39), sandbox settings |
| `sessions` | project, host, OS user, project directory, git commit at start, **pid**, **address** (where it listens for clients), Claude session id, model, state, state since, started/ended, started by |
| `agents` | the subagents of a session: agent id, **parent agent id** (NULL = the main agent), SDK agent id, description, type, background, status, started/ended (migration 0008) |
| `turns` | session, number, user inputs (verbatim), narrated answer, folded flag |
| `steps` | session, turn, sequence, agent id, tool name, input (JSON), git commit, rules outcome, checker decision, reason, step summary, narration, result summary, timestamps, the snapshot trees before and after the step and the paths it wrote (D31) |
| `checker_types` | name, route (command, edit, subagent result), instructions (Markdown), model, limits, version |
| `checker_runs` | step, checker type and version, model, decision, tokens, cache hits, duration |
| `escalations` | step, session, reason, full context (rendered step, recent turns, checker output), state (open, allowed, denied, cancelled), resolved by, resolved at, note |
| `rules` | scope (global, project), list (command/read/write allow/deny), pattern, comment, enabled |
| `config` | scope (global, project, session), key, value (JSON) |
| `chat_summaries` | session, folded summary text, covered turns |
| `audit_log` | who changed which configuration, rule or checker definition, when; and, with the session filled in, who changed a running session's own settings (D43) |
| `comments` | a user's comment on a selection in a diff or a file, with the step's turn (migration 0003) |
| `escalations.context` | also holds the tool input with workspace-relative paths and the content of the file an edit would change, so that `calu evals` can rebuild the step as a case |
| `client_tokens` | SHA-256 of the tokens with which clients may attach to a session, with label, address, last use and a revoked flag (migrations 0004 and 0009) |

Narrator instructions may stay built into calu (not in the database); `calu setup` loads the
built-in ones, and a session falls back to them if the database has no row for a checker type.
Notifications: `escalation_resolved`, `config_changed` (sessions reload rules and instructions).

Migrations: `0001_initial.sql` creates the tables, roles, triggers and the two escalation
functions; `0002_turn_records.sql` adds the remaining columns of a `TurnRecord` (`steps`,
`answer`, `narrated`, `started_at`, `ended_at`), an index on `sessions.claude_session_id` for
resuming, and replaces `calu_cancel_escalation(bigint)` with a version that records why the
session cancelled.

## 8. Command-line tool

| Command | Purpose |
|---|---|
| `calu db up` / `db down` | start or stop a local PostgreSQL with Docker Compose (simple setups) |
| `calu db migrate` | create or update roles and schema (migrations) |
| `calu setup` | first-time setup: migrations, default checkers and rules, login and encrypted credential of `calu_session` |
| `calu credential list/set/remove` | manage the encrypted credential store (D12), e.g. `set admin` for an existing PostgreSQL server |
| `calu project add <dir>`, `calu project list` | register a project (git remote, network policy) in the database; writes nothing into the project and leaves existing Claude Code configuration untouched |
| `calu config get/set/unset`, `calu checker list/show/set`, `calu rules list/add/remove/enable/disable` | configuration in the database (`calu.admin`; needs the `admin` credential, because a session may only read it) |
| `calu evals [FILE ...]` | run the checkers on evaluation cases (built-in, files, resolved escalations) and report accuracy, critical misses and over-escalations |
| `calu run [<dir>]` | start a session in the foreground (for tmux); prints the WebSocket address and a client token |
| `calu attach <session>` | terminal client for a running session |
| `calu sessions`, `calu escalations`, `calu resolve <id> allow\|deny [--note]` | herd management through the database |
| `calu install <component>` | additional components (IDE plugin, web UI, ...) |

## 9. Reuse from stay-in-the-loop

Taken over and adapted (with their unit tests): rules engine and shell parser
(`checkers/rules.py`, `checkers/shell.py`), checkers and their default instructions, result
checker, narrator, context builder and turn records, `SdkRunner` (Agent SDK, hook, auto mode),
skill-text filter, file links, chat queue, protocol models, fake runner and fake models for tests.
Rewritten: session state (multi-client, persisted), storage (database instead of files), bus
(WebSocket instead of stdio), configuration (database instead of `config.toml` and Markdown
files), CLI. Knowledge carried over: sitl's spike results on Claude Code's behaviour.

## 10. Development and tests

- `docker-compose.yml` with two services: `dev` (Linux with Python, uv, Node and the Claude
  Code CLI; the repository mounted) and `caludb` (PostgreSQL).
- Unit tests without database and without API keys (fake models, fake Claude runner, as in sitl).
- Database tests against `caludb` (marked `db`); live tests with real Claude and models (marked
  `live`; checker model Nemotron Lightning on Fireworks, Claude model haiku or sonnet).
- Spikes for every assumption about Claude Code behaviour, recorded like sitl's spike results.

## 11. Open questions

| # | Question |
|---|---|
| Q1 | Answered by D15, and for containers by the measurement in `spikes/RESULTS.md` ("The sandbox inside a Docker container"): a default container cannot create a user namespace, because Docker's seccomp profile blocks it, so bubblewrap and with it the sandbox cannot start; `--security-opt seccomp=unconfined` is the least of the options that work (`--cap-add SYS_ADMIN` and `--privileged` grant more, `apparmor=unconfined` does not help), and socat must be installed too — calu's own dev image now does both. Still untested: what `enableWeakerNestedSandbox` changes, which needs a logged-in `claude` inside a container. The proxy's allow list is answered by D39. |
| Q2 | Relationship to sitl: does calu replace it (VSCode extension as a calu client) or do both continue? |
| Q3 | Whether calu uses a per-session database role (the credential location is decided in D12). **Recommendation (2026-11-10), the owner's to accept:** not yet, and not as a role per session. What a per-session role would buy is row-level security -- session A could not read or write session B's rows -- and today one `calu_session` login can: `GRANT SELECT, INSERT, UPDATE ON hosts, sessions, turns, steps, chat_summaries` is not restricted by session (migration 0001). What it would cost is a `CREATE ROLE` and a `GRANT` per session, which means the owner credential has to be reachable when a session starts (today only `calu setup` needs it), plus a reaper for the roles of sessions that are gone -- and it would still not close the hole it looks like it closes, because a session process that is compromised **is** the session: it can write nonsense about itself, which no role separation prevents. The cheaper shape, if this is wanted, is one `calu_session` login with row-level security keyed on a session id the connection sets once (`SET LOCAL calu.session_id`), which needs no new role per session and no owner credential at run time. Either way it should come after the first real unsupervised runs, because it adds a failure mode (a session that cannot write) to a part that currently cannot fail that way. |
| Q4 | How clients authenticate **remotely**. The token half is answered by D30 (tokens minted per session, revocable, `calu token`). **Transport: recommendation (2026-11-10), implemented as documentation rather than code** -- keep binding loopback and send people through an SSH tunnel, which the README now spells out for the web interface, a session's port and a session's Unix socket. calu would otherwise have to carry TLS material, a certificate reload and a hostname policy to be worth the bind, and an SSH tunnel is already on every machine that runs a session; a herd is watched through the **database** anyway (D30), which is the path that needs no tunnel at all. **Operator logins: still the owner's call.** Today every operator shares the `admin` credential, which is the database owner: it can migrate, create roles and read everything. `calu_operator` exists and is exactly the right shape for a human (read everything, resolve an escalation, change nothing else), so the step is `calu credential set operator` plus the commands preferring it over `admin` where they only read and resolve -- worth doing before more than one person uses one database, and pointless before that. |
| Q5 | Answered as a **proposal** by D45, which needs the owner's word: a new project gets "proxy" with a starter allow list (package registries, git over HTTPS, OS packages) instead of "full". It is one flag to overrule (`calu project add --network full`) and one command to change afterwards (`calu project network`), and existing projects are untouched. What the owner should weigh: the starter list is opinionated and a task that needs an unlisted domain stops until someone adds it (`calu config add claude.sandbox.allowed_domains <domain>`), which is the cost of not leaving an unsupervised session able to reach anything. |
| Q6 | Answered by D27: it stops after `escalation_timeout_s`, and is resumed by Claude's session id. |
| Q7 | Answered by D16. |
| Q8 | How a client attached to the session process decides an escalation: as now (the session cancels the row with a note, D18), with the client's own `calu_operator` credentials, or with a database function that lets `calu_session` record a client decision for its own session. |
| Q11 | Answered by D23. |
| Q10 | Answered 2026-11-09: **no**. calu does not get the interactive-plugin mode of §4.5; third-party user interfaces are served through ACP instead (D24). |
| Q9 | Where sitl's training mode belongs in calu (human review of checker decisions, to collect evaluation data): it was left out of the session port and needs tables of its own. The user comments it was grouped with are ported (migration 0003). |
| Q13 | Answered by D36: a fork at an earlier turn works, with `resume_session_at` and `resume_drops_turn`. |
| Q14 | Answered by D46 after measuring it: **no**. `rewind_files` tracks only what Claude Code's edit tools changed -- a command's writes survive it, and a file a command created is not removed -- so a fork that used it would leave a half-reverted tree that looks consistent and is not. A fork rewinds the conversation only and says so; if file rewinding is wanted, it belongs on D31's snapshots. |
| Q15 | **The single executable of REQUIREMENTS 21 and soft goal 2: still wanted?** D41 recommends against building one and says why (it removes the Python interpreter, the cheapest of calu's three prerequisites, while PostgreSQL and the `claude` CLI stay; it costs a build per platform and libc; and it puts the package data calu reads at runtime behind a frozen-import layer). `uv tool install calu` and `calu doctor` cover what it was meant to make easy. The owner asked for the executable, so the recommendation needs their agreement -- or their instruction to build it anyway, in which case PyApp is the lighter of the two options, since it fetches a real interpreter instead of freezing imports. |
## 12. Phases

1. **Skeleton** (done): package layout, `calu` CLI, `docker-compose.yml` (`dev`, `caludb`),
   schema and migrations, `calu db up/init`, `calu setup`, configuration in the database.
2. **Core port** (done): rules engine, checkers, narrator, context builder, with configuration
   and checker definitions read from the database (`calu.store.ConfigStore`).
3. **Session runner** (done): `calu run`, Agent SDK with auto mode (D5), steps and escalations
   written to the database, escalation resolved from the database (`LISTEN/NOTIFY` and a poll),
   credentials dropped before Claude starts, sandbox settings passed with `--settings` (D15).
   `calu sessions/escalations/resolve` were pulled forward from phase 6, because without them
   nobody could unblock a session.
4. **Bus and terminal client** (done): `calu.bus` numbers the events and keeps them for replay,
   `calu.protocol.server` serves JSON-RPC over WebSocket to several clients at once (token per
   session, `state.get`, `events.subscribe`), `calu.protocol.client` is the client side, and
   `calu attach` is a terminal client. Comments were ported with it (migration 0003). Still
   open: a client cannot change the configuration, the rules, the checker instructions or the
   chat summary through the protocol, because the session role may only read them — that is done
   with the `admin` credential instead (§8 commands and the web interface, D40). The one
   exception is `auto_escalate.set`, which holds back approved steps in the running session only
   and is forgotten on restart (D42).
5. **Sandboxing of tool calls and the effects of a step**: the sandbox settings exist (D15). Still
   open: the write-set detection of D31 (snapshot per Bash step, the comparison of declared against
   written paths, the post-call review of the real diff, the revert), the network policy per project
   (Q5: the mechanism is built, D39, and only the default for new projects is open), the
   nested-sandbox case in Docker (Q1). The time and ctime
   attribution of D32 is **done** (`calu.clock`, the `step_times` and `changed_files` tools of
   `calu.checkers.tools`, `Session.clock_jumped`). The snapshot of D31 is **done**
   (`calu.changes`, `changes.snapshot` and `changes.review`, both off by default): calu records
   what a command wrote, warns about paths its summary did not mention, and can let a checker
   review the change and undo it. The evaluation of the checkers
   (`calu evals`, ported from sitl) is done.
6. **Herd management** (done): the commands of phase 3 and the web interface of `calu web`
   (D33, D34, D40) — the fleet grouped by what needs a human, a session's history at four levels
   of detail, the live view over the session's own WebSocket, the escalation queue of `/triage`,
   the settings generated from the configuration model, the checker instructions and the folded
   conversation editable, and on every page an attention badge and a jump palette (Ctrl+K).
7. **Third-party user interfaces and packaging:** `calu acp`, the ACP adapter of §6.1 (D24),
   is **done** (`src/calu/acp/`): `initialize`, `session/new`, `session/load` with the required
   replay from `session.history`, `session/prompt`, `session/cancel`, `session/close`, the
   `session/update` stream (message and thought chunks, tool calls with their checker verdict,
   diffs and results) and `session/request_permission` for escalations, over newline-delimited
   JSON on stdio. It is a client of a running session, so the session outlives every editor and
   several editors may attach at once. The editor's `fs/*` and `terminal/*` capabilities stay
   unused and its `mcpServers` are not forwarded (§6.1). Tested end to end against a real
   session over a real WebSocket with a scripted Claude (`tests/unit/test_acp.py`); **not yet
   driven by a real editor**. The single binary is decided against (D41) and the
   wheel is the shipping format; the session modes and config options of ACP carry
   `AutoEscalate` (D42). Nothing is open in this phase except what needs a real editor. A port of
   sitl's VSCode extension is only worth it for what ACP cannot carry (the checker verdicts as
   structure, the herd overview).
