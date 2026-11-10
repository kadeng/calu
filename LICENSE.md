# License

calu is free software under the **GNU General Public License, version 2**. The binding text is
[LICENSE](LICENSE); this file explains in plain language what it covers, because the GPL is
often read as saying more than it does.

## What it covers

calu's own source code. If you distribute calu, or a modified or extended version of it, that
version stays GPL-2.0 and you pass the source on with it. Patches and forks are welcome on the
same terms. The licence keeps calu free software and will keep it that way.

## What it does not cover: your work

**The software you develop with calu's help is yours, under any licence you choose.** calu is a
tool you run, like a compiler, an editor, a linter or a test runner — using it creates no claim
on what you build with it. In particular:

- Your repository can be closed source, commercial, or under any open source licence, GPL or
  not. A calu session supervising it changes nothing about its licence.
- Code that Claude writes while calu checks it belongs to you under whatever terms apply to your
  project. calu reviews and records the work; it is not an author of it.
- The checker instructions, rules, AGENTS.md and configuration you write are your own text, not
  a derived work of calu, even though calu reads them. They live in your project and in your
  database.
- What calu produces as it runs — verdicts, comments, summaries, reports, the rows in your
  database — carries no licence condition from the GPL. The GPL sets conditions on copying and
  distributing calu, not on the output of running it.

You also do not have to publish anything to use calu: running it, privately or inside a company,
on any number of machines, triggers no obligation at all. The obligations of the GPL begin when
you **distribute calu itself**.

## The checker instructions are in the public domain

The checker instructions that ship with calu are prompts for a language model, written to be
copied into projects and rewritten there. They are not licensed under the GPL at all:

> The copyright holders of calu place the checker instructions shipped with calu, and the rule
> lists that go with them, **in the public domain**, to the fullest extent permitted by
> applicable law. Where the law of a jurisdiction does not allow a work to be placed in the
> public domain, the copyright holders grant instead an unconditional, irrevocable, worldwide,
> royalty-free licence to use, copy, modify, publish, distribute and sublicense those texts and
> any derivative of them, for any purpose, with no conditions of any kind. **The creation of
> derivative works and their licensing under other terms are not limited in any way**, and no
> attribution, marking or notice is required. This dedication covers those texts and works
> derived from them; it does not extend to any other part of calu's source code, which remains
> under the GNU General Public License, version 2.

The texts it covers are the Markdown files under
[`src/calu/defaults/checkers/`](src/calu/defaults/checkers) — `command.md`, `edit.md`,
`changeset.md`, `honesty.md`, `subagent-result.md`, `narrator.md` — the rule lists under
[`src/calu/defaults/rules/`](src/calu/defaults/rules), and the same texts as `calu setup` loads
them into your database and as you then edit them with `calu checker edit`.

So: copy calu's command checker, change a paragraph or rewrite it entirely, and the result is
yours to do anything with — keep it private, ship it inside a closed source product, publish it
under any licence, or sell it. There is nothing to carry and nobody to ask. The same holds, more
obviously, for instructions you wrote yourself, which were never calu's to license.

## No warranty

**calu comes with no warranty and no guarantees of any kind. It is provided as is, free of
charge.** That is sections 11 and 12 of [LICENSE](LICENSE), and it is meant literally:

> calu is provided as is and free of charge. There is no warranty of any kind, express or
> implied, including but not limited to the implied warranties of merchantability and fitness
> for a particular purpose. The entire risk as to the quality and performance of calu is with
> you. In no event will any copyright holder or contributor be liable for any damages arising
> out of the use or inability to use it, including loss of data, lost profits, or a failure of
> calu to work with other software.

Two consequences worth stating, because calu's job invites the opposite assumption:

- **A checker is not a guarantee.** It is a language model reading a step. It will miss things,
  and it will object to things it should not. calu is a second opinion that catches more than no
  review does; it is not a promise that nothing bad gets through, and it does not make an
  unattended agent safe.
- **The sandbox, the rules and the deny lists are not a security boundary you should bet on.**
  They raise the cost of a mistake. Run agents on code and machines where the worst case is
  something you can live with, and keep backups.

The same applies to the public-domain texts above: they are prompts someone wrote, not a
certified policy, and nobody warrants what a model does with them.

## Summary

| You... | Then... |
|---|---|
| use calu on your own code | nothing is required of you; your code's licence is unaffected |
| ship a product you built with calu's help | nothing is required of you; calu is not part of your product |
| ship calu, or a fork or modified version of it | GPL-2.0 applies: pass on the source under the same licence |
| embed calu's source in another program you distribute | GPL-2.0 applies to that program |
| copy or modify calu's shipped checker instructions | nothing is required of you: those texts are in the public domain (above) |

This file is a summary written for readers, not a legal document. Where it and
[LICENSE](LICENSE) could be read differently, LICENSE is what governs, and a lawyer is who to
ask about your own case.
