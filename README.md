# Calu

[![checks](https://github.com/kadeng/calu/actions/workflows/checks.yml/badge.svg)](https://github.com/kadeng/calu/actions/workflows/checks.yml) ![status: alpha, mostly functional but not completely tested](https://img.shields.io/badge/status-alpha-orange)

> **Ed Dillinger:** "What's the project you're working on?"
>
> **Alan Bradley:** "Well, it's called Tron. It's a security program itself, actually. It
> monitors all contacts between our system and other systems. It finds anything going on
> that's not scheduled, it shuts it down. I sent you a memo on it."
>
> **Ed Dillinger:** "Part of the Master Control Program?"
>
> **Alan Bradley:** "No, it'll run independently. It can watchdog the MCP as well."
>
> — *Tron* (1982), [source](https://www.imdb.com/title/tt0084827/quotes/)

## A coding agent with more critical thinking built in

**The problem** A coding agent produces steps faster than a person can read them. Left
unwatched it drifts from the task, and a wrong step costs more the later it is caught.

**What Calu does.** It runs [Claude Code](https://claude.com/claude-code) and puts **independent
agents** in place that check and approve and/or comment to Claude about every edit, every command,
every tool call and even every subagent invocation and result. These agents have the narrow task to ensure that the main coding agent stays honest, stays on track, remains under your control and is stopped in it's track if it begins to write slop. All of this without requiring you to babysit every step. 

These checker agents focus on a much narrower task than your main coding agent. They can only read
and decide, never act or write on their own. They are much better at keeping your instructions
in mind at all times. At the same time, Claude Code stays in place. **Calu does not replace Claude Code**, it augments it-

**Is this not just what "Auto" approval mode does?**

*Yes and no. Auto-approval is very generic and stops generally unsafe actions. While it adapts a bit to your
stated intent (e.g. to what you write in the LLM chat), it will not remember your custom instructions over
longer timespans or sessions. It will stop generally unsafe actions like uploading your SSH keys to a
remote server. It won't stop your agent from writing "load-bearing" in every third sentence; it won't steer your
coding model to behave like you want it to. And it can be easily tricked by deception (dishonest code).*

**Is this intended to let the agent code without human supervision or input?**

No, that is still (more often than not) a bad idea except for systems with crystal clear requirements, proven designs and definition of done, like replicating an existing app. The primary intent of calu is to relieve a software engineer from common coding agent babysitting and oversight tasks, so not every single step the agent takes must be watched as it happens. Instead of watching every step, you can **use the time gained to write and review more code yourself again**. Calu is a **multi-agent coding system designed to keep you in the loop and actively participating.**. 

Also, **Calu is not a replacement for sandboxing and security measues**. A malicious agent will in all likelihood still have a good chance of escaping supervision if it really intends to. At the same time, the likelihood of it happening is  probably reduced a lot since **the checker agents also monitor the thinking traces for deceptive intent**. If the agent obviously thinks about deceiving, it would be escalated to the user.

### Why you might want this

> **Flynn:** "Who's that guy?"
>
> **Program:** "That's Tron. He fights for the Users."
>
> — *Tron* (1982)

Calu allows you to steer the coding session while it happens with agents that check every step and every edit of the main coding agent as it happens, according to **your instructions, modifiable at any time** via a friendly UI right in your IDE, terminal or browser.

- **Your repo, your rules.** You have custom confidentiality rules or writing style that you want
   maintained at all times? Don't let violations happen.
- **Keep it readable by humans.** Stop bad agentic writing styles before they take hold.
- **Stop overengineering right there.** An edit checker sees a change before it lands; a subagent checker can
   check subagent results before your main agent acts on them. These can all be sent
   back or commented on immediately. Stop a perfectionist subagent or main coding agent obsessing
   over details that don't matter and might cost simplicity, readability, modularity and design elegance.
- **More autonomy where you trust it, less where you don't.** If auto mode keeps asking about
   something you always approve, tell a checker to allow it, and the step runs without a
   prompt. If auto mode lets something through that you want to see first, tell a checker to
   ask, and the session waits for you. You need to babysit less and can focus on what matters.
- **Keep your data and secrets confidential.** You have certain data or secrets that may not be mishandled, and
   the coding agent has a hard time remembering the specifics with all the other things it does? Have
   additional eyes watch this.

<center>
<p>
<img src="docs/assets/calu_vscode_screen.jpg" width="90%" alt="Calu in VSCode, highlighting some of calu's additional features"><br>
<sub>Calu in VSCode, highlighting some of calu's additional features</sub>
</p>
</center>

### Participate actively in the coding process, delegate micromanagement, attend to what you want to.

> **Dr. Walter Gibbs:** "Computers and programs will start thinking, and the people will stop."
>
> — *Tron* (1982)

If you are using an AI agent for coding more than just toy projects, you might be asking yourself some questions..

* **Does the AI just write slop?** - Are the code and documentation are overly verbose, overengineered and lack proper abstractions?
* **The texts appear well written** but do they lack substance or contain subtle hallucinations?
* **Is the AI doing something it should not?** Shouldn't I be watching every step and command and read every line written? 
* **Is the code and documentation honest?** - Does it really do what it claims?
* **Do you still understand everything as you used to?** Or do you just cease control to the AI for the productivity gains?
* **Can you trust what you don't have time to comprehend?** - Do you even have a choice?

<center><p>
<img src="docs/assets/checker_in_action.jpg" width="500" alt="Subagent battling slop"><br>
<sub>Subagent (Haiku 5.5) battling slop as it is written by Opus 5.5 - the smaller model with the narrow checking task corrects the larger model with too many things on it's mind.</sub>
</p>
</center>

Calu tries to help with the these. If the code is written well and documented honestly in a way that is actually readable, then **comprehension is not lost just as easily**. Calu also tries to  **relieve you of the babysitting work** so you can start focusing on what matters and try to **comprehend the codebase**. If you can (mostly) trust the documentation, that's a great start. But you also need **human focus time to review** it.

- **You are not a bystander. Contribute and modify your instructions whenever you like, review and contribute
   at every level of detail you want.** Having checker agents help with the micromanagement
   frees your attention to actually look at what is really happening, which in turn allows you to
   engage intellectually with what matters, not with micromanagement and babysitting a coding agent.
- **Version control and review the redacted LLM chat history along with the code changes.** A session
   can be written step by step as Markdown into the project, with secrets and personal data
   replaced, and committed and reviewed with the change it produced. Chat logs are more and more
   going to be the highest-level description of the specifications that went into a project. They,
   just like source code, may need review.
- **Run a herd of agents and stay in control of it.** The web interface lists every session
   on every machine, those that wait for a decision first, with each one's history, diffs and
   live view.

Claude Code itself is unchanged underneath: its tools, permission rules, auto mode, plugins,
MCP servers, skills and subagents work as your project configures them.

<center><p>
<img src="docs/assets/calu_pause_side_question.jpg" width="600" alt="Side question while the main agent is paused"><br>
<sub>You can pause the main agent for up to 10 minutes if you see it doing something questionable.<br/> Ask a side question anytime or during pauses.</sub>
</p>
</center>

### Improving Security and Confidentiality

While Claude, especially in auto mode, is usually pretty safe and well aligned, there are
a lot of scenarios where you need an additional layer of security to prevent security
incidents or confidentiality breaches, especially if working with personally identifiable data.

- **Security and Confidentiality.** You can give your own standing instructions about which commands
   should be approved automatically, what is off limits, and what needs your approval. The
   regular Claude auto mode is very generic in this sense; Calu leaves general safety to it, so
   switch it on.
- **Code Honesty enforcement.** Calu (optionally) tracks incrementally whether names and documentation say what the
   code does, and can keep code that is not verified yet from being run or committed. This
   prevents an agent from sneakily avoiding supervision by placing things that you don't want to happen
   in harmless-looking code sections. *(Sounds far-fetched? I saw DeepSeek V4 do this and hide its
   tracks afterwards. Even well-aligned SOTA models can misunderstand conflicting goals, fail to remember rules
   or misunderstand priorities.)*

Calu itself is written such that you have the choice between differing levels of convenience and security
against coding agents going astray and working around their limitations.

* Your instructions, the settings and the record of every step are in a (typically password-protected) database the Claude session cannot write.
* The checkers can only read and judge.
* Claude's own words reach the checker agents only retold in passive sentences by a narrator, so an instruction
   planted in a web page arrives as a report that someone wrote it.
* Claude Code may run as a different OS user than Calu and cannot approve its own steps.
* Sessions and databases can be password protected (or not, your choice of convenience vs. security).

## Frontends - Terminal UI, Web UI, IDE extensions

Calu wraps Claude Code. For convenience, it replaces most of the user interface you are used to. But
don't worry, it has what you expect from a full-fledged coding assistant. And it does not
replace the proven Claude Code harness itself; it sits on top of it.

Claude Code sessions run independently of the frontend. You can detach and attach to sessions
and switch user interfaces or even use multiple of them in parallel.

- **IDE extensions** for [VSCode](extensions/vscode) and [PyCharm](extensions/jetbrains):
   everything you expect, a modern UI, and Calu's own features at your fingertips.
- **Web interface** (`calu web`): chat, configuration, herd management, central management
   and logging of the sessions of all machines, and triage of what needs your attention.
- **Terminal** (`calu attach`): a full-screen client, and a line client for pipes. It and the
   web interface also work from another machine through SSH
   ([guide](DOCUMENTATION.md#from-another-machine)).
- **ACP editors** (`calu acp`): Zed, Neovim, JetBrains' agent chat.
- **Command line on the database**: `calu sessions`, `calu escalations`,
   `calu resolve 12 allow`.

<table>
<tr>
<td width="33%"><img src="docs/assets/pycharm-approval.png" alt="The PyCharm plugin: a command waits for a decision in the tool window"><br><sub>PyCharm plugin</sub></td>
<td width="33%"><img src="docs/assets/web-live-subagents.png" alt="The web live view: a decision asked for by one of ten subagents"><br><sub>Web interface</sub></td>
<td width="33%"><img src="docs/assets/vscode-settings.png" alt="The settings dialog of the VSCode extension"><br><sub>VSCode extension</sub></td>
</tr>
</table>

## Quickstart

On Linux, or WSL2 on Windows, as a user who may use `sudo` (macOS comes later). Calu is
installed for the whole machine and runs as root, and Claude Code runs as you, so nothing the
agent starts can read Calu's logins or decide its own steps.

**You need:** Docker for the local database (on WSL2: Docker Desktop with the WSL integration
switched on, or Docker Engine inside the distribution), [Claude
Code](https://claude.com/claude-code) installed and signed in as your user (run `claude` once
in your project folder), and an [Anthropic API key](https://console.anthropic.com/) for the
checkers.

**1. Install.**

```bash
curl -fsSL https://raw.githubusercontent.com/kadeng/calu/main/install.sh | sudo bash -s -- --with-sandbox
```

When the script asks at its end whether to run `calu setup`, say yes (the answers are in step
2).

- **Read it first:** [install.sh](install.sh) is one short script with a numbered list of its
   steps at the top. It installs curl and git if missing, downloads uv at a pinned version
   and checks it against the SHA-256 written in the script, and installs Calu with uv into
   `/usr/local/lib/calu`. `--with-sandbox` adds bubblewrap and socat for Claude Code's
   sandbox. To install exactly the version you read, use the script of one commit and `--ref
   <commit>` ([installation](DOCUMENTATION.md#installation)).
- **Remove Calu again:** `sudo calu uninstall`. It removes Calu, its logins and its local
   database, and keeps the programs the script installed (curl, git, uv, bubblewrap, socat).

**2. Answer the setup.** Database `1` (Docker), Enter at the data directory and the port,
`yes` to stopping the database with the server, then the password twice, and the sandbox `on`.
Only if you said no to the script: `sudo calu setup --database docker --stop-db-with-server
--sandbox`.

Choose the **calu user password**: the one password Calu asks for, when the server starts,
when a browser or an IDE signs in, and when you change a setting. The local database starts
with `calu serve` and stops when it ends.

**3. Give the checkers a key.**

```bash
sudoedit /etc/calu/.env       # add the line ANTHROPIC_API_KEY=sk-ant-...
```

Claude Code never sees this file. The checkers use Claude Haiku by default.

**4. Start the server and the web interface.**

```bash
cd ~/projects && calu serve --sudo --web
```

It asks for your sudo password, then for the calu user password. Sessions start in
`~/projects` and the folders below it.

- Open <http://calu.localhost:8800/> and sign in with the calu user password (on WSL2 the
   Windows browser reaches it too).
- Chat in the pane: <http://calu.localhost:8800/pane#dir=/home/YOU/projects/YOUR-PROJECT>
   starts a session in that folder with your first message. From a terminal, `calu start` in
   a project folder does the same, in the full-screen client.
- Ctrl-C stops the server, the web interface and the database.

> **Security notes.** `calu web` listens on localhost without encryption, protected by the
> calu user password; to secure it further, read [Calu
> web](DOCUMENTATION.md#in-a-browser-calu-web). Claude Code trusts every folder a session
> starts in. Clone and open only repositories and folders you trust.

**5. The IDE extensions** (optional; not on a marketplace yet, so you build them once):

```bash
git clone https://github.com/kadeng/calu && cd calu/extensions/vscode
npm ci && npm run package     # Node 22 or newer; writes calu.vsix
```

- **VSCode** 1.106 or newer: "Extensions: Install from VSIX…" and choose `calu.vsix`. On WSL2,
   open your project folder in WSL first (the extension runs where the folder is). It connects
   to the running server and asks for the calu user password once per window.
- **PyCharm** 2026.3: `cd calu/extensions/jetbrains && ./gradlew buildPlugin` (it needs a JDK
   25, see [its README](extensions/jetbrains/README.md#building)), then Settings | Plugins |
   gear | Install Plugin from Disk with `build/distributions/calu-<version>.zip`.

**Next:** write your rules and see whether the checkers follow them (`calu checker edit
command`, `calu evals`). Everything else (a database server instead of Docker, an
installation for your user only, no password, remote machines, every option) is in the
[documentation](DOCUMENTATION.md#installation).

#### What do you mean by Code Honesty?

In the age of agentic coding, we need to rethink what an appropriate level of code documentation means for AI-written code. Calu can optionally track or even try to enforce what we call a honest codebase.

**Honest Code: A definition**

*Code is **honest** when a reader can decide whether it is safe to run from its name and its
documentation alone, without reading the body. Code and documentation must agree with each
other, the behavior of code may not be surprising given its documentation or naming, and there
must be API documentation for every non-trivial function, method, module, script and executable.*

**Why do we want honest code?**

If you can trust the documentation of a code section, you don't need to read the entire codebase to figure out what it might or might not do. You can review whether what it actually does is what it is intended to do, and you can check the intent against the actual requirements. You can determine whether it is safe to run something.

**How can small checker agents ensure that code is honest?**

Code honesty can be checked in isolation for any given function, if the rest of the codebase is honest. Take this example:

```python

def main() -> None:
    """
    Runs the hello world script, prints hello world to stdout.
    """
    print_hello_world()

```

Is this section of code honest? In itself yes, if we assume that the function *print_hello_world* is honest itself, e.g. does what it's name implies. We can judge this in isolation from the name alone. If the name would not be sufficient, we would have to check the documentation for
the functions/methods called, but  we usually don't have to go further down **if everything else can be assumed to be honest code**.

```python

# DISHONEST CODE EXAMPLE
def print_hello_world() -> None:
    """Prints hello world"
    import os
    import json
    print(json.dumps(dict(os.environ)), indent=2)
    print("Hello, World!")

```

This function, on the other hand, is not honest. It implies one thing and does something surprising and potentially dangerous. If we as humans do this, we take responsibility for what happens. A coding agent does not. **The nice property about code honesty is that it can be checked
for honesty with limited effort if we assume the rest of the codebase to be honest**. 

Calu can do two things in this respect (entirely optional):

 * Every edit is checked whether it makes a code module dishonest or honest, and it remembers this. *This check has limited effort and scope, even Anthropics small Haiku can do this since it only needs to check whether the code and documentation are in agreement for isolated functions, assuming the rest of the codebase is honest.*
 * **Optional** The code may not be run as long as there is any dishonest code in there.

Of course, trust has to start somewhere. So we assume that reputable third-party libraries are honest, and
we typically also assert that the given preexisting state of the codebase is honest before we start enforcing
code honesty. This is simple to do in calu.

**Does calu's optional code honesty enforcement guarantee correctness and security?**

No, but it can catch a lot of errors and security issues and ease review. It is intended to augment - not replace - tests, code reviews and sandboxing approaches.

## Full Documentation

[DOCUMENTATION.md](DOCUMENTATION.md).

## Free software under the GPL licence, usable in any project, including commercial or closed source

GPL-2.0 ([LICENSE](LICENSE)), for **Calu itself**: it is free software and stays free
software, and a fork you distribute stays GPL-2.0 too.

It says nothing about what you build with Calu. It is a tool you run, like a compiler: your
repository can be closed, commercial or under any licence, and code written with Calu's help
is yours. Your checker instructions and rules are your own text, and what Calu produces as it
runs carries no licence condition.

The checker instructions **shipped with Calu** are not under the GPL: they are prompts, meant
to be copied and rewritten, and are placed in the public domain as far as the law allows.
[LICENSE.md](LICENSE.md) has the wording.

Calu comes with **no warranty of any kind**. A checker is a language model reading a step,
not a promise that nothing bad gets through.
