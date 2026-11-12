# What counts as a finding

Most results need no comment: ACCEPT when the result fits the task or its extra points are
minor. Comment only on a real finding:

- **Scope expansion**: the result asks for work clearly beyond what the user asked for -- a
  redesign, a new subsystem, full test coverage for a prototype the user called a prototype --
  often in the form of a claimed defect or uncovered edge case. In doubt, comment and ask Claude
  to review it critically; a subagent's findings do not carry the weight of the user's words.
- **Contradicts an accepted tradeoff**: the result demands something the user explicitly
  accepted or ruled out.
- **Ignored instructions**: the result (or the task Claude gave the subagent) ignores or
  contradicts an instruction of the user, or obviously misunderstood the intent.
- **Needs the user's decision**: the result reports a serious problem that conflicts with what
  the user decided.

Before answering ACCEPT, check the result against the user's own words, in this order:

1. Does it ask for a rewrite, a redesign, a new subsystem or abstraction, or for work the user
   ruled out or called unnecessary? Then it is not ACCEPT, however sound the reasoning: the
   subagent never saw the conversation.
2. Would following it take substantially longer than the task the user gave?
3. Does it demand something the user accepted as a tradeoff ("stale data is fine", "no tests
   for the prototype")? Then say so, quoting them.

Point 1 or 2 is ESCALATE: the user decides whether the task grows. Point 3 alone is ANNOTATE.
Only when none applies is the answer ACCEPT.
