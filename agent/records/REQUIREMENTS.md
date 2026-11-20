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

28. The honesty checker has a different role than the other checkers and does not answer
    pass, deny or escalate like them (stated 2026-11-13). It marks files and functions as
    honest or not:
    - Its answer is HONEST or DISHONEST, and applies to the file.
    - DISHONEST is expected also on steps where honesty is lost only for the moment: a later
      edit may make the code honest again, and the answer is then HONEST.
    - A DISHONEST answer says briefly why.
    - In a structured form it can name the functions, units or methods that gained or lost
      honesty.
    - It escalates what looks like an intentional backdoor or malicious behaviour of some
      kind.

    - The honesty checker is what judges honesty; it is a demanding task that cannot be done
      together with other tasks (stated 2026-11-18). The other checkers do not judge honesty.
29. A `claude:` checker that runs as the session's user runs in that user's environment and
    does not obtain environment variables from root; it needs the Anthropic key from
    somewhere, i.e. from that user's own login (stated 2026-11-18). `calu login` delegates to
    Claude Code's login method in that user's account, with and without `--sudo` (stated
    2026-11-18). Parallel attempts at the web login need no cap (stated 2026-11-18).
30. "calu run and calu serve should not print interactions (neither user input nor claude
    responses) to stdout/stderr, they are supposed to just print a status. Full exception
    tracebacks should go to a logfile, stderr should only show a 1-liner in case of errors."
    (stated 2026-11-18)
31. "When calu attach or calu web connect locally through a Unix socket, they should be
    trusted and not require input of a session token/password. Only require these for network
    connections, unless --secure is passed during run or serve startup." (stated 2026-11-18)
    On the case where Claude Code runs as the same OS user as calu, the owner chose: trust
    only other uids, so a same-user session keeps requiring a token on its socket too; and
    `--secure` requires a token on every socket, root's included (2026-11-18).
32. "Now let's also allow to pass --insecure, which allows to bypass token/password for all
    unix socket connections as well as localhost TCP connections." (stated 2026-11-18)
33. "It should be possible to set up the DB without password if this is wished." (stated
    2026-11-18) A client that found its session through a socket learns the session's
    database from a root-owned file, not from the session ("Build as proposed", 2026-11-18).
    "When I use --no-password, security is weak. This should be allowed." and "not having a
    password when the password is actually empty is ok." (stated 2026-11-18, after a client
    without `--sudo` asked for an admin password nobody knew)
    "check that we don't have code that refuses client-side just because it's given a blank
    password. Blank passwords can be ok, the client should not decide it's invalid." (stated
    2026-11-18)
34. "write a VSCode extension similar to the one of stay-in-the-loop, just for calu this time.
    It should support all relevant settings. The checker instructions should be readily
    accessible, as should it be possible to easily look at the checker context. Subagents
    need to be fully supported. Ease of use and modern UX principles both in the main UI and
    in the settings dialog must be honored. The extension must be able to install uv and
    calu. Do not install claude, but check for it's presence. It must have a simple way of
    setting all agents to the same agent, with easily selectable presets for Anthropic and
    Bedrock Haiku 5.5 and Sonnet 5.5 as well as the other default models we use." (stated
    2026-11-18) Also: "calu web must also support attaching without a password." (stated
    2026-11-18)
    "The VSCode extension must also allow switching sessions between those running on the
    machine easily, maybe with a dropdown. It should be possible to give names to sessions.
    The VSCode session must be able to run when connected to a remote machine, in this case
    everything runs remotely. If there's no calu serve running, it should give instructions
    how to launch it in a tmux or screen session persistently." (stated 2026-11-18)
    "aim for a modern UI with good UX. Go for dark mode first. Consider that calu needs to be
    not just as good as Claude Code's plugin, it must be able to do more. But to do more, it
    must first support Claude Code's basic chat UI, including inline images, undo, fork,
    multiple chat sessions, settings dialogue, dash-commands, model and thinking level choice,
    display of context usage (at least percent or absolute number of tokens), display and
    handle questions and escalations as well as notifications. Then on top it needs to support
    the calu specific features: Take a quick look at the checker context, specify checker
    instructions. Configure calu's various settings. Enable honesty checking etc. - all of
    this must be smooth and well structured, so the most commonly used features are easily
    accessible while others are still easy to find in a settings dialogue etc. -
    stay-in-the-loop is a good first draft, but UX-wise it's certainly not perfect yet."
    (stated 2026-11-18)
    "By default, the extensions should require only one password on startup, maximum. Do not
    repeatedly ask in passwords, they must also be convenient to use." (stated 2026-11-18)
    "The VSCode and Pycharm extensions may assume that they may work against a "calu serve"
    backend. Let's try avoiding to need sudo rights in the client." (stated 2026-11-18)
    "It IS ok if a single password is required on startup of the extension and on each IDE
    restart. But not more frequently like that. A session/db password may be retained
    in-memory in the client." (stated 2026-11-18)
    "It must be user friendly and intuitive. Ideally, clicking on a line in the chat that
    indicates an edit happened should get us into a diff viewer showing the diff of the edit
    that was made. Clicking just on the filename might open the file and jump to the edited
    section. Niceties like these make it fluid to use." (stated 2026-11-18)
    "We can also use a nice logo, try to go for something resembling the tron disc and tron
    logo (without running into copyright conflicts, but the checkers HAVE the role of Tron in
    the original Tron movie)" (stated 2026-11-18)
    "I would prefer to have the chat bar sit on the right side of the screen by default, by
    the way. Currently it's on the left." (stated 2026-11-18)
    "Creating a new claude session should not ask for "A first message to claude", everyone
    should know how to find the chat input box." (stated 2026-11-18)
