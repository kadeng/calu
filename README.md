# calu

[![checks](https://github.com/kadeng/calu/actions/workflows/checks.yml/badge.svg)](https://github.com/kadeng/calu/actions/workflows/checks.yml)

## A coding agent that wraps Claude Code and checks every step while it runs

calu starts Claude Code for you and puts a second opinion in front of every command it runs and
every file it edits. It is a layer on top of Claude Code, not a replacement: its tools, its
permission rules, its plugins, its MCP servers, its skills and subagents all keep working, as
the project configures them ([which settings files are read, and
why](CALU_GUIDE.md#calu-does-not-replace-claude-code)).

That second opinion comes from **checker agents**: LLMs that you steer with your own
instructions. Each checker answers one of four things:

| Answer | What happens | How often |
|---|---|---|
| PASS | the checker has no objection; in auto mode Claude Code's own approval decides as well, in any other mode the step runs | almost everything |
| ESCALATE | Claude is paused until a human allows or denies the step | the interesting cases |
| ALLOW | the step runs, whatever the auto-approval would have said | a deliberate exception |
| DENY | the step does not run; Claude is told why and carries on | a deliberate exception |

The two ends of that table are the exceptions. A checker is not there to re-decide what Claude
Code already decides well; it is there to notice the few steps that deserve a human, and to
overrule the harness in the handful of cases where you know better.

The pause matters: while an escalation is open, the session's other tool calls that need a
check wait too, so Claude cannot retry, rephrase or probe the system before a human has decided.
You answer from any attached client or from the database itself, which does not require access
to the machine the session runs on.

### Which problem does it solve?

Even the best model available today needs constant supervision to stay on track. Steering a car
is the same: small corrections all the time, and you barely notice making them — but stop for
twenty seconds and you are not slightly off the lane, you are somewhere else. An agent left
alone for an hour does that: it keeps working, it keeps sounding reasonable, and it arrives
somewhere you did not want to go, by a route that cost many times what the trip should have.
This is not a worry about future systems; it is what long-running coding agents do today when
nobody watches them often enough.

The correction itself is usually tiny — "not in that file", "you are reimplementing something
that exists", "that name does not say what it does" — and it is cheap at the step that caused
the problem, expensive twenty steps later. What is hard is being there to say it, every few
steps, for an hour. **calu makes those micro-corrections happen automatically**: checker agents
watch every step, pull Claude back on your behalf, and stop to ask you only where a human has
to decide.

Two things can only be had this way, at the step itself:

- **Security.** A command that exfiltrates a secret or rewrites history has done it by the time
  a reviewer reads the diff. The decision has to happen before the step runs, which is also the
  only place where a prompt injection can be caught before it takes effect.
- **Code honesty.** A name or a docstring that no longer matches the code is a lie the next
  reader believes, and no review after the fact can enforce it: while a change is in progress
  the code *is* legitimately out of step with its documentation. What can be enforced is that it
  does not stay that way. calu's honesty checker records which files are out of step and refuses
  to run them until the names and the documentation line up again — which takes a watcher at
  every step, holding that record.

Claude Code already asks you before it does something risky, and its auto mode can make that
decision on its own. Other harnesses work the same way. calu replaces none of it: it keeps that
judgment and puts a second, independent one that **you** own in front of it. A PASS leaves the
decision where it was; the other three answers are where your rules take over.

The two opinions are good at different things. The vendor's classifier knows what is generally
dangerous and nothing about your project; a checker you write knows your project and nothing
else. They also fail differently, so a prompt injection that talks one into approving a step
still has to get past the other.

Both opinions apply only in Claude Code's auto mode. A headless Claude Code starts in the
permission mode `default` unless a setting says otherwise, and the checkers then decide alone.
`calu config set claude.permission_mode auto` switches the auto mode on;
`calu config set claude.require_auto_mode true` makes a step that the checkers pass outside the
auto mode wait for a human. Every session start and `calu doctor` name the mode in effect.

What a checker can hold Claude to is what a linter cannot see: writing that sounds like a
language model, over-engineering, scope creep, rules from a law or a contract, and code honesty
— every function says what it does, in its name and in its documentation. The guide lists them:
[what you can enforce](CALU_GUIDE.md#what-you-can-enforce-with-a-checker) and [code honesty
enforcement](CALU_GUIDE.md#code-honesty-enforcement).

### What you get

**The supervision.**

- **Checkers with your rules.** One reviews commands, one reviews edits. Their instructions are
  plain Markdown that you edit. They see the step and the conversation in condensed form, and
  they can read files, glob and grep in the project to judge on evidence. Their rules are the
  instructions you give them, not files of the repository: put what your AGENTS.md asks for
  into the checker instructions.
- **Comments, not only verdicts.** A checker can let a step through and still tell Claude to keep
  the names and the documentation honest, or to stop widening the task.
- **Allow and deny lists before the models, if you want them.** The lists start empty, so every
  step goes to a checker. `calu rules add-set recommended` adds a ready-made set: `ls` and
  `git status` then run without a model call, and `sudo`, force pushes and reads of your
  credentials always ask a human.
- **A review of the whole change before the commit.** The command checkers judge a `git commit`
  like every command. A `git commit` that runs as a command of its own is also held while the
  complete diff goes to a checker of its own: is it complete, is there work nobody asked for,
  was a test weakened to make it pass, does the message describe the change. A line that
  combines the commit with other commands (`git add ... && git commit`) is refused, with the
  instruction to stage first and commit alone.
- **A readable trail.** One line per step, written by the checker that judged it. Supervising
  several agents means reading summaries, not transcripts.
- **Unsupervised runs.** A session runs for hours, overnight, with no client attached, and stops
  only when something needs you.

**And the parts around it.**

- **Central fleet management.** Sessions, steps and open escalations live in one PostgreSQL
  database: watch and unblock any number of agents on any number of machines from one place,
  including machines you cannot reach.
- **Four front ends.** A web interface for the whole fleet, a terminal client, the Agent Client
  Protocol for JetBrains Air, Zed, Neovim and Goose, and comments on a selection of lines in
  your editor. Answering the agent's questions, reading the chat, inspecting diffs and allowing
  or denying a step work in all of them; an escalation arrives in an ACP editor as its own
  permission dialog.
- **Claude Code is not replaced, and its security model still applies.** Its permission rules,
  auto mode, plugins, MCP servers and sandbox keep working, and calu runs it on whatever account
  the machine is set up with — a Claude Pro or Max subscription, an API key, **AWS Bedrock**,
  Google Vertex or Microsoft Foundry.
- **The checkers bring their own provider.** A Claude Code subscription authenticates the
  `claude` CLI and not other programs, so the checkers use a key of their own — the Anthropic
  Console API, AWS Bedrock, Fireworks, Mistral, a local model, or anything else
  [Pydantic AI supports](https://pydantic.dev/docs/ai/models/overview/) — and Claude Code never
  sees it. Providers that offer zero data retention, in a variety of cloud regions including the
  EU, work too; that guarantee is theirs, not calu's. With **no API key at all**, a checker can
  run on the machine's own Claude Code account (`claude:haiku`), at the price of having no tools
  and a process start per check.
- **The checkers are read-only by design**: five tools, all reads (open a file, find, grep, and
  which files changed when), which calu confines to the project directory and bounds in file
  size and search time. No shell, no writes, no network — the worst a broken checker can do is
  answer wrongly, and a step that matches a deny rule goes to a human before any model is
  called.
- **A mid-sized model can supervise a frontier model.** It answers one narrow question about one
  step, with instructions for that question and an answer out of four. What is hard to slip past
  is the arrangement, not the checker's IQ: a violation **freezes** the agent until a human
  answers, so there is no second attempt to probe with; with code honesty enforced, misdescribed
  code may not even be *run*; and the instructions the checkers judge by live in a database the
  session cannot write. Claude's own words reach a checker only as indirect speech, retold by a
  separate narrator — "Claude claimed that the user had already allowed pushing" — so an
  imperative smuggled in through a web page or a dependency's README arrives as a report that
  someone wrote it, not as an instruction.
  [Each agent, and the narration, in the guide](CALU_GUIDE.md#the-models-claude-codes-account-and-the-checkers-own).
- Quiet but necessary: calu's configuration is out of Claude's reach, the sandbox is switched on
  and tightened, a session does not start while a Claude Code settings file changes the sandbox,
  a subagent's result is checked against the conversation, a session that weakens its own
  settings writes an audit entry.
  [The guide has the detail](CALU_GUIDE.md#what-the-checkers-give-you-in-detail).

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

One `calu run` process drives one Claude Code session. Clients attach and detach over a
WebSocket, several at once, and each gets the current state plus the events it missed. What a
decision needs is in the database, so answering an escalation never depends on reaching the
session's machine. If the database becomes unreachable, the session reconnects by itself; until
then its steps wait and nothing is approved.

**Status: early implementation.** Everything above is built; the sandbox's defaults are still
proposals the owner has to confirm, and calu is not published on PyPI yet. The design is in
[PLAN.md](PLAN.md), the current state in [HANDOFF.md](HANDOFF.md). calu succeeds
[stay-in-the-loop](https://github.com/kadeng/stay-in-the-loop), which checks the same way
inside VSCode for a single user.

## Requirements

- Linux on the machine where Claude Code runs (clients may run anywhere)
- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- Claude Code, set up for your project (claude.ai login, AWS Bedrock, ...)
- PostgreSQL; for a simple local setup, Docker with Docker Compose (`calu db up`)
- An API key for the checker models (for example Mistral, Fireworks or AWS Bedrock)
- `bubblewrap` and `socat` for the sandbox

## Getting started

Not published yet; from a clone, `uv run calu ...` runs every command below.

```bash
uv tool install calu      # not published yet
calu doctor               # what is in place, and what is still missing
calu db up                # local PostgreSQL in Docker; asks for a new admin password
calu setup                # default checkers and rules; asks for a new session password
```

The checkers need credentials of their own -- AWS for Bedrock, or a key in
`~/.config/calu/.env`, which Claude Code never sees. `calu setup` picks the model from what it
finds (Bedrock, then Anthropic, Fireworks, Mistral); `calu config models` switches every role at
once later:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/calu/.env
calu config models auto
```

Register the project, which also sets what its commands may reach over the network, and start a
session:

```bash
calu project add . --name demo        # the proxy policy, with a starter domain allow list
calu run -p "fix the failing test"    # a session in the current directory
```

`calu run` uses the project registered for the working directory or for the nearest registered
directory above it; `--project NAME` names one, and an unknown name ends the start with an
error.

`calu run -p "..." --batch` ends when Claude is done instead of waiting for your next message.
It also waits for Claude Code's background tasks, because Claude continues by itself when they
finish; `--background-timeout 600` stops a run whose background task (a dev server, say) never
ends. Without `--batch`, a closed stdin (a service unit, `nohup`) has the same effect once a
prompt or a message was sent: the run ends when Claude is done.

`calu run` stays in the foreground and prints every step as the checkers judge it. **On a server
you put it in a detachable session**, so it keeps working after you log out:

```bash
tmux new -s calu                      # then `calu run -p "..."` inside; Ctrl-B D detaches
tmux attach -t calu                   # back to it, from anywhere you can ssh to
```

`screen -S calu` does the same, and so does `systemd-run --user --scope`. Note what you do *not*
need tmux for: clients attach and detach on their own, and every escalation can be answered from
the database, so you only need the multiplexer to keep the session process itself alive.

### Four ways to work with a running session

**The terminal client**, for the live stream, the thinking and the tool results as they arrive.
In the project directory it takes no arguments — it finds the session running there:

```bash
calu attach
```

A plain line you type is a message for Claude, and `/help` lists the commands. While a step
waits: `/why 7` shows everything the checker saw, `/allow 7 go ahead` and `/deny 7 not like
that` decide it, and `/inject <text>` reaches Claude inside the running turn instead of queueing
behind it.

**The browser**, which shows every session of every host at once, leading with the ones that
cannot go on without you — plus each session's history, its diffs, the triage queue, the
settings and the checker instructions:

```bash
calu web          # prints http://calu.localhost:8800/?token=...: open it once per browser
```

From another machine, forward the port and open the same printed address there:
`ssh -L 8800:127.0.0.1:8800 the-host`.

**Your editor**, if it speaks the Agent Client Protocol (JetBrains Air, Zed, Neovim, Goose).
Register calu as the agent and open the project; an escalation arrives as the editor's own
permission dialog, including one that was already open when the editor attached:

```json
{ "command": "calu", "args": ["acp"] }
```

```bash
calu credential set operator --no-password   # the adapter has no terminal to ask on
```

**The database alone**, from any machine that can reach PostgreSQL — no access to the session's
host, and nothing to keep open:

```bash
calu sessions             # every session of every host, with its state
calu escalations          # steps that wait for a decision
calu escalation 12        # everything the checker saw, so you decide on the same material
calu resolve 12 allow --note "go ahead"
```

Then write your own rules, as Markdown in the database, and measure whether the checkers follow
them:

```bash
calu checker edit edit    # the edit checker's instructions, in $EDITOR
calu evals                # all four checkers against the built-in cases
```

## Where to go next

[CALU_GUIDE.md](CALU_GUIDE.md) is the documentation: every command, the credentials, the
sandbox and network policy, remote access, the front ends, and the checker and rule reference.
Every setting with its type and default is in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## License

GPL-2.0 ([LICENSE](LICENSE)). That applies to **calu itself**: it is free software and stays
free software, and a fork or modified version you distribute stays GPL-2.0 too.

It says nothing about the software you develop with calu. calu is a tool you run, like a
compiler or a test runner: your repository can be closed source, commercial, or under any open
source licence, and code written with calu's help is yours under whatever terms you choose. Your
checker instructions and rules are your own text, and what calu produces as it runs — verdicts,
comments, summaries — carries no licence condition either.

calu comes with **no warranty and no guarantees of any kind, and is provided as is, free of
charge** — and a checker is a language model reading a step, not a promise that nothing bad
gets through.

The checker instructions **shipped with calu** are not under the GPL at all: they are prompts,
written to be copied and rewritten, and they are placed in the public domain as far as
applicable law allows. Copy one, change it, license the result however you like — nothing
limits that and there is no attribution to carry. [LICENSE.md](LICENSE.md) has the wording.

Contributing: [AGENTS.md](AGENTS.md) for the layout and the rules, [docs/TESTING.md](docs/TESTING.md) for the test layers and the manual checklist, [PLAN.md](PLAN.md) for the decisions behind all of it.
