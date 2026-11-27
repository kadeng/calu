# The supervision level "Unchecked / Claude Code only", and levels that stay as chosen

Design note of 2026-11-27 (PLAN D299, D300). Built the same day, unit layer only.

## 1. What the owner asked for

Requirement 53: "In addition to Checkers decide and Confirm everything, we need to have a
mode where we do not have any checks beyond what Claude Code does itself. Let's call it
"Unchecked / Claude Code only". If it's enabled, we just pass through every check and go
with claude's decision."

Asked whether calu's rule lists and built-in rules still apply in this level, the owner
chose "Keep all rules": only the model checkers are skipped.

## 2. What the level does

A fourth switch, `auto_escalate.unchecked`, next to `commands`, `edits` and `subagents`.

| With `unchecked` on | |
|---|---|
| A step a checker would judge (command, edit, commit, subagent task) | passes with the source "unchecked" and the reason `gate.UNCHECKED_REASON`; Claude Code's permission mode and sandbox decide |
| The rule lists, the built-in rules, the tool policy, the rule for generated files, questions to the user, the clock alarm | apply as before; a step they stop waits for a person |
| What a command wrote (`changes.review`) | measured and recorded, not reviewed |
| A subagent's result | accepted with the summary `session.RESULT_UNCHECKED`; no checker run is recorded |
| Code honesty | no honesty check; `enforce` stops nothing; edited files stay unreviewed; a commit is not certified |
| The marker `#do-honesty-review` | starts no review |
| "Review now" by a person | runs |

Where it is in the code:

- `calu.config.AutoEscalate.unchecked`.
- `StepGate.judge` returns the pass after the rules and the tool policy, before anything
  that needs a checker.
- `Session._reviewed` skips the result checker.
- `Session._changes_config` switches the write-set review off.
- `Session._marker_seen` ignores the marker.

## 3. Choices Claude made (not the owner's)

- **A switch next to the three, not a new setting.** The protocol (`auto_escalate.set`,
  `auto_escalate.changed`, the snapshot), the audit log and every client carry the level
  without a new message. The name `auto_escalate` fits the new switch less well than the
  old three; the class docstring says what the group is now.
- **The three waiting switches still hold.** `unchecked` with `edits` means: every edit
  waits for a person, without a checker's opinion. Clients show such a mix as "custom".
- **No waiting behind an open escalation.** A rule can still open an escalation. Other
  steps do not wait for it in this level.
- **A failed settings reload still holds the session with checkers on** (`HOLD_ALL` has
  `unchecked` off).
- **An old client that sends three switches switches `unchecked` off.** That errs on the
  checked side.

The level is the first rung of the ladder in `calu.acp.modes` (the terminal client and the
ACP adapter), `extensions/vscode/src/shared/levels.ts` and the PyCharm plugin's `Levels.kt`.
ACP clients with boolean options get the switch `auto_escalate_unchecked`.

## 4. A level that stays as chosen (D300)

What the owner saw on 2026-11-27: "Checkers decide" turned into "Confirm everything" by
itself, again and again, and a changed `honesty.mode` did not arrive in the session.

The cause: calu was updated, with a database migration, and `calu serve` was not restarted.
The old code read a column the migration had dropped, so every settings reload failed.
After three failures the session held every checked step (fail closed), and every failed
retry put the hold back. A restart of the server ended it.

Two defects in calu, both fixed:

1. **A reload for another setting reset the level a client chose.**
   `Session.apply_settings` replaced the whole configuration. Now the session follows the
   stored switches only when the stored switches themselves changed.
2. **The hold came back at every failed retry.** Now it is placed once per streak of failed
   reloads (`run.ReloadHold`).
   - A reload that succeeds restores the level from before the hold.
   - A person who chooses another level during the hold keeps it.

The second fix weakens fail closed on purpose. After a person's choice, the session keeps
the settings it read last and uses the chosen level until a reload succeeds. The hold's
message says so. The person has seen that message when choosing.

The VSCode chat no longer says only "sessions started from now on use the stored mode" when
a session does not take a changed `honesty.mode`. It names the likely cause: the settings
could not be reloaded; after an update, restart `calu serve`.

## 5. Not done

- No run in a UI and none with a real Claude Code.
- The web settings form shows the switch in the group "Always ask a human", which does not
  describe it.
- A warning at session start, or a visible mark in the clients' headers, while the level is
  unchecked. Today only the selector shows it.
