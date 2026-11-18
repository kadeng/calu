# UI-testing desktop (`calu-uitest`)

A Linux desktop in a Docker container for testing the calu VSCode extension and, later, the
PyCharm plugin by screenshot, click and keyboard input. The desktop is the
[Cua](https://github.com/trycua/cua) Linux image; this directory adds VSCode, PyCharm and a small
command-line helper.

```
host (WSL2)                                   container calu-uitest
-----------                                   ---------------------
desktop.py --Cua SDK (gRPC, bearer token)-->  cua-spacesd :3211  --> XFCE on Xvfb :1 (1280x800)
browser    --http://127.0.0.1:56211/viewer/-> cua-spacesd :3211      user cua: VSCode, PyCharm
MCP client --http://127.0.0.1:56211/mcp ----> cua-spacesd :3211
```

## Start and stop

```bash
calu/uitest/run.sh              # build the image if missing, start or reuse the container
calu/uitest/run.sh --rebuild    # rebuild the image first (an existing container keeps the old image)
calu/uitest/stop.sh             # stop; the container and everything inside it are kept
calu/uitest/stop.sh --remove    # stop and delete the container (VSCode profile, PyCharm settings)
```

After a Dockerfile change: `stop.sh --remove`, then `run.sh --rebuild`.

| Item | Value |
| --- | --- |
| Container name | `calu-uitest` (plain `docker run`, no compose project) |
| Image | `calu-uitest:latest`, built `FROM ghcr.io/trycua/linux:24.04` |
| Published port | `127.0.0.1:56211` -> `3211` (cua-spacesd: gRPC, viewer, MCP). `CALU_UITEST_PORT` changes it. |
| Access token | `~/.cache/calu-uitest/token` (random, created by `run.sh`, mode 0600) |
| Mounts | calu repository -> `/workspace/calu` (read-only); `~/.cache/calu-uitest/share` -> `/share` (read-write) |
| Limits | 8 GB memory (`CALU_UITEST_MEMORY`), 1 GB `/dev/shm` |
| Desktop user | `cua` (uid 1000, home `/home/cua`, passwordless `sudo` inside the container) |

## Watch the desktop in a browser

```bash
uv run --no-project --with cua python calu/uitest/desktop.py viewer-url
uv run --no-project --with cua python calu/uitest/desktop.py viewer-url --view-only --ttl 3600
```

The command prints `http://127.0.0.1:56211/viewer/#<ticket>`. The page is cua-spacesd's HTML5
viewer (video, mouse, keyboard, clipboard, file upload). The ticket in the URL fragment expires
(default here: 8 hours, maximum 24) and permits only viewer calls. Without a ticket the page loads
but shows no desktop. There is no VNC server in this image.

## The helper: `desktop.py`

```bash
D="uv run --no-project --with cua python calu/uitest/desktop.py"
$D screenshot ~/.cache/calu-uitest/share/shot.png   # PNG of the whole desktop (1280x800)
$D click 640 400                                    # left click at pixel x y
$D type "hello"                                     # type into the focused window
$D key ctrl+shift+p                                 # one key or a chord: enter, escape, alt+F4
$D shell 'uv --version'                             # shell line as user cua; exit code is passed on
$D open-vscode                                      # VSCode on /workspace/calu
$D open-vscode /home/cua/work/demo                  # VSCode on another folder
$D open-pycharm /home/cua/work/calu                 # PyCharm on a writable folder
$D windows                                          # titles of the open windows
$D viewer-url                                       # browser link, see above
```

- The first run downloads the `cua` wheel (58 MB) and the SDK then fetches the `cua` CLI (95 MB)
  into `~/.cache/cua`. Later runs start in about a second.
- The helper sets `DO_NOT_TRACK=1` (Cua telemetry off) and `CUA_HOME=~/.cache/calu-uitest/cua-home`,
  so the SDK writes nothing to `~/.cua`.
- `shell` waits for the command. Start GUI programs with `setsid -f <program> >/tmp/x.log 2>&1`
  (the `open-*` commands do this).
- Coordinates are screenshot pixels, origin top left.

## Install the extension under test

Copy the `.vsix` to `~/.cache/calu-uitest/share/` on the host, then:

```bash
$D shell 'uitest-code --install-extension /share/x.vsix'
$D open-vscode
```

Use `uitest-code`, not plain `code`: the launcher passes the profile directories, so the
extension lands in the profile that `open-vscode` uses.

## Running the extension against a scripted session

No Claude Code, no API key and no sign-in are needed: `uitest/fake_server.py` runs the real
`calu.serve.Server` with a scripted runner and rule-based fake checkers, so "New session" in the
extension plays a prepared conversation.

```bash
# host: build the extension and put it into the exchange directory
(cd extensions/vscode && npm run check-types && npm run test:unit && npm run build && npm run package)
cp extensions/vscode/calu.vsix ~/.cache/calu-uitest/share/

# container: database, calu, demo project, fake server, extension, VSCode on ~/work/demo
$D shell 'bash /workspace/calu/uitest/setup-demo.sh'
$D screenshot ~/.cache/calu-uitest/share/shot.png
```

`setup-demo.sh` is idempotent; run it again after every change of the Python side (it copies
the host's working tree, uncommitted changes included, and restarts the fake server). Its
header lists the steps. `--no-vscode` leaves VSCode alone, `--stop-server` stops the fake
server (for the "no server" state of the extension). After a new `.vsix`:
`$D shell 'uitest-code --install-extension /share/calu.vsix --force'`, then "Developer: Reload
Window". A new VSCode profile (first-start layout): `UITEST_VSCODE_DATA=$HOME/.vscode-fresh`
before both `uitest-code` calls.

| Item | Value |
| --- | --- |
| Database | PostgreSQL of the container (apt), database `calu`, owner role `calu_owner` with a test password |
| Credentials | `~/.config/calu/credentials.json` of user `cua`: `admin` and `calu_admin` without a password (so `calu ide` asks none), `session` with a blank password |
| calu | `uv tool install` from `~/work/calu` (a copy of `/workspace/calu` without `.git`, `.venv`, `.env`), command `~/.local/bin/calu` |
| Demo project | `~/work/demo` (git repository; the scripted Edit changes `greeter.py`: `git -C ~/work/demo checkout greeter.py` resets it) |
| Fake server | tmux session `calu-fake`, log `/tmp/calu-fake-server.log` |

The first message of a session chooses the scenario: `scenario: <name>`; any other text plays
`tour`. The fake checkers decide by the markers `# calu:allow`, `# calu:deny` and
`# calu:escalate` in a scripted command.

| Scenario | What it plays |
| --- | --- |
| `tour` | streamed Markdown, thinking, Read, a real Edit (diff view), commands that pass, are allowed, denied and escalated, two subagents, a question; turn 2 and 3 escalate and ask after 4 s (notifications) |
| `subagents` | a foreground subagent, two at once (one escalates), one that fails, a background one that outlives the turn for a minute and starts the follow-up turn |
| `stop` | a slow text, a command of a minute, an escalating command: one per turn, to interrupt |
| `queue` | turns of 15 s to send queued and injected messages into, then an escalating command |
| `levels` | Read, Edit and a command per turn, for the supervision level |
| `edge` | a line of 3,000 characters, a result of 60,000, a code block of 300 lines, raw HTML, a `javascript:` link, failed calls, several scripts; turn 2 asks two questions (several choices, one choice) |
| `many` | 320 edits and 200 commands in one turn (more edits than the extension keeps diffs for) |
| `escalations` | three escalating commands at the same time |
| `nousage` | as `levels`, without context usage |

What the scripted runner does not play: the earlier turns of a resumed or forked conversation
(it has no transcript; the request and its fork point are real), an answer to a message for a
subagent, and Claude Code's own slash commands (a line names the command instead).

## What is installed, and where

| Tool | Version (2026-11-18) | Location | Source |
| --- | --- | --- | --- |
| Ubuntu, XFCE, cua-spacesd | 24.04, cua-spacesd 0.16.1 | base image | `ghcr.io/trycua/linux:24.04` (full tier) |
| uv | 0.12.19 | `/usr/local/bin/uv` | base image (the Astral installer in the Dockerfile runs only if `uv` is missing) |
| Node | 24.21.0 (with pnpm) | `/usr/local/bin/node` | base image |
| git, tmux | 2.43.0, 3.4 | `/usr/bin` | base image, apt |
| VSCode | 1.147.0 | `/usr/share/code`, command `code` | official `.deb`, SHA-256 pinned |
| PyCharm (unified) | 2026.3, build 263.7307.206 | `/opt/pycharm`, command `pycharm` | JetBrains tarball, SHA-256 pinned |
| `uitest-code` | | `/usr/local/bin` | this directory |
| `uitest-pycharm` | | `/usr/local/bin` | this directory |

`uitest-code` runs `code --no-sandbox --password-store=basic --user-data-dir ~/.vscode-uitest
--extensions-dir ~/.vscode-uitest/extensions` with `DONT_PROMPT_WSL_INSTALL=1`. Without that
variable `code` stops with a question on the terminal, because the container shares the WSL2
kernel. `rm -rf /home/cua/.vscode-uitest` resets VSCode.

Versions are build arguments at the top of the `Dockerfile`; update the URL and the SHA-256
together.

## First-start dialogs (a person decides)

- **VSCode** shows "Welcome to VS Code / Sign in to use GitHub Copilot" on the first start. During
  setup "Continue without Signing In" and "Get Started" were clicked; nobody is signed in.
  `/workspace/calu` opens in Restricted Mode until someone clicks "Manage" -> "Trust".
- **PyCharm** shows the "PyCharm User Agreement" (JetBrains User Agreement) on the first start,
  then asks about data sharing and about a licence or trial. The helper answers none of these.
  A person opens the viewer link, reads and accepts or declines. The answers are stored in
  `/home/cua/.config/JetBrains` and `/home/cua/.java` and survive `stop.sh` (not `--remove`).
- PyCharm writes `.idea/` into its project, and `/workspace/calu` is read-only. Work on a copy:
  `$D shell 'mkdir -p ~/work && git clone /workspace/calu ~/work/calu'`.

## Constraints

- The scripts touch only the container `calu-uitest`. The database containers
  (`calu-db-postgres-1`, `calu-dev-caludb-1`) are separate and must stay untouched.
- No `--privileged`, no Docker socket, no host home directory, no `~/.config`, `~/.claude`,
  `~/.ssh`, and no API keys or tokens in the container. The only secret is the container's own
  access token.
- The repository mount is read-only, but it is the whole checkout: files such as `.env` and
  `.venv/` in the repository are readable inside the container.
- Ports bind to 127.0.0.1 only.
- Downloads live in Docker images and `~/.cache/calu-uitest/`, `~/.cache/cua`, `~/.cache/uv`.

## Optional: MCP server for Claude Code

cua-spacesd serves the Cua Driver tools (screenshots, windows, accessibility tree, click, type) as
a Streamable-HTTP MCP server at `/mcp` on the same port, with the same token. Nothing is
registered by default. To register it for the current project:

```bash
claude mcp add --transport http calu-uitest http://127.0.0.1:56211/mcp \
  --header "Authorization: Bearer $(cat ~/.cache/calu-uitest/token)"
```

Remove it with `claude mcp remove calu-uitest`. The token is written into Claude Code's
configuration; it changes only when `~/.cache/calu-uitest/token` is deleted and the container is
recreated. These MCP tools have no shell tool; use `desktop.py shell` for commands.

## Cua facts this setup relies on

- Licence: the Cua SDK, CLI and Cua Driver are MIT. cua-spacesd and the viewer are
  source-available under FSL-1.1-MIT (free to use and self-host, no competing hosted service).
- The older pieces (`trycua/cua-xfce` and `trycua/cua-ubuntu` images, `cua-computer`,
  `cua-computer-server` on port 8000, `cua-agent`, `cua-mcp-server`) are deprecated. Their
  replacements are the `ghcr.io/trycua/linux` image, cua-spacesd on port 3211 and the `cua`
  package on PyPI.
- Sources: <https://github.com/trycua/cua> (README, `libs/images/README.md`,
  `libs/cua-spacesd/README.md`, `libs/cua/python/README.md`),
  <https://cua.ai/docs/cua-sdk/guides/desktop>, <https://cua.ai/docs/cua-sdk/guides/own-harness>,
  <https://cua.ai/docs/cua-sdk/guides/spacesd>, <https://cua.ai/docs/cua-sdk/concepts/telemetry>.
