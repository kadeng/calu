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
23. calu has proper plugins for VSCode (which may be modelled on stay-in-the-loop's extension)
    and for the JetBrains IDEs (PyCharm, RustRover). Both offer a full coding-agent interface
    and the settings calu adds on top, and let the user connect or attach to sessions. Both
    must also work in the most modern remote development mode the IDE supports (PyCharm has
    recently added remote functionality for this). They are implemented only after the
    higher-priority items (the terminal client, the web IDE, standalone use) work well.

24. calu is installed with `uv tool install`, from the GitHub URL first and from PyPI when it
    all works; `uv tool install calu` pulls in everything, without optional sub-packages.
25. PostgreSQL can be set up in three variants, and the calu tool sets each one up: (1) Docker
    locally, where calu creates the database service and launches it on demand when `calu run`
    is called, with its data directory in a location that can be overridden during setup;
    (2) a preexisting PostgreSQL server, for which setup just asks host, port, user, password
    and database; (3) a preexisting server with AWS authentication for RDS.
26. calu itself runs privileged and Claude Code runs as an unprivileged user (stated
    2026-11-13; this replaces "the credentials are dropped before Claude Code is started" of
    requirement 16: calu keeps its credentials, which RDS IAM tokens need anyway, and
    reconnects with them). `calu run` is invoked either as root or by a user who has sudo.
    - Invoked by a user with sudo rights (sudo is password protected): calu runs itself again
      through sudo with elevated privileges and passes the original user on the command line;
      that user is who Claude Code runs as.
    - Invoked as root: the user to run Claude Code as must be given explicitly (it may be
      root, but only when passed explicitly). A numeric uid makes more sense than a name:
      inside a Docker container calu is root and the user outside may be known only by its
      uid (typically 1000), which need not have a name or a home directory there. Running
      Claude Code under that uid also makes files in a project directory that is mapped from
      the host come out owned by the unprivileged host user instead of root.
    - Given a uid, calu provides the directory Claude Code keeps its own state in (its login
      and its conversations) and makes it that process's home; given a user name, that user is
      assumed to have a home directory, and Claude Code uses it. When calu runs itself again
      through sudo it therefore passes the user name, not only the uid.
    - Hard restriction: calu never adds users, sudo-capable or otherwise.
    - calu's own installation (Python runtime, libraries, source), its privileged
      configuration and its privileged data (the database files) live in system directories
      or below `/root`, never in the unprivileged user's home directory: for example
      `/etc/calu`, `/var/lib/calu/db`, `/usr/local/bin/calu`, `/usr/local/lib/calu`, in a way
      that is compatible with `uv tool install`. calu needs no home directory of the
      unprivileged user; everything that user may configure is in the database.
    - What starts Claude Code under the other uid and gid is built into calu: no script is
      written to disk for it, which could be hijacked (stated 2026-11-13).
    - What is privileged (clarified 2026-11-13): the database files and the database's
      configuration, and calu's configuration and executables on the file system. Changing
      settings is not reserved to root: the full-screen client must still allow it, asking
      for the admin password, which it does not store. PostgreSQL's permission system is to
      be used for this; the database choice is fixed to PostgreSQL also for that reason.
    - A client attached to a session that was started through sudo may itself run through
      sudo (suggested 2026-11-13): it then has privileged abilities with ease of use, and
      Claude cannot reach it. It should then also list sessions and switch between them
      without a new authentication, as `calu serve` starts sessions without one.
    - A session also listens on a root-owned Unix socket, in addition to TCP (stated
      2026-11-13). For connections over that socket the token authentication is skipped. It
      must be verified that the socket is actually owned by root and limited to mode 0600
      or similar.
27. `calu serve` (stated 2026-11-13): another run mode that works like `calu run` but can
    spawn and track multiple calu/Claude sessions in various project directories on the same
    system. With it, forking a session or creating a new one needs no new authentication each
    time. It might also allow picking up or resuming sessions that were launched directly as
    `claude`.

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
6. If possible, the web interface bundles a remote git browser that can work through an SSH
   connection or tunnel.
7. Tentative ("we might want"): every database user may have a namespace of its own in the
   database, so that there can be database users who cannot see or modify each other's tables,
   and also database users who can see and modify everything. Similarly there might be global
   and user-specific configuration settings in the database later.
