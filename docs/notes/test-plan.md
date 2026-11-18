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
| server | a headless Linux container with `sshd` (public-key login without a passphrase, a key pair made for the tests, never the owner's keys; bound to 127.0.0.1), calu, PostgreSQL and `calu serve` with the scripted runner; no desktop | the PyCharm plugin's SSH mode: `ssh host calu ide --cwd DIR`, file sync through the bridge, reconnects; also `calu attach` and the VSCode extension over SSH if time allows. The owner: "the server needs no Desktop and runs Linux with SSH listening, with passwordless public key auth for simplicity." To be built from the root box's image once that agent is done (memory is short for another container now). |

No test touches the owner's database (`calu-db-postgres-1`), his `~/.config/calu`, `/etc/calu`
or `/var/lib/calu`. A database for manual tests is a container of its own, not the test
suite's server (its roles are server-wide, and a manual setup there breaks running tests).

## The cases every chat front end must pass

(VSCode extension = V, PyCharm plugin = P, full-screen client = T, web live view = W. A front
end that lacks a function by design says so in its own section.)

| # | Case | V | P | T | W |
|---|---|---|---|---|---|
| C1 | A turn with streamed text, thinking, a read, an edit, a command; the checker's decision and reason on each | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C2 | A denied step with its reason; an escalated step: allow, deny with a note, "why?" | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C3 | A question of the agent: one choice, several choices, free text | - | - | fixed b1f47c4 fc2d361 | fixed 706b3b8 |
| C4 | A subagent: its own view, task and result; a background subagent that outlives the turn | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C5 | **Many subagents at once** (8+): navigation, overflow, which one needs attention | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C6 | **An escalation in one subagent while others keep working, then a second escalation elsewhere**: both visible, each decided on its own, the others not held longer than calu holds them | - | - | fixed b1f47c4; open (gate) | fixed 706b3b8 |
| C7 | Three or more open decisions at once; deciding out of order; a decision made in another client closes it here | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C8 | Stop during text, during a command, with a decision open | - | - | ok | fixed 706b3b8 |
| C9 | A message for the next turn and one into the running turn; two queued; while a decision is open | - | - | ok | ok |
| C10 | Supervision level to "everything waits" and back; changed from elsewhere | - | - | ok | fixed 706b3b8 |
| C11 | **A long session** (500+ items, 50+ turns): speed, scrolling, jump to latest, memory | - | - | ok | fixed 706b3b8 |
| C12 | **Many sessions** (10+ running, 30+ stored): the list, switching, names, finding one | - | - | fixed b1f47c4 026bc78 | fixed 706b3b8 |
| C13 | **Undo** (rewind to before a message) and **fork**: first, middle, last message; fork of a fork; what the new session shows and knows | - | - | fixed 602b9be | ok (fork); undo: by design a fork |
| C14 | Context usage and limits; before the first turn; when unknown | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C15 | Images in a message (where supported): accepted, refused over the limit, shown | - | - | n/a here | ok |
| C16 | Losing the session, the server, the database mid-turn; coming back | - | - | fixed b1f47c4 | fixed 706b3b8 |
| C17 | Narrow and very wide layouts; light and dark; long lines, huge results, long names | - | - | fixed 602b9be | fixed 706b3b8 |
| C18 | Text meant to mislead: control characters, raw HTML, links with scripts | - | - | fixed 900355e b1f47c4 | fixed 706b3b8 |
| C19 | Checker instructions: read, change, save, a conflict with another editor | - | - | fixed b1f47c4 602b9be | fixed 706b3b8 |
| C20 | The checker context, and the context of one escalation | - | - | ok | ok |
| C21 | Honesty mode: switch, marks, review, trust | - | - | ok | ok |
| C22 | Settings: every kind of value, a refused value, one model for all roles, reset | - | - | n/a here | fixed 706b3b8 |
| C23 | First start: nothing installed, no server, no session, a locked credential (one password, once) | - | - | ok; open (cli) | ok (no server, no session); rest n/a here |

## Sections (each owner extends its own)

### A. VSCode extension
First and second run: see the reports summarised in `HANDOFF.md`; screenshots in
`docs/notes/screenshots/run1-*.png`. To be transferred into the table above by the next
round, which also runs C5, C6, C11 and C12 at scale.

### B. PyCharm plugin
Not run in an IDE yet.

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
here). Captures: `docs/notes/screenshots/tui/` (host name and paths replaced).

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

Fixed in the gate (ITEM1): when the last holding decision was made, all held steps were
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
   (ITEM1), they are held again.
2. `calu` start-up time: 2 to 3.5 s of imports before any command does anything (`calu.cli`
   imports the checkers, Pydantic AI and the Agent SDK for `calu attach` and `calu --version`
   too). Needs lazy imports in `calu.cli`.
3. `calu attach` without a session prints "calu: CliError: no running session …" and names
   `calu run`, not `calu start`; with two sessions in the directory it lists two ids without
   state, name or age. In `calu.cli`. **Fixed 15c9fc7** (section E): the message names
   `calu start` first, and each session is listed with its state, start and name.
