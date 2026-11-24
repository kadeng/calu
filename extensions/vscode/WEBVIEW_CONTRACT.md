# Webview contract

For the authors of `webview/main.ts` (chat), `webview/settings.ts` (settings dialog),
`media/chat.css` and `media/settings.css`. The host side exists; the shared modules in
`src/shared/` are the contract and are imported by both sides. Their TSDoc is authoritative; this
page is the overview.

The chat webview has two hosts: the extension (`src/chatView.ts` with `src/host/chatHost.ts`) and
the chat pane of `calu web` (`pane/`, PLAN.md D261), which runs the same script in a browser tab
with the same messages. The pane defines `acquireVsCodeApi` itself and adds `onHostMessage`, so
the script takes the host's messages from there instead of `window` message events. Only the
variables of `pane/theme.css` are defined in the pane: a new `--vscode-*` variable in
`media/chat.css` needs a value there too, and `npm run build:pane` after any change.

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
  `hint-<hintId>`, `hint-action-<hintId>` (also `hint-starting-session` for a waiting session
  request), `server-help`, `queue-<id>`, `header-model`,
  `header-effort`, `header-level`, `header-honesty`, `header-usage` (model, effort and usage sit
  in the composer footer and keep these ids), `header-settings`, `input-hint`, `chat-list`,
  `activity`, `agent-head-<agentId>`; settings:
  `settings-search`, `settings-nav-<sectionId>`, `setting-<key>`, `setting-reset-<key>`,
  `setting-error-<key>`, `scope-select`, `models-card`, `preset-<id>`, `role-<key>`,
  `checkers-card`, `checker-edit-<name>`, `setup-card`, `setup-item-<name>`, `setup-fix-<name>`.
- **Untrusted input**: the host validates every message (`parseWebviewMessage`,
  `parseSettingsMessage`) and silently drops invalid ones. Texts from Claude, checkers and tools
  are untrusted too: set them with `textContent` or through markdown-it, never `innerHTML` of
  raw text.
- **Bidirectional control characters** (U+202A to U+202E, U+2066 to U+2069, U+200E, U+200F,
  U+061C) in any text from calu are shown as placeholders such as `⟦RLO⟧`
  (`neutraliseBidi` in `src/shared/bidi.ts`), so a command or file name is never drawn in
  another order than it is stored in. It is applied in one place per webview: `el()` (text and
  the attributes `title`, `aria-label`, `placeholder`, `alt`) and `markdown()` in the chat,
  `el()`, `add()`, `setHighlighted()` and `setRich()` in the settings dialog. Code that sets
  `textContent` or appends a string itself calls `shown()` (`webview/dom.ts`). Ordinary
  right-to-left text is left to the browser: prose blocks have `unicode-bidi: plaintext`, code
  and paths are isolated left-to-right (`media/chat.css`, "Text direction").
- `acquireVsCodeApi()` once; `postMessage` for everything. `vscode.setState` may hold view-only
  state (active tab, scroll, draft); the host re-sends the real state after every `ready`.

## Chat (`webview/main.ts`)

