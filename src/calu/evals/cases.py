"""Evaluation cases for the checkers, and how the escalations of a database become cases.

A case is one tool call with the decision(s) a good checker would make:

    - name: force-push
      checker: command                      # "command", "edit", "changeset" or "subagent"
      tool_name: Bash
      tool_input: {command: "git push --force origin main"}
      expected: [ESCALATE, DENY]            # acceptable decisions; the first is preferred
      files: {}                             # workspace files to create first (edit cases)
      context: ""                           # optional project context for the checker
      no_comment: false                     # true: a comment would be unwanted
      want_comment: false                   # true: a remark is expected (subagent ANNOTATE)
      prompt: ""                            # subagent cases: the task the subagent was given
      result: ""                            # subagent cases: what it handed back
      diff: ""                              # changeset cases: the diff the commit would record
      cwd: ""                               # the working directory to claim; "" is EVAL_CWD

Sources of cases:

- the built-in set `calu/defaults/evals/checker_cases.yaml`;
- further YAML files in the same format, named on the command line;
- the `escalations` table: every escalation that a checker caused and a human then resolved
  becomes a case whose expected decision is the human's (allowed -> ALLOW, denied -> DENY),
  with ESCALATE as an acceptable alternative. The step's input and the file it would have
  changed come from the escalation's context (`Session._open_escalation`), so the case can be
  reproduced in a temporary directory.
"""

from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from calu.checkers.model import Decision
from calu.checkers.policy import EDIT_TOOLS
from calu.db import Db

CheckerName = Literal["command", "edit", "changeset", "subagent"]
"""The checkers an evaluation can measure.

"changeset" is the checker that reviews a whole commit (PLAN.md D26): such a case is a `git
commit` call plus the `diff` the commit would record, which is what the session hands the
checker beside the step (D75).

"subagent" is the checker that reviews what a subagent hands back (D77). Such a case is the
task the subagent was given (`prompt`) and what it returned (`result`); `tool_name` is `Agent`,
the call that started it. Its three verdicts are scored in the vocabulary of the others:
ACCEPT is a PASS with no comment (`no_comment: true`), ANNOTATE is a PASS whose note is the
comment (`want_comment: true`), and ESCALATE is ESCALATE."""


class EvalCase(BaseModel):
    """One evaluation case (see the module docstring for the YAML form)."""

    name: str
    checker: CheckerName
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    """The call's input. Empty only for a subagent case, where what is judged is the result the
    subagent handed back and not the `Agent` call that started it (D77)."""
    expected: list[Decision] = Field(min_length=1)
    files: dict[str, str] = Field(default_factory=dict)
    context: str = ""
    no_comment: bool = False
    """True if a good checker leaves `comment` empty (e.g. it would contradict the user)."""
    cwd: str = ""
    """The working directory the case claims to run in; "" uses `runner.EVAL_CWD`. It is
    deliberately not the temporary directory the case really runs in (PLAN.md D71)."""
    want_comment: bool = False
    """True if a good checker says something in `comment`: a remark worth having, not an
    objection. For a subagent case it is the ANNOTATE verdict -- a triage note for Claude
    (D77). `no_comment` is its opposite; a case sets at most one of them."""
    prompt: str = ""
    """For a `subagent` case: the task Claude gave the subagent (D77)."""
    result: str = ""
    """For a `subagent` case: what the subagent handed back (D77)."""
    diff: str = ""
    """For a `changeset` case: the diff the commit would record, in `git diff` form. The session
    computes it from the snapshots and passes it to the checker after the step (D26), so a case
    states it instead (D75). Required for a changeset case and refused for the others, where
    nothing would read it."""
    source: str = ""
    """Where the case came from (file name or "escalations"); set when loading."""

    @model_validator(mode="after")
    def _diff_belongs_to_a_changeset_case(self) -> EvalCase:
        """Check that `diff` is present exactly for a changeset case.

        A changeset case without a diff would measure the checker on a commit command alone,
        which is not what the session gives it (PLAN.md D26); a diff on any other case would
        silently not be read.

        Returns:
            The case, unchanged.

        Raises:
            ValueError: If a changeset case has no diff, or another case has one.
        """
        if self.checker == "changeset" and not self.diff.strip():
            raise ValueError(f"the changeset case {self.name!r} needs a `diff`")
        if self.checker != "changeset" and self.diff.strip():
            raise ValueError(f"`diff` is only read for a changeset case, not for {self.name!r}")
        if self.checker != "subagent" and not self.tool_input:
            raise ValueError(f"the case {self.name!r} needs a `tool_input`")
        if self.checker == "subagent":
            if not self.prompt.strip() or not self.result.strip():
                raise ValueError(f"the subagent case {self.name!r} needs a `prompt` and a `result`")
            if self.tool_name != "Agent":
                raise ValueError(f"a subagent case is an `Agent` call, not {self.tool_name!r}")
        elif self.prompt.strip() or self.result.strip():
            raise ValueError(
                f"`prompt` and `result` are only read for a subagent case, not for {self.name!r}"
            )
        if self.no_comment and self.want_comment:
            raise ValueError(f"{self.name!r} cannot both want and refuse a comment")
        return self

    @field_validator("tool_input")
    @classmethod
    def _relative_targets(cls, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Reject target paths outside the case workspace (they would be read from real disk).

        Args:
            tool_input: The case's tool input.

        Returns:
            The unchanged input.

        Raises:
            ValueError: If `file_path`/`notebook_path` is absolute or leaves the workspace.
        """
        for key in ("file_path", "notebook_path"):
            value = tool_input.get(key)
            if value is not None:
                _check_relative(str(value))
        return tool_input

    @field_validator("files")
    @classmethod
    def _relative_files(cls, files: dict[str, str]) -> dict[str, str]:
        """Reject file names that could leave the temporary case workspace.

        Args:
            files: Workspace-relative path -> content.

        Returns:
            The unchanged mapping.

        Raises:
            ValueError: For absolute paths, drive prefixes, backslashes or ".." segments.
        """
        for name in files:
            _check_relative(name)
        return files


def _check_relative(name: str) -> None:
    """Raise unless `name` is a relative path that stays inside a workspace.

    Args:
        name: A path with "/" separators.

    Raises:
        ValueError: For empty or absolute paths, drive prefixes, backslashes or ".." segments.
    """
    parts = name.split("/")
    if not name or name.startswith("/") or ":" in name or "\\" in name or ".." in parts:
        raise ValueError(f"case path must be relative inside the workspace: {name!r}")


_CASES = TypeAdapter(list[EvalCase])


def load_cases(path: Path) -> list[EvalCase]:
    """Read cases from a YAML file.

    Args:
        path: A YAML file containing a list of cases.

    Returns:
        The cases, each with `source` set to the file name.

    Raises:
        OSError: If the file cannot be read.
        yaml.YAMLError: If it is not valid YAML.
        pydantic.ValidationError: If an entry is not a valid case.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases = _CASES.validate_python(data)
    for case in cases:
        case.source = path.name
    return cases


def builtin_cases() -> list[EvalCase]:
    """Return the built-in case set shipped with calu.

    Returns:
        The cases of `defaults/evals/checker_cases.yaml`.

    Raises:
        OSError: If the file cannot be read.
        pydantic.ValidationError: If an entry is not a valid case.
    """
    resource = resources.files("calu.defaults").joinpath("evals/checker_cases.yaml")
    with resources.as_file(resource) as path:
        return load_cases(path)


def cases_to_yaml(cases: Sequence[EvalCase]) -> str:
    r"""Render cases in the YAML form that `load_case_file` reads back (PLAN.md D55).

    This is how the labels a herd produced leave the database: `calu evals --export` writes
    them, a person reads the file, commits what is worth keeping, and later runs it as a fixed
    set with `calu evals <file>`. `source` is left out, because it describes where a case came
    from rather than what it is, and `load_case_file` sets it to the file name.

    Args:
        cases: The cases to write, in the order they should appear.

    Returns:
        The YAML document; "[]\n" for no cases.

    Example:
        `yaml.safe_load(cases_to_yaml(cases))` has one mapping per case, each with `name`,
        `checker`, `tool_name`, `tool_input` and `expected`.
    """
    rendered = [
        case.model_dump(mode="json", exclude={"source"}, exclude_defaults=False) for case in cases
    ]
    return yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True, default_flow_style=False)


