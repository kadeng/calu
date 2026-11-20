# The VSCode extension for calu: specification (D229)

Written 2026-11-18 from the owner's request (REQUIREMENTS 34, with 23) and two surveys: of
stay-in-the-loop's extension (`../extensions/vscode` of the outer checkout, same author; its
structure and code may be reused) and of what calu offers a client. This file is the contract
between the Python side and the TypeScript side; change it when the build deviates.

## 1. Shape

    VSCode (UI side)                      extension host (workspace side: WSL, SSH, container)
    ┌──────────────────────┐  postMessage ┌────────────────────────┐ stdio, NDJSON  ┌──────────┐
    │ webviews: chat,      │◄────────────►│ extension (TypeScript) │◄──────────────►│ calu ide │
    │ settings             │              │ thin: UI, commands     │  JSON-RPC 2.0  │ (Python) │
    └──────────────────────┘              └────────────────────────┘                └────┬─────┘
                                                                     WebSocket / Unix socket │
                                                             database (settings, sessions)   ▼
                                                                                  calu session(s)

- **Python first** (AGENTS.md): everything a second IDE plugin would need is in `calu ide`, a
  new command that speaks JSON-RPC 2.0 over stdin/stdout, one JSON object per line. The
  extension starts it as a child process in the workspace folder and never opens a socket or
  a database itself. `extensionKind: ["workspace"]`, so in Remote-WSL, Remote-SSH and Dev
  Containers both run where the session runs.
- `calu ide` finds sessions, attaches (token-free socket, shared login, or a minted token: the
  same rules as `calu attach`), reconnects, relays the session protocol, and answers the
  requests the session protocol does not have: settings, checker instructions, model presets,
  listing and requesting sessions, the environment check.
- The extension never starts `calu run` or `calu serve` as a child: they may need sudo and
  passwords, which belong in a terminal. The extension opens an integrated terminal with the
  command (`calu.server.command`, default `calu serve`, with `--sudo` / `--insecure` from two
  settings) and then asks for sessions through `calu ide`.

## 2. `calu ide`: the bridge (Python, `src/calu/ide/`)

Started as `calu ide` with the workspace folder as its working directory. stdout carries only
protocol lines; logs go to stderr and to `$XDG_STATE_HOME/calu/log/ide.log` (`calu.logfile`).
It never prompts: a credential that needs a password is reported as error -32010 with
`data: {"needs": "<credential name>"}`, and the request may be repeated with `password`.

Requests of the extension (all results are JSON objects):

