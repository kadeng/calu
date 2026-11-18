# Webview contract

For the authors of `webview/main.ts` (chat), `webview/settings.ts` (settings dialog),
`media/chat.css` and `media/settings.css`. The host side exists; the shared modules in
`src/shared/` are the contract and are imported by both sides. Their TSDoc is authoritative; this
page is the overview.

## Rules for both webviews

- **HTML**: the host serves `<div id="app"></div>`, the stylesheet(s) and one script
  (`dist/webview.js` or `dist/settings.js`, built by esbuild from `webview/*.ts`, IIFE, ES2022).
  Build the DOM with `document.createElement`; no UI framework. `markdown-it` (dev dependency,
  bundled) with `html: false` is the only library; render images in Markdown as links.
- **CSP** (`renderHtml` in `src/chatView.ts`): `default-src 'none'`; scripts only with the
  nonce; styles, fonts, images and media only from the extension (`media/`, `dist/`) plus
  `data:` for images and media. So: **no inline `<script>`, no inline `style="..."` attribute, no
  `<style>` element, no remote resources, no `eval`**. Setting `element.style.x = ...` from
  script works; prefer classes.
- **CSS**: VSCode CSS variables only (`--vscode-foreground`, `--vscode-editor-background`,
  `--vscode-button-background`, `--vscode-font-family`, ...); no fixed colours, so every theme
  works, including high contrast.
- **`data-testid`**: every element a test must find carries `data-testid`, kebab-case,
  `<area>-<thing>[-<id>]`: `session-dropdown`, `session-entry-<sessionId>`, `tab-main`,
  `tab-<agentId>`, `item-<itemId>`, `tool-<toolUseId>`, `escalation-<key>`,
  `escalation-allow-<key>`, `escalation-deny-<key>`, `escalation-answer-<key>`,
  `escalation-why-<key>`, `input-text`, `input-send`, `input-interrupt`, `side-toggle`,
  `hint-<hintId>`, `hint-action-<hintId>`, `server-help`, `queue-<id>`, `header-model`,
  `header-effort`, `header-level`, `header-honesty`, `header-usage`; settings:
  `settings-search`, `settings-nav-<sectionId>`, `setting-<key>`, `setting-reset-<key>`,
  `setting-error-<key>`, `scope-select`, `models-card`, `preset-<id>`, `role-<key>`,
  `checkers-card`, `checker-edit-<name>`, `setup-card`, `setup-item-<name>`, `setup-fix-<name>`.
- **Untrusted input**: the host validates every message (`parseWebviewMessage`,
  `parseSettingsMessage`) and silently drops invalid ones. Texts from Claude, checkers and tools
  are untrusted too: set them with `textContent` or through markdown-it, never `innerHTML` of
  raw text.
- `acquireVsCodeApi()` once; `postMessage` for everything. `vscode.setState` may hold view-only
  state (active tab, scroll, draft); the host re-sends the real state after every `ready`.

## Chat (`webview/main.ts`)

### Start-up and state

    webview: post {type: "ready"}
    host:    {type: "reset", state: ViewState}, {type: "settings", settings: ChatSettings}
    then:    {type: "event", event: ViewEvent}   ->  state = applyEvent(state, event); render
             {type: "reset", state}              ->  replace the state (also sent after an attach)

Use `applyEvent` from `src/shared/viewState.ts` unchanged; never mutate the state. `ViewState`
(all JSON):

