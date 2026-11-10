# calu

## A coding agent that wraps Claude Code and checks every step while it runs

calu starts Claude Code for you and puts a second opinion in front of every command it runs and
every file it edits. It is a layer on top of Claude Code, not a replacement: everything Claude
Code does — its tools, its permission rules, its plugins, its MCP servers — keeps working
([details below](#calu-does-not-replace-claude-code)).

That second opinion comes from **checker agents**: LLMs that you steer with your own
instructions. Each checker answers one of four things:

| Answer | What happens | How often |
|---|---|---|
| PASS | the checker has no objection; Claude Code's own approval decides as usual | almost everything |
| ESCALATE | time freezes: Claude is paused until a human allows or denies the step | the interesting cases |
| ALLOW | the step runs, whatever the auto-approval would have said | a deliberate exception |
| DENY | the step does not run; Claude is told why and carries on | a deliberate exception |

The two ends of that table are the exceptions. A checker is not there to re-decide what Claude
Code already decides well; it is there to notice the few steps that deserve a human, and to
overrule the harness in the handful of cases where you know better.

What a checker can do besides deciding is **comment**. It can let an edit through and still tell
Claude to keep the names and the API documentation honest, or to stop widening the task. Claude
receives the remark together with the approval. A checker can also **look at the code**: read a
file, list files by glob, grep for a pattern, so that it judges on evidence rather than on the
diff alone. That is all it can do: it cannot write, run commands or talk to the outside.

The pause matters. Claude cannot retry, rephrase or probe the system while it waits, so a
denial cannot be worked around. Decide from any client that is attached to the session, or
directly in the database — from the next room or from another continent.

calu runs Claude Code on whatever account the machine is set up with: a Claude Pro or Max
subscription, an API key, AWS Bedrock, Google Vertex or Microsoft Foundry. It does not need an
extra budget for Claude itself, only an API key for the (much smaller) checker models.

## What you get

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
- **A check on what subagents hand back.** A subagent's final result is reviewed against the
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

## Where calu fits in

Claude Code already asks you before it does something risky, and its auto mode can make that
decision on its own. Other harnesses work the same way. calu replaces none of it: it keeps that
judgment and puts a second, independent one that **you** own in front of it. A PASS leaves the
decision where it was; the other three answers are where your rules take over.

The two opinions are good at different things. The vendor's classifier knows what is generally
dangerous, and it knows nothing about your project. A checker you write yourself knows your
project and nothing about anything else. Together they cover more than either does alone, and
they fail differently: a prompt injection that talks one of them into approving a step still has
to get past the other, which reads different instructions and sees the step through a different
lens.

The second difference is *when* the judgment happens. Review after the fact catches a bad diff
once it exists, after Claude has built on it for twenty more steps. A checker sees each step
before it takes effect, so it can stop a direction instead of having to unwind it.

### What you can enforce with a checker

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

### Code honesty enforcement

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

### calu does not replace Claude Code

This is the part worth being explicit about: calu is a layer **on top of** Claude Code, not an
alternative to it. Claude Code stays the agent and keeps everything that makes it work:

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

```
   clients (any machine): terminal · IDE · web · herd overview
        │ WebSocket, JSON-RPC
   calu session process  ──────────────►  PostgreSQL
        │ (checkers, rules, escalations)    configuration, checker instructions,
   Claude Code                              every step, open escalations
        │
   Claude Code's own sandbox, configured by calu: the commands Claude runs
```

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
  write access to Claude Code's configuration, read denials for credentials — which is as far
  as the isolation goes today.

**Status: early implementation.** `calu run` starts a checked session, records it in the
database and waits for human decisions; the network clients, the web interface and the sandbox
defaults are not finished. The design and the order of work are in [PLAN.md](PLAN.md), the
current state in [HANDOFF.md](HANDOFF.md). calu is the successor of
[stay-in-the-loop](https://github.com/kadeng/stay-in-the-loop), which does the same checking
inside VSCode for a single user; calu reuses its core.

## Requirements

- Linux on the machine where Claude Code runs (clients may run anywhere)
- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- Claude Code, set up for your project (claude.ai login, AWS Bedrock, ...)
- PostgreSQL; for a simple local setup, Docker with Docker Compose (`calu db up`)
- An API key for the checker models (for example Mistral, Fireworks or AWS Bedrock)
- `bubblewrap` and `socat` for the sandbox

## Getting started

Not published yet; from a clone of this repository, `uv run calu ...` runs the commands below.

```bash
uv tool install calu      # not published yet
calu db up                # local PostgreSQL in Docker; asks for a new admin password
calu setup                # default checkers and rules; asks for a new session password
calu run -p "fix the failing test"   # start a session in the current directory
calu run --socket                    # ... serving its clients on a Unix socket, not a port
```

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
chain), and `claude.use_bedrock` is the separate switch that puts *Claude Code* on Bedrock. Without
`bubblewrap` and `socat` a session refuses to start, because the sandbox cannot be built; setting
`claude.sandbox.enabled` to false in the configuration turns the sandbox off, at the cost of its
protection.

While a session runs, another terminal — or another machine with access to the database — can
watch and unblock it:

```bash
calu sessions             # every session of every host, with its state
calu escalations          # steps that wait for a decision
calu escalation 12        # everything needed to decide that one
calu resolve 12 allow --note "go ahead"
calu resolve 13 answer "spaces, four of them"   # when Claude asked you something
calu attach ws://127.0.0.1:8765 --token ...     # the full event stream, and a prompt
```

Those first five need nothing but the database. That is deliberate: a session may run on a
machine you cannot reach, or have stopped since it asked, and a decision must not depend on
getting a connection to it. So the escalation itself carries what the decision needs — the
reason, the command line or the question with its options, the step as the checker rendered it,
the file before an edit, and the conversation as the checkers see it. The waiting session picks
your answer up within a second. What is *not* in the database is the live stream: the text as it
is written, the thinking, the tool results as they arrive. That is what attaching is for.

A browser shows every session of every host at once:

```bash
calu web          # http://127.0.0.1:8800, local only: there is no login yet
```

It leads with the sessions that cannot go on without you, opens one session's history at four
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

An editor that speaks ACP talks to the same session. Register `calu acp` as the agent's command
in the editor (JetBrains Air, Zed, Neovim, Goose); the editor spawns it and it attaches to the
running session:

```bash
calu acp ws://127.0.0.1:8765 --token ...
```

For a client that keeps its token in its own configuration, mint one instead of passing on the
token `calu run` printed: a minted token outlives the session you made it for, and you can take
it back.

```bash
calu token mint <session id> --label "zed on the laptop"   # prints the token once
calu token list                                            # labels and last use, never a token
calu token revoke 3
```

Your rules live in the database. Write them as Markdown, put them in, and measure whether the
checkers follow them:

```bash
calu checker edit edit                        # opens the edit checker's instructions in $EDITOR
calu rules edit command-deny                  # opens the whole deny list, one regex per line
calu config set claude.model opus
calu evals --from-db                          # accuracy, critical misses, over-escalations
```

`calu checker show NAME` and `calu checker set NAME --instructions-file FILE` do the same
without an editor, and `calu rules add|list|remove|enable|disable` work on single rules.

A running session picks up such a change within a moment, at its next step.

Not built yet: forking or rewinding a conversation (it needs support from Claude Code that may not exist) and the single-binary packaging. The ACP adapter has not yet been driven by a real editor, only by its own end-to-end test.

## License

GPL-2.0, see [LICENSE](LICENSE).
