"""Data types shared by the checkers: the step under review, the decision, the thinking effort."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

CheckerEffort = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
"""How much a checker model may think, passed to the provider as Pydantic AI's `thinking`
setting: "off" asks for no thinking at all, the rest name a level. None (the default) leaves the
provider's own default alone, and a model without a thinking mode ignores it."""


class Decision(StrEnum):
    """Outcome of a check (agent/records/PLAN.md D21).

    PASS is the normal answer: the checker has no objection, and Claude Code's auto mode decides
    about the step as it would without calu. ESCALATE pauses the tool call until a human allows
    or denies it. ALLOW and DENY are the deliberate exceptions: ALLOW runs the step whatever auto
    mode would have said, DENY stops it and tells Claude why. Without auto mode, PASS has the
    same effect as ALLOW, because the checkers then decide alone.
    """

    PASS = "PASS"
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"

    @property
    def approves(self) -> bool:
        """Whether the step may run as far as this checker is concerned.

        Returns:
            True for PASS and ALLOW.
        """
        return self in (Decision.PASS, Decision.ALLOW)


class CodeMark(BaseModel):
    """A file or a symbol that the honesty checker calls dishonest, or honest again.

    "Dishonest" means that the code and what it says about itself disagree: a name or a
    documentation comment promises something the code does not do. calu builds the marks from
    the honesty checker's answer (`HonestyAnswer.decision`) and keeps them in the session's
    honesty state (`calu.honesty.HonestyState`) after the step ran.
    """

    path: str = Field(
        description=(
            "Path of the file, relative to the project root, e.g. `src/parser.py`. Never "
            "absolute and never outside the project."
        )
    )
    symbol: str = Field(
        default="",
        description=(
            "The function, method or class inside the file, e.g. `fields` or "
            "`Parser.fields`. Empty if the whole file is meant."
        ),
    )
    reason: str = Field(
        description=(
            "What disagrees with what, in one or two sentences, and both ways to settle it, "
            "favouring neither (which behaviour is intended is Claude's decision): "
            '"the docstring promises a sorted list, the code returns insertion order; either '
            'sort the list or document the order". Claude reads this when a step is refused.'
        )
    )

    def key(self) -> tuple[str, str]:
        """Return what identifies this mark, for matching a clean mark to an open one.

        Returns:
            The path and the symbol; two marks with the same pair are the same spot.
        """
        return self.path, self.symbol

    def label(self) -> str:
        """Render the marked spot for a person or for Claude.

        Returns:
            `path:symbol`, or the path alone when no symbol is named.
        """
        return f"{self.path}:{self.symbol}" if self.symbol else self.path


