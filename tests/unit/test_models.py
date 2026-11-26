"""Model string resolution and model settings: EU endpoint, Bedrock, checker effort."""

import logging
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessage, ModelResponse, TextPart, ToolCallPart, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.mistral import MistralModel
from pydantic_ai.settings import ModelSettings

from calu.checkers import Checker
from calu.models import MISTRAL_EU_BASE_URL, OUTPUT_RETRIES, resolve_model, run_bounded


def test_mistral_uses_the_eu_endpoint_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    model = resolve_model("mistral:mistral-small-2603")
    assert isinstance(model, MistralModel) and model.model_name == "mistral-small-2603"
    assert model.base_url.rstrip("/").startswith(MISTRAL_EU_BASE_URL)


def test_mistral_base_url_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MISTRAL_BASE_URL", "https://api.mistral.ai")
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    model = resolve_model("mistral:ministral-14b-2512")
    assert isinstance(model, MistralModel) and model.base_url.startswith("https://api.mistral.ai")


@pytest.mark.parametrize(
    "name", ["eu.anthropic.claude-haiku-5-5", "eu.anthropic.claude-sonnet-5-5"]
)
def test_bedrock_claude_runs_without_thinking(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    model = resolve_model(f"bedrock:{name}")
    assert isinstance(model, BedrockConverseModel)
    assert model.settings is not None and model.settings.get("thinking") is False


@pytest.mark.parametrize(
    "spec",
    [
        "fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
        "bedrock:openai.gpt-oss-safeguard-120b",
        "test",
    ],
)
def test_other_strings_pass_through(spec: str):
    assert resolve_model(spec) == spec


@pytest.mark.anyio
async def test_run_bounded_retries_a_runaway_answer_once():
    calls: list[int | None] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append((info.model_settings or {}).get("max_tokens"))
        text = "again " * (5_000 if len(calls) == 1 else 1)
        return ModelResponse(parts=[TextPart(text)])

    agent = Agent(name="test")
    result = await run_bounded(
        agent,
        "hi",
        model=FunctionModel(answer),
        usage_limits=UsageLimits(output_tokens_limit=1_000),
        model_settings=ModelSettings(),
        max_tokens=123,
    )
    assert result.output == "again "
    assert calls == [123, 123]


@pytest.mark.anyio
async def test_run_bounded_gives_up_after_the_second_runaway():
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("again " * 5_000)])

    with pytest.raises(UsageLimitExceeded):
        await run_bounded(
            Agent(name="test"),
            "hi",
            model=FunctionModel(answer),
            usage_limits=UsageLimits(output_tokens_limit=1_000),
            model_settings=ModelSettings(),
        )


class _Verdict(BaseModel):
    verdict: str


def _answers(bad: int) -> tuple[FunctionModel, list[int]]:
    """A model whose first `bad` structured answers lack the required field."""
    calls: list[int] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        tool = info.output_tools[0].name
        args = {"verdikt": "ACCEPT"} if len(calls) <= bad else {"verdict": "ACCEPT"}
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    return FunctionModel(answer), calls


@pytest.mark.anyio
async def test_run_bounded_asks_again_for_an_invalid_answer_up_to_output_retries():
    """The case of 2026-11-26: one invalid retry failed the subagent check with
    "Exceeded maximum output retries (1)"."""
    model, calls = _answers(bad=OUTPUT_RETRIES)
    result = await run_bounded(
        Agent(name="test", output_type=_Verdict),
        "hi",
        model=model,
        usage_limits=UsageLimits(request_limit=10),
        model_settings=ModelSettings(),
    )
    assert result.output.verdict == "ACCEPT"
    assert len(calls) == OUTPUT_RETRIES + 1


@pytest.mark.anyio
async def test_run_bounded_logs_the_refused_answers_when_retries_run_out(
    caplog: pytest.LogCaptureFixture,
):
    model, _ = _answers(bad=100)
    with caplog.at_level(logging.WARNING, logger="calu.models"):
        with pytest.raises(UnexpectedModelBehavior):
            await run_bounded(
                Agent(name="judge", output_type=_Verdict),
                "hi",
                model=model,
                usage_limits=UsageLimits(request_limit=10),
                model_settings=ModelSettings(),
            )
    [record] = [r for r in caplog.records if "no valid answer" in r.getMessage()]
    text = record.getMessage()
    assert text.startswith("judge: no valid answer")
    assert text.count('"verdikt"') == OUTPUT_RETRIES + 1  # every refused answer
    assert "sent back" in text and "verdict" in text  # and why it was refused


def test_a_checker_passes_its_effort_to_the_provider() -> None:
    """The owner's ask: the effort level of a checker model is configurable (CheckerEffort)."""
    plain = Checker("command", "x", "mistral:mistral-small-2603", Path("/w"))
    assert "thinking" not in plain.settings()  # None leaves the provider's default alone

    thinking = Checker("command", "x", "mistral:mistral-small-2603", Path("/w"), effort="high")
    assert thinking.settings().get("thinking") == "high"

    off = Checker("command", "x", "anthropic:claude-haiku-4-5", Path("/w"), effort="off")
    assert off.settings().get("thinking") is False

    # The cache routing key of the model family is still there (one per checker and run).
    assert thinking.settings().get("mistral_prompt_cache_key", "").endswith("-command")


def test_importing_the_models_module_switches_the_banner_of_pydantic_ai_off() -> None:
    """`calu run` prints the checkers' verdicts; an advertisement between them is noise.

    Pydantic AI 2.66 prints a banner at an agent's first run unless this variable is set.
    """
    import os

    import calu.models

    assert calu.models.log
    assert os.environ.get("PYDANTIC_AI_NO_BANNER")
