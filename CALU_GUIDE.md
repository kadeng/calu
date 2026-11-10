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
- [Checker API keys and Claude Code's account](#checker-api-keys-and-claude-codes-account)
- [The sandbox and what a command may reach](#the-sandbox-and-what-a-command-may-reach)
- [Watching and unblocking a session](#watching-and-unblocking-a-session)
- [Attached to a session](#attached-to-a-session)
- [From another machine](#from-another-machine)
- [Client tokens](#client-tokens)
- [The web interface](#the-web-interface)
- [Editors that speak ACP](#editors-that-speak-acp)
- [Checker instructions, rules and evaluations](#checker-instructions-rules-and-evaluations)
- [Forking a conversation](#forking-a-conversation)
- [Installation and configuration reference](#installation-and-configuration-reference)

## What the checkers give you, in detail

What a checker can do besides deciding is **comment**. It can let an edit through and still tell
Claude to keep the names and the API documentation honest, or to stop widening the task. Claude
receives the remark together with the approval. A checker can also **look at the code**: read a
file, list files by glob, grep for a pattern, so that it judges on evidence rather than on the
diff alone. That is all it can do: it cannot write, run commands or talk to the outside.

- **Allow and deny lists before the models.** Harmless commands (`ls`, `git status`) run without
  a model call; known-dangerous ones (`sudo`, force pushes, remote deletions, reads of your
  credentials) always ask a human. Everything else goes to a checker.
- **Checkers with your rules.** One checker reviews commands, one reviews edits. Their
  instructions are plain Markdown that you edit: safety, readability, documentation, and the
  rules you would otherwise have to repeat to Claude every day (see
  [what you can enforce](#what-you-can-enforce-with-a-checker)). They get the conversation in
  condensed form, your project's AGENTS.md and the step itself, and read-only tools to look
  further when they need to: read a file or a range of its lines, find files by glob, grep for a
  pattern — so a checker can see who calls the function being changed instead of guessing. They
  can also ask **when** things happened: the times of the session's steps, and which step last
  changed a file. That last question is answered from the file's change time, which an
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
  refusal is then not a comment on something that already stands.
- **A review of the whole change before it is committed.** When Claude runs `git commit`, calu
  holds the commit and gives the complete diff to a checker of its own — the one checker that
  sees the result rather than one step of it. It judges what the step checkers cannot: whether
  the change is complete (a renamed function with callers left behind), whether it contains work
  nobody asked for, whether a test was weakened to make it pass, whether the message describes
  the change. A denial undoes nothing; it stops the commit and tells Claude what to fix first.
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
- **Unsupervised runs.** Claude Code works in its auto mode (its default) with the checkers on
  top of it, so a session can run for hours — overnight, for example — and stops only when
  something genuinely needs you.

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

### The register, because a change is honest only when it is finished

Edits come in steps, and a change of any size makes the codebase dishonest **on purpose** for a
while: a signature changes in one step, its callers and its documentation follow in the next
five. A checker that objected to every one of those steps would be noise, and one that refused
them would stop the work.

So the honesty checker does not judge each step in isolation. It **records** the spot — a file,
or one function in it — with what disagrees, and it clears the record when the code and its
documentation agree again. calu enforces that record at the one point where temporary
dishonesty becomes a risk: a command that would **run** the recorded code is refused, with the
reason, until the record is clear.

Editing recorded code is never refused. That is the way out, and it has to stay open: fixing
the dishonesty is an edit.

Switching it on is two steps, because both halves are deliberate: the checker that judges, and
what calu does with its judgement. calu ships the checker's instructions but leaves the
definition disabled.

    calu checker set honesty --enable       # the checker that judges honesty
    calu config set honesty.mode track      # record and report, block nothing
    calu config set honesty.mode enforce    # ... and refuse to run recorded code
    calu config set honesty.mode off        # the default

    calu checker show honesty               # read its instructions
    calu checker edit honesty               # change them, in $EDITOR

`track` is how to judge the checker on your own project before trusting it to block anything:
the marks appear, nothing stops. The register belongs to the project, not to one session, so
debt that one session leaves behind is still there for the next one, and a session with open
marks says so when it starts.

    calu honesty list                  # what is recorded as dishonest, and why
    calu honesty list --all            # ... including what has been cleared again
    calu honesty list --project demo   # only one project's
    calu honesty clear 12              # the override when a mark is wrong

`clear` keeps the row and records that a person closed it, so the register still shows what
happened. The judgement needs a capable checker model; give this checker one of its own with
`calu checker set honesty --model ...`.

### Judging the checker before you let it block

`track` records without blocking, which is what makes the decision measurable instead of a
guess. Two commands say whether the checker has earned `enforce`:

    calu honesty list --all     # every mark, who opened it, and how it ended
    calu checker stats          # per checker: marks opened, cleared, overruled by a person

Read the third number first. A mark the checker **cleared itself** is one it opened and closed
correctly: the code was dishonest for a few steps and then was not. A mark a **person
overruled** is a refusal nobody agreed with, which under `enforce` would have stopped work for
no reason. So:

- marks opened, almost all cleared by the checker, none overruled: switch `enforce` on.
- a steady stream of overruled marks: the checker's judgement does not match yours yet. Change
  its instructions (`calu checker edit honesty`) or give it a stronger model, and watch the
  numbers again -- not the other way round.
- no marks at all after a week of real work: either the codebase is honest, which is worth
  knowing, or the checker is not running (`calu checker list` shows whether it is enabled).

`calu honesty list --all` names the checker that opened each mark, which matters once more
than one checker writes to the register -- honesty, security and an over-engineering checker can
all run on the same edit (see the note on several checkers per route in PLAN.md §13.5).

## calu does not replace Claude Code

calu is a layer **on top of** Claude Code, not an alternative to it. Claude Code stays the agent
and keeps everything that makes it work:

- its models, its tools, its own permission rules and settings files;
- its skills, its subagents, its slash commands;
- its **plugins and MCP servers** — calu runs the real Claude Code and loads the project's
  settings, so what works in your `claude` session works in a calu session
  (`claude.setting_sources` decides which setting sources are loaded);
- its sandbox for Bash commands, which calu switches on and tightens rather than replaces.

calu changes none of those rules and does not even set the permission mode: Claude Code's own
default is the auto mode, and calu leaves that choice with you. It only adds a layer in front of
each tool call and a layer around the process. Running in the auto mode is the intended way to
use calu, because a step then needs the harness **and** your checkers — a PASS from a checker
leaves the harness's decision exactly where it was. In any other mode the checkers simply decide
alone; nothing has to be configured either way.

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
  and unblock any number of sessions from one place.
- **The configuration is out of the agent's reach.** The checker instructions, the rules and
  the settings are database rows, and the credential that reads them is wiped before Claude
  Code starts, so an edit or a command cannot change the rules it is judged by. For the
  commands themselves calu configures Claude Code's own sandbox — own process namespace, no
  write access to Claude Code's configuration, read denials for credentials. A session's
  database login reaches only its own rows, so one session cannot rewrite another session's
  history and change what you see when you review it.

## Checker API keys and Claude Code's account

Checker API keys belong in `~/.config/calu/.env`; Claude Code never sees them — calu blanks
every provider key in the environment it hands to Claude Code, so the checkers can bill an API
while the main conversation keeps whatever the machine is signed in with. The combination worth
knowing about: Claude Code on a Claude subscription (an Ultra subscription includes API credits)
and the checkers on an **Anthropic API key** from the Console:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/calu/.env
calu config set command_checker.model anthropic:claude-haiku-4-5
calu config set command_checker.effort low      # or "off" for no thinking at all
```

AWS Bedrock works the same way for the checkers (`bedrock:...`, credentials from the usual AWS
chain), and `claude.use_bedrock` is the separate switch that puts *Claude Code* on Bedrock.

## The sandbox and what a command may reach

Without `bubblewrap` and `socat` a session refuses to start, because the sandbox cannot be
built; setting `claude.sandbox.enabled` to false in the configuration turns the sandbox off, at
the cost of its protection. Read denials are a list you edit:

```bash
calu config add claude.sandbox.deny_read ~/secrets   # one entry, keeping the rest
calu config remove claude.sandbox.deny_read ~/.npmrc
```

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
calu attach ws://127.0.0.1:8765 --token ...     # the full event stream, and a prompt
```

Those first five need nothing but the database. That is deliberate: a session may run on a
machine you cannot reach, or have stopped since it asked, and a decision must not depend on
getting a connection to it. So the escalation itself carries what the decision needs — the
reason, the command line or the question with its options, the step as the checker rendered it,
the file before an edit, and the conversation as the checkers see it. The waiting session picks
your answer up within a second. What is *not* in the database is the live stream: the text as it
is written, the thinking, the tool results as they arrive. That is what attaching is for.

## Attached to a session

Attached, you type at the session: a plain line is a message for Claude, and the commands are
one line of `/help`. The ones that matter while a step waits:

```
/why 7            everything the checker saw: its verdict, the step as it rendered it,
                  the file before the change, the conversation as the checkers read it
/allow 7 go ahead   /deny 7 not like that   /answer 7 four spaces
/inject <text>    reaches Claude inside the running turn instead of queueing
/to <agent> ...   the same, to one of its subagents
```

## From another machine

Nothing calu runs binds a public interface, and that is deliberate: there is no login in front
of the web interface and no TLS in front of a session's WebSocket. Two ways to work remotely:

**Through the database.** The five commands above need nothing but a reachable PostgreSQL, so
an operator anywhere can watch the fleet and answer escalations without touching the machines
the sessions run on. This is the intended way to run a herd. Give that person their own
credential rather than the `admin` one, which is the database owner and can migrate, create
roles and rewrite every rule:

```bash
calu credential new operator     # on the database machine; prints a connection string once
calu credential set operator     # on the operator's machine; paste it there
```

The operator login reads every table, resolves escalations and mints or revokes client tokens,
and can change nothing else — not a rule, not a checker's instructions, not a setting. `calu
sessions`, `calu escalations`, `calu escalation`, `calu resolve`, `calu token` and `calu evals`
use it when it is there and fall back to `admin` when it is not. Running `calu credential new
operator` again replaces the password, which takes every copy handed out before out of use.

**Through an SSH tunnel**, when you want the live stream or the browser:

```bash
ssh the-host 'calu token mint <session id> --label laptop'   # asks for the admin password
ssh -L 8800:127.0.0.1:8800 the-host     # then open http://127.0.0.1:8800 at home
ssh -L 8765:127.0.0.1:8765 the-host     # the port `calu sessions` shows for that session
calu attach ws://127.0.0.1:8765 --token <the token it printed>
```

Mint the token in its own step: every command that reads the configuration asks for the `admin`
password, so it wants a terminal, and the token is printed once and never stored. A minted token
outlives the session you made it for and can be taken back (`calu token list|revoke`), which is
what makes it usable from a second machine at all. For a session that serves a Unix socket
instead of a port, forward the socket at the path `calu sessions` shows for it (it is named
after the session's process, `/run/user/<uid>/calu/<pid>.sock`): `ssh -L
8765:/run/user/1000/calu/4711.sock the-host`.

## Client tokens

For a client that keeps its token in its own configuration, mint one instead of passing on the
token `calu run` printed: a minted token outlives the session you made it for, and you can take
it back.

```bash
calu token mint <session id> --label "zed on the laptop"   # prints the token once
calu token list                                            # labels and last use, never a token
calu token revoke 3
```

## The web interface

A browser shows every session of every host at once:

```bash
calu web          # http://127.0.0.1:8800, local only: there is no login yet
```

A session stamps the database every few seconds, so the fleet separates the sessions that are
working from the ones whose process is gone — a killed process cannot set its own state, and an
escalation nobody can answer should not sit among the ones that need you.

Every page shows what needs you and jumps to any session with Ctrl+K. It leads with the
sessions that cannot go on without you, opens one session's history at four
levels of detail (from the summary a checker reads to every tool input), attaches to a running
one live — the stream, a visible message queue, stop, interrupt, "steer" into the running turn,
and a conversation with a background subagent — and answers escalations where they appear. The
settings page is generated from calu's own configuration model, so Claude's model and effort,
`CLAUDE_CODE_USE_BEDROCK=1`, each checker's model and thinking effort, the sandbox and the rest
are all editable, and so are the checker instructions themselves. For git it links into whatever
browser your project already has rather than growing one of its own.

Other agent fleet tools do much of this; what they do not do is the part calu exists for. Some
of them start the agent with `--dangerously-skip-permissions` so that nothing interrupts it.
calu goes the other way: the interruption is the product.

## Editors that speak ACP

An editor that speaks ACP talks to the same session. Register `calu acp` as the agent's command
in the editor (JetBrains Air, Zed, Neovim, Goose); the editor spawns it and it attaches to the
running session:

```bash
calu acp ws://127.0.0.1:8765 --token ...
```

The editor's own agent-mode picker works too. calu offers what waits for a human as an ACP
session mode and, for an editor that supports the newer config options, as three switches:

```bash
calu config set auto_escalate.edits true      # every edit waits, even an approved one
calu config set auto_escalate.commands true   # ... and every command
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
checkers follow them:

```bash
calu checker edit edit                        # opens the edit checker's instructions in $EDITOR
calu summary <session id> --edit              # correct what the checkers read of the old turns
calu rules edit command-deny                  # opens the whole deny list, one regex per line
calu config set claude.model opus
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

A running session picks up such a change within a moment, at its next step.

## Forking a conversation

A session can continue a conversation from before an earlier turn instead of from its end:

```bash
calu run --resume <claude session id> --fork-before-turn 7
```

The fork gets its own session id, so `calu sessions` and the web interface show it beside the
conversation it branched from, and the web interface lists the points a fork can start at. The
files are deliberately left as they are: Claude Code's file checkpointing only puts back what
its own edit tools changed, so rewinding them would leave a tree that looks consistent and is
not.

## Installation and configuration reference

`uv tool install` installs the wheel, and `calu doctor` reports what is still missing; a
single-file binary can be built with `scripts/build-binary.sh` (70 MiB, no Python needed to run
it) but is not published yet.

Every setting with its type, its default and what it does:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).
