"""Resolution of the model strings of checkers, narrator and summarizer.

Most Pydantic AI model strings ("fireworks:...", "bedrock:...", "openai:...") are passed through
unchanged. Two need settings that a plain string cannot carry:

    "mistral:<id>"                     -> MistralModel with the EU endpoint
                                          (https://api.eu.mistral.ai: EU/EFTA data centres);
                                          MISTRAL_BASE_URL overrides it (e.g. https://api.mistral.ai)
    "bedrock:<...anthropic.claude...>" -> BedrockConverseModel with thinking off: with thinking
                                          on, Pydantic AI cannot force the structured answer tool

Every checker, the narrator, the summarizer and the honesty reviewer run through Pydantic AI and
get the same code tools. The `claude` CLI runs only the main coding agent: the `claude:` model
type that ran checks through it was removed (agent/records/PLAN.md D258).

Models are created lazily by Pydantic AI only when called, so resolving needs no API key.

`run_bounded` runs an agent with a cap on the length of each answer and one retry, for all
models: a model that repeats itself without end, or reasons at great length, would otherwise
cost time and tokens and end the run with `UsageLimitExceeded`. It also gives the model
`OUTPUT_RETRIES` chances to correct a structured answer that does not validate, and logs the
rejected answers when it runs out of them, since nothing else keeps them. A request the
provider refused (`ContentFilterError`, e.g. Anthropic's `stop_reason: "refusal"`) is written
whole to the refusal file of `calu.logfile` (`record_refusal`), so that what triggered it can be
studied later; the log gets one line that names the file.

Importing this module sets `PYDANTIC_AI_NO_BANNER=1` unless the variable is set already:
Pydantic AI otherwise prints an advertisement for its observability service into the terminal
at an agent's first run, between the lines of `calu run`.
"""

