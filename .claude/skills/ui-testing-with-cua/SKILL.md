---
name: ui-testing-with-cua
description: How to run calu's UI tests in the Cua desktop container - the VSCode extension and the PyCharm plugin driven by screenshot, click and keyboard against a scripted calu server (or, with the owner's say, a real Claude Code). Use when asked to test, look at or fix anything in extensions/vscode or extensions/jetbrains in a real IDE, to reproduce a UI defect, to take screenshots of the extensions, or to work through agent/records/notes/test-plan.md.
---

# UI testing with the Cua desktop

calu's two IDE plugins are tested in a real IDE inside a Docker container, `calu-uitest`
(a [Cua](https://github.com/trycua/cua) Linux desktop: XFCE on a virtual display of 1280x800,
VSCode, PyCharm, uv, its own PostgreSQL). An agent drives it with a small helper and looks at
screenshots. `uitest/README.md` is the reference (ports, versions, mounts, every option); this
file is the working method.

    host                                            container calu-uitest (user cua)
    uitest/desktop.py ── screenshot/click/type ──►  VSCode / PyCharm
    extensions/*  ── build ── ~/.cache/calu-uitest/share ◄─► /share
    the checkout ── read-only ──────────────────►  /workspace/calu ── setup-demo.sh ──► ~/work/calu,
                                                    calu, PostgreSQL, ~/work/demo, fake server (tmux)

## The loop

```bash
cd calu
uitest/run.sh                                         # start or reuse the container
D="uv run --no-project --with cua python uitest/desktop.py"
$D screenshot ~/.cache/calu-uitest/share/run/01.png   # then Read the PNG
$D click 640 400 ; $D type "text" ; $D key ctrl+shift+p ; $D shell 'cmd' ; $D windows
```

1. **Look before every step.** Take a screenshot, read it, take the coordinates of what you
   click from it. Never click from memory of an earlier screenshot: layouts shift.
2. **One driver at a time.** The desktop has one screen, mouse and keyboard. Two agents driving
   it at once corrupt each other's input. `$D shell` lines that do not touch the screen are safe
   next to a driver, but they share the container's 8 GB.
3. **Keep the evidence.** Number the screenshots of a run in one folder under
   `~/.cache/calu-uitest/share/`; copy the few worth showing to `agent/records/notes/screenshots/`.
4. **Record the result** in `agent/records/notes/test-plan.md`: each case `ok`, `fixed (commit)`,
   `open (why)` or `n/a here`. Say what was exercised and what was only rendered.

A person can watch: `$D viewer-url` prints a browser link (localhost only).

## The scripted backend (the default)

No Claude Code, no key, no sign-in. `uitest/fake_server.py` runs the real `calu.serve.Server`
with a scripted runner and rule-based fake checkers, so the whole session machinery, the
database and the bridge are real and only Claude and the models are played.

```bash
$D shell 'bash /workspace/calu/uitest/setup-demo.sh'    # idempotent: database, calu, ~/work/demo,
                                                        # fake server in tmux "calu-fake", extension
```

- Run it again after every change of the Python side (it copies the host's working tree,
  uncommitted changes included). `--no-vscode` leaves the IDE alone; `--stop-server` gives the
  "no server" state.
- The first message of a session picks the script: `scenario: tour` (the default: text, an
  edit, pass/allow/deny/escalate, subagents, a question), `subagents`, `swarm` (ten at once,
  two escalate), `stop`, `queue`, `levels`, `edge` (huge output, raw HTML, script links),
  `many` (500+ items), `escalations` (three at once), `bidi`, `tall`, `nousage`, `honesty` (marks; needs the honesty checker enabled). Add a scenario
  to `fake_server.py` when a case needs one; do not fake behaviour inside a plugin.
- The fake checkers decide by markers in a scripted command: `# calu:allow`, `# calu:deny`,
  `# calu:escalate`.
- What a script cannot show is listed in `uitest/README.md` ("What a real session does that the
  scripted runner does not"): Claude Code's own slash commands, real cost, a subagent acting on
  a relayed message.

## VSCode extension

```bash
(cd extensions/vscode && export PATH=$(ls -d $HOME/.nvm/versions/node/v24*/bin | tail -1):$PATH && \
   npm run check-types && npm run test:unit && npm run build && npm run package)
