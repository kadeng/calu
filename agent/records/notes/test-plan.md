# Test plan: every front end, gone through by hand and by script (requirement 37)

Started 2026-11-18. The owner: "test it all … Develop test plans and go through them, ensuring
everything works as intended and has a good usability, exploring even visual/layout corner
cases like many subagents, long chat sessions, many chat sessions, undo, forking etc. and maybe
also what happens if an escalation in a subagent halts one but not other subagents, and another
escalation pops up."

This file is the record: each section lists its cases with a status, kept up to date by whoever
runs them. Automated regression tests stay in `tests/` and `extensions/*/test`; this plan is for
what a person would do with the running application, done here by agents that drive the real
programs and look at the result.

    Status:  ok        ran, behaves as intended
             fixed     was wrong, corrected (commit), ran again
             open      wrong or doubtful, not corrected (say why)
             n/a here  cannot be run in this environment (say what would be needed)
             -         not run yet

## Environments

| Name | What | Used for |
|---|---|---|
| desktop | Docker container `calu-uitest` (Cua): XFCE, VSCode, PyCharm, uv, own PostgreSQL; driven with `uitest/desktop.py`; scripted server `uitest/fake_server.py` with scenarios; a real Claude Code on Haiku with the owner's limited API key | the VSCode extension, the PyCharm plugin |
| terminal | a private tmux server on the host (`tmux -L calutest`), screenshots of panes as text | the full-screen client, the line client |
| browser | headless Chromium (the browser layer's Playwright), screenshots | the web interface |
| root box | a container of its own with a root user, an unprivileged user and sudo | `calu serve`/`run`/`attach`/`web` as root with `--user`, through sudo, with and without passwords |
| server | `uitest/sshbox/`: the container `calu-sshbox` (Ubuntu 24.04, `sshd` with public-key login only, user `dev`, calu, PostgreSQL, a scripted `calu serve` in tmux, the project `/home/dev/project`), on the Docker network `calu-uitest-net` with the desktop and on `127.0.0.1:56522` for the host; a key pair made for the tests in `~/.cache/calu-uitest/ssh/` | the PyCharm plugin's SSH mode (section H): `ssh sshbox calu ide --cwd DIR`, file sync through the bridge, reconnects, the one-password rule over SSH. Not done there: the VSCode extension over SSH |
| second server | a second SSH box `calu-sshbox2` (`CALU_SSHBOX_NAME=calu-sshbox2 CALU_SSHBOX_PORT=56523 CALU_SSHBOX_NETEM=1 uitest/sshbox/run.sh`): the same image with the capability NET_ADMIN for `tc qdisc ... netem`, its own host entry `~/.cache/calu-uitest/ssh/config-calu-sshbox2`, `uitest/tui_server.py` as its scripted server | the terminal clients and the web interface remote (section I) |

No test touches the owner's database (`calu-db-postgres-1`), his `~/.config/calu`, `/etc/calu`
or `/var/lib/calu`. A database for manual tests is a container of its own, not the test
suite's server (its roles are server-wide, and a manual setup there breaks running tests).

## The cases every chat front end must pass

(VSCode extension = V, PyCharm plugin = P, full-screen client = T, web live view = W. A front
end that lacks a function by design says so in its own section.)

| # | Case | V | P | T | W |
|---|---|---|---|---|---|
| C1 | A turn with streamed text, thinking, a read, an edit, a command; the checker's decision and reason on each | - | fixed 30554a1 | fixed b1f47c4 | fixed 706b3b8 |
| C2 | A denied step with its reason; an escalated step: allow, deny with a note, "why?" | - | fixed 30554a1 5dfb6b6 | fixed b1f47c4 | fixed 706b3b8 |
| C3 | A question of the agent: one choice, several choices, free text | - | ok | fixed b1f47c4 fc2d361 | fixed 706b3b8 |
| C4 | A subagent: its own view, task and result; a background subagent that outlives the turn | - | ok (the background subagent of `tour` seen 2026-11-19) | fixed b1f47c4 | fixed 706b3b8 |
| C5 | **Many subagents at once** (8+): navigation, overflow, which one needs attention | - | fixed 30554a1 c91feae | fixed b1f47c4 | fixed 706b3b8 |
| C6 | **An escalation in one subagent while others keep working, then a second escalation elsewhere**: both visible, each decided on its own, the others not held longer than calu holds them | - | fixed 30554a1 c91feae | fixed b1f47c4; open (gate) | fixed 706b3b8 |
| C7 | Three or more open decisions at once; deciding out of order; a decision made in another client closes it here | - | ok (decided with `calu resolve` on the server: the card closes, the turn goes on) | fixed b1f47c4 | fixed 706b3b8 |
| C8 | Stop during text, during a command, with a decision open | - | ok (during a command; the other two not run) | ok | fixed 706b3b8 |
| C9 | A message for the next turn and one into the running turn; two queued; while a decision is open | - | ok | ok | ok |
| C10 | Supervision level to "everything waits" and back; changed from elsewhere | - | ok (changed from elsewhere: not run) | ok | fixed 706b3b8 |
| C11 | **A long session** (500+ items, 50+ turns): speed, scrolling, jump to latest, memory | - | fixed 30554a1 | ok | fixed 706b3b8 |
| C12 | **Many sessions** (10+ running, 30+ stored): the list, switching, names, finding one | - | ok (10 running: the server's limit) | fixed b1f47c4 026bc78 | fixed 706b3b8 |
| C13 | **Undo** (rewind to before a message) and **fork**: first, middle, last message; fork of a fork; what the new session shows and knows | - | ok; open (fork of a rewind) | fixed 602b9be | ok (fork); undo: by design a fork |
| C14 | Context usage and limits; before the first turn; when unknown | - | ok | fixed b1f47c4 | fixed 706b3b8 |
| C15 | Images in a message (where supported): accepted, refused over the limit, shown | - | fixed c91feae (paste); limits by unit test | n/a here | ok |
| C16 | Losing the session, the server, the database mid-turn; coming back | - | fixed 5dfb6b6; database loss not run | fixed b1f47c4 | fixed 706b3b8 |
| C17 | Narrow and very wide layouts; light and dark; long lines, huge results, long names | - | ok; open (long code line) | fixed 602b9be | fixed 706b3b8 |
| C18 | Text meant to mislead: control characters, raw HTML, links with scripts | - | ok | fixed 900355e b1f47c4 | fixed 706b3b8 |
| C19 | Checker instructions: read, change, save, a conflict with another editor | - | ok | fixed b1f47c4 602b9be | fixed 706b3b8 |
| C20 | The checker context, and the context of one escalation | - | ok | ok | ok |
| C21 | Honesty mode: switch, marks, review, trust | - | ok (mode switch); marks, review, trust not run | ok | ok |
| C22 | Settings: every kind of value, a refused value, one model for all roles, reset | - | fixed c91feae | n/a here | fixed 706b3b8 |
| C23 | First start: nothing installed, no server, no session, a locked credential (one password, once) | - | ok (no server, no session, locked credential); install not run | ok; open (cli) | ok (no server, no session); rest n/a here |

## Sections (each owner extends its own)

### A. VSCode extension
First and second run: see the reports summarised in `agent/records/HANDOFF.md`; screenshots in
`agent/records/notes/screenshots/run1-*.png`. To be transferred into the table above by the next
round, which also runs C5, C6, C11 and C12 at scale.

### B. PyCharm plugin

Run 2026-11-18/19 by an agent in PyCharm 2026.3 (build 263.7307.206, Islands Dark, 1280x800)
in the desktop container, driven by screenshot, click and keyboard, against
`uitest/fake_server.py` in local mode (the plugin starts `calu ide` in `~/work/demo`). One short
pass ran against a real Claude Code on Haiku (one prompt and `/cost`: an edit with its diff,
thinking, cost and context usage arrived as with the scripted server). Screenshots of every
state: `~/.cache/calu-uitest/share/pycharm1/` (not in the repository). How to repeat it:
`uitest/README.md`, "Running the PyCharm plugin in the desktop". Fixes are in commit 30554a1
unless another is named.

What the first start showed (the plugin had never run in a window): it loaded and attached at
once, and the flow new session → transcript → Allow worked. Wrong were: "New Session" asked
for a first message in a dialog; a tool row showed the checker's summary instead of the
command; chips, cards and rows looked like a form (plain buttons, flow layouts that cut long
lines, an empty tab bar); a finished escalation and a user message were hard to tell apart;
Markdown tables were shown as pipes; the terminal used a deprecated API.

Added in the third round (0b1cab1, 56872ba): the side-question dialog (seen), the Setup page of
the settings dialog (seen), file links in Claude's text, enlarging an image, the session list
when several sessions match, the honesty checker's status per file on a checked edit (these
four built and unit-tested, not seen: the scripted server gives no occasion), the install
offer on the missing-calu card (seen; the Kotlin hint offered only "Open settings"), and the
file synchronisation of the SSH mode (section H).

| # | Case | Status |
|---|---|---|
| C1 | `tour`: streamed Markdown (table, code), thinking, Read, Edit, commands, verdict chips, the checker's remark under a denied step | fixed: rows name the command, chips are badges, tables are tables; a click on the edit row opens the diff at the changed line, on the file name the file at that line |
| C2 | denied command; escalation: Allow, Deny with a note, Why? | fixed: the card has a stripe, the step in a code box, the note field, buttons below; a decided card is one line with ALLOWED / DENIED and the note. An escalated edit showed only its file name: it now shows what is replaced and by what, with "Show Diff" (5dfb6b6). Why? opens "Escalation N.md" in an editor tab |
| C3 | one choice (radio buttons), several choices (check boxes), two questions in one card, free text | ok |
| C4 | the two subagents of `tour`: their tabs, task row, result | ok. A message to a subagent is refused with the reason in the composer ("runs in the foreground", "has finished"). The background subagent of `subagents` was not run |. Third round: the background subagent of `tour` has its tab with its command and result. A message to a running subagent (`swarm`) was not confirmed: the first tries pressed "Stop" instead (the keyboard focus sat on the button, a typed space interrupted the turn); fixed in 56872ba (the three buttons take no focus, a chosen tab gives it to the text field), the message itself still to be seen |
| C5 | `swarm`: ten subagents | fixed: the IDE's tab bar would scroll ten tabs out of sight; now chips that wrap, each with its state (running, finished, failed), "⚠" and bold for a decision that waits, "held" for a step calu holds back; beyond six tabs finished ones fold into "N finished" |
| C6 | `swarm`: an escalation in the third subagent, the others keep working, then a second and a third | fixed: the header counts what waits; one line says "N decisions wait in other tabs" with "Show next" and "M steps held until you decide"; held tool rows carry a HELD chip. As measured in section D: after the first escalation every other subagent stops at its next checked step, so nine chips read "held" within seconds |
| C7 | three open at once, decided in the order third, fifth, seventh; ten cards after the 300 s hold | ok: each card decides its own step; with ten open the strip stays one line (c91feae). A decision made in another client: ok (`calu resolve 5 allow` on the server closed the card and the turn went on) |
| C8 | Stop during a command (`queue`) | ok: "turn interrupted", the command row gets FAILED, the queued message is kept. During text and with a decision open: not run |
| C9 | `queue`: two queued, one into the running turn | ok: "Queue" and "Send Now" replace "Send" while Claude works; waiting messages are listed above the composer; the injected one appears in the turn |
| C10 | level to "Confirm edits": the next edit waits although the edit checker passed | ok. Changed from another client: not run |
| C11 | `many`: 522 rows in one turn | fixed: one row is built per event instead of all rows per event. 4,000 renders: mean 0.65 ms, longest 31.5 ms; the IDE process 2.0 GB resident with the session open. The view follows the end, stops following when the user scrolls, "Jump to latest" returns. An expanded result of 60,000 characters shows the first 20,000 and says so |
| C12 | ten running sessions (the server's limit of `--max-sessions`; twelve were refused) | ok: the menu lists them with state, directory and the first characters of the id (unnamed sessions of one directory share their title), a waiting one has a warning icon; typing filters; Rename gives a name |
| C13 | fork from the second message; rewind to before the last message; the first message | ok: "fork of …" / "rewind of …" start, show the inherited turns under "Earlier in this conversation", the rewound message's text is back in the composer; rewind and fork are disabled on the first message. The fork that showed one turn only (`/fork` of a rewound session): the fork was made while the rewound session's own turn still waited on an escalation, and `session.history` holds a turn only once it ended, neither for the session itself (there the replay shows it) nor for a fork of it. Fixed in the core (PLAN D237, 202b484): the history holds a turn that has not ended, and a fork inherits it. Found on the way and fixed (0b1cab1): the boundary between record and replay was found by the text of a message, so the same words in two turns dropped the turns between them; the prompt's id decides now |
| C14 | context chip: "ctx –" before the first turn, percent and tokens after it; the click lists limits and cost | ok. `nousage` not run |
| C15 | an image pasted from the clipboard | fixed (c91feae): the IDE's paste action never reached the key listener; the composer's paste now attaches the image, a thumbnail with a remove button appears. The limits (5 MiB, four, the types) are covered by `ImagesTest`, not by hand |
| C16 | `pkill -f "calu ide"` mid-turn; the server stopped mid-turn | ok: "calu ide exited with code 143" with Restart bridge and Show log; after the restart the session is attached again with its transcript. Server stopped: "Reconnecting…", then "connection is lost", the list of sessions empties, a new session request waits with "Start calu server". fixed (5dfb6b6): the terminal's shell had no `~/.local/bin` on PATH, so the command `calu serve` failed; the command now names calu's file. Database loss: not run |
| C17 | tool window 280 px and 520 px wide; Islands Dark and Islands Light; a line of 3,000 characters, a code block of 300 lines | ok. fixed (0b1cab1): a code block is lines that wrap, with their indentation kept (`edge` seen) |
| C18 | `bidi`, raw HTML and a `javascript:` link (`edge`) | ok: control characters are shown as `⟦RLO⟧` in text, file names, commands and cards; HTML is shown as text; only http, https and mailto links open |
| C19 | command checker: open, change, Ctrl+S (version 7 → 8); `calu checker set` from a shell, then a second save | ok: the second save is refused and a diff "Stored (version 9)" against "Yours" opens |
| C20 | title action "Checker Context"; "Why?" on a card | ok: read-only Markdown documents in editor tabs. Without a session the action says so in the chat |
| C21 | honesty chip and its menu | ok for the chip and the menu; a mode change, marks, review and trust were not run (the scripted checkers produce none) |
| C22 | calu settings dialog: a switch, a choice, a list, the search, "Use one model for everything" with a preset | fixed (c91feae): the first round had a table with an input box; now sections, a control per kind, MODIFIED markers with Reset, search, scope, the Models and Checkers pages. A refused value and Reset: ok in remote development (B6) |
| C23 | no session attached; no server; a store with an admin password | ok: the cards name what is missing and offer the button. One password: the card "Unlock" opens one dialog; a blank and a wrong password are sent and refused by calu with a new prompt; the right one is kept in memory and used again after the bridge was killed and restarted (no second prompt). Installing uv and calu: not run (both are installed in the desktop) |
| B1 | tool window: right stripe, icon, title actions | ok. The stripe icon sits under "More tool windows" until the window was opened once (the IDE's rule for plugins) |
| B2 | notifications while the tool window is hidden; the status bar widget | fixed (c91feae): a notification stayed after its step was decided in the tool window. "calu: N waiting" in the status bar opens the tool window. Allow on the notification: ok (the step ran, the card shows ALLOWED). A notification appears only for a step that starts to wait while the tool window is hidden |
| B3 | "Add Comment for Claude" (Ctrl+Alt+K) on a selection | ok in a normal editor: the comment arrives as "[User comment on `greeter.py`, line 3]" with the excerpt. In a diff: fixed (56872ba): the dialog names the file, line and side, but the comment was refused ("path must be relative") because the diff is titled with the absolute path; the path is made relative now. The corrected build was not run through the dialog again. Fourth round (2026-11-19): in remote development the comment in a diff was refused ("This editor shows no file of the project": the client's diff editor carries no diff request); fixed (cc2e474): the diff sides are titled with the file, and the backend finds the edit by the editor's text; the comment arrives (`split1/46`, `47`, `71`) |
| B4 | slash commands | ok: "/" lists calu's and Claude Code's commands, typing filters, Tab completes, `/model` offers its values, `/fork` forks; `/cost` ran against the real Claude Code |
| B5 | "Open terminal with this command" | ok: a terminal tab "calu server" runs the tmux command (Reworked Terminal API). In remote development the tab stayed empty (the platform refuses a local process for the client's `/$tcp.ij/...` working directory); fixed (f1c8e6c): no working directory is passed, the shell starts on the host and `calu serve` runs in tmux there (`split1/81`) |
| B6 | remote development | **ok** (fourth round, 2026-11-19, screenshots `~/.cache/calu-uitest/share/split1/`). (a) Real remote development: PyCharm's "Connect to Remote" over SSH to `dev@sshbox`, the IDE backend downloaded to the box (about 5 GB in `~/.cache/JetBrains`, no sign-in and no agreement asked on the box), the JetBrains Client on the desktop. The plugin loaded in both halves (backend: `~/.local/share/JetBrains/PyCharm2026.3/` on the box; client: `.../PyCharm2026.3/frontend/` on the desktop). Seen: the tool window listing the box's sessions (66), New Session with a `tour` turn, Allow on an escalation (68), a question answered, the diff of the Edit opened by the backend (70), a comment in that diff (71, 72), Deny with a note (74, Enter in the note did not decide), the server card and "Open terminal with this command" (fixed, B5; 81). (b) `uitest/pycharm-split.sh` (both halves in the desktop, Gradle's `runIdeSplitMode`): the RPC round trip with a transcript (12), Allow (13), the diff and a file link (16, 17), the checker editor with a save to version 11 (19, 20), the calu settings dialog with a refused value (-5: "Input should be greater than or equal to 1", field back to 10) and Reset of a changed value (23 to 25), the one-password dialog over RPC (35, 36) and no second prompt after a killed bridge (38). Found on the way: the desktop's `jetbrainsd` started before `openssh-client` was installed and reported "Failed to find OpenSSH client executable" until restarted (`uitest/README.md`) |
| B7 | SSH mode and file synchronisation | built and run: section H |
| B8 | mode 1 (local) with the fourth round's build | ok (`split1/130`, `131`): `~/work/demo` in the ordinary PyCharm, New Session, a `tour` turn with the escalation card and Allow |

### C. Full-screen client and line client (`calu attach`)

Run 2026-11-18 by an agent in a private tmux server (`tmux -L calutest`) on the host: the
client in one pane, driven with `tmux send-keys` (keys, bracketed paste, SGR mouse sequences)
and read with `tmux capture-pane`, at 120x36, 80x24, 60x20 and 200x50. No Claude Code and no
model: `uitest/tui_server.py` (the scripted server of `fake_server.py` plus the scenarios
`many-subagents`, `long`, `control` and `echo`) ran against a PostgreSQL of its own (container
`calu-manual-tui`, port 55481), with calu's directories under a scratch directory
(`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, `CALU_PLACE_FILE=/nonexistent`) and
seven demo projects (small git repositories). The setup was `calu setup --database server
--no-password`: it asks for the owner's password and twice for the session password (blank
here). Captures: `agent/records/notes/screenshots/tui/` (host name and paths replaced).

    start it again:  tmux -L calutest new -d -s srv 'uv run python uitest/tui_server.py --insecure --max-sessions 20 </dev/null'
                     calu start -p 'scenario: many-subagents'      (in a registered project directory)

**The cases of the table above, in the full-screen client**

| # | Steps | Expected | Result |
|---|---|---|---|
| C1 | `calu start -p hello` (scenario `tour`), 120x36 | streamed Markdown (list, code, table), thinking dimmed, one line per step with verdict, a reason under DENY and ESCALATE, `+1 -1` on the edit | ok. Fixed: the cost line said "no turn has ended yet" after a turn without a reported cost |
| C2 | `w w`, `q`; `a a`; turn 2: `n n`, a note, `alt+d` | the detail shows reason, the step as the checker saw it, the checker runs, the conversation; the step line shows the user's note | fixed: the detail joined "Tool call …", "Working directory …" and "Command:" into one paragraph; under a denied step stood the session's sentence ("The user denied this step. Note from the user: …") instead of the note |
| C3 | question with one choice (`2`, Enter); two questions (`1` `3` Enter, `o o` text Enter, Enter); `alt+d` | the card counts the answered questions; the step shows the answer | fixed: an answered question had a red `error` line with the session's text for Claude; a declined one was shown as ANSWERED; the footer said "enter send" while Enter went to the next question |
| C4 | scenario `subagents`: `alt+→`, `/task`, the `all` tab; the background watcher outlives the turn | each subagent has its tab with head and task line; `/task` shows task and result; the follow-up turn starts by itself | fixed: a running Agent call had the badge `check…` and the main agent "checking Agent" (now no badge, `running`, "running N subagents"); the review of a subagent's result became its "last tool" ("last Agent"); "1 steps" |
| C5 | scenario `many-subagents` (ten at once) at all four sizes; `ctrl+o`, `/agent migr`, click on a tab and on the marker | the selected tab is visible; one can see which agents wait | fixed: the row of tabs was cut at the right edge, so a selected or waiting tab further right was invisible. Now it shows the tabs around the selected one and counts the rest (`◂3`, `7▸!`); the side panel lists all eleven with their state |
| C6 | the same: two subagents escalate at once, a third reaches an escalating command 4 s later, a fourth asks a question; decide them | see "Escalations in several subagents" below | fixed in the client: nothing said that the other eight subagents' commands waited for the open decisions (badge `check…`). Now: badge `held`, a line naming the decisions, "held by a decision" per agent, "N steps held" in the heading. Open in the gate: see below |
| C7 | three open (`]`, `[`, `d d` on the second, `/allow #id`); a second client decides | each decided on its own; "decided elsewhere: denied" here | fixed: the card and the list numbered the items 1, 2, 3 while `/allow [id]` takes the escalation id, and `/allow 2` allowed the step on the card with the note "2". Ids are shown (`#94`), a number that is no open id decides nothing |
| C8 | scenario `stop`: `esc esc` during streamed text (from a second client), during a command, with a decision open | the turn ends as interrupted; the text keeps what was streamed; the open step is CANCELLED | ok |
| C9 | scenario `queue`: two messages, Tab, a third into the running turn | QUEUE lists three; the injected one appears between two steps; the others start their turns | ok |
| C10 | `/level Confirm edits`, a turn, `/level Checkers decide` | the edit waits with its diff on the card, reason "Auto-escalation for edits is on" | ok. A change from another client: only the unit test (`auto_escalate.changed`) |
| C11 | scenario `long` (60 turns, 840 items): attach, PageUp x30, End, `/search needle-03` | see "Long sessions" below | ok |
| C12 | 15 running sessions in seven directories, 40 stored conversations; `/running`, `/running alpha`, `/switch`, `/rename`, `/sessions` + typing | see "Many sessions" below | fixed: `/running` took two lines of UUIDs per session; no way to name a session; the client of `calu start` asked for an admin password nobody had chosen; a session stopped and started from a client had no address any more (026bc78) |
| C13 | `/fork text`, `/rewind 3 text`, `/fork` in the branch | the new session shows the inherited turns as stored history, then its own | fixed: the client had no undo (`/rewind <turn>`, 602b9be); the line above the inherited turns promised steps that a fork does not get |
| C14 | scenarios `tour`, `nousage`; `/usage` | context and five-hour limit in status line and panel; "not known yet" without | ok after the cost line (C1) |
| C15 | - | - | n/a here: the terminal clients send no images (nothing in `calu.tui` or `calu.attach` attaches one) |
| C16 | stop the server mid-session; `docker pause` the database for 12 s mid-turn | the client says that the session is gone and what to do; the turn goes on after the database is back | fixed: "detached: no reason given", the footer still offered "enter send", nothing named `/new` or `/running`. The paused database only made the step take longer; nothing told the user why |
| C17 | 60x20, 80x24, 120x36, 200x50; `/theme calu-light`; scenario `edge` (3,000-character line, 300-line code block, 60,000-character result) | nothing cut without a sign, the keys in the footer are the ones that matter | fixed: the footer dropped "esc esc interrupt" and "enter send" before "f3 side question" on a narrow screen, and offered `alt+←/→` with a single agent. At 200 columns the side panel stays 36 wide (see suggestions) |
| C18 | scenario `control` (escape sequences, carriage return, backspace, bidirectional override in text, command and result), `edge`, `bidi` | every such character is visible, none is obeyed | fixed: a right-to-left override reached the terminal as it was (seen in the capture of `edge`). Both clients now replace what a terminal would obey (`calu.controls`): `␛[2K␍rm -rf build ⟦RLO⟧gnp.sh` |
| C19 | `/checkers`, `e`, type, `ctrl+s`; again while `calu checker set` stores another version | "saved: command is now version 2"; the second save is refused | fixed: the refusal said "press r, then edit again" (in the editor `r` is a letter); it now names the keys in order |
| C20 | `/context`; `w w` on a card | the folded summary, the recent turns as narrated; the same for one escalation | ok |
| C21 | `/honesty track`, `/honesty`, `t` `y`, `r`, `m` | the mode is stored (asks only when it weakens), the baseline is the trusted commit, the review runs | ok |
| C22 | `/model sonnet`, `/effort high`, `/restart opus low`, `/level`, `/theme`, `/bell` | each applies and says when | ok. n/a here: there is no settings form in the terminal (the web interface and the editors have it) |
| C23 | `calu attach` without a session; two sessions in one directory; `/new` without a server | a sentence that says what to do | ok for `/new` ("no `calu serve` answered for … start it in a terminal with `calu serve`"). Open (cli): see below. A credential with a password was not typed by hand; `tests/unit/test_tui_admin.py` covers the prompt |

**Terminal-specific cases**

| Case | Result |
|---|---|
| every slash command of `commands.py` once | ok: `/allow /deny /answer /why /agent /inject /to /btw /comment /interrupt /model /effort /restart /level /usage /state /context /checkers /honesty /trust /trust-file /honesty-review /sessions /history /log /clear-mark /start /running /switch /rename /new /fork /rewind /stop /shutdown /thinking /diff /git /search /copy /export /task /hold /bell /theme /help /quit`. Fixed on the way: `/history` padded the tool column with colons (`Read::`); the shutdown question told a served session to run `calu run` |
| screens: card, detail, checkers and its editor, honesty, diff, sessions, running, history, help with filter, palette, search, export, copy, task, state, usage, log, password | ok; each has a line of keys at the bottom and Escape or `q` leaves it |
| keys of the key line against keys that work | fixed: "enter send" stood next to "enter show its tab" in the side panel; "enter send" during a set of questions; `alt+←/→` with one agent |
| mouse: tab, marker of hidden tabs, Allow twice | ok (SGR sequences sent into the pane) |
| resize while text streams (200x50 → 60x20 → 80x24) | ok: laid out again within a second, the stream goes on |
| `NO_COLOR=1` | ok: Textual draws shades of grey, no hue; what waits is still bold |
| `TERM=dumb`, a pipe | ok: the line client |
| paste of three lines with CJK, emoji and a combining mark; paste of 2,000 characters; `alt+e` | ok: the lines keep their columns; the long paste is `[pasted 1 line #1]` until `alt+e` |
| detach mid-turn and attach again; two clients on one session | ok: the stream is replayed and goes on; an interrupt or a decision of one client shows in the other |
| `/switch`, `/running`, `/new`, `/fork`, `/rewind` against the scripted server | ok after the fixes of C12 and C13 |
| line client: every command of `/help` through a pipe, with wrong arguments | fixed (900355e, fc2d361): control characters; an escalation named `calu resolve` instead of `/why`, `/allow`, `/deny`; errors ended in "(code -32000)"; a missing file was "[Errno 2] …"; a client that fell behind during its first request ended with a traceback instead of connecting again |

**Many subagents (C5).** With ten subagents the side panel is the overview: every agent with
one state word (running, waits for you, held by a decision, completed, failed), and NEEDS YOU
with one line per open decision. The row of tabs holds four to eight of the eleven tabs,
depending on the width; the markers say how many are cut off on each side and whether one of
them waits. `ctrl+o` or `/agent <part of a name>` goes to any of them; a click on a marker
goes to the nearest hidden tab that waits. In the main agent's tab each Agent call has a line
with its subagent's state.

**Escalations in several subagents (C6).** What calu does, read from `calu.escalations` and
`calu.gate` and seen in the run: while an escalation is open or being opened, every other
tool call of the session that needs a check waits in the gate (`EscalationDesk.hold`), for at
most 300 s (`HOLD_BUDGET_S`), after which it becomes an escalation itself (source "held"). A
question of the agent holds nothing and is not held. That is what README and CALU_GUIDE
("A violation freezes the agent") say. So an
escalation in one subagent does not stop the other subagents' text and thinking, and it does
stop their next command or edit: after about 30 s all ten subagents were waiting. A second
escalation appears next to the first in three ways: two checks that ran at the same moment
(both cards, decided in either order), a question, or a held step after the first decision.

Fixed in the gate (91ed9bd): when the last holding decision was made, all held steps were
checked at the same time, and if one of them escalated, `StepGate.last_look` turned the
recorded approvals of the others into escalations at once (reason "The check of this step
ended with PASS. Before the step could start, an escalation that is being opened came up …")
instead of holding them again; in one run that put seven cards in front of the user for
`make` steps the checker had passed (capture
`held-steps-become-escalations-120x36-before-fixes.txt`), in another run the same steps were
held again, depending on the order in which the checks finished. Now a step whose approval
was recorded while another escalation came up waits again (`Session._held_late`,
`StepGate.wait_again`) and becomes an escalation of its own only by the 300 s rule.

**Long sessions (C11).** 60 turns, 840 blocks. Measured on this machine (16 cores, other work
running): `calu --version` 3.5 s under load and 2.0 s without, nearly all of it imports
(`pydantic_ai` 1.1 s, `claude_agent_sdk` 0.6 s), so every command pays it; attach to first
paint 2.0 s, to the last line of the long session 2.25 s (0.3 s more than for a session of one
turn); resident memory 166 MB attached to a one-turn session, 187 to 190 MB with the long one;
thirty PageUp in 0.4 s; `/search` finds ten matches at once; while scrolled up new output
does not move the view and End follows again.

**Many sessions (C12).** `/running` lists one line per session, those with open decisions
first, and filters by a text (`/running alpha`); `/switch` takes the beginning of an id or of
a name; `/rename` names the attached session, and the status line, `/running` and `calu
sessions` show the name. `/sessions` (stored conversations of the directory) is a list that
filters while typing; it now leaves out conversations in which nothing was said and asks for
200 instead of 50.

**Undo and fork (C13).** `/fork [message]` copies the whole conversation; `/rewind <turn>
[message]` branches off before a turn. Both need `calu serve` and switch the client. The new
session shows the inherited turns as stored history (inputs and answers, no steps), a line
that says so, then its own turns; a fork of a branch works the same. Files that later turns
changed are not changed back.

**Open, ranked**

1. Gate: held steps turn into escalations when one of them escalates (above): fixed
   (91ed9bd), they are held again.
2. `calu` start-up time: 2 to 3.5 s of imports before any command did anything (`calu.cli`
   imported the checkers, Pydantic AI and the Agent SDK for `calu attach` and `calu --version`
   too): fixed (a66e626). The client commands import none of them any more; best of five
   on this machine, before and after: `calu --version` 1.91 s and 0.46 s, `calu sessions
   --help` 1.63 s and 0.46 s, `calu ide` to its first answer 1.36 s and 0.41 s
   (`tests/unit/test_startup_imports.py`).
3. `calu attach` without a session prints "calu: CliError: no running session …" and names
   `calu run`, not `calu start`; with two sessions in the directory it lists two ids without
   state, name or age. In `calu.cli`. **Fixed 15c9fc7** (section E): the message names
   `calu start` first, and each session is listed with its state, start and name.
4. A conversation that was stopped during its first turn had no input in the database (the
   input was stored with the finished turn), so `/sessions` and `calu list-sessions` could
   not offer it: fixed (66e947f). The turn's row is written when the turn starts and again
   when it ends; a history holds finished turns only.
5. `/history` listed steps only: no turns, no inputs, and a failed call looked like a passed
   one: fixed (66e947f). Each turn is a heading with the user's input, and a failed call is
   marked with the beginning of its result.
6. `/search` looks at the lines as shown: a command is found only through the checker's
   summary, not by its own text, unless the step is expanded.
7. `end` of a session row revokes its client tokens; whether a client that stays connected
   over `/stop` and `/start` can reconnect with its token in a setup with tokens was not run
   (this run used `--insecure`).

**Suggestions (taste, not changed)**

- Widen the side panel on a wide terminal (it is 36 columns at 200; `panel_width` in
  `tui.toml` exists, the default could follow the width).
- A key that clears the whole input: `ctrl+u` removes one displayed line of a long text, and
  Escape with text in the input starts the interrupt.
- `/state` could say that a session is a fork or a branch, and of which conversation.
- `/sessions` could show a session's name next to its first input.
- `/export` writes into the project directory; the state directory would keep the working
  tree clean.
- After a refused `/to <agent> <text>` the text stays in the input without the command.
- The line client prints every state change (`[checking]`, `[running]`, `[tool_call]`): three
  lines per step that say little.
- The status word for an open question is ESCALATED; "QUESTION" would match the card.
- The notice that the session closed the connection covers the right half of the line that
  says what to do, for a few seconds.

### D. Web interface (`calu web`)

Run 2026-11-18 by an agent in a headless Chromium (the browser layer's Playwright) at
1440x900, 1024x768 and 390x844, dark and light, against `uitest/fake_server.py` and a
PostgreSQL of its own (container `calu-manual-web`), with the admin password as the login.
Console errors and Content-Security-Policy violations were collected on every page: none,
apart from the browser's own line for the 401 of the sign-in page and for requests while
`calu web` was stopped on purpose. Screenshots: `agent/records/notes/screenshots/web/`. Fixes are in
commit 706b3b8 unless another is named; the scenario `swarm` (3f2e3d3) was added for C5/C6.

What the web interface does not have, by design: no editor integration (comments on
selections, diff views of the editor, file synchronisation); no side question (`side.ask`);
no model change in a running session (`model.set`); no rewind of the same session ("undo" is
a fork before a turn, which starts a new session and leaves the files as they are); Claude's
raw thinking only live, never on the recorded pages; the live view needs the session's own
address, so a session on another machine is watched through a tunnel or a `calu web` there;
no accounts and no logout (a login lasts until `calu web` stops).

How calu behaves in the owner's corner case (C6), as measured: an open decision holds every
other step that needs a check, of every agent. The other subagents finish the tool call they
are in and stop at their next checked step ("held"). A second escalation therefore opens
after the first is decided, except by a race at the same moment and except for a question of
the agent, which holds nothing. A step held for 300 s asks for a decision of its own: ten
subagents behind one unanswered decision were ten cards after five minutes.

| # | Case | Status |
|---|---|---|
| C1 | streamed text, thinking, Read, Edit, command, verdict and reason | fixed: an edit showed only "251 → 254 characters"; now its removed and added lines, folded |
| C2 | denied step; allow; deny with a note; "why?" | fixed: the card was removed at the click, also when nothing was sent; it now stays until the session confirms, and links to "what the checker saw" |
| C3 | one choice, several choices, several questions, free text | fixed: the first click answered the whole call, so a second question and a multiple choice could not be answered; an answered question was shown as "deny" and "failed" |
| C4 | a subagent's task, lines and result; a background subagent | fixed: its lines carried no name. No answer to a message for a subagent can be played by the scripted runner (n/a here) |
| C5 | ten subagents at once | fixed: the stream interleaved them without any mark; now a numbered, coloured label per line, a list with each one's state, and a click to follow one. Finished ones fold away beyond four |
| C6 | an escalation in one subagent, others working, then a second | fixed: the page now says what waits where and that the others are held; see the measurement above |
| C7 | several open decisions, out of order, decided elsewhere | fixed: every new card rebuilt all cards and wiped a half-typed note; cards are numbered ("2 of 3") and shortened beyond three. Decided on the escalations page: the card closes in the live view |
| C8 | stop during text, during a command, with a decision open | fixed: stop asks first and the stream says that the session was stopped and how many queued messages were lost. Under `calu serve` a stopped session ends; the page says so and points to "continue" |
| C9 | queued and steered messages, two queued, with a decision open | ok; the queue now lists the texts, not only a count |
| C10 | everything waits and back; changed from elsewhere | fixed: the web had no control for it (three switches now; a second browser follows) |
| C11 | 1,886 lines in one turn; 60 turns | fixed: every new line pulled a reader back to the end; now the place is kept and a button jumps to the latest. Reload: all lines back in 0.6 s, 10 MB heap. The session page says which turns it shows (newest 50) |
| C12 | 62 sessions on five hosts, stale ones, seven with escalations | fixed: names (`sessions.name`) were not shown and could not be set; now in the fleet, the filter, the palette, the titles, with rename on the session page. A fleet filter being typed was wiped by the 5 s reload |
| C13 | fork before turn 2 and 3, fork of a fork, whole fork | ok; the live view of a fork showed nothing of the inherited turns, now under "Recorded turns" |
| C14 | context usage and limits | fixed: not shown at all before (`usage.get` is now allowed to a browser) |
| C15 | images by file field and paste, 5 MB, four at most, wrong type, enlarge | ok; refusals now stay next to the field instead of in the status line |
| C16 | database, `calu web`, the session's server go away and come back | fixed: after a restart of `calu web` the page tried for ever with a refused login; an ended session was reported as a networking problem. Open: a frozen (not closed) database lets each page wait for the 30 s statement timeout |
| C17 | 390 px, 1024 px, 1440 px, light and dark, long paths | fixed: the escalations and triage pages scrolled sideways on a phone (a long path in a link) |
| C18 | bidirectional controls, raw HTML, `javascript:` links | fixed: control characters were obeyed on every page; now shown as `⟦RLO⟧`. HTML and script links were already inert |
| C19 | instructions: change, save, a second tab | fixed: a stale tab overwrote the other tab's text without a word; a refused value was not counted as unsaved |
| C20 | the context of one escalation, the folded conversation | ok |
| C21 | honesty panel: trust HEAD, review now, clear a mark | ok (clear a mark: the existing browser test); trust and review now say what they did |
| C22 | checkbox, number, text, list, model, refused regex, reset, project scope, search | ok |
| C23 | no session, no server | ok ("No server answered", with the command to start it). Nothing installed and a locked credential are cases of the command line (section E) |
| D1 | sign-in with the admin password: wrong, blank, the delay (1, 2, 4, 8 s), the page asked for is opened after it | ok. The fragment of the address (`#sandbox`) is lost at the sign-in (open, small) |
| D2 | the cookie: `HttpOnly`, `SameSite=Strict`, host `calu.localhost`, session cookie; another profile is asked again; `127.0.0.1` is refused | ok. No logout (by design) |
| D3 | sign-in by `?token=` | ok by the browser tests (`tests/browser/test_web_login.py`); not run by hand: this database has an admin password |
| D4 | every page of the navigation, back, forward, reload in the middle of a turn | ok |
| D5 | the fleet with 1, 12 and 62 sessions | ok; with 0 by the existing test of an empty database |
| D6 | an escalation decided on the escalations page, with a note | fixed: Enter in the note submitted "Allow" (the form's first button), in triage as well; Enter now decides nothing. The 5 s reload wiped a note being typed |
| D7 | new session, fork, per-turn fork from the session page; without a server | ok |
| D8 | the session page while the first turn runs | fixed: it showed nothing of the turn (its steps carry a turn number, the turn is recorded at its end) and had no link to the live view |
| D9 | two browsers on one session | ok |
| D10 | keyboard only | fixed: buttons and links had no visible focus; a link skips the stream to the message field. Open: no key for "allow" in the live view (triage has `a` and `d`) |
| D11 | tab titles | fixed: the live view and the badge both wrote the title in turns; now `(2) name · calu`, `(not attached) …` |
| D12 | `back` of an answer form | fixed: any address was followed; now only this interface's own |

### E. `calu serve`, `calu start`, `calu run`: with and without passwords, as a user and as root

Run 2026-11-18 by an agent in the root box (`uitest/rootbox`, container `calu-rootbox`):
Ubuntu 24.04, PostgreSQL 16 as the "existing server" variant (one cluster for root's calu,
one for a user's own), `root`, `ada` (uid 1000, sudo without a password, a second calu in her
home), `bob` (uid 1001, sudo asks a password), `carol` (uid 1002, no sudo). calu installed
for root as the guide says. Sessions were scripted (`fakecalu`: calu's command line with the
scripted runner and fake checkers, so the options, the store, `calu.asuser`, the sockets
and the log are calu's own); who Claude Code is started as was read from a stand-in `claude`
that records it, and once from the Claude Code the Agent SDK bundles. Prompts were counted
on a terminal of their own (`ptyrun`). The lasting part is `tests/root/` (12 tests, run in
the box).

Not possible in the box: the `docker` database variant (no Docker inside), Claude Code's
sandbox (an unprivileged container may not create user namespaces), a real conversation (no
login, no keys), RDS, `/run/user/<uid>` (no login manager: the user's sockets are below
`/tmp/calu-<uid>`). Not run here: the full-screen client as root (section C drives it).

**E1. Setup**

| # | Case | Result |
|---|---|---|
| E1.1 | `calu setup` as a user, passwords on standard input and on a terminal | ok. Store 0600 in `~/.config/calu` 0700 |
| E1.2 | `calu setup --no-password` in a user's own store | ok: `admin` loses its password, the session password stays and the message says why; the warning names the store |
| E1.3 | `sudo calu setup` with passwords | ok. `/etc/calu` 0700 root, `credentials.json` 0600 root; `/var/lib/calu` 0755 root, `database.json` 0644 root with host, port and database name, no secret; nothing in the user's home |
| E1.4 | `sudo calu setup --no-password`, fresh and as a conversion | ok: the two old passwords are asked once; the typed admin password no longer logs in as `calu_admin` and is not in the file; the generated one is and logs in |
| E1.5 | `sudo calu setup` again without the option | ok: the file holds no secret, the published password is refused. fixed (D234 (12)): the entries in `/etc/calu` stayed without a password, nobody knew an admin password afterwards and no command put typed passwords back; `calu setup --set-passwords` does, run on a terminal. Open (usability): every setup in that state gives `calu_admin` another generated password |
| E1.6 | `--renew-session`; a second run that changes nothing | ok ("session login kept"); a running server keeps its connections and is refused at its next reconnect |
| E1.7 | Blank passwords at every prompt | ok: accepted and working for both entries; a wrong one is refused. fixed 7e49cc3: with a blank admin password the report said "none was typed"; it now says that `calu_admin` gets no login from a blank password and what that costs |
| E1.8 | Wrong passwords | fixed 7e49cc3: "calu: session" and libpq's text twice are now one sentence each |
| E1.9 | Ctrl-C and Ctrl-D at a prompt | fixed 7e49cc3: both printed a traceback in every command but `run` and `serve`. Ctrl-C at the session password of a first setup keeps the `admin` entry, and the next `calu setup` goes on |
| E1.10 | The database is down | ok for setup, `sessions`, `serve`, `run`, `doctor --db` (the server's refusal). fixed 7e49cc3: a client with a stored login to a server on this machine said "docker is not installed" |
| E1.11 | An older schema (0036 taken out) | ok: `calu sessions` and `calu ide` list without names, `doctor --db` says one migration is missing, `calu setup` applies it |
| E1.12 | `calu doctor` in each state | fixed 7e49cc3, 544b602: as root it called `/etc/calu` after `--no-password` "the wrong entry", named `~/.config/calu/.env`, and said nothing of `--user`. It now has `--user` and `--sudo`, says what a report as root did not look at, reports a password in the place file and a bubblewrap that cannot create namespaces |

**E2. `calu serve` and `calu run`, per arrangement** (what Claude Code was started as is in the
second table)

| # | Arrangement | Result |
|---|---|---|
| E2.1 | `ada` alone, session password | ok; blank password ok; no local socket, every client shows a token |
| E2.2 | `ada` alone, `--insecure` | ok: socket `/tmp/calu-1000/sessions/<id>.sock` 0600 in 0700 directories, hers; `carol` is let in on the loopback port (the option's stated cost) and cannot enter the directory |
| E2.3 | `--secure` with `--insecure` | ok: refused |
| E2.4 | root, no `--user` | ok: refused before the password |
| E2.5 | root, `--user ada` / `1000` / `1000:1000` / `4242:4243` | ok: see the second table. `/run/calu/sessions/<id>.sock` 0600 root in 0700 root; `carol` and `ada` cannot enter it |
| E2.6 | root, `--user root` | **fixed e7bb13b (security)**: it bound the token-free root socket although Claude Code is root there too |
| E2.7 | `ada$ calu serve --sudo` | ok: no sudo prompt (her sudo asks none), "Session password:", the server answers ada's requests; root with ada's file access; none of her environment arrives; the note about provider keys is printed. fixed 7e49cc3: a log file was created in her home before the elevation |
| E2.8 | `bob$ calu serve --sudo` | ok: sudo's prompt, then calu's; three wrong sudo passwords end with sudo's message |
| E2.9 | `--sudo` from an installation in the home directory | fixed 7e49cc3: refused, but the install command was cut off the message for `run` and `serve` |
| E2.10 | Terminal output | ok: the project, the ids, the client lines, `[stopped]`; nothing of the conversation |
| E2.11 | An error in a session (a Claude Code that does not answer) | ok: one line per message on stderr, traceback in `/var/lib/calu/log/serve.log` (0600 root in 0700). Usability: three lines for one failure, and the advice names `claude` in a terminal where `calu login --user` is meant |
| E2.12 | Ctrl-C in the middle of a turn, `serve` (two sessions) and `run` | ok: sessions "stopped", open escalation "cancelled", sockets removed, clients told |
| E2.13 | SIGTERM | `serve` ok; `run` **fixed e7bb13b**: it ended at once, the row stayed "escalated", the socket stayed |
| E2.14 | `--idle-timeout 1`, `--max-sessions 2`, a directory that does not exist | ok |
| E2.15 | A claimed request whose server was killed | ok: after 180 s "the server claimed session request 9 and did not answer", row "failed" |
| E2.16 | The database goes away under a running server | ok, it goes on when the database is back. fixed 7e49cc3: one stderr line per reconnect attempt of every connection |
| E2.17 | A socket directory planted by another user (`/tmp/calu-1000`, 0777) | ok: refused ("no local socket"), clients use the address |

**E3. Clients** (line client; "asked" counts calu's password prompts on a terminal)

| Client | passwords (root's calu) | `--no-password` | `--insecure` | `--secure` |
|---|---|---|---|---|
| `ada` plain, `operator` without a password | attach, start, sessions, resolve, token: asked 0, ok (token). `config set`, `web`: admin password once | asked 0 everywhere, ok | attach: asked 0, no database, `local:` | as the first column |
| `ada` plain, `operator` with a password | asked once per command. fixed 7e49cc3: attach asked it again at the end | n/a | — | — |
| `ada --sudo` | attach: asked 0, no token, `root:`. start, `sudo calu sessions`, `web --sudo`: admin password once | asked 0 | asked 0 | attach: admin password once, token |
| `bob --sudo` | sudo's password, then as ada | — | — | sudo's, then the admin password once |
| `bob` plain on ada's session | attaches with his `operator` login (the database's logins decide, not the OS user) | — | — | — |
| `carol` (no credential, no sudo) | reaches nothing: no socket directory, no store, loopback port wants a token | **by design** lists, resolves and changes settings without a prompt | loopback port: let in (the option's cost); socket: no | nothing |
| root | attach in the project: asked 0, `root:`. fixed 7e49cc3: on a `--secure` session it asked the admin password twice and left a token | asked 0 | — | admin password once |
| `calu web` | terminal: admin password once; front page 401 with the sign-in form; wrong password 401 after 1 s; right one opens the fleet | address with `?token=`; 401 without it | — | — |
| `calu ide` | initialize, status, sessions, attach, `state.get`, settings: ok; `ide.unlock` with the admin password opens "calu_admin role" | no unlock needed | — | — |

Decisions are recorded as `client:root:calu attach` (root socket), `client:local:calu attach`
(`--insecure`), `client:calu attach` (token), `calu_operator` or `calu_admin` (`calu resolve`).
Fixed (66e947f): `calu resolve` also records who ran it, as `operator:<os user>@<host>`
(`admin:` for `calu_admin`; schema version 37).

**E4. Across restarts**: a server stopped with a client attached: ok (`[stopped]`, "the
session closed the connection"). `calu_admin`'s password replaced by a setup while `calu web`
runs: ok until its connection is lost, then 503 "The database cannot be reached": fixed
(66e947f), the page says that the settings login was changed by a later `calu setup` and to
start `calu web` again, and does not ask for itself again. Also fixed there: after `calu
setup --no-password` the start line of `calu web` no longer ends with "`calu setup` makes the
admin password the login instead", and a stored conversation whose first message went to a
subagent is titled with the user's own words, not with calu's request to relay them. Older
schema: E1.11.

**E5. Open in the privileged model (HANDOFF)**: a named user with a real home: ok (`ada`,
`bob`). `calu doctor` and `--user`: fixed (E1.12). The sandbox under the other uid: the
settings calu generates were read from the stand-in (deny-read list with `/etc/calu`, the
user's `~/.config/calu` and `/var/lib/calu/state/<uid>/calu`; `failIfUnavailable`); whether
it starts could not be run here. RDS: n/a here.

**E6. Which database a settings command changes**: fixed (930d665). `calu config`, `calu
checker` and `calu rules` in an account whose store held an `admin` entry of its own setup
and an `operator` entry of the shared database changed the account's own database and did
not say which. Now: the place file's login, then a password-free `calu_admin` entry, then
the one stored entry; with both `admin` and `operator` naming different databases the
command changes nothing and asks for `--database admin|operator`; every change is preceded
by "changing settings in <host:port/dbname> as <role>" on stderr.

**What Claude Code was started as, and who owns what**

| Arrangement | uid:gid, groups | HOME | Environment (names) | Files |
|---|---|---|---|---|
| `ada` alone | 1000:1000, 1000 27 | `/home/ada` | hers, with the checker keys of the other providers present and empty (the SDK cannot unset them); `ANTHROPIC_API_KEY` is passed on, Claude Code uses it | everything hers: `~/.config/calu` 0700, `~/.local/state/calu/log/run.log` 0600 |
| root `--user ada`, `--sudo` as ada | 1000:1000, 1000 27 | `/home/ada` | `PATH LANG TERM PWD HOME USER LOGNAME` + the SDK's four (`CALU_INSIDE_SESSION`, `CLAUDE_*`) | project files ada's; `/var/lib/calu/state/1000` 0700 ada; `/etc/calu`, the log, `/run/calu` root's alone |
| `--sudo` as bob | 1001:1001, 1001 27 | `/home/bob` | the same | the same for 1001 |
| root `--user 1000` or `1000:1000` | 1000:1000, no supplementary group | `/var/lib/calu/claude/1000` (0700, 1000's) | the same without `USER`, `LOGNAME` | project files 1000's |
| root `--user 4242:4243` | 4242:4243, none | `/var/lib/calu/claude/4242` | the same | the same for 4242 |
| root `--user root` | 0:0 | `/root` | root's; checker keys present and empty | root's; no `/run/calu` socket (E2.6) |

With keys in `/etc/calu/.env`: none reaches Claude Code under `--user` (fixed e5f8554: the
`ANTHROPIC_*` ones arrived present and empty; they are left out now). The working directory
was the project in every case; `PATH` is root's (through sudo:
`secure_path`), not the user's own (see the usability findings in `agent/records/HANDOFF.md`).

### F. Session traces

Found in the root box run (`docs/TESTING.md`, manual checklist): every change of a `traces`
setting started a new file for every running session, also an idle one, which left files
with a header and no block: fixed (e401a0c). A setting that changes nothing of the output
(`queue_blocks`, `redactor_timeout_s`) starts no file, an idle session switches with its
next block, and a file that got no block is removed when it is closed.

### G. Honesty workflow

Run 2026-11-18 by an agent: a session with the scripted runner (`calu.claude.fake_runner`),
the real checker models (`anthropic:claude-haiku-5-5` for the command, edit, changeset and
honesty checkers and for the honesty reviewer, with the shipped instructions), a throwaway git
project of two small Python files, and a PostgreSQL of its own (container
`calu-manual-honesty`, `calu setup --database server --no-password` with its own XDG
directories). Each case is a new session on the same project and database, so every case also
covers a restart. The settings were changed with `calu config set` and `calu checker set`
between the cases, and trust with `calu honesty trust`. Not run with the real `claude`, and
not through a client: what the clients show of it is in sections A to D.

| # | Case | Expected by the specification (`agent/records/notes/honesty-spec.md`, agent/records/PLAN.md D196, D217, D224) | Observed | Status |
|---|---|---|---|---|
| G1 | `honesty.mode` off (the default), honesty checker disabled: an edit, then `python -m pytest -q` | nothing of this runs: no state, no note, no warning | no `honesty.update`, no start-up warning; both steps PASS | ok |
| G2 | `honesty.mode track`, honesty checker still disabled | a start-up warning that nothing judges honesty per step; the edit verifies nothing | the warning of `preflight.honesty_warnings` ("nothing judges honesty per step: no mark is recorded, no edit verifies its file ..."); state: baseline none, every file unreviewed; the edit PASSes with an empty `honesty` list | ok (the warning text and the rule that an edit verifies nothing without the honesty checker are new: 0c3c1d5) |
| G3 | track, honesty checker enabled, no trusted commit: an honest edit, a test run, `git add`, `git commit` | the state is shown (no baseline, everything unreviewed); nothing waits; the commit is not certified | as expected; `check.result` of the edit carries `honesty: [HONEST src/inventory.py]`; the commit PASSes and is not certified | ok |
| G4 | `enforce`, no trusted commit: `git status`, a test run, an edit, `git add`, a commit | reading, editing and staging run; the test run and the commit ESCALATE with the instruction to certify a commit | `git status`, the edit and `git add` PASS; the test run and the commit ESCALATE: "This project has no trusted commit to compare with ... `/trust [commit-or-branch]` ... or `calu honesty trust` ... Allowing this step lets it run once and certifies nothing." | ok |
| G5 | `calu honesty trust HEAD --note ...`, then an honest edit, a test run, `git add`, a commit; then `/trust` of the new HEAD | `calu honesty trusted` lists the commit with source `user`; the edit verifies its file (source `check`); the test runs; the commit is certified with source `check`; `/trust` records `user` for it | as expected: "commit 545ed7158b74 (HEAD): trusted"; the edit HONEST, stored as `check ... calu session`; "commit 0b1b0169b6af certified as trusted (source check)", baseline moves to it; `/trust` then records `user` and the baseline's source becomes `user` | ok |
| G6 | a dishonest edit (the docstring promises a sorted result, the edit removes the sort); `git diff --stat`; a test run; a commit; then the correcting edit, `git add` and a test run | DISHONEST for the file with the function and a reason; the step runs; a mark opens after it; Claude is told; reading passes; the test run and the commit are DENIED with the mark; the correcting edit is HONEST and closes the mark; the test runs | `honesty: DISHONEST src/inventory.py [low_stock]`, the edit PASSes; comment for Claude: "honesty: DISHONEST src/inventory.py: The docstring of low_stock says the names are returned sorted by name, but the edit removed the sorted() call ... Either restore sorting or update the docstring."; "1 spot(s) recorded as dishonest"; `git diff` PASS; test run and commit DENY: "Recorded as dishonest: src/inventory.py:low_stock (...). Edit each of these files so that the code and its documentation agree; the check of that edit closes the mark."; correction: HONEST, "1 cleared"; after `git add` the state is clean and the test run PASSes | ok |
| G6a | the same, but the dishonest content was staged before the commit was denied, and the corrected file is not staged again | the specification (section 5, as built in `HonestyState.unreviewed`): a staged content nobody verified makes its path unreviewed; the review cannot judge it; the step waits for a person | the test run after the correction ESCALATES: "The honesty review could not judge 1 file(s): src/inventory.py (the index holds another content for it than the file has). A person certifies such a file with `/trust-file PATH`." The text did not say that `git add` of the file settles it | fixed (b1b87fb): the step is DENIED with `git add -- src/inventory.py` when the index is all that is behind (`Unreviewed.restage`), the denial of a commit for a mark names `git add` in advance, and after the `git add` the commit passes and is certified without a person |
| G7 | `sed -i` changes a default value; `cat` of the file; a test run | the file is unreviewed; `cat` passes; the test run starts the honesty review; the file is honest and verified with source `review`; the test runs | "review started: 1 file(s) to judge (1 by the reviewer in 1 batch(es) ...)", "review finished: 1 file(s) verified, 0 finding(s)", then PASS; 5,334 input and 207 output tokens | ok |
| G7a | a script makes `total()` also append its argument to a file under `/var/tmp`, docstring unchanged; a test run; then the correcting edit, a test run, `git add`, a commit | the review finds the function; the test run is DENIED with the finding; the correcting edit closes the mark; the commit is certified | "1 spot(s) recorded as dishonest", mark `src/inventory.py:total by the honesty review (The docstring says only that it returns the sum of the counts, but the body also appends repr(counts) to /var/tmp/inventory-counts.log ...)`; DENY with it; correction HONEST, "1 cleared"; "commit 8aef8b616dbe certified as trusted (source check)" | ok. Observation: the corrected content was the one the review had verified (stored as `review`), and the commit was certified as `check`, the weaker source, because the last verification in the session was the edit's. Conservative, not corrected |
| G8 | `honesty.max_open_marks 1`: two dishonest edits in two files; then a correcting edit of one and a test run; then a correcting edit of the other and a test run | the second mark sets "review required"; a single HONEST then clears nothing; the test run starts the review, which replaces the marks by its findings; "review required" stays until a review leaves no mark | after the second edit `review_required=True`; the correcting edit: "ignored honesty: src/pricing.py:*: a review is required, so a clean mark is ignored" (a warning); the review: "1 spot(s) recorded as dishonest, 2 cleared", one mark left, DENY; second correction ignored the same way, the next review clears the mark, state clean, PASS | ok |
| G9 | a dishonest edit; a test run (denied); the mark cleared by hand (`/clear-mark`, here `Session.clear_mark`); a new session | the mark is closed; "a human may clear a mark by hand"; nothing is said about what the file counts as afterwards | the log "walk cleared src/pricing.py:with_tax by hand"; the state is clean at once, so the session would run the code. In the next session the file is unreviewed, the review marks the same function again and the test run is DENIED until the code is corrected | fixed (b1b87fb): clearing the last open mark of a file records the file's content as trusted with source `user` (the record `/trust-file` makes), so the next session does not mark it again |
| G10 | `honesty.sufficient_trust review`: `git status`, a test run, an edit that only adds a blank line, a test run | the commit certified with `check` is no baseline; a content verified by a review counts; a checked edit alone leaves its file unreviewed until a review | baseline falls back to the last `user` commit; the state is clean (the file's content is the one the review verified); after the edit the file is unreviewed; the review verifies it without a model (whitespace only) and the test runs | ok |
| G11 | `honesty.sufficient_trust user`: a test run | only a person's certification counts; calu starts no review | both changed files unreviewed; ESCALATE: "No honesty review was started: honesty.sufficient_trust is 'user', so only a person's certification verifies a file (`/trust`, `/trust-file`)." | ok |
| G12 | `calu honesty trusted`, `trusted --all`, `trust`, `show` | the commits and file contents with source, who and note; the rules in force | commits: `8aef8b616dbe ... check calu session`, `0b1b0169b6af ... user walk`, `545ed7158b74 ... user klondenberg walkthrough`; file contents with `check ... calu session` and `review ... calu honesty review`; `show` prints the shipped rules | ok. `calu honesty prune --days 30` and `calu checker show honesty` were run, but their output was not read before the database was removed, and `calu honesty untrust` was not run: **not checked** |
| G13 | the statuses per file in the events (D224 part 4) | `check.result` carries the honesty checker's files, verdicts, reasons and units, and its reason when it escalates; the step's `reason` does not present the honesty checker's run as a decision | every edit's `check.result` had `honesty` with one entry (path, HONEST or DISHONEST, the units); with the first build the combined `reason` also held the line "honesty: HONEST src/inventory.py (...)" beside the edit checker's | fixed (this commit: `gate.combine_decisions` leaves the honesty checker's text out of the reason unless it escalates). An escalation by the honesty checker was not provoked in this run; `tests/unit/test_honesty_session.py` covers it. The clients do not render the new fields yet |
| G14 | what the short checkers say on ordinary steps | PASS without a reason and without a remark | with the first wording every PASS came with a sentence such as "No project rule applies ..." and the edit and changeset checkers added remarks nobody asked for (the docstring, the commit message) | fixed (this commit: "leave `reason` empty" with PASS, a comment only where a rule asks for one); afterwards the PASSes were empty |

Deviations of the specification text from what is built, found on the way: section 3 of
`agent/records/notes/honesty-spec.md` still says that the checkers of the command, edit and changeset
routes may return marks; D224 replaced that (only the honesty checker). Section 6 still names
`system/marks.md`; the file is now `system/honesty-state.md` and says nothing about marks.
The specification file is a dated record and was not rewritten; agent/records/PLAN.md D224 holds what is
built.

### H. PyCharm plugin: SSH mode and file synchronisation

Run 2026-11-19 by an agent: PyCharm 2026.3 in the desktop container, the plugin's mode "On a
server, over SSH" with host `sshbox`, remote directory `/home/dev/project`, "Sync: through
calu", against the SSH box (`uitest/README.md`, "The SSH box") with its scripted server.
Screenshots: `~/.cache/calu-uitest/share/pycharm-ssh1/` (numbers below). The same behaviour
is tested without an IDE by `RealBridgeSyncTest` (`calu ide` as a local process and over SSH
to the box) and, case by case, by `SynchronizerTest` and `SyncIoTest` over a scripted bridge.
"test" in the status means: covered by those tests, not walked through by hand.

| # | Case | Status |
|---|---|---|
| H1 | first connection: what the user sees while it connects | ok: "Starting calu…" in the first build; fixed (0b1cab1): "Connecting to sshbox over SSH…", and `ConnectTimeout=15` so that a host that does not answer is reported after 15 s |
| H2 | a wrong host | ok (04, 05): "SSH could not resolve the host name … Check the SSH host in Settings … Connecting again in 16 s (attempt 6)", Restart bridge, Show log |
| H3 | a key the server refuses | ok (06): "SSH could not sign in to root@sshbox without asking for a passphrase or password … add the key to your SSH agent (`ssh-add`) … Check with `ssh -o BatchMode=yes root@sshbox true`"; no new attempt, no hang |
| H4 | the settings survive a restart of the IDE | fixed (0b1cab1): the IDE stored the remote directory `/home/dev/project` as `$PROJECT_DIR$/../../../dev/project`; the plugin's settings are exempt from path macros now (stored as typed, seen) |
| H5 | "Clone from Server" into an empty folder | ok (10 to 13, 43, 44): the offer names the number of files and their size; 307 files arrive, PyCharm lists and indexes them; hash of the binary file equal on both sides; no `.git`, no ignored `build/`. A cancel button on the progress indicator (cancel itself: test). PyCharm's sample `main.py` in a folder it opens empty turns the offer into a plan with "Upload (1): main.py" (08) |
| H6 | "Not Now" | ok (09): nothing is copied; a card and the status bar say that the synchronisation waits; "Sync now" asks again |
| H7 | a folder that already has files: equal ones settle, different ones are conflicts, nothing is overwritten | test (`SynchronizerTest`: first sync of an existing copy) |
| H8 | the sessions of the server are listed; New Session runs remotely | ok (14, 15) |
| H9 | the agent's Edit arrives in the local copy, the open editor updates | ok (15): 0.3 to 0.8 s after the file changed on the server |
| H10 | the diff from the chat opens with local paths; a file link opens the local file | ok (25): the diff is titled `calu: /home/cua/work/sshproj/greeter.py`. The file link: test (`sshLocalPath`), not clicked |
| H11 | edit locally and save: it is on the server | ok (checked with `ssh`; also H19's offline edit) |
| H12 | edit locally WHILE the scripted turn has edited the same file | ok (16 to 18): "1 file held until the turn ends" with "Upload now", the server unchanged; a second change made on the server by hand meanwhile is not downloaded over the local one; when the turn ended both changes were merged on both sides, the editor shows the result, the chat says so |
| H13 | a conflicting change made on the server by hand (the same line, unsaved text in the editor) | ok (19 to 22): card "Merge greeter.py…", a notification with "Merge…", the status bar "conflict in greeter.py"; the IDE's three-way dialog (Yours / Result / Theirs); the applied result is on both sides with equal hashes |
| H14 | delete, rename, new file on the server | ok (26): all three arrive within the watcher's interval |
| H15 | delete, rename, new file locally | ok when made in the IDE (test: `localRenamed` is one `ide.fs.rename`); made outside the IDE (a shell): ok, but only after the IDE noticed the change, 10 to 40 s here |
| H16 | the directory of 300 files, the binary file | ok (hash equal on both sides); a 3 MiB file up in chunks and a 2.5 MiB file down in pieces: test |
| H17 | symbolic links: one inside the tree, one that points outside | ok: neither is created locally nor followed; "2 paths are not synchronised" with "Show" |
| H18 | a file above the size limit (35 MB on the server) | ok: not copied. open: it is listed as skipped only after the next full comparison (a reconnect or "Sync now"), because the bridge's watcher does not report files it would not list. Fixed (PLAN D237, d0fbaf2): `ide.fs.changed` carries `skipped`, the plugin lists the path at once (test; not walked through by hand) |
| H19 | the network gone for 20 s and `ssh` killed mid-turn | ok (27 to 29): status "calu sync: offline", an edit saved meanwhile: "offline: 1 change queued"; attempts with a growing wait; after the network was back the transcript continued with the step that had come up meanwhile (no doubled message), the queued file was uploaded, a full comparison ran (342 ms) |
| H20 | the server container stopped, then started | ok (32, 33): "connection lost" on the open card with Allow and Deny disabled, attempts with the reason; after the start the plugin connects by itself and the copy is in sync. open (core): the session that died with the container was still listed as running by `ide.sessions` (not marked stale for some minutes) -- since PLAN D237 (edc6c4d) stale after 60 s of silence, at once where the process table is the same and the pid gone; open (plugin): its waiting step still counts in "calu: 1 waiting" |
| H21 | the session list after the server stopped | fixed (0b1cab1): the list is asked again after every change of the session connection and every 15 s while no session is attached |
| H22 | two PyCharm projects on the same server project | - (not run this round). The path is the one of H14: the other copy's writes arrive as changes of another program |
| H23 | checker instructions, checker context, "Why?" | ok (30, 31): they go through the bridge unchanged |
| H24 | the Setup page and a side question over SSH | ok (46, 48): the findings are those of the server |
| H25 | the one-password rule over SSH (the store with passwords on the server, "calu command" = `/home/dev/pwstore/calu`) | ok (63 to 66): one card "Unlock", one dialog; a session starts; after the SSH connection was killed the session is attached again without a dialog. The synchronisation itself needs no password |
| H26 | "Sync: none" | ok (fourth round): with "Sync: none" and PyCharm's Deployment set to upload automatically to the SSH interpreter's server, no calu sync status and no warning appear; a local save of `README.md` is on the server through Deployment within 6 s; the diff of the agent's edit opens with the local path (`split1/127` to `129`). The agent's edits reach the copy only by a download the user makes |
| H27 | a bridge without the `fs` capability | test (`SynchronizerTest`): refused with what to do, no `ide.fs.*` request. No such bridge exists to run against |
| H28 | unsaved editor text and a remote change of other lines | test (merged; never uploaded before it is saved); H13 is the overlapping case by hand |
| H29 | a restart of the IDE | ok (58): the plugin connects by itself, compares (no question, the bases are stored) and is "in sync" |

Measurements (the box and the desktop are containers on one host): the first clone of the 307
files 1.3 s in the IDE after the confirmation (8 s before the VFS was refreshed once instead
of per file), 2.5 s in the test over SSH with its checks; an edit of the agent to the local
file 0.3 to 0.8 s; a change by another program on the server to the local file about 2.3 s
(the watcher polls every 2 s); a local save to the server about 0.2 s; a full comparison of
the unchanged tree 27 to 342 ms; CPU of the bridge's polling watcher on the tree 0.6 % of one
core (0.18 CPU seconds in 30 s, its `git` calls included), 72 MB resident.

Usability findings that are still open: the plan dialog lists at most eight paths per kind
and has no "show all"; a conflict in a file that is not open has no mark in the project view;
with ten subagents the tab chips take most of the height of a narrow tool window (a card can
be half hidden); the "Embedded Browser is suspended" notice of the IDE's own Markdown preview
appears in this container for every document opened from the chat.

### I. Remote operation: the terminal clients and the web interface on another machine

Run 2026-11-19 by an agent: the server was the second SSH box (`calu-sshbox2`, environment
"second server"), reached from the host over SSH on 127.0.0.1:56523 with the test key; on it
`uitest/tui_server.py` without `--insecure` (the store without passwords) and, for the cases
with passwords and `--insecure`, the store `/home/dev/pwstore` (admin password `admin-pw`).
The host ran calu from the checkout with its directories in a scratch directory and
`CALU_PLACE_FILE=/nonexistent` (no credential at all, except the `operator` of case I14). The
clients ran in `tmux -L calutest`, the browser was the browser layer's headless Chromium.
Latency and loss with `tc qdisc ... netem` inside the box (on its `eth0`, so on the SSH
traffic). Fixes are in c19c532 unless another commit is named.

What is remote by design: a session listens only on its machine's loopback interface and has
no TLS (CALU_GUIDE "From another machine"), so there is no direct TCP attach and no TLS case;
every remote client goes through SSH. Three ways were run: the client on the server (`ssh -t
host`, then `calu attach`), the server's `calu web` through one tunnel, and a client on the
host with the session's port forwarded and a token.

| # | Case | Status |
|---|---|---|
| I1 | finding what to forward: `calu sessions` and `calu token mint` on the server | fixed: neither showed the session's port (the guide said `calu sessions` did; only `calu token list` had it). `calu sessions` now ends each running session's line with `clients ws://127.0.0.1:<port>`; `calu token mint` prints the `ssh -N -L` and `calu attach` lines. Followed as printed from the host: ok |
| I2 | `calu attach ws://127.0.0.1:<forwarded port> --token=<minted>` (T): tour, a decision, a question decided with `calu resolve` on the server | ok: the card closes, "decided elsewhere: answered" |
| I3 | no token, a wrong token, a token in `CALU_TOKEN`, an address nobody listens at | fixed: "calu: CliError: cannot attach … invalid client token (code -32001)" with exit 1 and nothing else; a token had to stand on the command line. Now the address and token are checked first; on a terminal a refused or missing token is asked for ("Client token (Enter gives up)", three tries); `CALU_TOKEN` is read; the messages name `calu token mint` and, for a refused connection, the `ssh -L` forward. "CliError: " no longer stands in front of any command's error |
| I4 | the tunnel killed while attached (T and line client), then opened again | fixed: the client gave up at once ("detached: the session ended or the connection broke"), although the session ran on. Both clients now try again after 1, 2, 4, 8, then every 15 s for five minutes ("connection lost, try 3: nothing listens at the address (connection refused)"; the footer offers `^d leave`, not `enter send`) and continue after the last event; regression test `tests/unit/test_remote_attach.py` (a TCP relay that is cut and opened again) |
| I5 | 100 % packet loss for 25 s and for 50 s, then the network back | ok after I4: with `ServerAliveInterval` the `ssh` of the tunnel ends (open it again: the client attaches by itself); without it the WebSocket keepalive ends the connection after about 40 s, the client tries again and attaches through the surviving tunnel once the loss ends |
| I6 | latency: 300, 700 and 150 ms each way (RTT 0.3 to 1.4 s); `a a` on a card | fixed: nothing showed between the second press and the session's answer (up to 1.4 s). The card now says "allow sent: waiting for the session to confirm". Attach to the `many` session (520 items) at 0.3 s RTT: 3.7 s to "attached", including the token check and `uv run` |
| I7 | the server stopped (Ctrl-C of `calu serve`) with a client attached | fixed: the client showed the card "SESSION STOPPED … enter resumes the conversation" next to "detached", and Enter could resume nothing (under `calu serve` a stopped session has ended). The card is hidden while detached; the status says "detached: the session ended" (a close by the session is not retried) |
| I8 | `/new`, `/running` in a client given an address on a machine without calu | fixed: "/new" said "this machine has no 'admin' credential (calu credential set admin stores one …)", which leads a remote user to put the database owner's login on the laptop. Now it says that these commands work in a client on the session's machine (`ssh -t <host> calu attach`), and a detached client does not offer them |
| I9 | replayed decisions at attach | fixed: every decision made before the client attached came back as a toast "decided elsewhere: allowed (2 times)" |
| I10 | `calu attach` on the server over `ssh -t` (T at 120x36 and 80x24): `many-subagents`, `/running`; a decision made in the host's client closes the card there | ok |
| I11 | scenarios over the tunnel: `swarm` (ten subagents, held steps), `escalations`, `bidi`, `edge`, `many`; line client with `/state` and a message | ok: holds, `⟦RLO⟧` in the card's command, deny with a note (`n n`, `alt+d`) |
| I12 | a `calu serve --insecure` session through a tunnel without a token | ok as documented: the forwarded connection arrives on the server's loopback interface and is let in without a token; the guide now says that whoever can log in over SSH then attaches without one |
| I13 | the server's `calu web` through `ssh -L 8800:127.0.0.1:8800` (W): token login, fleet, live view, allow with a note, 390 px width; the tunnel killed for 8 s, a click on Allow meanwhile, the tunnel back | ok: "The connection to calu web ended; trying again in 8 s …", Allow says "not sent: this page is not attached to the session", the page attaches again by itself. The reconnect line now also names the SSH tunnel. Open (small): the "not sent" line replaces the reconnect line until the next attempt |
| I14 | `calu web` on the host against the server's database (forwarded PostgreSQL port, `calu credential set operator --no-password`, `calu web --database operator`): sign-in, fleet, escalations page, live view | ok for the sign-in (the admin password once at the terminal, once in the browser), the fleet, deciding on the escalations page (recorded as `admin:<user>@<host>`). The live view of a session of the other machine said "if that is another machine … forward the port": fixed (this commit), it now knows that the session's host is not this one and names `ssh -N -L <port>:127.0.0.1:<port> <host>`; with that forward, Try again attached and the live view worked |
| I15 | `calu web` of the server restarted while a page is open | not run remote; the local case (section D, C16) applies unchanged |
| I16 | the line client remote | ok for I2-I5 (`[reconnecting] connection lost: …; try 1 in 1 s (/quit or Ctrl-D leaves)`, `[attached again]`) |

Judgement calls for the owner (not decided here):

- A client given an address cannot follow `/new`, `/fork` or `/switch`: every session has its
  own port, its own forward and its own token. Possible: a `calu attach ssh://host[/dir]` that
  runs the client's half on the server, or a single relay port per `calu serve` (the web
  interface has one already).
- How long a broken connection is tried again (five minutes now) and whether a local session
  whose process died should end the client at once instead (a refused local connection looks
  the same as a dropped tunnel).
- `calu start --no-attach` leaves the token it minted valid until the session ends (it is
  listed as "never used").

Fourth round (2026-11-19, the owner's requirement 41: every conflict path resolved, `.git` on
one side only, remote run and debug). Screenshots `~/.cache/calu-uitest/share/split1/90` onward.

| # | Case | Status |
|---|---|---|
| H30 | change here against a deletion on the server, and the reverse | test (`SynchronizerTest`): a conflict that keeps the existing side; resolving keeps it or deletes on both |
| H31 | a rename on one side while the other edits the old file | test: the new name arrives, the edited old file is a conflict, nothing is lost |
| H32 | a new file of the same name on both sides | test: a conflict (no common version), both kept; a late notification does not overwrite |
| H33 | the same lines changed here while the agent's turn holds the file | test: held, then a conflict when the turn ends |
| H34 | unsaved editor text against a remote change of the same lines | test: a conflict, the typed text stays, nothing uploaded |
| H35 | a conflict in a file that is not open, by hand | ok (91): card, notification, status bar, and new (01fe208) the project view: the file in red with "sync conflict", the root "contains a sync conflict" |
| H36 | the IDE restarted with a conflict pending | ok (92) and test: the conflict, its marks and its merge base are back; the merge dialog resolves it (93, 94: both sides equal) |
| H37 | the conflict's notification after the conflict is resolved | fixed (8c91eda): it stayed open; it closes now (96) |
| H38 | `.git` in the local copy, `.git` on both sides | fixed (01fe208), test: never sent or fetched in either direction; the local `.git` is listed as not synchronised with a warning once per IDE start that git belongs on the server |
| H39 | PyCharm's SSH interpreter on the box with the mapping local copy → `/home/dev/project`, upload off: Run | ok (116): `/home/dev/project/run_demo.py` runs on host `sshbox` with the synchronised `greeter.py` |
| H40 | the same: Debug with a breakpoint, after an edit on the server that moved the lines | ok (117, 119): stops at `run_demo.py:7`, then at `greeter.py:5` after two lines were added on the server; the editor's line and the debugger's frame agree |
| H41 | PyCharm's Deployment uploading automatically into the synchronised directory | fixed (8b1d595): a "two writers" warning in the chat and as a notification (125); none when upload is off or goes elsewhere (`DeploymentTest`) |

Usability findings of this round (not changed): with no session the composer has no
placeholder that says what to do; when the tool window is short the stacked setup cards run
over the composer and the footer (`split1/76`, "effort: default" drawn over the tab chips in
`29`); the chat keeps the line "greeter.py was changed here and on sshbox … choose Merge" after
the conflict is resolved (it is a log line of the transcript); in the plugin settings page the
SSH fields stay editable when the mode is "Automatic".