| field | meaning |
|---|---|
| `bridge {status, message?, version?}` | `calu ide` process: `stopped`, `starting`, `running`, `exited`, `error`, `missing` (calu not installed) |
| `link {status, message?}` | bridge ↔ session: `detached`, `attached`, `reconnecting`, `lost`. `isAttached(state)` says whether requests can be sent |
| `session {state, sessionId, claudeSessionId, workspace, project, model, effort, backgroundTasks}` | the attached session; `state` is the protocol's (`starting`, `idle`, `running`, `checking`, `tool_call`, `escalated`, `stopped`, `failed`); `sessionPhase()` in `activity.ts` maps it to busy/waiting/... |
| `sessions {list: SessionEntry[], attachedId, loaded, error?}` | running sessions of the machine for the dropdown (`id, name, directory, state, openEscalations, here, stale`), workspace ones first; refreshed by the host on attach/detach, every 10 s while the view is visible, and on `refreshSessions`. `sessionTitle(entry)` gives the text |
| `serverHelp {command, tool: tmux/screen/plain, detach, note}` or `null` | how to start `calu serve`; set while nothing is attached. Show it in the empty state with the buttons `startServer` and `copyServerCommand` |
| `hints: SetupHint[]` | what is missing, most important first: `{id, level, message, action, actionLabel, href?}`. Render each as a line with a button that posts `{type: action}` (or `{type: "openLink", href}` for `openLink`). Ids: `calu-missing`, `claude-missing`, `database`, `no-server`, `no-session` |
| `autoEscalate {commands, edits, subagents}` | supervision switches; `levelOf` / `levelLabel` / `LEVELS` in `levels.ts` |
| `honesty: HonestyInfo` or `null` | code honesty state; `honestyChip(honesty)` gives the header chip (`off` = show nothing) |
| `usage {rateLimits, costUsd, numTurns, context}` | account limits (`type, status, utilization, resets_at`), cost, context window (`tokens, maxTokens, percent, model`; `null` until asked) |
| `focus {tab, seq}` or `null` | switch to `tab` once per new `seq` (set by a new escalation) |
| `queue: QueuedView[]` | messages sent but not taken in yet (`id, text, delivery, to`), shown above the input |
| `agents: AgentInfo[]` | subagents: one tab each (`agent_id, description, subagent_type, background, status`) plus the tab Main |
| `subagents[agentId]: SubagentView` | `task` (launch prompt) and `result {content, isError, final}` of a subagent, for the head of its tab |
| `items[agentKey]: Item[]` | the transcript per tab (`MAIN = "main"`). Kinds: `user` (`source` user/cross_session/system, `images`), `assistant` and `thinking` (`streaming`), `tool` (→ `tools[toolUseId]`), `checker`, `escalation` (→ `escalations[key]`), `turn`, `launch` (first item of a subagent tab), `notice` |
| `tools[toolUseId]: ToolView` | `name, input, check {route, decision, reason, summary, comment, checkerRoute}, result, diffPath, editRange, gitHead, files`. `menuFor`, `toolKind`, `fileLinkAction` in `itemMenu.ts` give the context menu. An `Agent`/`Task` tool item links to the tab `toolUseId` |
| `escalations[key]: EscalationView` | `status` open/allow/deny/cancelled/answered, `pending`, `reason`, `rendered` (Markdown), `toolInput` (show literally with `literalToolInput`), `questions[]`, `answer`, `note`, `openedAt` |
| `side: SideExchangeView[]` | side questions of this client (`id, question, answer?, error?`); no answer and no error = waiting |
| `lastSeq`, `nextId` | bookkeeping |

Read `items`, `tools`, `escalations`, `subagents` with `own(map, key)` (keys such as
`__proto__` are data). `messageTarget(state, tab)` says who receives a message typed in a tab,
or why nobody can. `formatAnswer(questions, choices)` builds the answer text of a question card.
`activityLine(...)` builds the activity line.

`ChatSettings`: `showThinking` (hide thinking items when false), `escalationSound`.

### Messages webview → host (`WebviewMessage` in `src/shared/messages.ts`)

| message | fields | what the host does |
|---|---|---|
| `ready` | | sends `reset`, `settings`, a pending `bell` |
| `send` | `text`, `delivery: queued/inject`, `to` ("main" or an agent id), `images: [{data (base64), mediaType}]` (≤ 4, raster types, main only) | `chat.send`; on failure a notice and `restoreInput {text}`. Enter = `queued`, Ctrl/Cmd+Enter = `inject` |
| `decide` | `escalationId` (= `EscalationView.key`), `decision: allow/deny`, `note?`, `answer?` | marks the card pending, `escalation.decide`; with `answer` (question cards) calu ignores the decision |
| `why` | `escalationId` | opens the escalation's stored context as a read-only document |
| `sideAsk` | `question` | `ui.sideAsk`, `side.ask` with the earlier answers as history, then `ui.sideAnswer` |
| `attach` | | session QuickPick (attach, new, resume, fork) |
| `newSession` | | asks first message and name, `ide.start` |
| `switchSession` | `sessionId` (from `sessions.list`) | `ide.attach` → `reset` |
| `renameSession` | `sessionId` | asks the name with an input box, `ide.session.rename`, refreshes the list |
| `refreshSessions` | | `ide.sessions` → `ui.sessions` (may ask a password) |
| `detach` | | `ide.detach`; the transcript stays |
| `startServer` | | opens a terminal running `serverHelp.command` |
| `copyServerCommand` | | copies that command |
| `install` | | asks, installs uv if missing and calu, restarts the bridge |
| `restartBridge` | | stops and starts `calu ide` |
| `openSettings` | | opens the settings dialog |
| `editChecker` | `name?` or `route?` (`check.checkerRoute`) | opens the checker's instructions; a QuickPick if several match |
| `showContext` | | opens the read-only checker context |
| `setModel` | `model` (`CLAUDE_MODELS` or an id; "default") | `model.set` → `ui.model` |
| `setEffort` | `effort` (`CLAUDE_EFFORTS`) | stores `claude.effort` in calu's settings (later sessions; a notice says so) |
| `setLevel` | `level` (`LEVEL_IDS`) | `auto_escalate.set`; `auto_escalate.changed` follows |
| `refreshUsage` | | `usage.get` → `ui.usage` (the host also asks after each turn) |
| `interrupt` | | `interrupt` |
| `openFile` | `path` | opens it only if a tool call listed it in `files` |
| `openToolFile` | `toolUseId`, `index` | opens `tools[id].files[index]` |
| `openDiff` | `toolUseId` | native diff editor for the edit |
| `openLink` | `href` | http(s): browser; relative: file in the workspace; else ignored |
| `itemAction` | `toolUseId`, `action` (`ItemActionId`) | context-menu action (`itemMenu.ts`) |
| `stateReport` | `requestId`, `state` | answer to `reportState` (tests) |

