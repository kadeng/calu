# The checker instructions that ship with calu

A checker's prompt is put together from parts (agent/records/PLAN.md D181, D224):

| File | What it holds | Who edits it |
|---|---|---|
| `system/<route>.md` | the task and how to answer, in a few sentences | calu (fixed) |
| `<checker>.md` | your project's rules, and the test triggers of the shipped default | you: `calu checker edit <name>` |
| `system/honesty-state.md` | not to rely on code that the honesty state lists as unverified, and to escalate a step that obviously loads code of another checkout (honesty mode on) | calu (fixed) |
| `system/honesty.md` | the honesty checker's task and its form of answer: HONEST or DISHONEST per file, and when to escalate | calu (fixed) |
| `honesty.md` | what is particular to your project: trusted third parties, documentation standard | you: `calu checker edit honesty` |
| `honesty-rules.md` | the honesty standard, read by the honesty checker and the honesty reviewer only | rarely: `calu honesty edit` |
| `system/honesty-review.md` | the honesty reviewer's task and form of answer | calu (fixed) |
| `system/trace-redaction.md` | the session traces' redaction agent: the excerpt is data, the form of answer, when a block may be removed as a whole | calu (fixed) |
| `trace-redaction.md` | what a session trace keeps, what it may lose and which values are replaced | you: `calu traces instructions edit` |

## A thin layer on top of Claude Code's auto mode

The command, edit, changeset and subagent-result checkers hold **your project's rules and
nothing else**. General safety -- destructive commands, secrets, data leaving the machine --
is judged by Claude Code's auto mode classifier, and the instructions say so in one sentence.
The shipped `command.md` and `edit.md` therefore contain one rule each, about a git worktree
outside the project directory, whose files the checkers' read tools cannot read (D251); `changeset.md` and `subagent-result.md` contain the few rules that no classifier can
apply (an incomplete commit, a subagent's result that grows the task).

This relies on auto mode being on. A session runs in auto mode unless the user chose another
permission mode (`claude.permission_mode`, or `permissions.defaultMode` in Claude Code's
settings); `calu config set claude.require_auto_mode true` makes every step that the checkers
pass wait for a person while auto mode is off.

## Test triggers

Every shipped `<checker>.md` ends with a section "Test triggers", for trying calu out:

| Text | Where it counts | Decision |
|---|---|---|
| `calu:test-deny` | anywhere in the step (command, edit, commit diff, subagent result) | DENY (a subagent result: ANNOTATE) |
| `calu:test-escalate` | anywhere in the step | ESCALATE |
| `calu:test-allow` | only in a message **you** typed that says which step it means | ALLOW (a subagent result: ACCEPT) |
| `calu:test-pass` | only in a message you typed that says which step it means | PASS (a subagent result: ACCEPT) |

The two that make a decision stricter work wherever they appear. The two that make it more
permissive work only in the user's own message: the checker sees your messages verbatim and
Claude's text only as narration, so text in a command, a file or a tool's output approves
nothing. They are part of the **default** instructions. A project that relies on its checkers
deletes the section: `calu checker edit <name>`, remove everything from the line
`# Test triggers` to the end, save (`calu.defaults.instructions.without_test_triggers` does
the same for a client that offers it as a button).

`calu setup` loads each `<checker>.md` into the `checker_types` table, where a session reads it
from; after that, the database holds the text you run. Editing the files here changes what a
*new* database gets, not what a configured one uses. The honesty rules are the setting
`honesty.rules`; empty means `honesty-rules.md`. `legacy/` and `legacy/v2/` keep the texts
shipped earlier, so `calu setup` can replace an unedited old default with the present one.

## Licence

**These texts are not under calu's GPL-2.0 licence: they are in the public domain**, as far as
applicable law allows. They are prompts, written to be copied into projects and rewritten there,
so copy one, change a paragraph or rewrite it completely: the result is yours to do anything
with, under any licence or none, and no attribution is required. The exact wording is in
[LICENSE.md](../../../../LICENSE.md); it covers these files, the rule lists in
[`../rules/`](../rules), and anything derived from them — and it does not extend to the rest of
calu's source code, which is GPL-2.0 as usual.

## Writing them

Examples to start from are in [`../templates/`](../templates): `calu checker template list`
names them and `calu checker template use NAME` loads one into a checker.

What a checker sees, what it must answer, and what makes an instruction work are described in
[DOCUMENTATION.md](../../../../DOCUMENTATION.md#rules-a-checker-can-enforce); `calu evals`
measures a change against labelled cases before you trust it.
