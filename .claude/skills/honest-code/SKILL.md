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
