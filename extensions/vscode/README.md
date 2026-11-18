# calu for VSCode

Attach to [calu](https://github.com/kadeng/calu) sessions from the editor: chat with Claude Code
while checker agents and you review its commands and edits, answer escalations, edit the checker
instructions and calu's settings.

Status: the extension host side is in place; the chat and settings webviews are placeholders
until their UIs land (`WEBVIEW_CONTRACT.md` is their contract).

## How it runs

    VSCode (your computer)            workspace machine (local, WSL, SSH host, dev container)
    webviews: chat, settings  <-->    extension host  <--stdio-->  calu ide  <-->  calu serve, sessions

The extension is a workspace extension (`extensionKind: ["workspace"]`). With Remote-SSH, WSL or
a Dev Container, **everything runs on the remote machine**: the extension host, `calu ide`, the
calu server and the sessions; uv and calu are installed there too. Only the webviews are drawn
on your computer.

The extension starts `calu ide` and nothing else. Sessions run in `calu serve`, which you start
in a terminal (**calu: Start calu Server**): with tmux or screen it keeps running after VSCode
closes. Passwords (sudo, calu credentials for the server) are typed into that terminal.

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

Layout: `src/shared/*.ts`, `src/rpc.ts`, `src/caluCommand.ts`, `src/commentPayload.ts` and
`src/bridgeProtocol.ts` import no `vscode` and hold the logic (unit-tested). The other files in
`src/` are thin wrappers around the VSCode API. `webview/` holds the two webview scripts.
Integration tests in a real VSCode are not written yet.

License: GPL-2.0-only (see `LICENSE` and `LICENSE.md` at the repository root).
