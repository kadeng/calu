# The PyCharm plugin: research of 2026-11-18 (requirement 35)

Condensed from a research agent's report (web sources of 2025-2026, plus a grep of the owner's
PyCharm log). Nothing here is built or decided. Items marked "not verified" need a spike.

## Found on the owner's machine (PyCharm 2026.3, build PY-263, Windows 11, project in WSL)

- `calu acp` already runs in PyCharm's agent chat as a custom agent from
  `C:\Users\KaiLo\.jetbrains\acp.json`, when started as `wsl.exe -d Ubuntu -- <script>`; a
  Linux path as the command fails (`EelPathException`).
- The IDE then refused four times: `session/new reused existing ACP session id`. The IDE
  expects a fresh id for every `session/new`; `calu acp` answered with the one session's id.
  **Fixed 2026-11-18** (`src/calu/acp/adapter.py`): every `session/new` answers with a new id
  `<calu session id>~<8 hex digits>`; all of them stand for the one calu session, each gets
  every update and every permission question, and the first answer decides. Not yet run
  again in the owner's PyCharm.
- Bundled in that PyCharm: SSH, SSH remote run, Deployment (FTP/SFTP), JCEF, terminal, remote
  development.

## Building a JetBrains plugin today

- IntelliJ Platform Gradle Plugin 2.x (`org.jetbrains.intellij.platform`), Kotlin with
  coroutines, target `pycharm("2026.x")` (PyCharm is one distribution since 2025.3), since-build
  253 or 261. Templates: `JetBrains/intellij-platform-plugin-template` and, for split mode,
  `JetBrains/intellij-platform-modular-plugin-template`.
- External process: `GeneralCommandLine` + `OSProcessHandler` (stable), or the Eel API
  (`eel.exec.spawnProcess`, covers local, WSL and Docker; still marked experimental).
- JSON-RPC over stdio: a small NDJSON client with kotlinx.serialization (lsp4j uses
  Content-Length framing).
- Tests: platform unit tests, Starter + Driver for whole-IDE tests, `runIdeSplitMode` for
  remote development; `verifyPlugin`, `signPlugin`, `publishPlugin`.

## UI

| Option | State | For calu |
|---|---|---|
| Swing with JetBrains components | works everywhere, also in remote development | most work: transcript, Markdown, streaming rebuilt |
| JCEF (embedded Chromium) | stable in a local IDE; unreliable in remote development (Kilo forbids it, Continue needed a workaround) | reuses the VSCode extension's webviews through a small `acquireVsCodeApi()` adapter and a generated `--vscode-*` stylesheet |
| Compose / Jewel | unclear for third-party plugins | not recommended |

Not verified: whether JCEF works in a *frontend* module in remote development.

## The three modes

    A  local IDE (also Windows IDE + WSL project) ── Eel spawn, fallback `wsl.exe -d <distro> --cd <dir> -- calu ide`
    B  JetBrains remote development ── backend module on the host starts `calu ide` as a local process
    C  local IDE + local project copy ── `ssh -T <host> calu ide --cwd <dir>` (system OpenSSH), plus file sync

One plugin with `shared`, `frontend` and `backend` modules serves A and B; mode C adds a third
way to start the bridge. The IDE's own SSH and Deployment plugins have no documented public
API (closed source): optional extras at most.

## Mode C: keeping a local copy in sync

| Option | Verdict |
|---|---|
| Through the bridge over the one SSH connection (`ide.fs.manifest/read/write/changed`) | first choice: needs only `ssh.exe`, no extra port; the bridge knows every edit and its before-hash, so conflicts are exact |
| git scratch ref | good for the first copy and bulk catch-up, too coarse per edit |
| Mutagen | fallback the user installs; not bundled (binary size; SSPL parts in official builds) |
| rsync | not on stock Windows |
| SFTP/scp, PyCharm Deployment | not as the basis: closed API, whole files, manual conflicts |

Design sketch: both sides at the same git commit, only the dirty overlay is synced
(`.gitignore` honoured); remote changes come as notifications and are applied through the
open document (undo works) or the VFS; local saves are uploaded with the hash of the content
they were based on, and a mismatch opens the IDE's three-way merge; offline changes are queued.

Additions `calu ide` would need: `--cwd`, `ide.ping`, `ide.fs.manifest`, `ide.fs.read`,
`ide.fs.write`, `ide.fs.delete`, `ide.fs.rename`, `ide.fs.subscribe` + `ide.fs.changed`,
`ide.git.head`, `ide.paths`, content hashes in `edit.diff`, a capability list in
`ide.initialize`.

## Existing plugins to look at

Licence note for a GPL-2.0-only project: MIT/BSD code may be reused; Apache-2.0, GPL-3.0 and
EPL code may not (ideas only).

| Plugin | Source | Licence | UI | Talks to its agent via | Worth |
|---|---|---|---|---|---|
| Kilo Code v7 | github.com/Kilo-Org/kilocode (`packages/kilo-jetbrains`) | MIT | Swing | spawns `kilo serve`, HTTP | best reference: built for remote development from the start; module layout, RPC rules, tests |
| Modular plugin template | github.com/JetBrains/intellij-platform-modular-plugin-template | Apache-2.0 | Swing chat demo | RPC | the skeleton to start from |
| Continue | github.com/continuedev/continue (`extensions/intellij`) | Apache-2.0 | JCEF, shared web GUI | core binary over stdio/TCP | shows the webview-reuse adapter; weak remote story |
| CC GUI | github.com/zhukunpenglinyutong/jetbrains-cc-gui | MIT | JCEF | drives the Claude/Codex CLIs | closest in purpose |
| Tabby | github.com/TabbyML/tabby (`clients/intellij`) | mixed | JCEF chat | agent over LSP/stdio | clean stdio supervision |
| AWS Toolkit / Amazon Q | github.com/aws/aws-toolkit-jetbrains | Apache-2.0 | JCEF | LSP server | mature CI and UI tests |
| ProxyAI (CodeGPT) | github.com/carlrobertoh/ProxyAI | Apache-2.0 | Swing | HTTP | native chat rendering |
| AutoDev | github.com/phodal/auto-dev | MPL-2.0 | Jewel + JCEF | in-process | the one sizeable Jewel example |
| coding-aider | github.com/p-wegner/coding-aider | GPL-3.0 | Swing/JCEF | runs the aider CLI | ideas only |
| Claude Code's plugin | closed | - | IDE terminal | the IDE runs a WebSocket MCP server (`openDiff`, selection) | interaction model for diff review |

Closed: Cline's JetBrains plugin, Copilot, Windsurf, Gemini, Junie, Codex. Dead: Roo Code
JetBrains, Sourcegraph's standalone repo.

## Recommended order (from the report)

Spikes first: (1) JCEF in a frontend module in split mode; (2) starting the bridge on the
owner's machine through Eel/`wsl.exe` and through `ssh.exe`; (3) the sync prototype.
Milestones: skeleton + bridge client + mode A → escalations, diffs, comments, checker editor,
session list → settings and presets → mode C → mode B hardening → Marketplace.
The cheap interim step: fix `session/new` in `calu acp` and document the `wsl.exe` wrapper.
