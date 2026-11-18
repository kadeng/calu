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
| `ide.status` | - | `findings`: list of `{name, ok, level: "ok"/"missing"/"note", message, advice}` from `calu.doctor` (claude present and its version, sandbox tools, git, credential store, checker keys) plus `uv` (present, version); `database`: `{reachable, place, problem}` (checked with the credential rules below, never prompting); `server`: whether a `calu serve` answers for this host and user is not known without asking, so: `{sessions_running: n}` |
| `ide.sessions` | `all: bool = false` | `running`: sessions of this host (`herd.sessions`), each `{id, directory, state, claude_session_id, open_escalations, started_at, stale, here: bool}` (`here`: its directory is the workspace or an ancestor); `stored`: conversations of the workspace (`herd` conversations with transcripts), each `{claude_session_id, last_input, turns, age_s, running, outside}` |
| `ide.attach` | `session: str | null` (id or prefix; null = the session of the workspace) , `after_seq = 0` | `{attached: true, session_id, initialize: InitializeResult, snapshot: SessionSnapshot, events: [...], complete}`; from then on session events arrive as notifications (below). Error -32011 if no session matches (message says what to do), -32012 if several match (`data.sessions`) |
| `ide.detach` | - | `{}`; a minted token is revoked |
| `ide.start` | `kind: "new"/"resume"/"fork"`, `claude_session_id`, `fork_before_turn`, `prompt`, `model`, `effort`, `project` | files a `session_requests` row for this host, user and workspace, waits (`session_requests.wait_for`), then attaches: result as `ide.attach`. Error -32013 if no server answered (message names the terminal command), -32014 if the server refused (its message) |
| `session.call` | `method`, `params` | forwards one request of the session protocol to the attached session and returns its result; session errors are passed through with their code. Error -32015 if not attached |
| `ide.settings.get` | `project: str | null` | `schema`: `Config.model_json_schema()`; `rows`: per setting `{key, type, default, description, value, is_set, kind, choices}` (from `calu.configdoc.setting_rows` and the kind logic of `calu.web.settings`); `checkers`: `[{name, route, model, enabled, version, request_limit, output_tokens_limit, max_tokens}]`; `projects`: names |
| `ide.settings.set` | `key`, `value` (JSON), `project`, `password?` | `{value}` after validation (`admin.set_setting`); validation errors as -32602 with the message |
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
| `ide.link` | `{state: "attached"/"reconnecting"/"lost", message}`: the connection to the session; after "lost" the extension offers to attach again |
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
