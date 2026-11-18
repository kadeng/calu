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
| `bidi` | text, a file name and commands with bidirectional control characters (shown as placeholders such as `⟦RLO⟧`), next to ordinary Arabic and Hebrew paragraphs; one command escalates |
| `tall` | an escalating command of 90 lines: an approval card taller than the view |
| `swarm` | ten subagents at the same time (eight commands of 3 s each); the third escalates after about 6 s, the seventh after about 15 s, the others keep working; turn 2 starts three background subagents |
| `nousage` | as `levels`, without context usage |

What the scripted runner does not play: an answer to a message for a subagent (the message
itself is shown in the subagent's tab), and Claude Code's own slash commands (a line names the
command instead). A resumed, forked or rewound session starts its script from the beginning;
its earlier turns are real rows of the database, and the extension shows them above the
transcript ("Earlier in this conversation").

Useful `desktop.py shell` lines for a run: `pkill -f "calu ide"` ends the bridge of every
VSCode window (the chat then offers "Restart bridge"); `DISPLAY=:1 xdotool mousemove X Y`
hovers; `DISPLAY=:1 xdotool key --clearmodifiers ctrl+v` pastes what `xclip -selection
clipboard -t image/png -i file.png` put on the clipboard.

## Running the extension against a real Claude Code

`uitest/setup-real.sh` replaces the scripted server by `calu serve` with a real Claude Code and
real checker models. It costs money on every step, so the owner decides when it runs and with
which key.

```bash
# host: the key file, two lines NAME=value (ANTHROPIC_API_KEY, optionally ANTHROPIC_BASE_URL)
install -m 600 /dev/null ~/.cache/calu-uitest/share/anthropic.env   # then fill it in an editor

# container
$D shell 'bash /workspace/calu/uitest/setup-real.sh'               # everything
$D shell 'bash /workspace/calu/uitest/setup-real.sh --no-deploy'   # keep the installed calu
$D shell 'bash /workspace/calu/uitest/setup-real.sh --stop-server'
$D shell 'bash /workspace/calu/uitest/setup-real.sh --remove-key'  # delete the key files
```

| Item | Value |
| --- | --- |
| Claude Code | native installer (`https://claude.ai/install.sh`), `~/.local/bin/claude`; no login |
| The key, for calu's agents | `~/.config/calu/.env` (mode 600): checkers, narrator, summarizer, trace redactor |
| The key, for Claude Code | the `env` block of `~/.claude/settings.json` (mode 600) |
| Models | every role `anthropic:claude-haiku-5-5` (`calu config models`), `claude.model = haiku` |
| Sandbox | off: Docker without privileges cannot create user namespaces (`bwrap --unshare-user` fails), although bubblewrap and socat are installed |
| Server | tmux session `calu-real`, log `/tmp/calu-real-server.log`; calu's own log `~/.local/state/calu/log/serve.log` |

The script never prints a value of the key file, moves the file out of `/share` and deletes
it there. Both places are needed: calu blanks every variable of its env file in the
environment it hands to Claude Code, so Claude Code reads the key from its own settings.
`--remove-key` deletes both copies; revoking the key is the owner's step.

`setup-demo.sh` and `setup-real.sh` install calu from the host's working tree, uncommitted
changes of every session in that checkout included. To test a commit instead, export it into
the exchange directory and name it:

```bash
git archive HEAD -- . ':(exclude)extensions' | tar -x -C ~/.cache/calu-uitest/share/src
$D shell 'CALU_UITEST_SOURCE=/share/src bash /share/src/uitest/setup-real.sh'
```

A long `desktop.py shell` line is cut off after 120 seconds; start the script detached
(`setsid -f bash -c "... > /tmp/setup-real.log 2>&1"`) and read the log.

### What a real session does that the scripted runner does not (CLI 2.1.327, Haiku)

| Area | Real Claude Code | Scripted runner |
| --- | --- | --- |
| Tool calls | Several in one message: the `tool.use` of call 2 arrives before the `check.result` of call 1; `file_path` is absolute; Haiku often reads with `cat` in Bash instead of Read | one call after the other, relative paths |
| Cost | `turn.result.cost_usd` is the total of the Claude Code process, and `num_turns` the model calls of that turn | no cost |
| `/cost`, `/usage`, `/context` | run without the model: a text of the model `<synthetic>`, a turn with 0 model calls, no replay of the input | a line that names the command |
| `/compact` | status "compacting", a summary as a user-role text, `<local-command-stdout>Compacted`, and a SubagentStop of the agent that wrote the summary | as above |
| `/clear` | a new Claude session id, cost counted from 0, no output | as above |
| `/help` | "/help isn't available in this environment." | as above |
| Interrupt | the turn ends as `error_during_execution` with the error flag, after the user-role text "[Request interrupted by user]"; the SDK runner reports it as "interrupted" | "interrupted" |
| Fork, rewind | Claude Code checks the fork point; a fork that discards more than the newest turn is started a second time without the check | the script starts again |
| Background subagent | every pause of the subagent calls SubagentStop: the result checker reviews an interim result, its note starts a turn of the main agent ("still waiting") | one result at the end |
| Message to a subagent | the main agent loads SendMessage through ToolSearch and relays; the subagent reads it at its next tool round | shown, not answered |
| A foreground `sleep N` | refused by Claude Code's Bash tool ("Blocked: standalone sleep"); use `python3 -c 'import time; time.sleep(N)'` for a long command | runs |
| Questions | AskUserQuestion with a header, options with descriptions | the scripted questions |

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
  access token. The one exception is a run of `setup-real.sh`, which the owner decides: it
  holds the API key the owner put into the exchange directory, until `--remove-key`.
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

## The root box (`calu-rootbox`)

A second container, without a desktop: a machine with root, sudo and three users, for running
calu's privileged arrangements for real (calu as root with `--user`, through `--sudo`, with and
without passwords; test plan section E). The host needs no sudo for it, and nothing of the host
but the calu checkout (read-only) is mounted.

