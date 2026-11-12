"""What the full-screen client asks of the session: `calu.tui.actions.Actions`.

Against the scripted session of `calu.tui.fake`, without an app: the exact request of each
intention, and how a refusal reaches the user.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from calu.attach import HELP
from calu.protocol.answers import compose_answer
from calu.protocol.messages import CLOSE_FELL_BEHIND, CLOSE_TOKEN_REVOKED
from calu.tui import scenarios
from calu.tui.actions import NOT_ATTACHED, Actions
from calu.tui.capabilities import Capabilities
from calu.tui.commands import COMMANDS, by_slash, completions, slash_names
from calu.tui.connection import Link
from calu.tui.fake import FakeSession, fake_connector
from calu.tui.model import Change, SessionModel, StepBlock

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


@asynccontextmanager
async def attached(
    session: FakeSession, notify: Callable[[Change], None] | None = None
) -> AsyncGenerator[tuple[Actions, SessionModel, Link]]:
    """Attach a model to a scripted session and yield the actions for it."""
    model = SessionModel()
    link = Link(fake_connector(session), model, notify or (lambda change: None), lambda: 10.0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(link.run)
        with anyio.fail_after(5):
            while model.connection.status != "attached":
                await anyio.sleep(0.01)
        try:
            yield Actions(link, model), model, link
        finally:
            link.detach()


def waiting_session() -> FakeSession:
    """Return a session whose history ends with one open escalation and two queued messages."""
    session = FakeSession()
    scenarios.preload(session, scenarios.escalation_scenario())
    return session


async def test_attaching_replays_the_history_and_takes_the_snapshot() -> None:
    session = waiting_session()
    async with attached(session) as (_, model, link):
        assert list(model.decisions) == ["e1"] and model.state == "escalated"
        assert [entry.text for entry in model.queue] == [
            "also bump the version",
            "then write the changelog entry",
        ]
        assert model.model_name == "opus" and link.capabilities.has("side_question")
        assert session.sent("state.get") == [{}]
        steps = [block for block in model.blocks if isinstance(block, StepBlock)]
        assert all(step.duration is None for step in steps), "replayed events carry no time"
    assert model.connection.status == "detached"


async def test_each_send_mode_is_one_chat_send_with_its_delivery_and_target() -> None:
    session = FakeSession()
    scenarios.preload(session, scenarios.two_subagents())
    async with attached(session) as (actions, model, _):
        assert (await actions.send("next please")).ok
        assert (await actions.send("now", delivery="inject")).ok
        assert (await actions.send("look at docs/ too", target="a1", delivery="queued")).ok
        assert session.sent("chat.send") == [
            {"text": "next please", "delivery": "queued", "to": "main"},
            {"text": "now", "delivery": "inject", "to": "main"},
            {"text": "look at docs/ too", "delivery": "inject", "to": "a1"},
        ]
        assert [entry.to for entry in model.queue] == ["main", "main", "a1"]
        refused = await actions.send("hello", target="a2")
        assert not refused.ok and "not a running background subagent" in refused.message
        assert not (await actions.send("   ")).ok and len(session.sent("chat.send")) == 4


async def test_allow_and_deny_send_the_decision_with_the_note() -> None:
    session = waiting_session()
    async with attached(session) as (actions, model, _):
        outcome = await actions.decide("e1", "deny", "do not publish yet")
        assert outcome.ok and not model.decisions
        assert session.sent("escalation.decide") == [
            {"escalation_id": "e1", "decision": "deny", "note": "do not publish yet"}
        ]
        step = model.step("t5")
        assert step is not None and step.verdict == "DENIED" and step.note == "do not publish yet"


async def test_a_refused_decision_stays_open_and_carries_the_sessions_message() -> None:
    session = waiting_session()
    session.fail["escalation.decide"] = "the escalation is being decided by another client"
    async with attached(session) as (actions, model, _):
        outcome = await actions.decide("e1", "allow")
        assert not outcome.ok
        decision = model.decisions["e1"]
        assert decision.error == "the escalation is being decided by another client"
        assert not decision.own


async def test_an_answer_is_composed_from_the_choices_in_the_shape_every_client_sends() -> None:
    session = FakeSession()
    scenarios.preload(session, scenarios.question_multi())
    async with attached(session) as (actions, model, _):
        decision = model.decisions["e1"]
        assert not (await actions.answer("e1")).ok, "nothing chosen yet"
        decision.picks[0] = {0}
        decision.picks[1] = {1, 2}
        decision.free[2] = "next to each test, but named fixtures_*.py"
        expected = compose_answer(
            scenarios.SETUP_QUESTIONS,
            [["pytest"], ["type check", "lint"], ["next to each test, but named fixtures_*.py"]],
        )
        assert (await actions.answer("e1")).ok
        assert session.sent("escalation.decide") == [{"escalation_id": "e1", "answer": expected}]
        step = model.step("t1")
        assert step is not None and step.verdict == "ANSWERED" and step.note == expected
        assert expected.splitlines()[1] == "Checks: type check, lint"


async def test_a_single_question_is_answered_with_the_bare_text() -> None:
    assert compose_answer(scenarios.RUNNER_QUESTIONS, [["pytest"]]) == "pytest"
    assert compose_answer(scenarios.RUNNER_QUESTIONS, [[]]) == ""


async def test_a_side_question_is_asked_beside_the_conversation_with_its_history() -> None:
    session = waiting_session()
    session.side_answers["What waits?"] = "A force push."
    async with attached(session) as (actions, model, _):
        before = len(session.events)
        outcome = await actions.ask_side("What waits?")
        assert outcome.ok and outcome.data == "A force push."
        assert (model.side[0].answer, model.side[0].pending) == ("A force push.", False)
        await actions.ask_side("And why?")
        assert session.sent("side.ask") == [
            {"question": "What waits?", "history": []},
            {
                "question": "And why?",
                "history": [{"question": "What waits?", "answer": "A force push."}],
            },
        ]
        assert len(session.events) == before, "no event: the conversation did not change"
        assert not session.sent("chat.send") and list(model.decisions) == ["e1"]
        session.fail["side.ask"] = "Claude Code returned no answer to the side question"
        failed = await actions.ask_side("Still there?")
        assert not failed.ok and model.side[-1].error.startswith("Claude Code returned no answer")


async def test_usage_is_fetched_into_the_model() -> None:
    session = waiting_session()
    session.rate_limits = [{"type": "seven_day", "status": "allowed", "utilization": 0.31}]
    async with attached(session) as (actions, model, _):
        assert model.usage.limits[0].type == "seven_day", "already in the snapshot"
        assert model.usage.context is None
        assert (await actions.refresh_usage()).ok
        assert model.usage.context is not None and model.usage.context.model == "claude-opus-5"


async def test_the_model_changes_in_a_running_session_and_starts_a_stopped_one() -> None:
    session = waiting_session()
    async with attached(session) as (actions, model, _):
        outcome = await actions.set_model("sonnet")
        assert outcome.ok and "next step" in outcome.message
        assert session.sent("model.set") == [{"model": "sonnet"}] and model.model_name == "sonnet"
        await actions.set_model("default")
        assert session.sent("model.set")[-1] == {"model": None}
        await actions.stop()
        await actions.set_model("haiku")
        assert session.sent("session.start") == [{"resume_id": "5f0c2a9e-claude", "model": "haiku"}]


async def test_level_interrupt_stop_and_start_are_single_requests() -> None:
    session = waiting_session()
    async with attached(session) as (actions, model, _):
        assert (await actions.set_level("edits")).ok
        assert session.sent("auto_escalate.set")[0]["edits"] is True
        assert model.level() == "Confirm edits"
        assert not (await actions.set_level("nonsense")).ok
        assert (await actions.interrupt()).ok
        assert not model.decisions and model.state == "idle"
        assert (await actions.stop()).ok and model.state == "stopped"
        assert (await actions.start("abc", effort="high")).ok
        assert session.sent("session.start") == [{"resume_id": "abc", "effort": "high"}]
        why = await actions.why("e1")
        assert why.ok and why.data["tool_name"] == "Bash"


async def test_a_file_comment_needs_the_project_directory_on_this_machine(tmp_path: Any) -> None:
    session = FakeSession(workspace=str(tmp_path / "missing"))
    async with attached(session) as (actions, _, _):
        outcome = await actions.comment("a.py", 1, 2, "why?")
        assert not outcome.ok and "on the session's machine" in outcome.message
    (tmp_path / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    session = FakeSession(workspace=str(tmp_path))
    async with attached(session) as (actions, _, _):
        assert (await actions.comment("a.py", 1, 2, "why?")).ok
        (sent,) = session.sent("comment.add")
        assert sent["after_text"] == "one\ntwo\n" and sent["kind"] == "file"


async def test_without_a_connection_nothing_is_sent() -> None:
    session = FakeSession()
    model = SessionModel()
    link = Link(fake_connector(session), model, lambda change: None, lambda: 0.0)
    outcome = await Actions(link, model).send("hello")
    assert not outcome.ok and outcome.message == NOT_ATTACHED and session.requests == []


async def test_a_client_that_fell_behind_reconnects_after_its_last_event() -> None:
    session = waiting_session()
    async with attached(session) as (_, model, _):
        seen = session.seq
        session.drop(CLOSE_FELL_BEHIND, "fell behind")
        session.record("assistant.text", {"text": "written while the client was away"})
        with anyio.fail_after(5):
            while session.connects < 2 or model.connection.status != "attached":
                await anyio.sleep(0.01)
        assert session.seq == seen + 1
        assert model.blocks[-1].text == "written while the client was away"  # type: ignore[attr-defined]
        assert len(model.decisions) == 1, "the model was continued, not rebuilt"


async def test_an_incomplete_replay_rebuilds_the_model_from_the_snapshot() -> None:
    session = waiting_session()
    async with attached(session) as (_, model, _):
        blocks_before = len(model.blocks)
        session.drop(CLOSE_FELL_BEHIND, "fell behind")
        session.record("assistant.text", {"text": "lost"})
        session.record("assistant.text", {"text": "kept"})
        session.buffer_start = session.seq - 1
        with anyio.fail_after(5):
            while session.connects < 2 or model.connection.status != "attached":
                await anyio.sleep(0.01)
        texts = [getattr(block, "text", "") for block in model.blocks]
        assert len(model.blocks) < blocks_before
        assert "rebuilt from the session's current state" in texts[0]
        assert texts[1] == "kept", "what the session could still replay follows the note"
        assert list(model.decisions) == ["e1"], "the open escalation comes from the snapshot"


async def test_a_revoked_token_ends_the_link_with_the_reason() -> None:
    session = waiting_session()
    changes: list[Change] = []
    async with attached(session, changes.append) as (_, model, link):
        session.drop(CLOSE_TOKEN_REVOKED, "the client token was revoked")
        with anyio.fail_after(5):
            while model.connection.status != "detached":
                await anyio.sleep(0.01)
        assert link.closed_reason == "the client token was revoked" and session.connects == 1
        notices = [notice.text for change in changes for notice in change.notices]
        assert any("the client token was revoked" in text for text in notices)


def test_every_command_of_the_line_client_exists_in_the_full_screen_client() -> None:
    words = {word.split()[0] for word in HELP.replace("(or /exit)", "/exit").split("  ") if word}
    words = {word for word in words if word.startswith("/")}
    assert len(words) >= 18
    missing = sorted(word for word in words if by_slash(word) is None)
    assert missing == [], f"the line client's {missing} have no entry in COMMANDS"


def test_the_command_table_is_consistent_and_says_why_something_is_unavailable() -> None:
    ids = [command.id for command in COMMANDS]
    assert len(ids) == len(set(ids))
    names = [command.name for command in COMMANDS if command.name]
    assert len(names) == len(set(names)) and all(name.startswith("/") for name in names)
    assert completions("/a") == ["/allow", "/answer", "/agent"]
    assert "/btw" in slash_names() and by_slash("/exit") is by_slash("/quit")
    none = Capabilities()
    side = next(command for command in COMMANDS if command.id == "side")
    assert "not available from this session" in str(side.unavailable(none))
    assert side.unavailable(Capabilities(frozenset({"side_question"}))) is None
    later = [command for command in COMMANDS if command.later]
    assert later and all(command.unavailable(none) == command.later for command in later)
    withdraw = next(command for command in COMMANDS if command.id == "withdraw")
    assert "cannot be withdrawn" in str(withdraw.unavailable(Capabilities(frozenset({"usage"}))))
