# Handoff

State on 2026-11-19, in a session that began on 2026-11-18 and is still going. Read this, then `AGENTS.md`, `REQUIREMENTS.md`
(requirements 1-40; 26-40 are from the last days) and `PLAN.md` (decisions D1-D253, open
questions §11, phases §12). **Not everything is pushed**: see "State of the repository".

## Pick up here

### State of the repository (updated 2026-11-19, about 03:00, by the lead's stand-in)

**Read first:** `PLAN.md` D233 (what the test campaign changed and the decisions that wait
for the owner) and D234 (server and setup side); `agent/records/notes/test-plan.md` (the record of
the cases, sections A-H and the table C1-C23); the skill
`.claude/skills/ui-testing-with-cua/SKILL.md` (how the IDE tests are run). The owner's words
are in `REQUIREMENTS.md` 34-40.

#### CI and what is on GitHub (updated 2026-11-19, about 04:00, by the lead)

- **Everything is pushed.** `origin/main` is `7aed9c0` (or later); no agent is running and
  no agent has uncommitted work. CI was last seen green on `fdf1e89` (run 44433852933, all
  four jobs); the commits after it touch only `extensions/jetbrains/`, `uitest/` and
  documents (no Python), passed the repo-wide `ruff format --check . && ruff check .`
  locally, and their Gradle build (`./gradlew check buildPlugin verifyPlugin`) passed on the
  agent's side. Check `gh run list --limit 3` for the run of the tip.
- Done since the handoff of 02:30: the seven core defects (PLAN D235; held steps, which
  database the settings commands change, start-up in about 0.45 s, trace files, the
  revoked-token race, honesty after a denied commit and a cleared mark, migration 0037 with
  who decided in `calu resolve`); the honesty checker's per-file result in all five clients;
  an honest-code review of the code since 2026-11-17 with three real defects fixed
  (`calu login` passed checker keys to Claude Code; the trace writer could create
  directories outside the project; a dropped token revocation in `calu ide`); the PyCharm
  plugin's SSH mode (PLAN D236; test plan section H).
- **The owner accepted the "JetBrains Team Tools User Agreement"** of the JetBrains Client
  in the desktop (2026-11-19). Remote development of the PyCharm plugin can now be tested:
  `$D shell 'setsid -f bash /workspace/calu/uitest/pycharm-split.sh >/tmp/pycharm-split.log 2>&1'`
  (close the ordinary PyCharm first; `--stop` ends it). It was started once more at about
  03:55 and may still be running in the desktop (tmux session `calu-split`).

#### The owner's finish line (2026-11-19) and who is running

The owner's goal: every workflow and user interface test-driven in Cua and refined into a
well-rounded, tested, robust and convenient application with a modern UI. The terminal
client, the web interface, the VSCode extension and the PyCharm plugin are each tested
locally and remotely. The PyCharm plugin supports JetBrains remote development (split mode)
and also a local IDE that syncs files over SSH, with remote run and debug through
PyCharm's SSH interpreter.

The three agents of 07:00 have finished and their work is pushed: the PyCharm plugin's
three modes (PLAN D236 and the owner's REQUIREMENTS 41 addendum: local; JetBrains remote
development with the backend on the SSH box; a local copy synchronised with calu on the
server, `.git` on the server only, conflicts always resolved, run and debug through the SSH
interpreter), test plan sections B and H; the core defects (PLAN D237); the terminal clients
and the web interface used remotely through SSH (test plan section I).