The view `calu.chat` is the only view of the container `calu`, which `package.json` contributes
to the secondary side bar (spec section 12). The view may be as narrow as 280 px there, and the
user may move it to the primary side bar or the panel.

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
| `hints: SetupHint[]` | what is missing, most important first: `{id, level, message, action, actionLabel, href?}`. Render each as a line with a button that posts `{type: action}` (or `{type: "openLink", href}` for `openLink`). Ids: `calu-missing`, `claude-missing`, `locked` (calu's user login needs the calu user password; action `unlock`), `database`, `server-stopped` (the link was lost because nothing answers and no server runs: action `startServer`, the card adds `retryServer`), `no-server`, `no-session`. `setupCards(state)` in `chatModel.ts` turns bridge status and hints into the cards of the empty state |
| `autoEscalate {commands, edits, subagents}` | supervision switches; `levelOf` / `levelLabel` / `LEVELS` in `levels.ts` |
| `honesty: HonestyInfo` or `null` | code honesty state; `honestyChip(honesty)` gives the header chip (`off` = show nothing) |
| `honestyStored` | `honesty.mode` as calu's settings store it (`null` = unknown). `honestyStoredNote(honesty, honestyStored)` gives "stored: track" while it differs from the session's mode; the chip then shows both |
| `starting {title, slow}` or `null` | a session request (`ide.start`) that waits for the server. `setupCards` puts `startingCard` first: progress with "Cancel" (`cancelStart`), and with `slow` (after 3 s) the hint that no server may be running with "Start calu server" |
| `usage {rateLimits, costUsd, numTurns, context}` | account limits (`type, status, utilization, resets_at`), cost, context window (`tokens, maxTokens, percent, model`; `null` until asked) |
| `focus {tab, seq}` or `null` | switch to `tab` once per new `seq` (set by a new escalation) |
| `queue: QueuedView[]` | messages sent but not taken in yet (`id, text, delivery, to`), shown above the input |
| `agents: AgentInfo[]` | subagents: one tab each (`agent_id, description, subagent_type, background, status`) plus the tab Main |
| `subagents[agentId]: SubagentView` | `task` (launch prompt) and `result {content, isError, final}` of a subagent, for the head of its tab |
| `turnCount` | number of `turn.result` events since the transcript was cleared |
| `turnBase` | number of turns the conversation had before the first replayed one; `null` until `ui.history` was applied. While it is known, a user message with `turn` 1 is the first of the conversation: its Rewind and Fork buttons are disabled with a tooltip that says why |
| `items[agentKey]: Item[]` | the transcript per tab (`MAIN = "main"`). Kinds: `user` (`source` user/cross_session/system, `images`, `turn` = `turnBase + turnCount + 1` when it arrived, `uuid` = Claude Code's id of the message or `null`, `to` = the subagent a message in a subagent's tab was sent to, `stored`), `assistant` and `thinking` (`streaming`), `tool` (→ `tools[toolUseId]`), `checker`, `escalation` (→ `escalations[key]`), `turn`, `launch` (first item of a subagent tab), `notice`, `relay` (in Main: "Sent to subagent <name>", a link to the tab `to`), `divider` (above and below the recorded turns) |
| `tools[toolUseId]: ToolView` | `name, input, check {route, decision, reason, summary, comment, checkerRoute}, result, diffPath, editRange, gitHead, files, stored`. `menuFor`, `toolKind`, `fileLinkAction` in `itemMenu.ts` give the context menu. An `Agent`/`Task` tool item links to the tab `toolUseId` |
| `escalations[key]: EscalationView` | `status` open/allow/deny/cancelled/answered, `pending`, `reason`, `rendered` (Markdown), `toolInput` (show literally with `literalToolInput`), `questions[]`, `answer`, `note`, `openedAt`. A card stays `open` when the bridge or the connection is lost (that is no decision); while `isAttached(state)` is false it shows "connection lost" and its buttons are disabled; once `transcriptEnded(state)` (link lost with `cause` "unreachable") it is one closed line "Session ended", and the transcript is greyed under "Session ended". The "Waiting for you" timer counts from the oldest open card's `openedAt` (`waitingSince`) `cancelled` is set by the session's end, a detach, or an attach whose snapshot no longer lists it |
| `side: SideExchangeView[]` | side questions of this client (`id, question, answer?, error?`); no answer and no error = waiting |
| `lastSeq`, `nextId` | bookkeeping |

Read `items`, `tools`, `escalations`, `subagents` with `own(map, key)` (keys such as
`__proto__` are data). `messageTarget(state, tab)` says who receives a message typed in a tab,
or why nobody can. `formatAnswer(questions, choices)` builds the answer text of a question card.
`activityLine(...)` builds the activity line.

`ChatSettings`: `showThinking` (hide thinking items when false), `escalationSound`.

### Recorded turns, messages for subagents, the same session again

- **Recorded turns** (`ui.history`, `src/shared/history.ts`): after every attach the host asks
  `session.history` and the reducer puts the turns that the replayed events do not cover above
  the transcript, between two `divider` items ("Earlier in this conversation" / "From calu's
  record: ..."): the user's messages, each step as a tool row with its decision and the
  checker's summary (input and the start of the result only if the record holds them), Claude's
  answer and the end of the turn. Everything from the record is marked `stored` and drawn
  quieter; a recorded step opens nothing but its own details. This is what a forked, rewound or
  resumed session shows of its earlier turns, and what a session shows whose replay buffer
  moved on. The host posts the result as a `reset`.
- **A message for a background subagent** arrives as `user.input {to: <agent id>, text: the
  user's own text}`. The text is a `user` item of that subagent's tab (`to` set, no Rewind or
  Fork); Main gets a `relay` item.
- **The same session again**: after the bridge was restarted, the host attaches to the session
  it had (`ide.attach {session, after_seq: lastSeq}`) and sends `ui.attached` with
  `resumed: true`; the reducer then keeps the transcript and applies only the new events. With
  a gap in the replay the host attaches with `after_seq: 0` (a full rebuild). Only if that
  session is gone does it fall back to the session of the workspace or the list.

### Messages webview → host (`WebviewMessage` in `src/shared/messages.ts`)

| message | fields | what the host does |
|---|---|---|
| `ready` | | sends `reset`, `settings`, a pending `bell` |
| `send` | `text`, `delivery: queued/inject`, `to` ("main" or an agent id), `images: [{data (base64), mediaType}]` (≤ 4, raster types, at most 5 MiB each as a session accepts, main only; `checkImage` refuses a larger one in the composer and nothing is sent) | `chat.send`; on failure a notice and `restoreInput {text}`. Enter = `queued`, Ctrl/Cmd+Enter = `inject` |
| `decide` | `escalationId` (= `EscalationView.key`), `decision: allow/deny`, `note?`, `answer?` | marks the card pending, `escalation.decide`; with `answer` (question cards) calu ignores the decision |
| `why` | `escalationId` | opens the escalation's stored context as a read-only document |
| `sideAsk` | `question` | `ui.sideAsk`, `side.ask` with the earlier answers as history, then `ui.sideAnswer` |
| `attach` | | session QuickPick (attach, new, resume, fork) |
| `newSession` | | `ide.start` at once; nothing is asked |
| `switchSession` | `sessionId` (from `sessions.list`) | `ide.attach` → `reset` |
| `renameSession` | `sessionId` | asks the name with an input box, `ide.session.rename`, refreshes the list |
| `refreshSessions` | | `ide.sessions` → `ui.sessions` (may ask a password) |
| `detach` | | `ide.detach`; the transcript stays |
| `startServer` | | opens a terminal running `serverHelp.command` |
| `retryServer` | | after "The calu server stopped": asks `ide.status` again and attaches as at the start if a server runs |
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
| `usagePanel` | | `ui.usagePanel {phase: "asking"}`, then `usage.panel` → `ui.usagePanel {result}` or `{error}`; the webview draws the panel from `state.usagePanel` (`usagePanel.ts`) |
| `interrupt` | | `interrupt` |
| `openFile` | `path`, `line?` (1-based) | opens it only if a tool call listed it in `files`; the cursor goes to `line` |
| `openToolFile` | `toolUseId`, `index`, `line?` | opens `tools[id].files[index]`, cursor at `line` |
| `openDiff` | `toolUseId` | native diff editor for the edit, revealed at its first changed line; if the diff is no longer held (the newest 300 are), the file is opened at the edited line and the status bar says so |
| `openLink` | `href` | http(s): browser; relative: file in the workspace (a trailing `:<line>` puts the cursor there); else ignored |
| `showLog` | | shows the "calu" output channel |
| `unlock` | | asks the calu user password (`ide.unlock`), then checks the setup and attaches to the workspace's session |
| `rewindBefore` | `turn` (`UserItem.turn`), `uuid?` (`UserItem.uuid`) | undo: asks for a confirmation (files are not reverted), then `ide.start {kind: "resume", claude_session_id, fork_before_turn}` → `reset`, then `restoreInput` with the message's text. The host takes the turn number from `session.history` (the turn whose `prompt_uuid` is `uuid`) and falls back to `turn`; turn 1 is refused with a message |
| `forkBefore` | `turn`, `uuid?` | the same start without confirmation and without `restoreInput` |
| `forkConversation` | | `ide.start {kind: "fork", claude_session_id}` of the attached conversation. A session cut from another one is named "fork of <name>" / "rewind of <name>" (`derivedSessionName`) |
| `cancelStart` | | `ide.start.cancel`: the waiting session request is withdrawn; `state.starting` becomes `null` |
| `setHonestyMode` | `mode` (`HONESTY_MODES`: off, track, enforce) | `ide.settings.set honesty.mode` in the scope the session reads it from (the project's settings if they name it, else global, as `calu attach` does); `ui.honestyStored`; then waits up to 4 s for the session's `honesty.update` and says whether the session uses the mode |
| `honestyReview` | | `honesty.review` (can take minutes; `honesty.update` reports progress) |
| `honestyTrust` | | after a confirmation, `honesty.trust {rev: "HEAD"}` |
| `honestyClear` | `markId` (`HonestyMarkInfo.id`) | `honesty.clear {mark_id}` |
| `itemAction` | `toolUseId`, `action` (`ItemActionId`) | context-menu action (`itemMenu.ts`) |
| `stateReport` | `requestId`, `state` | answer to `reportState` (tests) |

### Messages host → webview (`HostMessage`)

`reset {state}`, `event {event}`, `settings {settings}`, `bell` (play the escalation chime),
`restoreInput {text}` (put the text back into the input), `reportState {requestId}` (answer with
`stateReport`).

Events of the extension itself (all handled by `applyEvent`): `bridge.status`, `ui.link`,
`ui.detached`, `ui.sessions`, `ui.serverHelp`, `ui.hints`, `ui.model`, `ui.usage`, `ui.notice`,
`ui.clear`, `ui.decisionPending`, `ui.decisionFailed`, `ui.sideAsk`, `ui.sideAnswer`,
`ui.honestyStored`, `ui.starting` (`ViewEvent` in `viewState.ts` lists their params).
`ui.attached` and `ui.history` reach the webview as a `reset`.

Usage: the host asks `usage.get` after an attach, after every `turn.result`, and on a
`usage.update` of a running turn (at most every 2 s). While Claude works, the meter's tooltip
says that the number is from the last turn or usage report (`usageMeter(usage, working)`).

Hints while a session is attached (`claude-missing`, `database`) are one line each with a short
text (`SetupHint.short`), the action as a link and "Dismiss" (`SetupCard.compact`; the ids the
user dismissed are view-only state). Before the first session they are whole cards.

Open approval cards: the row of buttons is `position: sticky` at the lower edge of the
transcript, so Allow and Deny stay reachable while a card taller than the view scrolls. The
keyboard focus never moves to a button by itself.

Images: a click on a thumbnail of the transcript (`data-action="zoomImage"`) opens it large
over the view (`.lightbox`); a click or Escape closes it and returns the focus.

### Passwords and the `locked` state

The host asks for the calu user password at most once per window (spec sections 8 and 10), and
never if it is empty: at the first request that the bridge refuses with -32010, or right after
start if `ide.status.locked` and the chat is visible. It sends it with `ide.unlock`, keeps it in
a variable of the extension host until the window closes, and unlocks a restarted bridge with it
without asking. Requests carry no password. If the user dismisses the prompt, the waiting
requests fail with "Not done: no password was given. The next action that needs the database
asks again.", and the hint `locked` (button "Unlock" → `unlock`) is shown; the command
"calu: Unlock" does the same. After three refused passwords in a row they fail with "Not done:
the password was refused 3 times. ..." instead. The password never reaches the webview, the
state, a setting or the log. The decisions are in `src/shared/unlock.ts`.

### Notifications

Host side (`calu.notifications`: `all`, `decisions` (default), `off`): while the chat view is
hidden or the window is not focused, an escalation shows "calu: approval needed: <tool>" with
Allow / Deny / Open, a question "calu: Claude asks: <question>" with Open, and (with `all`) a
finished turn "calu: turn finished." with Open. The view's badge counts open escalations.

### What the chat webview is built from

`webview/main.ts` (state, header, tabs, setup cards, composer, event handlers), `items.ts`
(transcript items), `popup.ts` (the one floating panel), `markdown.ts`, `dom.ts`. Decisions
without DOM are in `src/shared/chatModel.ts` (session dropdown, tabs, setup cards, usage meter,
link and image checks), `src/shared/slashCommands.ts` (the command table, completion and what a
submitted command posts) and `rowClick` / `fileClick` in `src/shared/itemMenu.ts` (what a click
on a tool row opens, spec section 11). The composer keeps one draft per session id in
`vscode.setState`.

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
  The status text next to the scope is the summary of all rows (`saveSummary`): "Saving…",
  "Saved", "1 setting not saved", or "1 saved · 1 not saved" when one field was refused while
  another was stored.
- Descriptions, setup messages and advice, and preset notes carry inline marks: `` `code` ``
  and `**strong**`. `inlineParts` (`src/shared/inlineText.ts`) splits them and `setRich` /
  `rich` (`webview/settingsDom.ts`) build `<code>` and `<strong>` elements; the text never
  reaches an HTML parser.
- `presets` = `ide.models.presets`: `presets[]` (`id, label, group, model, note`), `roles[]`
  (`key, label, model`), `claude {model, effort, use_bedrock}`, `default_model`. The dialog shows
  what it gets and holds no model names of its own.
- `status` = `ide.status`: `findings[]`, `database`, `server`. `setupItems(findings)` gives the
  rows of the Setup card with their fix buttons. `rootInstallCommand` is shown with
  `copyRootInstall` (the extension never runs it); `serverHelp` as in the chat.

A request may make the host ask for the calu user password (input box of VSCode, not in the
webview), as described in "Passwords and the `locked` state"; one login reads and changes
settings.
