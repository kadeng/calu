---
checker: edit
summary: Example rules for edits of an imagined project (docstrings, plain prose, no fixes for minor review corner cases, data stays in its region, tests are not weakened). Adapt the names before use.
---
# Project rules

- Every new or changed function has a Google-style docstring with Args, Returns and Raises.
  Missing or stale: DENY and name the function.
- Prose in comments, docstrings and Markdown is plain: subject before object, no slogans,
  no rhythmic lists of three. Otherwise PASS with a comment that quotes the sentence;
  DENY when a whole paragraph is written that way.
- An edit that adds a check, option or code path for a corner case a review subagent
  reported: DENY when the case is rare or harmless here and the change adds noticeable
  complexity; say which finding it answers.
- Code that sends data to a host outside `*.internal.example` or stores personal data
  outside the `eu-central-1` region: ESCALATE.
- A test that is deleted, skipped or weakened: ESCALATE.