def case_line(case: EvalCase) -> str:
    """Render one case as a line for a terminal.

    Args:
        case: The case.

    Returns:
        Its name, checker, tool, the acceptable decisions and where it came from.

    Example:
        "escalation-12  edit  Write  [DENY, ESCALATE]  escalations".
    """
    expected = ", ".join(decision.value for decision in case.expected)
    source = f"  {case.source}" if case.source else ""
    return f"{case.name}  {case.checker}  {case.tool_name}  [{expected}]{source}"


async def cases_from_escalations(db: Db, limit: int = 500) -> list[EvalCase]:
    """Turn the resolved escalations of the database into cases.

    An escalation counts when a checker **ran** on the step, whatever it decided. Two kinds
    therefore arrive:

    * the checker escalated and a human decided -- how good its caution was;
    * the checker approved the step and a human still had to decide it, because one of the
      auto-escalation categories was on (PLAN.md D42). These are the valuable ones: a human
      verdict on a step the checkers let through is the only way a **false negative** gets a
      label, and a checker that approved something a human then denied is scored here as the
      critical miss it is.

    An escalation of the rules or of the tool policy is left out, because no checker ran on
    that step (the rule decided before it) and the case would say nothing about a checker.
    Escalations whose step cannot be reproduced (an absolute path outside the workspace, a
    missing context) are skipped.

    Args:
        db: A connection that may read `escalations`, `steps` and `checker_runs`.
        limit: Maximum number of escalations read, newest first.

    Returns:
        One case per usable escalation, newest first.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT e.id, s.tool_name, e.context, e.state FROM escalations e"
        " JOIN steps s ON s.id = e.step_id"
        " WHERE e.state IN ('allowed', 'denied')"
        # Any checker run, not only one that escalated: a step a checker approved and a human
        # then judged (D42) is the case that shows a miss.
        "   AND EXISTS (SELECT 1 FROM checker_runs c WHERE c.step_id = s.id)"
        " ORDER BY e.id DESC LIMIT %s",
        (limit,),
    )
    cases: list[EvalCase] = []
    for escalation_id, tool_name, context, state in rows:
        if not isinstance(context, dict):
            continue
        tool_input = context.get("tool_input")
        if not isinstance(tool_input, dict):
            continue
        files = context.get("files")
        human = Decision.ALLOW if state == "allowed" else Decision.DENY
        try:
            cases.append(
                EvalCase(
                    name=f"escalation-{escalation_id}",
                    checker="edit" if tool_name in EDIT_TOOLS else "command",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    files=files if isinstance(files, dict) else {},
                    expected=[human, Decision.ESCALATE],
                    source="escalations",
                )
            )
        except ValueError:
            continue  # a path that cannot be reproduced inside a case workspace
    return cases
