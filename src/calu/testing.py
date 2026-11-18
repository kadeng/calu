"""Offline stand-ins for the checker and summarizer models (tests, front-end development).

`rule_based_checker` decides by markers, so scripted scenarios can produce every decision
without a model. It knows the test triggers of the shipped checker instructions
(`calu/defaults/checkers/*.md`, section "Test triggers") and the older markers:

    step text contains "calu:test-deny" or "calu:deny"          -> DENY
    step text contains "calu:test-escalate" or "calu:escalate"  -> ESCALATE
    a message of the user contains "calu:test-allow"            -> ALLOW
    a message of the user contains "calu:test-pass"             -> PASS
    step text contains "calu:allow"                             -> ALLOW (runs whatever the
                                                                   harness would say)
    otherwise                                                   -> PASS
    step text contains "calu:comment"   -> a remark to Claude next to the decision

The rows are tried from the top. As in the shipped instructions, "calu:test-allow" and
"calu:test-pass" count only in what the user wrote (the "**User:**" blocks of the context,
`user_texts`), never in the step. The instructions also ask that the user's message says which
step it means; the fake cannot judge that and takes the trigger for the step it checks. The
older "calu:allow" in the step is a marker of scripted test scenarios only.

`honesty_answering` is the stand-in for the honesty checker, which answers per file.
"""

import re
from collections.abc import Mapping, Sequence

from pydantic_ai import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _last_prompt(messages: list[ModelMessage]) -> str:
    """Return the text of the user prompt parts of the last message.

    Non-text content (e.g. images) is skipped.

    Args:
        messages: The message history passed to a `FunctionModel`; must not be empty.

    Returns:
        The prompt texts joined with newlines; "" if the last message has no user prompt.
    """
    parts = getattr(messages[-1], "parts", [])
    texts: list[str] = []
    for part in parts:
        if isinstance(part, UserPromptPart):
            content = part.content
            if isinstance(content, str):
                texts.append(content)
            else:
                texts.extend(c for c in content if isinstance(c, str))
    return "\n".join(texts)


def user_texts(context: str) -> list[str]:
    r"""Return what the user wrote, from the context part of a checker prompt.

    The session renders each user input as a line "**User:**", a blank line and the text
    indented by four spaces (`calu.context.turns.TurnRecord.render_parts`); only calu writes
    that structure.

    Args:
        context: The prompt before its "# Step to check" part.

    Returns:
        One text per "**User:**" block, without the indentation.

    Example:
        user_texts("## Turn 1\n\n**User:**\n\n    Fix it.\n\n**Steps:**") is ["Fix it."].
    """
    found: list[str] = []
    lines = context.splitlines()
    for number, line in enumerate(lines):
        if line != "**User:**":
            continue
        text: list[str] = []
        for following in lines[number + 1 :]:
            if following.startswith("    "):
                text.append(following[4:])
            elif following.strip():
                break
            elif text:
                text.append("")
        found.append("\n".join(text).strip())
    return found


