"""Subagent result checker: review, note injection, escalation (offline)."""

from pathlib import Path

import anyio
import pytest
from pydantic_ai import CachePoint, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers import CheckContext
from calu.checkers.result_checker import ResultChecker, ResultVerdict, SubagentResult
from calu.config import Config
from calu.protocol.messages import (
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    TurnResult,
    UserInput,
)
from tests.unit.helpers import make_session

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


async def test_annotated_result_injects_a_note_for_the_main_agent(tmp_path: Path):
    session, rec, store = make_session(
        tmp_path, agent_turn("1. bug 2. polish 3. redesign calu:scope")
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="small fix only"))
        await rec.wait_for(TurnResult)
        await session.stop()
    check = next(c for c in rec.of(CheckResult) if c.route == "subagent")
    assert check.comment == "Fake result checker: points 2-3 are out of scope."
    injected = [u.text for u in rec.of(UserInput) if "result checker" in u.text]
    assert injected and 'subagent "reviewer"' in injected[0]
    reviewed = next(s for s in store.steps.values() if s.row.tool_name == "SubagentResult")
    assert reviewed.checker_runs[0].checker_name == "subagent-result"
    assert reviewed.checker_runs[0].decision == "pass"


async def test_accepted_result_injects_nothing(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, agent_turn("All good."))
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert not [u for u in rec.of(UserInput) if "result checker" in u.text]
    assert next(c for c in rec.of(CheckResult) if c.route == "subagent").decision == "PASS"


async def test_escalated_result_waits_for_the_user(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, agent_turn("Rewrite everything calu:escalate-result"))
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        esc = (await rec.wait_for(EscalationRequest))[0]
        assert esc.tool_name == "SubagentResult" and "expands the scope" in esc.reason
        await session.decide_escalation(esc.escalation_id, "deny", "keep it small")
        await rec.wait_for(TurnResult)
        await session.stop()
    injected = [u.text for u in rec.of(UserInput) if "result checker" in u.text]
    assert injected and "keep it small" in injected[0]


async def test_the_review_can_be_turned_off(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        agent_turn("Rewrite everything calu:escalate-result"),
        Config(check_subagent_results=False),
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert not [c for c in rec.of(CheckResult) if c.route == "subagent"]
    assert not rec.of(EscalationRequest)
