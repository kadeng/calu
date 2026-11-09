# calu: requirements

This file holds only requirements and priorities stated by the project owner, reworded but not
broken down or extended. Derived requirements and design decisions belong in
[PLAN.md](PLAN.md).

## Hard requirements

1. calu works similarly to stay-in-the-loop: LLM checker agents check and comment on a main LLM
   agent that does the coding.
2. calu lets Claude Code sessions run unsupervised for longer stretches (for example overnight),
   using Claude Code's auto mode on top of the checkers.
3. Linux only, on the machine where Claude Code runs.
4. At its core, calu consists of a standalone command-line tool and a PostgreSQL database.
5. The command-line tool configures a Claude-based project to use the checker agent hooks and
   sets up and configures a coding project, without interfering too much with an existing Claude
   Code configuration.
6. Above all, the command-line tool launches and wraps Claude Code, like stay-in-the-loop does.
7. Unlike stay-in-the-loop, which runs inside an IDE, calu runs independently and is meant to run
   in the background (for example in tmux).
8. User interfaces (IDE plugin, TUI client, web interface) connect to calu over WebSocket with
   JSON-RPC.
9. Several user interfaces can be connected to the same Claude Code session; a message-bus-like
   architecture likely makes sense.
10. calu keeps a headless state similar to stay-in-the-loop's; the state can be queried, and
    updates are streamed to subscribed clients.
11. The database is central to the design. It does not receive real-time token streams. It holds:
    - the full session information of each session step, including hostname, OS user, project
      directory, git commit and process id;
    - the definitions and instructions of all checker agent types (the narrator instructions may
      be hard-coded instead);
    - the checker history (narrated steps etc.);
    - the state (running, idle, stopped, escalation, checker running, tool call);
    - escalated decisions with their full context.
12. Resolving an escalation in the database is possible and unlocks the session, so that it can
    continue.
13. Claude Code sessions can be launched on servers and attached to with a selection of IDEs or
    TUI clients; herd management through the database allows monitoring and unlocking as many
    agent sessions as needed.
14. Tool calls are isolated in a sandbox (a regular sandboxing approach). Claude Code itself is not
    put into the same box as its tools, because tools running in the same user space as Claude
    Code could always change how Claude Code passes information to calu.
15. The calu process, its database connection and the instructions are out of reach of the Claude
    Code session and are not stored locally.
16. calu connects to the database at start, for example with a password or a decoded secret key;
    the credentials that allow this connection are dropped before Claude Code is started.
17. Starting a session is password protected (for example by decoding a secret with that password,
    strengthened by rehashing).
18. For simple setups, calu can launch PostgreSQL locally ad hoc; Docker and Docker Compose may be
    assumed to be present.
19. For development and testing, the project has a `docker-compose.yml` with a simple Linux system
    (`dev`) and a PostgreSQL database (`caludb`).
20. All configuration and security settings live in the database, so that securing the database
    and the calu process secures everything that would allow an agent to bypass the checker
    agents.
21. calu is written in Python, first as a package installable with uv plus a binary.
22. The model layer follows Pydantic AI's patterns and supports AWS Bedrock and the Anthropic
    subscription out of the box.

## Soft goals and priorities

1. Simple requirements first: calu should become functional before anything else; a port to Go
   or Rust remains possible later.
2. A single executable that bundles setup, configuration and server functionality and can install
   additional components (PyApp or PyInstaller, later).
3. Fireworks.ai and Mistral as model providers.
4. The isolation mechanism is not decided yet; options are kept open, including light isolation
   with bubblewrap (own PID and mount namespace, selected folders hidden or read-only) and
   separate users. A separate user may not be feasible in the intended usage scenarios.
5. The relationship between calu and stay-in-the-loop is decided later.