def _rule_based(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Decide by the markers in the last prompt (see the module docstring).

    Args:
        messages: The message history.
        info: Agent information; its first output tool receives the decision.

    Returns:
        A response that calls the output tool with decision, reason and step summary.
    """
    prompt = _last_prompt(messages)
    context, _, step = prompt.rpartition("# Step to check")
    said = "\n".join(user_texts(context))
    if "calu:test-deny" in step or "calu:deny" in step:
        decision, reason = "DENY", "Denied by the rule-based fake checker (calu:deny)."
    elif "calu:test-escalate" in step or "calu:escalate" in step:
        decision, reason = "ESCALATE", "Escalated by the rule-based fake checker (calu:escalate)."
    elif "calu:test-allow" in said:
        decision, reason = "ALLOW", "Allowed by the rule-based fake checker (calu:test-allow)."
    elif "calu:test-pass" in said:
        decision, reason = "PASS", ""
    elif "calu:allow" in step:
        decision, reason = "ALLOW", "Allowed by the rule-based fake checker (calu:allow)."
    else:
        decision, reason = "PASS", ""
    comment = "Fake checker remark (calu:comment)." if "calu:comment" in step else ""
    first_line = next((ln for ln in step.splitlines() if ln.startswith("Tool call")), "a step")
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "decision": decision,
                    "reason": reason,
                    "step_summary": f"Fake summary of {first_line}.",
                    "comment": comment,
                },
            )
        ]
    )


def _fake_summary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Return a fixed summary text that names the number of "## Turn " headings in the prompt.

    Args:
        messages: The message history.
        info: Agent information (unused).

    Returns:
        A text response.
    """
    prompt = _last_prompt(messages)
    turns = prompt.count("## Turn ")
    return ModelResponse(parts=[TextPart(f"Fake summary covering {turns} more turns.")])


def rule_based_checker() -> Model:
    """Create a checker model that decides by markers (see module docstring).

    Returns:
        A `FunctionModel` named "rule-based-checker".
    """
    return FunctionModel(_rule_based, model_name="rule-based-checker")


def fixed_checker(decision: str, reason: str = "", comment: str = "") -> Model:
    """Create a checker model that answers with the same decision every time.

    Used where a test needs two checkers of one route to disagree (PLAN.md D81), which the
    marker-driven `rule_based_checker` cannot do: both of them read the same step and would
    answer the same.

    Args:
        decision: "PASS", "ALLOW", "DENY" or "ESCALATE".
        reason: The reason to return; DENY and ESCALATE need a non-empty one.
        comment: The remark to return, if any.

    Returns:
        A `FunctionModel` named "fixed-<decision>".
    """

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Return the fixed decision.

        Args:
            messages: The message history (unused).
            info: Agent information; its first output tool receives the decision.

        Returns:
            A response that calls the output tool.
        """
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "decision": decision,
                        "reason": reason,
                        "step_summary": "Fixed summary of the step.",
                        "comment": comment,
                    },
                )
            ]
        )

    return FunctionModel(answer, model_name=f"fixed-{decision.lower()}")


def marking_checker(
    dirty: Sequence[tuple[str, str, str]] = (),
    clean: Sequence[tuple[str, str, str]] = (),
    decision: str = "PASS",
    executes: Sequence[str] | None = None,
) -> Model:
    """Create a checker model that answers in the common form with fixed fields.

    Stands in for the command checker's `executes_project_code`, and for a checker that
    writes marks into the common answer although it is not the honesty checker: the session
    ignores those (PLAN.md D224). The stand-in for the honesty checker is `honesty_answering`.

    Args:
        dirty: (path, symbol, reason) of each entry of `marks_dirty`.
        clean: (path, symbol, reason) of each entry of `marks_clean`.
        decision: What it answers about the step itself; "PASS" leaves the step to the others.
        executes: None leaves `executes_project_code` out of the answer. Otherwise the answer
            says true if the "# Step to check" part of the prompt contains one of these
            texts, and false if it contains none (an empty sequence always says false).

    Returns:
        A `FunctionModel` named "marking-checker".
    """

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Return the fixed decision with the fixed marks.

        Args:
            messages: The message history; the step is read for `executes`.
            info: Agent information; its first output tool receives the decision.

        Returns:
            A response that calls the output tool.
        """
        step = _last_prompt(messages).rsplit("# Step to check", 1)[-1]
        extra: dict[str, bool] = {}
        if executes is not None:
            extra["executes_project_code"] = any(text in step for text in executes)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        **extra,
                        "decision": decision,
                        "reason": "" if decision == "PASS" else "the marking fake checker",
                        "step_summary": "Fixed summary of the step.",
                        "comment": "",
                        "marks_dirty": [
                            {"path": path, "symbol": symbol, "reason": reason}
                            for path, symbol, reason in dirty
                        ],
                        "marks_clean": [
                            {"path": path, "symbol": symbol, "reason": reason}
                            for path, symbol, reason in clean
                        ],
                    },
                )
            ]
        )

    return FunctionModel(answer, model_name="marking-checker")


DISHONEST_MARKER = "calu:dishonest"
"""A step that contains this text is answered DISHONEST by `honesty_answering()`."""
BACKDOOR_MARKER = "calu:backdoor"
"""A step that contains this text is escalated by `honesty_answering()`."""
_FILE_LINE = re.compile(r"^File: `([^`]*)`", re.MULTILINE)
"""The line with which a rendered edit names its file (`calu.checkers.render`)."""