class StepVerdict(BaseModel):
    """Structured output of a checker that approves or refuses a step.

    This is the form the model of the command, edit and changeset checkers and of every
    user-defined checker answers in. Field descriptions are part of the JSON schema the
    checker model sees. It has no field for honesty marks: honesty is judged by the honesty
    checker alone (agent/records/PLAN.md D224), which answers with an `HonestyAnswer`.
    """

    decision: Decision = Field(
        description=(
            "PASS if no rule of your instructions applies (the normal answer); ALLOW if a "
            "rule explicitly approves the step; DENY if a rule forbids it; ESCALATE to pause "
            "the step and ask a person."
        )
    )
    reason: str = Field(
        description=(
            "Why this decision was made. For DENY, say what Claude should do instead; Claude "
            "reads this text. For ESCALATE, say what the person should look at. For ALLOW, "
            "name the rule. May be empty for PASS. At most about 100 words."
        )
    )
    comment: str = Field(
        default="",
        description=(
            "Only for PASS and ALLOW: an optional remark to Claude that does not block the "
            "step. Claude reads it and it is shown in the user's chat. Leave empty if there "
            "is nothing worth saying. At most about 100 words."
        ),
    )
    step_summary: str = Field(
        description=(
            "What the step does and why Claude is doing it: one or two plain sentences for a "
            "small step; for a large edit or command, describe each substantial change "
            "faithfully (up to about 400 words). A command that edits files (sed -i, a "
            "script, a heredoc, ...) must be named as an edit, with the files and the change. "
            "This becomes the record of the step in the context of later checks."
        )
    )
    executes_project_code: bool | None = Field(
        default=None,
        description=(
            "Answered by the command checker, for a shell command and for any other tool "
            "call: true if the step executes code of this project (tests, a build that runs "
            "code, a script, an interpreter, a package script, a make target), false if it "
            "does not (reading, searching, editing, git, formatting). Other checkers leave "
            "it empty."
        ),
    )
    """Whether the step executes code of the project. The gate reads it while `honesty.mode`
    is "enforce" and the honesty state is not clean (`calu.gate`): for a shell command an
    answer without it counts as true there, for another tool as false."""
    writes_outside_worktree: bool | None = Field(
        default=None,
        description=(
            "Answered by the command checker, for a shell command: true if the command plainly "
            "writes or changes source code, a configuration file, a script or another "
            'executable file outside the checkout named on the call\'s "Checkout" line (for '
            "example a shell profile, a file in another checkout, a global configuration, an "
            "installed program). Logs, caches, temporary files, reports and generated data do "
            "not count. Judge what the call shows; you need not find every side effect. False "
            "otherwise. Other checkers leave it empty."
        ),
    )
    """Whether a command writes code or configuration outside its checkouts. With locking on
    (agent/records/notes/locking.md, section 9) such a command takes the global lock
    exclusively instead of shared, like an edit outside every checkout; no answer counts as
    false."""


class CheckDecision(StepVerdict):
    """The decision of one checker about a step, as the gate and the session take it.

    A `StepVerdict` plus the honesty marks. `marks_dirty` and `marks_clean` belong to the honesty
    checker (agent/records/PLAN.md D224): calu fills them when it converts an `HonestyAnswer`
    (`HonestyAnswer.decision`), and the session reads them only from the honesty checker's runs. No
    other checker's model can write them: its answer is a `StepVerdict`.
    """

    marks_dirty: list[CodeMark] = Field(
        default_factory=list,
        description="Code the step leaves dishonest, from the honesty checker's answer.",
    )
    marks_clean: list[CodeMark] = Field(
        default_factory=list,
        description="Code that says what it does again, from the honesty checker's answer.",
    )


ALL_SYMBOLS = "*"
"""Symbol of a clean mark that closes every open mark of its file: what a file-wide HONEST
verdict of the honesty checker turns into (`HonestyAnswer.decision`)."""


class UnitHonesty(BaseModel):
    """What the honesty checker says about one function, method or class."""

    symbol: str = Field(
        description="The function, method or class, e.g. `fields` or `Parser.fields`."
    )
    honest: bool = Field(
        description=(
            "true if its name and documentation comment now agree with what it does; false "
            "if they do not."
        )
    )
    reason: str = Field(
        default="",
        description="For false: what disagrees with what, in one sentence. Claude reads it.",
    )


class FileHonesty(BaseModel):
    """The honesty checker's verdict on one file the step changes."""

    path: str = Field(
        description="Path of the file, relative to the project root, e.g. `src/parser.py`."
    )
    verdict: Literal["HONEST", "DISHONEST"] = Field(
        description=(
            "HONEST if after this step the names and documentation comments in the file "
            "agree with what the code does; DISHONEST if some unit in it does not."
        )
    )
    reason: str = Field(
        default="",
        description=(
            "For DISHONEST: what disagrees with what, in one or two sentences, and both ways "
            "to settle it: change the code to do what the name and documentation say, or "
            "change the name and documentation to say what the code does. Favour neither: "
            "which behaviour is intended is Claude's decision. Claude reads it. Empty for "
            "HONEST."
        ),
    )
    units: list[UnitHonesty] = Field(
        default_factory=list,
        description=(
            "The units whose honesty this step changed, or that are dishonest now. Empty "
            "when the verdict is about the file as a whole."
        ),
    )


