"""The state of the full-screen client: `calu.tui.model.SessionModel` fed with protocol events.

No Textual, no connection: events go in, the model and its `Change` come out.
"""

from typing import Any

import pytest

from calu.protocol.messages import EVENTS
from calu.tui import scenarios
from calu.tui.capabilities import CAPABILITIES, NOT_AVAILABLE, capabilities
from calu.tui.fake import FakeSession
from calu.tui.model import (
    ALL,
    HANDLED,
    MAIN,
    Change,
    JoinBlock,
    SessionModel,
    StepBlock,
    TextBlock,
    ThinkingBlock,
    TurnEndBlock,
    UserBlock,
    parse_questions,
)


def feed(model: SessionModel, items: list[scenarios.Item], start: float | None = 100.0) -> Change:
    """Apply the events of a scenario, advancing a clock by each item's delay.

    Args:
        model: The model.
        items: The scenario.
        start: The clock at the start; None applies the events as replayed (without a time).

    Returns:
        The merged change.
    """
    now = start
    total = Change()
    for item in items:
        if now is not None:
            now += item.after
        if item.method:
            total.merge(model.apply(item.method, item.params, now))
    return total


def steps(model: SessionModel, tab: str = MAIN) -> list[StepBlock]:
    """Return the step blocks of a tab."""
    return [block for block in model.blocks_of(tab) if isinstance(block, StepBlock)]


def test_the_model_handles_exactly_the_events_of_the_protocol() -> None:
    """A new event has to be shown or deliberately ignored, never dropped unseen."""
    assert {event.method for event in EVENTS} == HANDLED


def test_a_turn_becomes_blocks_in_order_with_one_step_per_tool_call() -> None:
    model = SessionModel()
    feed(model, scenarios.simple_turn())
    kinds = [type(block).__name__ for block in model.blocks]
    assert kinds == [
        "UserBlock",
        "ThinkingBlock",
        "TextBlock",
        "StepBlock",
        "StepBlock",
        "StepBlock",
        "TextBlock",
        "TurnEndBlock",
    ]
    read, edit, tests = steps(model)
    assert (read.tool, read.verdict, read.summary) == ("Read", "PASS", "read gate.py")
    assert edit.checks[0].comment == "the docstring of judge_step still names the old function"
    assert edit.diffs and edit.diffs[0].counts() == (2, 2)
    assert tests.result is not None and "412 passed" in tests.result
    assert tests.duration == pytest.approx(8.7), "from the call to its result, the check included"
    end = model.blocks[-1]
    assert isinstance(end, TurnEndBlock) and end.cost == pytest.approx(0.84) and end.number == 1
    assert model.usage.turns == 1 and model.state == "idle" and model.saw_thinking


def test_a_diff_that_arrives_before_its_tool_call_is_kept_for_it() -> None:
    model = SessionModel()
    diff = {"tool_use_id": "t9", "path": "a.py", "before": "x\n", "after": "y\n"}
    assert not model.apply("edit.diff", diff, 1.0).blocks_added
    model.apply("tool.use", {"tool_use_id": "t9", "name": "Edit", "input": {}}, 1.0)
    assert steps(model)[0].diffs[0].path == "a.py"


def test_several_checks_of_one_step_give_the_strictest_verdict() -> None:
    model = SessionModel()
    model.apply("tool.use", {"tool_use_id": "t1", "name": "Bash", "input": {}}, 1.0)
    check: dict[str, Any] = {"tool_use_id": "t1", "tool_name": "Bash", "route": "command"}
    model.apply("check.result", {**check, "decision": "PASS", "step_summary": "first"}, 1.0)
    model.apply("check.result", {**check, "decision": "DENY", "reason": "no"}, 1.0)
    step = steps(model)[0]
    assert len(step.checks) == 2 and step.verdict == "DENY" and step.summary == "first"


def test_streamed_text_is_collected_and_replaced_by_the_final_text() -> None:
    model = SessionModel()
    model.apply("assistant.delta", {"message_id": "m1", "text": "Hel"}, 1.0)
    change = model.apply("assistant.delta", {"message_id": "m1", "text": "lo"}, 1.1)
    block = model.blocks[0]
    assert isinstance(block, TextBlock) and block.text == "Hello" and block.streaming
    assert change.blocks_updated == {0} and not change.blocks_added
    model.apply("assistant.text", {"message_id": "m1", "text": "Hello there."}, 1.2)
    assert block.text == "Hello there." and not block.streaming and len(model.blocks) == 1