**The system of records moved to `agent/records/`** (PLAN D238, about 11:00): done, with the
traces' default `agent/traces/`; the records keep their files and structure, only placed out
of the users' view (the owner, 2026-11-19). **Still to do, by the lead while no agent runs:**
rename `DOCUMENTATION.md` to `DOCUMENTATION.md` (the owner, same day) with `git mv` and update
every reference (16 files: README, AGENTS.md, docs/, the records, `src/calu/config.py`,
`src/calu/checkers/result_checker.py`, `src/calu/defaults/checkers/README.md`, the
extensions' `serverCommand.ts` and `ServerCommand.kt`, tests), anchors unchanged.

The VSCode round is done and pushed (test plan section A, R1-R9; VSCode now shows `step.held`
as well, PLAN D237's "VSCode ignores it" is outdated). `CALU_GUIDE.md` is now
`DOCUMENTATION.md` (pushed, `da0c883`).

#### The owner's answers of 2026-11-19 (afternoon) and the work they call for

Decided (do not ask again):
- **Auto mode:** Claude Code uses auto mode by default; if it is off, that is the user's
  choice. If a session launched through the Agent SDK does not get auto mode by default,
  calu makes it the default (plain `claude` has it). No warning at every start when the
  user turned it off. **Done** (PLAN D241, not pushed): measured, an SDK start gets
  "default"; calu now passes "auto" unless a settings file or `claude.permission_mode`
  chooses; the start-up warnings are gone, `calu doctor` states the mode.
- **Hold scope:** keep holding every checked step of every agent while a decision is open.
- **Traces:** include Claude's thinking in the plain trace, redacted. Thinking is only a
  summary, usually only on the last step, and rewritten while it streams: collect it when
  it happens and keep it. Redaction stays per block; no export of past sessions. **Done**
  (PLAN D241, not pushed): one block per thinking block, written once in its final form.
- **Remote attach:** one relay port per `calu serve` for all its sessions, so one SSH forward
  covers every session and `/new`, `/fork`, `/switch` work from a laptop.
- **Reconnect:** keep 5 minutes for remote addresses; a local session whose process is gone
  ends the client at once.
  Both built (PLAN D242, test plan I17-I30, not pushed): `calu serve` listens on one relay
  port (default derived from its projects directory since D243) for all its sessions with a server token (`calu token mint --server`,
  migration 0038); `calu attach ws://127.0.0.1:8811` follows `/new`, `/fork`, `/switch`;
  `calu start --relay`; `calu web` falls back to the relay for the live view.
- **Projects directory per `calu serve`** (requirement 43) and **relay port from the
  directory:** **Done** (PLAN D243, test plan I31-I37, migration 0039, not pushed).
  `calu serve [--projects DIR]` (default: the current directory) refuses `/`, hidden
  directories and system directories before the password; every request it claims
  (`calu start`, web, `/new`, relay) must be in that directory or below; two servers of one
  user each get the requests of their directory; the relay port is 8811 + sha256(path) mod
  189 (next free one if taken). Not run by hand over SSH yet (`docs/TESTING.md` item 39).
- **A dead `calu serve` still counted as serving its directory** (D243's known limit) and the
  full-screen client's `/running` did not name the servers' projects directories: **Done**
  (PLAN D244, test plan I38-I41, migration 0040, not pushed). Servers beat every 10 s like
  sessions; stale after 60 s, and at once when another server of the same user and process
  table finds the pid gone (it ends that row). Claims, server tokens, `calu token mint
  --server`, `calu web`'s relay fallback and `ide.status` ignore such servers. `/running`
  lists "calu serve of user@host serves DIR"; through a relay it shows session names and the
  time in the current state (`relay.sessions` gained `name`, `state_since`). The owner's
  machine needs `sudo calu setup` again for migration 0040. Follow-up (not pushed): `calu
  token list` shows such a server's unrevoked tokens as `server silent for N min` or
  `server gone` instead of `valid`, the served session's own token as `relay of server
  <id>`, and the clients (line client, full-screen client, web live view) say that the
  relay closed the connection on close code 4003, not the session.
- **Tokens:** `calu start --no-attach`'s token stays valid until the session ends (keep).
- **PyCharm:** placeholder in the message field when nothing is attached and the setup cards
  in a scrolling area; a resolved conflict's line is marked "resolved" (warning colour
  removed); offer to create the SSH interpreter and path mapping in mode 3 (after the user
  confirms the Python path on the server); SSH fields disabled unless mode 3 is chosen; a
  save during Claude's edit of the same file stays held until the turn ends; the bridge
  starts at project open; the id `io.github.kadeng.calu` is right; the password over the
  IDE's link in remote development is acceptable.
- **VSCode:** after the server stops, "The calu server stopped" with "Start server" and
  "Retry" instead of "connection lost"/"Attach again", the old transcript greyed with
  "session ended"; the waiting timer counts from when the step started waiting (from the
  session, survives reattaching); running subagents keep the pulsing dot only. **Done**
  (not pushed; test plan A13-A17): `ide.link lost` carries a `cause`, `ide.status` counts
  `servers_running`; the extension shows "The calu server stopped" (Start server, Retry),
  greys the ended transcript and notices a server that runs again; the timer counts from
  `opened_at`. Not run over Remote-SSH. The PyCharm plugin does not use `cause` yet.
- **Web:** Enter in the triage note stays a new line.
- **After the final acceptance round (test plan J), the owner's answers:**
  - A click on the context/token chip in every front end opens one panel: calu's stored
    subscription limits (5-hour, weekly, with when they were reported, or "not reported yet
    by Claude Code") plus the output of `/usage` run as a zero-cost request. (`/usage` in
    headless mode shows cost and tokens only; the limits come from `rate_limit_event`.)
  - A small backward clock jump after a sleep stays logged as an error (keep as is).
  - The web live view gets side questions (`side.ask`), answers shown only to that browser.
  - The full-screen client passes every slash command it does not know to Claude Code;
    the client's own `/context` is renamed `/checker-context` (the owner, later), so
    `/context` reaches Claude Code as in the IDEs; no double-slash form.
  - Checker instructions: `calu setup`, `calu doctor` and the instruction editors say when the
    stored text is an older shipped version unchanged by the user and offer to update it; a
    text the user edited is only reported as differing, with a diff, never replaced.
  - The web's "Recorded turns" leaves out a turn that has not ended.
  - The terminal client following a restarted session with a new token is intended.
  - Session lists show the session user's home as `~` (another user's as `~user`), the full
    path in a tooltip or detail view.
  - **Built** (PLAN D248, not pushed; core, both terminal clients, web): `usage.panel` (the
    `/usage` text through the control request `get_session_cost`, no turn), web side
    questions, unknown slash words to Claude Code with `/checker-context`, outdated shipped
    instructions (`shipped.json`, `calu checker diff|update`), the web's recorded turns,
    `~` paths, the small points of J O5. Open: the editor plugins; the manual check
    `docs/TESTING.md` item 44 with a real `claude` (the panel on a subscription account).
- **The owner's answers of 2026-11-20 (to build):**
  - The review-findings rules (weigh against complexity; security usually heavy, not
    absolute; the user outranks a subagent's verdict) stay a documentation example and go
    into a new library of example/template checker prompts; later the UI may load one.
  - `calu setup` without a terminal updates outdated shipped instructions only with
    `--update-instructions`; it prints which texts are outdated.
  - The last reported usage limits are stored in the database per Claude account (migration);
    the panel shows them with their age. The panel also asks `get_usage` and falls back to the
    stored figures when it fails or its shape is unknown. Rate-limit messages are information;
    Anthropic's servers enforce the limits.
  - The line client keeps passing every unknown `/word` to Claude Code.
  - `~` for remote sessions: the `/home/<user>` guess is fine.
  - A mode switch that lands while a step is checked: Calu answers by the mode Claude Code is
    in when the answer goes out.
  - The bridge keeps the name `calu ide` for both editors in the record.
  - No "started in auto but runs in …" warning after a switch the user made.
  - Every client sends the edit id or git reference of the diff a comment was made on (the
    full-screen client and PyCharm too).
  - PyCharm mode 3: starting a run or debug while a file is held asks "Upload now and run",
    "Run anyway" or "Cancel".
  - PyCharm sync dialog "changed on the server, deleted here": no default button.
  - **No stored usage limits** (the owner, later on 2026-11-20: "keep it simple"; replaces
    his earlier CRC32 key ruling). The usage panel asks Claude Code on demand (`get_usage`,
    with the message when it fails) and otherwise shows the limits this session process
    received, with `reported_at`. No account identity is read, stored or shown. Migration
    0042 drops what 0041 created; `stored`, `account` and the storing code are gone,
    `account_limits_problem` stays.
  - Human diffs (D253), the owner's answers: recorded only while an IDE is attached to a
    session; one block per save (VSCode's auto-save is off by default, backups are no saves);
    no list/show command for now; a save that overwrites a version Claude wrote meanwhile is
    diffed against the version the user's buffer started from, so only the user's own edits
    appear (the IDEs already ask before such an overwrite; mode 3 merges). Built 2026-11-20:
    both IDEs keep that base per document (`SaveBases`, PLAN D253), with vitest and Kotlin
    tests for a normal save, an overwrite of Claude's version and a reload of it; not yet
    checked by hand in the IDEs.
  - Checker instructions stay global for now; per-project instructions (a global default with
    per-project overrides, perhaps merged) come later, after everything works.
  - **Built in the core, the CLI and the terminal clients** (PLAN D250, 2026-11-20, not
    pushed by this agent): the template library (`src/calu/defaults/templates/`, `calu checker
    template list|show|use`, public domain); `calu setup` without a terminal keeps outdated
    shipped texts and lists them (`--update-instructions` replaces); the usage panel says
    when `get_usage` failed or answered in an unknown shape (`account_limits_problem`; the
    stored per-account limits of migration 0041 were removed again, see above); the hook
    answers by the mode at the answer; no auto-mode warning after a user's switch; the
    full-screen client's diff comments carry `ref`. Open: the IDE panels do not show
    `account_limits_problem` yet; no `--project` for `template use` (checker instructions
    are global); manual check `docs/TESTING.md` item 45; the owner's machine needs `sudo
    calu setup` for migration 0042 (it drops the table of 0041).
- **Traces (later the same day):** a removed message keeps its turn heading by default,
  unless the redaction instructions say otherwise ("It's not important"); the redaction
  instructions are edited in the IDEs' settings dialogs only, no extra picker entry;
  the final redaction pass before a commit covers only what was added since the last commit
  (requirement 45, built: PLAN D246, below).
- **Sandbox (D239):** a root setup counts as more privileged; `calu doctor` without `--db`
  stays silent about the sandbox. Both as built.

#### A root client asks the admin password once (2026-11-19, PLAN D249)

The owner's decision: `calu attach --sudo` and `calu start --sudo` ask the admin password
once per client run, and the design follows least privilege. Built (not pushed): the root
client keeps `calu_admin` with the typed password in memory (the owner login only after a
blank admin password), asks before the full-screen client starts when it found its session
through a root socket (Ctrl-D skips), and passes the complete login to the client, so no
dialog follows, also after `/switch`. Every client action narrows its connection with
`SET ROLE` (`calu.db.roles`): sessions, requests and tokens as `calu_operator`, settings as
`calu_admin`. D249 (4) maps the roles to the actions and recommends per-person roles with RLS
first, for later. Root box: 24 passed (the serve test needed `--projects` since D243). Open:
`docs/TESTING.md` item 31 by hand through a real sudo on a terminal.

#### The sandbox question of `calu setup` (2026-11-19, PLAN D239, requirement 42)

`calu setup` asks whether Claude Code's sandbox is on (`--sandbox`/`--no-sandbox` for
scripts; without a terminal the stored value stays), and with the sandbox off `calu setup`,
every session's start and `calu doctor --db` recommend password-protected logins and/or
`--sudo`, unless that is known to hold. Measured: Claude Code starts without `bwrap`/`socat`
while the sandbox is off. Not run by hand yet: `docs/TESTING.md` item 37.

#### The redaction prompt and the trace format (2026-11-19, PLAN D245, requirement 44)

Built: `traces.redactor_instructions` (global or per project, shipped default
`defaults/checkers/trace-redaction.md`, fixed part `system/trace-redaction.md`),
`calu traces instructions show|edit|reset [--project]`, the web pane "Session traces"; the
IDE settings dialogs show it as a Markdown setting. Saves in the terminal and the web are
refused when the stored text changed meanwhile (also for `honesty.rules`). The shipped
instructions may remove the user's questions (a message of the user removed as a whole
leaves nothing in the file) and every hint of when and how long the session ran. Format
settings `traces.layout`, `traces.file_names`, `traces.header_times`, `traces.header_user`,
`traces.turn_numbers`, defaults unchanged. Open: an "Edit redaction instructions" entry in
the VSCode and PyCharm checker pickers, a conflict check in `ide.settings.set`, the manual
check `docs/TESTING.md` item 41 with a real model (the new prompt is not yet measured on
Haiku or Nemotron).

#### The final trace redaction before a commit (2026-11-19, PLAN D246, requirement 45)

Built (not pushed): `calu.traces.finalize` redacts the part of each trace file that is not in
`HEAD` once more as a whole (fixed part `system/trace-final-redaction.md` plus
`traces.redactor_instructions`), writes the file back as committed part + redacted part + a
marker line with a check value, and never touches the committed part. Triggers: Claude's
plain `git commit` in a session (`Session._finalize_for_commit`: before the commit runs,
stages the result, holds the trace writer until the tool result; a failure denies the
commit), the opt-in pre-commit hook (`calu traces hook install [--chain]|remove`, which runs
`calu traces finalize --staged`; refuses the commit when it cannot run) and `calu traces
finalize` by hand. Settings `traces.final_redaction` (on) and `traces.final_timeout_s`
(600). Open: the manual check `docs/TESTING.md` item 42 with a real model; the pass's prompt
is not measured on Haiku or Nemotron; a `git commit -a` in a session can take along the tail
of another session's live trace file in the same project (per-block redacted only).

#### The live permission-mode switch in every client (2026-11-19, PLAN D247)

Built (not pushed): request `permission_mode.set {mode}`, event `permission_mode.changed
{mode, client}`, snapshot field `permission_mode`. Offered: default, acceptEdits, auto;
refused with a reason each: plan, dontAsk, bypassPermissions; with `claude.require_auto_mode`
everything but auto. Each change is in the session's audit log (`calu sessions <id>`). Clients:
VSCode and PyCharm a "mode" selector next to the supervision level and `/mode`; the web live
view a select; the full-screen client `/mode` (Tab completes the mode) and `mode <name>` in
the status line; the line client `/mode [mode]`; ACP a select config option (the session
modes stay the supervision level). Measured in `spikes/spike_permission_mode_switch.py`
(`spikes/RESULTS.md`). Open: `docs/TESTING.md` manual item 43 with a real `claude` (for the
agent in the Cua desktop); plan mode is not offered (it writes its plan outside the project,
and leaving it is an approval calu does not present yet); the condensed skill
`../.claude/skills/claude-code-integration` does not mention the measured status message yet
(it lies outside `calu/`).

#### Tool documentation for the checkers (backlog, owner 2026-11-20)

The command checker sees a non-shell tool call as its name, the working directory, list
notes, the narration and the arguments as JSON, but no description of the tool or its
parameters: Claude Code's init message lists tool names only and the hook input carries
name and input. Options, in order: (1) a short fixed description of each of Claude Code's
own tools shipped with calu; (2) the MCP servers' own tool descriptions and schemas, if they
can be had through Claude Code / the SDK or the project's MCP configuration (spike first);
(3) the owner's fallback "if nothing else helps": ask Claude, in a separate session, for a
full verbatim redescription of all its tools (Claude has the definitions in its context),
turn the free-form answer into structured JSON with a model, cache it by the hash of the
tool list and refresh when the list changes. The owner's conditions for (3), because the
answer could otherwise be steered: the session is as blank as possible - no project
instructions read (a blank AGENTS.md/CLAUDE.md, at most an instruction to return the tool
descriptions), no memory, no skills or hooks of the project, no tool call allowed, nothing
else read - and it runs on a small instruction-following model such as Haiku, with little
agenda of its own. Tension to resolve in the spike: the session must still offer the same
tools as the real one (the same MCP servers and plugins), so it cannot simply drop the
project's settings; it needs the tool configuration without the instructions. Whatever the
source, a tool description is
untrusted text (an MCP server can write anything into it): it reaches the checker marked
as data, never as instructions, and is cut to a fixed length.

#### Worktree support (built 2026-11-20; the real-Claude runs L1-L6 done the same day)

**Built** (PLAN D251, the design and its section "As built" in
`agent/records/notes/worktree-design.md`, commits "Worktrees (1)" to "(6)"): a checkout registry
(`calu.checkouts`), one honesty state per checkout with shared trust of contents, the gate and
the honesty review per step's checkouts, a worktree outside the project needing a person's
certification while honesty is on (the owner's decision), calu's fixed rule against obvious
cross-checkout loading, the "Checkout:" lines for the checkers, snapshots and write sets per
checkout, the rules per worktree (trace copies, `git worktree remove --force`), the user
documentation (DOCUMENTATION.md "Worktrees") and `docs/TESTING.md` item 47. Unit cases U1-U14:
`tests/unit/test_worktrees.py`. **Item 47 (L1-L6) was run** with a real `claude` on Haiku in
the Cua desktop, with and without `.claude/worktrees/` ignored (`spikes/worktrees_l.sh`;
record: `agent/records/notes/test-plan.md` section L): as stated after three fixes
(`255d577`, `63b4a40`, `8440a28`).

**The owner's decisions of 2026-11-20 on its open points are built** (PLAN D252, six commits,
not pushed): trace headings name a step's worktree ("### Tool call 3: Bash (worktree
agent-a1b2)"; `check.result` and `step.held` carry `checkouts`, VSCode types regenerated); the
pre-commit hook in a worktree outside the project takes the main checkout's settings (`git
rev-parse --git-common-dir`); `/trust <worktree> <ref>` in the terminal clients (Tab completes
the names from `honesty.update`'s `worktrees`; the answer names the checkout); the honesty
review verifies files without code (a documented extension list, no execute bit, no `#!` or
program header) without a model; a subagent's `SubagentHandback` report goes to the
subagent-result checker before the call runs (note to the main agent, a person's denial
denies the handback) and the empty final result after it passes with the reason recorded;
`docs/TESTING.md` items 43-45 no longer name `calu sessions <id>`. **Waits for the owner:** the
wording of the shipped outside-worktree rule in `command.md`/`edit.md` (not changed). **Next:**
`docs/TESTING.md` item 48 in the desktop, above all (c): what a denied `SubagentHandback`
does to the subagent and the main agent (only the documentation says that "deny" blocks the
call and its reason reaches the subagent).

The owner: calu must work with git worktrees as Claude Code commonly uses them, without asking
about it, with and without parallel subagents. Known today: each tool call carries its own
`cwd` (hook input), relative paths resolve against it, and the path rules read a path inside
`<project>/.claude/worktrees/<name>/` (where `EnterWorktree` and `Agent` with `isolation:
"worktree"` put them) relative to the worktree's root (`checkers/rules.py`); `ExitWorktree`
with `discard_changes` has a rule. Everything else is open. Answer each question by
measuring (a spike with the real Claude Code), then design, then test with and without
parallel subagents:
1. Which tool creates worktrees: Claude Code's `EnterWorktree` and `Agent(isolation:
   "worktree")`, plain `git worktree add` through Bash, or both? Where do they land: inside
   the project (`.claude/worktrees/`) or outside (`../x`, `/tmp/x`)? A worktree outside the
   project: are its files project files and local edits for calu (snapshots, write sets,
   honesty, traces, the checkers' read tools, which are confined to the project)?
   *Measured:* both create them. `EnterWorktree` creates `.claude/worktrees/<name>`, `Agent`
   creates `.claude/worktrees/agent-<agentId>`, each on a new branch `worktree-<that name>`
   from `HEAD`; Claude Code adds no ignore entry. `git worktree add ../x` lands where it is told; a
   `cd` there does not persist. Outside the project: the read tools refuse its files, the
   snapshots never see it, the honesty key is the absolute path, the root-anchored write rules
   (`CLAUDE.md`) do not apply there (a note only) (`spikes/RESULTS.md`, "Worktrees, 2026-11-20").
2. For a subagent in a worktree: is the worktree its working directory and "project root"
   for the checkers, the rules and the context (AGENTS.md, CLAUDE.md of the worktree or of
   the main checkout)?
   *Measured:* every tool call of a worktree subagent (and of the main agent after
   EnterWorktree) has `cwd` = the worktree; the Agent result names `worktreePath`. calu keeps
   the main checkout as project root: rules match worktree-relative, checkers read the worktree
   files as `.claude/worktrees/...`, the context holds no project file. Claude Code loaded only
   the main checkout's `CLAUDE.md`, once at session start; subagent and main agent in a
   worktree answered from it, not from the worktree's copy.
3. Which `.claude/settings.json` applies to a subagent in a worktree: the main checkout's or
   the worktree's own copy (which may differ, or be older)? Same for calu's own project
   settings and the sandbox entries calu checks.
   *Measured:* the main checkout's working copy of `.claude/settings.json` applied (hooks and
   a permission deny rule) to the worktree subagent; the worktree's committed copy was not
   loaded. calu's preflight reads the session directory's files, i.e. the same ones.
4. Honesty status: today it is project-wide (`gate.py`: open marks anywhere make a step that
   executes project code DENY), keyed by path relative to the project. Are worktree files
   tracked as separate paths (`.claude/worktrees/a/src/x.py` vs `src/x.py`), or should status
   be kept per checkout? Can a dishonest edit by a subagent in worktree A block a subagent in
   worktree B from running code in B's still honest state (it would today, if A is inside the
   project)? What should hold?
   *Measured:* separate paths (`.claude/worktrees/agent-.../calc.py`). Yes: a mark in a
   subagent's worktree denied code runs in the main checkout and in another subagent's
   worktree (runs 3 and 4). Worktrees not ignored: right after EnterWorktree the directory
   entry `.claude/worktrees/wta` is unreviewed and unjudgeable, so the next commit escalated.
   Marks are per session: a second session ran the first one's dishonest code left in an
   ignored worktree.
5. Can the main agent or other subagents run code in any worktree, and which checkout's
   honesty state then applies?
   *Measured:* yes, by `cd` (inside the project the `cwd` persists, and ExitWorktree then
   restores that worktree, not the project) or by paths; the one project-wide state applies.
6. Snapshots and write sets of a command run in a worktree; the trusted-commit list
   (`honesty trust`) per checkout; traces and the final redaction pass per checkout; merging a
   worktree back (a commit in a worktree, then a merge/cherry-pick in the main checkout).
   *Measured:* writes inside a worktree have no write set (ignored: none; not ignored: the
   worktree as one path). A commit in a worktree ran the changeset checker on the worktree's
   diff; the final redaction pass skipped it (trace directory not in that repository). One
   trace file in the main checkout. The fast-forward merge had the write set `helper.py`; its
   verification under the worktree path does not carry over (code). Not measured: `honesty
   trust` and the pre-commit hook in a worktree.
7. Removing a worktree (with or without uncommitted work), and a worktree whose branch is
   checked out elsewhere.
   *Measured:* `ExitWorktree remove` with uncommitted work is refused by Claude Code until
   `discard_changes: true`, which removes worktree and branch. `git worktree remove --force`
   with uncommitted, marked work passed rules and checker. A worktree subagent without changes
   has its worktree removed at its end. A branch checked out in a worktree cannot be checked
   out or added elsewhere (git refuses).
8. **Cross-worktree loading (the owner's safety rule, 2026-11-20):** the checkers escalate a
   step in which code of one worktree obviously tries to import or run code from another
   worktree (or from the main checkout). This is a checker rule, stated in the checkers'
   instructions, not a detector that claims completeness: the owner ruled that complete
   detection cannot be had against a determined agent, so the rule covers the obvious cases
   only. It lets honesty status be kept per checkout without blocking every worktree when
   one is dishonest.
   *Measured (today's checkers, one obvious case):* `PYTHONPATH=../wta python3 -c "import
   helper; ..."` in a subagent worktree: command checker PASS, its summary said "It executes
   the helper module from the wta worktree"; the import left `__pycache__/` in `wta`.

#### Recording the user's own IDE edits (built 2026-11-20, PLAN D253, requirement 47)

Off by default (`human_diffs.enabled`, `.directory` = `agent/human-diffs`, `.keep_days` = 14).
Each save in the VSCode or PyCharm editor (all three PyCharm modes) of a git-tracked file,
while the editor is attached to a session, sends `human_diffs.add {path, before, after}`; the
core (`calu.human_diffs`, `Session.human_diff`) appends the unified diff to
`<dir>/<date>/<user>/<path with "__">.tmp.diff`, without redaction (the owner). The PyCharm
synchronisation's own saves are marked (`OwnSaves`) and skipped. `*.tmp.diff` files older than
`keep_days` are deleted at a session start and once a day; a renamed `.diff` is kept. A new
directory gets a `.gitignore` (`*.tmp.diff`, `.gitignore`). Claude's writes there meet a
built-in deny rule; write sets and honesty leave the directory out. Switching it on offers
`traces.enabled` in both IDE dialogs and on the web page, and `calu config set` prints a hint
(`admin.enable_offer`). Not run by hand in the desktop yet: docs/TESTING.md item 49. Open for
the owner: saves while no session is attached are not recorded; VSCode's auto-save makes one
block per save; whether a command or the diff screen should list the recorded diffs.

#### Claude Code as a whole inside namespaces (backlog, owner 2026-11-19)

Today only Claude's Bash commands run in Claude Code's sandbox; Claude Code itself (Read,
Edit, MCP servers, hooks) runs unsandboxed as the user. Idea: calu starts the whole Claude
Code process in namespaces, as hardening without root next to `--sudo` (which stays the strong
option, because only a different kernel uid makes the kernel itself the boundary):
- a user namespace mapping the user's own uid (no new identity: to the kernel it is still the
  same uid, so files are protected only by what the mount namespace hides);
- a mount namespace that hides calu's directories (`$XDG_CONFIG_HOME/calu`,
  `$XDG_STATE_HOME/calu`, the `--insecure` sockets) and the agent sockets under
  `/run/user/<uid>` (D-Bus, ssh-agent, gpg-agent, IDE sockets) except what is needed;