def honesty_answering(
    dishonest: Sequence[tuple[str, str, str]] = (),
    honest: Sequence[str] = (),
    escalate: str = "",
) -> Model:
    """Create a model for the honesty checker, which answers HONEST or DISHONEST per file.

    The answer has the form of `calu.checkers.model.HonestyAnswer`. With `dishonest` or
    `honest` the answer is fixed. Without both it is read from the step:

        the step names a file ("File: `path`")           -> HONEST for that file
        ... and contains `DISHONEST_MARKER`              -> DISHONEST for that file
        the step contains `BACKDOOR_MARKER`              -> also `escalate`

    Args:
        dishonest: (path, symbol, reason) of each dishonest spot; the files are answered
            DISHONEST with those units. An empty symbol means the file as a whole.
        honest: Paths answered HONEST.
        escalate: A reason; non-empty sets `escalate` in every answer.

    Returns:
        A `FunctionModel` named "honesty-answering".
    """

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Return the verdicts per file.

        Args:
            messages: The message history; the step is read when no verdict is fixed.
            info: Agent information; its first output tool receives the answer.

        Returns:
            A response that calls the output tool.
        """
        step = _last_prompt(messages).rsplit("# Step to check", 1)[-1]
        files: dict[str, dict[str, object]] = {}
        for path, symbol, reason in dishonest:
            entry = files.setdefault(
                path, {"path": path, "verdict": "DISHONEST", "reason": reason, "units": []}
            )
            units = entry["units"]
            if symbol and isinstance(units, list):
                units.append({"symbol": symbol, "honest": False, "reason": reason})
        for path in honest:
            files.setdefault(path, {"path": path, "verdict": "HONEST"})
        why = escalate
        if not dishonest and not honest:
            named = _FILE_LINE.search(step)
            if named and DISHONEST_MARKER in step:
                files[named[1]] = {
                    "path": named[1],
                    "verdict": "DISHONEST",
                    "reason": f"the fake honesty checker ({DISHONEST_MARKER})",
                }
            elif named:
                files[named[1]] = {"path": named[1], "verdict": "HONEST"}
            if BACKDOOR_MARKER in step:
                why = why or f"the fake honesty checker ({BACKDOOR_MARKER})"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "files": list(files.values()),
                        "escalate": bool(why),
                        "escalate_reason": why,
                        "step_summary": "Fixed summary of the step.",
                    },
                )
            ]
        )

    return FunctionModel(answer, model_name="honesty-answering")


def fake_reviewer(
    findings: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    fails: bool = False,
    seen: list[str] | None = None,
) -> Model:
    """Create a model for the honesty reviewer that answers with fixed findings.

    It reads the "## File N: path" headings of the request and answers for each file.

    Args:
        findings: Path -> (symbol, reason) of each spot the review finds in that file. A file
            that is not named, or named with no spot, is answered as honest.
        fails: True answers with no file at all, which is a malformed answer: the review
            fails.
        seen: A list that gets the request text of every call, for a test to read.

    Returns:
        A `FunctionModel` named "fake-reviewer".
    """
    found = dict(findings or {})

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Return one verdict per file heading of the request.

        Args:
            messages: The message history; the last one holds the request.
            info: Agent information; its first output tool receives the answer.

        Returns:
            A response that calls the output tool.
        """
        prompt = _last_prompt(messages)
        if seen is not None:
            seen.append(prompt)
        files: list[dict[str, object]] = []
        for line in prompt.splitlines():
            head, _, path = line.partition(": ")
            if not head.startswith("## File ") or fails:
                continue
            spots = found.get(path, ())
            files.append(
                {
                    "file": int(head.removeprefix("## File ")),
                    "honest": not spots,
                    "findings": [{"symbol": symbol, "reason": reason} for symbol, reason in spots],
                }
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"files": files})])

    return FunctionModel(answer, model_name="fake-reviewer")


