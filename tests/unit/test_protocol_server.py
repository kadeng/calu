"""The protocol server and client over a real WebSocket: tokens, requests, events, replay."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.bus import Bus
from calu.claude import FakeRunner, FakeScript
from calu.config import Config
from calu.protocol.client import ProtocolClient, RequestFailed, connect
from calu.protocol.server import SessionServer
from calu.session import Session
from calu.store import MemorySessionStore
from calu.testing import fake_models
from calu.tokens import new_token, token_hash
from tests.unit.helpers import default_settings

pytestmark = pytest.mark.anyio


@dataclass
class Events:
    """Collects the `(method, params)` pairs a client received."""

    received: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        """Record one event.

        Args:
            method: The notification method.
            params: Its params.
        """
        self.received.append((method, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        """Return the params of the events of one method.

        Args:
            method: The notification method.
        """
        return [params for name, params in self.received if name == method]

    async def wait_for(self, method: str, count: int = 1, timeout: float = 10) -> list[dict]:
        """Wait until `count` events of `method` arrived.

        Args:
            method: The notification method.
            count: How many are awaited.
            timeout: Seconds before the wait fails.
        """
        with anyio.fail_after(timeout):
            while len(self.of(method)) < count:
                await anyio.sleep(0.01)
        return self.of(method)


@dataclass
class Served:
    """A running session with its protocol server, for the tests below."""

    server: SessionServer
    session: Session
    store: MemorySessionStore
    token: str

    @property
    def address(self) -> str:
        """The WebSocket address of the server."""
        return self.server.address


@asynccontextmanager
async def served(
    workspace: Path,
    turns: Sequence[Sequence[dict[str, Any]]] = (),
    config: Config | None = None,
) -> AsyncGenerator[Served]:
    """Run a session with a protocol server on a free local port."""
    store = MemorySessionStore()
    bus = Bus()
    script = FakeScript(turns=[list(t) for t in turns])
    session = Session(
        workspace,
        default_settings(workspace, config),
        store,
        lambda: FakeRunner(script, workspace),
        bus.publish,
        checker_models=fake_models(),
    )
    token = new_token()
    async with anyio.create_task_group() as tg:
        server = SessionServer(session, bus, token_hash(token), tg)
        tg.start_soon(server.listen, "127.0.0.1", 0)
        with anyio.fail_after(10):
            await server.ready.wait()
        try:
            yield Served(server=server, session=session, store=store, token=token)
        finally:
            await session.stop()
            tg.cancel_scope.cancel()


@asynccontextmanager
async def client(
    run: Served, events: Events, name: str = "test", after_seq: int = 0
) -> AsyncGenerator[ProtocolClient]:
    """Connect a client to a running session."""
    async with connect(run.address, run.token, name, events, after_seq=after_seq) as protocol:
        yield protocol


async def test_a_client_without_a_valid_token_gets_nowhere(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        with pytest.raises(RequestFailed) as bad:
            async with connect(run.address, "wrong-token", "test", events):
                pass
        assert bad.value.code == -32001
        # A request before `initialize` is refused as well.
        from websockets.asyncio.client import connect as ws_connect

        async with ws_connect(run.address) as connection:
            protocol = ProtocolClient(connection, events)
            async with anyio.create_task_group() as tg:
                tg.start_soon(protocol._read)
                with pytest.raises(RequestFailed) as early:
                    await protocol.request("chat.send", {"text": "hi"})
                assert early.value.code == -32001
                tg.cancel_scope.cancel()


async def test_a_client_starts_a_session_and_receives_its_events(tmp_path: Path):
    turns = [
        [
            {"text": "Looking."},
            {"tool": "Bash", "input": {"command": "ls"}, "result": "a.py"},
            {"text": "Done."},
        ]
    ]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            assert protocol.initialize["workspace"] == str(tmp_path)
            assert protocol.initialize["protocol_version"] == 1
            snapshot = await protocol.request("state.get")
            assert snapshot["state"] == "stopped" and snapshot["escalations"] == []
            started = await protocol.request("session.start", {})
            assert started["claude_session_id"] is None  # known after the first turn
            await protocol.request("chat.send", {"text": "please look"})
            await events.wait_for("turn.result")
            texts = [p["text"] for p in events.of("assistant.text")]
            assert texts == ["Looking.", "Done."]
            checks = events.of("check.result")
            assert checks and checks[0]["tool_name"] == "Bash"
            # Every event carries an increasing sequence number.
            seqs = [params["seq"] for _, params in events.received]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            assert protocol.last_seq == max(seqs)
            snapshot = await protocol.request("state.get")
            # "checking" while the narrator still retells the last answer in the background.
            assert snapshot["state"] in ("idle", "checking")
            assert snapshot["claude_session_id"] == "fake-session"
            context = await protocol.request("context.get")
            assert "please look" in context["recent"]
            await protocol.request("session.stop")


async def test_two_clients_see_the_same_events_and_either_may_decide(tmp_path: Path):
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with served(tmp_path, turns) as run:
        watcher, operator = Events(), Events()
        async with client(run, watcher, "watcher") as first:
            async with client(run, operator, "operator") as second:
                await first.request("session.start", {})
                await first.request("chat.send", {"text": "ship it"})
                request = (await operator.wait_for("escalation.request"))[0]
                assert "calu:escalate" in request["reason"] or "fake" in request["reason"]
                # The second client sees the open escalation in its snapshot as well.
                snapshot = await second.request("state.get")
                assert [e["escalation_id"] for e in snapshot["escalations"]] == [
                    request["escalation_id"]
                ]
                await second.request(
                    "escalation.decide",
                    {
                        "escalation_id": request["escalation_id"],
                        "decision": "allow",
                        "note": "go ahead",
                    },
                )
                await operator.wait_for("turn.result")
                await watcher.wait_for("turn.result")
                assert [p["content"] for p in watcher.of("tool.result")] == ["ok"]
                assert watcher.of("escalation.resolved")[0]["decision"] == "allow"


async def test_a_client_that_reconnects_gets_the_events_it_missed(tmp_path: Path):
    async with served(tmp_path, [[{"text": "first answer"}]]) as run:
        first = Events()
        async with client(run, first) as protocol:
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "hello"})
            await first.wait_for("turn.result")
            seen = protocol.last_seq
        second = Events()
        async with client(run, second, after_seq=seen) as protocol:
            assert protocol.complete
            assert second.received == []  # nothing happened while nobody was connected
            await protocol.request("chat.send", {"text": "again"})
            await second.wait_for("turn.result")
            assert [p["text"] for p in second.of("user.input")] == ["again"]
        third = Events()
        async with client(run, third, after_seq=0) as protocol:
            # A client that asks for everything gets the whole buffer, in order.
            assert [p["text"] for p in third.of("user.input")] == ["hello", "again"]


async def test_unknown_methods_and_invalid_params_are_reported(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            with pytest.raises(RequestFailed) as unknown:
                await protocol.request("does.not.exist")
            assert unknown.value.code == -32601
            with pytest.raises(RequestFailed) as invalid:
                await protocol.request("chat.send", {"text": ""})
            assert invalid.value.code == -32602
            with pytest.raises(RequestFailed) as failed:
                await protocol.request("chat.send", {"text": "no session runs"})
            assert failed.value.code == -32000


async def test_shutdown_ends_the_server(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            assert await protocol.request("shutdown") == {}
        with anyio.fail_after(5):
            await run.server.shutdown.wait()


async def test_the_session_list_comes_from_the_store(tmp_path: Path):
    from datetime import UTC, datetime

    from calu.protocol.messages import SessionSummary

    async with served(tmp_path) as run:
        run.store.stored_sessions = [
            SessionSummary(
                calu_session_id="s1",
                claude_session_id="claude-1",
                state="stopped",
                directory=str(tmp_path),
                started_at=datetime.now(UTC),
                first_input="the earlier request",
            )
        ]
        events = Events()
        async with client(run, events) as protocol:
            result = await protocol.request("session.list", {"limit": 5})
            assert [s["claude_session_id"] for s in result["sessions"]] == ["claude-1"]
            assert result["sessions"][0]["first_input"] == "the earlier request"


async def test_a_client_reads_the_recorded_conversation(tmp_path: Path):
    from calu.context.turns import StepRecord, TurnRecord
    from calu.protocol.messages import SessionHistoryResult, StoredStep

    async with served(tmp_path) as run:
        run.store.history = SessionHistoryResult(
            calu_session_id="memory-session",
            claude_session_id="claude-old",
            chat_summary="# Chat summary\n\nEarlier work.\n",
            turns=[
                TurnRecord(
                    index=1,
                    user_inputs=["the earlier request"],
                    steps=[StepRecord(tool_name="Bash", decision="pass", summary="Listed.")],
                    answer="done",
                )
            ],
            steps=[
                StoredStep(
                    seq=1,
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    decision="pass",
                    summary="Listed.",
                    result_summary="a.py",
                )
            ],
        )
        events = Events()
        async with client(run, events) as protocol:
            history = await protocol.request("session.history", {"claude_session_id": "claude-old"})
            assert history["claude_session_id"] == "claude-old"
            assert "Earlier work." in history["chat_summary"]
            assert history["turns"][0]["user_inputs"] == ["the earlier request"]
            assert history["steps"][0]["tool_input"] == {"command": "ls"}
            assert history["steps"][0]["result_summary"] == "a.py"


async def test_the_history_of_the_running_session_needs_no_id(tmp_path: Path):
    async with served(tmp_path, [[{"text": "hello"}]]) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "say hello"})
            await events.wait_for("turn.result")
            history = await protocol.request("session.history", {})
            # The in-memory store builds it from the turns it stored.
            assert [t["user_inputs"] for t in history["turns"]] == [["say hello"]]


async def test_a_client_inspects_what_the_checker_saw_for_an_escalation(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    turns = [
        [
            {
                "tool": "Edit",
                "input": {
                    "file_path": "a.txt",
                    "old_string": "old",
                    "new_string": "new # calu:escalate",
                },
                "result": "ok",
            }
        ]
    ]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            await protocol.request("chat.send", {"text": "change it"})
            request = (await events.wait_for("escalation.request"))[0]
            found = await protocol.request(
                "escalation.context", {"escalation_id": request["escalation_id"]}
            )
            assert found["state"] == "open"
            assert found["tool_name"] == "Edit"
            assert found["rendered"] == request["rendered"]
            assert found["files"] == {"a.txt": "old\n"}  # before the change
            assert "change it" in found["recent"]
            assert "No turns have been folded" in found["chat_summary"]
            with pytest.raises(RequestFailed):
                await protocol.request("escalation.context", {"escalation_id": "999999"})
            with pytest.raises(RequestFailed):
                await protocol.request("escalation.context", {"escalation_id": "not-a-number"})
            await protocol.request(
                "escalation.decide",
                {"escalation_id": request["escalation_id"], "decision": "deny", "note": "no"},
            )
            await events.wait_for("turn.result")


async def test_a_client_reads_the_checker_definitions(tmp_path: Path):
    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            result = await protocol.request("checkers.get")
            names = [c["name"] for c in result["checkers"]]
            assert names == ["command", "edit", "narrator", "subagent-result"]
            command = result["checkers"][0]
            assert command["route"] == "command" and command["version"] == 1
            assert command["instructions"].startswith("#")
