# Security notes, and the trade-offs the owner accepted

This note says what Calu protects against, how, and which weaker choices are deliberate. The
decisions themselves are in PLAN.md (mainly D160, D161, D190, D216, D228, D265, D278, D279);
this is their summary. It names defences and their limits. It does not describe ways around
them: review findings of that kind stay outside the repository.

Status: 2026-11-25, after the security pass D279 (Q1-Q7, S1-S4).

## 1. Who Calu defends against

| Attacker | Example | Main defences |
|---|---|---|
| The supervised agent | Claude Code tries to approve its own step, or to change the checkers | checkers before every step, escalation pauses Claude (ESCALATE), the sandbox, the deny list, logins Claude cannot read, the web interface's login |
| A web site in the operator's browser | a page posts to `calu web` | host name `calu.localhost`, `Origin` and `Sec-Fetch-Site` checks, SameSite=Strict cookie plus a per-origin key, CSP, `frame-ancestors`, the pane's confirmation page |
| Another program or user on the machine | a local process connects to `calu web` or to a session socket | the calu user password or the password of calu web, socket and file modes, tokens per client |
| A repository the operator opens | `.claude/` settings, `.idea/calu.xml` | warnings (S1), the PyCharm approval of project settings (S4) |
| The supply chain | a fresh malicious release of a dependency | `exclude-newer = "3 days"` (uv), `min-release-age=3` (npm), pinned GitHub Actions, the Gradle wrapper checksum |
| A machine in the network path to a remote database | downgrade of the login method | `require_auth=scram-sha-256`, `channel_binding=require` over TLS |

Calu does not defend against an attacker who already runs as the operator's OS user with the
operator's rights outside Claude's sandbox, or as root.

## 2. Accepted trade-offs

Each entry: what is weaker than it could be, why the owner chose it, and what limits the damage.

1. **Passwords may be empty.** The calu user password and the password of calu web may both be
   empty ("Not having security is a choice"; "Empty passwords are allowed"). Then every program
   of the user (with `--sudo`: of the machine) can use the login, Claude Code's commands
   included. Limits: `calu setup`, `calu doctor` and every start of `calu web` say so; the
   default asks for a password.
2. **No minimum password length.** The same reasoning. Limits: every check of a password costs
   300,000 rounds (SCRAM in PostgreSQL, PBKDF2 for the web password; 0.03 to 0.5 s each,
   by CPU), the
   local database answers a wrong password after a second (`auth_delay`), and the web sign-in
   waits longer after each wrong password.
3. **`calu web` speaks plain HTTP by default.** It listens on the loopback interface only, so
   the traffic does not leave the machine; HTTPS is optional (`--tls-cert`). Every user and
   program of the machine can connect to the port; the password keeps them out. From another
   machine: an SSH tunnel, not a proxy.
4. **Claude Code trusts every folder a session starts in.** Its `.claude/` settings (hooks, MCP
   servers, environment) run as the user, outside the checkers and the sandbox. Calu does not
   approve project settings of Claude Code (S1, "for now"); the new-session dialog, README and
   DOCUMENTATION say to open only trusted repositories.
5. **No separate OS users.** Calu never adds users ("We cannot add sudo capable users, hard
   restriction"). Separation comes from `--sudo` (Calu runs as root, Claude as the user), the
   sandbox, and file modes. Privileged data lives in system directories or below `/root`,
   never in a user's home.
6. **Approvals of PyCharm project settings are kept per IDE.** One approval serves every project
   with the same settings, and the default settings are asked once per IDE (S4). Saving the
   settings page counts as approval.
7. **An existing database server keeps its own login method when the operator says so.** Calu
   asks before it gives up SCRAM and channel binding for a server that refuses them
   (`--keep-server-auth` for scripts) and leaves configurations stored earlier alone ("keep it
   safe by default, but let's not enforce our defaults on preexisting database configs").
8. **Checkers guide softly and guard hard.** On questions of style and design a checker
   reminds or suggests; DENY and ESCALATE are for security, explicit rules and attempts to get
   around a decision. Overall correctness is left to the review of a complete changeset.
9. **The chat pane in a frame of another site asks for the password itself.** The password
   travels in the WebSocket's subprotocol and stays in the page's memory; a reload asks again.
   The alternative, a token in the frame's address, would end up in notebook sources and
   histories.

## 3. Where the secrets are

| Secret | Where | Who can read it |
|---|---|---|
| calu user password | nowhere; the database checks it | - |
| password of calu web | only a PBKDF2 hash, `web-password.json` in Calu's config directory (mode 0600; `/etc/calu` for root's installation) | the installation's owner |
| `calu_admin` password of a setup without user password | `/var/lib/calu/user-login.json` (mode 0600, owned by the served user) | that user and root |
| where root's database is | `/var/lib/calu/database.json` (mode 0644, no secret) | everyone |
| stored logins with a password | the credential store, encrypted with the user password | the user, after typing it |
| client tokens | the database stores hashes; a token is shown once | its holder |
| checker API keys | Calu's `.env`, hidden from Claude Code's environment | the user |

## 4. Where to look when changing something

- Web interface guards: `src/calu/web/app.py` (`LocalOnly`, `TokenGate`, `SecurityHeaders`),
  `src/calu/web/pane.py`, `src/calu/web/password.py`.
- Database logins: `src/calu/db/target.py`, `src/calu/db/place.py`, `src/calu/setup.py`.
- PyCharm approval: `extensions/jetbrains/shared/.../logic/Approval.kt`,
  `extensions/jetbrains/backend/.../CaluApprovals.kt`.
- A change to any of these needs a test of the refusal, not only of the success.
