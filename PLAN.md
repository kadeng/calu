# calu: implementation plan

Status: phases 1-3 done (2026-11-09; see §12 and `HANDOFF.md`). This file holds the decisions,
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
| D1 | **Python 3.14, managed with uv** (2026-11-09). First deliverable: a package installable with `uv tool install`, providing one `calu` command. A single-file binary (PyApp or PyInstaller) comes later; a port to Go or Rust stays possible once calu works. |
| D2 | **Clients connect over WebSocket with JSON-RPC** (2026-11-09), the front-end-neutral protocol of sitl extended for many clients (§6). |
| D3 | **PostgreSQL is the central store** (2026-11-09): configuration, security settings, checker definitions, session and step history, escalations. No token-level streaming goes into the database. |
| D4 | **The sandbox boundary encloses the tool calls, not Claude Code** (2026-11-09). calu and the Claude Code process form the trusted zone; every command Claude runs is executed in a sandbox that cannot reach or modify the trusted zone. The mechanism is open (§4.4). |
| D5 | **Checkers AND Claude Code's auto mode** (2026-11-09, verified in sitl's `spikes/spike_auto_mode.py`): Claude Code runs with `permission_mode="auto"`; the PreToolUse hook answers a checker ALLOW with no decision (the auto mode classifier then decides as well), DENY with "deny", ESCALATE with "ask" (goes straight to calu's `can_use_tool`). Models without auto mode fall back to "default"; the hook reads the effective mode from its input and then answers "allow". |
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
| D18 | **A decision that reaches the session from a client is recorded as a cancellation with a note** (2026-11-09). Only `calu_operator` may call `calu_resolve_escalation`; the session role may not, so that a session which somehow lost its credential cannot approve its own steps (§4.1 point 5). A client attached to the session process therefore decides in the session, which then cancels the row with a note that holds the decision and the client. The audit trail of such a decision is weaker than that of an operator's resolution; whether clients should instead resolve escalations with their own operator credentials is Q8. |
| D23 | **The tool gate stays an in-process hook of the Agent SDK** (2026-11-09, verified in `spikes/RESULTS.md`, "Hook timeouts"): a hook that does not answer within its timeout stops the step there — the call is not run even with `permission_mode: "bypassPermissions"`, and Claude retries it once and then reports it. A shell hook of a plugin does the opposite (its timeout discards the output and the call continues through the normal permission flow), which is why the gate does not move into a plugin (§4.5). The PreToolUse timeout is 900 s, just above the checker deadline of 840 s, because no human wait happens in that hook; the two hooks that review a subagent's result may wait for a human and keep 24 hours. |
| D21 | **Four decisions: PASS, ALLOW, DENY, ESCALATE** (2026-11-09, the owner's wording). PASS is the normal answer: the checker has no objection and Claude Code's auto mode decides as before (the hook returns no decision). ESCALATE asks a human. ALLOW and DENY are deliberate exceptions: ALLOW lets the step run whatever auto mode would say (the hook answers "allow"), DENY stops it. Without auto mode (a model that has none, or `claude.auto_mode = false`), PASS has the same effect as ALLOW, because then the checkers decide alone. The allow lists of the rules produce PASS, not ALLOW: they say "no check needed", not "run this whatever the harness thinks". This replaces the three-valued decision of sitl, where ALLOW already meant "no decision" under auto mode (D5). |
| D22 | **Checkers may read files** (2026-11-09): a checker agent gets one read-only tool for files of the workspace, bounded by the read deny lists, so it can look at the file a step changes when the step alone is not enough. It gets no other tool: no writes, no commands, no network. |
| D20 | **One token per session for its clients** (2026-11-09): `calu run` issues a client token, prints it once and stores only its SHA-256 (`client_tokens`). A client sends it in `initialize`; every other request is refused before that. The endpoint binds 127.0.0.1 by default, so remote access goes through an SSH tunnel or a TLS proxy until Q4 is answered. |
| D19 | **One database connection per session, serialized and shielded** (2026-11-09): `calu run` opens two connections before it wipes the credentials (one for queries, one for notifications) and cannot open more later, because reconnecting would need the password again (§5). psycopg allows one statement at a time per connection, and a session writes from several tasks at once, so every statement goes through `calu.db.Db`: a lock serializes them, and each statement is shielded from cancellation (bounded by a timeout), because a task cancelled in the middle of a statement leaves the connection unusable for every other task. |

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

- One WebSocket endpoint per session process (default: local only, `127.0.0.1` or a Unix
  socket; remote access through SSH tunnels or a TLS proxy). JSON-RPC 2.0 messages.
- Requests (from sitl's protocol): `session.start/stop/interrupt`, `chat.send` (queued or
  inject), `escalation.decide`, `context.get`, `comment.add`, plus `state.get` and
  `events.subscribe`.
- Events: sitl's event set (`assistant.*`, `tool.use`, `tool.result`, `check.result`,
  `escalation.request/resolved`, `chat.queue`, `tool.files`, ...), each with a sequence number
  (`Event.seq`, counted per calu process). A client that connects gets a snapshot (`state.get`)
  and then the events after its sequence number (`events.subscribe`); several clients see the
  same stream. The replay buffer holds the last 5000 events of the process and nothing is
  written to the database (D3), so a client that was away longer is told that its replay has a
  gap and takes a fresh snapshot.
- The protocol is defined by Pydantic models; a JSON Schema is generated for non-Python clients.

## 7. Database (first sketch)

| Table | Content |
|---|---|
| `hosts` | hostname, OS, first and last seen |
| `projects` | name, git remote, default checker set, network policy, sandbox settings |
| `sessions` | project, host, OS user, project directory, git commit at start, pid, Claude session id, model, state, state since, started/ended, started by |
| `turns` | session, number, user inputs (verbatim), narrated answer, folded flag |
| `steps` | session, turn, sequence, agent id, tool name, input (JSON), git commit, rules outcome, checker decision, reason, step summary, narration, result summary, timestamps |
| `checker_types` | name, route (command, edit, subagent result), instructions (Markdown), model, limits, version |
| `checker_runs` | step, checker type and version, model, decision, tokens, cache hits, duration |
| `escalations` | step, session, reason, full context (rendered step, recent turns, checker output), state (open, allowed, denied, cancelled), resolved by, resolved at, note |
| `rules` | scope (global, project), list (command/read/write allow/deny), pattern, comment, enabled |
| `config` | scope (global, project, session), key, value (JSON) |
| `chat_summaries` | session, folded summary text, covered turns |
| `audit_log` | who changed which configuration, rule or checker definition, when |
| `comments` | a user's comment on a selection in a diff or a file, with the step's turn (migration 0003) |
| `escalations.context` | also holds the tool input with workspace-relative paths and the content of the file an edit would change, so that `calu evals` can rebuild the step as a case |
| `client_tokens` | SHA-256 of the tokens with which clients may attach to a session (migration 0004) |

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
| Q1 | Answered by D15. Still open: the sandbox inside Docker containers (`enableWeakerNestedSandbox`), the network proxy's domain allow list. |
| Q2 | Relationship to sitl: does calu replace it (VSCode extension as a calu client) or do both continue? |
| Q3 | Whether calu uses a per-session database role (the credential location is decided in D12). |
| Q4 | How clients authenticate remotely (tokens per session, operator accounts in the database, TLS). |
| Q5 | Network policy for the sandbox (none, proxy allow list, full) and its default. |
| Q6 | What happens to a session when no human answers an escalation for a long time (keep waiting, give Claude other work, stop after a timeout). |
| Q7 | Answered by D16. |
| Q8 | How a client attached to the session process decides an escalation: as now (the session cancels the row with a note, D18), with the client's own `calu_operator` credentials, or with a database function that lets `calu_session` record a client decision for its own session. |
| Q11 | Answered by D23. |
| Q10 | Should calu support interactive `claude` sessions through a plugin (§4.5)? It would need a `calu hook` entry point, an MCP server for the human wait and a second client kind in the session process; the sandbox settings and auto mode would still have to be passed on the command line, and the shell hook fails open where the in-process hook fails closed. |
| Q9 | Where sitl's training mode belongs in calu (human review of checker decisions, to collect evaluation data): it was left out of the session port and needs tables of its own. The user comments it was grouped with are ported (migration 0003). |

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
   open: a client cannot change the configuration, the rules or the checker instructions
   through the protocol, because the session role may only read them (§8 commands, or a web UI
   with operator rights).
5. **Sandboxing of tool calls**: the settings exist (D15); still open are the network policy
   per project (Q5), the nested-sandbox case in Docker (Q1) and `calu project add`.
   The evaluation of the checkers (`calu evals`, ported from sitl) is done.
6. **Herd management:** a web UI on top of the commands of phase 3.
7. **IDE client** (from sitl's VSCode extension), packaging as a single binary.
