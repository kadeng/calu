# calu for PyCharm and other JetBrains IDEs

A tool window "calu" (on the right) that shows a Claude Code session which calu runs and checks:
the transcript with the checkers' decisions, cards to allow or deny a waiting step, the diff of
every edit, and calu's settings. It offers what the VSCode extension offers
(`../vscode`, `docs/notes/vscode-extension-spec.md`) and reaches calu in three ways.

State: second round. The plugin runs in PyCharm 2026.3 on Linux in a local IDE and was walked
through there against a scripted calu server ("State" at the end, `docs/notes/test-plan.md`
section B). Remote development (split mode) and the SSH mode have not run yet.

## How calu is reached

    A  local IDE ─────────────────────────── calu ide            (child process in the project directory)
       Windows IDE, project in WSL ───────── wsl.exe -d <distro> --cd <dir> -- calu ide
    B  JetBrains remote development ──────── calu ide            (started by the backend on the remote host)
    C  local IDE + local copy of the project ─ ssh -T -o BatchMode=yes ... <host> 'calu ide --cwd <dir>'

In every mode the plugin speaks the protocol of `calu ide` (JSON-RPC over newline-delimited
JSON) and nothing else: it opens no socket and no database. It assumes a running `calu serve`
and never calls sudo; when no server answers it shows the tmux or screen command and can open a
terminal with it.

Mode C needs the system's OpenSSH client and a key in the SSH agent: the plugin never types a
passphrase (`BatchMode`), and says so instead of hanging. It reconnects with a growing wait
(0.5 s to 30 s) and attaches again with `after_seq`, so the transcript continues. The bridge
side of mode C is built (`calu ide --cwd`, `ide.fs.*`, `docs/notes/jetbrains-sync-spec.md` with
its section 13 "Deviations"); the plugin has the requests and answers as pure functions
(`shared/sync/Wire.kt`) and the decisions (`SyncEngine`), but **no code that reads or writes the
local copy yet**. Until then choose "Sync: none" and synchronise the copy yourself (the IDE's
Deployment, rsync, Mutagen).

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
      sync/      decisions and wire shapes of the file synchronisation for mode C (no I/O yet)
      remote/    the RPC interface between frontend and backend
    backend/   next to the project: settings, the bridge, editors (diff, file, checker, context), installer
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
    ./gradlew --stop         # the Gradle and Kotlin daemons hold 2 to 4 GB; stop them when idle

Versions (they move together, see `settings.gradle.kts`): Gradle 9.4.0 (wrapper), IntelliJ
Platform Gradle Plugin 2.20.1, Kotlin 2.3.20, RPC compiler plugin 2.3.20-RC2-0.1, JVM
toolchain 25, compiled against PyCharm 2026.3 (`gradle.properties`), `since-build` 263. The
first build downloads PyCharm (about 1.2 GB) into `.intellijPlatform/`. The tests start
`python3` (the scripted bridge `shared/src/test/resources/fake_bridge.py`) and `sh`.

Plugin Verifier result (2026-11-19): compatible with PyCharm 2026.3 (PY-263.7307.206), 8
usages of deprecated API (default methods of `ToolWindowFactory` and `StatusBarWidget`,
`FileEditorManager.openFile`), 23 of experimental API (the RPC and `ProjectId` API that split
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
    transcript Markdown (tables, code), tool rows with the checker's decision, a click on an edit
               opens the diff, on a file name the file at the line, on a checker's name its
               instructions; right click: expand, copy, open; a user message's menu: rewind, fork, copy
    cards      Allow / Deny with a note / Why? / Show Diff; questions with one or several choices
    composer   Enter sends (queues while Claude works), Ctrl+Enter sends into the running turn,
               Shift+Enter a new line, "/" slash commands, paste or "Add image" (5 MiB, four)
    footer     model, effort, supervision level, context usage (click: limits and cost)
    title bar  New Session, Checker Instructions, Checker Context, Models, calu Settings, Plugin Settings
    elsewhere  editor popup "Add Comment for Claude" (Ctrl+Alt+K; also in a diff), status bar
               "calu: N waiting", notifications with Allow / Deny while the tool window is hidden

## Settings

Settings | Tools | calu holds only what belongs to the editor: how calu is reached, the calu
command, SSH host, remote directory and options, the sync mode, notifications, and the flags of
the server command. They are stored in `.idea/calu.xml` by the backend. calu's own settings are
in calu's database: the title action "calu Settings" opens a dialog generated from
`ide.settings.get` (sections, search, a control per kind, a marker for what differs from the
default, reset, global or per project), with the pages "Models" (presets, one model for
everything, one field per role) and "Checkers" (Edit Instructions).

## State

"seen" = walked through in PyCharm 2026.3 in the UI-test desktop (`uitest/README.md`,
"Running the PyCharm plugin in the desktop") against the scripted server; the cases are in
`docs/notes/test-plan.md` section B.

| Part | State |
|---|---|
| JSON-RPC client, reducer, the pure logic of `shared/logic` | built, unit-tested (ports of the VSCode extension's modules and tests) |
| Launchers local, WSL, SSH; supervisor; reconnect | built, tested against a scripted bridge process; local seen (a killed bridge is restarted and attaches again); SSH and WSL only through stand-in `ssh` and `wsl.exe` scripts |
| Transcript, cards, tabs, composer, session menu, selectors, usage, honesty chip, setup cards | seen; transcript and card also tested in a headless IDE |
| Diff viewer, file at line, checker instructions with the conflict path, context documents | seen |
| One password per IDE start (blank, wrong, right; kept over a bridge restart) | seen |
| Notifications, status bar widget, plugin settings page, calu settings dialog | seen |
| Open terminal with the server command (Reworked Terminal API) | seen |
| Slash commands, images (paste), rewind and fork from a message, Add Comment for Claude | seen (comment in a normal editor; in a diff built, not seen) |
| Installing uv and calu | built from the tested decisions of `logic/Bootstrap.kt`; not run (the desktop has both) |
| Remote development (split mode) | built for it; not run: see `docs/notes/test-plan.md` section B |
| File sync for mode C | decisions, wire shapes and spec only; no I/O |
| Side questions (`side.ask`), the activity verbs' spinner, a browser surface | not built in the UI |

Known gaps against the VSCode extension: no side-question panel; a link in Claude's text to a
file of the project is not clickable (`lookupFile` exists in `shared`); thinking text is shown
only when its row is expanded; an image in the transcript cannot be enlarged; the settings
dialog has no "setup" page (`calu doctor` findings).

## Licence notes

The plugin is part of calu (GPL-2.0-only, `../../LICENSE`). The Gradle layout
(`settings.gradle.kts`, `build.gradle.kts`, the module build files and the shape of the module
descriptors and of the RPC wiring) follows JetBrains' intellij-platform-modular-plugin-template,
which is published under the Apache License 2.0 as a template for copying; see `NOTICE`. No code
of another plugin was copied. The icons are calu's own drawing (`../vscode/media`).
