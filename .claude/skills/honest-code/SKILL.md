---
name: honest-code
description: The rule for honest code and documentation. Use whenever you write, rename or change a function, method, class, module, script or executable, and before you run or test code you changed.
---

# Honest code

Code is **honest** when a reader can decide whether it is safe to run from its name and its
documentation alone, without reading the body.

## The rule

A unit is anything that can be executed: a function, method, class, module, script, command or
executable. A unit is honest when all three hold:

1. **Its name says what it does.** The name alone must not mislead: `hello_world` does not
   write to a database or train a model.
2. **Its documentation states the contract.** A doc comment, or documentation kept next to the
   code, gives the intent, the behaviour, the inputs, the outputs and the side effects.
3. **It calls only honest units and trusted third-party libraries.**

A unit is **dishonest** when it does something else than its name and documentation say, does
more, or does less.

**What counts as documentation.** The name and the doc comment, or documentation kept next to
the code: what a reader sees when looking at the unit. A README or a guide is outside this
rule; it may or may not be read by somebody who looks at a function.

**What counts as trusted.** That depends on the project. Typically: popular libraries with
good documentation, and components, libraries and applications that come with the operating
system's distribution or from a renowned vendor. A popular library can be expected to be
honest, because its users would soon report and correct a difference between its name or
documentation and what it does.

**Tests are units too.** A test's name and doc comment say what it checks. Beyond that, an
honest test describes every effect it has other than checking: files, databases, services or
processes it touches, and what it prints, both when it passes and when it fails. Plain error
information needs little description. What must be named is printed data that the test read
from somewhere: a database, a file, an environment variable, a subprocess or global state. A
reader needs that to judge whether running the test is safe and keeps confidential data
confidential.

**Configuration is code.** When a configuration can widen what a unit does -- name classes
or functions that are called, files that are read or written, services that are contacted --
the unit's contract says so ("... according to the configuration" does), and the configuration
then counts like an imported unit: the honesty rule applies to it, wherever it is stored.
Where a configuration format allows no doc comments, it is honest when what it references is:
a reader follows the references.

## How much to write

Write the contract compactly, at the unit's own level of abstraction. State what a competent
engineer would not expect; leave out what follows from what you stated.

- Enough: "Runs the PyTorch training loop with the given configuration." Writing checkpoints
  and logging to an experiment tracker follow from the configuration and surprise nobody.
- Not enough: the same sentence on a function that also uploads the dataset to a server the
  configuration does not name.

A side effect needs its own mention when it reaches outside what the name and the contract
imply: the network, files outside the unit's purpose, other processes, credentials, global or
persistent state.

## When honesty must hold

Code may be dishonest while you are editing it. Before you **run or test** it, make every unit
you changed honest again: name, documentation and behaviour agree, and so do the callers whose
contracts your change affected. Whether an action is permitted is decided from the contract,
so a contract that is wrong at that moment makes the decision wrong.

## Before you run or test, check each changed unit

- Does the name still fit what the body does?
- Does the documentation cover intent, behaviour, inputs, outputs and side effects, and is each
  statement true of the code as it is now?
- Does the unit do anything the name and documentation do not lead a reader to expect?
- Does it leave out anything they promise?
- Is everything it calls either honest or a trusted third-party library?
