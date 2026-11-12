# Code honesty: the state and the marks

The notes of the step hold the code honesty state that calu computed: the baseline (the trusted
commit the files are compared with), the open marks with their reasons, the unreviewed files
and whether a review is required. calu tracks which code was verified to say what it does.

- Do not rely on the names or the documentation of code the state lists: a marked file or
  symbol, and every unreviewed file. Read the implementation with your tools, or ESCALATE.
- `marks_dirty`: every spot this step leaves dishonest by the honesty rules: the file, and the
  function, method or class in it. For a Python file write the symbol as the list of changed
  definitions of the edit names it (`fields`, `Parser.fields`); leave it empty for the whole
  file. `reason` says what disagrees with what and what would settle it; Claude reads it. A step
  that is one part of a larger change is a PASS with a mark.
- `marks_clean`: an open mark on a file this step edits, when the step makes that code and its
  documentation agree. Name only what you see in this step or read in the file.
- A mark you do not name stays open. While the state says "Review required: yes", `marks_clean`
  is ignored: an honesty review clears the marks.
- For a command, set `executes_project_code`: true if the command executes code of this project
  (tests, a build that runs code, a script, an interpreter, a package script, a make target),
  false if it does not (reading, searching, editing, git, formatting). With honesty.mode
  "enforce", calu lets code run only from a clean state and decides that itself from your
  answer.

calu records marks after the step ran without error.
