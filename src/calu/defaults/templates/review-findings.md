---
checker: subagent-result
summary: Weigh a review subagent's findings against the complexity their fixes add; security usually weighs heavily but not absolutely; the user outranks a subagent's verdict.
---
# Review results

- A result of a review or test subagent lists findings. Before the main agent acts, judge
  each finding: how likely is it, and what happens if it is not handled?
- PASS with a comment that names the findings worth fixing and the ones to leave, with one
  line of reason each. Findings to leave: rare corner cases with harmless consequences,
  style preferences, "could be more general", hypothetical future needs.
- A result that asks for new options, abstraction layers or defensive checks for cases
  that cannot occur in this project: PASS with a comment that says the main agent should
  not build them.
- A finding about data loss, security or a wrong result: such findings usually matter.
  Weigh it against what this project is and does (see AGENTS.md). PASS with a comment that
  says whether it matters here and why; when that depends on something only the user knows,
  the comment says to ask the user.
- A finding that contradicts what the user asked for or ruled out (the user wanted a quick
  solution, or said the case cannot occur): PASS with a comment that the user's word wins
  and the main agent should not act on that finding. When the finding could matter a lot
  (data loss, security), the comment says to tell the user about it, and the user decides.
