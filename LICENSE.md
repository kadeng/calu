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

## Summary

| You... | Then... |
|---|---|
| use calu on your own code | nothing is required of you; your code's licence is unaffected |
| ship a product you built with calu's help | nothing is required of you; calu is not part of your product |
| ship calu, or a fork or modified version of it | GPL-2.0 applies: pass on the source under the same licence |
| embed calu's source in another program you distribute | GPL-2.0 applies to that program |

This file is a summary written for readers, not a legal document. Where it and
[LICENSE](LICENSE) could be read differently, LICENSE is what governs, and a lawyer is who to
ask about your own case.