def test_thinking_streams_into_its_own_block_before_the_text() -> None:
    model = SessionModel()
    model.apply("assistant.delta", {"message_id": "m1", "text": "hm", "thinking": True}, 1.0)
    model.apply("assistant.delta", {"message_id": "m1", "text": "Answer"}, 1.1)
    thinking, text = model.blocks
    assert isinstance(thinking, ThinkingBlock) and thinking.streaming
    assert isinstance(text, TextBlock)
    model.apply("assistant.thinking", {"message_id": "m1", "text": "hm, done"}, 1.2)
    assert thinking.text == "hm, done" and not thinking.streaming


def test_an_escalation_opens_a_decision_and_its_resolution_closes_it() -> None:
    model = SessionModel()
    change = feed(model, scenarios.escalation_scenario())
    assert change.bell and model.state == "escalated"
    (decision,) = model.open_decisions()
    assert decision.by_rule and decision.opened_at is not None and not decision.is_question
    step = model.step(decision.tool_use_id)
    assert step is not None and step.verdict == "ESCALATE" and step.waiting == "open"
    assert model.activity_of(model.agents[MAIN]) == "waits for you"
    resolved = model.apply(
        "escalation.resolved", {"escalation_id": "e1", "decision": "allow", "note": "ok"}, 200.0
    )
    assert not model.decisions and step.verdict == "ALLOWED" and step.note == "ok"
    assert [notice.text for notice in resolved.notices] == ["decided elsewhere: allowed"]


def test_a_decision_of_this_client_is_not_reported_as_made_elsewhere() -> None:
    model = SessionModel()
    feed(model, scenarios.escalation_scenario())
    model.decisions["e1"].own = True
    resolved = model.apply("escalation.resolved", {"escalation_id": "e1", "decision": "deny"}, 1.0)
    assert not resolved.notices


def test_an_answered_question_is_shown_as_answered_not_as_denied() -> None:
    model = SessionModel()
    feed(model, scenarios.question_multi())
    (decision,) = model.open_decisions()
    assert decision.is_question and [q.header for q in decision.questions] == [
        "Runner",
        "Checks",
        "Fixtures",
    ]
    decision.picks[0] = {0}
    decision.picks[1] = {0, 2}
    decision.free[2] = "in a new fixtures module"
    assert decision.complete() and decision.answered() == 3
    assert decision.answer_text() == (
        "Runner: pytest\nChecks: unit tests, lint\nFixtures: in a new fixtures module"
    )
    decision.own = True
    model.apply(
        "escalation.resolved",
        {"escalation_id": "e1", "decision": "deny", "note": "The user answered: ..."},
        5.0,
    )
    step = steps(model)[0]
    assert step.verdict == "ANSWERED" and step.note.startswith("Runner: pytest")


def test_a_malformed_question_set_falls_back_to_one_free_text_question() -> None:
    (question,) = parse_questions([{"question": "ok?", "options": ["not", "objects"]}])
    assert question.options == [] and "ok?" in question.question
    assert parse_questions([]) == []


def test_each_agent_has_its_own_blocks_state_and_badges() -> None:
    model = SessionModel()
    feed(model, scenarios.showcase())
    assert model.tabs() == [MAIN, "a1", "a2", ALL]
    assert [agent.label for agent in model.agents.values()] == ["main", "explore", "tests"]
    explore, tests = model.agents["a1"], model.agents["a2"]
    assert explore.background and explore.takes_messages and not tests.takes_messages
    assert [step.tool for step in steps(model, "a1")] == ["Grep", "Grep"]
    assert all(block.agent_id == "a2" for block in model.blocks_of("a2"))
    assert len(model.blocks_of(ALL)) == len(model.blocks)
    assert model.activity_of(tests) == "waits for you"
    assert model.activity_of(explore) == "tool call Grep"
    assert [d.escalation_id for d in model.open_decisions("a2")] == ["e2"]
    assert explore.unread and tests.unread
    model.select("a1")
    assert not explore.unread and tests.unread


def test_the_result_of_an_agent_call_ends_a_foreground_subagent() -> None:
    model = SessionModel()
    feed(model, scenarios.two_subagents())
    model.apply("tool.result", {"tool_use_id": "a2", "content": "done"}, 300.0)
    assert model.agents["a2"].status == "completed"
    model.apply("tool.result", {"tool_use_id": "a1", "content": "started"}, 300.0)
    assert model.agents["a1"].status == "running", "a background agent goes on after its call"


