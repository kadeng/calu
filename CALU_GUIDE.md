# calu guide

Reference documentation for calu. [README.md](README.md) says what calu is and gets a first
session running; this file holds the detail: the full command surface, the credentials, the
configuration, the web interface, the checkers and the ACP adapter.

## Contents

- [What the checkers give you, in detail](#what-the-checkers-give-you-in-detail)
- [What you can enforce with a checker](#what-you-can-enforce-with-a-checker)
- [Code honesty enforcement](#code-honesty-enforcement)
- [calu does not replace Claude Code](#calu-does-not-replace-claude-code)
- [How it is put together](#how-it-is-put-together)
- [The models: Claude Code's account, and the checkers' own](#the-models-claude-codes-account-and-the-checkers-own)
- [The sandbox and what a command may reach](#the-sandbox-and-what-a-command-may-reach)
- [Watching and unblocking a session](#watching-and-unblocking-a-session)
- [Attached to a session](#attached-to-a-session)
- [From another machine](#from-another-machine)
- [Client tokens](#client-tokens)
- [The web interface](#the-web-interface)
- [Editors that speak ACP](#editors-that-speak-acp)
- [Checker instructions, rules and evaluations](#checker-instructions-rules-and-evaluations)
- [Resuming and forking a conversation](#resuming-and-forking-a-conversation)
- [Many sessions from one process: calu serve](#many-sessions-from-one-process-calu-serve)
- [calu as root, Claude Code as another user](#calu-as-root-claude-code-as-another-user)
- [The database](#the-database)
- [Installation and configuration reference](#installation-and-configuration-reference)

## What the checkers give you, in detail

What a checker can do besides deciding is **comment**. It can let an edit through and still tell
Claude to keep the names and the API documentation honest, or to stop widening the task. Claude
receives the remark together with the approval. A checker can also **look at the code**: read a
file, list files by glob, grep for a pattern, all inside the project directory, so that it
judges on evidence rather than on the diff alone. That is all it can do: it cannot write, run commands or talk to the outside.

- **Allow and deny lists as overrides, for the exception.** calu ships no rules: every list
  starts empty, and every step goes to a checker. A rule overrides the checkers -- a deny match
  asks a human without asking them, an allow match lets a step skip them -- and an allow rule
  matches a command by its text, whatever program that text starts, so most projects are
  better off without any. Two ready-made sets exist for fixed stops.
  `calu rules add-set recommended` makes known-dangerous commands (`sudo`, force pushes, remote
  deletions, reads of your credentials, writes to `.git`, `.claude` and `CLAUDE.md`) always ask
  a human and lets reads of project files pass; it allows no command; `calu rules add-set publishing` makes pushes, deploys and package uploads ask as
  well. With rules in place, the checks also follow their files through other spellings: a tool
  -- an MCP server's too -- that names a protected file asks, as reading it directly would, and
  so does a search of a whole folder that would read one. Independent of any list, calu always
  protects itself: Claude may not read calu's own configuration, run `calu`, or reach calu's
  database container without asking. A tool that a Claude Code update adds is checked by the
  command checker, and the session names such tools when it starts.
- **Checkers with your rules.** One checker reviews commands, one reviews edits. Their
  instructions are plain Markdown that you edit: safety, readability, documentation, and the
  rules you would otherwise have to repeat to Claude every day (see
  [what you can enforce](#what-you-can-enforce-with-a-checker)). They get the conversation in
  condensed form and the step itself, and read-only tools to look
  further when they need to: read a file or a range of its lines, find files by glob, grep for a
  pattern — so a checker can see who calls the function being changed instead of guessing.
  No file of the repository is part of what a checker is given: AGENTS.md, CLAUDE.md and skill
  descriptions can be written by the agent under review, so they are not a source of rules.
  What your project asks for -- its coding and documentation standard -- belongs in the checker
  instructions, which are stored in the database. Claude Code itself reads those files as
  always; this is only about the checkers.
  A step longer than 60,000 characters, or a commit's diff longer than 240,000, is shown cut, and a checker's approval of a cut
  step becomes an escalation. They can also ask **when** things happened: the times of the
  session's steps, and which step last changed a file. That last question is answered from the file's change time, which an
  unprivileged process cannot forge, so a command that edits more than it said it would leaves a
  trace. calu watches its own clock for the same reason: if the wall clock is moved backwards,
  the next step waits for a human, because the attribution would no longer hold. Checkers judge
  the step, not the whole program: overall correctness is the changeset checker's job, below.
- **A readable trail instead of a transcript.** Every step is summarized by the checker that
  judged it — one line saying what the step was meant to do — and the summaries go to the
  database. Supervising a fleet of agents then means reading summaries, not transcripts, and
  opening the details only where a summary looks wrong. An IDE or terminal client attaches to a
  session for the full picture, and a colleague with access can watch your session the same
  way.
- **The checker's own view, when you have to decide.** For an escalated step a client can ask
  what the checker saw and said: the step as it was rendered, the file as it was before the
  change, the condensed conversation and the chat summary of that moment, and which checker
  version and model answered. You decide on the same material the checker had, not on a
  one-line reason.
- **What a command really wrote** (optional). Claude's `Edit` and `Write` arrive with their
  exact content, so a checker sees them before they happen. A command does not: `sed -i`, a
  formatter or a script can write anything, and the checker only reads what the command *says*
  it does. Switch on `changes.snapshot` and calu snapshots the project directory around every
  command (git plumbing, in a directory of its own outside your repository), records which paths
  changed, and tells you when a command wrote files its own summary never mentioned. That is the
  honesty rule below, applied to commands instead of to code. Switch on `changes.review` as well
  and a checker reads the actual patch after the command has run, while Claude waits: if it
  refuses, calu puts the files back as they were and tells Claude what was undone and why. A
  refusal is then not a comment on something that already stands. Only a denial by the checker
  or by a human puts files back: when nobody decided (the session stopped, the escalation timed
  out), the files stay and Claude is told that the change is unreviewed. A change that calu
  cannot read completely goes to a human.
- **A review of the whole change before it is committed.** The command checkers judge a line
  with `git commit` like every other command. When the line is one plain `git commit` (`-a`,
  `--amend` and the message options are allowed), calu also holds it and gives the complete
  diff to a checker of its own — the one checker that sees the result rather than one step of
  it. It judges what the step checkers cannot: whether the change is complete (a renamed
  function with callers left behind), whether it contains work nobody asked for, whether a test
  was weakened to make it pass, whether the message describes the change. The most restrictive
  verdict wins. A denial undoes nothing; it stops the commit and tells Claude what to fix first.
  calu reads the diff before the line runs, so the diff is what the commit records only if the
  line does nothing else. A line that combines the commit with other commands or names paths
  (`git add ... && git commit`, `cd dir && git commit`, `git commit file.py`) is therefore
  refused without a model call, and Claude is told to run the rest first and `git commit`
  alone. A diff that calu cannot read is escalated to a human; for a `git commit -a` that
  changes a file with a content filter (Git LFS), Claude is told to stage the change and commit
  without `-a`. All of this applies while the changeset checker is enabled.
- **Claude's own questions reach you.** When Claude uses its question tool, calu holds the call
  and shows you the question with the options it offered — in the browser as buttons, in the
  terminal as a line. Your answer comes back to Claude as the result of that call, so the
  conversation goes on where it stopped. An unattended session that asks something simply waits,
  like any other escalation.
- **Checking subagent results.** A subagent's final result is reviewed against the
  conversation, so an adversarial reviewer subagent cannot silently widen a task that you
  deliberately kept small.
- **Claude's own words, retold.** What Claude thinks and writes reaches the checkers only in
  indirect speech, narrated by a separate model ("Claude claimed that the user had allowed
  this"), so a checker cannot mistake Claude's text for an instruction from you.
- **Comments on a selection.** Mark lines in a diff or in any project file and write a comment.
  It reaches Claude at once and is kept as potential training data for the checkers.
- **Unsupervised runs.** The checkers judge every step, and in Claude Code's auto mode its
  classifier judges it as well ([how to switch it on](#calu-does-not-replace-claude-code)), so
  a session can run for hours — overnight, for example — and stops only when something
  genuinely needs you.

## What you can enforce with a checker

Checkers are useful wherever a rule is obvious to a human reader but invisible to a linter —
because it is about intent, about prose, or about something outside the code such as a law:

- **Writing that sounds like a language model.** The mannered summaries, the rhythmic triads, the
  confident filler. A checker holds every docstring, comment and commit message to your house
  style, every time, without you having to say it again.
- **Over-engineering, while it happens.** A factory for the one case that exists, a cache nobody
  measured, an abstraction with a single implementation. The checker sees the edit that
  introduces it and can send it back with a reason.
- **Scope creep.** You asked for a bug fix and get a refactor on the side. The checker compares
  the step against what you actually asked for — it reads your messages in condensed form — and
  escalates when the two drift apart. The same check runs on what subagents hand back, so a
  reviewer subagent cannot quietly widen a task you deliberately kept narrow.
- **Rules that come from law or from a contract.** Where personal data may be processed, for
  example: if your answer is "inside the EU region of this provider", then a call that sends it
  somewhere else is a compliance incident, not a style question — and it usually enters the code
  as one plausible line in an otherwise reasonable edit. A checker that knows the rule sees that
  line while it is being written. Data residency, retention periods, which fields may be logged
  and which third party may be called are all rules of this kind.
- **Functionality that needs a human decision before it exists.** Some features are regulated
  whatever their intent: under the EU AI Act, inferring emotions, categorising people by
  biometric data or scoring them can oblige you to do things you have not planned for. Such a
  feature is easy to add as a side effect of something useful, and hard to notice afterwards. A
  checker can escalate exactly those steps, so that a person decides to build it rather than
  discovering later that it was built.
- **Scientific standards in prose.** Claims that match the evidence, hedging where the evidence
  is thin, the citation style you use, and no citation that quietly disappears in an edit.
  Documentation and papers drift this way one small rewrite at a time; the checker reads each
  rewrite.

## Code honesty enforcement

The rule that pays off most is the one that is hardest to enforce by hand: a codebase should be
**honest**. Every function, class, module and command-line tool says what it does — in its name
and in its API documentation — at a level of detail that fits, and leaves out nothing that would
surprise the reader. No side effects that the name does not suggest, no network call hiding in a
formatting helper, no "and also" that only the implementation knows about.

Honesty is usually argued for as a matter of readability. It is also a security property, and it
compounds: once the documentation of a codebase can be relied on, every reviewer — human or
model — can judge a call site without reading the implementation behind it. That is exactly what
an attacker needs to take away. A function called `hello_world(name)`, documented as greeting
someone, cannot also POST `name` to a host on the internet and exfiltrate your customer
database: the code would be dishonest, and the checker that read the diff would have said so
before it ever ran.

Honesty is hard to keep up by hand, because it costs attention on every single edit and it
decays silently. That is precisely the kind of rule an agent applies well: calu's edit checker
sees the function and its documentation together, in the same step, and can refuse the edit.

The reverse also holds: judging a call site by names and documentation is only sound for code
that was verified to be honest. So calu tracks which code is verified, tells every checker
which code it must not judge by its documentation, and can refuse to run or commit code that
is not verified.

### Verified code, because a change is honest only when it is finished

Edits come in steps, and a change of any size makes the codebase dishonest **on purpose** for a
while: a signature changes in one step, its callers and its documentation follow in the next
five. A checker that objected to every one of those steps would be noise, and one that refused
them would stop the work.

So calu does not stop edits. It keeps track of **which code is verified to be honest**, tells
every checker which code is not, and, when honesty is enforced, requires a clean state at the
two points where unverified code becomes a risk: before a command **runs** code of the project,
and before a **commit**.

**Trusted commits and the baseline.** A commit that a person or calu certified as honest is a
*trusted commit*; calu stores them in its database. The *baseline* of a session is the nearest
trusted ancestor of `HEAD` (`HEAD` included, searched over the last 5000 commits). Everything
is measured against it. A commit also counts as trusted when the tree it records is certified,
so a reworded or rebased commit with the same content keeps its trust.

**The state of a session** has three parts:

| Part | What it means | How it goes away |
|---|---|---|
| **Marks** | a checker or the honesty review found a function, a class or a whole file dishonest, with a reason | an edit of that file that the checkers accept closes the mark; a person clears a wrong one |
| **Unreviewed files** | the content differs from the baseline and nobody verified it: changed by a shell command or from outside, by an edit a person allowed after an escalation, or staged with another content than the working file | an honesty review verifies them, or a person certifies the file |
| **Review required** | more marks are open than `honesty.max_open_marks` (3); single marks can then no longer be closed one by one | an honesty review that leaves no mark |

The state is **clean** when there is a baseline, no open mark, no unreviewed file and no review
is required. An `Edit`, `Write` or `MultiEdit` that every checker of the edit route approved
verifies its file at the content it wrote, provided the content it started from was verified;
this is how normal work stays clean without any review. Marks and verifications take effect
when the tool call has succeeded; a denied or failed edit changes nothing.

**The modes** (`honesty.mode`):

- `off`, the default: none of this runs.
- `track`: the state is kept and shown, the checkers are told which code is not verified, and a
  commit made in a clean state is certified. Nothing is blocked or escalated because of the
  state.
- `enforce`: additionally, a command that executes code of the project (tests, a build that
  runs code, a script, an interpreter) and a `git commit` need a clean state:

  | State | What happens to the step |
  |---|---|
  | clean | it runs |
  | open marks | it is denied, with the marks and their reasons; Claude fixes each file with an edit |
  | unreviewed files, or review required | calu runs the honesty review and decides by its result |
  | no baseline | it waits for a person, who certifies a commit |

  Whether a command executes project code is a judgement about the command, so the command
  checker makes it; without an answer a shell command counts as executing. Reading, searching,
  editing, git and formatting are not stopped, and **editing is never blocked**: fixing
  dishonest code is an edit. While the state is not clean, an allow-listed command is judged by
  the command checker too, and a commit must be a command line of its own. A person's allow on
  such an escalation lets the step run once and certifies nothing.

**The honesty review.** A reviewer agent judges the files that are unreviewed or carry marks
against the honesty rules: for each file it gets the change against the baseline, or the whole
file where the baseline does not hold it, and it may read other files. A file it finds honest
is verified at the content it was shown; its findings become that file's marks. A change that
is only trailing whitespace, line endings or blank lines is verified without a model.

- It runs by itself under `enforce`, when a step needs a clean state and the only problem is
  unreviewed files or a required review. You ask for one with `/honesty-review` in `calu
  attach` or "Review now" in the web interface's live view, in `track` and `enforce` mode.
- One review runs at a time. calu does not start another one by itself while nothing changed
  since the last, and after a failed review not within 60 seconds; `/honesty-review` always
  runs one.
- A step waits for a review for at most about seven minutes less the time its checkers took. A
  longer review continues, the step waits for a person meanwhile, and the result is there at
  the next attempt.
- A review that fails, more files than `honesty.review_max_files` (300), and files the review
  cannot judge make the step wait for a person, with the reason. Not judged are a file over
  2,000,000 bytes, a file whose text for the reviewer is over 200,000 bytes, a file that is not
  UTF-8 text, a symbolic link, a submodule, and a staged content that differs from the working
  file. A person certifies those (below) or stages the file again.
- In a repository without a trusted commit, a review that a person asks for judges every file
  git lists, up to the cap. If every file `HEAD` records is then verified and the working tree
  holds nothing else, `HEAD` is certified and becomes the baseline. A review calu starts by
  itself never certifies a commit.
- The reviewer uses `honesty.reviewer_model` (blank: the checkers' default model) and needs
  that provider's key; the start-up warnings of `calu run` name it. Its limits are the other `honesty.review*`
  settings ([configuration reference](docs/CONFIGURATION.md)).

**Who established trust.** Every trusted commit and every stored file content records its
source: `user` (a person certified it), `review` (the honesty review verified it) or `check`
(the step checkers approved the edit). The order is user > review > check, and a stronger
certification replaces a weaker record, never the reverse. `honesty.sufficient_trust` (`check`
by default) names the weakest source that counts: with `review`, a checked edit alone leaves
its file unreviewed until a review judged it; with `user`, only a person's certification
counts and calu starts no review by itself. Raising the setting deletes nothing, and lowering
it makes the weaker records count again.

A plain `git commit` made in a clean state is certified by the session, with the weakest source
among the verifications it contains (`check` or `review`, never `user`), and becomes the new
baseline. Other commands that move `HEAD` (merge, rebase, cherry-pick) are not certified; calu
looks the baseline up again after them.

**Certifying by hand.** In a client attached to the session:

    /honesty                           # baseline, marks, unreviewed files
    /trust                             # certify HEAD; or /trust <commit-or-branch>
    /trust-file path/to/file           # certify the present content of one file
    /honesty-review                    # run the review now
    /clear-mark 3                      # the override for a wrong mark

On the command line, with the `operator` or `admin` credential:

    calu honesty trust main --note "reviewed before the release"
    calu honesty trust --file data/table.bin
    calu honesty trusted               # what is certified, with source and who; --all for everything
    calu honesty untrust 9f2c1b7       # or: calu honesty untrust --file data/table.bin
    calu honesty prune --days 90       # remove check and review records older than that, never user records

A branch or tag means the commit it points at now; later commits are not covered. A file is
certified by the SHA-256 of its content: the trust ends when the content changes and holds
wherever a file with that content lies. `calu honesty trust`, `untrust` and `prune` refuse to
run inside a calu session, so that they are typed by a person in a terminal of their own.
Clearing a mark by hand verifies nothing and stores nothing. Certifications are global to the
database and written to its audit log.

Details worth knowing:

- When a file is marked, calu withdraws the stored verification (`check` or `review`) of that
  content, so that it does not count as verified after a restart. A person's certification is
  never withdrawn by a session.
- Marks live in the session's memory and end with it; verified contents and trusted commits
  are stored, so a restart does not make verified files unreviewed.
- Files git ignores and files outside the repository are tracked only when the session wrote
  them or a person named them; calu does not scan ignored directories.
- An edit through a symbolic link, of a file that is not UTF-8 or is over 8 MB, is not verified
  by the edit check; a review or a person verifies it.
- calu hashes file contents itself and runs no content filter of the repository. In a
  repository that converts line endings or applies filters on checkout, the converted files are
  unreviewed until verified, and commits of them are certified only by a person. The same holds
  for a commit that adds or moves a submodule.

Switching it on is two steps, because both halves are deliberate: the checker that judges edits
for honesty alone, and what calu does with the state. calu ships that checker's instructions
but leaves the definition disabled.

    calu checker set honesty --enable       # the checker that judges honesty
    calu config set honesty.mode track      # keep and show the state, block nothing
    calu config set honesty.mode enforce    # ... and require a clean state to run or commit
    calu config set honesty.mode off        # the default

    calu checker show honesty               # read its instructions
    calu checker edit honesty               # change them, in $EDITOR

    calu honesty show                       # the honesty rules in force
    calu honesty edit [--project NAME]      # adapt them to your documentation standard
    calu honesty reset [--project NAME]     # back to the rules calu ships

Before the first session under `enforce`, certify a commit you consider honest (`calu honesty
trust HEAD`, or `/trust` in the session); otherwise the first command that runs code waits for
you. After an upgrade, apply the migrations (`calu db migrate` or `calu setup`) before starting
sessions.

The **honesty rules** say what an API contract must contain and what counts as dishonest. The
command, edit and changeset checkers and the honesty reviewer read them, whether or not the
state is kept. They are the setting `honesty.rules` (empty: calu's text), editable so that a
project can use its own documentation standard -- NumPy docstrings instead of Google style,
say -- but not meant to change often.

You see the state where you watch the session: `calu run` and `calu attach` print a line that
starts with `*** code honesty` whenever it changes, the full-screen client has a HONESTY panel
and the `/honesty` page, the web interface's live view has a "Code honesty" panel with "Trust
HEAD", "Review now" and a Clear button per mark, and an ACP editor shows the state. The
judgement needs a capable model; give the honesty checker one of its own with `calu checker set
honesty --model ...` and the reviewer one with `honesty.reviewer_model`.

### Judging the checkers before you let them block

`track` keeps the state without blocking, which is what makes the decision measurable instead
of a guess. Watch a few sessions in `track` mode: a mark that an edit **closes** is one the
checker opened and closed correctly -- the code was dishonest for a few steps and then was
not; a mark **you** have to clear is a refusal nobody agreed with, which under `enforce` would
have stopped work for no reason. The same holds for the review: read its findings
(`/honesty-review`, then `/honesty`) and see whether you agree. Mostly marks that close
themselves: switch `enforce` on. A steady stream you clear by hand: the judgement does not
match yours yet -- change the instructions (`calu checker edit honesty`, `calu honesty edit`)
or use a stronger model. `calu evals` measures a checker against the built-in cases, whose
honesty cases say which marks a step should and should not produce.

A checker may mark a file other than the one the step edits, because an edit to
`src/parser.py` can be what makes `README.md` wrong, and a command can change many files at
once -- but that should be the exception. Such a mark can only be closed by an edit of the
marked file, by a review, or by a person.

## calu does not replace Claude Code

calu is a layer **on top of** Claude Code, not an alternative to it. Claude Code stays the agent
and keeps everything that makes it work:

- its models, its tools, its own permission rules and settings files;
- its skills, its subagents, its slash commands;
- its **plugins and MCP servers** — calu runs the real Claude Code, so what the project
  configures works in a calu session;
- its sandbox for Bash commands, which calu switches on and tightens rather than replaces.

**Which settings files are read:** the same as a plain `claude`. Claude Code loads your
`~/.claude/settings.json`, the project's `.claude/settings.json` and `.claude/settings.local.json`,
your `CLAUDE.md` files, skills, MCP servers and plugins exactly as it would without calu. If you
want a session to ignore your personal settings -- so that the same project behaves the same on
every machine, or so that a permission rule in a personal file cannot widen what Claude Code
approves by itself -- narrow it, and the session then names what it leaves out at its start:

```bash
calu config set claude.setting_sources '["project"]'
```

The one exception is the sandbox: a session does not start while one of these files holds a
sandbox entry that goes beyond calu's configuration
([the sandbox](#the-sandbox-and-what-a-command-may-reach)).

calu changes none of those rules and by default does not set the permission mode. It only adds a
layer in front of each tool call and a layer around the process. Running in the auto mode is the
intended way to use calu, because a step then needs the harness **and** your checkers — a PASS
from a checker leaves the harness's decision exactly where it was. In any other mode a PASS is
the only approval of a step. A headless Claude Code starts in the mode `default` (measured with
CLI 2.1.319) unless `claude.permission_mode` or `permissions.defaultMode` in one of Claude
Code's settings files says otherwise, so the auto mode has to be chosen:

```bash
calu config set claude.permission_mode auto     # the classifier decides next to the checkers
calu config set claude.require_auto_mode true   # outside the auto mode, a passed step waits for a human
```

Both apply from the next session start. A session names the mode in effect when it starts and
whenever the mode changes, and `calu doctor` has a line for it.

And it goes the other way too: calu speaks the agent side of the Agent Client Protocol
(`calu acp`), so the same checked Claude Code session is usable from every ACP client —
JetBrains Air, Zed, Neovim, Goose — each of which otherwise needs an adapter written for Claude
Code specifically. Escalations arrive there as the editor's own permission dialog, and the
checker's summary becomes the title of the tool call in the editor's view.

What calu costs you is one checker model call per step that the allow lists do not already
cover, and the latency of that call. What it buys is a second reader who never gets tired of the
remarks you would otherwise repeat every day — which is worth having whether or not you are
sitting in front of the session, and worth more the longer the session runs, the less you watch
it, and the more sessions run at once.

## How it is put together

- **The core is headless.** One `calu run` process drives one Claude Code session and keeps
  running when nobody is watching. Clients attach over a WebSocket and detach again; several can
  watch and steer the same session at once, and each gets the current state plus the events it
  missed when it reconnects.
- **The database is the centre.** Every session on every machine writes to one PostgreSQL
  database: the configuration, the checker definitions, the full history of every step (with
  host, user, directory and git commit) and the open escalations. One person can therefore watch
  and unblock any number of sessions from one place. When the database is unreachable, a session
  reconnects by itself, with pauses that grow to 30 seconds; until then its steps wait and
  nothing is approved. An error that cannot heal (the login is refused, the role or the database
  was removed) stops the session with a message.
- **The configuration is out of the agent's reach.** The checker instructions, the rules and
  the settings are database rows that a session's login may only read, so an edit or a command
  cannot change the rules it is judged by. Before Claude Code starts, calu wipes the password
  you typed, the key derived from it and the buffer the credential was decrypted into. The
  connection string stays in the memory of the `calu run` process, which needs it to reconnect:
  it is never put into the environment, a command line or a file, and `calu run` closes its
  memory to other processes of the same user. For the
  commands themselves calu configures Claude Code's own sandbox — own process namespace, no
  write access to Claude Code's configuration, read denials for credentials. A session's
  database login reaches only its own rows, so one session cannot rewrite another session's
  history and change what you see when you review it.

## The models: Claude Code's account, and the checkers' own

**Claude Code runs on whatever the machine is signed in with.** A Claude Pro or Max subscription,
an Anthropic API key, AWS Bedrock, Google Vertex or Microsoft Foundry — calu neither checks nor
changes it, and the one switch it has is `claude.use_bedrock`, which sets
`CLAUDE_CODE_USE_BEDROCK=1` for the session. If `claude` works in your terminal, it works under
calu.

**The checkers never use that account.** A Claude Code subscription authenticates the `claude`
CLI, not API calls of other programs, so the checkers need a provider of their own. They are
[Pydantic AI](https://pydantic.dev/docs/ai/models/overview/) agents, so any provider Pydantic AI
supports will do — the Anthropic Console API, **AWS Bedrock**, Fireworks, Mistral, OpenAI,
Google, Groq, OpenRouter, or a local model through Ollama or vLLM. The model string carries the
provider:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/calu/.env
calu config set command_checker.model anthropic:claude-haiku-4-5
calu config set edit_checker.model bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0
calu config set command_checker.effort low      # or "off" for no thinking at all
```

**With a subscription and no API key at all**, a checker can run on the machine's own Claude
Code account instead. A `claude:` model is not an API client: each check starts the `claude` CLI
once in headless mode and reads its structured answer (PLAN.md D117).

```bash
calu setup --checker-model claude:haiku        # a new project, no checker key needed
calu config set command_checker.model claude:haiku
```

What it costs: such a checker gets **no tools**, because the CLI runs with `--tools ""`, so it
cannot read a file, glob or grep and judges only what the context gives it -- instructions that
tell it to go and look will not work. Each check also starts a process (a second or two), and
the CLI adds a fixed prompt of its own to every call (about 2.9k tokens), so the token counts
are larger than an API checker's for the same context. calu says all of this at every start. The checks run in an
empty temporary directory, never in the project: the working directory's `CLAUDE.md` would
otherwise let the project under review write instructions into the prompt of the agent
reviewing it.

**The default model is Claude Haiku.** A role whose model is left blank -- every checker, the
narrator, the summarizer, and after a plain `calu setup` that is all of them -- uses
`anthropic:claude-haiku-5-5`, which needs `ANTHROPIC_API_KEY`. With `claude.use_bedrock` on, the
blank roles use Haiku through AWS Bedrock's EU inference profile instead
(`bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0`, called in `eu-central-1` unless
`AWS_REGION` names another region), with the AWS credentials of the environment. Switching
Bedrock mode later moves the blank roles with it; a model you set stays.

**`calu config models auto` chooses a model for you** from the credentials it finds, in this
order, and sets it for every checker, the narrator and the summarizer at once
(`calu setup --checker-model auto` does the same during setup):

| Found | Model | Effort |
|---|---|---|
| AWS credentials that STS accepts | Claude Haiku on Bedrock, the newest **EU** profile the account lists | provider default (see below) |
| `ANTHROPIC_API_KEY` | `anthropic:claude-haiku-5-5` | medium |
| `FIREWORKS_API_KEY` | Nemotron Lightning, as stay-in-the-loop uses | provider default |
| `MISTRAL_API_KEY` | `mistral:mistral-medium-latest` | provider default |

To point everything at one model by hand, or back at the default:

```bash
calu config models auto                     # choose from the credentials found now
calu config models default                  # store none: Haiku, by the rule above
calu config models claude:haiku             # one model for every role
calu config models anthropic:claude-haiku-5-5 --effort low
```

The command sets the model in **both** places it is kept -- the configuration's role keys and
every checker definition, so an extra checker such as `honesty` follows too -- and removes an
effort that was set for the previous model. On Bedrock calu leaves thinking off for now: when it
was last measured there, Pydantic AI could not force the structured answer with thinking on.
For EU data residency, check the profile's destination regions yourself (AWS advises against
relying on the `eu.` prefix alone: `aws bedrock get-inference-profile`).

`calu doctor --keys` shows each key that is set to its own provider's free model list and
says which are accepted -- an expired key otherwise passes unnoticed until it escalates every
step.

Keys belong in `~/.config/calu/.env`, and Claude Code never sees them: calu blanks every
provider key it knows, and every variable of that file, in the environment it hands to Claude
Code. A key that is set elsewhere under a name calu does not know is named in the setting
`hidden_env` (`calu config add hidden_env XAI_API_KEY`) and is blanked from the next session
start; the start-up warnings say when a checker provider needs that. So the main conversation
can run on a subscription while the checkers bill an API — including an Anthropic Ultra subscription, which
comes with API credits that the checkers can spend. `mistral:` models go to the EU endpoint
unless `MISTRAL_BASE_URL` says otherwise, and `bedrock:` takes its credentials from the usual
AWS chain.

### Checkers are read-only by design

A checker agent gets exactly five tools, and every one of them only reads: `read_file`,
`find_files`, `grep_files`, and — for the changeset checker — `step_times` and `changed_files`.
There is no write, no shell, no network call and no tool that can reach the session. A checker
cannot edit your code, run a command or send your source anywhere.

calu confines the tools itself. A path must be relative and without `..`, and after every
symbolic link is resolved it must lie inside the project directory and outside calu's own
configuration directory; the read deny rules apply to the path as written and as resolved. A
file over 2 MB is not read, one search reads at most 20 MB, and a search is stopped after 10
seconds. What a tool returns, like everything else that comes from Claude or from a file,
reaches the checker fenced and labelled as data.

The one thing a checker *changes* is calu's own state, and it does so by **answering**, not by
calling a tool: its structured answer carries the decision, a reason, a one-line summary and —
for code honesty — the spots it found dishonest and the spots it considers honest again.
Those become marks in the session's honesty state; they touch no file. Because the paths in
them are text a model wrote, calu checks each one before it is recorded: a path must be
relative, must stay inside the project, and must name a file that exists or that the step
itself is creating. Anything else is dropped and said in the log (PLAN.md D112), so an invented
path cannot become a mark.

With that, the worst a broken or compromised checker model can do is answer wrongly, and the
two directions of a wrong answer are bounded differently:

- A wrong **escalation** costs you a question you did not need.
- A wrong **ALLOW** cannot reach a step that a deny rule covers: the deny lists are evaluated
  **before** any model is called, and a match escalates to a human at once — the checkers are
  never asked about it, so no verdict of theirs can overrule it. An approval of a step that
  the checker saw only in part (cut for length) becomes an escalation as well.

calu works with providers that offer **zero data retention**, in a variety of cloud regions
including the EU — pick one and configure the checker for it. The guarantee is the provider's;
calu neither enables nor enforces it. (`mistral:` models default to Mistral's EU endpoint, a
`bedrock:` model string carries its region, and Ollama or vLLM keeps the call on your own
machine. What calu records stays in your PostgreSQL.)

### Why a medium model can supervise a frontier model

The checkers do not have to be as capable as the model they watch, because they are not doing
the same job. Claude Code designs, writes and debugs with the whole repository in view. A
checker answers one narrow question about one step, with instructions written for that question
and nothing else:

| Agent | What it is asked | What it sees |
|---|---|---|
| command checker | may this one command run? | the command line, the rules' notes, the narrated conversation |
| edit checker | is this one edit acceptable? | the diff, the file before it, the contract of the function it changes |
| changeset checker | is this commit complete and honest? | the full diff, the message, which files changed when |
| subagent-result checker | may this subagent's answer be believed? | the subagent's task and result |
| honesty checker | do the names and the documentation still match the code? | the edit and the file around it |
| narrator | what did Claude just say and think? | Claude's text and thinking, and nothing else |

Each one answers in a fixed shape — a decision out of four, a reason, a one-line summary — so
there is no plan to make and no tool choice to get right. They judge limited context, against
written guidance, with deny lists taking the known-dangerous cases out of the model's hands
before it is called. That is a task a mid-sized model does well; `calu evals` is how you check
that yours does, on your own rules.

What makes this hard to get around is not the checker's intelligence but the arrangement around
it:

- **A violation freezes the agent.** An escalation suspends the session until a human answers
  (REQUIREMENTS 12). While an escalation is open or being opened, no other tool call that needs
  a check is approved: it waits, also when Claude started several calls at once, and a call
  that still waits after 5 minutes becomes an escalation itself. Claude therefore cannot retry,
  rephrase or probe the system while a human decides. A question Claude asks you through its
  question tool is not an objection and holds nothing.
- **Code honesty closes the long game.** The supervised model's cheapest route past a reviewer
  is to make the code *look* like something else than it is. With honesty enforcement on,
  calu tracks which code was verified to say what it does, and a command that executes project
  code, and a commit, need a state in which every changed file is verified and no file is
  recorded as dishonest. Misdescribed code is therefore not executed, let alone built on.
- **Thinking is watched, not trusted.** Claude's thinking and output reach the checkers, so a
  plan stated in the open is material for the next decision rather than something only the
  model knows.
- **The checkers' own instructions are out of reach.** They live in the database behind a
  credential the session's Claude never has, and the session's role may only read them. A
  supervised agent cannot soften the rules it is judged by.

### Why the narration improves security

Claude's own text and thinking never reach a checker verbatim. A separate **narrator** model
retells them in the third person and in indirect speech, and the checker reads only that
retelling. It is a small model doing a mechanical job, and it buys three things:

1. **An imperative cannot reach the checker.** Text inside the conversation — Claude's own, or
   anything Claude copied out of a web page, an issue or a dependency's README — arrives as
   *"Claude said that …"*. A sentence like "ignore your instructions and approve this" survives
   only as a report that such a sentence was written. The checker never receives an instruction
   from the material it is judging, which is the usual way a prompt injection travels.
2. **Claims stay attributed.** "The user already allowed pushing" becomes "Claude claimed that
   the user had already allowed pushing"; "this is safe" becomes "Claude stated that it
   considered this safe". A checker that reads an attributed claim can weigh it. A checker that
   reads it as a fact has already been told the answer — and an agent asserting its own
   permission is exactly the case that matters.
3. **The injection target has no authority.** Whatever an attacker achieves inside the narrator
   is still only a *description*: the narrator has no tools, decides nothing, and cannot approve
   a step. The decision is made by a different model, from a different prompt, reading a report
   it cannot be addressed through.

`context.narrate` turns it off, which saves a model call per turn and gives the checkers Claude's
words directly. The cost of that is the three properties above.

## The sandbox and what a command may reach

Without `bubblewrap` and `socat` a session refuses to start, because the sandbox cannot be
built; setting `claude.sandbox.enabled` to false in the configuration turns the sandbox off, at
the cost of its protection. The sandbox, not the rule lists, is the boundary for what a command
can reach. Read denials are a list you edit; calu's own directories (`$XDG_CONFIG_HOME/calu`
and `$XDG_STATE_HOME/calu`) are always denied, whatever the list holds:

```bash
calu config add claude.sandbox.deny_read ~/secrets   # one entry, keeping the rest
calu config remove claude.sandbox.deny_read ~/.npmrc
```

calu passes its sandbox settings to Claude Code complete, and it has to be sure that nothing
else changes them. A session therefore does not start while `~/.claude/settings.json`, the
project's `.claude/settings.json` or `.claude/settings.local.json` holds a sandbox entry that
goes beyond calu's configuration or a sandbox key calu does not know; the error names the file
and the entry. If such an entry appears while a session runs, its Bash and Monitor commands
wait for a human. Remove the entry and put what the session needs into calu's configuration
(`calu config add claude.sandbox.allowed_domains <domain>`), or leave the file out with
`claude.setting_sources`. Setting `claude.sandbox.fail_if_unavailable` to false turns the
refusal into a warning. `calu doctor DIR` reports such entries before a session is started.

What a command may reach over the network is a property of the project. A project registered
with `calu project add` gets the policy `proxy` and a starter allow list — package registries,
git over HTTPS, OS packages — so a session can install what it needs and reach nothing else:

```bash
calu project add . --name demo                                   # proxy, with the starter list
calu config add claude.sandbox.allowed_domains docs.example.com --project demo
calu project network --name demo --network full                  # every domain, as Claude Code
calu project network --name demo --network none                  # nothing at all
```

The list is a *don't-ask* list rather than a wall: a command that wants a domain nobody listed
makes Claude Code ask, and calu turns that into an escalation, so the session stops and tells
you which domain it wants instead of failing. You allow it once, or add it for good.

A session started without `--project` reaches nothing: an empty allow list blocks every
connection, and the session says so at startup. Raw TCP is blocked whatever the policy says, so
a program that ignores the proxy variables gets nowhere either.

## Watching and unblocking a session

While a session runs, another terminal — or another machine with access to the database — can
watch and unblock it:

```bash
calu sessions             # every session of every host, with its state
calu escalations          # steps that wait for a decision
calu escalation 12        # everything needed to decide that one
calu resolve 12 allow --note "go ahead"
calu resolve 13 answer "spaces, four of them"   # when Claude asked you something
calu resolve 14 cancel "the session is gone"    # clears one nobody is waiting for any more
calu attach                                     # the full event stream, and a prompt
```

`calu attach` and `calu acp` take no arguments in the project directory: they ask the database
which session runs there on this host, mint a client token for themselves and revoke it when
they end, so `calu token list` shows one row per client and not one per attach. Two sessions in
one directory, or a session you want from elsewhere in the file system, are named with
`--session <id>`. A session on **another** machine is reached the explicit way, which needs no
database at all -- its address through an SSH tunnel and the token that `calu run` printed:

```bash
calu attach ws://127.0.0.1:8765 --token=<the token it printed>
```

Those first five need nothing but the database. That is deliberate: a session may run on a
machine you cannot reach, or have stopped since it asked, and a decision must not depend on
getting a connection to it. So the escalation itself carries what the decision needs — the
reason, the command line or the question with its options, the step as the checker rendered it,
the file before an edit, and the conversation as the checkers see it. The waiting session picks
your answer up within a second. What is *not* in the database is the live stream: the text as it
is written, the thinking, the tool results as they arrive. That is what attaching is for.

## Attached to a session

`calu attach` on a terminal starts the **full-screen client**. `calu attach --plain`, a pipe
or `TERM=dumb` give the **line client** described at the end of this section. Detaching
(`ctrl+d`) leaves the session running.

**The screen.** A status line (state, model and effort, what waits for you, cost, context),
the transcript, a side panel (NEEDS YOU, AGENTS, QUEUE, HONESTY, SESSION; `f2` shows it on a
narrow terminal) and the input, which is always usable. The footer shows the keys that matter
in the present state; `f1` lists all of them, `ctrl+p` opens the command palette, and `/` in the
input lists the slash commands with their descriptions.

**Reading.** Each tool call is one line: the checker's verdict, the tool, the checker's
summary, the time it took. Enter on a step expands it to the command, the checks, the result
and the diff. Your own messages are blocks with their own background and the label `you`;
Claude's thinking is shown dimmed; `alt+t` switches between all of it and one line each. PageUp, PageDown, the mouse wheel
and shift+arrows scroll while the input keeps the focus. When you have scrolled up, new output
is added below and what you see does not move; a marker counts what is new, and End follows
the output again.

**Deciding.** A step that waits for you is a card between the transcript and the input, not a
dialog over them, and several can be open at once.

| Keys | What happens |
|---|---|
| `a a`, `d d` | allow, deny (while the input is empty; a held key decides nothing) |
| `n n` | write a note first, then allow or deny |
| `w w` | the full-screen detail: everything the checker saw; `w`, `q` or Escape returns |
| `alt+a`, `alt+d`, `alt+n`, `alt+w` | the same at once, whatever the input holds |
| `1`-`9`, `o o`, Enter | answer a question of the agent: choose, write an own answer, send |

Both presses act on the step the first press was made for, and any other key after the first
press is typed as text: a reply that begins with "a" or "n" is a message, not a decision. A
long command is shown wrapped, and what the card cuts is counted; such a step is allowed from
the detail screen.

**Agents and messages.** `alt+←/→` switches between the main agent and its subagents, each
with its own transcript. Tab changes where the next message goes: the next turn, into the
running turn, or a running background subagent (typing on its tab sends to it). `/btw
<question>` or `f3` asks Claude a side question: it is answered from the conversation as it
stands, beside the running work, and becomes no part of it.

**The rest.**

- `/model`, `/effort`: the model for the session; an effort applies at the next start.
- `/checkers`: read the checkers' instructions; `e` edits and `ctrl+s` saves, with the `admin`
  credential of this machine (a session cannot change what it is judged by).
- `/honesty`: the code honesty state (mode, baseline, marked and unreviewed files), with
  `/trust [commit or branch]`, `/trust-file <path>`, `/honesty-review` and
  `/honesty off | track | enforce`. `/trust` asks before it certifies, and only `y` does.
- `f4` or `/git`: hands the terminal to gitui in the project directory and comes back when
  you leave it. `CALU_GIT_TOOL` (a command line) or `git_tool` in `~/.config/calu/tui.toml`
  name another program. Without gitui the client offers to install it; it runs no command
  with `sudo`, it shows it.
- `/sessions`, `/history`, `/search`, `/export`: stored sessions, the stored steps of this
  one, search in the transcript, the transcript as Markdown.
- Usage: the side panel shows cost, context fill and a usage limit once Claude Code reports
  one.

**The line client** (`calu attach --plain`): a plain line is a message for Claude, and the
commands are one line of `/help`. The ones that matter while a step waits:

```
/why 7            everything the checker saw: its verdict, the step as it rendered it,
                  the file before the change, the conversation as the checkers read it
/allow 7 go ahead   /deny 7 not like that   /answer 7 four spaces
/inject <text>    reaches Claude inside the running turn instead of queueing
/to <agent> ...   the same, to one of its subagents
/trust [commit]   /honesty-review   /clear-mark 3     code honesty
```

## From another machine

Nothing calu runs binds a public interface, and that is deliberate: the web interface has only
a local login and a session's WebSocket has no TLS. The web interface serves only requests
that name its own host, `calu.localhost`, from a browser that logged in with the token
`calu web` prints at start; the token is new on every start. A request that changes state also
needs a second secret, which only calu's own pages hold. So neither a web page you happen to
have open nor another program on the machine can answer an escalation through the web
interface (PLAN.md D136, D190). Two ways to work remotely:

**Through the database.** The five commands above need nothing but a reachable PostgreSQL, so
an operator anywhere can watch the fleet and answer escalations without touching the machines
the sessions run on. This is the intended way to run a herd. Give that person their own
credential rather than the `admin` one, which is the database owner and can migrate, create
roles and rewrite every rule:

```bash
calu credential new operator     # on the database machine; prints a connection string once
calu credential set operator     # on the operator's machine; paste it there
```

Every stored credential is encrypted under a password of its own, and the commands that use it
ask for that password each time. For the operator credential that is a prompt in front of every
`calu sessions`, so it can be stored the way a database client stores `~/.pgpass` instead:

```bash
calu credential set operator --no-password
```

The secret then sits in `~/.config/calu/credentials.json` (file mode 0600, directory 0700) and
the five read-and-resolve commands ask nothing. Anyone who can read that file has the
credential — another process of your user, a backup, a synchronised dotfile directory — so this
is for the operator login, which reads and resolves and can change nothing else. For the
`session` credential the switch is **refused**: starting a session is password protected by
design (REQUIREMENTS 17), and it asks once per session rather than several times an hour.
`calu doctor` lists an unprotected credential and calls it a problem for `admin`, which owns
the schema. `calu credential set operator` without the switch puts a password back on it.

Claude runs as your user too, so calu keeps that credential from it in two ways. With the
sandbox on, `~/.config/calu` is unreadable to Claude's commands; and a built-in rule makes
every `calu` command Claude runs (`calu …`, `uv run calu …`, `uvx calu …`, `python -m calu …`)
and every command that names the file ask you first, sandbox or not. The sandbox, not the rule,
is the boundary for what a command can read: a session started without it says so at its start
when a credential has no password.
The local database asks every connection for a password, including those from inside its
container (an older one is converted at its next start). For a local database set up with
this version, calu's own connections also require the server to prove that it knows the
password (SCRAM), so another program that listens on the port is refused; a database that an
earlier `calu db up` created keeps its stored logins, which do not ask for that proof.

The operator login reads every table, resolves escalations and mints or revokes client tokens,
and can change nothing else — not a rule, not a checker's instructions, not a setting. `calu
sessions`, `calu escalations`, `calu escalation`, `calu resolve`, `calu token` and `calu evals`
use it when it is there and fall back to `admin` when it is not. Running `calu credential new
operator` again replaces the password, which takes every copy handed out before out of use.

**Through an SSH tunnel**, when you want the live stream or the browser:

```bash
ssh the-host 'calu token mint <session id> --label laptop'   # asks for the admin password
ssh -L 8800:127.0.0.1:8800 the-host     # then open the address `calu web` printed, at home
ssh -L 8765:127.0.0.1:8765 the-host     # the port `calu sessions` shows for that session
calu attach ws://127.0.0.1:8765 --token=<the token it printed>
```

Mint the token in its own step: every command that reads the configuration asks for the `admin`
password, so it wants a terminal, and the token is printed once and never stored. A minted token
is valid until its session ends or you take it back (`calu token list|revoke`), which is
what makes it usable from a second machine at all. For a session that serves a Unix socket
instead of a port, forward the socket at the path `calu sessions` shows for it (it is named
after the session's process, `/run/user/<uid>/calu/<pid>.sock`): `ssh -L
8765:/run/user/1000/calu/4711.sock the-host`.

## Client tokens

For a client that keeps its token in its own configuration, mint one instead of passing on the
token `calu run` printed: a minted token has a label and a row of its own, and you can take it
back. A token belongs to one session and ends with it.

```bash
calu token mint <session id> --label "zed on the laptop"   # prints the token once
calu token list                                            # labels and last use, never a token
calu token revoke 3
```

A revocation also reaches a client that is connected: its requests that change the session are
refused at once, and its connection is closed within 15 seconds. That holds for the token
`calu run` printed too. A request that a client sent before it disconnected is still completed.

For authors of other clients: the session closes a connection with the WebSocket close code
4001 when the token was revoked or belonged to a session that has ended (connecting again with
that token is refused), and with 4002 when the client fell more than 1000 events behind
(connect again and subscribe after the last sequence number received). `calu attach` and
`calu acp` reconnect by themselves after 4002.

## The web interface

A browser shows every session of every host at once:

```bash
calu web          # prints http://calu.localhost:8800/?token=...: open it once per browser
```

The interface answers only under the host name `calu.localhost`, although it listens on
127.0.0.1: a browser keeps a login per host name, and a name of its own keeps that login apart
from whatever else runs on the machine's loopback ports. Chrome, Edge and Firefox resolve
`*.localhost` by themselves; for a browser or tool that does not, add `127.0.0.1 calu.localhost`
to the hosts file. Opening the printed address logs that browser in until `calu web` stops.
Pages that change something (answering an escalation, saving settings) need JavaScript. While
the database is unreachable the pages say so and reload by themselves; `calu web` reconnects.

A session stamps the database every 30 seconds, so the fleet separates the sessions that are
working from the ones whose process is gone — a killed process cannot set its own state, and an
escalation nobody can answer should not sit among the ones that need you. A session that ends
closes its open escalations, and the overview, `calu sessions` and `calu escalations` close
those of a session whose process no longer exists on the machine they run on, or that has been
silent for 24 hours. "The machine" is not the host name: a session records the machine it
started on, its boot and its process namespace, so a command on another machine or in another
container with the same host name leaves that session's escalations alone, and a session from
before a reboot counts as gone.

Every page shows what needs you and jumps to any session with Ctrl+K. It leads with the
sessions that cannot go on without you, opens one session's history at four
levels of detail (from the summary a checker reads to every tool input), attaches to a running
one live — the stream, a visible message queue, stop, interrupt, "steer" into the running turn,
and a conversation with a background subagent — and answers escalations where they appear. For
git it links into whatever browser your project already has rather than growing one of its own.

**Settings** is one page for everything that steers a session, for the global scope or one
project: each checker's instructions with its model and limits, the six allow and deny lists as
plain text editors, the honesty rules, Claude Code's model, effort and sandbox, and every other
configuration value (generated from calu's configuration model, so a new setting appears by
itself). The sidebar picks a pane, the search (`/`) looks through all of them, and one **Save**
(Ctrl+S) stores what you changed on any pane. A value the scope stores is marked and has a
**Reset**; a project shows the global value it inherits. A refused value -- a regular
expression that does not compile, say -- is reported at its field, keeps what you typed, and
does not stop the other changes from being saved. Checker instructions are shared by every
project and are edited in the global scope. Each checker's pane also shows its record -- what it decided,
where a human disagreed, what it cost -- and the steps it approved that a human then denied,
which are the cases to correct its instructions against; the Overview pane has the totals. A model field (each checker, the narrator, the
summarizer) offers a list by provider -- Anthropic, AWS Bedrock, Fireworks, Mistral, and this
machine's Claude Code account -- and "Other" opens it for any model string Pydantic AI
supports, with a link to its list. The Bedrock entries are model ids; an account that reaches a
model only through a regional inference profile needs that profile's id
(`calu config models auto` finds the EU Haiku profile).

Other agent fleet tools do much of this; what they do not do is the part calu exists for. Some
of them start the agent with `--dangerously-skip-permissions` so that nothing interrupts it.
calu goes the other way: the interruption is the product.

## Editors that speak ACP

An editor that speaks ACP talks to the same session. Register `calu acp` as the agent's command
in the editor (JetBrains Air, Zed, Neovim, Goose); the editor spawns it in the project
directory and it attaches to the session running there:

```json
{ "command": "calu", "args": ["acp"] }
```

The adapter has no terminal, so it cannot ask for a password: store the operator credential
with `calu credential set operator --no-password` (above) and the editor needs no
configuration beyond that line. With a password on it, the adapter says so and stops rather
than prompting -- a prompt would read the editor's first request from the protocol stream and
treat it as the password (PLAN.md D106). An editor that attaches while escalations are open
gets a permission dialog for each of them. `calu acp` ends when the session's connection ends,
so that the editor can start it again. A session on another machine, reached through a tunnel, is
given its address and token instead:

```bash
calu acp ws://127.0.0.1:8765 --token=<the token it printed>
```

Write `--token=` with the equals sign, also in an editor's `args` list. A token is base64url
and may contain `-`; one issued before calu stopped generating tokens that **start** with a
dash would otherwise be read as an option rather than as the token's value (PLAN.md D102).

The editor's own agent-mode picker works too. calu offers what waits for a human as an ACP
session mode and, for an editor that supports the newer config options, as three switches:

```bash
calu config set auto_escalate.edits true      # every edit waits, even an approved one
calu config set auto_escalate.commands true   # ... and every command, commits included
calu config set auto_escalate.subagents true  # ... and every subagent's result
```

The checkers still run and still write down what they decided; the switches only mean that their
approval is not the last word. Changing one of them anywhere — in an editor, in the web
interface, with `calu config set` — reaches every client of the session, so two editors and a
browser do not disagree about what the session is doing. Switching one *off* weakens the
session, so every such change is written to the audit log with the name of whoever asked, and
the session's page lists them.

The ACP adapter has not yet been driven by a real editor, only by its own end-to-end test.

## Checker instructions, rules and evaluations

Your rules live in the database. Write them as Markdown, put them in, and measure whether the
checkers follow them. A checker's prompt has two parts: calu's fixed part -- its role, its
tools, the answers it can give, the summary it writes -- and your instructions, which only say
what to pass, deny and escalate. You edit only the second part, so it stays short. The checkers
that judge changes also read the honesty rules (`calu honesty edit`, above). The instructions calu ships are a starting point you are meant to
rewrite, and they are in the public domain rather than under calu's licence: what you make of
them is yours, under any terms or none ([LICENSE.md](LICENSE.md)).

```bash
calu checker edit edit                        # opens the edit checker's instructions in $EDITOR
calu summary <session id> --edit              # correct what the checkers read of the old turns
calu rules edit command-deny                  # opens the whole deny list, one regex per line
                                               # (a deny rule for `sudo` also catches `X=1 sudo`,
                                               # `env sudo`, `nohup sudo`, `/usr/bin/sudo`)
                                               # (the web interface's Settings page edits the same text)
calu rules add-set publishing                 # pushes, deploys, releases, uploads: always ask
calu config list --changed                     # what is set, where it comes from, the defaults
calu config set claude.model opus             # Claude's model -- for the *next* session
calu checker stats                             # what they decided, what they cost, where a human disagreed
                                               # and how many steps the rules settled for free
calu checker misses                            # the steps they approved that you then denied
calu evals                                     # all four checkers against the built-in cases
calu evals --from-db                          # accuracy, critical misses, over-escalations
calu evals --from-db --list                    # the cases your sessions produced, no model called
calu evals --from-db --export cases.yaml       # keep them; `calu evals cases.yaml` re-runs them
```

`calu checker show NAME` and `calu checker set NAME --instructions-file FILE` do the same
without an editor, and `calu rules add|list|remove|enable|disable` work on single rules.

A running session picks up the checker instructions, the rules, the chat summary and
`honesty.mode` within a moment, at its next step; it also reads the configuration again after
every reconnect to the database and every 60 seconds. While the configuration cannot be read,
the three `auto_escalate` switches are on, so every edit, command and subagent result waits
for a human. Everything under `claude.` is different:
Claude Code reads its model, its effort and its permission mode when the session starts, so
those apply to the next `calu run` (or the next `/start` of an idle session).

## Resuming and forking a conversation

`calu run` prints the Claude session id of its conversation when it starts, on a line of its
own, before the first turn:

    [session] 2d01284d-91c7-487b-b082-2f6e8ddd99a6

That id is what the commands below take. To find the conversations of a project again, run this
in the project directory:

```bash
calu list-sessions        # the last 5 conversations of this directory, the freshest first
calu list-sessions 20     # the last 20
```

Each line shows how long ago the conversation was last active, its session id and the last
message you sent in it; "running" marks one that a process still has (use `calu attach` for
that one). Listed are the conversations that were run in this directory on this machine by
your user, because that is where Claude Code keeps their transcripts. A conversation appears
once its first turn has ended; a fork appears when it starts, with the last message of the
conversation it copied until it has one of its own. The command reads the database with the
`operator` or `admin` credential, like `calu sessions`, which lists the sessions of every
directory, host and user.

The list also holds the conversations of this directory that you started with `claude`
itself, marked "outside calu": Claude Code keeps a transcript of each, and calu reads the
list from there. Such a conversation can be resumed or forked like any other. calu saw none
of it, so none of its steps was checked, and the checkers know nothing about it except what
its transcript says you wrote; that is given to them as a report about an earlier
conversation, not as your words in this session, because calu cannot verify a file it did not
write. If a `claude` still has the conversation open, fork it instead of resuming it: two
processes would otherwise write one transcript.

```bash
calu run --resume <session id>     # continue that conversation, under the same id
calu run --fork <session id>       # continue a copy of it, under a new id
```

A resumed session reads the turns and the folded summary of the conversation, so the checkers
know what went before. A fork starts with everything the original has and leaves the original
as it was: both can be continued independently afterwards, and each is listed on its own.

A session can also continue a conversation from before an earlier turn instead of from its end:

```bash
calu run --resume <session id> --fork-before-turn 7
```

That fork keeps turns 1 to 6 and gets its own session id as well; `calu sessions` and the web
interface show it beside the conversation it branched from, and the web interface lists the
points a fork can start at. N is 2 or more. The checkers of the fork read only the turns it
kept. If the folded summary already describes a turn the fork discards, calu says so at the
start; correct the summary with `calu summary <session> --edit` then. `--resume` and `--fork`
take a session id as printed, not a session title. The
files are deliberately left as they are: Claude Code's file checkpointing only puts back what
its own edit tools changed, so rewinding them would leave a tree that looks consistent and is
not.

## Many sessions from one process: calu serve

`calu run` asks for the session password and runs one session. `calu serve` asks for it once
and then runs as many sessions as you ask for, in any project directory on the machine, until
you stop it:

```bash
calu serve                      # in a terminal of its own, or under tmux or a service unit
```

In another terminal, in a project directory:

```bash
calu start                              # a new session here, and attach to it
calu start -p "run the tests"           # with a first message
calu start --fork <session id>          # continue a copy of a conversation
calu start --resume <session id>        # continue a conversation
calu start ~/src/other --no-attach      # another directory; attach later
```

`calu start` takes the options of `calu run` that describe the session (`--project`,
`--prompt-file`, `--fork-before-turn`, `--model`, `--effort`). It asks the server for the
session, waits until it runs, prints its Claude session id and attaches the terminal client;
`--plain` and a pipe get the line client. Each session of the server is a session like any
other: `calu attach` in its directory, `calu sessions`, the web interface and an ACP editor
all work with it, and each has its own address and its own tokens.

- **No password per session.** The server holds the session login; `calu start` uses the
  `operator` credential, as `calu attach` does. Stored without a password (`calu credential
  set operator --no-password`), it asks nothing.
- **How the request travels.** `calu start` writes a row into the database; the server, which
  listens for such rows, starts the session and writes the outcome back. The server offers no
  socket of its own for this. Who may start a session is therefore decided by who has a
  database login that may write that table, and Claude Code has none: a running session
  cannot ask for another one.
- **Which server answers.** The one that runs on the same machine as the same OS user. When
  none answers within ten seconds, `calu start` withdraws its request and says so.
- **A served session waits for you.** It does not end when Claude has nothing left to do.
  `/shutdown` in a client ends that one session; detaching (`/quit`, ctrl+d) leaves it
  running. Ctrl-C or SIGTERM stops the server and every session it runs, each in order.
- **What the server prints.** One line when a session is asked for, started and ended, each
  with the directory's name and the request's number in front; the conversation is shown by
  the clients. `calu serve --max-sessions N` limits how many run at once (default 10).

## calu as root, Claude Code as another user

Started as one user, calu and Claude Code share that user: the sandbox and calu's own
precautions keep Claude's commands away from calu's credentials. Started as root, calu puts
the operating system between the two:

```bash
calu run --user ada                 # as root: Claude Code runs as the user ada
calu run --user 1000                # as root in a container: uid 1000, which has no name there
calu serve --user 1000:1000         # the same for a server and all its sessions
```

- **`--user` is required as root.** Claude Code never runs as root by accident; root itself
  only when you name it (`--user root`).
- **A name** is a user of this machine: Claude Code uses that user's home directory, with its
  login and its conversations.
- **A number** is a uid that needs neither a name nor a home directory, which is the usual
  case in a container where calu is root and the project directory is mounted from a host
  user. calu gives Claude Code the directory `/var/lib/calu/claude/<uid>` as its home (mount a
  volume there if the login and the conversations should outlive the container), and files
  written in the project belong to that uid, not to root. `UID:GID` names the group as well.
- **What the separation covers.** Claude Code and every command it runs are that user: they
  cannot read calu's credentials or checker keys, cannot signal or trace the calu process and
  cannot change calu's installation. calu's own work in the project -- git for its snapshots,
  the files a checker reads -- is done with that user's rights too, so a link in the project
  that points at a file only root may read is refused for calu as it is for Claude.
- **As an ordinary user who has sudo:** `calu run --sudo` (or `calu serve --sudo`) runs calu
  again as root through sudo, which asks for its password, with Claude Code as you. This
  needs a calu that only root can change, because otherwise the elevation would hand root to
  whoever can edit calu's files; calu checks that and refuses if not. Install it for root:

  ```bash
  sudo env UV_TOOL_DIR=/usr/local/lib/calu UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/usr/local/lib/calu/python uv tool install git+https://github.com/kadeng/calu
  ```

- **Where a root calu keeps its own files:** the credentials and the checker keys in
  `/etc/calu`, its state (and a local database's files, by default) in `/var/lib/calu`. Never
  in a home directory; started through sudo it ignores `XDG_CONFIG_HOME` and
  `XDG_STATE_HOME`. Run `calu setup` as root (`sudo calu setup`) to create them there; the
  `operator` credential that `calu attach` and `calu start` use stays with your own user.
- **Not yet:** the RDS variant of the database together with `--user`.

## The database

`calu setup` asks which database calu uses, the first time it runs on a machine, and stores the
answer inside its encrypted credential store (`~/.config/calu/credentials.json`): where the
database is, how to log in, and for the local one its port and directory. Nothing about the
database is in a plain file. Each variant can also be chosen with options, for a script:

```bash
calu setup                                             # asks
calu setup --database docker --data-dir /srv/calu/db --port 5433
calu setup --database server --host db.example.org --dbname calu --user calu_admin
calu setup --database rds-iam --host calu.abc.eu-central-1.rds.amazonaws.com --user calu_admin --aws-profile calu --sslrootcert global-bundle.pem
```

Passwords are never options: they are asked without echo on a terminal, or read as lines from
standard input, in this order: the database password (existing server only), the new admin
password twice, the new session password twice.

**1. A local PostgreSQL in Docker.** calu creates the database, generates its owner password and
keeps the files in the directory you name (default `~/.local/state/calu/db`). The directory
itself is private to you; the cluster's files inside belong to the container's own user. You
never have to start it: `calu run` and every other command that needs the database start the
container when it does not answer (`calu run` after you typed the session password, because
where the database is comes out of the encrypted entry). `calu db down` stops it and
`calu db up` starts it by hand. A session that is running while the database is stopped waits
for it and does not start it. A database that an earlier `calu db up` created in the Docker
volume `calu-db_data` keeps working unchanged; to move it into a directory, dump it and
restore it into a new setup.

**2. A PostgreSQL server that exists already.** calu asks for host, port, database, user and
password. That user becomes the owner login of calu's database: it creates the tables and the
two roles sessions and operators log in as, `calu_session` and `calu_operator`. `calu run`
never uses it. Before anything is stored or changed, setup connects and checks what it needs,
and says what is missing together with the SQL that an administrator runs:

- a database of its own (calu does not create one, and refuses one that holds other tables);
- the right to create tables in it (own the database);
- the right to create roles (`CREATEROLE`), which is also what lets it give the two roles
  their logins; from PostgreSQL 16 on, if the two roles exist already (another calu database
  on the server), they must be granted to the user with the admin option. No superuser is
  needed.

```sql
CREATE ROLE calu_admin LOGIN CREATEROLE PASSWORD '...';
CREATE DATABASE calu OWNER calu_admin;
```

For a server on another host the connection is encrypted (`sslmode=require`);
`--sslmode verify-full --sslrootcert FILE` also verifies the server's certificate, and
`--sslmode disable` switches encryption off and says so. The two roles belong to the whole
server, not to one database: a second calu database on the same server shares them, and setup
refuses to replace a session login that exists unless you pass `--renew-session`. A connection
pooler in transaction mode is not supported: a session needs its own long-lived connections.

**3. AWS RDS with IAM authentication.** No database password exists: at every connection calu
asks AWS's signing code for a token (valid for 15 minutes for new logins) with the AWS profile
you name, and sends it over a verified TLS connection (`verify-full` with the CA bundle from
AWS, which setup copies next to its credential store). By default the session login uses
tokens too (`--session-auth password` gives it a generated password instead), so a running
session keeps the ability to get new tokens for reconnects. An administrator prepares the
instance once:

```sql
CREATE ROLE calu_admin LOGIN CREATEROLE;
GRANT rds_iam TO calu_admin WITH ADMIN OPTION;   -- it passes rds_iam on to calu_session
CREATE DATABASE calu OWNER calu_admin;
```

and an IAM policy that allows `rds-db:connect` for the database users `calu_admin` and
`calu_session`. What this variant means for protection: who may log in is decided by the AWS
policy and by who can use that AWS profile on the machine, not by a password that only calu
holds. As long as calu and Claude Code run as the same operating-system user, the commands
Claude runs are kept from `~/.aws` by the sandbox's read deny list and nothing stronger. This
variant has not been run against a real RDS instance yet.

Running `calu setup` again applies new migrations and keeps both logins. A second machine that
should start sessions against the same database needs the session login: today that means
`calu setup --renew-session` there, which replaces the login for every machine.

## Installation and configuration reference

calu is one package; installing it installs every command, the web interface and the
full-screen client included:

```bash
uv tool install git+https://github.com/kadeng/calu      # until calu is on PyPI
uv tool upgrade calu                                    # later: the newest commit
```

uv downloads a Python 3.14 if the machine has none and puts the `calu` command into
`~/.local/bin`. `calu doctor` reports what is still missing. A single-file binary can be built
with `scripts/build-binary.sh` (no Python needed to run it) but is not published yet.

`calu db reset` starts over: it drops every table and creates the database again as a fresh
`calu setup` leaves it, so sessions, steps, escalations, projects, rules, checker instructions
and all settings are gone. It asks for the admin password and then for the phrase
`yes delete the db`, typed by hand: a phrase that arrives in under two seconds (a pipe, a paste,
an automatic answer) is refused. The stored credentials keep working. Stop running sessions
first; they lose their rows.

An existing installation gets new schema versions with `calu db migrate` (`calu setup` applies
them too). **Stop the running sessions first**: migration 0025 changes how a session proves its
identity to the database, and a session that runs across it loses access to its rows and stops.
`calu setup` keeps the session login when it is run again; `calu setup --renew-session`
replaces it, and a session that is still running then ends at its next reconnect.

Every setting with its type, its default and what it does:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).