def fake_summarizer() -> Model:
    """Create a summarizer model that returns a fixed text naming the number of folded turns.

    Returns:
        A `FunctionModel` named "fake-summarizer".
    """
    return FunctionModel(_fake_summary, model_name="fake-summarizer")


def _fake_result_review(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Rule-based stand-in for the result checker.

    "calu:escalate-result" in the result -> ESCALATE, "calu:scope" -> ANNOTATE, else ACCEPT.

    Args:
        messages: The request messages (the last one holds the prompt).
        info: Agent information (provides the output tool name).

    Returns:
        A response that calls the output tool with the review.
    """
    prompt = _last_prompt(messages).rsplit("# Result to review", 1)[-1]
    if "calu:escalate-result" in prompt:
        verdict, note = "ESCALATE", "Fake result checker: the result expands the scope."
    elif "calu:scope" in prompt:
        verdict, note = "ANNOTATE", "Fake result checker: points 2-3 are out of scope."
    else:
        verdict, note = "ACCEPT", ""
    args = {"verdict": verdict, "note": note, "summary": "Fake review of a subagent result."}
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


def fake_result_checker() -> Model:
    """Create the rule-based result checker model (see `_fake_result_review`).

    Returns:
        A `FunctionModel`.
    """
    return FunctionModel(_fake_result_review, model_name="rule-based-result-checker")


def _fake_narration(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Stand-in for the narrator: "Narrated: " followed by the narrated text on one line.

    The prompt's heading, the "Thinking:"/"Output:" labels and the fences are left out.

    Args:
        messages: The request messages (the last one holds the prompt).
        info: Agent information (provides the output tool name).

    Returns:
        A response that calls the output tool with the narration.
    """
    prompt = _last_prompt(messages).split("\n", 1)[-1]
    skip = ("Thinking:", "Output:")
    text = " ".join(w for w in prompt.split() if not w.startswith("```") and w not in skip)
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"narration": f"Narrated: {text}"})]
    )


def fake_narrator() -> Model:
    """Create the stand-in narrator model (see `_fake_narration`).

    Returns:
        A `FunctionModel`.
    """
    return FunctionModel(_fake_narration, model_name="fake-narrator")


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
"""An e-mail address, as the stand-in redaction agent recognises one."""


def excerpt_of(prompt: str) -> str:
    """Return the text the redaction agent was given, without its heading and its fence.

    Args:
        prompt: The prompt of `calu.traces.redact.Redactor.redact`.

    Returns:
        The lines between the first and the last line of the prompt that are a code fence.
    """
    lines = prompt.split("\n")
    opening = next(i for i, line in enumerate(lines) if line.startswith("```"))
    return "\n".join(lines[opening + 1 : -1])


def _fake_redaction(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Stand-in for the redaction agent: replaces e-mail addresses, keeps everything else.

    Args:
        messages: The request messages (the last one holds the prompt).
        info: Agent information (provides the output tool name).

    Returns:
        A response that calls the output tool with the excerpt in which every e-mail address
        is replaced by "[redacted: e-mail address]".
    """
    text = _EMAIL.sub("[redacted: e-mail address]", excerpt_of(_last_prompt(messages)))
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": text})])


def fake_redactor() -> Model:
    """Create the stand-in redaction agent of the session traces (see `_fake_redaction`).

    Returns:
        A `FunctionModel` named "fake-redactor".
    """
    return FunctionModel(_fake_redaction, model_name="fake-redactor")


def fake_models() -> dict[str, str | Model]:
    """Create checker, summarizer and narrator models for `Session(checker_models=...)`.

    Returns:
        New models under the keys "command", "edit", "changeset", "subagent", "summarizer",
        "narrator", "honesty" (the honesty checker: `honesty_answering()`, used only where
        that checker is enabled), "honesty-review" (a reviewer that finds every file honest)
        and "trace-redactor" (replaces e-mail addresses).
    """
    return {
        "command": rule_based_checker(),
        "edit": rule_based_checker(),
        "changeset": rule_based_checker(),
        "subagent": fake_result_checker(),
        "summarizer": fake_summarizer(),
        "narrator": fake_narrator(),
        "honesty": honesty_answering(),
        "honesty-review": fake_reviewer(),
        "trace-redactor": fake_redactor(),
    }
