# calu

## A coding agent that wraps Claude Code and checks every step while it runs

calu starts Claude Code for you and puts a second opinion in front of every command it runs and
every file it edits. That second opinion comes from **checker agents**: LLMs that you steer with
your own instructions. Each checker answers one of three things:

| Answer | What happens |
|---|---|
| ALLOW | the step runs; the checker may add a short remark that Claude receives with the approval |
| DENY | the step does not run; Claude is told why and carries on |
| ESCALATE | time freezes: Claude is paused until a human allows or denies the step |

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
  [what you can enforce](#what-you-can-enforce-with-a-checker)). They get high-level context
  only — the conversation in condensed form, your project's AGENTS.md and the step itself —
  because they judge the step, not the whole program. Overall correctness stays with a review
  of the finished changeset.
- **A check on what subagents hand back.** A subagent's final result is reviewed against the
  conversation, so an adversarial reviewer subagent cannot silently widen a task that you
  deliberately kept small.
- **Claude's own words, retold.** What Claude thinks and writes reaches the checkers only in
  indirect speech, narrated by a separate model ("Claude claimed that the user had allowed
  this"), so a checker cannot mistake Claude's text for an instruction from you.
- **Comments on a selection.** Mark lines in a diff or in any project file and write a comment.
  It reaches Claude at once and is kept as potential training data for the checkers.
- **Unsupervised runs.** Claude Code works in its auto mode with the checkers on top of it, so a
  session can run for hours — overnight, for example — and stops only when something genuinely
  needs you.

## Where calu fits in

Claude Code already asks you before it does something risky, and its auto mode can make that
decision on its own. Other harnesses work the same way. calu does not replace any of this: it
adds a second, independent opinion that **you** own, and a step runs only if both agree.

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

Checkers are useful for everything that is obvious to a human reader but invisible to a linter:

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

### Code honesty, and why it is a security measure

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

### When plain Claude Code is the better tool

If you sit in front of the session and read every diff as it appears, you are already the
checker, and calu mostly adds latency and the cost of the checker models. calu earns its place
when the session is long, when it runs while you are not watching, when several sessions run at
once, or when the rules you care about are too specific for a linter and too repetitive to
restate every day.

## How it is put together

```
   clients (any machine): terminal · IDE · web · herd overview
        │ WebSocket, JSON-RPC
   calu session process  ──────────────►  PostgreSQL
        │ (checkers, rules, escalations)    configuration, checker instructions,
   Claude Code                              every step, open escalations
        │
   sandbox: the commands Claude runs
```

- **The core is headless.** One `calu run` process drives one Claude Code session and keeps
  running when nobody is watching. Clients attach over a WebSocket and detach again; several can
  watch and steer the same session at once, and each gets the current state plus the events it
  missed when it reconnects.
- **The database is the centre.** Every session on every machine writes to one PostgreSQL
  database: the configuration, the checker definitions, the full history of every step (with
  host, user, directory and git commit) and the open escalations. One person can therefore watch
  and unblock any number of sessions from one place, and nothing that steers the checkers is
  read from a file that the agent could edit.
- **The agent is kept away from the controls.** Commands that Claude runs are executed in a
  sandbox: their own process namespace, no access to the calu process, to its database
  credentials or to your keys, and no way to change how Claude Code reports back.

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
```

Checker API keys belong in `~/.config/calu/.env`; Claude Code never sees them. Without
`bubblewrap` and `socat` a session refuses to start, because the sandbox cannot be built; setting
`claude.sandbox.enabled` to false in the configuration turns the sandbox off, at the cost of its
protection.

While a session runs, another terminal — or another machine with access to the database — can
watch and unblock it:

```bash
calu sessions             # every session of every host, with its state
calu escalations          # steps that wait for a decision
calu resolve 12 allow --note "go ahead"
```

Not built yet: `calu attach` and the other clients, `calu project add`, `calu config set`.

## License

GPL-2.0, see [LICENSE](LICENSE).
