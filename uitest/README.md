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

## Running the PyCharm plugin in the desktop

The plugin (`extensions/jetbrains`) runs in the desktop's PyCharm against the same scripted
server as the VSCode extension.

```bash
# host: build the plugin (extensions/jetbrains/README.md, "Building") and put it into the exchange directory
(cd extensions/jetbrains && ./gradlew check buildPlugin)
cp extensions/jetbrains/build/distributions/calu-*.zip ~/.cache/calu-uitest/share/

# container: calu, database, demo project, scripted server; then the plugin into PyCharm's plugins directory
$D shell 'bash /workspace/calu/uitest/setup-demo.sh --no-vscode'
$D shell 'pkill -f "[/]opt/pycharm/bin/pycharm"; sleep 3; cd ~/.local/share/JetBrains/PyCharm2026.3 && rm -rf calu && unzip -q /share/calu-*.zip'
$D open-pycharm /home/cua/work/demo
```

- Write `pkill -f "[/]opt/..."` with the bracket: `desktop.py shell` runs the line through a
  shell whose own command line would match the plain pattern.
- PyCharm asks once whether to trust `~/work/demo` (and once more for `~/work`); the tool
  window "calu" is in the right stripe, under "More tool windows" until it was opened once.
  The notification "Cannot update plugin" at each start belongs to a plugin unpacked by hand.
- Menus and popups of the IDE react only after a mouse move: move, then click
  (`DISPLAY=:1 xdotool mousemove X Y; sleep 0.2; xdotool click 1`). `desktop.py key` knows no
  `grave`; use `xdotool key ctrl+grave` (Quick Switch Scheme: theme light / dark).
- The IDE's log: `~/.cache/JetBrains/PyCharm2026.3/log/idea.log`; the plugin's lines contain
  `[calu]`, among them the time of the chat's renders (every 500th, and each one over 100 ms).
- `calu serve` runs at most ten sessions (`--max-sessions`); the scripted server keeps the default.

