# Temporary rules: design (draft for the owner's approval, 2026-11-26)

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
| `scope` | `global`, `project` (one registered project) or `session` (one session) |
| `expires_at` | Creation time plus the duration: at least 1 hour, at most 7 days, default 1 day |
| `created_by`, `created_at` | Who added it (as for escalation decisions: `client:<name>`, `web:<user>`, `cli:<user>`) |
| `removed_at`, `removed_by` | Set when a person removes it before it expires |
| `origin` | `manual`, or the escalation id whose dialog created it |

Several rules can be active at once, each with its own scope and expiry. An expired or removed
rule stays in the table for the record and is no longer shown to the checkers.

## 3. How the checkers see them

`compose_instructions` gets one more part for the command and edit routes, after the user's
instructions:

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
- The changeset checker (commits) and the subagent checker do not get them; the request names
  the edit and command checkers. This is open question 3.
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

The database enforces the limits: a check constraint keeps `expires_at - created_at` between 1
hour and 7 days, and the session role can only read the table. Adding and removing go into the
audit log, as setting changes do (D43).

### The slash commands

```
/rule [duration] [--global | --project | --session] <text>    add; duration 1h..7d, default 1d
/rules                                                        list the active ones with ids
/rule-remove <id>                                             remove one before it expires
```

- Duration: a number with `h` or `d` (`1h`, `12h`, `3d`, `7d`). A missing duration means `1d`;
  more than 7 days is refused with the hint to add the rule to the checker instructions instead.
- Scope default: the session's project if it has one, else the session. `--global` must be typed;
  a security-relevant override for every project should not happen by default.
- The client answers with the rule as stored ("#12 added for this project until Fri 14:00").

## 5. Escalation dialogs

### What the checker suggests

When the command or edit checker answers ESCALATE, its answer may carry up to two suggested
rules (a new optional field `suggested_rules`):

| Kind | Example |
|---|---|
| `specific` | "Claude may run `pytest tests/db` with `CALU_TEST_DSN` pointing at the local test container." |
| `general` | "Claude may run the project's database tests against local containers." |

The instructions ask for a rule that would have let the step pass, written as an intent or a
scope, not as one literal command line. The checker leaves the field empty when no rule would
be sensible: a backdoor, a deliberate evasion, a step that only a person can judge each time.

### What the dialog shows

Below the usual Allow and Deny:

```
  Also add a temporary rule (optional)
  [ ] Claude may run pytest tests/db with CALU_TEST_DSN pointing at the local ...   [edit]
  [ ] Claude may run the project's database tests against local containers.        [edit]
  [ ] My own rule: [_____________________________________________]
      for [1d v]   scope [this project v]
```

- Nothing is ticked by default. Allow alone never adds a rule.
- A ticked rule is added when the user presses Allow or Deny; its text can be edited first.
- With Deny, the own-rule line is the natural place for a refusal ("ask before any network
  access from tests"); the checker's suggestions are for approvals and are not offered with
  Deny.
- The rule is added by the client with the user login, before the decision is sent. If adding
  fails, the decision is still sent, and the dialog says that the rule was not added.
- Every client with escalation dialogs gets this: the VSCode chat, the `calu web` chat pane and
  escalation page, and the full-screen terminal client. The line client and ACP show the
  suggestions as text and offer `/rule`.

## 6. The built-in protections (needs the owner's decision)

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

I recommend A now, and B as a separate step if debugging calu inside calu needs it often. C is
not safe.

## 7. Open questions for the owner

1. Section 6: A, B or C.
2. Scope default: "the session's project, else the session", with `--global` to be typed. Is
   that right, or should the default be global?
3. Should the changeset checker (commit review) also read the temporary rules? The request names
   the edit and command checkers only. A commit is a command, but its review is a checker of its
   own.
4. Durations below 1 hour (`30m`)? The request says "1 hour to 3 days" and "never longer than
   7d"; this design allows 1h to 7d.
5. Should Claude be told which temporary rules are active? It would explain why a step that
   escalated yesterday passes today, but it also tells Claude exactly what it may now do.
   This design does not tell it.

## 8. Plan of work, after approval

| Step | Content | Size |
|---|---|---|
| 1 | Migration: table `temporary_rules`, check constraints, grants (session role reads only); `calu.temp_rules` with parsing of durations and scopes, add, list, remove, audit | half a day |
| 2 | The checkers' prompt part, the session's reload and the expiry at prompt time; tests with fake checkers | 2 hours |
| 3 | `calu rule add/list/remove`; the slash commands in `calu attach` (both clients); the bridge's `ide.rules.*` requests and the slash commands of the VSCode chat and the web pane | half a day |
| 4 | The settings pane of `calu web`, the card in the VSCode settings dialog | 3 hours |
| 5 | `suggested_rules` in the checkers' answer and instructions, in `escalation.request`, and the dialogs in the VSCode chat, the web pane and escalation page, and the full-screen client | 1 day |
| 6 | Documentation (DOCUMENTATION.md, the protocol, CONFIGURATION if a setting comes up), PLAN.md decision, UI test in the Cua desktop | 2 hours |

About three days in all. Steps 1 to 3 give a working feature from the chat and the terminal;
I would report after step 3 and after step 5.
