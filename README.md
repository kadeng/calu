# calu

[![checks](https://github.com/kadeng/calu/actions/workflows/checks.yml/badge.svg)](https://github.com/kadeng/calu/actions/workflows/checks.yml)

## A coding agent that wraps Claude Code and checks every step while it runs

calu starts Claude Code for you and puts a second opinion in front of every command it runs and
every file it edits. It is a layer on top of Claude Code, not a replacement: everything Claude
Code does — its tools, its permission rules, its plugins, its MCP servers — keeps working
([details in the guide](CALU_GUIDE.md#calu-does-not-replace-claude-code)).

That second opinion comes from **checker agents**: LLMs that you steer with your own
instructions. Each checker answers one of four things:

| Answer | What happens | How often |
|---|---|---|
| PASS | the checker has no objection; Claude Code's own approval decides as usual | almost everything |
| ESCALATE | Claude is paused until a human allows or denies the step | the interesting cases |
| ALLOW | the step runs, whatever the auto-approval would have said | a deliberate exception |
| DENY | the step does not run; Claude is told why and carries on | a deliberate exception |

The two ends of that table are the exceptions. A checker is not there to re-decide what Claude
Code already decides well; it is there to notice the few steps that deserve a human, and to
overrule the harness in the handful of cases where you know better.

The pause matters: Claude cannot retry, rephrase or probe the system while it waits, so a
denial cannot be worked around. You answer from any attached client or from the database
itself, which does not require access to the machine the session runs on.

### Which problem does it solve?

Even the best model available today needs constant supervision to stay on track. Steering a car
is the same: you make small corrections all the time, and you do not notice making them. Stop
for twenty seconds and you are not slightly off the lane, you are somewhere else entirely. An
agent that runs for an hour without a correction does the same thing — it keeps working, it
keeps sounding reasonable, and it arrives somewhere you did not want to go, by a route that cost
many times what the trip should have cost. This is not a theoretical worry about future systems:
it is what long-running coding agents do today when nobody watches them often enough.

The correction itself is usually tiny — "not in that file", "you are reimplementing something
that exists", "that name does not say what it does". It is cheap when it happens at the step
that introduced the problem and expensive once twenty steps are built on top of it. What makes
it hard is not knowing what to say but being there to say it, every few steps, for an hour.
**calu makes those micro-corrections happen automatically**: checker agents watch every step and
pull Claude back on your behalf, and they stop and ask you only where a human really has to
decide.

Two things can only be had this way, at the step itself:

- **Security.** A command that exfiltrates a secret or rewrites history has done it by the time
  a reviewer reads the diff. The decision has to happen before the step runs, which is also the
  only place where a prompt injection can be caught before it takes effect.
- **Code honesty.** A name or a docstring that no longer matches the code is a lie the next
  reader believes, and a review after the fact cannot enforce it: during an incremental change
  the code *is* temporarily out of step with its documentation, and that is normal. What can be
  enforced is that it does not stay that way. calu's honesty checker records which files are out
  of step, and Claude may not run the code it marked until the names and the documentation line
  up again — which takes a watcher present at every step, holding the record.

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

Rules a checker can hold Claude to are the ones a linter cannot see: writing that sounds like a
language model, over-engineering while it happens, scope creep, rules that come from law or from
a contract, scientific standards in prose, and code honesty — every function says what it does,
in its name and in its documentation, so that a reviewer can judge a call site without reading
the implementation. The guide explains each of them: [what you can enforce with a
checker](CALU_GUIDE.md#what-you-can-enforce-with-a-checker) and [code honesty
enforcement](CALU_GUIDE.md#code-honesty-enforcement).

### What you get

- **Checkers with your rules.** One checker reviews commands, one reviews edits. Their
  instructions are plain Markdown that you edit. They see the step, the conversation in condensed
  form and your project's AGENTS.md, and they can read files, glob and grep to judge on evidence.
- **Comments, not only verdicts.** A checker can let a step through and still tell Claude to keep
  the names and the API documentation honest, or to stop widening the task. Claude gets the remark
  together with the approval.
- **Allow and deny lists before the models.** Harmless commands (`ls`, `git status`) run without a
  model call; known-dangerous ones (`sudo`, force pushes, reads of your credentials) always ask a
  human. Everything else goes to a checker, so the model cost is one call per interesting step.
- **A review of the whole change before it is committed.** `git commit` is held and the complete
  diff goes to a checker of its own: is the change complete, does it contain work nobody asked
  for, was a test weakened to make it pass, does the message describe the change.
- **A readable trail instead of a transcript.** Every step is summarized in one line by the
  checker that judged it. Supervising several agents means reading summaries and opening the
  details only where a summary looks wrong.
- **Unsupervised runs.** A session runs for hours — overnight, for example — and stops only when
  something needs you. It keeps running when no client is attached.
- **One database for every session on every machine.** Sessions, steps and open escalations live
  in one PostgreSQL database, so one person can watch and unblock any number of sessions from one
  place, including the ones on machines they cannot reach.
- **Claude's own questions reach you.** When Claude uses its question tool, you get the question
  with its options; your answer comes back as the result of that call.
- **Your editor, your browser or your terminal.** A terminal client, a web overview of the whole
  herd, and the agent side of the Agent Client Protocol (`calu acp`) for JetBrains Air, Zed,
  Neovim and Goose, where an escalation arrives as the editor's own permission dialog.
- **Comments on a selection.** Mark lines in a diff or in a project file and write a comment. It
  reaches Claude at once and is kept as potential training data for the checkers.
- Features that matter without being exciting: checker API keys are hidden from Claude Code and
  the configuration is out of its reach; Claude Code's sandbox is switched on and tightened;
  optional snapshots record what a command really wrote; a subagent's result is checked against
  the conversation; Claude's own text reaches the checkers only in indirect speech, narrated by a
  separate model, so a checker cannot mistake it for an instruction from you; a session that
  weakens its own settings writes an audit entry; a backward jump of the clock stops the session.
  [The guide has the detail](CALU_GUIDE.md#what-the-checkers-give-you-in-detail).

calu runs Claude Code on whatever account the machine is set up with: a Claude Pro or Max
subscription, an API key, AWS Bedrock, Google Vertex or Microsoft Foundry. It does not need an
extra budget for Claude itself, only an API key for the (much smaller) checker models.

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

One `calu run` process drives one Claude Code session. Clients attach over a WebSocket and
detach again; several can watch and steer the same session at once, and each gets the current
state plus the events it missed when it reconnects. Everything that a decision needs is in the
database, so answering an escalation never depends on reaching the session's machine.

**Status: early implementation.** `calu run` starts a checked session, records it in the
database and waits for human decisions; the network clients, the web interface and the sandbox
are built, and the sandbox's defaults are proposals the owner has still to confirm. The design
and the order of work are in [PLAN.md](PLAN.md), the current state in
[HANDOFF.md](HANDOFF.md). calu is the successor of
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
calu doctor               # what is in place, and what is still missing
calu db up                # local PostgreSQL in Docker; asks for a new admin password
calu setup                # default checkers and rules; asks for a new session password
```

The checkers need a key of their own, in `~/.config/calu/.env`. Claude Code never sees it:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/calu/.env
calu config set command_checker.model anthropic:claude-haiku-4-5
```

Register the project, which also decides what its commands may reach over the network, and
start a session:

```bash
calu project add . --name demo        # the proxy policy, with a starter domain allow list
calu run -p "fix the failing test"    # a session in the current directory
calu run --socket                     # ... serving its clients on a Unix socket, not a port
```

### A session in use

The session prints the steps as the checkers judge them and stops at the first escalation.
From another terminal — or another machine with access to the database — you see it and answer:

```bash
calu sessions             # every session of every host, with its state
calu escalations          # steps that wait for a decision
calu escalation 12        # everything the checker saw, so you decide on the same material
calu resolve 12 allow --note "go ahead"
```

Attach when you want the live stream, the thinking and the tool results as they arrive. In the
project directory it takes no arguments — it finds the session running there:

```bash
calu attach
```

A plain line you type there is a message for Claude; the rest are commands, and `/help` lists
them. The ones that matter while a step waits:

```
/why 7            everything the checker saw: its verdict, the step as it rendered it,
                  the file before the change, the conversation as the checkers read it
/allow 7 go ahead   /deny 7 not like that   /answer 7 four spaces
/inject <text>    reaches Claude inside the running turn instead of queueing
```

A browser shows every session of every host at once, leading with the ones that cannot go on
without you:

```bash
calu web          # http://127.0.0.1:8800, local only: there is no login yet
```

Then write your own rules, as Markdown in the database, and measure whether the checkers follow
them:

```bash
calu checker edit edit    # the edit checker's instructions, in $EDITOR
calu evals                # all four checkers against the built-in cases
```

## Where to go next

[CALU_GUIDE.md](CALU_GUIDE.md) is the documentation: the full command surface, the checker API
keys and the operator credentials, the sandbox and the network policy, remote access over the
database or an SSH tunnel, client tokens, the web interface, the ACP adapter, and the checker
and rule reference. Every setting with its type, its default and what it does is in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## License

GPL-2.0, see [LICENSE](LICENSE).

Contributing: [AGENTS.md](AGENTS.md) for the layout and the rules, [docs/TESTING.md](docs/TESTING.md) for the test layers and the manual checklist, [PLAN.md](PLAN.md) for the decisions behind all of it.