35. "after the VSCode extension, I want to have an even more powerful pycharm extension. More
    powerful because it should support both local and remote mode and an operation where the
    remote server, the db and calu are being accessed over SSH only and changes are
    synchronized back and forth (via rsync, scp, sftp or pycharm internal methods)." (stated
    2026-11-18)
    "Once the VSCode session is ready for manual testing and nothing can be done beyond that
    for it, continue with the Pycharm plugin for real. It should be similar to the VSCode one.
    Importantly, it needs to be working with the new Pycharm remote IDE mode and the older
    local IDE with SSH sync and remote execution via SSH mode." (stated 2026-11-18; he also
    said "Both" to starting the PyCharm plugin and the session traces now, in parallel)
    "We only need to support the latest stable PyCharm version and maybe additionally the
    latest EAP if possible. Test it in Pycharm as well." (stated 2026-11-18)
    On testing the SSH mode against a server: "if you can test a server that would be great.
    In general, the server needs no Desktop and runs Linux with SSH listening, with
    passwordless public key auth for simplicity." (stated 2026-11-18)
36. "calu should have a setting that automatically logs the narrated or the plain session log
    as markdown (just without any folding summarization happening, only steps being appended)
    logged to a file inside the project so they can be version controlled along with the
    rest. The idea is that chat sessions are a form of source code, and this is how this can
    happen. Chats should be written to a structured dir tree, for example something like
    .agent/traces/<date>/<user>/session_[NNN].md or such. These files are intended for code
    review and therefore should be run through an additional redaction agent which (by
    default) censors potential secrets and personally identifiable information (PII) in the
    chat logs. Also, images are not included of course." (stated 2026-11-18, "for the plan")
    Later: "Plan that the system of records ( e.g. all the documents intended only for
    consumption and maintenance by and for the coding agent ) except AGENTS.md will be moved
    to a separate directory ( for example .agent/records or such ). Do this only when no
    subagents are running and ensure file references are updated everywhere ( especially in
    AGENTS.md )." and, choosing the place: "or better agent/records, similar, let's place the
    agent traces into agent/traces by default. No need to hide that, it just needs to be a
    single dir." (stated 2026-11-19)
37. "over the course of this development session, test it all in this: The pycharm extension,
    the VSCode extension, the TUI, the web app, calu serve with/without password. Develop test
    plans and go through them, ensuring everything works as intended and has a good
    usability, exploring even visual/layout corner cases like many subagents, long chat
    sessions, many chat sessions, undo, forking etc. and maybe also what happens if an
    escalation in a subagent halts one but not other subagents, and another escalation pops
    up. Try to arrive at a fully functional, tested and well useable application that is well
    rounded. Push whenever you have reached a milestone." (stated 2026-11-18) For tests with a
    real Claude Code: his Anthropic API key with a tightly limited budget, Haiku only; he
    revokes the key after testing (stated 2026-11-18).
