# The checker instructions that ship with calu

Each `.md` file beside this one is the system prompt of one checker type. `calu setup` loads
them into the `checker_types` table, where a session reads them from; after that, the database
holds the text you run and `calu checker edit <name>` is how you change it. Editing the files
here changes what a *new* database gets, not what a configured one uses.

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