class HonestyAnswer(BaseModel):
    """Structured output of the honesty checker (agent/records/PLAN.md D217).

    It judges files, it does not approve or refuse the step: `decision` turns the answer into
    what the gate and the honesty state take.
    """

    files: list[FileHonesty] = Field(
        default_factory=list, description="One entry for every file the step changes."
    )
    escalate: bool = Field(
        default=False,
        description=(
            "true only for what looks deliberate: a backdoor, hidden behaviour, or a name "
            "or comment written to mislead. A human then decides."
        ),
    )
    escalate_reason: str = Field(
        default="", description="For escalate: what the human should look at."
    )
    step_summary: str = Field(description="What the step changes, in one or two plain sentences.")

    def lines(self, verdict: str) -> list[str]:
        """Render the files with one verdict, one line each, for a person and for Claude.

        Args:
            verdict: "HONEST" or "DISHONEST".

        Returns:
            `VERDICT path: reason (symbol: reason; ...)` per file with that verdict.
        """
        found: list[str] = []
        for entry in self.files:
            if entry.verdict != verdict:
                continue
            line = f"{verdict} {entry.path}"
            if entry.reason:
                line += f": {entry.reason}"
            wanted = verdict == "HONEST"
            units = [
                f"{unit.symbol}: {unit.reason}" if unit.reason else unit.symbol
                for unit in entry.units
                if unit.honest == wanted
            ]
            if units:
                line += f" ({'; '.join(units)})"
            found.append(line)
        return found

    def decision(self) -> CheckDecision:
        """Turn the answer into the decision the gate and the honesty state take.

            escalate                ──► ESCALATE with `escalate_reason`
            otherwise               ──► PASS: the step is never stopped by a verdict
            a DISHONEST file        ──► dirty marks: its dishonest units, or the whole file
                                        when none is named
            an HONEST file          ──► one clean mark for all of the file (`ALL_SYMBOLS`)
            an honest unit of a
            DISHONEST file          ──► a clean mark for that unit

        A unit whose symbol the model wrote as `ALL_SYMBOLS` names no unit: as a dishonest
        unit it becomes a mark on the whole file (symbol ""), as an honest unit it is left out.
        Only a file-wide HONEST closes every mark of a file.

        The verdicts are also written out as text: all of them in `reason` (the record of the
        check), the DISHONEST ones in `comment`, which Claude reads with the approval.

        Returns:
            The decision.
        """
        dirty: list[CodeMark] = []
        clean: list[CodeMark] = []
        for entry in self.files:
            if entry.verdict == "HONEST":
                clean.append(CodeMark(path=entry.path, symbol=ALL_SYMBOLS, reason="honest"))
                continue
            named = [unit for unit in entry.units if not unit.honest]
            for unit in named:
                dirty.append(
                    CodeMark(
                        path=entry.path,
                        # Said explicitly rather than left to the state, which reads "*" as
                        # every symbol of the file only on a clean mark.
                        symbol="" if unit.symbol.strip() == ALL_SYMBOLS else unit.symbol,
                        reason=unit.reason or entry.reason or "dishonest",
                    )
                )
            if not named:
                dirty.append(
                    CodeMark(path=entry.path, symbol="", reason=entry.reason or "dishonest")
                )
            clean += [
                CodeMark(path=entry.path, symbol=unit.symbol, reason="honest")
                for unit in entry.units
                # An honest "*" would close every mark of a file the checker calls DISHONEST.
                if unit.honest and unit.symbol.strip() != ALL_SYMBOLS
            ]
        dishonest = self.lines("DISHONEST")
        verdicts = "\n".join([*dishonest, *self.lines("HONEST")])
        if self.escalate:
            reason = self.escalate_reason or "The honesty checker asks for a human."
            return CheckDecision(
                decision=Decision.ESCALATE,
                reason=f"{reason}\n{verdicts}".strip(),
                step_summary=self.step_summary,
                marks_dirty=dirty,
                marks_clean=clean,
            )
        return CheckDecision(
            decision=Decision.PASS,
            reason=verdicts,
            comment="\n".join(dishonest),
            step_summary=self.step_summary,
            marks_dirty=dirty,
            marks_clean=clean,
        )


