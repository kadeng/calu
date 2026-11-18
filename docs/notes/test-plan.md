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

No test touches the owner's database (`calu-db-postgres-1`), his `~/.config/calu`, `/etc/calu`
or `/var/lib/calu`. A database for manual tests is a container of its own, not the test
suite's server (its roles are server-wide, and a manual setup there breaks running tests).

## The cases every chat front end must pass

(VSCode extension = V, PyCharm plugin = P, full-screen client = T, web live view = W. A front
end that lacks a function by design says so in its own section.)

| # | Case | V | P | T | W |
|---|---|---|---|---|---|
| C1 | A turn with streamed text, thinking, a read, an edit, a command; the checker's decision and reason on each | - | - | - | fixed 706b3b8 |
| C2 | A denied step with its reason; an escalated step: allow, deny with a note, "why?" | - | - | - | fixed 706b3b8 |
| C3 | A question of the agent: one choice, several choices, free text | - | - | - | fixed 706b3b8 |
| C4 | A subagent: its own view, task and result; a background subagent that outlives the turn | - | - | - | fixed 706b3b8 |
| C5 | **Many subagents at once** (8+): navigation, overflow, which one needs attention | - | - | - | fixed 706b3b8 |
| C6 | **An escalation in one subagent while others keep working, then a second escalation elsewhere**: both visible, each decided on its own, the others not held longer than calu holds them | - | - | - | fixed 706b3b8 |
| C7 | Three or more open decisions at once; deciding out of order; a decision made in another client closes it here | - | - | - | fixed 706b3b8 |
| C8 | Stop during text, during a command, with a decision open | - | - | - | fixed 706b3b8 |
| C9 | A message for the next turn and one into the running turn; two queued; while a decision is open | - | - | - | ok |
| C10 | Supervision level to "everything waits" and back; changed from elsewhere | - | - | - | fixed 706b3b8 |
| C11 | **A long session** (500+ items, 50+ turns): speed, scrolling, jump to latest, memory | - | - | - | fixed 706b3b8 |
| C12 | **Many sessions** (10+ running, 30+ stored): the list, switching, names, finding one | - | - | - | fixed 706b3b8 |
| C13 | **Undo** (rewind to before a message) and **fork**: first, middle, last message; fork of a fork; what the new session shows and knows | - | - | - | ok (fork); undo: by design a fork |
| C14 | Context usage and limits; before the first turn; when unknown | - | - | - | fixed 706b3b8 |
| C15 | Images in a message (where supported): accepted, refused over the limit, shown | - | - | - | ok |
| C16 | Losing the session, the server, the database mid-turn; coming back | - | - | - | fixed 706b3b8 |
| C17 | Narrow and very wide layouts; light and dark; long lines, huge results, long names | - | - | - | fixed 706b3b8 |
| C18 | Text meant to mislead: control characters, raw HTML, links with scripts | - | - | - | fixed 706b3b8 |
| C19 | Checker instructions: read, change, save, a conflict with another editor | - | - | - | fixed 706b3b8 |
| C20 | The checker context, and the context of one escalation | - | - | - | ok |
| C21 | Honesty mode: switch, marks, review, trust | - | - | - | ok |
| C22 | Settings: every kind of value, a refused value, one model for all roles, reset | - | - | - | fixed 706b3b8 |
| C23 | First start: nothing installed, no server, no session, a locked credential (one password, once) | - | - | - | ok (no server, no session); rest n/a here |

## Sections (each owner extends its own)

### A. VSCode extension
First and second run: see the reports summarised in `HANDOFF.md`; screenshots in
`docs/notes/screenshots/run1-*.png`. To be transferred into the table above by the next
round, which also runs C5, C6, C11 and C12 at scale.

### B. PyCharm plugin
Not run in an IDE yet.

### C. Full-screen client and line client (`calu attach`)

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

### F. Session traces
