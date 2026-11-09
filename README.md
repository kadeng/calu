# calu

**Status: early implementation. Nothing is usable yet.** The plan is in [PLAN.md](PLAN.md).

calu lets Claude Code work unsupervised for long stretches, for example overnight, while other
LLM agents ("checkers") and humans keep control over what it does.

## What calu will do

- **Run Claude Code in the background.** You start a session with `calu run` on a Linux machine
  (for example inside tmux). calu wraps Claude Code, runs it in its auto mode, and keeps running
  when you disconnect.
- **Check every step.** Before a command runs or a file is edited, calu applies your allow and
  deny lists and asks checker agents (LLMs with your instructions for safety, style and
  documentation). A step runs only if the checkers and Claude Code's auto mode both approve it.
  Checkers can also leave remarks for Claude.
- **Ask a human when needed.** A checker can escalate a step: the session pauses until someone
  allows or denies it, from any connected interface or directly in the database.
- **Keep the agent away from the controls.** Commands that Claude runs are executed in a
  sandbox. They cannot reach calu, its database credentials or its configuration, and cannot
  change how Claude Code reports to calu.
- **Attach from anywhere.** Terminal, IDE and web clients connect to a running session over a
  WebSocket; several can watch and steer the same session at once.
- **Manage many sessions.** All sessions report to one PostgreSQL database: configuration,
  checker definitions, the full history of every step (with host, user, directory and git
  commit) and the open escalations. From there you can watch and unblock any number of sessions.

## Planned requirements

- Linux on the machine where Claude Code runs
- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- Claude Code, set up for your project (claude.ai login, AWS Bedrock, ...)
- PostgreSQL; for a simple local setup, Docker with Docker Compose (`calu db up`)
- An API key for the checker models (for example Mistral, Fireworks or AWS Bedrock)

## Getting started (planned)

```bash
uv tool install calu      # not published yet
calu db up                # local PostgreSQL in Docker; asks for a new admin password
calu setup                # default checkers and rules; asks for a new session password
calu project add .        # register the current project
calu run                  # start a session; attach with: calu attach <session>
```

## License

GPL-2.0, see [LICENSE](LICENSE).
