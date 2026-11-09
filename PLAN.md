# calu: implementation plan

Status: planning (2026-11-09). This file holds the decisions, the architecture and the order of
work, derived from the owner's requirements in [REQUIREMENTS.md](REQUIREMENTS.md). Decisions carry
a date; open questions are listed in §11.

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

## 5. Credentials and start

```
calu run  ──► asks for the password ──► Argon2id(password, salt) ──► key
                                                     │
          encrypted DB credential (see Q3) ──decrypt─┘──► connect to PostgreSQL
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
  `escalation.request/resolved`, `chat.queue`, `tool.files`, ...), each with a sequence number.
  A client that connects gets a snapshot (`state.get`) and then the events after its sequence
  number; several clients see the same stream.
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

Narrator instructions may stay built into calu (not in the database). Notifications:
`escalation_resolved`, `config_changed` (sessions reload rules and instructions).

## 8. Command-line tool

| Command | Purpose |
|---|---|
| `calu db up` / `db down` | start or stop a local PostgreSQL with Docker Compose (simple setups) |
| `calu db init` / `db migrate` | create roles and schema; apply migrations |
| `calu setup` | first-time setup: admin password, encrypted credential, default checkers and rules |
| `calu project add <dir>` | register a project (git remote, settings) in the database; writes nothing into the project by default and leaves existing Claude Code configuration untouched |
| `calu config get/set`, `calu checker list/show/edit`, `calu rules ...` | configuration in the database |
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
| Q1 | Isolation mechanism for tool calls (§4.4); first spikes: Claude Code's sandbox (PID namespace?), `CLAUDE_CODE_SHELL_PREFIX` wrapping. |
| Q2 | Relationship to sitl: does calu replace it (VSCode extension as a calu client) or do both continue? |
| Q3 | Where the encrypted database credential is kept (local file readable only by the calu user, or typed in directly), and whether calu uses a per-session database role. |
| Q4 | How clients authenticate remotely (tokens per session, operator accounts in the database, TLS). |
| Q5 | Network policy for the sandbox (none, proxy allow list, full) and its default. |
| Q6 | What happens to a session when no human answers an escalation for a long time (keep waiting, give Claude other work, stop after a timeout). |
| Q7 | Checkers on the Anthropic subscription through headless Claude Code (§3.2): does `claude -p` with a JSON schema give reliable structured answers, and how fast is it? |

## 12. Phases

1. **Skeleton:** package layout, `calu` CLI, `docker-compose.yml` (`dev`, `caludb`), schema and
   migrations, `calu db up/init`, `calu setup`, configuration in the database.
2. **Core port:** rules engine, checkers, narrator, context builder, with configuration and
   checker definitions read from the database.
3. **Session runner:** `calu run`, Agent SDK with auto mode (D5), steps and escalations written
   to the database, escalation resolved from the database (`LISTEN/NOTIFY`), credentials dropped
   before Claude starts.
4. **Bus and terminal client:** WebSocket JSON-RPC, snapshot and event stream, `calu attach`.
5. **Sandboxing of tool calls** after the spikes (Q1).
6. **Herd management:** `calu sessions/escalations/resolve`, then a web UI.
7. **IDE client** (from sitl's VSCode extension), packaging as a single binary.