| method | params | result |
|---|---|---|
| `ide.initialize` | `client`, `client_version` | `bridge_version` (calu's version), `protocol_version`, `workspace` |
| `ide.status` | - | `findings`: list of `{name, ok, level: "ok"/"missing"/"note", message, advice}` from `calu.doctor` (claude present and its version, sandbox tools, git, credential store, checker keys) plus `uv` (present, version); `database`: `{reachable, place, problem}` (checked with the credential rules below, never prompting); `server`: whether a `calu serve` answers for this host and user is not known without asking, so: `{sessions_running: n, servers_running: n or null}` (`servers_running`: the `servers` rows of this host that are not ended and whose process is not known to be gone; null without a database, with a login that may not read `servers`, or before migration 0038) |
| `ide.sessions` | `all: bool = false` | `running`: sessions of this host (`herd.sessions`), each `{id, directory, state, claude_session_id, open_escalations, started_at, stale, here: bool}` (`here`: its directory is the workspace or an ancestor); `stored`: conversations of the workspace (`herd` conversations with transcripts), each `{claude_session_id, last_input, turns, age_s, running, outside}` |
| `ide.attach` | `session: str | null` (id or prefix; null = the session of the workspace) , `after_seq = 0` | `{attached: true, session_id, initialize: InitializeResult, snapshot: SessionSnapshot, events: [...], complete}`; from then on session events arrive as notifications (below). Error -32011 if no session matches (message says what to do), -32012 if several match (`data.sessions`) |
| `ide.detach` | - | `{}`; a minted token is revoked |
| `ide.start` | `kind: "new"/"resume"/"fork"`, `claude_session_id`, `fork_before_turn`, `prompt`, `model`, `effort`, `project` | files a `session_requests` row for this host, user and workspace, waits (`session_requests.wait_for`), then attaches: result as `ide.attach`. Error -32013 if no server answered (message names the terminal command), -32014 if the server refused (its message) |
| `session.call` | `method`, `params` | forwards one request of the session protocol to the attached session and returns its result; session errors are passed through with their code. Error -32015 if not attached |
| `ide.settings.get` | `project: str | null` | `schema`: `Config.model_json_schema()`; `rows`: per setting `{key, type, default, description, value, is_set, kind, choices}` (from `calu.configdoc.setting_rows` and the kind logic of `calu.web.settings`); `checkers`: `[{name, route, model, enabled, version, request_limit, output_tokens_limit, max_tokens}]`; `projects`: names |
| `ide.settings.set` | `key`, `value` (JSON), `project`, `password?` | `{value}` after validation (`admin.set_setting`), plus `offer` `{key, text}` when a related switch is still off (`admin.enable_offer`, D253: switching `human_diffs.enabled` on offers `traces.enabled`; the editor asks and sets it only on a yes); validation errors as -32602 with the message |
| `ide.settings.unset` | `key`, `project`, `password?` | `{}` |
| `ide.models.presets` | - | `presets`: `[{id, label, group, model, note}]` (section 5); `roles`: `[{key, label, model}]` for every role that has a model (the checkers, narrator, summarizer, honesty reviewer, extra checker types); `claude`: `{model, effort, use_bedrock}` |
| `ide.models.set_all` | `model` (a model string, or `"default"`), `effort?`, `project?`, `password?` | `{changed: [keys]}`; sets every role (`admin.set_all_models`, and `honesty.reviewer_model`, and the checker definition rows) |
| `ide.models.set` | `role` (a `roles[].key`), `model`, `password?` | `{}` |
| `ide.checker.get` | `name` | `{name, route, model, version, enabled, instructions}` from the database |
| `ide.checker.save` | `name`, `instructions`, `base_version`, `password?` | `{version}`; error -32016 if the stored version differs from `base_version` (`data.version`, `data.instructions`) |
| `ide.shutdown` | - | `{}`; the bridge detaches and exits |

Notifications from the bridge:

| method | params |
|---|---|
| `session.event` | `{method, params}`: one event of the attached session, in order, `params.seq` included |
| `ide.link` | `{state: "attached"/"reconnecting"/"lost", message}`: the connection to the session; "lost" also carries `cause`: "unreachable" (nothing answers at the address: the session or its server stopped, or the network is down), "revoked" (close code 4001), "refused" (the session refused the connection), "closed" (connections stood but were closed again and again). After "unreachable" with no server running the VSCode extension says "The calu server stopped" (Start server, Retry); otherwise it offers to attach again |
| `ide.log` | `{level, message}` |

Credential rules (never a prompt): the shared login of the place file (`cli._shared_admin`),
then a password-free `calu_admin`, `operator` or `admin` entry of the store; reading uses the
narrowest that works, changing settings needs `calu_admin`/`admin`. With only password-protected
entries: error -32010 and the `password` parameter.

Reconnect: as `calu acp` (`acp/serve.py`): on close 4002 reconnect with `after_seq`; on
another loss try again with backoff for 15 s while reporting `ide.link reconnecting`, then
`lost`.

The session protocol itself is documented by `calu protocol` (JSON Schema); `side.ask` and
`usage.get` results are to be added to that schema (`protocol/schema.py`).

## 3. The extension (`extensions/vscode/`, TypeScript, esbuild, no UI framework)

Same structure as stay-in-the-loop's: pure modules without a `vscode` import (unit-tested with
vitest), thin `vscode` wrappers, one reducer shared by host and webview, VSCode CSS variables
only, strict CSP with a nonce, no runtime dependencies besides what esbuild bundles
(markdown-it).

    src/extension.ts          activate: wiring, commands
    src/bridge.ts             supervises `calu ide` (spawn, status machine, typed request())
    src/rpc.ts                JSON-RPC over NDJSON (pure)
    src/caluCommand.ts        finding `calu`: setting, PATH (no /mnt/* under WSL), ~/.local/bin
    src/protocol.generated.ts types from `calu protocol` (scripts/gen-protocol.mjs)
    src/bridgeProtocol.ts     types of section 2 (hand-written, small)
    src/chatView.ts           WebviewViewProvider `calu.chat`
    src/settingsPanel.ts      WebviewPanel "calu Settings"
    src/checkerFs.ts          FileSystemProvider `calu-checker:/<name>.md` (read, save)
    src/contextDoc.ts         read-only document `calu-context:/Checker context.md`
    src/diffView.ts           `calu-diff:` before/after documents, `vscode.diff`
    src/comment.ts            "Add comment for Claude" on a selection (file or diff side)
    src/bootstrap.ts          installing uv and calu; checking for claude
    src/sessions.ts           QuickPick: attach, new, resume, fork
    src/shared/*.ts           viewState reducer, messages, settings form model, presets model,
                              bootstrap decisions, activity line (all pure)
    webview/main.ts           the chat
    webview/settings.ts       the settings dialog
    media/*.css, icon.svg

### 3.1 Main UI (sidebar view "calu")

- Header: connection state, session picker (attach / new / resume / fork), model and effort,
  supervision level (auto-escalation), honesty state chip, usage.
- Tabs: **Main** and one tab per subagent (`agents.update`), with status chips; the transcript,
  the input and "send to" follow the tab. A subagent tab shows the task it was given and what
  it returned; a message to a background subagent goes with `chat.send {to}`; a tab of a
  finished subagent stays readable. Launch items in the main transcript link to the tab.
- Transcript: streaming text (Markdown), thinking (collapsed by default), tool calls with the
  checker's decision and reason, file links, "Open diff" (native diff editor), results.
- Escalations: a card with the tool, the reason, the rendered step, a note field, Allow / Deny;
  questions of the agent with their options; "Why?" opens the escalation context. A badge on
  the view counts open escalations; an optional sound.
- Input: Enter sends for the next turn, Ctrl/Cmd+Enter into the running turn, Shift+Enter a
  new line; pasted or dropped images attach (main agent only); an interrupt button; a
  side-question toggle (`side.ask`).
- Empty and error states say what to do and offer the action as a button: calu missing →
  Install; no server → "Start calu server" (opens the terminal); no session → New session;
  `claude` missing → how to install it (the extension does not install it).
- View title buttons: New session, Attach, Checker instructions, Checker context, Settings.

### 3.2 Checker instructions and context

- **Instructions**: "calu: Edit checker instructions" lists the checkers (QuickPick) and opens
  `calu-checker:/<name>.md` in a normal editor: Markdown highlighting, save with Ctrl+S, which
  calls `ide.checker.save` with the version read; a conflict offers a diff against the stored
  text. Also reachable from the view title, from the settings dialog and from a checker's
  decision in the transcript.
- **Context**: "calu: Show checker context" opens the read-only document from `context.get`
  (chat summary plus recent turns as the checkers see them), reloaded (debounced) while the
  session works; "Why?" on an escalation opens that escalation's stored context.

### 3.3 Settings dialog (editor-area webview)

Generated from `ide.settings.get`, so every setting calu has is in it: grouped by section with a
navigation list and a search field; each row with label, description, a control by kind
(toggle, select, number, text, list, JSON), a "modified" marker and "reset to default"; saved per
field on change with inline validation errors from calu; a scope switch (global / project).
On top, a **Models** card: the current model per role, "Use one model for everything" with the
presets as selectable cards plus a custom field (`ide.models.set_all`), and Claude Code's own
model and effort. A **Checkers** card: enable/disable, model, "Edit instructions". A **Setup**
card: the findings of `ide.status` with fix buttons. VSCode's own settings hold only what
belongs to the editor (section 3.5).

### 3.4 Installing

On activation the extension looks for `calu` (and asks `ide.status` once it runs):

    calu missing ──► offer: install uv if missing (astral.sh script, in ~/.local/bin),
                     then `uv tool install --python 3.14 <source>`; progress notification,
                     output in the "calu" channel. Source: `calu.install.source`
                     (default: git+https://github.com/kadeng/calu).
    claude missing ─► reported with the install address; never installed by the extension.
    update          ─► command "calu: Update calu" (`uv tool upgrade calu`).

The root installation that `--sudo` needs is shown as a command to run in a terminal
(it needs sudo); the extension does not run it.

### 3.5 VSCode settings (`calu.*`)

`calu.command` (machine scope, restricted), `calu.install.source`, `calu.server.sudo`,
`calu.server.insecure`, `calu.server.extraArgs`, `calu.chat.showThinking`,
`calu.chat.escalationSound`. Everything about calu itself is in calu's database and edited in
the settings dialog.

## 4. Tests

- Python: `tests/unit/test_ide_*.py` (the bridge against a served fake session, as
  `tests/unit/test_protocol_server.py::served`), `tests/db/test_ide_*.py` (settings, models,
  checker save and conflict, session listing and `ide.start` against a `calu.serve.Server`).
- TypeScript: vitest for every pure module; `npm run check-types`; `npm run build`.
- Integration tests in a real VSCode (`@vscode/test-cli`) with a scripted bridge
  (`calu ide --fake` is not planned; the test starts a real fake session in Python) follow
  after the first version; say so in `docs/TESTING.md`.

## 5. Model presets (`calu.ide.presets`, built from `calu.web.settings.MODEL_OPTIONS`)

| id | label | model |
|---|---|---|
| `default` | calu's default (Haiku 5.5; Bedrock in Bedrock mode) | (none stored) |
| `anthropic-haiku` | Anthropic · Claude Haiku 5.5 | `anthropic:claude-haiku-5-5` |
| `anthropic-sonnet` | Anthropic · Claude Sonnet 5.5 | `anthropic:claude-sonnet-5-5` |
| `bedrock-haiku` | AWS Bedrock · Claude Haiku 5.5 | `bedrock:anthropic.claude-haiku-5-5` |
| `bedrock-sonnet` | AWS Bedrock · Claude Sonnet 5.5 | `bedrock:anthropic.claude-sonnet-5-5` |
| `bedrock-eu-haiku` | AWS Bedrock · Claude Haiku 4.5 (EU profile) | `config.BEDROCK_EU_HAIKU` |
| `fireworks-nemotron` | Fireworks · Nemotron Lightning 3.5 | `model_choice.FIREWORKS_MODEL` |
| `mistral-small` | Mistral · Mistral Small | the small model of `MODEL_OPTIONS` |
| `claude-cli-haiku` | Claude Code's own account · Haiku | `claude:haiku` |

Each preset carries a note on what it needs (which key or sign-in). No Mistral Medium or Large
preset (project rule). The extension shows what `ide.models.presets` returns and holds no
model names of its own.

## 6. Additions of 2026-11-18 (the owner's second message)

- **Switching sessions**: the chat header has a session dropdown that lists the sessions
  running on this machine (name, directory, state, open escalations; those of the workspace
  first) and switches with one click (`ide.attach {session}`); "New session…", "Resume a
  stored conversation…" are entries of the same dropdown. The QuickPick command stays.
- **Names for sessions**: a session has a `name` (free text, at most 80 characters, "" = none),
  stored in the `sessions` row (migration 0036, column `name`; `calu_operator` and
  `calu_admin` may update that column, the session login may not). Bridge: `ide.sessions`
  returns `name` per running session; `ide.session.rename {session_id, name, password?}` →
  `{name}`; `ide.start` takes an optional `name`, set on the row once the session started.
  `calu sessions` shows the name. The dropdown shows the name before the directory, and
  "Rename session…" sets it.
- **Remote**: nothing new: `extensionKind: ["workspace"]`, so with Remote-SSH, WSL or a Dev
  Container the extension host, `calu ide`, the server and the sessions all run on the remote
  machine; installing uv and calu happens there too.
- **No `calu serve` running** (`ide.start` error -32013, or the status says so): the extension
  shows how to start it so that it survives the editor: with tmux
  (`tmux new-session -A -s calu 'calu serve [--sudo] [--insecure]'`, detach with Ctrl+B D),
  else with screen (`screen -S calu -D -R calu serve ...`, detach with Ctrl+A D), and offers
  "Open terminal with this command" and "Copy command". Which of tmux and screen is installed
  is part of `ide.status` (`findings` named `tmux` and `screen`). "calu: Start calu server"
  uses the tmux form when tmux is installed, else the screen form, else the plain command with
  a note that it ends with the terminal.

## Deviations (the Python side as built, 2026-11-18)

Method names and the shapes of sections 2 and 6 are as specified. What `calu ide` adds or
decides where the table left it open:

- **`password` on every request that reads the database**, not only on the changing ones:
  `ide.sessions`, `ide.attach`, `ide.start`, `ide.session.rename`, `ide.settings.get`,
  `ide.models.presets`, `ide.checker.get`. A store with only protected entries answers each
  of them with -32010. For a change with only an `operator` entry stored, `data.needs` is
  `"admin"` and `password` is the admin password (the login is `calu_admin`, nothing is stored).
- **Error -32017** (new): no database credential is stored here, or the database cannot be
  reached or refuses. The message names the command that helps.
- **`ide.attach`** takes two more optional parameters, `address` and `token`: an explicit
  endpoint, for which no socket and no database is asked. Without them the order is: a local
  socket that needs no token, then the database. An address that cannot be reached is -32011;
  a token the session refuses is the session's own error (its code, -32001).
- **`ide.link`** params carry two more members: `session_id`, and for `attached` `complete`
  (false after a reconnect whose replay has a gap: read `state.get` again). The first
  `ide.link attached` follows the response to `ide.attach` / `ide.start`; no notification of a
  session is written before that response.
- **`ide.settings.get` rows**: `default` is the text of `calu.configdoc` (without backticks);
  `default_value` is the default as JSON; `inherits` is `"global"` or `"default"`. `value` is
  what the scope stores, else what it inherits. `kind` is one of `bool`, `choice`, `int`,
  `number`, `text`, `model`, `markdown`, `lines`, `json`, `section` (a nested group: no control).
- **`ide.sessions`**: `all: true` adds the sessions of other hosts (they cannot be attached
  from here); each running row also has `hostname`. `stored` holds at most 20 conversations.
- **`ide.models.presets`** takes optional `project` and `password`, and also returns
  `default_model`. Without a database login it still returns the presets, with `roles: []`,
  `claude: null` and `problem`. Role keys are the configuration keys (`command_checker.model`,
  `edit_checker.model`, `changeset_checker.model`, `subagent_checker.model`,
  `context.narrator_model`, `context.summarizer_model`, `honesty.reviewer_model`) and
  `checker:<name>` for an extra checker type.
- **`ide.models.set_all` / `ide.models.set`**: `model` may also be a preset id; `"default"`
  (for `set` also `""`) stores no model. `set` takes an optional `project`. `changed` holds
  role keys.
- **`ide.checker.save`**: `base_version: null` stores without the version check.
- **`ide.status`**: `database` has `needs` (the credential's name) when only a password is
  missing; `advice` is `""` for a finding that is ok. Without a reachable database,
  `server.sessions_running` counts the token-free local sockets that answer.
- **`ide.start`**: -32014 also when a server claimed the request and did not finish it in time.
- **`ide.start.cancel`** (new, no params) → `{cancelled: n}`: takes back the `ide.start`
  requests that still wait for a server. Each withdraws its `session_requests` row and fails
  with the new error **-32018**. A request whose session has started is not affected. The
  extension's "Cancel" on the waiting request uses it.
- **`user.input`** (session protocol) has `to`: "main", or the `agent_id` of the background
  subagent the user wrote to with `chat.send {to}`. For a subagent, `text` is the user's own
  text; the request to relay it, which the main agent receives around it, is not sent to the
  clients (the checkers' context keeps what Claude received).
- **Error texts** name no protocol method: they say what a person can do ("start a new session
  in the editor", "the session list shows the running ones").
- **`ide.log`** is sent only when a minted token could not be revoked.
- **Reconnect**: after close 4002 at once, otherwise with a growing wait (0.25 s to 2 s) for
  15 s; a revoked or refused token and four connections in a row without a new event end in
  `lost` at once. `session.call` waits up to 15 s while the bridge is connecting again.
- **`ide.unlock` (section 8)**: in `unlocked`, a store entry has its name (`operator`, `admin`,
  `calu_admin`) and the login of the role `calu_admin` with a typed password is called
  `calu_admin role`. The password is tried as the role's password only while no login that
  changes settings is available. An unlock that opens nothing is -32010 only while a login
  is still missing; with reading and changing both available it returns the state.
  `ide.status` has `unlocked` (the held names) next to `locked`. Every -32010 carries
  `data.unlocked`, also from a request that brought its own `password`.

## 7. Additions of 2026-11-18 (the owner's third message): parity with Claude Code's chat first

Dark theme first (the other themes must still work through the VSCode variables). The chat
must offer what Claude Code's own extension offers, then calu's features on top, with the
common actions one click away and the rest in the settings dialog:

| Claude Code basics | In the extension |
|---|---|
| inline images | thumbnails in user messages; paste, drop, attach |
| undo | "Rewind to before this message" on a user message: a new session that continues the conversation from before that turn (`ide.start` kind `resume` with `fork_before_turn`), after a confirmation that says files are not reverted |
| fork | "Fork from here" on a user message (same call, the original session keeps running), and "Fork conversation" (kind `fork`) in the session menu |
| multiple chat sessions | the session dropdown; switching keeps each session's draft |
| settings dialogue | the gear in the view title and in the header |
| slash commands | typing `/` opens a completion list: Claude Code's commands the session reports or a built-in list (`/compact`, `/clear`, `/usage`, `/context`, ...), which are sent as chat text, and the extension's own actions (`/model`, `/effort`, `/level`, `/honesty`, `/checkers`, `/context` (checker context), `/sessions`, `/new`, `/fork`, `/rename`, `/settings`, `/interrupt`) |
| model and thinking level | selectors in the composer footer |
| context usage | always visible in the composer footer: percent and tokens (`usage.get`, refreshed after each turn), rate limits in its tooltip/popover |
| questions and escalations | cards, as section 3.1 |
| notifications | a VSCode notification with Allow / Deny / Open when an escalation or a question arrives while the view is hidden or the window unfocused, and when a turn ends then; a badge on the view; optional sound |

calu's own features one click away: a header row of three quiet buttons - **Checkers** (opens
the instructions picker), **Context** (the checker context document), **Honesty** (a chip
showing the mode and state; its menu switches off / track / enforce through
`ide.settings.set honesty.mode`, shows open marks and "Review now", "Trust HEAD").

## 8. Passwords: at most one, once (the owner, 2026-11-18)

"By default, the extensions should require only one password on startup, maximum."

- After `sudo calu setup --no-password`, or with password-free store entries, nothing is asked.
- Otherwise the first request that needs a protected credential makes the extension ask ONE
  password, once per bridge process. It is sent with `ide.unlock {password}`: the bridge tries
  it on every protected entry it may need (`admin`, `operator`) and as the password of the role
  `calu_admin`, keeps the logins that opened in memory until it exits, and answers
  `{unlocked: [names], reading: bool, changing: bool}`. Later requests use the held logins and
  carry no password. The `password` parameter per request stays for other clients.
- Only if that one password does not cover changing settings (an `operator` entry with one
  password and a different admin password) is a second one asked, at the moment a setting is
  first changed, and then also kept by the bridge.
- A wrong password is said so and asked again; Escape stops asking until the user starts an
  action that needs it. Background requests (the session list, the status) never open a
  prompt.
- "Remember on this machine" (off by default, offered after the first successful unlock):
  the password is kept in VSCode's secret storage (the OS keychain) and sent with
  `ide.unlock` at start, so that nothing is asked at all. "calu: Forget stored password"
  removes it.

## 9. A `calu serve` backend, and no sudo in the client (the owner, 2026-11-18)

- The plugins may assume a running `calu serve`: sessions are created only by asking it
  (`ide.start`), never by starting `calu run`. Attaching to a session that `calu run` started
  still works where the bridge finds it, but no feature depends on it.
- Nothing on the client side runs as root or calls sudo: the extension and `calu ide` run as
  the user. They reach a root server's sessions as that user: through the user-side socket of
  an `--insecure` session, or through the database with a minted token (the shared login after
  `calu setup --no-password`, or the one password of section 8). They change settings through
  the database as `calu_admin`, not through root's files.
- Starting the server is the one step that may need sudo, and a person does it: the extension
  shows the command (tmux or screen, section 6) and can open a terminal with it, where sudo
  asks its password. The same holds for the root installation of calu. Neither is run by the
  extension itself.
- Consequence for the bridge: no code path of `calu ide` may require euid 0 or read
  `/etc/calu`; a request that cannot be served without them fails with a message that says
  what the person has to do on the server side.

## 10. The one password, made precise (the owner, 2026-11-18)

One password at the start of the extension, and again after each restart of the IDE, is fine;
more often is not. The client may keep the password in memory. So:

- The extension host keeps the password it asked for in memory (a variable, never a file, a
  setting, the webview state or a log) until the IDE window closes. When the bridge process is
  restarted within that time (a crash, "Restart bridge", an update of calu), the extension
  sends `ide.unlock` again by itself and asks nothing.
- The bridge keeps the opened logins in memory, as section 8 says.
- "Remember on this machine" (VSCode's secret storage) is therefore not needed and is not
  part of the first version; section 8's last point is withdrawn. If it was already built, it
  stays off by default and is not offered by a notification.

## 11. Direct manipulation in the transcript (the owner, 2026-11-18)

"Niceties like these make it fluid to use": what a row shows is what a click on it opens.

| Click on | Opens |
|---|---|
| the row of an edit (Edit, Write, MultiEdit, NotebookEdit) | the native diff editor with that edit's before and after (`edit.diff`), scrolled to the first changed line |
| the file name in that row | the file itself, cursor at the edited section (`start_line` of the edit) |
| the row of a Read | the file, at the line range that was read |
| a file path anywhere in Claude's text or in a tool's files list | the file (with `:line` if given) |
| the row of a command | expands the command and its output in place; the row's menu offers "Copy command" |
| a checker's decision chip | expands the reason; its menu offers "Open this checker's instructions" |
| a subagent's launch row | that subagent's tab |
| an escalation in the strip above the input | the tab and card of that escalation |
| the context-usage meter | the usage popover |

Rows that open something show it: pointer cursor, hover background, a tooltip naming the
action ("Open diff", "Open file at line 42"), and the same action by keyboard (Enter on the
focused row; the file name is a separate focus stop). An edit whose diff is no longer held
(the store keeps the newest 300) opens the file instead and says so in the tooltip.

## 12. The chat sits on the right by default (the owner, 2026-11-18)

The calu view opens in VSCode's secondary side bar (the right side in the default layout),
where the editor's own chat sits, not in the primary side bar on the left. The user can still
drag it elsewhere; VSCode remembers that. If the VSCode version in use cannot place a
contributed view container there by default, the extension moves the view once, at first
activation, and says so in the README.

As built: `contributes.viewsContainers.secondarySidebar` in `package.json`, which VSCode offers
from 1.106 on (release notes of 1.106, "View containers in Secondary Side Bar"), so
`engines.vscode` is `^1.106.0` and nothing is moved at activation. Checked in VSCode 1.147 with
a new profile: the container opens on the right next to the editor's chat, its tab carries the
badge of open escalations, and "calu: Open Chat" shows it there.