def test_a_message_relayed_to_a_subagent_is_labelled_with_its_target() -> None:
    model = SessionModel()
    feed(model, scenarios.two_subagents())
    queued = {"id": "q1", "text": "also check docs/", "delivery": "inject", "to": "a1"}
    model.apply("chat.queue", {"messages": [queued]}, 1.0)
    assert model.queue[0].to == "a1"
    model.apply("user.input", {"text": "Relay this to explore:\nalso check docs/"}, 2.0)
    model.apply("chat.queue", {"messages": []}, 2.0)
    block = model.blocks[-1]
    assert isinstance(block, UserBlock)
    assert (block.text, block.relayed_to) == ("also check docs/", "explore")
    assert model.queue == []


def test_a_snapshot_opens_escalations_the_events_did_not_carry_and_closes_stale_ones() -> None:
    session = FakeSession()
    scenarios.preload(session, scenarios.escalation_scenario())
    model = SessionModel()
    change = model.load_snapshot(session.snapshot(), 50.0)
    assert change.decisions and list(model.decisions) == ["e1"]
    assert model.decisions["e1"].opened_at is None, "it was open before this client attached"
    assert (model.model_name, model.effort, model.project) == ("opus", "high", "myproject")
    session.record("escalation.resolved", {"escalation_id": "e1", "decision": "allow"})
    model.load_snapshot(session.snapshot(), 60.0)
    assert not model.decisions and steps(model)[0].verdict == "RESOLVED"


def test_replayed_events_get_no_times() -> None:
    model = SessionModel()
    feed(model, scenarios.escalation_scenario(), start=None)
    assert all(step.duration is None for step in steps(model))
    assert model.decisions["e1"].opened_at is None


def test_usage_limits_and_the_context_fill_reach_the_model() -> None:
    model = SessionModel()
    limit = {"type": "five_hour", "status": "allowed_warning", "utilization": 0.82}
    assert model.apply("usage.update", {"rate_limits": [limit]}, 1.0).status
    assert model.usage.limits[0].utilization == pytest.approx(0.82)
    reached = model.apply("usage.update", {"rate_limits": [{**limit, "status": "rejected"}]}, 2.0)
    assert reached.notices and reached.notices[0].severity == "error"
    context = {"tokens": 10, "max_tokens": 100, "percent": 10.0, "model": "m"}
    model.set_usage({"context": context, "rate_limits": [], "cost_usd": 1.5})
    assert model.usage.context is not None and model.usage.context.percent == pytest.approx(10.0)
    assert model.usage.limits == [] and model.usage.cost == pytest.approx(1.5)


def test_side_questions_are_kept_beside_the_conversation() -> None:
    model = SessionModel()
    exchange, change = model.ask_side("what now?")
    assert change.side and exchange.pending and model.blocks == []
    assert model.side_history() == [], "an unanswered question is no history"
    exchange.answer, exchange.pending = "this", False
    assert model.side_history() == [{"question": "what now?", "answer": "this"}]
    model.reset()
    assert model.side == [exchange], "a rebuild of the session data keeps them"


def test_an_event_that_does_not_fit_the_protocol_becomes_a_log_line() -> None:
    model = SessionModel()
    change = model.apply("tool.use", {"name": 3}, 1.0)
    assert change.notices and model.blocks == [] and "tool.use" in model.logs[-1].message
    assert not model.apply("no.such.event", {}, 1.0)
    assert "not known" in model.logs[-1].message


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_every_prefix_of_a_scenario_applies_without_an_error(name: str) -> None:
    items = scenarios.SCENARIOS[name]()
    if name == "long_session":
        items = scenarios.long_session(40)
    model = SessionModel()
    for index, item in enumerate(items):
        if item.method:
            model.apply(item.method, item.params, float(index))
        assert all(block.id == position for position, block in enumerate(model.blocks))
        assert all(
            step.waiting == "open"
            for step in steps(model, ALL)
            if step.escalation_id in model.decisions
        )


def test_the_client_can_add_a_line_of_its_own() -> None:
    model = SessionModel()
    change = model.note("above: stored history")
    assert change.blocks_added == [0] and isinstance(model.blocks[0], JoinBlock)


def test_a_session_with_the_new_requests_offers_them_and_an_older_one_says_why_not() -> None:
    session = FakeSession()
    new = capabilities(session.snapshot())
    assert new.has("side_question") and new.has("usage") and new.has("live_model_change")
    assert new.reason("side_question") is None
    assert not new.has("queue_withdraw") and not new.has("stop_subagent")
    old_snapshot = {k: v for k, v in session.snapshot().items() if k != "rate_limits"}
    old = capabilities(old_snapshot)
    assert not old.has("side_question")
    assert all(entry.reason.startswith(NOT_AVAILABLE) for entry in old.missing())
    assert len(old.missing()) == len(CAPABILITIES)
