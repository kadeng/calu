"""Offline stand-ins for the checker and summarizer models (tests, front-end development).

`rule_based_checker` decides by markers in the rendered step, so scripted scenarios can produce
every decision without a model:

    step text contains "calu:deny"      -> DENY
    step text contains "calu:escalate"  -> ESCALATE
    otherwise                           -> ALLOW
"""

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


def _rule_based(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Decide by the markers in the "# Step to check" part of the last prompt.

    Args:
        messages: The message history.
        info: Agent information; its first output tool receives the decision.

    Returns:
        A response that calls the output tool with decision, reason and step summary.
    """
    prompt = _last_prompt(messages)
    step = prompt.rsplit("# Step to check", 1)[-1]
    if "calu:deny" in step:
        decision, reason = "DENY", "Denied by the rule-based fake checker (calu:deny)."
    elif "calu:escalate" in step:
        decision, reason = "ESCALATE", "Escalated by the rule-based fake checker (calu:escalate)."
    else:
        decision, reason = "ALLOW", ""
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


def fake_models() -> dict[str, str | Model]:
    """Create checker, summarizer and narrator models for `Session(checker_models=...)`.

    Returns:
        New models under the keys "command", "edit", "subagent", "summarizer" and "narrator".
    """
    return {
        "command": rule_based_checker(),
        "edit": rule_based_checker(),
        "subagent": fake_result_checker(),
        "summarizer": fake_summarizer(),
        "narrator": fake_narrator(),
    }