| Script | What it adds |
| --- | --- |
| `setup-password-store.sh` | a second calu store with passwords, for the one-password flow: its own XDG directories below `~/pwstore`, its own PostgreSQL cluster on port 5433 (calu's roles are server-wide), a scripted server in the tmux session `calu-fake-pw`. Admin password `admin-pw`, session password `sess-pw`. The plugin reaches it with Settings, Tools, calu, "calu command" = `/home/cua/pwstore/calu`. `--stop` stops its server |
| `pycharm-split.sh` | JetBrains remote development on the desktop: copies the plugin's sources to `~/work/jb` and runs `./gradlew runIdeSplitMode -PlocalIde=/opt/pycharm` in the tmux session `calu-split` (a JDK 25 in `/share/jdk` is needed once; close the ordinary PyCharm first: backend, client and Gradle need about 6 GB). `--stop` ends it. The thin client shows the "JetBrains Team Tools User Agreement" at its first start; a person decides |

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

## The SSH box (`calu-sshbox`)

A third container, without a desktop: a server that is reached over SSH only, for the PyCharm
plugin's SSH mode (requirement 35: "the server needs no Desktop and runs Linux with SSH
listening, with passwordless public key auth for simplicity"). calu, PostgreSQL and a scripted
`calu serve` run inside; the plugin on the desktop (or a test on the host) starts
`ssh sshbox calu ide --cwd /home/dev/project` and synchronises files through that bridge.

```
host (WSL2)                         network calu-uitest-net                 container calu-sshbox
ssh -F ~/.cache/calu-uitest/ssh/config sshbox ── 127.0.0.1:56522 ──────►  sshd :22 (key login only, user dev)
container calu-uitest: ssh sshbox ───────────── sshbox:22 ─────────────►  calu ide --cwd /home/dev/project
                                                                          PostgreSQL, fake_server.py in tmux
```

```bash
calu/uitest/sshbox/run.sh                  # key pair, image, network, container, calu inside, the desktop's key
calu/uitest/sshbox/run.sh --rebuild        # after a change to the Dockerfile or the scripts beside it
calu/uitest/sshbox/run.sh --reset-project  # install calu again and create /home/dev/project anew
docker exec calu-sshbox /usr/local/sbin/sshbox-install    # install calu again after a change to the checkout
docker exec -u dev calu-sshbox sshbox-server --stop       # stop the scripted servers ("no server answers")
calu/uitest/sshbox/stop.sh                 # stop; `run.sh` starts it again (its servers start with it)
calu/uitest/sshbox/stop.sh --remove        # delete the container and the network (the key is kept)
```

| Item | Value |
| --- | --- |
| Image | `calu-sshbox:latest`, built `FROM ubuntu:24.04` (openssh-server, sudo, git, tmux, uv, PostgreSQL 16) |
| Limits, rights | 2 GB memory, not privileged, no Docker socket; only the calu checkout is mounted (read-only, `/src/calu`) |
| Network | the user-defined network `calu-uitest-net`; the box has the alias `sshbox`. `run.sh` connects the running desktop container to it with `docker network connect` (no restart) |
| Published port | `127.0.0.1:56522` -> `22` (`CALU_SSHBOX_PORT` changes it), for tests that run on the host |
| `sshd` | public keys only: no password, no keyboard-interactive, no root login, `AllowUsers dev` |
| User | `dev` (uid 1000, sudo without a password inside the container) |
| The key | `~/.cache/calu-uitest/ssh/id_ed25519` on the host: made by `run.sh` for the tests, no passphrase. `run.sh` installs its public half for `dev` and copies the private half to the desktop's user `cua` (`~/.ssh/calu_sshbox`, mode 600, with a `Host sshbox` entry in `~/.ssh/config`). The owner's keys, agent and `~/.ssh` are never read or changed; the host's entry for the box is the separate file `~/.cache/calu-uitest/ssh/config` |
| calu | `uv tool install` for `dev` from a copy of the checkout; `/usr/local/bin/calu` links to it, because an SSH command runs without a login shell and so without `~/.local/bin` on its `PATH` |
| Stores | without passwords: the cluster on port 5432 (`calu ide` as `dev` asks nothing). With passwords: `/home/dev/pwstore/calu` (cluster on port 5433, admin password `admin-pw`, session password `sess-pw`), for the one-password rule over SSH: set the plugin's "calu command" to that path |
| Project | `/home/dev/project`: a git repository with Python files, a `.gitignore` (and an ignored `build/`), `assets/blob.bin` (200 kB of random bytes), `many/` with 300 small files, `link-inside.py` (a link to a file of the tree) and `link-outside` (a link to `/etc/hostname`) |
| Scripted servers | tmux sessions `calu-fake` and `calu-fake-pw` of `dev` (`uitest/fake_server.py`); logs `/tmp/calu-fake-server.log`, `/tmp/calu-fake-pw-server.log` |

The desktop image has the OpenSSH client (`openssh-client` in the `Dockerfile`): the plugin's
SSH mode and PyCharm's remote development start the system's `ssh`. A container built before
that line was added needs it once:
`$D shell 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-client'` (`run.sh`
says so if it is missing). PyCharm's JetBrains daemon (`jetbrainsd`) looks for `ssh` when it
starts: after installing the client by hand, end it (`pkill -f "[j]etbrainsd run"`; it starts
again by itself), or "Connect to Remote" fails with "Unexpected error" ("Failed to find OpenSSH
client executable" in `~/.local/share/JetBrains/Daemon/logs/`).

Running the plugin's SSH mode in the desktop: install the plugin as above, open an **empty**
writable folder (`$D shell 'mkdir -p ~/work/sshproj'`, `$D open-pycharm /home/cua/work/sshproj`;
PyCharm puts a sample `main.py` into an empty folder, delete it for a plain clone), then
Settings, Tools, calu: "How calu is reached" = "On a server, over SSH", SSH host `sshbox`,
remote project directory `/home/dev/project`. The plugin offers "Clone from Server". Useful
lines: `pkill -f "[s]sh -T -o BatchMode"` in the desktop cuts the bridge's connection (the
plugin connects again); `docker network disconnect calu-uitest-net calu-uitest` and `docker
network connect --alias calu-uitest calu-uitest-net calu-uitest` take the network away and give
it back; `docker stop calu-sshbox` / `docker start calu-sshbox` stop and start the server.

The plugin's tests against the box (the host needs the box running, nothing else):

```bash
cd extensions/jetbrains
./gradlew :shared:test --tests '*RealBridgeSyncTest*' -Psshbox=$HOME/.cache/calu-uitest/ssh/config
```

Without `-Psshbox` (or the environment variable `CALU_SSHBOX`) the test over SSH is skipped;
the same walk against `uv run calu ide` as a local process always runs when `uv`, `git` and
the checkout are there.

