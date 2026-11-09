"""Resolution of the model strings of checkers, narrator and summarizer.

Most Pydantic AI model strings ("fireworks:...", "bedrock:...", "openai:...") are passed through
unchanged. Two need settings that a plain string cannot carry:

    "mistral:<id>"                     -> MistralModel with the EU endpoint
                                          (https://api.eu.mistral.ai: EU/EFTA data centres);
                                          MISTRAL_BASE_URL overrides it (e.g. https://api.mistral.ai)
    "bedrock:<...anthropic.claude...>" -> BedrockConverseModel with thinking off: with thinking
                                          on, Pydantic AI cannot force the structured answer tool

Models are created lazily by Pydantic AI only when called, so resolving needs no API key.

`run_bounded` runs an agent with a cap on the length of each answer and one retry, for all
models: a model that repeats itself without end, or reasons at great length, would otherwise
cost time and tokens and end the run with `UsageLimitExceeded`.
"""

import logging
import os
from collections.abc import Sequence

from pydantic_ai import Agent, AgentRunResult, CachePoint, UsageLimits
from pydantic_ai.exceptions import IncompleteToolCall, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.mistral import MistralModel
from pydantic_ai.providers.mistral import MistralProvider
from pydantic_ai.settings import ModelSettings

MISTRAL_EU_BASE_URL = "https://api.eu.mistral.ai"
"""Mistral's EU endpoint; the default for all "mistral:" models."""

MAX_ANSWER_TOKENS = 4_000
"""Default cap on the output tokens of one model answer (thinking included on most providers)."""

log = logging.getLogger(__name__)


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
        return await agent.run(
            prompt, model=model, usage_limits=usage_limits, model_settings=settings
        )
    except (IncompleteToolCall, UsageLimitExceeded) as exc:
        log.warning("%s: answer too long, asking once more (%s)", agent.name, exc)
    return await agent.run(prompt, model=model, usage_limits=usage_limits, model_settings=settings)


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

    Returns:
        A `Model` for the cases in the module docstring; otherwise `model` unchanged.

    Example:
        `resolve_model("mistral:mistral-small-2603")` is a `MistralModel` whose provider uses
        https://api.eu.mistral.ai; `resolve_model("fireworks:x")` is "fireworks:x".
    """
    if not isinstance(model, str):
        return model
    provider, _, name = model.partition(":")
    if provider == "mistral" and name:
        base_url = os.environ.get("MISTRAL_BASE_URL") or MISTRAL_EU_BASE_URL
        return MistralModel(name, provider=MistralProvider(base_url=base_url))
    if provider == "bedrock" and "anthropic.claude" in name:
        return BedrockConverseModel(name, settings=ModelSettings(thinking=False))
    return model