38. "Also go through the workflow for honesty. The checker instructions are way too verbose at
    the moment, they should be *much* shorter, we will mostly rely on Claude's auto mode
    classifier for everything that's already described in there. Instead, make them as simple
    as possible and put in test instructions that make it easy to trigger escalations, pass,
    deny and approve for everything. The honesty checker and related agents should get real
    instructions, though. These instructions themselves need to be tested (with Haiku or
    Sonnet as agents doing the checking). The narrator instructions seem to work well, leave
    them as-is." (stated 2026-11-18)
39. "Create screenshots of the test runs from within cua when everything is close to finished
    and add a few to a docs/assets folder and embed them into the readme. Also improve the
    readme a bit, I think some sections can be shortened while the new features can be
    described more prominently. The ALLOW/ESCALATE/DENY response is a detail that doesn't need
    to take the center stage in the readme." (stated 2026-11-19)
    "Lead the readme with a quote from the movie Tron. Where Flynn asks Tron what his function
    is." Then: "The 'I fight for the users' part isn't bad either", and the two passages as he
    recalls them from the 1982 film: Flynn: "Who's that guy?" / Program: "That's Tron. He
    fights for the Users."; and Alan Bradley to Dillinger: "It's called Tron. It's a security
    program itself, actually. It monitors all contacts between our system and other systems.
    Finds anything going on that's not scheduled, it shuts it down. ... It'll run
    independently and watchdog the MCP as well." (stated 2026-11-19; the wording is his and
    was not checked against a transcript)
    He agreed to open with the Flynn / Program exchange and to place Alan Bradley's description
    where the README explains the checkers ("agreed, do it like that"), and: "Finish the
    readme with the quote 'Dr. Walter Gibbs: Computers and programs will start thinking, and
    the people will stop.'" (stated 2026-11-19; source he named: IMDb's quotes page of the
    film, which could not be fetched from here)
    On the README's tone: "I dislike the "What you get" heading, it sounds as if I'm selling
    something. Make it sound human, from nerds, for nerds. Make it clear this is an attempt
    to stay in control of coding without having to babysit each step." (stated 2026-11-19)
40. "put an easter egg into calu (use a subagent): When you run "calu lightcycles" it should
    launch a multiplayer game modeled after the old C64 game "Ultimate Tron II"". After the
    check of that game's public reverse-engineered source showed no licence: "Alternatively,
    clone this game which I personally hold the authorship rights of (I wrote it):
    https://sourceforge.net/projects/metatron/files/OldFiles/MetaTron1-2-src.zip/download",
    and: "It does not matter which license it states: I wrote it and hold the copyright. My
    name is Kai Londenberg ... The user who uploaded that archive to sourceforge is called
    "klondenberg", that's me." (stated 2026-11-19)
41. "Remember that you can test drive everything in cua and that I gave the task to try and
    refine all the workflows and user interfaces. ... please ensure we have a well rounded,
    tested, robust and convenient application with good usability and modern UI in the end.
    All of the TUI interface, the web interface, the VSCode plugin and the Pycharm plugin need
    to have been tested, ideally both for local and remote operation." Then, on the PyCharm
    plugin: "Let's focus on three operation modes in Pycharm: 1.) Entirely local (should work
    already). Pycharm is local, execution is local, calu server and claude run locally. 2.)
    Pycharm remote development. In this scenario, Pycharm connects to a headless IDE instance
    over SSH (similar to VSCode remote development). The local IDE is just the GUI, the code
    analysis, execution, calu, claude, terminal sessions etc. and source code all lie
    remotely. 3.) Legacy PyCharm remote development. In this mode, we have a local IDE and a
    local project directory and git repo, but everything is synchronized to a remote server
    via SCP/SFTP/Rsync. Execution and debugging might happen through SSH (or not), depending
    on Python Interpreter settings ( SSH Remote interpreter or not). In this case it's likely
    best to have calu and claude run locally and just tell them that they need to (rsync?)
    sync to remote and prefix shell commands with something like "ssh user@remote bash -c
    '<original command>'" or similar. Letting calu run remotely in this scenario would have to
    involve two-way sync, which can lead to races and data corruption." (stated 2026-11-19)
    After the existing SSH mode (PLAN D236: a local IDE on a local copy, calu and Claude on
    the server, two-way sync through `calu ide`, `.git` on the server only) was described to
    him: "No, let's keep that, it's good. Instead of my variant 3. We just need to ensure edit
    conflicts gets resolved and one side only holds .git." (stated 2026-11-19)