class Step(BaseModel):
    """One tool call by Claude (main agent or subagent) that is about to run.

    `tool_input` is the input as Claude sent it, e.g. `{"command": ...}` for Bash or
    `{"file_path", "old_string", "new_string"}` for Edit. `agent_id` is `None` for the main
    agent, otherwise the id of the subagent that made the call.
    """

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None = None
    agent_id: str | None = None
    cwd: str
    checkout: str = ""
    """calu's description of the checkouts the call belongs to: the one its working directory
    lies in, e.g. "`.claude/worktrees/wta` (worktree, branch `worktree-wta`)", then, comma
    separated, those its `cd` targets lie in (`calu.checkouts`, D251); "" when the project has no
    worktree."""
    other_checkouts: str = ""
    """calu's description of the project's other checkouts; "" when there are none."""
    sandbox: str = ""
    """calu's sentence on why a Bash call runs, or may run, outside Claude Code's sandbox
    (`calu.config.SandboxConfig.outside_reason`); "" when it stays in the sandbox."""
    generated: str = ""
    """calu's text naming the generated files a shell command refers to, comma separated, e.g.
    "`dist/app.js` (write, redirect >)" (`calu.gate.StepGate.generated_named`); "" when it
    names none."""
    notes: list[str] = Field(default_factory=list)
    """Hints from the user's rules for the checker, e.g. paths outside the allowlists."""
    narration: str = ""
    """Claude's thinking and text before this call, narrated in indirect speech (may be "")."""
    narration_cut: int = 0
    """Characters of Claude's thinking and text before this call that the narrator did not
    read, because they were longer than `calu.context.narrator.MAX_INPUT_CHARS` (0: none).
    The rendered step says so; with `context.escalate_on_cut` an approval becomes an escalation."""
    asked_about: str = ""
    """What Claude Code says it is asking permission for, when **it** raised the question
    (agent/records/PLAN.md D60).

    Its own prompt sentence and subtitle, e.g. "Claude wants to fetch https://example.net/" --
    the SDK says to show this rather than rebuilding a sentence from the tool name and the
    input, and for a capability request like `SandboxNetworkAccess` the name and the input say
    nothing a human can act on. Empty for the ordinary case, where a checker or a rule raised
    the escalation and the step speaks for itself."""


@dataclass(frozen=True)
class SubagentTask:
    """What Claude tells one of its subagents: the task that starts it, or a later message.

    `agent_id` is the protocol id of the subagent (the id of the Agent tool call that starts
    it), or for a message the `to` value of the `SendMessage` call; `prompt` is Claude's text
    as written; `kind` is "start" for the task of a new subagent and "message" for a later
    message; `background` tells whether a new subagent runs in the background.
    """

    agent_id: str | None
    description: str
    prompt: str
    kind: Literal["start", "message"] = "start"
    background: bool = False
    narration: str = ""
    """Claude's thinking and text before the call, narrated ("" if none)."""


@dataclass(frozen=True)
class SubagentResult:
    """The final result of a subagent and the task it was given.

    `agent_id` is the protocol agent id (id of the Agent tool call); `prompt` is the task text
    Claude gave the subagent; `background` tells whether it ran in the background; `handback`
    tells that `result` is the report the subagent handed back with Claude Code's
    `SubagentHandback` tool, reviewed before the tool call runs (agent/records/PLAN.md D252).
    """

    agent_id: str | None
    description: str
    prompt: str
    result: str
    background: bool = False
    handback: bool = False
