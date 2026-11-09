"""Subagent result checker: review, note injection, escalation (offline)."""

from pathlib import Path

import pytest
from pydantic_ai import CachePoint, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers import CheckContext
from calu.checkers.result_checker import ResultChecker, ResultVerdict, SubagentResult

pytestmark = pytest.mark.anyio


def agent_turn(result: str) -> list[list[dict]]:
    return [
        [
            {
                "agent": {
                    "description": "reviewer",
                    "prompt": "Review the change strictly.",
                    "result": result,
                    "actions": [{"text": "reviewing"}],
                }
            },
            {"text": "done"},
        ]
    ]


async def test_prompt_contains_task_result_and_context(tmp_path: Path):
    instructions = (
        Path(__file__).resolve().parents[2] / "src/calu/defaults/checkers/subagent-result.md"
    ).read_text(encoding="utf-8")
    seen = {}

    def fake(messages, info: AgentInfo) -> ModelResponse:
        seen["content"] = next(
            p.content for p in messages[-1].parts if isinstance(p, UserPromptPart)
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"verdict": "ANNOTATE", "note": "Only point 1 is in scope.", "summary": "s"},
                )
            ]
        )

    checker = ResultChecker(instructions, FunctionModel(fake))
    review = await checker.review(
        SubagentResult(
            agent_id="a1",
            description="reviewer",
            prompt="Review X",
            result="1. bug\n2. add a plugin system",
        ),
        CheckContext(prefix="User: this is a prototype", turns="Turn 3"),
    )
    assert review.verdict is ResultVerdict.ANNOTATE
    content = seen["content"]
    assert [type(p) for p in content] == [str, CachePoint, str, CachePoint, str]
    assert "prototype" in content[0] and "Review X" in content[4] and "plugin system" in content[4]
