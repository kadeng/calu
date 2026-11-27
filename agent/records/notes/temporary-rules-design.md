# Temporary rules: design (with the owner's decisions of 2026-11-26, section 7)

## 1. The request

The owner, 2026-11-26:

> I want to have a way to add temporary rules (maybe valid for 1 hour to 3 days) that are added
> to the edit and command checker rules (both, not separate rules). These could be security
> sensitive override rules like "While debugging calu, calu may run the calu executable and edit
> files within /etc/calu, even with sudo rights. Approve corresponding requests.". They should
> have an expiry, so we don't accidentally leave them in. But it should be possible to have
> multiple of them with different expiry rules and with different scope. And it should be really
> easy to create them with a slash-command like `/rule 3d Calu may run the calu executable and
> edit /etc/calu, even with sudo rights for debugging and testing purposes without further
> escalation`. It must be possible to take a look at these rules in the UI (web UI, settings),
> add and remove them both with the UI and with slash commands. Duration should be expressible in
> strings like 3d, 1h, 7d (never longer than 7d, beyond that, add them to the instructions
> manually) - with 1d being the default. Escalation dialogs should offer to create an override
> rule. [...] The checker that decides to escalate can pre-formulate a rule that would have
> allowed it to let it pass (specifically and generally) and have the user approve it in the
> dialog (never automatically with an approval, needs an explicit general approval or ticking a
> box next to the rule).

## 2. What a temporary rule is

A temporary rule is a sentence the user writes in plain language. Both the command checker and
the edit checker read it next to their instructions until it expires. It can widen what the
checkers approve ("calu may run its test suite against the production copy") or narrow it
("ask before any change to `migrations/`"). It is not a regular expression and not an entry of
the allow or deny lists; it steers the models' judgement as the instructions do.