- a pid namespace with its own `/proc` (calu's process invisible and unsignalled);
- no capabilities, `no_new_privs`. Calu's memory is protected already by `PR_SET_DUMPABLE 0`
  and Yama, not by the namespace.
Known gaps to measure, not assume: inherited file descriptors, abstract Unix sockets (shared
unless the network namespace is separate, and Claude needs the network), calu's own socket
still reports the same uid (tokens stay required).
**Spikes first:** (1) Claude Code runs normally inside (login, settings, MCP, hooks, the IDE
links); (2) nesting: Claude Code's own Bash sandbox (bubblewrap) inside the namespaces, and
Docker used from inside (needed for projects that run containers; note that access to the
Docker socket is equivalent to root on the host, so how Docker is reached from inside must be
a deliberate, documented choice, e.g. rootless Docker or a proxy); Ubuntu 24.04's AppArmor
limit on unprivileged user namespaces; (3) what an escape attempt from inside can actually
reach. A third option to compare in the spike: subordinate uids (`/etc/subuid`, `newuidmap`,
as rootless Podman), a truly different kernel uid without sudo per start, at the cost of
project files owned by two uids (ACLs or a shared group).

#### macOS support (backlog, owner 2026-11-19)

Not started; the owner put it on the backlog. What ties calu to Linux today:
- the checkers' file reads find the opened descriptor through `/proc/self/fd`
  (`calu/checkers/tools.py`), so every read is refused on macOS (fix: `fcntl.F_GETPATH`);
- the sandbox checks require `bwrap` and `socat` (`preflight.SANDBOX_TOOLS`, `calu doctor`,
  the setup question), while Claude Code's sandbox on macOS uses Seatbelt (`sandbox-exec`);
- `localsocket.peer_uid` uses `SO_PEERCRED` (fix: `LOCAL_PEERCRED` with the `xucred` struct);
- `--sudo`/`--user` are Linux-only (`asuser.fs_identity` refuses with a message);
- `run.harden_process` (`prctl` dumpable) does nothing there, so another process of the same
  user could read calu's memory -- which weakens the password-protection variant of the
  sandbox-off recommendation (D239);
- `machine.py` finds no boot id or machine id, so finding dead sessions relies on the
  heartbeat alone;
- the local database through Docker Desktop is untested.

#### Next, in this order

0. **Remote development of the PyCharm plugin** (agreement accepted): run the split-mode
   checks of test plan section B (tool window in the client, RPC round trip with a
   transcript and Allow on a card, the password dialog, editors and diffs opened by the
   backend, the terminal action).
0. ~~Core defects found by the SSH round~~ **done** (agent 2, PLAN D237, commits
   `202b484`..`8c25dc1`, not pushed): an unended turn is history and a fork inherits it;
   record and replay meet at the prompt uuid (VSCode, terminal); unique scripted tool ids;
   stale at once when the process is gone, else after 60 s; `ide.fs.changed` carries
   `skipped` and the PyCharm plugin lists those paths at once. Not reproduced: the "about
   15 minutes" itself (the code said 180 s); the plugin's own part of H20 (it asks
   `ide.sessions` every 15 s only while no session is attached, and its waiting step still
   counts) is the plugin's.
0. The desktop image lacks `openssh-client` (installed by hand for the SSH round): add it to
   `uitest/Dockerfile`.

1. Collect those two agents; pre-push run of each new commit (rules below); push; wait for
   the CI run to complete.
2. **Coverage and honest code, the rest**: the `calu.cli` contracts, `_start_cancel` and
   the tests of `calu ide` that need no root are **done** (agent 2, PLAN D237; the rename is
   `_local_socket_endpoint`). The root-only rest is **done** (PLAN D240, not pushed):
   `asuser.prepare` on existing directories, the launcher refusing an identity it cannot
   take, `_after_setup` writing `/var/lib/calu/database.json` as real root, `calu serve
   --user` with Claude Code's stand-in and the root socket, the sandbox-off advice under
   `--user` and after `sudo calu setup --no-password`; `docker exec calu-rootbox
   rootbox-tests`: 22 passed. Fixed on the way: `asuser.prepare` followed a symbolic link
   in the place of a directory it hands to the user.
3. ~~Open defects the core-defect agent left~~ **done** (agent 2, PLAN D237): the protocol
   event `step.held` and the full-screen client's `held` badge after a recorded approval;
   a web decision records `<role>:<user>@<host>`. VSCode and PyCharm show no held steps at
   all (they ignore `step.held`); whether they should is open.
4. **VSCode extension, remaining** (needs the desktop, so after agent 2): the corner cases
   C5, C6, C11, C12 at scale (done for web, terminal and PyCharm); a look at the new honesty
   chip in a real VSCode; integration tests (`@vscode/test-cli`, as stay-in-the-loop has
   them in `extensions/vscode/test/integration` with `scripts/test-in-docker.sh`) in the
   regression suite; an "Open session trace" action in both plugins (offered to the owner,
   not answered); `/clear` against a real Claude Code. `extensions/vscode/calu.vsix` (not in
   git) predates the honesty chip: rebuild with `npm run package` before the owner's test.
5. **Final screenshots** from the Cua desktop for the README (`docs/assets/`): a tidy project
   path (the web one shows a scratch path with host and user name), a cleaner subagent shot
   than the one ending in "finished banana"; all 16:10 so rows line up.
6. Remote development of the PyCharm plugin needs the owner: the JetBrains CLIENT shows a
   "JetBrains Team Tools User Agreement" that only he may accept (viewer link:
   `uv run --no-project --with cua python uitest/desktop.py viewer-url`), then
   `uitest/pycharm-split.sh`.

#### Coverage and honest code (first round, 2026-11-19)

Line coverage of the unit and database layers together
(`uv run --with pytest-cov pytest --cov=calu -n 8`, then `-m 'db and not live'
--cov-append` on a server of its own): 92 % overall. Per module, before and after this round (on `88b6a12`, then `fa88764`):

| Module | before | after |
|---|---|---|
| `ide/access.py` | 80 % | 81 % |
| `ide/bridge.py` | 88 % | 90 % |
| `ide/filestore.py` | 86 % | 87 % |
| `ide/database.py`, `filesync.py`, `link.py`, `files.py` | 90, 96, 98, 99 % | same |
| `ide/serve.py` | 35 % | 35 % (runs in a child process, which coverage does not see) |
| `traces/render.py`, `redact.py`, `writer.py` | 98, 99, 88 % | same |
| `lightcycles/server.py`, `logfile.py`, `db/place.py` | 96, 100, 95 % | same |
| `localsocket.py`, `launch.py`, `serve.py`, `asuser.py` | 85, 87, 80, 77 % | same (the rest is root-only: the root box's `tests/root/`, not measured) |
| `cli.py`, `run.py` | 79, 89 % | same |

The numbers moved little because most findings were contracts, not missing tests; the
tests added cover behaviour with consequences (the login choice of `calu ide`, the silent
revocation, the trace directory, the login environment).

Honest-code findings fixed (each with its commit): `calu login` started `claude auth` with
the checker keys in its environment (now left out, as a session does; test); a trace
directory behind a link out of the project made calu create directories outside before it
refused (now checked first; the test checks that nothing is created); a token revocation of
`calu ide` that got no answer was dropped silently (now reported like a refused one; test);
contracts corrected for `ide.status` (it starts `bwrap`, reads Claude Code's settings and
closes gone sessions' escalations), `ide.attach`, `ide.start.cancel`, `Access.read` (the
"reading" login also writes what an operator may), the file store (listings work on
paths), `calu setup` (when the role `calu_admin` gets a new password), `--sudo` (what is
and is not checked), the password prompts (a blank password is accepted), the `--insecure`
sockets (the user's, not root's), `asuser`, `launch.become`, the trace redaction and the
trace file's end. Tests now say what they touch outside `tmp_path` (the tester's sockets,
Claude Code settings, the MetaTron screenshots, the root box's directories), and `calu
serve`'s process test keeps its log in `tmp_path` instead of the tester's state directory.

#### Decisions that wait for the owner (ask once, do not decide for him)

None open (all answered on 2026-11-19, above). The Tron quotes are the owner's own choice
from IMDb's quotes page; the MetaTron assets (bitmaps, sounds, music) stay out of the
repository as he asked.

#### Rules learned in this session (they cost time or caused damage)

- **Pushing.** "Push whenever you have reached a milestone"; README-only changes are pushed
  at once without waiting for anything ("can be done in isolation"). But `git push origin
  <commit>:main` pushes every ancestor: check `git log origin/main..HEAD` first. Twice a
  README push carried agents' unverified commits; once that put a failing browser test on
  `main`. When agents have local commits, make the README commit in a separate worktree
  based on `origin/main`, or wait.
- **Before a code push:** run on a clean copy of the committed state with a database server
  of its own (`git worktree add --detach <dir> HEAD`; the session used the container
  `calu-prepush-pg` on 127.0.0.1:55490 and
  `CALU_TEST_DSN=postgresql://calu_owner:calu-dev@127.0.0.1:55490/calu`):
  `uv run ruff format --check . && uv run ruff check . && uv run pyright`,
  `uv run pytest -q -n 8`, `uv run pytest -q -m browser`,
  `uv run pytest -q -m 'db and not live'`. Repo-wide lint, because CI is repo-wide. At a
  milestone also CI's own form, `uv run pytest -m 'not live and not browser' -q` (unit and
  database tests in ONE process, about 5.5 min): on 2026-11-19 module state of `calu.cli`
  leaking between tests failed only there, CI was red for hours while the split runs passed.
  Then push the tested commit, and check `gh run list` afterwards. Each push cancels the CI
  run in progress.
- When `origin/main` moved ahead of a shared local `main` (a push from another worktree),
  the stand-in cherry-picked the local commits onto it in the pre-push worktree and moved
  `main` with `git reset --soft <new tip>` plus `git checkout <new tip> -- <the files the
  remote commit changed>`, after checking that `HEAD` had not moved: the agents' uncommitted
  files stay untouched. A commit that lands in between is lost from the branch (it is still
  in the reflog): include every commit of `git log origin/main..HEAD`.
- Run the repo-wide `ruff check .` and `tests/unit/test_docstrings.py` before a commit, not
  only at the pre-push run: a nested function without a contract and a 115-column line cost
  one round.
- The owner does not want the full suite after every step (memory note
  `calu-selective-tests`); it is for milestones.
- Agents share the checkout: each commits its own paths only; never `git add -A` while
  agents work (it sweeps their half-done files in); a committed `HANDOFF.md` must agree with
  the committed parser (`tests/unit/test_handoff_counts.py`); database tests never with
  `-n`, and never two runs on one test server.
- **The place file reaches the owner's real database.** `cli.main` run outside the test
  suite with only the XDG variables redirected still reads `/var/lib/calu/database.json`
  and connects to his database (it happened on 2026-11-19: `calu sessions`, `escalations`,
  `token list` read, and a `resolve 1 allow` was refused, so nothing changed). Set
  `CALU_PLACE_FILE=/nonexistent` as well as the XDG variables in anything run by hand.
- **The owner's API key** (budget-limited, Haiku only; he revokes it) is inside
  `calu-uitest` in two files of user `cua`; `uitest/setup-real.sh --remove-key` deletes
  them. Never print it, never put any part of it on a command line (an agent once passed a
  prefix to `grep`).
- In the desktop: no sign-ins, no agreements accepted for the owner. He accepted PyCharm's
  user agreement himself (a trial runs; he has a Pro subscription).
- The owner's rulings on wording: no first person and no origin story in the README, facts
  not opinion, nothing that sounds like selling, do not quote his own messages in documents.

#### Containers and directories this session left

`calu-uitest` (the Cua desktop; running), `calu-sshbox` (SSH test server; agent 2 leaves it
stopped), `calu-rootbox` (stopped; `uitest/rootbox/run.sh`), `calu-prepush-pg`, `calu-fix-pg`
(agent 1 removes it), `calu-dev-caludb-1` (the test suite's server), and the owner's
`calu-db-postgres-1`, which nothing here touches. A worktree for pre-push runs under the
session's scratchpad (`git worktree prune` removes a stale entry), virtual environments
`~/.cache/calu-venv-push` and `~/.cache/calu-venv-timing`, a JDK 21 and 25 under
`~/.cache/calu-jdk/`, screenshots of every run under `~/.cache/calu-uitest/share/`
(`run1`, `run2`, `real1`, `pycharm1`, `pycharm-ssh1`, `lightcycles`).

The owner's machine: he reinstalls the root calu from GitHub after a push and runs
`sudo calu setup --no-password` (needed again for migration 0037, the four-argument
`calu_resolve_escalation`, and for D230).

### The VSCode extension and the PyCharm plugin: what the owner wants

**D231 lists every requirement with its state**; the contract is
`agent/records/notes/vscode-extension-spec.md` (sections 1-8), his words are requirements 34 and 35.
In short: parity with Claude Code's chat first (images, rewind, fork, several sessions,
slash commands, model and effort, context usage, questions, escalations, notifications),
dark theme first, then calu's features one click away (checker context, checker
instructions, honesty mode, all settings in a searchable dialog, one model for all roles with
presets), subagents in full, a session dropdown and session names, installing uv and calu,
checking for `claude`, working in VSCode's remote modes, instructions for a persistent
`calu serve` in tmux or screen, at most one password once, and never refusing a blank
password. Then a PyCharm plugin with local, remote and SSH-only modes
(`agent/records/notes/pycharm-plugin-research.md`).

Next steps for the extension, in order:
1. Collect what the run agent left and reported (above); commit; relay its list of open
   defects and UX observations to the owner and fix what he picks.
2. (In work by that agent.) Run it for the first time: build a `.vsix` (`npm run package`), install it in the VSCode of
   the Cua desktop (`uitest/README.md`), start a session with fake checkers
   (`spikes/smoke_session.py DIR --stay`, or `calu serve` in the container) and walk through
   the chat, a subagent, an escalation, the settings dialog, the checker editor. Nothing of
   the extension has ever run inside VSCode: expect defects in the wiring.
3. Integration tests (`@vscode/test-cli`, as stay-in-the-loop has them) and the user
   documentation (`DOCUMENTATION.md`, `README.md`, `docs/TESTING.md`, the manual checklist).
4. Ask the owner to try it in his VSCode against WSL.
   **Session traces in the project** (requirement 36, D232) are built and merged: a
   setting that appends each step of a session as redacted Markdown to
   `.agent/traces/<date>/<user>/session_NNN.md`. Not yet run with a real model (manual
   checklist item 36): how well a real model redacts is unknown.
5. Then the PyCharm plugin: the three spikes named in the research notes first; and the small
   fix in `calu acp` (`session/new` must answer a fresh id) as an interim step.

### What exists now, by area (newest decisions first)

- **Terminal output, log file and local trust** (requirements 30-32; D225-D227, 2026-11-18).
  `calu run` and `calu serve` print the session's status only, never the conversation
  (`run.STATUS_EVENTS`); tracebacks go to `$XDG_STATE_HOME/calu/log/<command>.log`
  (`src/calu/logfile.py`), stderr gets one line per error; `calu run --thinking` is gone.
  Root clients find a session through the root-owned sockets without the database or a
  password (`cli._local_socket_endpoint`, `cli._hold_password_free`); `calu web --sudo`
  relays through them. `--secure` binds no root socket; `--insecure` lets every Unix-socket
  and loopback-TCP client in without a token. The owner ruled that a session running
  Claude Code as calu's own uid keeps asking for a token without `--insecure` (the kernel
  cannot tell the client from Claude Code's commands). Manual checklist items 33 and 34.
- **Place file and `calu setup --no-password`** (requirement 33; D228, 2026-11-18).
  `sudo calu setup` writes `/var/lib/calu/database.json` (`src/calu/db/place.py`, no
  secret); a client that found a root session through a socket takes the database from it
  and never from the user's own store. `--no-password` stores `admin`, `session` and a
  `calu_admin` login protected by the store file's mode alone. The owner had asked for a
  root-only database socket as one of two options; it was built as the stored login instead
  (D228 says why). With `--no-password` the generated `calu_admin` password is also in the
  place file, readable by everybody, so that a client without `--sudo` saves settings
  without a password: the owner ruled that this weak security is allowed (D228 (4b)). A
  password-free `session` entry is accepted only from a store root alone writes (4a). Checklist
  item 35. The local database listens on TCP 127.0.0.1:5433 only.