42. On the README's requirements: "The "getting started" in the readme mentions bubblewrap and
    socat for the sandbox. This is a claude code requirement for a Claude Code managed
    sandbox. Calu does not modify or interfere with this sandboxing. Don't consider it a
    requirement of calu. But you do need to highlight that calu requires a postgresql
    instance running somewhere, either centrally or dockerized on the machine where calu
    serve/calu run is running. This needs to be made plain since it has implications for
    sysadmins/devops and managing security ( the DB might contain sensitive information in
    form of chat logs and might need protection, upgrades etc.). Ensure this is clearly (but
    shortly) mentioned in the Readme, with a link to the corresponding documentation section
    for details." When told that calu does configure the sandbox and refuses to start without
    bubblewrap: "Calu should ask whether it should enable it during setup. If it's off, it
    should warn that in this case it's strongly recommended to at least password protect
    session and db and/or use --sudo so calu itself runs as a more privileged process than
    claude." (stated 2026-11-19)

43. On where a remote client may start sessions through `calu serve`'s relay port: they
    "must be subdirectories of a projects directory served by calu serve ( current dir by
    default ). Err out on start of calu serve if it's a hidden dir or subdir of a hidden
    directory or within a critical system dir on Linux or MacOS ( /etc, /proc /dev /sbin
    /var/lib /usr/lib /usr/bin /var/log etc... )" (stated 2026-11-19)
44. "Ensure that the redaction agent that redacts chat logs has a configurable prompt. I just
    learned that a lot of people consider their LLM chat logs to be a private space and might
    not want to expose how many questions they had to ask or when / how long they worked. The
    redaction should ensure that requirements that entered the chat ( or were proposed by the
    LLM and accepted by the user ) are accurately reflected, while possibly redacting aspects
    that users find personal." (stated 2026-11-19)
45. "The final trace redaction does not happen per step, I would say, it happens before the
    file is committed. Can we handle it that way? Otherwise, summaries will have content
    summarized and folded into summaries that are folded into summaries." Asked whether to keep
    the per-block redaction of the live file and add a final pass at commit: "Both, but try to
    summarize only what was added since last commit. The final diff to be committed needs
    redaction. We don't want the previous committed summary folded into the next." On a
    removed message's turn heading: "Whatever the custom prompt says. By default it should all
    be kept, unless the prompt says so. It's not important." (stated 2026-11-19)
46. "DOCUMENTATION.md is not written for serial reading. Despite the fact that readers will
    typically have read the readme already, it needs to re-describe everything from scratch.
    It needs to define terms and provide context. Use examples. Avoid long prose sections
    without any structure. Use plain technical language. Remember that there are two ways to
    read the documentation: From start to bottom ( or starting at some section header down ),
    or as reference documentation. It should work both ways. Don't just treat it as a randomly
    ordered collection of exhaustive natural language description of algorithms. Describe
    things at a high level. Some things are best described with the help of diagrams,
    examples or pseudocode. Give examples, especially for usage scenarios, including example
    prompts. - Task a fable subagent with rewriting it. Try to use best practices for readable
    and useful technical documentation." (stated 2026-11-19)
47. "Can we add an optional (disabled by default) feature to the IDE plugins to track diffs
    introduced by manual editing? The assumption would be that if we track these, we can
    later tell the model "See, these are the stylistic language changes I want you to make
    all over the codebase" or "Please rewrite documentation like this" etc.. - it makes sense
    to be able to track which edits were human and look at them separately. Let's put them
    into agent/human-diffs by default if we do ( they should be typically gitignored, should
    be mentioned in the docs )" Then: "No secret redactions, these diffs are left alone.
    Also, these edits should only be tracked for files under version control and only if the
    editing happens in the IDE." (stated 2026-11-20)
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
