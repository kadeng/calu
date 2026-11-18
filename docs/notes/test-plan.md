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
| C1 | A turn with streamed text, thinking, a read, an edit, a command; the checker's decision and reason on each | - | - | - | - |
| C2 | A denied step with its reason; an escalated step: allow, deny with a note, "why?" | - | - | - | - |
| C3 | A question of the agent: one choice, several choices, free text | - | - | - | - |
| C4 | A subagent: its own view, task and result; a background subagent that outlives the turn | - | - | - | - |
| C5 | **Many subagents at once** (8+): navigation, overflow, which one needs attention | - | - | - | - |
| C6 | **An escalation in one subagent while others keep working, then a second escalation elsewhere**: both visible, each decided on its own, the others not held longer than calu holds them | - | - | - | - |
| C7 | Three or more open decisions at once; deciding out of order; a decision made in another client closes it here | - | - | - | - |
| C8 | Stop during text, during a command, with a decision open | - | - | - | - |
| C9 | A message for the next turn and one into the running turn; two queued; while a decision is open | - | - | - | - |
| C10 | Supervision level to "everything waits" and back; changed from elsewhere | - | - | - | - |
| C11 | **A long session** (500+ items, 50+ turns): speed, scrolling, jump to latest, memory | - | - | - | - |
| C12 | **Many sessions** (10+ running, 30+ stored): the list, switching, names, finding one | - | - | - | - |
| C13 | **Undo** (rewind to before a message) and **fork**: first, middle, last message; fork of a fork; what the new session shows and knows | - | - | - | - |
| C14 | Context usage and limits; before the first turn; when unknown | - | - | - | - |
| C15 | Images in a message (where supported): accepted, refused over the limit, shown | - | - | - | - |
| C16 | Losing the session, the server, the database mid-turn; coming back | - | - | - | - |
| C17 | Narrow and very wide layouts; light and dark; long lines, huge results, long names | - | - | - | - |
| C18 | Text meant to mislead: control characters, raw HTML, links with scripts | - | - | - | - |
| C19 | Checker instructions: read, change, save, a conflict with another editor | - | - | - | - |
| C20 | The checker context, and the context of one escalation | - | - | - | - |
| C21 | Honesty mode: switch, marks, review, trust | - | - | - | - |
| C22 | Settings: every kind of value, a refused value, one model for all roles, reset | - | - | - | - |
| C23 | First start: nothing installed, no server, no session, a locked credential (one password, once) | - | - | - | - |

## Sections (each owner extends its own)

### A. VSCode extension
First and second run: see the reports summarised in `HANDOFF.md`; screenshots in
`docs/notes/screenshots/run1-*.png`. To be transferred into the table above by the next
round, which also runs C5, C6, C11 and C12 at scale.

### B. PyCharm plugin
Not run in an IDE yet.

### C. Full-screen client and line client (`calu attach`)

### D. Web interface (`calu web`)

### E. `calu serve`, `calu start`, `calu run`: with and without passwords, as a user and as root

### F. Session traces
