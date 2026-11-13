# The checker instructions that ship with calu

A checker's prompt is put together from parts (PLAN.md D181):

| File | What it holds | Who edits it |
|---|---|---|
| `system/<route>.md` | the role, the tools, how to answer | calu (fixed) |
| `<checker>.md` | what to pass, deny and escalate | you: `calu checker edit <name>` |
| `honesty-rules.md` | API contracts and what dishonesty is | rarely: `calu honesty edit` |
| `system/honesty.md` | the honesty checker's own form of answer: HONEST or DISHONEST per file, and when to escalate | calu (fixed) |
| `system/marks.md` | the code honesty state in the step's notes, and how to record marks (honesty mode on) | calu (fixed) |

`calu setup` loads each `<checker>.md` into the `checker_types` table, where a session reads it
from; after that, the database holds the text you run. Editing the files here changes what a
*new* database gets, not what a configured one uses. The honesty rules are the setting
`honesty.rules`; empty means `honesty-rules.md`. `legacy/` keeps the texts shipped before the
split, so `calu setup` can replace an unedited old default with the compact one.

## Licence

**These texts are not under calu's GPL-2.0 licence: they are in the public domain**, as far as
applicable law allows. They are prompts, written to be copied into projects and rewritten there,
so copy one, change a paragraph or rewrite it completely: the result is yours to do anything
with, under any licence or none, and no attribution is required. The exact wording is in
[LICENSE.md](../../../../LICENSE.md); it covers these files, the rule lists in
[`../rules/`](../rules), and anything derived from them — and it does not extend to the rest of
calu's source code, which is GPL-2.0 as usual.

## Writing them

What a checker sees, what it must answer, and what makes an instruction work are described in
[CALU_GUIDE.md](../../../../CALU_GUIDE.md#what-you-can-enforce-with-a-checker); `calu evals`
measures a change against labelled cases before you trust it.