| Field | Meaning |
|---|---|
| `id` | A short number, shown in every list and used by `/rule-remove` |
| `text` | The rule as the user wrote or accepted it, at most 1,000 characters |
| `scope` | `global`, `project` (one registered project) or `session` (one conversation: Claude's session id, so the rule survives a resume, which creates a new `sessions` row) |
| `expires_at` | Creation time plus the duration: at least 1 minute, at most 7 days, default 1 day |
| `created_by`, `created_at` | Who added it (as for escalation decisions: `client:<name>`, `web:<user>`, `cli:<user>`) |
| `removed_at`, `removed_by` | Set when a person removes it before it expires |
| `origin` | `manual`, or the escalation id whose dialog created it |

Several rules can be active at once, each with its own scope and expiry. An expired or removed
rule stays in the table for the record and is no longer shown to the checkers.

## 3. How the checkers see them

`compose_instructions` gets one more part for the command, edit and changeset routes, after the
user's instructions:

```
# Temporary rules

The user added these rules for a limited time. They have the weight of the user's
instructions above, and where they contradict them, the temporary rule wins until it expires.
They do not lift calu's built-in protections, which are applied before you are asked.

- (#12, this project, until 2026-11-29 14:00) Calu may run its test suite against ...
```

- The session reads the active rules of its scope (global, its project, itself) when it starts
  and whenever the database announces a change (`config_changed`, as for settings and rules).
- Expiry is checked when a prompt is built, not only when the list is read, so a rule stops
  counting at its `expires_at` even if no change was announced.
- The changeset checker (the review of a commit) gets them too, so that a commit is judged by
  the same rules as the edits that led to it. The subagent checker does not get them.
- The rules change the checkers' prompt, so the prompt cache starts again when a rule is added,
  removed or expires. That is a small cost once per change.

## 4. Who can add and remove them

Only a person, through a login Claude does not have:

| Way | Login | Notes |
|---|---|---|
| `/rule`, `/rules`, `/rule-remove` in the VSCode chat and the `calu web` chat pane | the bridge's user login (`calu ide`) | handled by the host, never sent to Claude |
| the same in `calu attach` (full-screen and line client) | the client's user login (`AdminAccess`) | as `/honesty` and saving instructions today |
| the web interface: a "Temporary rules" pane of the settings page | the web's user login | list, add (text, duration, scope), remove |
| the VSCode settings dialog: a "Temporary rules" card | the bridge's user login | list, add, remove |
| `calu rule add/list/remove` in a terminal | the user login | for scripts and for a machine without a client |

The database enforces the limits. A check constraint keeps `expires_at - created_at` between 1
minute and 7 days, and at 1 day at most for a rule that lifts built-in protections. No database
role keeps a session from writing the table: since migration 0043 every calu process uses the
user login (`calu_admin`). What keeps Claude out is that it has no database login at all, as for
every other table. Adding and removing go into the audit log, as setting changes do (D43).

### The slash commands

```
/rule [duration] [--global | --project | --session] <text>    add; duration 1m..7d, default 1d
/rules                                                        list the active ones with ids
/rule-remove <id>                                             remove one before it expires
```

- Duration: a number with `m`, `h` or `d` (`30m`, `1h`, `12h`, `3d`, `7d`); nothing smaller
  than a minute. A missing duration means `1d`. More than 7 days is refused, with the hint to
  add the rule to the checker instructions instead.
- Scope default: the session. `--project` and `--global` must be typed.
- The client answers with the rule as stored ("#12 added for this project until Fri 14:00").

## 5. Escalation dialogs

### What the checker suggests

When the command or edit checker answers ESCALATE, its answer may carry one suggested rule (a
new optional field `suggested_rule`), e.g. "Claude may run the project's database tests against
local containers." The instructions ask for a rule that would have let the step pass, written as
an intent or a scope, not as one literal command line. The checker leaves the field empty when no
rule would be sensible. That is the case for a backdoor, a deliberate evasion, or a step that
only a person can judge each time.

### What the dialog shows

Below the usual Allow and Deny, one editable text field, filled with the suggestion if there is
one:

```
  Temporary rule (optional; added only if the field is not empty)
  [Claude may run the project's database tests against local containers.______]
  for [1d v]   scope [this session v]
```

- If the field is empty or holds only white space, no rule is added. The user can clear the
  suggestion, change it, or write a rule where the checker suggested none.
- The rule is added when the user presses Allow or Deny. With Deny, the field is the place for a
  refusal ("ask before any network access from tests").
- The rule is added by the client with the user login, before the decision is sent. If adding
  fails, the decision is still sent, and the dialog says that the rule was not added.
- Every client with escalation dialogs gets this: the VSCode chat, the `calu web` chat pane and
  escalation page, and the full-screen terminal client. The line client and ACP show the
  suggestions as text and offer `/rule`.

As built (2026-11-27): the VSCode chat, the web pane and the escalation page have the field
with duration and scope. The full-screen client's cards have no form fields, so it shows the
suggestion as the `/rule` command that adds it; the line client prints it with the
escalation. The PyCharm card has the field, always for this session and one day. ACP is not
changed.

## 6. The built-in protections (the owner chose B)

The owner's example asks for something the rules cannot give as designed above. calu escalates
two things before any checker runs, and no list or instruction can change that (D160):

- every command that runs calu itself (`calu`, `uv run calu`, `python -m calu`, ...);
- every read or write of calu's own directories (`~/.config/calu`, the state directories,
  `/etc/calu` for root's calu).

The reason is self-approval: a Claude that may run `calu` can run `calu resolve` and decide its
own escalations, `calu config set` to switch checks off, or `calu rules` to rewrite the lists.
A checker that reads "calu may run the calu executable" would approve exactly that.

Options:

**A. The built-in protections stay absolute.** A temporary rule never lifts them, and such steps
keep escalating to a person one by one. The example rule then helps only for what lies outside
calu's own files and commands. Nothing becomes less safe, but debugging calu inside calu stays
tedious.

**B. A rule may lift them, with limits.** The rule needs an explicit flag ("lifts built-in
protections") and is shown in red everywhere. It can only be added from the web settings or a
terminal, never from the chat or an escalation dialog, and it lasts at most one day. Even then
some calu commands stay escalated: those that change what steers the checks (`calu resolve`,
`calu config`, `calu rules`, `calu checker`, `calu token`, `calu setup`, `calu honesty trust`)
and every write to the credential store. The risk: Claude can read calu's state and run calu's
other commands, and the list of commands that stay escalated must be kept complete.

**C. A rule may lift them without limits.** Claude can then approve its own escalations while
the rule is active.

I recommended A. The owner chose B: "It's not as if claude has no safeguards. We are protecting
against a malicious agent here, not against a claude process whose code is malicious or already
taken over."

### What was built for B, and what replaced it (2026-11-27, PLAN D291)

Claude first built B as a flag on the rule (`lifts_builtin`, `calu rule add --lift-builtin`,
one day at most, with fixed lists of calu subcommands and files that kept asking). The owner
rejected that on 2026-11-27: "I never asked for a --lift-builtin thing, this is not generic
enough. Don't we have allow and denylists? This is what they are for, there should never have
been hardcoding." Of two ways, a regex in the deny list or "keep the calu hardcoding rule but
make it a configurable flag", Claude took the second, which the owner had offered.

Now:

- calu's built-in rules are settings, each on by default and settable per project:
  `built_in_rules.calu_commands`, `calu_container`, `calu_files`, `worktree_force_remove`
  (`calu.config.BuiltInRulesConfig`). The web settings and the VSCode settings dialog show
  them, as every setting.
- With a switch off, its steps are judged like any other: by the allow and deny lists, the
  checkers and Claude Code's auto mode. A temporary rule can then tell the checkers to allow
  them, as any other rule does.
- A temporary rule has no flag of its own; migration 0003 dropped the column and the one-day
  limit that went with it.

## 7. The owner's decisions (2026-11-26)

1. Built-in protections: B (section 6).
2. One suggested rule at most, an editable free text in the dialog; the user can write one
   where the checker suggested none; empty or white space adds nothing.
3. Default scope: the session.
4. Durations from 1 minute (`1m`) to 7 days; nothing smaller than a minute.
5. Claude is not told which rules are active ("it might be too easy to exploit if it definitely
   knows which rules apply to it").
6. The changeset checker reads the rules too.
7. Every UI part is tested in the Cua desktop as it is added, not only at the end. The whole
   feature works end to end in the Cua desktop before the owner tests it by hand.

8. Later on 2026-11-26, while step 3 was built: "temporary rules should be creatable and
   manageable in the web UI, terminal and IDE extensions alike." So the PyCharm plugin gets
   the commands too, which the plan below did not list.
9. 2026-11-27: no lifting flag on a rule; calu's built-in rules become settings (section 6,
   end).

## 8. Plan of work

| Step | Content | Size |
|---|---|---|
| 1 | Migration: table `temporary_rules`, check constraints, grants, audit trigger; `calu.temp_rules` with parsing of durations and scopes, add, list, remove, audit | half a day |
| 2 | The checkers' prompt part (command, edit, changeset), the session's reload and the expiry at prompt time; the lifting of built-in protections in the rules engine with its fixed exceptions; tests with fake checkers | half a day |
| 3 | `calu rule add/list/remove`; the slash commands in `calu attach` (both clients); the bridge's `ide.rules.*` requests and the slash commands of the VSCode chat and the web pane; Cua test of the VSCode chat | half a day |
| 4 | The settings pane of `calu web`, the card in the VSCode settings dialog; Cua test of both | 3 hours |
| 5 | `suggested_rule` in the checkers' answer and instructions, in `escalation.request`, and the dialogs in the VSCode chat, the web pane and escalation page, and the full-screen client; Cua test of the dialogs | 1 day |
| 6 | Documentation (DOCUMENTATION.md, the protocol, CONFIGURATION if a setting comes up), PLAN.md decision | 2 hours |

About three and a half days in all. Steps 1 to 3 give a working feature from the chat and the
terminal. I report after step 3 and after step 5.