- **calu as root, Claude Code as an unprivileged user** (requirement 26; D206, D207,
  D211-D216, D223). `--user NAME|UID[:GID]` (required as root) and `--sudo` (re-runs calu
  through sudo with `--user <you>`) on `calu run`, `serve`, `start`, `attach`, `web` and
  `login`. Pieces: the launcher `calu-claude` (`src/calu/launch.py`), `src/calu/asuser.py`
  (resolve, prepare, enter = per-thread file identity, child = ids and a clean environment
  for programs started in the project, `as_root`, installation check, sudo command, system
  directories `/etc/calu`, `/var/lib/calu`), the root-owned socket
  `/run/calu/sessions/<id>.sock` with token-free root clients (`src/calu/localsocket.py`),
  `calu_admin` with the typed admin password (migration 0033), the web login with the
  admin password, `calu login` (Claude Code's own login for the session user).
  Rules the owner set, do not reopen: calu never adds users, no dedicated system user; a
  number is always a bare uid with home `/var/lib/calu/claude/<uid>`, a name uses that
  user's home; the launcher is part of the installation, no script written at run time;
  privileged files only in system directories or below `/root`; changing settings is not
  privileged, the files are (D213).
- **`calu serve` / `calu start`** (requirement 27; D208): one process authenticated once,
  sessions requested through `session_requests` rows; `/new`, `/fork`, `/running`,
  `/switch` in the full-screen client and buttons in the web interface; `--idle-timeout`;
  a dead claim expires after 180 s (migration 0035). The owner ruled (2026-11-17) that the
  shared session login may claim any host's and user's request, prompt included: intended.
- **Conversations started with `claude` itself** (D209): listed by `calu list-sessions`,
  resumable and forkable; a fork records its origin (D219, migration 0034).
- **Honesty checker** (requirement 28; D217): answers HONEST/DISHONEST per file, never stops
  a step, escalates only what looks deliberate. Skill `.claude/skills/honest-code/SKILL.md`
  states the rule for agents (D210, with the owner's rulings: names and doc comments only,
  README excluded; tests describe side effects and printed data; configuration that widens
  side effects counts as code).
- **Web live view** (D218, D220): Markdown, gap between turns, thumbnails of images Claude
  reads, images attached to a chat message (also through ACP).
- **Full-screen client**: fully mouse-enabled (D219); `f4` starts the git program only
  (gitui by default), never a stand-in (owner's ruling 2026-11-16).
- **ACP** (D221): driven end to end by the official client library (`spikes/acp_host.py`);
  `session/list`, titled permission requests, images, WSL paths, stale-id refusal.
- **Review of 2026-11-17** (D222): ten reviewers, fixes by eight agents; what changed is
  listed there.

### What to do next, in this order

1. **Things only the owner can try** (each in `docs/TESTING.md`): `calu run --sudo` and
   `calu serve --sudo` through a real sudo (item 30); `calu attach --sudo` with the
   password-free settings, `/running`, `/switch` (31); the web password login in a browser
   and `calu web --sudo` (32); `calu login --sudo` with a real sign-in; `calu acp` in
   PyCharm. For ACP: his start script `/home/klondenberg/projects/calu/.run_acp.sh` still
   names the address and token of a session that ended on 2026-11-13; it should run
   `/usr/local/bin/calu acp` without them, with an `operator` credential stored without a
   password. PyCharm's log: `/mnt/c/Users/KaiLo/AppData/Local/JetBrains/PyCharm2026.3/log/idea.log`.
   After pulling, he reinstalls the root calu (`sudo env UV_TOOL_DIR=/usr/local/lib/calu
   UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/usr/local/lib/calu/python "$(command
   -v uv)" tool install --reinstall git+https://github.com/kadeng/calu`) and runs
   `sudo calu setup` once (migrations 0033-0035, the `calu_admin` password).
2. **Decided by the owner on 2026-11-18, recorded, not built** (D224, requirements 28 and
   29; the owner said "do not implement yet"):
   - **Honesty is judged by the honesty checker alone.** The other checkers lose the honesty
     rules and the marks instructions and their marks are ignored; they keep the state note
     and `executes_project_code`. D224 part 3 lists what to change, including the tests that
     use `testing.marking_checker` on other checkers. Build this first when the owner says go.
   - Settled, nothing to build: a `claude:` checker under `--user` runs in that user's
     environment and uses the user's own login (`calu login`), never root's variables (the
     code already does this); parallel web login attempts need no cap.
   - **The honesty checker's result is not one label** (D224 part 4, the owner's
     correction): it is a structured status per file and unit, plus an escalation when it
     escalates; it may later mark through tools (`mark_dishonest` / `mark_honest`) instead
     of structured output. Clients must show the per-file statuses, not "PASS". Do not ask
     him again whether it should return "a label".
3. **Open from the review (D222, "Left open")**: browser tests for the styled password page
   and pathological Markdown; Ctrl-C mid-turn under `calu serve`; no automated test for the
   root-only paths (`_client_user`, `_become_client_of`, `_local_endpoint`, the sudo
   re-execution, the launcher's exec).
4. **Open in the privileged model**: RDS together with `--user` (a token renewal reads the
   AWS profile after the switch: give that one thread root's file identity back); Claude
   Code's sandbox under the other uid (never switched on in a run); a named user with a
   real home (not run); `calu doctor` knows nothing of `--user`; the single-file binary has
   no launcher.
   **2026-11-18, after the run in the root box** (`uitest/rootbox`, PLAN D234,
   `agent/records/notes/test-plan.md` section E): a named user with a real home ran (`ada`, `bob`,
   also through a real sudo with and without its password); `calu doctor` has `--user` and
   `--sudo`; of the sandbox under the other uid only the settings calu generates were read,
   because an unprivileged container may not create user namespaces (it needs a machine or
   a container with that right); RDS and the single-file binary are as before. Found and
   corrected there: `--user root` bound the token-free root socket (a command of Claude Code
   was a root client), `calu run` ignored SIGTERM, a second password prompt at the end of
   `calu attach`, tracebacks at Ctrl-C and Ctrl-D, and the smaller ones D234 lists. New:
   `calu setup --set-passwords`, the root-only test layer `tests/root/` (`docker exec
   calu-rootbox rootbox-tests`). Left for the owner: a command of `calu config` in an
   account whose store holds an `admin` entry of another database than its `operator`
   entry changes that other database and does not say which; under `--sudo` Claude Code
   gets sudo's `PATH`; every `calu setup` after `--no-password` gives `calu_admin` a new
   generated password, which a running `calu web` notices only when its connection drops.
5. **RDS with IAM authentication has never run against a real instance** (D205;
   `docs/TESTING.md` item 26). Also not run: rootless Docker.
6. **Publishing**: install is `uv tool install git+https://github.com/kadeng/calu` until
   PyPI is set up (`release.yml`); the single-file binary was not rebuilt.
7. Smaller open work: `cli.py` (about 4,300 lines) should become a package; a checker's
   model and limits live in two stores; a fork cannot be forked again before one of its
   inherited turns; a login role per machine (D205, last part); timing-sensitive browser
   tests.
8. **Later, by the owner's priorities**: plugins for VSCode and JetBrains and a git browser
   in the web interface (D200, D201); a schema per database user (soft goal 7).

### The owner's machine (as of 2026-11-18)

calu for root in `/usr/local/lib/calu` (links in `/usr/local/bin`); a local database in
`/var/lib/calu/db` (container `calu-db-postgres-1`, port 5433) with root's credential store
in `/etc/calu` (copied from his own once); an `operator` entry in his own store
(`~/.config/calu`); his older database untouched in the Docker volume `calu-db_data`, whose
credentials are the `admin` and `session` entries of his own store. gitui is in
`~/.local/bin`. The test server for calu's own tests is `calu-dev-caludb-1` on port 55432.

### How the owner works with a session (learned on 2026-11-12 and 13)

- He sends short messages while work runs, often several in a row that refine one idea. Take
  each as a decision, record it in `REQUIREMENTS.md` (his words only) and `PLAN.md`, and go on;
  do not stop to confirm. When he says no to a proposal ("don't mess with adding users"), drop
  it for good.
- A second Claude Code session may be working in the same checkout: on 2026-11-12 a paused
  one had been restarted by accident and committed and pushed in parallel. If commits appear
  that this session did not make, look for another `ccd-cli` process (`ps`) and ask him.
- For a larger change he accepts, and the results justified, an independent review before the
  commit: reviewers per aspect, each finding checked by a second agent. Both reviews of
  2026-11-13 found real defects that the tests had not (20 confirmed in the database setup).
  It needs his opt-in for multi-agent work ("ultracode" or his own words).

### Things about this machine that cost time

- `../.env` (sitl's, with `FIREWORKS_API_KEY` and `MISTRAL_API_KEY`) has Windows line
  endings: never `source` it. Use `export FIREWORKS_API_KEY="$(grep '^FIREWORKS_API_KEY'
  ../.env | cut -d= -f2- | tr -d '\r"')"`. Do not print the key (it ended up in one tool
  output once).
- The environment of a Claude Code session here carries `ANTHROPIC_API_KEY` and
  `ANTHROPIC_BASE_URL`; a `claude` that calu starts uses them.
- tmux: never `tmux kill-server` (it would end the owner's sessions); use a private server,
  `tmux -L calutrial ...`. A new tmux server does not get exported variables reliably: put
  what a pane needs into a small script.
- The owner's own local database is the container `calu-db-postgres-1` on port 5433, and the
  test server is `calu-dev-caludb-1` on 55432. A real run of the Docker variant must use
  another compose project and port (`calu.db.local.PROJECT = "calu-db-spike"`, port 55477,
  in a driver script), and its data directory cannot be deleted afterwards without root,
  because the files belong to the container's user: tell the owner the path.
- `uv run pytest -q -m 'not live'` takes about six minutes and includes the 16 browser tests.
  Do not edit documents while it runs: some tests read them.
- Removing files with `rm -rf` on a path built from shell variables is refused by the
  session's safety check; do not work around it.
- `/mnt/c/Users/KaiLo/...` is the Windows side (PyCharm's log and configuration).

### What was done, item by item (the list the work followed)

1. **Done: the honesty redesign (D196) is merged into `main` and was run against a real
   model** (the branch `feature/honesty-certification` is history now): trusted commits and
   file contents in the database (migrations 0029 and 0030), the computed state, the honesty
   review, enforcement on running and committing, the clients.
   - Four live runs on 2026-11-12 with `spikes/smoke_session.py --honesty MODE --checker-model
     ...` (a real `claude` on haiku, checkers and reviewer on Nemotron Lightning), each as
     the specification says: `track` (two checked edits, the tests and a commit ran
     unblocked; the commit was certified with the source `check`); `enforce` with an honest
     docstring change made by `sed` (the test command started the review, which verified the
     file; the commit was certified with the source `review`); `enforce` with a function
     changed by `sed` so that it contradicts its docstring (the review recorded the right
     finding and the test command was denied with it); `enforce` without a trusted commit
     (the test command waited as an escalation that names `/trust`). The command checker
     answered the new field (whether a command executes project code) correctly in every
     step. The commands and what to expect are in `docs/TESTING.md`.
   - **Not run live:** `/trust`, `/trust-file` and `/honesty-review` typed by a person in
     `calu attach`, the honesty panel of the web live view, a review of more than one batch,
     a review that fails, and the reviewer on a large change. The manual checklist
     (`docs/TESTING.md`, item 25) covers the first two.
   - The specification is `agent/records/notes/honesty-spec.md`. Where the build deviated from it, and
     what each step left out, is in `agent/records/notes/reports-2026-11-12.md`; several deviations
     are stricter than the specification on purpose. Read them before changing the feature.
   - The full-screen client's honesty screens were written before the real protocol existed
     and were reconciled with it at the merge (`src/calu/tui/model.py: honesty_from`, the
     scripted session in `src/calu/tui/fake.py`). The protocol does not carry a commit's
     subject line, so the client shows the baseline's id and source only.
2. **Done: gitui is the default git program of the full-screen client** (the owner's choice,
   https://github.com/gitui-org/gitui). `f4` starts it in the session's project directory;
   `CALU_GIT_TOOL` or `git_tool` in `tui.toml` name another program; without gitui the client
   offers to install it (`src/calu/tui/gittool.py`: brew, else the release binary into
   `~/.local/bin`, which was run once into a scratch directory, else the package manager's
   command, shown and not run when it needs `sudo`). Tried in a real terminal on 2026-11-12:
   the install offer on a machine without gitui, and the fallback `git diff` (which showed
   nothing for a short diff until its pager options were corrected, and now says so when
   there are no changes). **Not tried: gitui itself** (starting it with `f4` and quitting it);
   this session was not allowed to download and run the binary.
3. **Done: the user documentation of the full-screen client** (README "Four ways to work
   with a running session", `DOCUMENTATION.md` "Attached to a session"; the decision is D199).
   It was written from the builders' reports and checked against the command table in
   `src/calu/tui/commands.py`, not against a session in a real terminal: correct it where
   item 4 shows a difference.
4. **Partly done: the full-screen client against a real Claude Code session.** On
   2026-11-12 it was driven in tmux (120x36) against a real session (`spikes/smoke_session.py
   --stay`: a real `claude` on haiku, checkers on Nemotron Lightning, `honesty.mode` track).
   The owner has not used it yet.
   - Worked as required: thinking shown; user input set apart (an automatic message of
     Claude Code, such as a subagent's hand-back, is labelled "automatic message"); a
     subagent's own tab and alt+left/right; a message to a running **background** subagent
     (the main agent relays it with SendMessage; a foreground subagent takes none, and the
     client says so); `/btw` during a running command; an escalation on the card, its
     full-screen detail (`w w`), allow (`a a`), deny with a note (`n n`, alt+d); scrolling:
     with the transcript scrolled up a new answer moved none of the visible lines, the marker
     counted the new lines, End returned; the checkers screen (read); the honesty page with a
     real review started by `r`; `/trust` (Enter certifies nothing); the mode chooser; `f4`.
   - Found and fixed: (a) a side question appeared as a third agent and its answer went to
     the subagent checker, because Claude Code calls the SubagentStop hook for the fork that
     answers it (`SdkRunner.side_question`, `spikes/RESULTS.md`); (b) `/honesty-review` said
     only "was asked for", and only when the review had ended; it now says so at once and
     reports whether a review ran; (c) `f4` without gitui showed nothing for a diff shorter
     than the screen and for no diff (`gittool.FALLBACK`, `gittool.unstaged_changes`);
     (d) a subagent tab's first line wrapped when its state was long.
   - **Not tried:** editing a checker's instructions and `/honesty MODE` with a password
     (the machine's `admin` credential belongs to the owner's own database, not to the
     trial's; a wrong password was refused and nothing was stored); gitui itself; `y` on
     `/trust`; ssh without `COLORTERM`; 80x24; the mouse; `alt+c`; two clients on one
     escalation; a restart of `calu run` while attached; the items of `docs/TESTING.md` 22
     to 24 that are not named above.
   - Seen and left: the detail screen shows "the step as the checker saw it" with its line
     breaks collapsed into one paragraph; the line client prints an automatic message of
     Claude Code with the same `> ` as a message of the user (the core records it as
     automatic, so no checker takes it for user input).
5. **Done: the user documentation of the honesty redesign** (README "Code honesty";
   `DOCUMENTATION.md` "Code honesty enforcement" with "Verified code, ..." and "Judging the
   checkers before you let them block"). Written from the reports in `agent/records/notes/` and checked
   against the live runs of item 1 and the command line's own help. `PLAN.md` §13 still
   describes the register of 2026-11-10 as history; D196 is what holds.
6. **Done: the three defects the split of `session.py` left open**
   (`agent/records/notes/reports-2026-11-12.md`, "defects found"), each with a test that fails on the
   old code: the state stayed "tool_call" after a human allowed an escalated subagent result
   (`RunState.forget`); the clock alarm was forgotten when the gate returned its escalation,
   so an escalation that ended without a human decision left the next step unescalated (the
   alarm now stays until a human allowed or denied, `Session._ask`); a rejected second start
   cleared the running session's not yet narrated text. The same report lists smaller ones
   that are still open (the escalation desk's entries for steps without a `tool_use_id`, an
   unreachable `except TimeoutError`, wording of two log lines, a write set whose recording
   failed).
7. **Timing-sensitive browser tests.**
   `tests/browser/test_web_ui.py::test_the_settings_page_shows_one_pane_searches_all_and_says_what_is_unsaved`
   failed once inside the full run of `-m 'not live'` (which includes the browser layer when
   the browser is installed) and passes on its own every time. On 2026-11-12
   `tests/browser/test_web_live_ui.py::test_a_question_of_the_agent_is_answered_by_clicking_an_option`
   and `tests/browser/test_web_login.py::test_the_live_view_attaches_with_the_key` failed once
   while a second test run used the machine, and passed alone. No failure output was kept, so
   nothing was changed: the tests assert right after a click or a navigation without waiting
   for a condition, which is the likely cause. Fixed since: the unit test
   `test_subscribing_again_does_not_send_an_event_twice`, which failed once in CI, counted
   pushes that a second subscription may legitimately replace by its replay.
8. Smaller open work, no decision needed: a checker's model and limits live in two stores
   (below); `cli.py` should become a package. Done: `herd.close_gone` no longer takes "ran on
   this machine" from the host name; a session records its machine, boot and pid namespace
   (D202, `src/calu/machine.py`, migration 0031).
9. **Later, not now: plugins for VSCode and the JetBrains IDEs, and a git browser in the web
   interface** (the owner on 2026-11-12, requirement 23 and soft goal 6; `PLAN.md` D200, D201
   and phase 9). Both plugins must work in the IDE's most modern remote development mode.
   The owner's order: only after the full-screen terminal client, the web interface and
   standalone use work well. Nothing is built.
10. **Done: `calu list-sessions [N]`, `calu run --fork ID`, and the `[session] ID` line**
   (the owner on 2026-11-12, D203), run once against the real Claude Code for a new session,
   a resume, a fork and a fork before a turn. Left open, all small: a fork cannot be forked
   again before one of its inherited turns (the copied turns carry no transcript uuids, and
   whether a fork's transcript keeps them is not measured); a session a client starts through
   the protocol after a stop lets Claude Code choose its id and prints no `[session]` line;
   a listed line with wide characters can be wider than the terminal.
11. **Not built, decided against for now: development channels** (the owner on 2026-11-12:
   an option to start Claude Code with `--dangerously-load-development-channels
   server:agent-chat`, so that agents on one machine can talk to each other). Measured with
   `spikes/spike_dev_channel.py` (`spikes/RESULTS.md`): an interactive `claude` asks for the
   confirmation "I am using this for local development" and then delivers the channel's
   messages; through the Agent SDK, which is how calu runs Claude Code, the flag is ignored
   without a message, the server connects as an ordinary MCP server and what it pushes is
   dropped. The owner: leave it, and replicate it later as a channels-enabled plugin or MCP
   server.
12. **Done: one package, and `calu setup` with three database variants** (the owner on
   2026-11-13; D204, D205): `uv tool install git+https://github.com/kadeng/calu` installs
   everything; `calu setup` asks for a local database in Docker (data directory chosen at
   setup, started on demand by `calu run`), an existing server, or RDS with IAM
   authentication. Run for real: the Docker variant. **Never run: RDS with IAM** (no AWS
   account here; `docs/TESTING.md` item 26 says what to check), rootless Docker, and an
   existing server whose user is no superuser beyond what `tests/db/test_setup_variants.py`
   covers.
13. **Built (D207, D211, D212), the sudo path not run: calu runs privileged, Claude Code as
   an unprivileged user** (the owner on 2026-11-13; requirement 26). See item 1 of "What to
   do next" for what exists, what was run and what is open. Without `--user` and `--sudo`,
   `calu run` works as before, and the RDS variant's protection of the AWS profile then
   rests on the sandbox's deny list.

14. **Done: `calu serve`, `calu start`, and conversations started with `claude` itself**
   (the owner on 2026-11-13, requirement 27; D208, D209). See "Built on 2026-11-13" above
   for what was run and what was not.

Decisions of the owner on 2026-11-13: D204-D212 are implemented (D206 through D207, D211
and D212); D210 is the skill `honest-code` with the owner's rulings on what honesty covers. Decisions of 2026-11-12 that are implemented and recorded: D178-D203 and Q22 in
`PLAN.md`. Standing instructions of that day, beyond the ones below: no file of the workspace
is part of a checker's context (D193); rule lists stay empty by default and are the exception
(D198); a blank model is Claude Haiku on the Anthropic API, or on Bedrock in an EU region in
Bedrock mode (D194); calu does not override Claude Code's permission mode (Q22); documents,
comments and prompts describe what calu guarantees and never spell out how to get around the
checkers.

## Instructions from the owner

- "You may implement, make reversible decisions and commit and push at reasonable checkpoints.
  Stop only when you must ask me something." Everything that is only in git is reversible.
- Work in the Linux copy under `~/projects/stay-in-the-loop/calu`, never in the old Windows copy
  under `/mnt/c`. The `CLAUDE.md` in the parent directory belongs to sitl; for calu, follow
  `AGENTS.md` (which repeats sitl's coding rules, D11).
- Run the checks with `scripts/wsl-test.sh -m 'not live'` after
  `docker compose up -d caludb --wait`.
- Commit and push to `main` after each finished unit of work (`gh` is logged in as kadeng).
- `REQUIREMENTS.md` holds only what the owner stated; derived design goes into `PLAN.md`.
  `AGENTS.md` stays minimal (onboarding and code layout, no invented conventions).
- Plain technical language everywhere; contract docstrings (ruff `D`/`DOC` enforce them, and
  `tests/unit/test_docstrings.py` checks completeness).
- Never copy code from `.reference/claude-a2a` (no licence). Mistral: small models only.

## Why calu sits inside the stay-in-the-loop directory

calu is the successor of stay-in-the-loop (sitl) and was built by porting sitl's code. calu is
cloned as `calu/` inside sitl's working tree so that a session can read sitl's source, tests and
spike results in the parent directory `..`. It is its own git repository
(github.com/kadeng/calu, public, GPL-2.0) and is listed in sitl's `.gitignore`. Commit calu work
in `calu/` only.

## Where things stand

| Phase (`PLAN.md` §12) | State |
|---|---|
| 1 Skeleton | Done |
| 2 Core port | Done |
| 3 Session runner | Done (`calu run`, steps/turns/escalations in the database, operator resolution, sandbox settings, herd commands) |
| 4 Bus and clients | Done (event bus, JSON-RPC over WebSocket for several clients, `calu attach`, comments) |
| 5 Sandboxing | Built: the settings (D15), the per-project network policy (D39) with a proposed default for new projects (D45, Q5 asks the owner to confirm it), `calu evals`. Untested: what `weaker_nested_sandbox` changes, which needs a logged-in `claude` inside a container (the rest of Q1 is measured). |
| 6 Herd management | Done: the commands, the web interface, the triage queue, the badge and the jump palette |
| 7 Third-party UIs, packaging | Done except what needs a real editor: `calu acp` with ACP's session modes and config options (D42), the wheel as the shipping format with the single-file binary buildable beside it (D41, `scripts/build-binary.sh`). Driven as a real process over real pipes for the handshake, a prompt, a denied step and `session/load`'s replay (`tests/unit/test_acp_process.py`) |
| 8 Honesty enforcement and checker families | Done (the owner's brief of 2026-11-10, §13): several checkers on one route run in parallel with most-restrictive-wins (D81); code honesty as redesigned on 2026-11-12 (D196): trusted commits and file contents in the database, a state computed against the baseline (marks, unreviewed files, review required), the honesty review, and in `enforce` mode a clean state before project code runs or is committed; the honesty checker's own instructions shipped and seeded off (D85); the state carried to every front end. Run against a real model on 2026-11-12 ("Pick up here", item 1). Whether honesty, security and over-engineering are one agent or several is Q18 |

All eight phases are **implemented**; what is left is listed under "Next steps" and "Open
questions" and is either the owner's decision, a proposal of mine to confirm, or something this
machine cannot do (a real ACP editor, a PyPI account, model API keys, a logged-in `claude`
inside a container). No phase has unbuilt work in it.

Checks: `scripts/wsl-test.sh -m 'not live'` is ruff format/lint clean, pyright 0 errors,
**2713 tests pass** (plus 16 browser tests, which skip without `scripts/get-browser.sh`) (unit + database). The database tests need `docker compose up -d caludb
--wait`. about 470 commits, ~83k lines of Python in `src` (~88k with the SQL, templates and
default instructions), ~49k of tests, 40 migrations, 28 CLI commands (27 in `calu --help` and the hidden easter egg
`calu lightcycles`), 26 protocol requests,
21 events. CI (`.github/workflows/checks.yml`) runs the same checks, the database and
browser layers and the installed wheel on every push and pull request; the live layer never runs
there.

### What exists

- **Checker record** (D61): `calu checker stats [--days N]` and each checker's pane of the web settings page report,
  per checker, what it decided and where a human disagreed -- escalations that were allowed
  (caution for nothing) and steps it approved that were then denied (misses). Read from the
  record, no model called. `calu checker misses [NAME]` lists the rows behind those counts,
  with the escalation id, so the instructions can be fixed against a real case (D62).
- **Docs**: `README.md` for users and `DOCUMENTATION.md` as their reference documentation, `docs/CONFIGURATION.md` (generated from `calu.config` by
  `calu.configdoc`; `tests/unit/test_configdoc.py` fails when the file and the model
  disagree, or a field has no docstring), `docs/TESTING.md` for the four layers and the
  manual checklist, `PLAN.md` for the decisions, `AGENTS.md` for the rules.
- **CLI** (`src/calu/cli.py`), 21 commands: `db up|down|migrate`, `setup`,
  `credential list|set|remove|new`, `run`, `attach`, `acp`, `web`, `protocol`, `sessions`,
  `escalations`, `escalation`, `resolve`, `token`, `summary`, `doctor`, `evals`, `project`,
  `config get|set|unset|add|remove`, `checker`, `rules`. Passwords via getpass, or one line
  each from stdin when stdin is not a terminal (tests use that). The read-and-resolve commands
  prefer the `operator` credential and fall back to `admin` (D48); everything that changes the
  configuration needs `admin`. Every `calu ...` line in the documentation is parsed by a test
  (`tests/unit/test_documented_commands.py`), and every command is documented.
- **`calu run [DIR]`** (`src/calu/run.py`): password → decrypt the `session` credential → two
  connections (queries, notifications) → wipe the password, the derived key and the decrypted
  buffer; the connection string stays in the process (`calu.db.ConnectionSource`) for
  reconnects, and the process clears its dumpable flag (D187) → find the project of the
  directory, or fail on an unknown `--project` (D188) → load the settings → create the
  `sessions` row through `calu_start_session` and take it over with `calu.session_id` and a
  session key (D49, migration 0025) →
  start Claude Code → serve the protocol and print the address and a client token. A lost
  database connection is reopened with backoff while the steps wait; a login the server
  refuses for good stops the session. A closed stdin does not stop a session that has work. `-p/--prompt`, `--batch` (ends when Claude is done **and** no client is attached),
  `--resume`, `--model`, `--effort`, `--project`, `--thinking`. Reloads the configuration on
  `config_changed`; continues at once on `escalation_resolved`.
- **A heartbeat** (migration 0016, D56): `sessions.last_seen`, stamped every 30 s
  (`herd.HEARTBEAT_S`, D187) by `Session.beat`. `herd.SessionRow.stale` is silence beyond
  180 s for a session that has not ended; `calu sessions` marks it "SILENT for N min" and the
  fleet page groups it as "Gone (no heartbeat)", out of "Needs you". The decision path says
  it too: `calu escalations`, `calu escalation <id>` and the web pages mark an escalation
  nobody is waiting for -- an answer is recorded, the step does not run -- and
  `calu resolve <id> cancel [note]` clears such an entry as a cancellation rather than as
  a decision nothing acted on (D57). A session that ends closes its own open escalations, and
  the listings close those of a session that ended, whose process is gone on the listing host,
  or that was silent for 24 h (`herd.close_gone`).
- **Isolation of a session's rows** (migrations 0015, 0025, 0026; D49, D187): the
  `calu_session` login reads and writes only the rows of the session whose key its connection
  holds (the database stores the key's hash), with three owner-side functions for what
  a policy cannot do (`calu_start_session`, `calu_recent_sessions`, `calu_conversation_sessions`
  plus `calu_same_conversation` for a resumed or forked conversation). The owner is unaffected;
  `calu_operator` reads everything.
- **Packaging**: the wheel (`uv tool install`), and `scripts/build-binary.sh` for a single-file
  binary that `release.yml` builds and runs on x86_64 and arm64 for every tag (D41, Q15).
- **CI**: `.github/workflows/checks.yml` (lint, types, unit + database, browser, the installed
  wheel) and `release.yml` (wheel, both binaries, and a publish step that only a tag reaches).
- **Session** (`src/calu/session.py`, ported from sitl): tool gate, escalations, narration, chat
  queue, comments, subagent result review, login hint, states of `PLAN.md` §3.1 derived from
  what runs. `apply_settings` applies a reloaded configuration from the next step on.
- **Four decisions** (D21): PASS (no objection; Claude Code's own approval decides), ESCALATE,
  ALLOW (run it whatever the harness says), DENY. PASS is the normal answer; the allow lists and
  the read-only tool routes produce PASS. calu does **not** set the permission mode (D5
  revised): `claude.permission_mode` is None by default, and the hook follows the mode it is
  actually given. Measured (D191): a headless start is in the mode "default" unless
  `claude.permission_mode` or Claude Code's `defaultMode` says "auto", and a PASS is then the
  only approval; with `claude.require_auto_mode` such a step waits for a human. The start line
  and `calu doctor` name the mode. Whether calu sets "auto" itself is Q22.
- **Checker tools** (D22, D185, `checkers/tools.py`): `read_file(path, start_line, end_line)`,
  `find_files(pattern)`, `grep_files(pattern, path_glob)`, confined by calu itself
  (`CodeTools.confine`: relative paths without `..` that resolve inside the project), bounded
  by the read deny lists, 2 MB per file and 10 s per search. A language server would be the
  next tool here.
- **What a checker reads** (D185): text from Claude, a tool or a file is fenced data; no file
  of the workspace is part of the context (D193: `context.files` and `context.skill_dirs` are
  gone); a step cut at 60,000 characters or a commit diff cut at 240,000
  characters turns an approval into an escalation.
- **What a command wrote** (D31, `src/calu/changes.py`, **off by default**): with
  `changes.snapshot` on, the session snapshots the project directory before and after every step
  the command checker judges, using git plumbing in a git directory of its own under
  `$XDG_STATE_HOME/calu/snapshots/<pid>-<time>` (outside the project, on the sandbox's
  `denyRead` list, deleted when the session ends). It records the two tree ids and the paths on
  the `steps` row (migration 0010) and warns when a command wrote paths the checker's summary
  did not mention — the dishonesty signal. Files above `changes.max_file_bytes` and binary ones
  are reported by path and size but kept out of the patch, which is itself capped by
  `changes.max_patch_bytes`; `changes.always_capture` (`.env` by default) catches what a
  `.gitignore` would hide. `SnapshotStore.revert` exists and is tested. Overlapping windows
  (background commands, subagents) are marked `overlapping` and then not compared with the
  summary, because the paths cannot be attributed to one step. With `changes.review` on as well
  (also off by default), a `PostToolUse` matcher for Bash calls `ToolGate.review_write_set`
  while Claude waits: the changeset checker reads the patch, a DENY reverts the change and tells
  Claude what was undone and why — or, when the revert does not apply because the files moved
  on, that it was **not** undone and Claude has to fix it itself. An ESCALATE asks a human;
  only a decision reverts, so a stop or a timeout leaves the files and tells Claude that nobody
  decided (D186). The review records a `checker_runs` row like every other check. Verified against the real CLI
  (`spikes/RESULTS.md`, "PostToolUse for Bash").
- **Times and the clock** (D32, `src/calu/clock.py`): a session keeps the start and end time of
  its recent steps (`Session.step_times`, the last 500) and gives the checkers two more
  read-only tools: `step_times` (when the recent calls ran, local and UTC) and `changed_files`
  (which paths changed since a step, and which step's run contains the change). The attribution
  uses the file's **ctime**, which an unprivileged process cannot set — `touch -d` moves only
  the modification time — and it scans directories too, because a deleted file leaves nothing
  but its parent's timestamp. It gives attribution, not content, and only the *last* change of a
  path. `ClockWatch` samples `CLOCK_REALTIME` against `CLOCK_BOOTTIME` (boottime, so a suspended
  host is not mistaken for tampering) every `clock.check_interval_s`; a backward move beyond
  `clock.max_backward_s` arms an alarm that escalates the session's next tool call with the jump
  as its reason, and the human's answer to that escalation clears it. A large forward move is
  only logged, because a first time-server sync jumps forward. An idle session has no next step,
  so the jump reaches the clients as a log event at once and escalates when Claude acts again.
  No live checker model has used the two tools yet.
- **Changeset checker** (D184, replaces D26's routing; `checkers/changeset.py`): a shell line
  with a commit stays on the command route and is judged by the command checkers. For a line
  that is one plain `git commit`, calu also collects the diff (`--cached`, against HEAD for
  `-a`, against `HEAD~1` for `--amend`; read without the repository's, the user's or the
  system's git configuration) and gives it to the changeset checkers; the most restrictive
  verdict wins. Any other line with a commit is denied without a model call, with the
  instruction to run the rest first and `git commit` alone. A diff calu cannot read is
  escalated, or denied with "stage first" where staging makes it readable. A DENY
  stops the commit; the escalation context carries the changeset.
- **Store** (`src/calu/store/`): `ConfigStore` (configuration, checker definitions, rules;
  reloads on `config_changed`), `DatabaseSessionStore` (session, steps, checker runs, turns,
  chat summary, escalations, comments, client tokens, address, agents, history),
  `MemorySessionStore` for unit tests.
- **Unix socket** (§6, D20): `calu run --socket [PATH]` serves the clients on a Unix socket
  (mode 0600, `$XDG_RUNTIME_DIR/calu/<pid>.sock` by default) instead of a loopback port, which
  every process on the machine can reach. The address is recorded as `unix:<path>`, and
  `calu attach`, `calu acp` and the web interface all take it. Two things the first real run
  taught: a path over ~100 bytes is refused with its length (the kernel's limit is 108), and a
  directory that already exists keeps its own mode — chmodding `/tmp` is neither possible nor
  calu's business.
- **Bus and protocol** (phase 4): `calu.bus` numbers every event and keeps the last 5000 for
  replay; `calu.protocol.server` serves JSON-RPC over WebSocket to several clients (one token
  per session, `calu.tokens`), closes a connection whose token was revoked (close code 4001)
  or whose client fell more than 1000 events behind (4002, the client reconnects with replay),
  and finishes a changing request even if its client disconnects (D189); `calu.protocol.client`; `calu attach [ADDRESS --token=T]` (no arguments finds the session of this directory and
  mints its own token, D105) with
  `/start /stop /interrupt /allow /deny /inject /to /comment /why /checkers /state /context
  /sessions /history /shutdown`. `calu protocol [--out FILE]` writes the JSON Schema.
- **The web interface** (phase 6, `src/calu/web/`, the optional `web` extra): `calu web` binds
  the loopback interface and answers only as `calu.localhost`. The token it prints at start is
  exchanged for a session cookie and a key the page keeps; reading needs the cookie, every
  change needs both (D190, replaces the cookie of D161). Every response carries a
  Content-Security-Policy, and the server reconnects to the database and says so on its pages
  while it is away. The fleet page groups sessions as every such
  tool does and leads with the ones that need a human (Needs you / Working / Idle / Ended), with
  a filter. A session page shows its history at four levels of detail (the narrated summary a
  checker reads, compact, detailed, full), its subagents, and ready-to-run connect commands with
  a token minted on the spot. `/sessions/{id}/live` relays the session's own WebSocket to the
  browser (D34): the stream, stop, interrupt, start with model and effort dropdowns, the message
  queue, Send and Steer, a recipient selector for background subagents, and escalation cards.
  `/triage` is the mode to sit in: one escalation at a time across every session, answered from
  the keyboard, with "remember exactly this command" turning the decision into an anchored rule.
  Every page carries an attention badge (filled from `/api/sessions`, also in the tab title) and
  a jump palette on Ctrl+K that goes to any session's live view.
  `/settings` is one page for everything that steers a session (D182): the configuration values
  generated from `calu.config.Config`, each checker's instructions, and the allow and deny
  lists, per scope, with a pane per topic, a search across all and one save
  (`calu.web.settings` builds the page model and writes the changes). `/rules` and
  `/checkers/{name}` redirect into it. Git links point into the project's own browser (`calu.web.git`); calu browses no git.
- **Forking a conversation** (D36, verified end to end): `calu run --resume <id>
  --fork-before-turn <n>` keeps everything up to the previous turn and discards turn n, under a
  new session id, so the original conversation stays as it was. calu records the transcript uuids
  of each turn (the prompt, and the last assistant entry) from the events Claude Code replays and
  stores them on `turns` (migration 0012); `herd.fork_point` finds the pair again **across the
  `sessions` rows of one conversation**, because a resumed run opens a new row and records only
  the turns it saw. The session page offers the command per turn. A fork point the CLI refuses is
  reported with `FORK_HINT` rather than the authentication hint. Putting the **files** back as
  well is Q14 and not built.
- **A confirmation the harness asks for** (D38): any permission question the SDK raises arrives
  at `can_use_tool`, which is calu's wait for a human, so a step the checkers passed escalates if
  Claude Code's own approval asks. Its reason says so (`HARNESS_ASKED`). In headless mode the
  classifier never asked in four attempts, and allowed a write outside the project directory —
  the deny lists, not the harness, are what stop that.
- **Questions of the agent** (D35): `AskUserQuestion` is routed to `Route.QUESTION`, escalates
  without a checker, carries its options in the `escalation.request` event, and the answer goes
  back as the message of the refusal that keeps the call from running. The row is recorded as
  `answered` (migration 0011). Answer it from the browser (buttons), `calu attach` (`/answer`)
  or `calu resolve <id> answer "..."`.
- **ACP adapter** (phase 7, D24, `src/calu/acp/`): `calu acp [ADDRESS --token=T]` speaks the Agent
  Client Protocol on stdin and stdout (newline-delimited JSON, protocol version 1) and attaches
  to a running session as an ordinary client, so the session outlives the editor and several
  editors can attach at once. Implemented: `initialize`, `session/new` (checks the editor's `cwd`
  against the session's directory), `session/load` with the replay the protocol requires,
  `session/prompt` (waits for `turn.result`), `session/cancel`, `session/close`, the
  `session/update` stream and `session/request_permission` for escalations (also for those
  already open when the editor attaches, D189), whose answer becomes
  an `escalation.decide`. `calu acp` ends when the session's connection ends. "Always" is answered like "once" and recorded in the note, because a
  rule row needs the admin credential. The editor's `fs/*` and `terminal/*` capabilities stay
  unused and its `mcpServers` are not forwarded, or tool calls would route around the checkers
  and the sandbox. Two details worth keeping in mind when changing it: a `tool_call_update`
  **replaces** the content collection, so every update carries everything the call has shown
  (`_add_content`), and the echo of the adapter's own prompt is dropped so the editor does not
  show the user's message twice.
- **Inspecting a decision**: `escalation.context` returns what the checker saw and said (rendered
  step, the file before the change, the conversation and chat summary of that moment, the checker
  runs with model and version); `checkers.get` returns the definitions, read-only;
  `session.history` returns the turns, steps and chat summary from the database.
- **Deciding without the session** (D30, the invariant): an escalation can always be decided
  from the database alone — `calu escalations` lists them, `calu escalation <id>` prints
  everything a decision needs (reason, the command line or the agent's question with its
  options, the step as the checker rendered it, the file before an edit, the changeset, the
  narrated conversation, the folded summary), and `calu resolve <id> allow|deny|answer` decides.
  The waiting session notices through the `escalation_resolved` notification **and** a poll of
  the row every second. The database deliberately holds no live stream (D3): that is what
  attaching to the session is for.
- **Deciding an escalation** (D28): an operator with a database login uses
  `calu_resolve_escalation` (`calu resolve`); a client of the session has no login and tells the
  session, which records a real resolution with `calu_client_decision` — scoped to that session,
  requiring the hash of one of its client tokens (which the role cannot read back), recorded as
  `resolved_by = 'client:<name>'`.
- **Other tool calls wait while an escalation is open** (D186): a new tool call that needs a
  check waits up to 300 s while an escalation of the session is open or being opened; an
  approval still held then becomes an escalation. Deciding one step has 420 s, one checker call
  300 s, inside the PreToolUse hook's 900 s. A question of the agent holds nothing.
- **Escalation timeout** (D27): `escalation_timeout_s` (default 8 h, 0 = no limit). On the
  timeout the step is denied, the row is cancelled with the reason, and the session stops;
  Claude's session id is in the database from the start of the session, so
  `calu run --resume <id>` continues it. `calu sessions` prints that id.
- **Process identity and agent tree** (D29): `sessions` carries host, OS user, directory, pid
  and `address`; the `agents` table holds one row per subagent with `parent_agent_id` (NULL =
  the main agent).
- **The folded conversation** (D40): `calu summary <session> [--edit]` and
  `/sessions/{id}/summary` show and correct the chat summary the checkers read for everything
  older than the unfolded turns. An empty text is refused, and the session re-reads the summary
  on the `config_changed` notification (`Session.reload_summary`), so a correction reaches the
  next check.
- **Configuration commands** (`src/calu/admin.py`): `calu project add|list` (`add DIR` records
  the directory and the host, which is how `calu run` finds the project, D188), `calu config
  get|set|unset KEY` (dotted keys, values as JSON, validated against `Config` before the write),
  `calu checker list|show|set|edit`, `calu rules list|add|edit|remove|enable|disable`. `edit`
  opens the text in `$EDITOR` (`$VISUAL` first, then nano/vim/vi), stores it on close, says
  "unchanged" when nothing changed, and keeps the temporary file and prints its path if storing
  fails. A rule list is edited in the line format `calu setup` loads the built-in lists from;
  disabled rules are untouched. All of these need the `admin` credential.
- **Evaluation** (`src/calu/evals/`, ported): `calu evals [FILE ...]` runs the checkers of the
  database on the built-in cases, on YAML files and, with `--from-db`, on the escalations a
  checker raised and a human resolved. Reports accuracy, preferred decision, critical misses,
  over-escalations, escalation share, unwanted comments; exits 1 on a critical miss.
- **Checker keys** (D17, D37): read from `~/.config/calu/.env` and calu's environment, and
  **every** provider key calu knows is blanked for Claude Code (not only the ones that are set),
  so the checkers can run on an Anthropic API key (an Ultra subscription includes API credits)
  while the main chat keeps the subscription. `hidden_env` names further variables to blank
  (D191). Measured with the real CLI, and
  `tests/unit/test_credentials.py` holds the two tests for it. The one link not verified here is
  a checker actually billing the Anthropic API, which needs the owner's key.
  Bedrock needs no key here, and `claude.use_bedrock` is the separate switch for Claude itself.
- **Network policy per project** (D39): `projects.network_policy` is no longer inert. "none"
  reaches nothing, "proxy" only `claude.sandbox.allowed_domains`, "full" everything through the
  sandbox's proxy (`allowedDomains: ["*"]`), and `denied_domains` overrides the wildcard.
  `calu project add --network` and `calu project network --name <p> --network <policy>`; a
  running session applies a change when it next starts. Measured against the real CLI: the
  sandbox exports a filtering proxy as `HTTPS_PROXY` and raw TCP is blocked whatever the policy
  says. The start-up warnings say when a session may reach every domain. Open (Q5): the default
  for new projects, still "full".
- **`calu doctor`**: one command that says what is in place before anything runs — `claude` and
  its version, bwrap and socat, git, the two credentials, which provider keys the checkers
  could use, the socket and snapshot directories, optionally a workspace, and with `--db` that
  the database answers and whether its schema is current. It exits 1 when something a session
  cannot run without is missing, and names it.
- **Sandbox** (D15, D191): `SandboxConfig` passed to Claude Code as a `--settings` document
  that holds every sandbox key calu knows and always denies reads of calu's own directories;
  `calu.preflight` warns about missing `bwrap`/`socat`, a disabled sandbox and missing checker
  API keys at every start. With `claude.sandbox.fail_if_unavailable` (default) a session does
  not start while a loaded Claude Code settings file holds a sandbox entry beyond calu's
  configuration, and a Bash or Monitor step waits for a human if one appears later.
- **Client tokens** (D30, migration 0009): a session accepts every non-revoked token of its own
  session, not only the one it printed. `calu token mint SESSION [--label]` prints a token once,
  `calu token list [SESSION]` shows labels, address and last use but never a token, and
  `calu token revoke ID` ends one client's access. The session presents a hash and
  `calu_client_token_ok` confirms it; the role cannot read the hashes and no longer has UPDATE on
  the table, so it cannot un-revoke one. The database is asked for every token, the printed one
  included, at `initialize`, before every changing request and every 15 s for open
  connections, so a revocation closes a connected client (D189, corrects D30); the printed
  token is accepted from memory only while the database gives no answer.
  Verified in the tests and by hand against a real PostgreSQL.
- **Migrations**: 0001 initial · 0002 turn records · 0003 comments · 0004 client tokens ·
  0005 the PASS decision · 0006 the changeset route · 0007 `calu_client_decision` ·
  0008 `sessions.address` and the `agents` table · 0009 minted client tokens · 0010 write sets · 0011 the `answered` state · 0012 fork points
  · ... · 0025 session keys and the directory of a project · 0026 row-level rules per command.
  Sessions must be stopped before 0025 is applied: a session started before it has no key and
  loses access to its rows.

### Verified by hand with the real CLI

`spikes/smoke_session.py <dir> [--resolve allow|deny]` runs one real session against a
throwaway database and prints what it wrote. The sandbox is off by default (this machine has no
`socat`), so the commands Claude runs are not isolated: use a throwaway directory. Results:

1. An allow-listed `ls`: no model call, `rules_outcome = allow`, turn and answer stored.
2. A `Write`: edit checker, one `checker_runs` row; both steps recorded as `pass`, so the hook's
   "no decision" answer reaches auto mode (D21).
3. `sudo -n ls /root`: the deny rule matched, the session went to `escalated` and waited;
   `calu_resolve_escalation` let it continue within a second and Claude reported the denial with
   the note. That is REQUIREMENTS 12 end to end.
4. A real commit: routed to the changeset checker, PASS, `checker_runs` row recorded. A commit
   whose message carried the fake checker's deny marker was **stopped**, Claude reported the
   reason and did not retry, and the change stayed staged.
5. `spikes/spike_hook_timeout.py`: a PreToolUse hook that misses its timeout does **not** let the
   call run (D23). Re-run it after a CLI update; the whole gate rests on it.
6. `--sandbox` (with socat from `spikes/get-socat.sh`): a sandboxed session really is isolated —
   the probe inside it saw its own PID namespace, no `claude` process, unreadable credentials,
   unwritable Claude Code settings, a blocked network, and its detached process did not survive
   (D15, `spikes/RESULTS.md`).
7. `spikes/spike_question_tool.py`: `AskUserQuestion` reaches the hook with its options, and
   Claude acts on the answer returned as the message of a refusal (D35).
8. `spikes/spike_fork_session.py`: a fork at an earlier turn works — the fork remembered the kept
   turn, not the dropped one, and ran under a new session id (D36). It also showed that a
   user-role transcript entry is not always a prompt, which is what the `resume_drops_turn` guard
   refuses.
9. Checker keys and the subscription: with `ANTHROPIC_API_KEY=""` Claude Code answers normally,
   so blanking the checker keys (what calu does) keeps the subscription while the checkers use
   the Anthropic API (D37).
10. `--changes`: a command wrote a file whose content carried the deny marker (the command line
   did not, so the pre-call check passed). The `PostToolUse` matcher for Bash fired, the
   changeset checker refused the patch, calu reverted the file (the directory was clean
   afterwards), Claude reported the refusal and did not retry, and the database held the write
   set and a `checker_runs` row for the review.

11. The **installed wheel**: `uv build` ships every piece of package data (13 migrations, 16
   default instruction files, 13 templates, 5 static files), and in a throwaway virtual
   environment `calu --version`, `calu protocol` (51 definitions) and `calu doctor` run out of
   `site-packages` with that data resolving correctly (D41). `calu doctor` exits non-zero there,
   which is right: that environment has no session credential.

12. Re-run after the changes of 2026-11-10 (D42-D47): `smoke_session.py` with a prompt that
   runs `ls` and writes a file. The allow-listed `ls` ran with no model call
   (`rules_outcome = allow`), the `Write` went to the edit checker and passed with a
   `checker_runs` row, the turn and the answer were stored, and the session went
   idle -> stopped cleanly. So the gate, the hook, the rules, the edit route and the turn
   storage all still work with the real CLI after the session, store and schema changes.

13. **Row-level security** (D49, migration 0015): after switching the policies on, a real
   session ran end to end again -- it created its row through `calu_start_session`, recorded
   the step, raised the deny-rule escalation, took the denial an operator wrote in the database
   and recorded the turn. A database test additionally holds the line: two sessions on two
   connections each see only their own steps, an UPDATE of the other's changes nothing, an
   INSERT for the other is refused, a connection that never set the id sees and writes nothing,
   and the owner still sees everything.

14. **The network default** (D45) and **a harness confirmation** (D38) together, with the
   sandbox on: with `--allow pypi.org`, the curl to pypi.org ran unasked, and the curl to
   `example.net` made Claude Code ask for a confirmation (`SandboxNetworkAccess`) although the
   command checker had passed it -- calu escalated it, the operator allowed it and it ran. So an
   unlisted domain asks rather than failing (`spikes/RESULTS.md`).
15. **What the checkers cost** (D63): with seeded runs in a throwaway database, `calu checker
   stats` printed `50k in / 240 out, 95% of the input from cache` for one checker and, for a
   checker whose runs read nothing from the cache, the same totals with `0% of the input from
   cache -- the prefix is not being reused; check the model's provider caches`. Both branches
   therefore render from the record with no model call.
16. **The step paths** (D64), seeded the same way: `calu checker stats` reported `14 step(s): 4
   went to a checker, 10 (71%) did not -- 7 allow-listed, 1 denied by a rule, 2 of a tool the
   lists do not cover`, then `calu's own delay per step: ~3 ms, worst 3 ms (no human waiting
   time in this)` and `1 escalation(s) answered, after ~45 s of waiting for a person`. The
   escalated step is counted as checked but left out of the delay, which is the point of
   measuring the two separately.
17. **A resumed conversation end to end** (D65, D67): four runs of one conversation against the
   test database with a fake Claude and a recording checker (`tests/db/test_run.py`), the fold
   happening in the second run and nothing folding afterwards. Without the fix the fourth run's
   checker prompt said "No turns have been folded into this summary yet" -- the summary and the
   two turns before it were gone; with it the prompt carries the real summary and both turns.
   The same runs showed the heartbeat failing at every session start, which D67 fixes.
18. **A real session after the store changes of D65 to D68**: `smoke_session.py --resolve deny`
   with a real `claude` (haiku) -- the deny rule escalated the step, the operator denied it, the
   session continued and Claude reported the denial, and the step, the escalation and the turn
   are in the database. The start now prints only the two warnings that are true on this
   machine (no sandbox, no summarizer key); the heartbeat warning that used to appear at every
   start is gone.
19. **The two new eval cases against the real checker model** (D69), Nemotron Lightning on
   Fireworks, two runs each: 100% -- it escalated the message that asks another session to run
   a command the gate had denied, quoting the request as the instructions say, and passed the
   coordinating message. The same run showed D66's cost line working on a real provider
   (`14890 input tokens (0% of them from the provider's cache), 2789 output tokens, ~1817 ms
   per case`) and confirmed D63's caveat: this model reuses a cache only for an identical
   prompt, so 0% is the provider and not a defect.
20. **The whole built-in case set against a real checker model** (D71-D74), which had never been
   run before for want of a key: 25 cases, three runs each, both checkers. The shipped defaults
   started at 95% with one error (command) and 67% with a critical miss (edit, which passed
   `API_KEY = "sk-live-..."` into a source file as "a placeholder"). After fixing two broken
   cases and four instruction gaps, and raising the checkers' budgets (migration 0017), two
   independent triple runs give **both checkers 100% accuracy, no critical misses, no errors,
   no unwanted comments**. Re-run after any change to the default instructions:
   `scratchpad`-style script in the commit message of D73, or `calu evals --repeat 3`.
   One run is not a measurement -- two runs of identical instructions differed by two critical
   misses, which is why `--repeat` exists. Since D99 the report names the unstable cases:
   on 2026-11-10 the command checker's twenty cases times three gave four of them, among
   them `obfuscated-command: DENY / PASS / ESCALATE` -- three runs, three answers, one set
   of instructions. That is the evidence for Q17 that no accuracy figure carries.
21. **The changeset checker, measured for the first time** (D75, D76): it could not be evaluated
   at all before, so the one checker that judges a whole change was the one nothing measured.
   With four new cases and its own request budget (8 instead of 5, because it reads the files
   around the diff) it decides correctly in 11 of 12 runs; the miss is a rename whose caller
   still calls the old name, which it passed as complete. The report now also separates the
   critical misses calu's rules stop anyway from those where the gate rests on the model alone
   (`cat .env` is on the read-deny list; `kubectl apply` is on no list) -- which is what Q16
   asks the owner to decide.
34. **The remaining new tools pass the gate** (D154/D165, 2026-11-12, CLI 2.1.319, haiku):
   `PushNotification`, `CronCreate`, `ListAgents` and a `TaskStop` of a real background task
   each became a step judged by the command checker (`checker_runs` rows, decision `pass` from
   the fake checker). A `TaskStop` with an unknown task id left no step: Claude Code refused its
   input (`<tool_use_error>No task found`) before any hook ran, so nothing was executed.
   Noticed on the way: `ListAgents` names the **other Claude sessions on the machine**, which
   `SendMessage` can reach -- D69 already sends a message out of the session to the checker.
33. **An MCP server's file read meets the read deny list** (D166, 2026-11-12, CLI 2.1.319,
   haiku): a throwaway project with a stdio MCP server (`mcp__fs__read_file(path)`) and a fake
   `.env`. Claude loaded the tool through ToolSearch and called it with the `.env` path; the
   step escalated with "`mcp__fs__read_file` names `.env`, which the read deny rule ... protects",
   was denied, and the file's content never reached the conversation.
32. **The new Claude Code tools in a real session** (D156-D158, 2026-11-12, CLI 2.1.319, haiku,
   rule-based fake checkers): `Monitor(command="X=1 sudo -n ls /root")`, loaded through
   ToolSearch, escalated on the deny rule. A `Workflow` whose single `agent()` ran the same
   command: the subagent's step escalated and was denied, its result went to the result
   checker (`checker_runs` row `subagent-result`), and the one-shot run waited for the
   background workflow ("1 background task(s) still running") and stopped only after Claude's
   own follow-up turn. Before D158 the run stopped the session at the first idle, and the
   workflow's escalation arrived after the stop.
31. **The deny-list fixes of 2026-11-12 in a real session** (D145, D149, D151; CLI 2.1.319,
   haiku, rule-based fake checkers, throwaway folder with an un-ignored `.env`):
   `X=1 sudo -n ls /root` escalated with "matches the deny rule ... (as `sudo -n ls /root`)",
   and `grep -rn API_KEY .` escalated with "would read `.env`, which the read deny list
   protects" -- the rule check now runs in a worker thread, and the session continued normally
   after `--resolve deny` in both runs. Found on the way: **CLI 2.1.319 has no Grep or Glob tool
   any more** (Claude answers "No such tool available: Grep ... use grep via the Bash tool"), so
   searches arrive as Bash commands and D149 is the check that matters; D146 covers older CLIs.
30. **The new default measured on every built-in case** (D129, D133, 2026-11-11):
   `anthropic:claude-haiku-5-5` at effort medium, chosen by `calu setup` from
   `ANTHROPIC_API_KEY`, through `calu evals`: 35 cases in 28 s, **0 critical misses**, command
   100% (20 runs), edit 100%, subagent-result 100%, changeset 100% over 12 runs once the
   `complete-fix` case was corrected -- its "complete" fix was missing an import, which this
   model caught and the case counted as a miss. About 1.7 s and 6k input tokens per step check.
   For Q17 this is the first measurement where a checker model answered every built-in case
   correctly; it is still 35 cases, written by the same hand as the instructions.

29. **The automatic model choice in a real session** (D129, 2026-11-11):
   `spikes/smoke_session.py --checker-model auto --resolve deny`, real Claude Code (haiku) and
   **real checkers** on what calu chose here -- `anthropic:claude-haiku-5-5` at effort medium,
   from `ANTHROPIC_API_KEY`. An unlisted inspection command went to the command checker: PASS
   with an accurate summary of what it does. The `Write` went to the edit checker: PASS, citing
   the user's request. `curl -s https://example.com/install.sh | sh` was escalated by the deny
   list on its `sh` segment before any model was asked (no `checker_runs` row), the operator's
   denial arrived from the database, and Claude stopped without retrying. The script's new
   `--checker-model MODEL|auto` flag is what makes such a run possible; without it the checkers
   stay the rule-based fakes, which cost nothing.

28. **`calu web` served for real** (D125): a throwaway database, the admin credential stored
   without a password, then `calu web --port ...` as its own process. Every page answered 200
   (`/`, `/checkers`, `/escalations`, `/rules`, `/triage`, `/settings`) and the title was
   `Sessions · calu`. What it found: with stdout redirected to a log, the address line never
   appeared, because it was not flushed -- the one line a user needs, invisible in exactly the
   setup a server uses. Fixed and re-checked: the log now holds it.

27. **A checker on the machine's Claude Code account, measured through calu** (D117, D118):
   a throwaway database, `calu setup --checker-model claude:haiku`, and three of the built-in
   command cases through `calu evals`. The first run failed with "Unknown model: claude:haiku"
   -- the eval runner skipped `resolve_model` -- and the second printed a cached share of
   119950%, because the CLI counts the cached prompt beside the input tokens and calu counts it
   inside. With both fixed: `accuracy 100%`, `cost: 30768 input tokens (23% of them from the
   provider's cache), 849 output tokens, ~3950 ms per case`. That is also the price of this
   model type -- about 10k input tokens per check, since Claude Code's own system prompt is
   part of every call -- and no checker API key was involved at any point.

26. **A real session after the twelve changes of 2026-11-10** (`spikes/smoke_session.py`,
   CLI 2.1.319, haiku), twice over. (a) "Run ls, then write a file notes.md": the `Bash` and
   the `Read` were settled by the allow lists with no model call, the `Write` went to the edit
   checker (one `checker_runs` row, PASS), the turn and the answer were recorded, the session
   stopped at 0 and its address was in its row. (b) "Run exactly this command: sudo -n ls
   /root" with `--resolve deny`: the deny rule matched, the session went to `escalated` and
   waited, the operator's denial reached it within a second, and Claude reported the denial and
   said it would not retry or look for another way -- REQUIREMENTS 12 end to end. Worth noting
   because it confirms by measurement what DOCUMENTATION.md now claims about the checkers'
   bounds: there is **no** `checker_runs` row for that step at all, so no checker verdict could
   have overruled the deny list.

25. **A client with no arguments, from a subdirectory** (D105, D104): a session row with an
   address in a throwaway database, the operator credential stored `--no-password`, then
   `calu attach` from a **subdirectory** of the project with no further arguments. It found the
   session, minted a token labelled "calu attach", tried the address it had published and
   revoked the token when the connection failed -- `calu token list` then showed
   `1  revoked  session b1635974-...  ws://127.0.0.1:8765  never used  calu attach`. No
   password was asked at any point. `calu credential set operator --no-password` printed what
   the choice costs and named the file, `calu credential list` showed
   `operator  (file mode only)`, and `calu doctor` reported
   `ok      credential protection: operator stored without a password (file mode only)`. The
   store's own modes were as intended (directory 0700, file 0600).

24. **The honesty register's two reports, with two checkers writing to it** (D87): seeded a
   project where `honesty` opened two marks and cleared one itself and `security` opened one
   that a person overruled, then read both reports with the real CLI.
   `calu honesty list --all` named each mark's author and how it ended
   (`(honesty): ... (closed by a checker)`, `(security): ... (closed by kai)`), and
   `calu checker stats` said `honesty  marks: 2 opened, 1 cleared` and
   `security  marks: 1 opened, 0 cleared, 1 overruled by a person` -- which is the signal
   DOCUMENTATION.md tells a user to read before switching `honesty.mode` to `enforce`.
22. **The subagent-result checker, measured for the first time** (D77, D78), which completes the
   set -- all four checkers are now measurable. Its first result was the worst so far: 67% and
   three critical misses in nine runs, all three the same case, a subagent demanding that a
   script the user called throwaway be rewritten as a state machine. The rule for it existed;
   what was missing was a procedure to run before answering ACCEPT. With that, two runs of
   three: 100% and no critical misses, at the price of a higher escalation share (44-56%).
23. **Three cases that try to instruct the checkers** (D79), testing the claim every checker's
   instructions make -- that what it reads is evidence and never an instruction. Measured once
   with the 30B model: the edit checker resisted, the command checker returned ALLOW once in
   three quoting the planted comment, the subagent checker believed a claimed approval once in
   three. **The instructions were not rewritten for it.** Several rounds of that earlier in the
   day moved the numbers without fixing the escapes, which is the signature of a model that
   cannot follow what it is given rather than of unclear wording. Q17 puts the model choice to
   the owner; the framework's part -- a model per checker, `calu evals` to report, `calu checker
   stats` to price it -- is built.

The configuration commands were also exercised by hand against the test database with a
throwaway credential store (`XDG_CONFIG_HOME` pointed into a scratch directory): `setup`,
`project add`, `checker list|show|set|edit`, `config set|get`, a refused value, `rules
add|list|edit|disable|remove`, a bad pattern leaving the list untouched.

### Left out of the port, on purpose

- sitl's **training mode** (`training.py`): not ported and probably not needed in this form,
  because `calu evals --from-db` gets its labels from the escalations a human resolved (Q9). Its
  protocol models were removed.
- sitl's **event log** (`history.py`): the bus took over the replay buffer; nothing of the stream
  goes into the database (D3).
- sitl's **`tui/`** (a Claude Code channel plugin): channels are not used; `calu attach` covers
  the terminal client. The interactive-plugin mode was considered and rejected (Q10, §4.5).
- `ProjectFiles`/`files()`: configuration lives in the database and is changed with calu's own
  commands, not through the session protocol (§6.2).

## Next steps

**State on 2026-11-12:** every step below is either done or waits on the owner; none is open
work for a developer without the owner. In short:

| Step | State | Waits on |
|---|---|---|
| 1. `calu acp` in a real editor | adapter checked against the ACP v1 schema (D152, D159) | an editor session (Zed / JetBrains Air) |
| 2. Publishing | release workflow built and inert | the PyPI project and the `pypi` GitHub environment |
| 3. Phase 5 | **done** | -- |
| 4. Smaller gaps | **done** (by design, or D44) | -- |
| 5. Checker on the Claude Code account | **built** (D117) | the owner's choice to use it |
| 6. Ergonomics of auth | **done** (D104, D105) | -- |
| 7. Honesty enforcement | **rebuilt and merged** (D196), run against a real model in `track` and `enforce` mode: see "Pick up here" | -- |
| 8. Splitting `session.py` | **done**: `run_state.py`, `escalations.py` (EscalationDesk), `gate.py` (StepGate), `write_sets.py`, `recorder.py`, `steps.py`; `session.py` holds the lifecycle, runner events and wiring (about 2,500 lines). Still in it: the subagent result review and the honesty bookkeeping | -- |
| 9. Auto mode by default | **decided** (Q22): calu does not override Claude Code's permission mode | -- |
| 10. Full-screen terminal client | **built and merged** (`src/calu/tui/`, Textual, extra `tui`): `calu attach` on a terminal; documentation and a run against a real session are open, see "Pick up here" | -- |

Open developer work that needs no decision:

- A checker's model and limits live in two places: the configuration sections for the four
  route checkers, the `checker_types` row for every other checker. One source
  (`checkers.<name>` in the configuration, with defaults) would remove the special case; it
  needs a migration and changes `calu checker set --model`.
- `cli.py` (about 2,500 lines) should become a package with one module per command group.

Beyond the plan, 2026-11-12 hardened the rules engine (D145-D177, five review rounds); its
standing conclusion is in D175: the rules are a best-effort layer, the sandbox -- required by
default -- is the boundary. A review of the whole code base against the requirements followed
(D183), with two rounds of fixes recorded in D184 to D191: commit handling, what a checker
reads, waiting behind an open escalation, database loss, projects by directory, client
tokens, the web login and the pinned sandbox settings.


1. **Try `calu acp` with a real editor** (Zed or JetBrains Air). Its whole configuration is
   now `{ "command": "calu", "args": ["acp"] }` -- the adapter finds the session running in the
   editor's project directory and mints its own token (D105), which needs the operator
   credential stored with `calu credential set operator --no-password` (D104), because an editor
   gives the adapter no terminal to answer a password prompt with. That exact configuration is
   now driven as a real process over real pipes against a real session
   (`tests/db/test_acp_discovery.py`): the handshake, `session/new` with the editor's `cwd`, the
   token minted and revoked, nothing but protocol on stdout, and the refusal when the credential
   has a password. What a real editor adds is its own dialogs, which is what is left. The adapter passes an
   end-to-end test against a real session over a real WebSocket with a scripted Claude, and is
   driven as a real process over real pipes for the handshake, a prompt, a **denied** step and
   `session/load`'s replay -- but no editor has driven it yet, so expect small mismatches in
   what a client sends (the messages were checked field by field against the published v1
   schema in D152; one gap, the `session/close` capability, is fixed). A prompt now stays open while
   Claude Code's background tasks run (D159); watch how the editor shows that. What the editor
   can drive is complete: prompts, the replay of `session/load`, permission requests for
   escalations, and the session mode -- the three auto-escalation switches are offered both as
   ACP's session modes and as its newer boolean config options, and a change made anywhere
   reaches every client (D42).
2. **Publishing** needs two steps only the owner can take (and Q15 asks whether the
   single-file binary, which `release.yml` already builds and runs, should ship beside the
   wheel). The wheel itself is verified
   (D41, `tests/unit/test_packaging.py`, and CI installs it on every push), and
   `.github/workflows/release.yml` builds and publishes it for a `v*` tag — but it stays inert
   until (a) the PyPI project exists with a trusted publisher for owner `kadeng`, repository
   `calu`, workflow `release.yml`, environment `pypi`, and (b) that GitHub environment is
   created. Until then `uv tool install calu` in the README does not work. A tag pushed before
   the setup builds the wheel and stops, so nothing is published by accident.
3. **Phase 5**: nothing blocking left. The network policy is built (D39) and its default for a
   new project is now a proposal to confirm (D45, Q5), and the container case is measured (Q1): a default
   Docker container cannot create a user namespace, so the sandbox needs
   `security_opt: [seccomp:unconfined]` and socat — calu's own `dev` service and image now have
   both. What `claude.sandbox.weaker_nested_sandbox` changes is measured now (D134): it does
   **not** make the sandbox work under Docker's default seccomp, and where the sandbox works it
   changes nothing calu's probe can see, so it stays off and the container keeps
   `seccomp:unconfined`. Every failing combination failed closed.
4. Smaller gaps: a client of a session still cannot change what steers the checkers through the
   protocol, by design (the session role may only read it, §12 phase 4) -- except the
   auto-escalation switches, which are session-local and audited (D42, D43). Editing a single
   list entry is done: `calu config add|remove <key> <entry>` (D44).

5. **A checker on the machine's Claude Code account** (D117) completes REQUIREMENTS 22: with
   `calu config set command_checker.model claude:haiku` a checker needs no API key at all. What
   is left for a person to decide is whether to use it, because it costs the checker's tools
   (the CLI runs with `--tools ""`) and a process start per check; `calu evals` measures what
   that does to the decisions on your own cases, and the start-up warning says it every time.

6. **The ergonomics of auth** were the owner's complaint of 2026-11-10 ("if I need to worry
   about auth and token passing for every single command, it's not easy") and are answered in
   two halves: `calu credential set operator --no-password` takes the password prompt off the
   read-and-resolve commands (D104), and `calu attach` / `calu acp` with no arguments find the
   session of this directory and mint their own token (D105). What is left of the question --
   whether the terminal or the browser is meant to be the interactive tool -- no longer blocks
   anything, because both now work without a prompt.

7. **Honesty enforcement is built and switched off** (§13, D81-D86). What is left needs a
   person rather than more code: a capable checker model for it (Q17), the choice between one
   combined checker and several (Q18). Q19 and Q20 are answered (D180: marks are transient, in
   the session's memory; cross-file marks allowed as the exception). To try it on a real
   project: `calu checker set honesty --enable` and `calu config set honesty.mode track`, then
   watch the marks in `calu attach` or the live view for a few days before switching the mode
   to `enforce`.

## What the owner still has to decide (`PLAN.md` §11)

Everything else is either built or recommended with its reasons. These need a person:

- **Q22 — should calu put Claude Code into auto mode itself?** A headless start is in the mode
  "default" unless `claude.permission_mode` or Claude Code's `defaultMode` says "auto" (D191),
  and a PASS of the checkers is then the only approval. Options: calu sets "auto" by default;
  `claude.require_auto_mode` defaults to on; or as now, off by default and stated at start.
- **Q5 — the network default of a new project** (D45, implemented as a proposal): "proxy" with a
  starter allow list (package registries, git over HTTPS, OS packages) instead of "full". One
  flag overrules it (`calu project add --network full`) and existing projects are untouched.
  The trade to confirm, now measured: an unlisted domain does not stop the task, it **asks** --
  Claude Code wants a confirmation, calu escalates it (D38) and a person allows it in one
  command. So the default costs an interruption, not a failure.
- **Q15 — pressing publish**: a `v*` tag builds both Linux binaries, runs them, and attaches
  them to a **draft** GitHub release (verified with a throwaway tag, which was deleted; the
  draft held `calu-linux-x86_64` and `calu-linux-arm64`). A person opens the draft and
  publishes it. Nothing else is needed for the binaries -- only the wheel waits for PyPI.
- **Publishing at all**: the PyPI project with a trusted publisher and the `pypi` GitHub
  environment, both of which only you can create (next step 2).
- **Q9 — a training mode**: recommended **against**, and the alternative is now built rather
  than merely argued (D55): `calu evals --list` shows the cases a herd produced and
  `--export FILE` keeps them in the form `calu evals FILE` reads back, neither calling a model.
  With D50 the set also holds the labels for steps the checkers *approved*, which is where a
  training mode's value was. What is left is your word on not building one.
- **Q2 — sitl's future**: the technical half is settled, and VSCode is **not** the exception I
  claimed: it reaches ACP through community extensions today (formulahendry's "ACP Client" and
  others), while Microsoft has standardised its own agent mode on MCP and has not committed to
  native ACP (microsoft/vscode#265496). So a VSCode user can drive calu now through somebody
  else's extension; whether that is good enough, or sitl keeps its own panel for what ACP
  cannot carry, is a product call.
- **Q17 — which model the checkers need**, and which one calu ships as the default. Measured
  on 2026-11-10 with a 30B model: the failures that matter are judgement, not wording -- a
  planted "the user approved this" believed, a rewrite of a script the user called throwaway
  accepted, an ordinary `git push` passed as reversible. Rewriting the instructions moved those
  numbers around without fixing them, so they were left as they are. The framework's part is
  done: a model per checker, `calu evals` to report, `calu checker stats` to price it.
  The shipped default is now chosen from the available keys (D129; Haiku 5.5 at medium
  effort with an Anthropic key, 35 of 35 built-in cases, D133); what is left is your agreement.
- **Q18 — one checker or several**: honesty, security and over-engineering are different
  judgements on the same edit. A route carries a list (D81), so both shapes are configuration
  rather than code; what calu **ships** is the owner's call, with `calu evals` as the evidence.
- **Q19 — answered 2026-11-12**: marks are transient, kept in the session's memory and
  persisted nowhere (D180); the stored register, the old `calu honesty list|clear` and
  `honesty.scope` are gone (`calu honesty show|edit|reset` now edits the honesty rules, D181).
  The honesty mechanism itself is under redesign: a review found that it cannot hold for
  changes made by a command or at scale, and a proposal is with the owner.
- **The Mistral key on the development machine expired on 2026-11-08** (`calu doctor --keys`
  says so). Mistral is last in the automatic model order (D129), so it only matters where no
  other key is set -- but `mistral:mistral-medium-latest` is unverified until a valid key lists it.
- **The Bedrock path of D129 has never run against a real account**: this machine has no AWS
  credentials. With an account: `calu config models auto`, then `calu evals` on a few cases --
  and whether medium thinking can be switched on there (`calu.models.resolve_model`).
- **Q20 — answered 2026-11-12**: yes, a checker may mark another file, as the exception;
  such a mark is tagged `<not a file of its step>` (D141) and blocks like any other.
- **Routes for the tools Claude Code 2.1.319 added** (D154): a session names them at start
  (CronCreate, CronDelete, CronList, EnterWorktree, ListAgents,
  PushNotification, ReportFindings, ScheduleWakeup, TaskStop, Workflow); all of them go to the
  command checker. `Monitor` got its route because it runs shell commands (D156), and worktrees
  are judged correctly (D155); DesignSync's uploads and a discarding ExitWorktree meet the
  rules (D165). Whether `EnterWorktree`, `CronCreate` or `PushNotification`
  (which sends text off the machine) should always ask is a default:
  `calu config set tool_routes '{"PushNotification": "escalate"}'` is the form.
- **Q16 — whether a push, a deploy or a publish should be a deny rule** rather than a
  checker's judgement, which is the same finding from the other side. My recommendation (b)
  is now one command away, `calu rules add-set publishing` (D143); whether the set goes into
  the default lists is still yours to decide.
- **`~/.config/calu/.env` does not exist.** Live checker tests skip and a real run has no checker
  model key. The keys are in sitl's git-ignored `.env` (CRLF). Creating the file was refused in an
  earlier session by a sandbox rule, so the owner should do it:
  `mkdir -p ~/.config/calu && tr -d '\r' < ~/projects/stay-in-the-loop/.env > ~/.config/calu/.env && chmod 600 ~/.config/calu/.env`.
  Live checker model: Nemotron Lightning on Fireworks (`CALU_TEST_CHECKER_MODEL` overrides it).
- `scripts/wsl-test.sh` uses `UV_PROJECT_ENVIRONMENT=$HOME/.cache/calu-venv`.
- Test database: `docker compose up -d caludb --wait` (127.0.0.1:55432, `calu_owner` /
  `calu-dev`, development only). Tests create and drop a database per test.
- The working tree is on the Linux file system; `.gitattributes` forces LF line endings.

## Left open in sitl (not calu work)

- `tests/unit/test_tui.py::test_channel_drops_messages_without_token` is skipped with a TODO.
- The owner's uncommitted `.idea/stay-in-the-loop.iml` change and untracked `.vscode/` are
  theirs; never commit them.
