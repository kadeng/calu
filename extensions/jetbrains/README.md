# calu for PyCharm and other JetBrains IDEs

A tool window "calu" (on the right) that shows a Claude Code session which calu runs and checks:
the transcript with the checkers' decisions, cards to allow or deny a waiting step, the diff of
every edit, and calu's settings. It offers what the VSCode extension offers
(`../vscode`, `docs/notes/vscode-extension-spec.md`) and reaches calu in three ways.

State: first round. Everything below is built and tested without a running IDE window; the
plugin has **not run in an IDE yet** (see "State" at the end).

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
(0.5 s to 30 s) and attaches again with `after_seq`, so the transcript continues. Mode C needs
`calu ide --cwd` and, for file synchronisation, the bridge additions of
`docs/notes/jetbrains-sync-spec.md`; **neither is built on the Python side yet**. Until then
choose "Sync: none" and synchronise the copy yourself (the IDE's Deployment, rsync, Mutagen).

## Modules

    shared/    no user interface, almost no platform code; unit-tested with JUnit 5
      json/      defensive readers for the bridge's JSON
      rpc/       JSON-RPC over NDJSON
      protocol/  error codes of the bridge
      state/     the chat's view state and its reducer (ported from viewState.ts)
      logic/     one-password decisions, server command, session list, hints, paths, Markdown
      bridge/    the launchers (local, WSL, SSH, scripted) and the supervisor of `calu ide`
      chat/      the chat controller (state + actions), the diffs, the transcript's row model
      sync/      decisions of the file synchronisation for mode C (no I/O yet)
      remote/    the RPC interface between frontend and backend
    backend/   next to the project: settings, the bridge, editors (diff, file, checker, context), terminal
    frontend/  the tool window (native Swing), notifications, status bar widget, settings page, dialogs
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

No system Java or Gradle is needed; the JDK lives outside the repository.

    # once: a JDK 21 (the platform 2025.3 - 2026.1 needs Java 21; 2026.2 and later run on 25,
    # and load a plugin compiled for 21)
    mkdir -p ~/.cache/calu-jdk && cd ~/.cache/calu-jdk
    curl -fsSL -o jdk21.tar.gz https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse
    tar xzf jdk21.tar.gz && rm jdk21.tar.gz

    export JAVA_HOME=$HOME/.cache/calu-jdk/jdk-21.0.13+9      # the directory the tarball unpacked to
    cd extensions/jetbrains
    ./gradlew check          # unit tests of shared, and the plugin in a headless IDE
    ./gradlew buildPlugin    # build/distributions/calu-<version>.zip
    ./gradlew verifyPlugin   # the Plugin Verifier against the PyCharm builds below
    ./gradlew verifyPlugin -PverifyIdeVersions=2026.2,2026.3    # further builds

Versions (they move together, see `settings.gradle.kts`): Gradle 9.4.0 (wrapper), IntelliJ
Platform Gradle Plugin 2.20.1, Kotlin 2.3.20, RPC compiler plugin 2.3.20-RC2-0.1, compiled
against PyCharm 2026.1 (`gradle.properties`), `since-build` 261. The first build downloads
PyCharm (about 1 GB) into `~/.gradle`. The tests start `python3` (the scripted bridge
`shared/src/test/resources/fake_bridge.py`) and `sh`.

Why not `since-build` 253: the RPC compiler plugin is built per platform line (2.1.20-0.1 for
2025.3, 2.3.20-RC2-0.1 for 2026.1); one binary for both lines is not verified.

Installing the zip by hand: Settings | Plugins | gear | Install Plugin from Disk. In remote
development the plugin must be installed on the host and in the client.

## Settings

Settings | Tools | calu holds only what belongs to the editor: how calu is reached, the calu
command, SSH host, remote directory and options, the sync mode, notifications, and the flags of
the server command. They are stored in `.idea/calu.xml` by the backend. calu's own settings are
in calu's database; the tool window's title actions open "Models: use one model for
everything" and the list of all settings.

## State

| Part | State |
|---|---|
| JSON-RPC client, reducer, one-password logic, server command, session list, hints | built, unit-tested (ports of the VSCode extension's modules and tests) |
| Launchers local, WSL, SSH; supervisor; reconnect | built, tested against a scripted bridge process (SSH and WSL through stand-in `ssh` and `wsl.exe` scripts; no real SSH server, no Windows) |
| Chat controller | built, tested against the scripted bridge |
| Transcript, escalation cards, composer, session menu, tabs, selectors, usage, honesty chip, no-server state | built; transcript and card tested in a headless IDE; not seen in a window |
| Diff viewer, open file at line, checker instructions, context documents | built; diff request and checker save tested headless |
| Notifications, status bar widget, settings page, models dialog, settings list | built, not tested |
| Open terminal with the server command | built with the terminal plugin's `createShellWidget` (deprecated API), not tested |
| Remote development (split mode) | built for it; never run in split mode |
| File sync for mode C | decision logic and spec only; no I/O, no Python side |
| Images in messages, slash commands, rewind/fork from a message, comments on selections, install of calu | not started |

## Licence notes

The plugin is part of calu (GPL-2.0-only, `../../LICENSE`). The Gradle layout
(`settings.gradle.kts`, `build.gradle.kts`, the module build files and the shape of the module
descriptors and of the RPC wiring) follows JetBrains' intellij-platform-modular-plugin-template,
which is published under the Apache License 2.0 as a template for copying; see `NOTICE`. No code
of another plugin was copied. The icons are calu's own drawing (`../vscode/media`).
