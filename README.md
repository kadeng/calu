# calu

[![checks](https://github.com/kadeng/calu/actions/workflows/checks.yml/badge.svg)](https://github.com/kadeng/calu/actions/workflows/checks.yml)

> **Flynn:** "Who's that guy?"
> **Program:** "That's Tron. He fights for the Users."
>
> — *Tron* (1982)

## Staying in control of a coding agent without babysitting it

Coding agents got fast. Fast enough that reading every step no longer scales, and good enough
that it is tempting to stop reading at all. We tried both. Reading everything means you do
nothing else. Not reading means the agent keeps working, keeps sounding reasonable, and an
hour later has arrived somewhere you did not want to go.

calu is our attempt at the ground in between. It runs [Claude Code](https://claude.com/claude-code)
for you and has a second set of eyes on every command it runs and every file it edits:
**checker agents**, small models with instructions you write. They let nearly everything
through, pull Claude back where it drifts, and stop and ask you only when a step needs a
person. While they ask, the agent waits, so there is nothing to retry or talk its way around.
What happened stays readable afterwards: one line per step, and if you want, the whole
conversation as a redacted file next to your code.

It is a layer on top of Claude Code, not a replacement. Its tools, permission rules, auto mode,
plugins, MCP servers, skills and subagents keep working as your project configures them
([which settings are read, and why](CALU_GUIDE.md#calu-does-not-replace-claude-code)).

![An approval card in the VSCode extension: Claude Code asked to edit a shell profile, the checker's remark is above, Allow and Deny below](docs/assets/vscode-approval.png)

**Status: early, and used by the people who write it.** Things work, things are rough, and the
list of what we have not tested is in [HANDOFF.md](HANDOFF.md). It is not on PyPI yet. calu
succeeds [stay-in-the-loop](https://github.com/kadeng/stay-in-the-loop), which did the same
inside VSCode for one user.

## What is in the box

**Front ends, all on the same sessions.** Start a session in one, watch or answer from another.

- **A VSCode extension** ([extensions/vscode](extensions/vscode)): a chat on the right like the
  one you know, with what calu adds one click away. Inline images, rewind and fork from any
  message, several sessions with names, slash commands, model and thinking level, context usage,
  questions and approvals as cards, notifications when something waits. A click on an edit
  opens the diff; a click on the file name opens the file at the line. It runs wherever your
  workspace is: WSL, SSH, a dev container.
- **A PyCharm plugin** ([extensions/jetbrains](extensions/jetbrains)), in the works: the same
  chat as a native tool window, built for JetBrains' remote development and for a local IDE
  that reaches the server over SSH and syncs files through calu.
- **A web interface** (`calu web`): every session of every machine, the ones that need you
  first; history, diffs, a live view, settings.
- **A full-screen terminal client** (`calu attach`), and a line client for pipes.
- **Any editor that speaks the Agent Client Protocol** (`calu acp`): Zed, Neovim, JetBrains'
  agent chat.
- **The database alone**: `calu sessions`, `calu escalations`, `calu resolve 12 allow`. A
  decision never depends on reaching the machine the session runs on.

![Subagents as tabs in the VSCode extension](docs/assets/vscode-subagents.png)

**Subagents, in full.** Each gets its own tab or view with the task it was given and what it
returned; you can message a background subagent; a step of one that waits for you shows up
wherever you are looking.

![Ten subagents in the web live view, one waiting for a decision](docs/assets/web-live-subagents.png)

**Sessions you can leave alone.** `calu serve` runs in tmux on a server, asks for its password
once (or never, if you set it up that way) and starts sessions on request. They run for hours
with no client attached and stop only when something needs you. Conversations can be resumed,
forked, and rewound to before any message, including ones you started with plain `claude`.

**The conversation as a file you can review.** Chat sessions are a kind of source code. With
`traces.enabled`, calu appends every step as Markdown to
`.agent/traces/<date>/<user>/session_NNN.md` inside the project, after a redaction pass that
replaces secrets and personal data. Commit it with the change it produced.
([Session traces](CALU_GUIDE.md#session-traces-the-conversation-as-a-reviewable-file))

**Code that says what it does.** A name or a docstring that no longer matches the code is a
lie the next reader believes, and agents produce those at speed. With honesty checking on, calu
tracks which code was verified to say what it does, file by file, and can refuse to run or
commit the rest. ([Code honesty](CALU_GUIDE.md#code-honesty-enforcement))

**Settings you can find.** Checker instructions are plain Markdown, editable from every front
end. One step sets one model for every role, with presets for Anthropic, AWS Bedrock,
Fireworks, Mistral, or your Claude Code account without any API key.

![The settings dialog of the VSCode extension](docs/assets/vscode-settings.png)

## How the checking works

> **Alan Bradley:** "It's called Tron. It's a security program itself, actually. It monitors
> all contacts between our system and other systems. Finds anything going on that's not
> scheduled, it shuts it down. … It'll run independently and watchdog the MCP as well."
>
> — *Tron* (1982)

Before a step runs, a checker reads it together with a condensed view of the conversation and
answers: nothing to say (the usual case), go ahead with a remark for Claude, don't (Claude is
told why and carries on), or a person should look. The last one pauses the session until
somebody answers, from any front end or straight from the database.

Three things about that arrangement matter more than how smart the checker is:

- **Two opinions that fail differently.** Claude Code's own auto mode knows what is generally
  dangerous and nothing about your project. Your checkers know your project's rules and
  nothing else. The checker instructions calu ships are short on purpose and leave general
  safety to auto mode, so **switch it on**: `calu config set claude.require_auto_mode true`.
  calu warns at every start while it is off.
- **Claude's words reach a checker as hearsay.** A separate narrator retells them in indirect
  speech ("Claude claimed that the user had already allowed pushing"), so an instruction
  smuggled in through a web page arrives as a report that someone wrote it.
- **The rules live where the session cannot write.** Instructions, settings and the record of
  every step are in PostgreSQL, behind a login the session does not have. Checkers are
  read-only: five tools, all reads, confined to the project.

What a checker can hold Claude to is what a linter cannot see: scope creep, over-engineering,
a test weakened to pass, a commit that does not match its message, rules from a contract.
([What you can enforce](CALU_GUIDE.md#what-you-can-enforce-with-a-checker))

```
   VSCode · PyCharm · browser · terminal · ACP editors
        │ calu ide (bridge) or WebSocket, JSON-RPC
   calu session process  ──────────────►  PostgreSQL
        │ (checkers, rules, escalations)    settings, checker instructions,
   Claude Code                              every step, open decisions
        │
   Claude Code's own sandbox, configured by calu
```

## Getting started

You need Linux where Claude Code runs (clients may be anywhere), Python 3.14 with
[uv](https://docs.astral.sh/uv/), Claude Code set up for your project, PostgreSQL (calu can
start one in Docker, or use a server or RDS you have), credentials for the checker models, and
`bubblewrap` and `socat` for the sandbox.

```bash
uv tool install git+https://github.com/kadeng/calu   # one package, every command
calu doctor               # what is in place and what is missing
calu setup                # picks or creates the database; asks for two passwords
```

The checkers need a key of their own, which Claude Code never sees. By default every role uses
Claude Haiku on the Anthropic API:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/calu/.env
calu config models auto                          # or pick one model for every role
calu config set claude.require_auto_mode true    # the checkers rely on auto mode
```

Register the project and start working:

```bash
calu project add . --name demo        # also sets what its commands may reach on the network
calu run -p "fix the failing test"    # one session, here, in the foreground
```

`calu run` and `calu serve` print only what needs your attention; the conversation is in the
clients. For more than one session, keep a server running and ask it:

```bash
tmux new-session -A -s calu 'calu serve'   # detach with Ctrl-B D; it keeps running
calu start                                 # a session in this directory, attached
calu attach                                # the full-screen client on the session here
calu web                                   # prints the address of the web interface
```

**The VSCode extension** is not on the marketplace yet. Build it and install the file:

```bash
cd extensions/vscode && npm ci && npm run package   # writes calu.vsix
```

Then "Extensions: Install from VSIX…" in VSCode 1.106 or newer. It finds `calu` itself, offers
to install uv and calu where they are missing, tells you how to start a server if none
answers, and checks that Claude Code is there (it does not install it).

**Running the agent as a user that cannot touch calu.** With `--sudo`, calu runs as root and
Claude Code as you, so nothing the agent starts can read calu's credentials or approve its own
steps: `calu serve --sudo`, `calu attach --sudo`.
([calu as root](CALU_GUIDE.md#calu-as-root-claude-code-as-another-user)) If you would rather
have no passwords at all on a machine that is yours alone, `sudo calu setup --no-password`
does that, and says plainly what it gives up.

Then write your rules and see whether the checkers follow them:

```bash
calu checker edit command    # the command checker's instructions, in $EDITOR
calu evals                   # the checkers against the built-in cases
```

## Where to go next

[CALU_GUIDE.md](CALU_GUIDE.md) is the documentation: every command, credentials, the sandbox
and network policy, remote access, the front ends, checkers and rules. Every setting with its
type and default is in [docs/CONFIGURATION.md](docs/CONFIGURATION.md). The decisions behind it
all are in [PLAN.md](PLAN.md); how it is tested, including the things we drive by screenshot
in a container, in [docs/TESTING.md](docs/TESTING.md) and
[docs/notes/test-plan.md](docs/notes/test-plan.md). Contributing starts at
[AGENTS.md](AGENTS.md).

## License

GPL-2.0 ([LICENSE](LICENSE)), for **calu itself**: it is free software and stays free
software, and a fork you distribute stays GPL-2.0 too.

It says nothing about what you build with calu. It is a tool you run, like a compiler: your
repository can be closed, commercial or under any licence, and code written with calu's help
is yours. Your checker instructions and rules are your own text, and what calu produces as it
runs carries no licence condition.

The checker instructions **shipped with calu** are not under the GPL: they are prompts, meant
to be copied and rewritten, and are placed in the public domain as far as the law allows.
[LICENSE.md](LICENSE.md) has the wording.

calu comes with **no warranty of any kind**. A checker is a language model reading a step,
not a promise that nothing bad gets through.

> **Dr. Walter Gibbs:** "Computers and programs will start thinking, and the people will stop."
>
> — *Tron* (1982)

The point of all this is that we don't.
