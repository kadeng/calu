"""Subagent result checker: review, note injection, escalation (offline)."""

from pathlib import Path
from typing import Any

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
    ToolResult,
    TurnResult,
    UserInput,
)
from tests.unit.helpers import make_session

pytestmark = pytest.mark.anyio


def result_checks(rec) -> list[CheckResult]:
    """Return the subagent checker's reviews of results, without its reviews of tasks."""
    return [
        c
        for c in rec.of(CheckResult)
        if c.route == "subagent"
        and not c.step_summary.startswith(("Task for subagent", "Message to subagent"))
    ]


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
    check = result_checks(rec)[0]
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
    assert result_checks(rec)[0].decision == "PASS"


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


def handback_turn(
    report: str, after: list[dict] | None = None, result: str = ""
) -> list[list[dict]]:
    """A subagent that hands its report back, then ends with `result` (empty by default)."""
    return [
        [
            {
                "agent": {
                    "description": "reviewer",
                    "prompt": "Review the change strictly.",
                    "background": True,
                    "result": result,
                    "actions": [
                        {"tool": "SubagentHandback", "input": {"message": report}, "result": "ok"},
                        *(after or []),
                    ],
                }
            },
            {"text": "done"},
        ]
    ]


async def test_a_handed_back_report_is_reviewed_and_its_empty_result_passes(tmp_path: Path):
    """D252: the report of `SubagentHandback` goes to the result checker (not the command
    checker); its note reaches the main agent; the empty final result after it passes
    without a checker, with the reason recorded."""
    from calu.testing import _fake_result_review, _last_prompt, fake_models

    seen: list[str] = []

    def spy(messages, info: AgentInfo) -> ModelResponse:
        if "# Task to review" not in _last_prompt(messages):  # the review of the Agent call
            seen.append(_last_prompt(messages))
        return _fake_result_review(messages, info)

    session, rec, store = make_session(
        tmp_path,
        handback_turn("1. bug 2. polish calu:scope"),
        checker_models={**fake_models(), "subagent": FunctionModel(spy)},
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="small fix only"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert len(seen) == 1 and "handed back its report" in seen[0] and "Its report:" in seen[0]
    checks = result_checks(rec)
    assert [c.decision for c in checks] == ["PASS", "PASS"]
    assert checks[0].comment == "Fake result checker: points 2-3 are out of scope."
    assert "handed its report back with SubagentHandback" in checks[1].step_summary
    assert not [c for c in rec.of(CheckResult) if c.tool_name == "SubagentHandback"]
    injected = [u.text for u in rec.of(UserInput) if "result checker" in u.text]
    assert len(injected) == 1 and 'subagent "reviewer"' in injected[0]
    rows = [s for s in store.steps.values() if s.row.tool_name == "SubagentResult"]
    assert [row.row.tool_input.get("handback") for row in rows] == [True, None]
    assert rows[1].checker_runs == [] and rows[1].decision is not None
    assert "passed without a check" in (rows[1].decision.reason or "")


async def test_claude_codes_pointer_to_a_handed_back_report_passes_without_a_check(
    tmp_path: Path,
):
    """Dogfooding 2026-11-20: a foreground subagent's final result is Claude Code's pointer
    to the report it handed back, not an empty text. The result checker reviewed the
    pointer a second time and sent the main agent a confusing note about it."""
    from calu.testing import _fake_result_review, _last_prompt, fake_models

    seen: list[str] = []

    def spy(messages, info: AgentInfo) -> ModelResponse:
        if "# Task to review" not in _last_prompt(messages):  # the review of the Agent call
            seen.append("x")
        return _fake_result_review(messages, info)

    pointer = (
        'This agent\'s report was delivered to you as a message from "a39b9640361a8957e" '
        "(its SubagentHandback call). Read it there; it is not repeated here."
    )
    session, rec, _ = make_session(
        tmp_path,
        handback_turn("1. fine", result=pointer),
        checker_models={**fake_models(), "subagent": FunctionModel(spy)},
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="review it"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert len(seen) == 1, "only the handed-back report was reviewed"
    checks = result_checks(rec)
    assert "handed its report back with SubagentHandback" in checks[-1].step_summary


async def test_a_person_who_denies_a_handed_back_report_stops_the_handback(tmp_path: Path):
    """D252: an escalated report waits for a person; a denial denies the `SubagentHandback`
    call (the subagent reads why) and the main agent gets the note with the decision."""
    session, rec, _ = make_session(
        tmp_path, handback_turn("Rewrite everything calu:escalate-result")
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        esc = (await rec.wait_for(EscalationRequest))[0]
        assert esc.tool_name == "SubagentResult" and esc.tool_input.get("handback") is True
        await session.decide_escalation(esc.escalation_id, "deny", "keep it small")
        await rec.wait_for(TurnResult)
        await session.stop()
    results = [r for r in rec.of(ToolResult) if r.is_error]
    assert results and "does not want this report handed back" in results[0].content
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
    assert not result_checks(rec)
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
    assert ResultChecker("i", "test", effort="high").settings().get("thinking") == "high"
    assert ResultChecker("i", "test", effort="off").settings().get("thinking") is False


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


def test_an_escalation_without_a_note_takes_the_summary_as_its_note() -> None:
    """A small model often puts what the user has to decide into the summary; the check then
    does not fail, and the person reads that text. An ANNOTATE or DENY without a note is asked
    again. On the last attempt an ANNOTATE without a note counts as ACCEPT, so that the check
    does not fail and make a person decide about an ordinary result; a DENY still fails."""
    from types import SimpleNamespace

    from pydantic_ai import ModelRetry

    from calu.checkers.result_checker import ResultReview, ResultVerdict, _require_note

    first: Any = SimpleNamespace(retry=0, max_retries=2)
    last: Any = SimpleNamespace(retry=2, max_retries=2)
    escalated = _require_note(
        first, ResultReview(verdict=ResultVerdict.ESCALATE, summary="Asks for a redesign.")
    )
    assert escalated.note == "Asks for a redesign."
    kept = ResultReview(verdict=ResultVerdict.ESCALATE, note="Decide.", summary="s")
    assert _require_note(first, kept) is kept
    for verdict in (ResultVerdict.ANNOTATE, ResultVerdict.DENY):
        with pytest.raises(ModelRetry):
            _require_note(first, ResultReview(verdict=verdict, summary="s"))
    given_in = _require_note(last, ResultReview(verdict=ResultVerdict.ANNOTATE, summary="s"))
    assert given_in.verdict is ResultVerdict.ACCEPT
    with pytest.raises(ModelRetry):
        _require_note(last, ResultReview(verdict=ResultVerdict.DENY, summary="s"))


def test_a_note_written_into_the_summary_is_taken_out_of_it() -> None:
    """The failure of 2026-11-27: the model left `note` out and wrote its remark at the end of
    the summary after "Note:". The summary is split there and the check does not fail. The
    schema the model is shown names `note` as required, so that it writes the key at all."""
    from types import SimpleNamespace

    from calu.checkers.result_checker import ResultReview, ResultVerdict, _require_note

    first: Any = SimpleNamespace(retry=0, max_retries=2)
    review = _require_note(
        first,
        ResultReview(
            verdict=ResultVerdict.ANNOTATE,
            summary="The subagent wrote 13 drafts.\n\nNote: Check the quotations first.",
        ),
    )
    assert review.verdict is ResultVerdict.ANNOTATE
    assert review.summary == "The subagent wrote 13 drafts."
    assert review.note == "Check the quotations first."
    assert "note" in ResultReview.model_json_schema()["required"]
    assert ResultReview(verdict=ResultVerdict.ACCEPT, summary="s").note == ""


async def test_a_denied_task_goes_back_to_claude_with_the_reason(tmp_path: Path):
    """Requirement 51: the subagent checker's DENY for a task refuses the Agent call, and the
    reason Claude reads says that it may write the task again. Nobody is asked."""
    turn = [
        [
            {
                "agent": {
                    "description": "reviewer",
                    "prompt": "Rewrite the whole module calu:deny-task",
                    "result": "done",
                    "actions": [{"text": "working"}],
                }
            },
            {"text": "done"},
        ]
    ]
    session, rec, _ = make_session(tmp_path, turn)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="small fix only"))
        await rec.wait_for(TurnResult)
        await session.stop()
    tasks = [
        c
        for c in rec.of(CheckResult)
        if c.route == "subagent" and c.step_summary.startswith("Task for subagent")
    ]
    assert tasks[0].decision == "DENY"
    assert "refused this task" in tasks[0].reason and "write the task again" in tasks[0].reason
    assert "reaches beyond the request" in tasks[0].reason
    assert not rec.of(EscalationRequest)


async def test_a_report_the_checker_denies_is_not_handed_back(tmp_path: Path):
    """Requirement 51: a DENY of the subagent checker for a result stays possible. The
    handback is refused without asking a person, the subagent reads that the checker refused
    it, and the main agent gets the checker's note instead of the report."""
    session, rec, store = make_session(tmp_path, handback_turn("The report calu:deny-result"))
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert not rec.of(EscalationRequest)
    results = [r for r in rec.of(ToolResult) if r.is_error]
    assert results and "The subagent checker refused this report" in results[0].content
    injected = [u.text for u in rec.of(UserInput) if "result checker" in u.text]
    assert injected and "so you do not get it" in injected[0]
    reviewed = next(s for s in store.steps.values() if s.row.tool_name == "SubagentResult")
    assert reviewed.checker_runs[0].decision == "deny"


async def test_a_task_or_result_longer_than_the_checker_reads_escalates(tmp_path: Path):
    """D273: the review shows the start and the end of an over-long task or result; with
    `context.escalate_on_cut` (the default) an approval of it becomes an ESCALATE whose note
    names the cut and the model's own verdict; without it the model's verdict stands."""
    from calu.checkers.result_checker import MAX_RESULT_CHARS, MAX_TASK_CHARS, SubagentTask

    def accept(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"verdict": "ACCEPT", "summary": "s"})]
        )

    checker = ResultChecker("Review subagents.", FunctionModel(accept))
    long_result = SubagentResult(
        agent_id="a1", description="d", prompt="p", result="r" * (MAX_RESULT_CHARS + 10)
    )
    outcome = await checker.run(long_result, CheckContext())
    assert outcome.review.verdict is ResultVerdict.ESCALATE
    assert "10 characters longer than the checker reads" in outcome.review.note
    assert "ACCEPT" in outcome.review.note
    kept = await checker.run(long_result, CheckContext(), escalate_cut=False)
    assert kept.review.verdict is ResultVerdict.ACCEPT

    task = SubagentTask(agent_id="", description="d", prompt="t" * (MAX_TASK_CHARS + 5))
    reviewed = await checker.run_task(task, CheckContext())
    assert reviewed.review.verdict is ResultVerdict.ESCALATE
    short = await checker.run_task(
        SubagentTask(agent_id="", description="d", prompt="t"), CheckContext()
    )
    assert short.review.verdict is ResultVerdict.ACCEPT
