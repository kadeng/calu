# calu for PyCharm and other JetBrains IDEs

A tool window "calu" (on the right) that shows a Claude Code session which calu runs and checks:
the transcript with the checkers' decisions, cards to allow or deny a waiting step, the diff of
every edit, and calu's settings. It offers what the VSCode extension offers
(`../vscode`, `agent/records/notes/vscode-extension-spec.md`) and reaches calu in three ways.

State: fourth round. The plugin runs in PyCharm 2026.3 on Linux in all three modes below, each
walked through in the UI-test desktop ("State" at the end, `agent/records/notes/test-plan.md` sections B
and H): a local IDE; JetBrains remote development with the IDE backend on a headless SSH server
and the JetBrains Client on the desktop; a local copy kept equal over SSH, with PyCharm's SSH
interpreter running and debugging the code on the server.

## How calu is reached

    A  local IDE ─────────────────────────── calu ide            (child process in the project directory)
       Windows IDE, project in WSL ───────── wsl.exe -d <distro> --cd <dir> -- calu ide
    B  JetBrains remote development ──────── calu ide            (started by the backend on the remote host;
                                                                  the JetBrains Client only shows what it opens)
    C  local IDE + local copy of the project ─ ssh -T -o BatchMode=yes ... <host> 'calu ide --cwd <dir>'

In every mode the plugin speaks the protocol of `calu ide` (JSON-RPC over newline-delimited
JSON) and nothing else: it opens no socket and no database. It assumes a running `calu serve`
and never calls sudo; when no server answers it shows the tmux or screen command and can open a
terminal with it.

