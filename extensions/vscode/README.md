# calu for VSCode

Attach to [calu](https://github.com/kadeng/calu) sessions from the editor: chat with Claude Code
while checker agents and you review its commands and edits, answer escalations, edit the checker
instructions and calu's settings.

Status: first version; the chat and the settings dialog follow `WEBVIEW_CONTRACT.md`.

## Where the chat is

The view **calu** opens in the secondary side bar (on the right in VSCode's default layout,
next to the editor's own chat), not in the activity bar on the left. Show it with
**calu: Open Chat** or **View: Toggle Secondary Side Bar** (Ctrl+Alt+B). Drag the view's title
to the activity bar or the panel to keep it elsewhere; VSCode remembers the place. The
extension needs VSCode 1.106 or newer: that version introduced view containers of extensions
in the secondary side bar (`contributes.viewsContainers.secondarySidebar`).

The **calu** item in the status bar (right) is always there: "calu: 2 waiting" with the
warning colour while decisions wait, otherwise the attached session's state ("working",
"idle", "connection lost", "not installed"). A click opens the chat. A new VSCode profile shows
its own chat in the secondary side bar first; calu's view is then one of its tabs or behind
its "…".

## How it runs

    VSCode (your computer)            workspace machine (local, WSL, SSH host, dev container)
    webviews: chat, settings  <-->    extension host  <--stdio-->  calu ide  <-->  calu serve, sessions

The extension is a workspace extension (`extensionKind: ["workspace"]`). With Remote-SSH, WSL or
a Dev Container, **everything runs on the remote machine**: the extension host, `calu ide`, the
calu server and the sessions; uv and calu are installed there too. Only the webviews are drawn
on your computer.

The extension starts `calu ide` and nothing else. Sessions run in `calu serve`, which you start
in a terminal (**calu: Start calu Server**): with tmux or screen it keeps running after VSCode
closes. Passwords the server needs (sudo, the calu user password) are typed into that terminal.
The chat itself asks for the calu user password at most once per window, and never if it is
empty.

## What the chat shows

- **Earlier turns** of a resumed, forked or rewound conversation stand above the session's own
  output, under "Earlier in this conversation". They come from calu's record: your messages,
  each step with its decision and the checker's summary, and Claude's answers; thinking and
  diffs are not recorded. "Rewind" and "Fork from here" work on recorded messages too. The
  first message of a conversation has nothing before it, so both are disabled there.
- A session cut from another one is named "fork of <name>" or "rewind of <name>"; rename it
  with `/rename`.
- **A message to a background subagent** (written in its tab) is shown in that tab; Main only
  notes "Sent to subagent <name>". The main agent relays the text.
- **New session** shows at once that the calu server is asked, with **Cancel**; after three
  seconds without an answer it says that no server may be running and offers **Start calu
  server** while the request keeps waiting.
- **If the bridge (`calu ide`) stops**, open approval cards stay open and say "connection lost"
  (nothing was decided). **Restart bridge** attaches to the same session again and continues
  the transcript; the session list appears only if that session is gone.
- **If the calu server stops**, the chat says "The calu server stopped" with **Start server**
  (a terminal with the server command, as "Open terminal with this command") and **Retry**.
  The last session's transcript stays readable, greyed under "Session ended"; its open cards
  become one line "Session ended" each, since nothing can decide them any more. When a server
  runs again (Retry, or the chat's own check every 10 seconds while it is visible, every 30
  seconds otherwise) the chat attaches as at the start: the same session if it still runs,
  else the session of the workspace, else it offers a new one. A loss for another reason (a
  revoked token, a refused connection) keeps "connection lost" with "Attach again".
- **"Waiting for you"** under the transcript counts from the time the session gives for the
  oldest open step, so a window reload, a bridge restart or a reattach does not start it again.
- **Approval cards** keep Allow and Deny at the lower edge of the view while a long card
  scrolls.
- **Honesty mode**: the menu stores `honesty.mode` in calu's settings; the running session
  reads the change and the chip follows. If the session does not report the stored mode, the
  chip shows both ("stored: track").
- **Usage panel**: a click on the context chip below the message field asks the session
  (`usage.panel`) and shows the 5-hour and weekly limits as Claude Code reported them, each
  with the time of the report or "not reported yet by Claude Code", the plan's limits when
  Claude Code can read them, Claude Code's own `/usage` text, the cost and the context fill.
  The request makes no model call and starts no turn. **Ask again** repeats it.
- **Images**: up to 4 per message, at most 5 MB each (what a session accepts); a larger one is
  refused with a message and nothing is sent. A click on a thumbnail shows the image large.
- **Text direction**: invisible characters that reorder text (right-to-left override and the
  like) are shown as placeholders such as `⟦RLO⟧`, so a command or file name reads as it is
  stored. Arabic and Hebrew text reads right to left as usual.

## Install

1. Install the extension (`calu.vsix`, built with `npm run package`).
2. If calu is missing on the workspace machine, the chat view offers **Install calu**: uv is
   installed first if needed (official script from astral.sh, into `~/.local/bin`), then
   `uv tool install --python 3.14 <calu.install.source>`. **calu: Update calu** runs
   `uv tool upgrade calu`.
3. Claude Code (`claude`) is only checked, never installed: see
   <https://docs.anthropic.com/en/docs/claude-code>.
4. `calu serve --sudo` needs calu installed for root; **calu: Show Root Installation Command**
   shows the command to run in a terminal. The extension does not run it.

## Settings of the editor (`calu.*`)

`calu.command`, `calu.install.source`, `calu.server.sudo`, `calu.server.insecure`,
`calu.server.extraArgs`, `calu.chat.showThinking`, `calu.chat.escalationSound`. Everything about
calu itself is stored in calu's database and edited in **calu: Open Settings**.

## Development

    npm install            # once
    npm run gen:protocol   # after changes of calu's session protocol (needs uv)
    npm run check-types
    npm run test:unit      # vitest, pure modules only
    npm run build          # dist/extension.js, dist/webview.js, dist/settings.js
    npm run package        # calu.vsix
    scripts/test-in-docker.sh   # unit and integration tests in a real VSCode under xvfb (Docker)

Layout: `src/shared/*.ts`, `src/rpc.ts`, `src/caluCommand.ts`, `src/commentPayload.ts` and
`src/bridgeProtocol.ts` import no `vscode` and hold the logic (unit-tested). The other files in
`src/` are thin wrappers around the VSCode API. `webview/` holds the two webview scripts.
`test/integration/` holds the integration tests (`@vscode/test-cli`, `.vscode-test.mjs`) and
the scripted session they attach to; `docs/TESTING.md` says what they cover.

License: GPL-2.0-only (see `LICENSE` and `LICENSE.md` at the repository root).
