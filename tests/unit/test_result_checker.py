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


async def test_an_interrupt_ends_a_waiting_subagent_review_without_telling_claude(tmp_path: Path):
    """The hook that waits for a result review is not cancelled by Claude Code (D119).

    A subagent result that escalated holds a hook open until a human answers. If the turn is
    interrupted instead, nobody is going to answer it: the escalation belongs to a turn that no
    longer exists. The session therefore ends it as **cancelled** -- the waiting call returns,
    the escalation row is closed, and Claude is told nothing, because an interrupted turn needs
    no note about a review of it. Without this the hook would wait for the session's lifetime.
    """
    session, rec, store = make_session(
        tmp_path, agent_turn("Rewrite everything calu:escalate-result")
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        esc = (await rec.wait_for(EscalationRequest))[0]
        assert esc.tool_name == "SubagentResult"

        await session.interrupt()
        # The turn ends, which it cannot do while a hook is still waiting for a decision.
        await rec.wait_for(TurnResult)

        # Nothing was injected: a note about the review of an interrupted turn would be noise,
        # and the interrupt is what Claude already knows about.
        assert not [u for u in rec.of(UserInput) if "result checker" in u.text]
        # The escalation is gone from the session and closed in the store, so `calu escalations`
        # does not keep offering a decision nobody can use.
        assert session.snapshot().escalations == []
        closed = [e for e in store.escalations.values() if e.state != "open"]
        assert closed, "the row has to be closed, or the fleet shows a dead escalation"
        assert any("interrupted" in (e.note or "") for e in closed)
        await session.stop()


def test_the_result_checker_honours_its_effort_like_a_step_checker() -> None:
    """D135: `subagent_checker.effort` was a documented setting that reached nothing."""
    assert ResultChecker("i", "test").settings().get("thinking") is None
    assert ResultChecker("i", "test", effort="high").settings()["thinking"] == "high"
    assert ResultChecker("i", "test", effort="off").settings()["thinking"] is False


async def test_a_reload_gives_the_result_checker_its_new_limits_and_effort(tmp_path: Path):
    """D135: the reload updated its model and instructions and kept its old limits."""
    from dataclasses import replace

    from calu.config import CheckerConfig, Config
    from tests.unit.helpers import default_settings

    session, _rec, _store = make_session(tmp_path, [])
    assert session.result_checker.effort is None
    changed = Config(subagent_checker=CheckerConfig(request_limit=9, max_tokens=1234, effort="low"))
    session.apply_settings(replace(default_settings(tmp_path, changed)))
    assert session.result_checker.effort == "low"
    assert session.result_checker.usage_limits.request_limit == 9
    assert session.result_checker.max_tokens == 1234