Mode C needs the system's OpenSSH client and a key that needs no passphrase at that moment
(in the SSH agent, or without one): the plugin never types a passphrase (`BatchMode`) and says
what to do instead of hanging (`ConnectTimeout=15`). It reconnects with a growing wait (0.5 s
to 30 s) and attaches again with `after_seq`, so the transcript continues. calu must be on the
`PATH` of a non-interactive SSH command on the server (`~/.local/bin` is not: link calu into
`/usr/local/bin`, or set the plugin's "calu command" to its full path).

## SSH mode and file synchronisation

Settings | Tools | calu: "How calu is reached" = "On a server, over SSH", the SSH host (an
alias of `~/.ssh/config` or `user@host`), the remote project directory, and "Sync":

- **Through calu** (default): this project folder is kept equal to the remote directory
  through the bridge (`ide.fs.*`, `agent/records/notes/jetbrains-sync-spec.md`); no second connection
  and no other tool. It refuses to start, with what to do, if the server's bridge reports no
  `fs` capability.
- **None (I sync myself)**: only the chat is remote; file links and diffs open the local file
  of the same relative path, and no `ide.fs.*` request is sent (the IDE's Deployment, rsync
  or Mutagen keep the copy equal).

Remote development (mode B): File | Remote Development | SSH, `user@host`, the project
directory; PyCharm installs its backend on the server. The plugin must be in both halves: on the
server in `~/.local/share/JetBrains/PyCharm2026.3/`, in the client in
`~/.local/share/JetBrains/PyCharm2026.3/frontend/` (Linux paths; the client reads its plugins
from that `frontend` folder, so a plugin installed only for the ordinary IDE is missing there).
Diffs, files, checker instructions and the terminal with the server command open on the server
and appear in the client. A comment in the diff of an edit finds its file through the backend
(the client's diff editor knows only its text).

What the user goes through the first time in mode C:

    1  open a local folder (empty for a clone) and fill the three settings
    2  the plugin connects (a card says "Connecting to <host> over SSH…"; a wrong host, a
       refused key, an unknown host key and a missing calu each get their own sentence)
    3  an empty folder: "The local folder is empty. Clone 307 files (202 KiB) from host:dir?"
       [Clone from Server] [Not Now]. A folder with files: the plan (download, upload, delete,
       files that differ on both sides) and [Apply] [Not Now]. Nothing is changed before that.
       PyCharm puts a sample `main.py` into a folder it opens empty: it is listed as an upload.
    4  the files arrive (a progress indicator with a cancel button; the status bar counts)
    5  the tool window lists the server's sessions; New Session runs on the server

Afterwards:

    the agent edits a file         -> the local file and an open editor follow within about a second
    a local save                   -> on the server at once (300 ms after the last change)
    a save while the agent edits   -> held ("1 file held until the turn ends", "Upload now");
      that file in a running turn     merged with the agent's change when the turn ended
    both sides changed             -> different lines: merged on both sides, a line in the chat;
                                      the same lines: nothing is overwritten, a card and a
                                      notification offer the IDE's three-way merge dialog
    unsaved text in an editor      -> merged with a remote change; never uploaded before it is saved
    no connection                  -> "calu sync: offline: 2 changes queued"; the queue survives a
                                      restart and is replayed, then both sides are compared
    links, files above 32 MiB,     -> never followed or copied; listed ("3 paths are not
      what git ignores                synchronised", Show)

Conflicts are never resolved silently. Every case ends with both sides equal or with a
conflict that keeps both contents until the user decides: the same lines changed on both sides
(saved, or unsaved text in an editor), a change against a deletion in either direction, a
rename on one side while the other edits the old file, a new file of the same name on both
sides, a held file whose turn ended with an overlap, an offline queue replayed onto a changed
file. A conflict is shown by a card ("Merge …"), a notification that closes when it is
resolved, the status bar, and the project view (the file in red, "sync conflict"; its folders
"contains a sync conflict"); it survives a restart of the IDE with its merge base.
`SynchronizerTest` has one test per case.

`.git` is never synchronised, in either direction: the server's repository is the one calu and
Claude commit to. A local copy that gets a `.git` of its own (git init, "Enable Version Control
Integration", a clone into a repository) is listed under "not synchronised" with a warning
once per IDE start that says so; git commands belong on the server (an SSH terminal, or
Claude).

Running and debugging on the server: add PyCharm's SSH interpreter for the same host
(Add Interpreter | On SSH), sync folder `<Project root>` → the remote directory, and leave
"Automatically upload project files to the server" off. Run and Debug then start the code on
the server; breakpoints and line numbers match because calu keeps both copies equal (checked
after an edit on the server that moved lines). If PyCharm's Deployment uploads changed files
by itself into the same directory, the plugin warns in the chat and with a notification: two
writers can overwrite each other.

The status bar shows one line ("calu sync: in sync", "3 files uploading", "conflict in x.py",
"offline: 2 changes queued"); the title action "Sync Now" compares both sides again. The last
synchronised contents (the merge bases), the bases' hashes and the queue are in the IDE's
system directory (`calu-sync/<id>/`), not in the project.

Measured against the SSH box (`uitest/README.md`), a project of 307 files: first clone 1.3 s
in the IDE (2.5 s in the test with its checks); an edit of the agent to the local editor 0.3
to 0.8 s; a remote change by another program about 2.3 s (the bridge polls every 2 s); a
local save to the server 0.2 s; a full comparison of an unchanged tree 30 to 340 ms; the
bridge's polling watcher 0.6 % of one core with its `git` calls.

Not built: the commit comparison of the spec's section 7 (the copy made by "Clone from
Server" has no `.git`, so git operations happen on the server); `ide.ping`; marks on editor
tabs and in the project view; a setting for the per-file limit; file modes on Windows. A
change made to the local copy outside the IDE is uploaded when the IDE notices it (its file
watcher; up to its next refresh). A file above the limit that appears on the server is listed
only after the next full comparison.

## Modules

    shared/    no user interface, almost no platform code; unit-tested with JUnit 5
      json/      defensive readers for the bridge's JSON
      rpc/       JSON-RPC over NDJSON
      protocol/  error codes of the bridge
      state/     the chat's view state and its reducer (ported from viewState.ts)
      logic/     what the chat shows and offers, as pure functions (ports of the VSCode extension's
                 shared modules): one-password decisions, server command, session list and menu, hints
                 and setup cards, tabs, held steps, usage, slash commands, item menus, images, comments,
                 the settings model, installing calu, Markdown, bidi neutralising
      bridge/    the launchers (local, WSL, SSH, scripted) and the supervisor of `calu ide`
      chat/      the chat controller (state + actions), the diffs, the transcript's row model
      sync/      the file synchronisation of mode C: decisions, wire shapes, the remote store over the
                 bridge, the local store over a directory, the synchroniser and its driver (no IDE class)
      remote/    the RPC interface between frontend and backend
    backend/   next to the project: settings, the bridge, editors (diff, file, checker, context), installer,
               the synchronisation's IDE side (documents, VFS listener, merge dialog)
    frontend/  the tool window (native Swing): UiKit (cards, chips, wrapping layout), Transcript,
               EscalationCard, Composer, ChatSurface, SettingsDialog, CommentAction, TerminalOpener,
               notifications, status bar widget, settings page
    src/       plugin.xml (wiring only) and the tests of the whole plugin in a headless IDE

In a local IDE all three load in one process. In remote development `backend` runs on the remote
host and `frontend` in the thin client; `shared` is in both.

## Rules this plugin follows

- **Python first.** Logic that another front end needs is in calu (`calu ide`). What is in Kotlin
  is the port of the VSCode extension's pure modules, kept in `shared` without IDE classes.
- **RPC** (`shared/remote/CaluRemoteApi.kt`): only `suspend` methods; never called from the EDT;
  long-lived flows are collected inside `durable {}`. Arguments and results are small
  `@Serializable` classes of `shared` made of strings, numbers and booleans; JSON travels as text.
- **Serializer class loaders.** No module bundles kotlinx.serialization or kotlinx.coroutines:
  all three compile against them (`compileOnly`) and use the platform's copy at run time. A module
  that bundled its own copy and asked a `shared` class for its serializer would fail with a
  `LinkageError` in the IDE only, which Gradle tests cannot show.
- **Native Swing.** The transcript is drawn with JetBrains components, because an embedded
  browser is not reliable in remote development. `ChatSurface` is the seam for a browser surface
  in local mode later. Components are created once and changed in place; all Swing work is on the EDT.
- **Passwords.** At most one password per IDE start: asked by a dialog, sent with `ide.unlock`,
  kept in memory in the backend (`PasswordMemory`), and sent again by itself when a bridge is
  restarted. Never stored, never logged; an empty password is sent like any other.
- **Contracts.** Every class, function and property has a KDoc comment with its contract.
- Module descriptors (`calu.<module>.xml`) hold the registrations; `plugin.xml` only lists the modules.

## Building

No system Java or Gradle is needed; the JDK lives outside the repository. The plugin supports
the latest stable PyCharm only (2026.3, build 263; requirement 35). That platform is compiled
for Java 25, so the build needs a JDK 25.

    # once: a JDK 25
    mkdir -p ~/.cache/calu-jdk && cd ~/.cache/calu-jdk
    curl -fsSL -o jdk25.tar.gz https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jdk/hotspot/normal/eclipse
    tar xzf jdk25.tar.gz && rm jdk25.tar.gz

    export JAVA_HOME=$HOME/.cache/calu-jdk/jdk-25.0.5.1+2     # the directory the tarball unpacked to
    cd extensions/jetbrains
    ./gradlew check          # unit tests of shared, and the plugin in a headless IDE
    ./gradlew buildPlugin    # build/distributions/calu-<version>.zip
    ./gradlew verifyPlugin   # the Plugin Verifier against PyCharm 2026.3
    ./gradlew verifyPlugin -PverifyIdeVersions=<version>    # a further build, e.g. an EAP
    ./gradlew buildPlugin -PlocalIde=/opt/pycharm           # against an installed PyCharm, no download
    ./gradlew :shared:test --tests '*RealBridgeSyncTest*' -Psshbox=$HOME/.cache/calu-uitest/ssh/config
                             # the sync against calu ide over real SSH (uitest/sshbox/run.sh first)
    ./gradlew --stop         # the Gradle and Kotlin daemons hold 2 to 4 GB; stop them when idle

Versions (they move together, see `settings.gradle.kts`): Gradle 9.4.0 (wrapper), IntelliJ
Platform Gradle Plugin 2.20.1, Kotlin 2.3.20, RPC compiler plugin 2.3.20-RC2-0.1, JVM
toolchain 25, compiled against PyCharm 2026.3 (`gradle.properties`), `since-build` 263. The
first build downloads PyCharm (about 1.2 GB) into `.intellijPlatform/`. The tests start
`python3` (the scripted bridge `shared/src/test/resources/fake_bridge.py`) and `sh`; the
synchronisation's test against a real bridge starts `uv run calu ide` from this checkout (it
is skipped where `uv` or `git` is missing). The Gradle build cache is off (`gradle.properties`):
it restored test classes compiled against an older `shared`.

Plugin Verifier result (2026-11-19): compatible with PyCharm 2026.3 (PY-263.7307.206), 8
usages of deprecated API (default methods of `ToolWindowFactory` and `StatusBarWidget`,
`FileEditorManager.openFile`), 23 of experimental API, none of internal API (the progress
indicator of the synchronisation has no percentage for that reason) (the RPC and `ProjectId` API that split
mode needs, and the Reworked Terminal API). No EAP of the next version (2027.1) was published
when this was written (JetBrains' release list held 2026.3 as the newest build of every
channel), so the verifier has not run against one.

What changed with the move from 2026.1 to 2026.3: `RemoteApiProviderService` is in a module
jar of its own (`bundledModule("intellij.platform.rpc")` in `shared`), the platform's inline
functions need JVM target 25, and the terminal is opened with
`TerminalToolWindowTabsManager` (the Reworked Terminal API) from the frontend.

## Installing

Settings | Plugins | gear | Install Plugin from Disk, choose `build/distributions/calu-<version>.zip`,
restart. Or unpack the zip into the IDE's plugins directory (Linux:
`~/.local/share/JetBrains/PyCharm2026.3/`) while the IDE is closed. In remote development the
plugin must be installed on the host and in the client. The IDE shows "Cannot update plugin:
unable to identify where calu was installed from" once per start for a plugin installed this
way; it has no effect.

The tool window "calu" is in the right stripe (below "More tool windows" until it is used once).

## What the tool window offers

    header     the session (click: switch, new, resume, fork, rename, detach; type to filter),
               what Claude does and since when, the honesty chip (mode, marks, review, trust)
    cards      what is missing, each with its button: install calu, unlock (one password), the
               server command for tmux or screen with "Open terminal with this command",
               the running sessions, a session request that waits (with Cancel)
    tabs       Main and one chip per subagent: running, finished, failed, "⚠" for a decision that
               waits there, "held" for a step calu holds back; beyond six tabs finished ones fold
    transcript Markdown (tables, code that wraps), tool rows with the checker's decision and, under a
               checked edit, the honesty checker's status per file; a click on an edit opens the
               diff, on a file name (also in Claude's text) the file at the line, on a checker's
               name its instructions, on an image the image at its size; right click: expand, copy, open; a user message's menu: rewind, fork, copy
    cards      Allow / Deny with a note / Why? / Show Diff; questions with one or several choices
    composer   Enter sends (queues while Claude works), Ctrl+Enter sends into the running turn,
               Shift+Enter a new line, "/" slash commands, paste or "Add image" (5 MiB, four)
    footer     model, effort, supervision level, context usage (click: limits and cost)
    title bar  New Session, Checker Instructions, Checker Context, Side Question, Models, calu Settings,
               Plugin Settings, Sync Now (SSH mode)
    elsewhere  editor popup "Add Comment for Claude" (Ctrl+Alt+K; also in a diff), status bar
               "calu: N waiting", notifications with Allow / Deny while the tool window is hidden

## Settings

Settings | Tools | calu holds only what belongs to the editor: how calu is reached, the calu
command, SSH host, remote directory and options, the sync mode, notifications, and the flags of
the server command. They are stored in `.idea/calu.xml` by the backend. calu's own settings are
in calu's database: the title action "calu Settings" opens a dialog generated from
`ide.settings.get` (sections, search, a control per kind, a marker for what differs from the
default, reset, global or per project), with the pages "Models" (presets, one model for
everything, one field per role), "Checkers" (Edit Instructions) and "Setup" (what `ide.status`
found, with the buttons of the chat's setup cards).

## State

"seen" = walked through in PyCharm 2026.3 in the UI-test desktop (`uitest/README.md`,
"Running the PyCharm plugin in the desktop") against the scripted server; the cases are in
`agent/records/notes/test-plan.md` section B.

| Part | State |
|---|---|
| JSON-RPC client, reducer, the pure logic of `shared/logic` | built, unit-tested (ports of the VSCode extension's modules and tests) |
| Launchers local, WSL, SSH; supervisor; reconnect | built, tested against a scripted bridge process; local and SSH seen (a killed bridge or a cut network is followed by a new start and the transcript continues); WSL only through a stand-in `wsl.exe` script |
| Transcript, cards, tabs, composer, session menu, selectors, usage, honesty chip, setup cards | seen; transcript and card also tested in a headless IDE |
| Diff viewer, file at line, checker instructions with the conflict path, context documents | seen, also over SSH (local paths) |
| One password per IDE start (blank, wrong, right; kept over a bridge restart) | seen, also over SSH |
| Notifications (Allow on a notification clicked), status bar widget, plugin settings page, calu settings dialog with the Setup page | seen |
| Open terminal with the server command (Reworked Terminal API) | seen |
| Slash commands, images (paste, enlarge built), rewind and fork from a message, Add Comment for Claude (editor and diff) | seen |
| Side-question dialog, code blocks that wrap, a decision made in another client, a background subagent | seen |
| File links in Claude's text, the session list for several matching sessions (-32012), the honesty checker's status per file | built and unit-tested; not seen (the scripted server produces none of the three) |
| Installing uv and calu | the offer is seen ("Install calu" on the card); the installation itself was not run |
| File sync for mode C | built; unit-tested, tested against `calu ide` as a local process and over SSH, and seen in the IDE (section H of the test plan) |
| A message to a subagent | built; not confirmed in the IDE this round |
| Remote development | seen with the backend on the SSH box and the JetBrains Client on the desktop: tool window, sessions, a turn with Allow and Deny with a note, diffs and file links opened by the backend, a comment in a diff, the terminal with the server command; with `runIdeSplitMode` also the one-password dialog, the settings dialog with a refused value and Reset, the checker editor |
| SSH interpreter next to the SSH mode | seen: run and debug on the server with a breakpoint, line numbers match after a server edit; the "two writers" warning for PyCharm's automatic upload; "Sync: none" with PyCharm's Deployment uploading |
| The activity verbs' spinner, a browser surface | not built in the UI |

Known gaps: thinking text is shown only when its row is expanded; with ten subagents the tab
chips can take most of a narrow tool window's height; a step that waits in a session whose
connection is lost still counts in "calu: N waiting".

## Licence notes

The plugin is part of calu (GPL-2.0-only, `../../LICENSE`). The Gradle layout
(`settings.gradle.kts`, `build.gradle.kts`, the module build files and the shape of the module
descriptors and of the RPC wiring) follows JetBrains' intellij-platform-modular-plugin-template,
which is published under the Apache License 2.0 as a template for copying; see `NOTICE`. No code
of another plugin was copied. The icons are calu's own drawing (`../vscode/media`).