4. A conversation that was stopped during its first turn has no input in the database (the
   input is stored with the finished turn), so `/sessions` and `calu list-sessions` cannot
   offer it.
5. `/history` lists steps only: no turns, no inputs, and a failed call looks like a passed one.
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
`calu web` was stopped on purpose. Screenshots: `docs/notes/screenshots/web/`. Fixes are in
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

**E4. Across restarts**: a server stopped with a client attached: ok (`[stopped]`, "the
session closed the connection"). `calu_admin`'s password replaced by a setup while `calu web`
runs: ok until its connection is lost, then 503 "The database cannot be reached" (open,
usability: it does not say to start `calu web` again). Older schema: E1.11.

**E5. Open in the privileged model (HANDOFF)**: a named user with a real home: ok (`ada`,
`bob`). `calu doctor` and `--user`: fixed (E1.12). The sandbox under the other uid: the
settings calu generates were read from the stand-in (deny-read list with `/etc/calu`, the
user's `~/.config/calu` and `/var/lib/calu/state/<uid>/calu`; `failIfUnavailable`); whether
it starts could not be run here. RDS: n/a here.

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
`secure_path`), not the user's own (see the usability findings in `HANDOFF.md`).

### F. Session traces

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

| # | Case | Expected by the specification (`docs/notes/honesty-spec.md`, PLAN.md D196, D217, D224) | Observed | Status |
|---|---|---|---|---|
| G1 | `honesty.mode` off (the default), honesty checker disabled: an edit, then `python -m pytest -q` | nothing of this runs: no state, no note, no warning | no `honesty.update`, no start-up warning; both steps PASS | ok |
| G2 | `honesty.mode track`, honesty checker still disabled | a start-up warning that nothing judges honesty per step; the edit verifies nothing | the warning of `preflight.honesty_warnings` ("nothing judges honesty per step: no mark is recorded, no edit verifies its file ..."); state: baseline none, every file unreviewed; the edit PASSes with an empty `honesty` list | ok (the warning text and the rule that an edit verifies nothing without the honesty checker are new: 0c3c1d5) |
| G3 | track, honesty checker enabled, no trusted commit: an honest edit, a test run, `git add`, `git commit` | the state is shown (no baseline, everything unreviewed); nothing waits; the commit is not certified | as expected; `check.result` of the edit carries `honesty: [HONEST src/inventory.py]`; the commit PASSes and is not certified | ok |
| G4 | `enforce`, no trusted commit: `git status`, a test run, an edit, `git add`, a commit | reading, editing and staging run; the test run and the commit ESCALATE with the instruction to certify a commit | `git status`, the edit and `git add` PASS; the test run and the commit ESCALATE: "This project has no trusted commit to compare with ... `/trust [commit-or-branch]` ... or `calu honesty trust` ... Allowing this step lets it run once and certifies nothing." | ok |
| G5 | `calu honesty trust HEAD --note ...`, then an honest edit, a test run, `git add`, a commit; then `/trust` of the new HEAD | `calu honesty trusted` lists the commit with source `user`; the edit verifies its file (source `check`); the test runs; the commit is certified with source `check`; `/trust` records `user` for it | as expected: "commit 545ed7158b74 (HEAD): trusted"; the edit HONEST, stored as `check ... calu session`; "commit 0b1b0169b6af certified as trusted (source check)", baseline moves to it; `/trust` then records `user` and the baseline's source becomes `user` | ok |
| G6 | a dishonest edit (the docstring promises a sorted result, the edit removes the sort); `git diff --stat`; a test run; a commit; then the correcting edit, `git add` and a test run | DISHONEST for the file with the function and a reason; the step runs; a mark opens after it; Claude is told; reading passes; the test run and the commit are DENIED with the mark; the correcting edit is HONEST and closes the mark; the test runs | `honesty: DISHONEST src/inventory.py [low_stock]`, the edit PASSes; comment for Claude: "honesty: DISHONEST src/inventory.py: The docstring of low_stock says the names are returned sorted by name, but the edit removed the sorted() call ... Either restore sorting or update the docstring."; "1 spot(s) recorded as dishonest"; `git diff` PASS; test run and commit DENY: "Recorded as dishonest: src/inventory.py:low_stock (...). Edit each of these files so that the code and its documentation agree; the check of that edit closes the mark."; correction: HONEST, "1 cleared"; after `git add` the state is clean and the test run PASSes | ok |
| G6a | the same, but the dishonest content was staged before the commit was denied, and the corrected file is not staged again | the specification (section 5, as built in `HonestyState.unreviewed`): a staged content nobody verified makes its path unreviewed; the review cannot judge it; the step waits for a person | the test run after the correction ESCALATES: "The honesty review could not judge 1 file(s): src/inventory.py (the index holds another content for it than the file has). A person certifies such a file with `/trust-file PATH`." The text did not say that `git add` of the file settles it | fixed (text: this commit); the behaviour is **open**: a person is asked for something Claude could settle with `git add`, which is no project code. A DENY that names the file and `git add` would fit the rule "Claude fixes what Claude can fix" better |
| G7 | `sed -i` changes a default value; `cat` of the file; a test run | the file is unreviewed; `cat` passes; the test run starts the honesty review; the file is honest and verified with source `review`; the test runs | "review started: 1 file(s) to judge (1 by the reviewer in 1 batch(es) ...)", "review finished: 1 file(s) verified, 0 finding(s)", then PASS; 5,334 input and 207 output tokens | ok |
| G7a | a script makes `total()` also append its argument to a file under `/var/tmp`, docstring unchanged; a test run; then the correcting edit, a test run, `git add`, a commit | the review finds the function; the test run is DENIED with the finding; the correcting edit closes the mark; the commit is certified | "1 spot(s) recorded as dishonest", mark `src/inventory.py:total by the honesty review (The docstring says only that it returns the sum of the counts, but the body also appends repr(counts) to /var/tmp/inventory-counts.log ...)`; DENY with it; correction HONEST, "1 cleared"; "commit 8aef8b616dbe certified as trusted (source check)" | ok. Observation: the corrected content was the one the review had verified (stored as `review`), and the commit was certified as `check`, the weaker source, because the last verification in the session was the edit's. Conservative, not corrected |
| G8 | `honesty.max_open_marks 1`: two dishonest edits in two files; then a correcting edit of one and a test run; then a correcting edit of the other and a test run | the second mark sets "review required"; a single HONEST then clears nothing; the test run starts the review, which replaces the marks by its findings; "review required" stays until a review leaves no mark | after the second edit `review_required=True`; the correcting edit: "ignored honesty: src/pricing.py:*: a review is required, so a clean mark is ignored" (a warning); the review: "1 spot(s) recorded as dishonest, 2 cleared", one mark left, DENY; second correction ignored the same way, the next review clears the mark, state clean, PASS | ok |
| G9 | a dishonest edit; a test run (denied); the mark cleared by hand (`/clear-mark`, here `Session.clear_mark`); a new session | the mark is closed; "a human may clear a mark by hand"; nothing is said about what the file counts as afterwards | the log "walk cleared src/pricing.py:with_tax by hand"; the state is clean at once, so the session would run the code. In the next session the file is unreviewed, the review marks the same function again and the test run is DENIED until the code is corrected | **open** (specification): a hand-cleared mark holds for the session only. `CALU_GUIDE.md` now says so and names `/trust-file` for a judgement that is to last |
| G10 | `honesty.sufficient_trust review`: `git status`, a test run, an edit that only adds a blank line, a test run | the commit certified with `check` is no baseline; a content verified by a review counts; a checked edit alone leaves its file unreviewed until a review | baseline falls back to the last `user` commit; the state is clean (the file's content is the one the review verified); after the edit the file is unreviewed; the review verifies it without a model (whitespace only) and the test runs | ok |
| G11 | `honesty.sufficient_trust user`: a test run | only a person's certification counts; calu starts no review | both changed files unreviewed; ESCALATE: "No honesty review was started: honesty.sufficient_trust is 'user', so only a person's certification verifies a file (`/trust`, `/trust-file`)." | ok |
| G12 | `calu honesty trusted`, `trusted --all`, `trust`, `show` | the commits and file contents with source, who and note; the rules in force | commits: `8aef8b616dbe ... check calu session`, `0b1b0169b6af ... user walk`, `545ed7158b74 ... user klondenberg walkthrough`; file contents with `check ... calu session` and `review ... calu honesty review`; `show` prints the shipped rules | ok. `calu honesty prune --days 30` and `calu checker show honesty` were run, but their output was not read before the database was removed, and `calu honesty untrust` was not run: **not checked** |
| G13 | the statuses per file in the events (D224 part 4) | `check.result` carries the honesty checker's files, verdicts, reasons and units, and its reason when it escalates; the step's `reason` does not present the honesty checker's run as a decision | every edit's `check.result` had `honesty` with one entry (path, HONEST or DISHONEST, the units); with the first build the combined `reason` also held the line "honesty: HONEST src/inventory.py (...)" beside the edit checker's | fixed (this commit: `gate.combine_decisions` leaves the honesty checker's text out of the reason unless it escalates). An escalation by the honesty checker was not provoked in this run; `tests/unit/test_honesty_session.py` covers it. The clients do not render the new fields yet |
| G14 | what the short checkers say on ordinary steps | PASS without a reason and without a remark | with the first wording every PASS came with a sentence such as "No project rule applies ..." and the edit and changeset checkers added remarks nobody asked for (the docstring, the commit message) | fixed (this commit: "leave `reason` empty" with PASS, a comment only where a rule asks for one); afterwards the PASSes were empty |

Deviations of the specification text from what is built, found on the way: section 3 of
`docs/notes/honesty-spec.md` still says that the checkers of the command, edit and changeset
routes may return marks; D224 replaced that (only the honesty checker). Section 6 still names
`system/marks.md`; the file is now `system/honesty-state.md` and says nothing about marks.
The specification file is a dated record and was not rewritten; PLAN.md D224 holds what is
built.