```bash
calu/uitest/rootbox/run.sh              # build if missing, start, install calu inside (root and ada)
calu/uitest/rootbox/run.sh --rebuild    # after a change to the Dockerfile or the scripts beside it
docker exec -it calu-rootbox bash                # a root shell
docker exec -it -u ada calu-rootbox bash -l      # ada; also bob, carol
docker exec calu-rootbox rootbox-install         # install calu again after a change to the checkout
docker stop calu-rootbox                         # stop; `run.sh` starts it again
docker rm -f calu-rootbox                        # delete it with everything set up inside
```

| Item | Value |
| --- | --- |
| Image | `calu-rootbox:latest`, built `FROM ubuntu:24.04` (sudo, uv, git, tmux, bubblewrap, socat, PostgreSQL 16) |
| Ports, rights | no published port, not privileged, no Docker socket |
| `root` | calu installed as the guide says for `--sudo`: `/usr/local/lib/calu`, links in `/usr/local/bin` |
| `ada` | uid 1000, sudo without a password; a second calu in her home (`~/.local/bin`, first on her `PATH`), which `--sudo` must refuse |
| `bob` | uid 1001, sudo asks his password: `bob-sudo` |
| `carol` | uid 1002, no sudo: another user of the machine, who must reach nothing |
| Databases | two PostgreSQL clusters on 127.0.0.1, the "existing server" variant: port 5432 for root's calu, port 5433 for a user's own. Owner login `calu_owner`, password `owner-pw`, databases `calu` and `calu_old` |

```bash
# as root, or as ada with sudo in front: passwords on standard input, in the order the guide gives
printf 'owner-pw\nadmin-pw\nadmin-pw\nsess-pw\nsess-pw\n' | \
    calu setup --database server --host 127.0.0.1 --port 5432 --dbname calu --user calu_owner
```

What stands in for Claude Code and the checkers:

- **`fakecalu ...`** is calu's own command line with the scripted runner and the fake checker
  models of `fake_server.py` (`rootbox/fakecalu.py`). Everything else is calu's code: the
  options, the credential store, the session password, `calu.asuser`, the sockets, the log
  file. `fakecalu serve --user ada` as root, or `sudo fakecalu serve --user "$USER" --from-sudo`
  as ada or bob (the command line `calu serve --sudo` builds), runs sessions that play the
  scenarios above. `fakecalu-user` is the same with the interpreter of the caller's own
  installation, for a server that runs as ada alone.
- **`claude`** on `PATH` is a stand-in that records who started it (uid, gid, groups, home,
  working directory, arguments, the names of its environment variables) in
  `/var/tmp/standin/*.json` and says nothing. A session uses it after `calu config set
  claude.cli_path /usr/local/bin/claude`; it then fails at its start after 60 s, which is
  the case "an error in a session". Without that setting a session starts the Claude Code
  that the Agent SDK bundles, which runs and answers "Not logged in".
- **Helpers:** `ptyrun 'command' [line ...]` runs a command on a terminal of its own, types
  the lines and counts the password prompts; `webcheck PORT 'calu web' [terminal password]
  [browser password]` starts the web interface and fetches the front page with and without
  a login; `idedrive [password]` sends `calu ide` a few requests; `rootbox-reset root|ada`
  puts a database and its credential store back to "never set up".

What the box cannot do: Docker is not available inside, so the `docker` variant of `calu
setup` cannot run; bubblewrap cannot create user namespaces in an unprivileged container, so
Claude Code's sandbox cannot start (`calu doctor` reports it as "sandbox start"); there is no
`/run/user/<uid>`, so `--insecure` sockets of a user are below `/tmp/calu-<uid>`.

The root-only tests (`tests/root/`, `docs/TESTING.md`) run in the box:
`docker exec calu-rootbox rootbox-tests`.
