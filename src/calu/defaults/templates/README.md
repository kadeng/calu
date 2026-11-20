# Templates of checker instructions

Example instructions to load into a checker and adapt. Unlike the instructions in
`../checkers/`, `calu setup` loads none of them: you choose one.

```bash
calu checker template list                        # name, checker type, what it says
calu checker template show review-findings        # the text
calu checker template use review-findings         # load it into its checker type
calu checker template use edit-project-rules --append   # add it after the stored text
```

| Template | Checker | What it says |
|---|---|---|
| `review-findings.md` | `subagent-result` | weigh review findings against the complexity their fixes add; security usually weighs heavily but not absolutely; the user outranks a subagent's verdict |
| `command-project-rules.md` | `command` | example rules for commands of an imagined project |
| `edit-project-rules.md` | `edit` | example rules for edits of an imagined project |

Each file starts with a header between two `---` lines: `checker` (the checker type the
template is written for) and `summary` (one line for `list`). The rest of the file is the text
that `use` stores.

`use` replaces the stored text only when it is a text calu shipped or an unchanged template;
your own text is kept unless you pass `--append` (the template goes after it) or `--replace`
(the old text stays in the audit log). The write is refused when somebody changed the
instructions between the read and the write.

The examples are also in [DOCUMENTATION.md](../../../../DOCUMENTATION.md#example-instructions),
and a test keeps the two copies equal.

## Licence

These texts are in the public domain, as far as applicable law allows, like the shipped
checker instructions: the exact wording is in [LICENSE.md](../../../../LICENSE.md). Copy one,
change it, keep the result under any licence or none; no attribution is required.