cp extensions/vscode/calu.vsix ~/.cache/calu-uitest/share/
$D shell 'uitest-code --install-extension /share/calu.vsix --force'
```

Then "Developer: Reload Window" from the command palette. Always `uitest-code`, never plain
`code` (it carries the profile and suppresses a WSL prompt). A first-start layout needs a new
profile: `UITEST_VSCODE_DATA=$HOME/.vscode-fresh` before the `uitest-code` calls. The view sits
in the secondary side bar (right). Trusting `~/work/demo` in this container is allowed.

Useful: `$D shell 'pkill -f "calu ide"'` kills the bridge (the chat must offer "Restart
bridge"); a second window on the same folder tests two clients on one session.

## PyCharm plugin

```bash
export JAVA_HOME=$HOME/.cache/calu-jdk/<the JDK named in extensions/jetbrains/README.md>
(cd extensions/jetbrains && ./gradlew check buildPlugin verifyPlugin)
```

Install the zip from `build/distributions/` into the PyCharm of the container (the plugin's
README says how), restart the IDE, open a **writable** project (`~/work/demo`; PyCharm writes
`.idea/`, and `/workspace/calu` is read-only). Stop Gradle daemons when idle
(`./gradlew --stop`): the host is short of memory when several agents run.

## What an agent must not do there

- **No sign-ins and no agreements.** Dismiss "Sign in", Copilot, marketing and tips. A licence
  or agreement dialog is the owner's to answer: screenshot it, say what it asks, stop that step.
  (He accepted PyCharm's user agreement himself; a 30-day trial runs.)
- **No credentials into the container**, with one exception the owner granted on 2026-11-18:
  his budget-limited Anthropic key for a real Claude Code, set up by `uitest/setup-real.sh`
  from a mode-600 file in the share. **Haiku only, for everything**; short prompts; never print,
  screenshot, log or commit the key, never put it (or a part of it) on a command line; check
  for leaks with scripts that print counts only. Run the real backend only when the task says
  so: every step costs money. `setup-real.sh --remove-key` deletes the two files that hold it.
- **Never touch** the containers `calu-db-postgres-1` (the owner's real database) and
  `calu-dev-caludb-1` (the test suite's server), nor `~/.config/calu`, `/etc/calu`,
  `/var/lib/calu` on the host. No `sudo` on the host; inside the container it is fine.
- **Do not publish ports** beyond 127.0.0.1, and do not start the container privileged.

## Fixing what a run shows

Fix in priority order: what blocks the main flow, wrong wiring, what hurts use. Keep logic in
the pure modules with unit tests (`extensions/vscode/src/shared`, `extensions/jetbrains/shared`)
and the UI thin; rebuild, reinstall, reload and **look again** before calling it fixed. A change
of the session protocol needs `npm run gen:protocol` in `extensions/vscode` and the same change
in the Kotlin types. UX suggestions go into the report, not silently into the code.

Things that went wrong in earlier runs, so check them on every round:

- Keyboard focus must never land on Allow by itself, and Enter in a note field must never
  decide (both happened, in two front ends).
- A click target that fills its row hides the row's own action (the diff behind the file name).
- A pattern passed to `pkill`/`grep` in the container can match the IDE itself.
- Text from Claude or a tool can carry control characters and markup: it must stay text.

## Other test environments

`agent/records/notes/test-plan.md` lists them: the root box (`uitest/rootbox/`, a container with root,
sudo and several users, for `calu serve`/`setup` in every privilege arrangement), a private
tmux server for the terminal clients, headless Chromium for the web interface, and a headless
SSH server container for the PyCharm plugin's SSH mode. Each builds its own PostgreSQL
container; none uses the test suite's server, because its roles are server-wide.