import dataclasses
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import Agent, AgentRunResult, CachePoint, UsageLimits, capture_run_messages
from pydantic_ai.exceptions import (
    ContentFilterError,
    IncompleteToolCall,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu import logfile

BEDROCK_EU_REGION = "eu-central-1"
"""The AWS region an EU inference profile is called in when the environment names none."""

MISTRAL_EU_BASE_URL = "https://api.eu.mistral.ai"
"""Mistral's EU endpoint; the default for all "mistral:" models."""

MAX_ANSWER_TOKENS = 8_000
"""Default cap on the output tokens of one model answer (thinking included on most providers).

Measured on a reasoning model: with 4000, a checker judging an obfuscated command reproducibly spent
the whole cap on thinking and never wrote a decision, which escalates the step with "the checker
failed" instead of deciding it (agent/records/PLAN.md D72). `CheckerConfig.max_tokens` overrides it
per checker."""

OUTPUT_RETRIES = 2
"""How often one run asks the model again for an answer that failed validation (wrong shape,
or refused by an output validator). Pydantic AI's default is 1; a single rejected retry then
failed a whole check ("Exceeded maximum output retries (1)"), which escalates the step to a
person for nothing."""

LOGGED_PART_CHARS = 2_000
"""Longest text of one message part in the log of a failed run (`describe_messages`)."""

log = logging.getLogger(__name__)

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


async def run_bounded[T](
    agent: Agent[None, T],
    prompt: str | Sequence[str | CachePoint],
    *,
    model: str | Model,
    usage_limits: UsageLimits,
    model_settings: ModelSettings,
    max_tokens: int = MAX_ANSWER_TOKENS,
) -> AgentRunResult[T]:
    """Run `agent`; run it once more if its answer runs too long, then give up.

    Each answer is capped at `max_tokens` (model setting `max_tokens`), so a runaway answer
    stops at the provider. A cut-off structured answer (`IncompleteToolCall`) or a run over
    `usage_limits` (`UsageLimitExceeded`) is retried once with a fresh run; such runaways are
    usually random, e.g. a model repeating a sentence. The retry is logged as a warning.
    Within each run, an answer that fails validation is sent back up to `OUTPUT_RETRIES`
    times; when those run out, the answers are logged (`describe_messages`).

    Args:
        agent: The agent to run.
        prompt: The user prompt (text, or texts with cache points).
        model: The model for this run.
        usage_limits: Limits per run (each of the at most two runs gets them in full).
        model_settings: Settings for the run; `max_tokens` is added to a copy.
        max_tokens: Cap on the output tokens of one answer.

    Returns:
        The result of the first run that finished within the limits.

    Raises:
        IncompleteToolCall: If the answer of the second run was cut off as well.
        UsageLimitExceeded: If the second run exceeded `usage_limits` as well.
        Exception: Any other error of `agent.run`, raised at once (no retry).
    """
    settings = ModelSettings(**model_settings)
    settings["max_tokens"] = max_tokens
    try:
        return await _run_logged(agent, prompt, model, usage_limits, settings)
    except (IncompleteToolCall, UsageLimitExceeded) as exc:
        log.warning("%s: answer too long, asking once more (%s)", agent.name, exc)
    return await _run_logged(agent, prompt, model, usage_limits, settings)


async def _run_logged[T](
    agent: Agent[None, T],
    prompt: str | Sequence[str | CachePoint],
    model: str | Model,
    usage_limits: UsageLimits,
    settings: ModelSettings,
) -> AgentRunResult[T]:
    """Run `agent` once with `OUTPUT_RETRIES`; log the model's answers if they never validated.

    Side effects: on `ContentFilterError` (the provider refused the request), the whole request
    goes to the refusal file (`record_refusal`) and the log gets one warning naming it; on any
    other `UnexpectedModelBehavior` (e.g. the output retries ran out), one warning with the
    agent's name, the error and `describe_messages` of the run's answers and of the reasons
    they were sent back.

    Args:
        agent: The agent to run.
        prompt: The user prompt.
        model: The model for this run.
        usage_limits: Limits of the run.
        settings: Model settings of the run, `max_tokens` included.

    Returns:
        The result of the run.

    Raises:
        ContentFilterError: After it was kept and logged, unchanged.
        UnexpectedModelBehavior: After it was logged, unchanged.
        Exception: Any other error of `agent.run`, unchanged and not logged here.
    """
    with capture_run_messages() as messages:
        try:
            return await agent.run(
                prompt,
                model=model,
                usage_limits=usage_limits,
                model_settings=settings,
                retries={"output": OUTPUT_RETRIES},
            )
        except ContentFilterError as exc:
            where = record_refusal(agent, prompt, model, usage_limits, settings, messages, exc)
            response = _last_response(messages)
            details = (response.provider_details or {}) if response is not None else {}
            log.warning(
                "%s: the provider refused the request (category %s, response %s); %s",
                agent.name,
                details.get("refusal_category", "unknown"),
                (response.provider_response_id if response is not None else None) or "unknown",
                f"the whole request is in {where}" if where else "the request was not kept",
            )
            raise
        except UnexpectedModelBehavior as exc:
            log.warning(
                "%s: no valid answer (%s); the model's answers and why they were refused:\n%s",
                agent.name,
                exc,
                describe_messages(messages),
            )
            raise


def _last_response(messages: Sequence[ModelMessage]) -> ModelResponse | None:
    """Return the last model answer of a run.

    Args:
        messages: The messages of one run.

    Returns:
        The last `ModelResponse`, or None if the model gave none.
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return message
    return None


def _model_name(model: str | Model) -> str:
    """Name a model for the refusal file.

    Args:
        model: A Pydantic AI model string or model.

    Returns:
        The string itself, or "<system>:<model name>" of a `Model`.
    """
    return model if isinstance(model, str) else f"{model.system}:{model.model_name}"


def _tool_definitions(agent: Agent[None, Any]) -> list[dict[str, Any]]:
    """List the function tools an agent offers the model, as far as they can be read statically.

    Args:
        agent: The agent.

    Returns:
        One dict per tool of the agent's function toolsets: name, description and the JSON
        schema of its parameters. Tools of other toolsets (built per run) are not listed.
    """
    tools: list[dict[str, Any]] = []
    for toolset in agent.toolsets:
        for name, tool in (getattr(toolset, "tools", None) or {}).items():
            tools.append(
                {
                    "name": name,
                    "description": getattr(tool, "description", None),
                    "parameters": getattr(
                        getattr(tool, "function_schema", None), "json_schema", None
                    ),
                }
            )
    return tools


def refusal_record(
    agent: Agent[None, Any],
    prompt: str | Sequence[str | CachePoint],
    model: str | Model,
    usage_limits: UsageLimits,
    settings: ModelSettings,
    messages: Sequence[ModelMessage],
    exc: ContentFilterError,
) -> dict[str, Any]:
    """Collect everything about a refused request that calu knows, for the refusal file.

    The provider's own wire format is not reproduced; these are its inputs as calu gave them to
    Pydantic AI: the instructions, the prompt, the output schema and the tools, the settings,
    and every message of the run with the provider's answer and its details.

    Args:
        agent: The agent whose request was refused.
        prompt: Its user prompt.
        model: The model of the run.
        usage_limits: The limits of the run.
        settings: The model settings of the run.
        messages: The run's messages, as `capture_run_messages` collected them.
        exc: The refusal.

    Returns:
        A dict that `json.dumps(..., default=str)` can serialise.
    """
    response = _last_response(messages)
    details = (response.provider_details or {}) if response is not None else {}
    try:
        output_schema: Any = agent.output_json_schema()
    except Exception as schema_error:  # a diagnostic must not fail the record
        output_schema = f"(not available: {schema_error!r})"
    return {
        "time": datetime.now(UTC).isoformat(),
        "agent": agent.name,
        "model": _model_name(model),
        "category": details.get("refusal_category"),
        "refusal": details.get("refusal"),
        "response_id": response.provider_response_id if response is not None else None,
        "error": exc.message,
        "body": exc.body,
        "instructions": [
            m.instructions
            for m in messages
            if isinstance(m, ModelRequest) and m.instructions is not None
        ],
        "prompt": prompt
        if isinstance(prompt, str)
        else [p if isinstance(p, str) else repr(p) for p in prompt],
        "output_schema": output_schema,
        "tools": _tool_definitions(agent),
        "model_settings": dict(settings),
        "usage_limits": dataclasses.asdict(usage_limits)
        if dataclasses.is_dataclass(usage_limits)
        else repr(usage_limits),
        "output_retries": OUTPUT_RETRIES,
        "messages": ModelMessagesTypeAdapter.dump_python(list(messages), mode="json"),
    }


def record_refusal(
    agent: Agent[None, Any],
    prompt: str | Sequence[str | CachePoint],
    model: str | Model,
    usage_limits: UsageLimits,
    settings: ModelSettings,
    messages: Sequence[ModelMessage],
    exc: ContentFilterError,
) -> str | None:
    """Append a refused request to the refusal file of `calu.logfile`; never raises.

    Side effects: one line of JSON (`refusal_record`) as a record of
    `calu.logfile.REFUSALS_LOGGER`, only when `calu.logfile.setup` opened the refusal file; a
    failure to build or write it is logged as a warning.

    Args:
        agent: The agent whose request was refused.
        prompt: Its user prompt.
        model: The model of the run.
        usage_limits: The limits of the run.
        settings: The model settings of the run.
        messages: The run's messages.
        exc: The refusal.

    Returns:
        The refusal file's path as text, or None when the request was not kept.
    """
    path = logfile.refusals_file()
    if path is None:
        return None
    try:
        line = json.dumps(
            refusal_record(agent, prompt, model, usage_limits, settings, messages, exc),
            default=str,
            ensure_ascii=False,
        )
        logging.getLogger(logfile.REFUSALS_LOGGER).info("%s", line)
    except Exception:  # the diagnostic must never hide the refusal itself
        log.warning("%s: the refused request could not be kept", agent.name, exc_info=True)
        return None
    return str(path)


def describe_messages(messages: Sequence[ModelMessage]) -> str:
    """Render what a model answered in a run and why answers were sent back, for the log.

    The prompt is left out (it is large, and the session has it): only the model's answers
    (text, structured answers and other tool calls, the length of its thinking) and the retry
    prompts (the validation error or the validator's reason) are listed, in order. Each text
    is cut to `LOGGED_PART_CHARS`.

    Args:
        messages: The messages of one run, as `capture_run_messages` collected them.

    Returns:
        One line per part, e.g. `answer: tool call final_result {...}` or `sent back: ...`;
        "(the model gave no answer)" if there is none.
    """

    def cut(text: str) -> str:
        """Shorten `text` to `LOGGED_PART_CHARS`.

        Args:
            text: The text.

        Returns:
            The text, or its start with the number of characters left out.
        """
        if len(text) <= LOGGED_PART_CHARS:
            return text
        return f"{text[:LOGGED_PART_CHARS]} [... {len(text) - LOGGED_PART_CHARS} more characters]"

    lines: list[str] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    lines.append(f"answer: text {cut(part.content)!r}")
                elif isinstance(part, ToolCallPart):
                    lines.append(
                        f"answer: tool call {part.tool_name} {cut(part.args_as_json_str())}"
                    )
                elif isinstance(part, ThinkingPart):
                    lines.append(f"answer: thinking ({len(part.content)} characters)")
                else:
                    lines.append(f"answer: {part.part_kind}")
            if message.finish_reason not in (None, "stop", "tool_call"):
                lines.append(f"answer ended: {message.finish_reason}")
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                # A refused plain-text answer comes back as a retry prompt; a refused
                # structured answer (a tool call) as a tool return marked "retried".
                if isinstance(part, RetryPromptPart) or (
                    isinstance(part, ToolReturnPart) and part.outcome == "retried"
                ):
                    content = part.content if isinstance(part.content, str) else str(part.content)
                    target = f" ({part.tool_name})" if part.tool_name else ""
                    lines.append(f"sent back{target}: {cut(content)}")
    return "\n".join(lines) or "(the model gave no answer)"


def cache_settings(model: str | Model, key: str) -> ModelSettings:
    """Return model settings that help providers with automatic prompt caching.

    Mistral, Fireworks and OpenAI cache prompt prefixes by themselves, but only on the server
    that handles the request; a stable key per checker routes its requests to the same place.
    Anthropic and Bedrock use the explicit `CachePoint`s of the prompt instead (no settings).

    Args:
        model: The model (string or resolved `Model`).
        key: A stable routing key, e.g. one per checker and core run.

    Returns:
        `mistral_prompt_cache_key` for Mistral, the `x-session-affinity` header for Fireworks,
        `openai_prompt_cache_key` for OpenAI; otherwise empty settings.
    """
    system = model.system if isinstance(model, Model) else model.partition(":")[0]
    if system == "mistral":
        return ModelSettings(mistral_prompt_cache_key=key)  # type: ignore[typeddict-unknown-key]
    if system == "fireworks":
        return ModelSettings(extra_headers={"x-session-affinity": key})
    if system == "openai":
        return ModelSettings(openai_prompt_cache_key=key)  # type: ignore[typeddict-unknown-key]
    return ModelSettings()


def resolve_model(model: str | Model) -> str | Model:
    """Return the model to pass to Pydantic AI for a configured model.

    Reads `MISTRAL_BASE_URL` from the environment for "mistral:" models.

    Args:
        model: A Pydantic AI model string (e.g. "mistral:mistral-small-2603") or a `Model`.

    A provider that refuses to be built — Mistral's, for example, raises when no API key is set
    — leaves the string unchanged and logs a warning, so that resolving a model never stops a
    session from starting. The failure then happens at the first check of that checker, where it
    becomes an ESCALATE whose reason names the error and a human decides. `calu.preflight` warns
    about the missing key at every start.

    Returns:
        A `Model` for the cases in the module docstring; otherwise `model` unchanged.

    Example:
        `resolve_model("mistral:mistral-small-2603")` is a `MistralModel` whose provider uses
        https://api.eu.mistral.ai; `resolve_model("fireworks:x")` is "fireworks:x".
    """
    if not isinstance(model, str):
        return model
    provider, _, name = model.partition(":")
    try:
        # The provider modules are imported where a model of theirs is built: each pulls in
        # its vendor's SDK (mistralai, botocore), which a session with other models never uses.
        if provider == "mistral" and name:
            from pydantic_ai.models.mistral import MistralModel
            from pydantic_ai.providers.mistral import MistralProvider

            base_url = os.environ.get("MISTRAL_BASE_URL") or MISTRAL_EU_BASE_URL
            return MistralModel(name, provider=MistralProvider(base_url=base_url))
        if provider == "bedrock" and "anthropic.claude" in name:
            from pydantic_ai.models.bedrock import BedrockConverseModel
            from pydantic_ai.providers.bedrock import BedrockProvider

            settings = ModelSettings(thinking=False)
            regional = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            if name.startswith("eu.") and not regional:
                # An EU inference profile is called from an EU region; without one in the
                # environment the call would go to the SDK's default region and fail there.
                region = BedrockProvider(region_name=BEDROCK_EU_REGION)
                return BedrockConverseModel(name, provider=region, settings=settings)
            return BedrockConverseModel(name, settings=settings)
    except Exception as exc:
        log.warning(
            "%s cannot be built yet (%s: %s); it is used as a string",
            model,
            type(exc).__name__,
            exc,
        )
    return model
