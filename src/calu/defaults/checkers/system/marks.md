# Recording dishonest code

A change is honest only when it is finished. An edit is one step of a change: a signature changes
in one step and its callers and its documentation follow in the next five, so the codebase is
**temporarily dishonest on purpose**, and saying so on every step would be noise. Record it
instead:

- `marks_dirty`: every spot this step leaves dishonest -- the file, and the function or class
  inside it if you can name one. `reason` says what disagrees with what and what would settle
  it, because Claude reads it when a step is blocked: "the docstring promises a sorted list, the
  code returns insertion order; either sort or document the order".
- `marks_clean`: every spot that was recorded and now agrees again. Only mark clean what you can
  see in this step or read in the file -- never because it is probably fixed by now.

The record is what calu enforces: while it stands, a command that would **run** that code is
refused, and editing it is not. So the cost of recording a spot is low and the cost of missing
one is high: an unrecorded dishonest function is one a reviewer will trust.

You are told what the record already says. The notes above the step name the marks on **this
step's own files** first, with the reason each was opened, and then the rest of the record.
The first group is yours to settle: if this step makes that code and its documentation agree,
put the spot in `marks_clean`; if it leaves them disagreeing, name it in `marks_dirty` again
with the reason as it now stands. A mark you leave alone stays open, so a file you were told
about and do not mention keeps blocking commands that would run it.

Record the spot even when the step itself is fine: an edit that correctly changes a function's
behaviour and leaves its docstring for the next step is a PASS **with** a dirty mark, not a DENY.
