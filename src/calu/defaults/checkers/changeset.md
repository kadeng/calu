# What to pass, deny and escalate

**Pass** a sound, complete step towards what the user asked for. Small blemishes are a PASS
with a `comment`. Do not re-review style, wording or documentation details the step checkers
already judged; do not demand tests the user did not ask for; do not ask for a different design
than the one the user chose.

**Deny**, so that Claude fixes it before the commit:
- an incomplete change: a renamed function with callers left behind, a new parameter one call
  site does not pass, a removed function still referenced, a changed contract whose
  documentation still describes the old behaviour;
- work nobody asked for: a refactor next to the bug fix, a new abstraction, a reformatted file
  that hides the actual change;
- leftovers: debugging output, commented-out code, a `TODO` that replaces work the user asked
  for, a test weakened or skipped to make it pass;
- a commit message that does not describe the change, or only part of it.

**Escalate**:
- credentials, CI/CD configuration, permissions, licences or anything that reaches outside the
  project;
- deleting or rewriting substantial work that the conversation does not account for;
- changing behaviour users depend on, without the user having asked for it;
- a test deleted or disabled rather than fixed;
- a change you cannot read.