### Messages host → webview (`HostMessage`)

`reset {state}`, `event {event}`, `settings {settings}`, `bell` (play the escalation chime),
`restoreInput {text}` (put the text back into the input), `reportState {requestId}` (answer with
`stateReport`).

Events of the extension itself (all handled by `applyEvent`): `bridge.status`, `ui.link`,
`ui.detached`, `ui.sessions`, `ui.serverHelp`, `ui.hints`, `ui.model`, `ui.usage`, `ui.notice`,
`ui.clear`, `ui.decisionPending`, `ui.decisionFailed`, `ui.sideAsk`, `ui.sideAnswer`
(`ViewEvent` in `viewState.ts` lists their params). `ui.attached` reaches the webview as a
`reset`.

## Settings dialog (`webview/settings.ts`)

A `WebviewPanel` with `retainContextWhenHidden`. Messages: `src/shared/settingsMessages.ts`;
form model: `src/shared/settingsModel.ts`.

    webview: post {type: "load", project: null}
    host:    {type: "data", project, settings, presets, status, serverHelp, rootInstallCommand, problems}

| webview → host | fields | host answers |
|---|---|---|
| `load` | `project` (`null` = global scope) | `data` |
| `set` | `key`, `value` (JSON), `project` | `saved {key, value}` (value after calu's validation) or `error {key, message}` |
| `unset` | `key`, `project` | `saved {key, unset: true}`, then `data`; or `error` |
| `setAllModels` | `model` (a model string or "default"), `effort?`, `project` | `busy`, then `data`; or `error {key: "models"}` |
| `setRoleModel` | `role` (`presets.roles[].key`), `model` ("" = default), `project` | `data`; or `error {key: "models"}` |
| `editChecker` | `name` | opens `calu-checker:/<name>.md` in an editor |
| `runStatus` | | `busy`, `status {status}`; or `error {key: "status"}` |
| `fix` | `action` (`FIX_ACTIONS`: installCalu, updateCalu, startServer, copyServerCommand, copyRootInstall, openClaudeDocs, restartBridge) | runs it, then `data` |

`data` parts (each `null` if its request failed; `problems` then says why):

- `settings` = `ide.settings.get`: `rows` (`key, type, default, default_value, description,
  value, is_set, inherits, kind, choices`), `checkers`, `projects`, `schema`. Use
  `buildSections(rows, search)` → sections with `SettingsRowView` (`label`, `control`: toggle /
  select / number / text / model / markdown / list / json, `choices`, `modified`, `inherits`,
  `defaultText`, `nullable`); `formatControlValue` and `parseControlValue` convert between a
  control's text and the JSON value. Save per field on change; show `error.message` at the row.
- `presets` = `ide.models.presets`: `presets[]` (`id, label, group, model, note`), `roles[]`
  (`key, label, model`), `claude {model, effort, use_bedrock}`, `default_model`. The dialog shows
  what it gets and holds no model names of its own.
- `status` = `ide.status`: `findings[]`, `database`, `server`. `setupItems(findings)` gives the
  rows of the Setup card with their fix buttons. `rootInstallCommand` is shown with
  `copyRootInstall` (the extension never runs it); `serverHelp` as in the chat.

A request that changes settings may make the host ask for a credential's password (input box of
VSCode, not in the webview); the password is used for that request only.
