"""The requests `side.ask`, `usage.get` and `model.set` and the event `usage.update`.

All through a real WebSocket and the scripted fake runner (`tests/unit/test_protocol_server`).
"""

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from calu.claude import FakeRunner
from calu.protocol.client import RequestFailed
from tests.unit.test_protocol_server import Events, client, served

pytestmark = pytest.mark.anyio


async def test_a_side_question_is_answered_while_a_turn_runs_and_changes_nothing(tmp_path: Path):
    turns = [[{"text": "Working."}, {"sleep": 0.6}, {"text": "Done."}]]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "please work"})
            await events.wait_for("assistant.text")
            assert not events.of("turn.result"), "the turn has to be running for this test"
            seen = len(events.received)
            state = (await protocol.request("state.get"))["state"]
            answer = await protocol.request(
                "side.ask",
                {
                    "question": "what are you doing?",
                    "history": [{"question": "earlier", "answer": "that"}],
                },
            )
            assert answer == {"answer": "Side answer to: what are you doing?"}
            runner = run.session.runner
            assert isinstance(runner, FakeRunner)
            assert runner.side_questions == [("what are you doing?", [("earlier", "that")])]
            # Nothing of the session changed: no event, the same state, an empty queue.
            assert not events.of("turn.result")
            between = [method for method, _ in events.received[seen:]]
            assert "user.input" not in between and "chat.queue" not in between
            assert (await protocol.request("state.get"))["state"] == state
            await events.wait_for("turn.result")
            texts = [params["text"] for params in events.of("assistant.text")]
            assert texts == ["Working.", "Done."]
            assert [params["text"] for params in events.of("user.input")] == ["please work"]
            history = await protocol.request("session.history", {})
            assert "what are you doing?" not in str(history)


async def test_a_side_question_needs_a_running_session_and_reports_a_failure(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            with pytest.raises(RequestFailed) as stopped:
                await protocol.request("side.ask", {"question": "anyone there?"})
            assert "no Claude session is running" in str(stopped.value)
            await protocol.request("session.start", {})
            runner = run.session.runner
            assert isinstance(runner, FakeRunner)
            runner.side_error = "Claude Code returned no answer to the side question"
            with pytest.raises(RequestFailed) as failed:
                await protocol.request("side.ask", {"question": "and now?"})
            assert "returned no answer" in str(failed.value)
            with pytest.raises(RequestFailed) as empty:
                await protocol.request("side.ask", {"question": ""})
            assert empty.value.code == -32602


async def test_a_side_question_does_not_hold_up_the_clients_other_requests(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            runner = run.session.runner
            assert isinstance(runner, FakeRunner)
            release = anyio.Event()
            asked = anyio.Event()

            async def slow(question: str, history: object = ()) -> str:
                """Answer only when the test says so."""
                asked.set()
                await release.wait()
                return "late answer"

            runner.side_question = slow  # type: ignore[method-assign]
            answers: list[dict] = []

            async def ask() -> None:
                """Ask the side question and keep the answer."""
                answers.append(await protocol.request("side.ask", {"question": "slow?"}))

            async with anyio.create_task_group() as tg:
                tg.start_soon(ask)
                with anyio.fail_after(5):
                    await asked.wait()
                    await protocol.request("chat.send", {"text": "hello"})
                    await events.wait_for("turn.result")
                assert answers == []
                release.set()
            assert answers == [{"answer": "late answer"}]


async def test_usage_reports_context_limits_cost_and_turns(tmp_path: Path):
    limit = {"type": "five_hour", "status": "allowed_warning", "utilization": 0.82}
    turns = [[{"rate_limit": limit}, {"text": "Done."}]]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            before = await protocol.request("usage.get")
            assert before["context"] is None and before["rate_limits"] == []
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "go"})
            await events.wait_for("turn.result")
            update = events.of("usage.update")[0]
            assert update["rate_limits"][0]["utilization"] == pytest.approx(0.82)
            usage = await protocol.request("usage.get")
            assert usage["context"] == {
                "tokens": 42_000,
                "max_tokens": 200_000,
                "percent": 21.0,
                "model": "fake-model",
            }
            assert usage["rate_limits"][0]["type"] == "five_hour"
            assert usage["rate_limits"][0]["status"] == "allowed_warning"
            assert usage["num_turns"] == events.of("turn.result")[0]["num_turns"]
            snapshot = await protocol.request("state.get")
            assert snapshot["rate_limits"][0]["type"] == "five_hour"


async def test_a_usage_report_does_not_make_an_idle_session_look_busy(tmp_path: Path):
    from calu.protocol.messages import RateLimit, UsageUpdate

    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            assert (await protocol.request("state.get"))["state"] == "idle"
            await run.session._on_runner_event(
                UsageUpdate(rate_limits=[RateLimit(type="seven_day", utilization=0.1)])
            )
            await events.wait_for("usage.update")
            assert (await protocol.request("state.get"))["state"] == "idle"


async def test_the_model_of_a_running_session_can_be_changed(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            with pytest.raises(RequestFailed) as stopped:
                await protocol.request("model.set", {"model": "opus"})
            assert "no Claude session is running" in str(stopped.value)
            await protocol.request("session.start", {})
            await protocol.request("model.set", {"model": "opus"})
            runner = run.session.runner
            assert isinstance(runner, FakeRunner)
            assert runner.model == "opus" and runner.model_changes == 1
            assert (await protocol.request("state.get"))["model"] == "opus"
            logs = await events.wait_for("log")
            assert any("model changed to opus" in line["message"] for line in logs)
            await protocol.request("model.set", {})
            assert runner.model is None and runner.model_changes == 2
            assert (await protocol.request("state.get"))["model"] is None


async def test_an_escalation_says_when_it_began_to_wait_also_in_the_snapshot(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    edit = {"file_path": "a.txt", "old_string": "old", "new_string": "new # calu:escalate"}
    turns = [[{"tool": "Edit", "input": edit, "result": "ok"}]]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            before = datetime.now(UTC)
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "say hi"})
            request = (await events.wait_for("escalation.request"))[0]
            opened = datetime.fromisoformat(request["opened_at"])
            assert before <= opened <= datetime.now(UTC)
            listed = (await protocol.request("state.get"))["escalations"]
            assert [entry["opened_at"] for entry in listed] == [request["opened_at"]]
            await protocol.request(
                "escalation.decide", {"escalation_id": request["escalation_id"], "decision": "deny"}
            )
            await events.wait_for("turn.result")
