"""The protocol server and client over a real WebSocket: tokens, requests, events, replay."""

import stat
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
    store: MemorySessionStore | None = None,
) -> AsyncGenerator[Served]:
    """Run a session with a protocol server on a free local port."""
    store = store or MemorySessionStore()
    bus = Bus()
    script = FakeScript(turns=[list(t) for t in turns])
    session = Session(
        workspace,
        default_settings(workspace, config),
        store,
        lambda: FakeRunner(script, workspace),
        bus.publish,
        checker_models=fake_models(),
        snapshot_root=workspace.parent / f"{workspace.name}-snapshots",
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
            # Known right after the start, so a session that ends before its first turn is
            # still resumable (D27).
            assert started["claude_session_id"] == "fake-session"
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
            # The session settles into `idle` after the turn result, so waiting for the turn
            # alone can leave that last event outside `last_seq`, and the reconnecting client
            # would then receive it. Wait until the session is idle and this client has seen
            # every event the bus has.
            with anyio.fail_after(10):
                while True:
                    state = await protocol.request("state.get")
                    if state["state"] == "idle" and state["last_seq"] == protocol.last_seq:
                        break
                    await anyio.sleep(0.01)
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
            assert names == ["changeset", "command", "edit", "narrator", "subagent-result"]
            command = next(c for c in result["checkers"] if c["name"] == "command")
            assert command["route"] == "command" and command["version"] == 1
            assert command["instructions"].startswith("#")


async def test_a_minted_token_of_the_session_is_accepted_too(tmp_path: Path):
    """D30: any non-revoked token of the session is accepted, not only the printed one."""
    async with served(tmp_path) as run:
        events = Events()
        # An operator minted this one into `client_tokens` (here: the in-memory store).
        await run.store.add_client_token(token_hash("minted-for-zed"), run.address)
        async with connect(run.address, "minted-for-zed", "zed", events) as protocol:
            snapshot = await protocol.request("state.get")
            assert snapshot["workspace"] == str(tmp_path)
        # A token that belongs to no row is still refused.
        with pytest.raises(RequestFailed) as refused:
            async with connect(run.address, "guessed", "intruder", events):
                pass
        assert refused.value.code == -32001


async def test_a_session_can_serve_a_unix_socket_instead_of_a_port(tmp_path: Path):
    """§6: the file system decides who may connect, not every local process via a port."""
    store = MemorySessionStore()
    bus = Bus()
    session = Session(
        tmp_path,
        default_settings(tmp_path),
        store,
        lambda: FakeRunner(FakeScript(turns=[[{"text": "hello"}]]), tmp_path),
        bus.publish,
        checker_models=fake_models(),
        snapshot_root=tmp_path.parent / f"{tmp_path.name}-snapshots",
    )
    token = new_token()
    socket_path = tmp_path / "run" / "session.sock"
    async with anyio.create_task_group() as tg:
        server = SessionServer(session, bus, token_hash(token), tg)
        tg.start_soon(server.listen, "127.0.0.1", 0, socket_path)
        with anyio.fail_after(10):
            await server.ready.wait()
        try:
            # The recorded address names the socket, which is what `sessions.address` keeps (D29).
            assert server.address == f"unix:{socket_path}"
            assert socket_path.is_socket()
            # Only this user can reach it: 0700 on the directory, 0600 on the socket.
            assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600

            events = Events()
            async with connect(server.address, token, "over the socket", events) as protocol:
                snapshot = await protocol.request("state.get")
                assert snapshot["workspace"] == str(tmp_path)

            # The token is still required, socket or not.
            with pytest.raises(RequestFailed) as refused:
                async with connect(server.address, "wrong", "intruder", events):
                    pass
            assert refused.value.code == -32001
        finally:
            await session.stop()
            tg.cancel_scope.cancel()
    # The socket file goes with the server, so a later run does not trip over it.
    assert not socket_path.exists()


async def test_a_stale_socket_file_is_replaced(tmp_path: Path):
    """A process that died leaves its socket behind; binding must not fail because of it."""
    socket_path = tmp_path / "run" / "stale.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.write_text("not a socket any more", encoding="utf-8")
    async with served(tmp_path) as run:
        async with anyio.create_task_group() as tg:
            server = SessionServer(run.session, Bus(), token_hash(new_token()), tg)
            tg.start_soon(server.listen, "127.0.0.1", 0, socket_path)
            with anyio.fail_after(10):
                await server.ready.wait()
            assert socket_path.is_socket()
            tg.cancel_scope.cancel()


async def test_setting_the_auto_escalation_categories_tells_every_client(tmp_path: Path):
    """`auto_escalate.set` changes the session and announces it to the other clients.

    A client may offer the categories as a setting of its own -- the ACP adapter maps them to
    ACP's session modes -- so one client changing them must not leave another showing the old
    state (PLAN.md D42).
    """
    async with served(tmp_path) as run:
        setter_events, watcher_events = Events(), Events()
        async with client(run, setter_events, "setter") as setter:
            async with client(run, watcher_events, "watcher") as watcher:
                assert run.session.config.auto_escalate.commands is False
                await setter.request(
                    "auto_escalate.set", {"commands": True, "edits": False, "subagents": True}
                )
                announced = (await watcher_events.wait_for("auto_escalate.changed"))[0]
                assert announced["settings"] == {
                    "commands": True,
                    "edits": False,
                    "subagents": True,
                }
                # The running session really uses them, and the setter hears it too.
                assert run.session.config.auto_escalate.commands is True
                assert run.session.config.auto_escalate.subagents is True
                assert (await setter_events.wait_for("auto_escalate.changed"))[0]["settings"] == (
                    announced["settings"]
                )
                # A client that joins afterwards reads them from the snapshot.
                snapshot = await watcher.request("state.get")
                assert snapshot["auto_escalate"] == announced["settings"]


async def test_the_history_holds_the_turn_that_just_ended(tmp_path: Path):
    """A client reading the history right after `turn.result` sees that turn.

    The turn is narrated and stored in a background task while the event is already out, so
    without a wait this is a race -- and it was one: the test above failed intermittently.
    """
    turns = [[{"text": "first"}], [{"text": "second"}]]
    async with served(tmp_path, turns) as run:
        events = Events()
        async with client(run, events) as protocol:
            await protocol.request("session.start", {})
            for number, text in enumerate(("one", "two"), start=1):
                await protocol.request("chat.send", {"text": text})
                with anyio.fail_after(15):
                    while len(events.of("turn.result")) < number:
                        await anyio.sleep(0.005)
                history = await protocol.request("session.history", {})
                # Every turn that ended is in the history, including the one just finished.
                assert [t["user_inputs"] for t in history["turns"]] == [
                    [name] for name in ("one", "two")[:number]
                ]


# ------------------------------------------------------------------ revoked tokens (pc0)


class RowPerStart(MemorySessionStore):
    """An in-memory store that, like the database, makes a new session row for every start."""

    starts = 0

    async def start(self, identity: Any) -> str:
        await super().start(identity)
        self.starts += 1
        self.session_id = f"row-{self.starts}"
        return self.session_id


def revoke(store: MemorySessionStore, token: str) -> None:
    """What `calu token revoke` does to the database, for the in-memory store."""
    store.client_tokens[:] = [row for row in store.client_tokens if row[0] != token_hash(token)]


async def wait_closed(protocol: ProtocolClient) -> None:
    with anyio.fail_after(10):
        await protocol.closed.wait()


async def test_a_revoked_token_cannot_change_the_session_and_loses_its_connection(tmp_path: Path):
    """D30: revoking a token ends what its holder can do, also on a connection that is open."""
    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        await run.store.add_client_token(token_hash("minted-for-zed"), run.address)
        events = Events()
        async with client(run, events) as owner:
            await owner.request("session.start", {})
            async with connect(run.address, "minted-for-zed", "zed", Events()) as zed:
                await zed.request("auto_escalate.set", {"edits": True})  # works while valid
                revoke(run.store, "minted-for-zed")
                with pytest.raises(RequestFailed) as refused:
                    await zed.request("auto_escalate.set", {"edits": False})
                assert refused.value.code == -32001
                assert run.session.snapshot().auto_escalate.edits is True  # nothing changed
                await wait_closed(zed)
                assert zed.close_code == 4001
                assert not zed.fell_behind
            # The token is refused from now on, and the other client is untouched.
            with pytest.raises(RequestFailed):
                async with connect(run.address, "minted-for-zed", "zed", Events()):
                    pass
            assert (await owner.request("state.get"))["workspace"] == str(tmp_path)


async def test_the_scheduled_check_closes_a_connection_that_only_listens(tmp_path: Path):
    """A revoked client that sends nothing must not keep reading the session's events."""
    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        run.server.revalidate_s = 0.05
        await run.store.add_client_token(token_hash("minted"), run.address)
        async with client(run, Events()) as owner:
            await owner.request("session.start", {})
            async with connect(run.address, "minted", "watcher", Events()) as watcher:
                assert run.server.bus.clients == 2
                with anyio.fail_after(10):  # the first scheduled check confirms the token
                    while token_hash("minted") not in run.server._confirmed:
                        await anyio.sleep(0.01)
                revoke(run.store, "minted")
                await wait_closed(watcher)
                assert watcher.close_code == 4001
            with anyio.fail_after(5):
                while run.server.bus.clients != 1:
                    await anyio.sleep(0.01)


async def test_the_printed_token_can_be_revoked_like_any_other(tmp_path: Path):
    """The printed token has a row in `client_tokens`; revoking that row has to end its use."""
    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        await run.store.add_client_token(token_hash(run.token), run.address)  # as `calu run` does
        await run.store.add_client_token(token_hash("minted"), run.address)
        async with connect(run.address, "minted", "other", Events()) as other:
            await other.request("session.start", {})
            async with client(run, Events()) as printed:
                await printed.request("state.get")
                revoke(run.store, run.token)
                await run.server.sweep()
                await wait_closed(printed)
                assert printed.close_code == 4001
            with pytest.raises(RequestFailed) as refused:
                async with client(run, Events()):
                    pass
            assert refused.value.code == -32001
            # Refused from memory as well: an unreachable database must not bring it back.
            run.store.client_token_ok = failing_database  # type: ignore[method-assign]
            with pytest.raises(RequestFailed) as still_refused:
                async with client(run, Events()):
                    pass
            assert still_refused.value.code == -32001


async def failing_database(token_sha256: str) -> bool:
    import psycopg

    raise psycopg.OperationalError("the database is gone")


async def test_a_database_outage_keeps_the_printed_token_and_open_connections(tmp_path: Path):
    """D30: attaching with the printed token works while the database is unreachable, and an
    outage is not mistaken for a revocation."""
    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        await run.store.add_client_token(token_hash("minted"), run.address)
        async with connect(run.address, "minted", "zed", Events()) as zed:
            await zed.request("session.start", {})
            run.store.client_token_ok = failing_database  # type: ignore[method-assign]
            await run.server.sweep()
            await zed.request("auto_escalate.set", {"edits": True})  # still allowed
            assert not zed.closed.is_set()
            async with client(run, Events()) as printed:
                assert (await printed.request("state.get"))["workspace"] == str(tmp_path)
            # A minted token cannot be checked now: an error, but not "invalid token".
            with pytest.raises(RequestFailed) as unknown:
                async with connect(run.address, "minted", "second", Events()):
                    pass
            assert unknown.value.code == -32603


async def test_ending_the_session_does_not_cut_its_clients_and_the_printed_token_follows(
    tmp_path: Path,
):
    """A session revokes its own tokens when it ends (D108). That is not an operator's
    revocation: the clients stay, and may start the next session in the same process. The
    printed token gets a row for that next session, so that it stays revocable."""
    store = RowPerStart()
    async with served(tmp_path, [[{"text": "one"}], [{"text": "two"}]], store=store) as run:
        await store.add_client_token(token_hash(run.token), run.address)
        async with client(run, Events()) as printed:
            await printed.request("session.start", {})
            await run.server.sweep()  # confirms the printed token for the first row
            await printed.request("session.stop", {})
            assert store.client_tokens == []  # the store dropped them with the session
            await run.server.sweep()
            assert not printed.closed.is_set()
            await printed.request("session.start", {})
            await run.server.sweep()
            assert not printed.closed.is_set()
            assert store.client_tokens == [(token_hash(run.token), run.address)]
            # ... and that row is what an operator revokes.
            revoke(store, run.token)
            await run.server.sweep()
            await wait_closed(printed)
            assert printed.close_code == 4001


async def test_a_decision_is_recorded_under_the_token_of_the_client_that_made_it(tmp_path: Path):
    """The database checks a decision against a token; it must be the deciding client's own,
    or revoking one token would make the decisions of every other client unrecordable."""
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with served(tmp_path, turns) as run:
        await run.store.add_client_token(token_hash(run.token), run.address)
        await run.store.add_client_token(token_hash("minted"), run.address)
        events = Events()
        async with connect(run.address, "minted", "zed", events) as zed:
            await zed.request("session.start", {})
            await zed.request("chat.send", {"text": "ship it"})
            request = (await events.wait_for("escalation.request"))[0]
            await zed.request(
                "escalation.decide",
                {"escalation_id": request["escalation_id"], "decision": "allow"},
            )
            await events.wait_for("turn.result")
        assert list(run.store.decided_with.values()) == [token_hash("minted")]


# ------------------------------------------------------------------ falling behind (pc3)


async def test_a_client_that_falls_behind_is_disconnected_and_can_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A client whose queue overflowed gets a close with its own code instead of silence."""
    from calu import bus as bus_module
    from calu.protocol.messages import Log

    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as slow:
            monkeypatch.setattr(bus_module, "CLIENT_QUEUE", 3)
            for number in range(10):  # no await gives the push task a chance in between
                await run.server.bus.publish(Log(level="info", message=f"line {number}"))
            assert run.server.bus.clients == 0, "an overflowed client still counts as attached"
            assert run.server.bus.no_clients.is_set()
            await wait_closed(slow)
            assert slow.close_code == 4002
            assert slow.fell_behind
            seen = slow.last_seq
        monkeypatch.setattr(bus_module, "CLIENT_QUEUE", 1000)
        again = Events()
        async with client(run, again, after_seq=seen) as back:
            assert back.complete
            assert [e["message"] for e in again.of("log")][-1] == "line 9"
            assert [e["seq"] for e in again.of("log")] == list(range(seen + 1, 11))


async def test_subscribing_again_does_not_send_an_event_twice(tmp_path: Path):
    from calu.protocol.messages import Log

    async with served(tmp_path) as run:
        events = Events()
        async with client(run, events) as protocol:
            for number in range(5):
                await run.server.bus.publish(Log(level="info", message=f"line {number}"))
            # A second subscription drops what the first one has not pushed yet (those events
            # are in its replay), so the count below holds only once all five have arrived.
            # Without this wait the test failed once in CI with five pushes instead of six.
            await events.wait_for("log", 5)
            replay = await protocol.request("events.subscribe", {"after_seq": 0})
            assert len(replay["events"]) == 5
            await run.server.bus.publish(Log(level="info", message="after"))
            await events.wait_for("log", 6)
            await anyio.sleep(0.05)
            pushed = [e["seq"] for e in events.of("log")]
            assert sorted(set(pushed)) == pushed, f"an event was pushed twice: {pushed}"


# ------------------------------------------------------------------ control requests (pc4)


async def test_a_control_request_is_answered_while_an_ordinary_request_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`interrupt`, `shutdown`, `state.get` and `escalation.decide` must not wait behind a
    slow `session.start` of the same connection."""
    import json

    from websockets.asyncio.client import connect as ws_connect

    async with served(tmp_path) as run:
        release = anyio.Event()

        async def slow_start(*args: Any, **kwargs: Any) -> None:
            await release.wait()

        monkeypatch.setattr(run.session, "start", slow_start)
        async with ws_connect(run.address) as socket:

            async def send(request_id: int, method: str, params: dict[str, Any]) -> None:
                await socket.send(
                    json.dumps({"id": request_id, "method": method, "params": params})
                )

            await send(1, "initialize", {"client": "raw", "token": run.token})
            await send(2, "session.start", {})
            await send(3, "checkers.get", {})  # ordinary: waits for its turn
            await send(4, "state.get", {})  # control: answered at once
            with anyio.fail_after(10):
                first = json.loads(await socket.recv())
                second = json.loads(await socket.recv())
            assert [first["id"], second["id"]] == [1, 4]
            release.set()
            with anyio.fail_after(10):
                rest = [json.loads(await socket.recv())["id"] for _ in range(2)]
            assert rest == [2, 3], "ordinary requests keep the order they were sent in"


async def test_a_request_is_finished_when_its_client_disconnects(tmp_path: Path):
    """A client that goes away while its `session.start` runs must not leave the session
    halfway: the start and the requests the client sent after it are completed, and the next
    client finds a session it can use."""
    import json

    from websockets.asyncio.client import connect as ws_connect

    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        entered, release = anyio.Event(), anyio.Event()
        start = run.session.start

        async def delayed_start(*args: Any, **kwargs: Any) -> None:
            entered.set()
            await release.wait()
            await start(*args, **kwargs)

        run.session.start = delayed_start  # type: ignore[method-assign]
        async with ws_connect(run.address) as socket:
            for number, (method, params) in enumerate(
                [
                    ("initialize", {"client": "raw", "token": run.token}),
                    ("session.start", {}),
                    ("auto_escalate.set", {"edits": True}),  # waits behind the start
                ]
            ):
                await socket.send(json.dumps({"id": number, "method": method, "params": params}))
            with anyio.fail_after(10):
                await entered.wait()
        with anyio.fail_after(10):  # the server has noticed that the client is gone
            while run.server._clients and all(c._tasks for c in run.server._clients):
                await anyio.sleep(0.01)
        release.set()
        with anyio.fail_after(10):
            while run.session.state != "idle" or not run.session.snapshot().auto_escalate.edits:
                await anyio.sleep(0.01)
        with anyio.fail_after(10):  # and the connection's handler has ended
            while run.server._clients:
                await anyio.sleep(0.01)
        events = Events()
        async with client(run, events) as successor:
            await successor.request("chat.send", {"text": "hello"})
            await events.wait_for("turn.result")


async def test_a_session_stop_is_finished_when_its_client_disconnects(tmp_path: Path):
    import json

    from websockets.asyncio.client import connect as ws_connect

    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        entered, release = anyio.Event(), anyio.Event()
        stop = run.session.stop

        async def delayed_stop() -> None:
            entered.set()
            await release.wait()
            await stop()

        async with client(run, Events()) as first:
            await first.request("session.start", {})
        run.session.stop = delayed_stop  # type: ignore[method-assign]
        async with ws_connect(run.address) as socket:
            await socket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {"client": "raw", "token": run.token},
                    }
                )
            )
            await socket.send(json.dumps({"id": 2, "method": "session.stop", "params": {}}))
            with anyio.fail_after(10):
                await entered.wait()
        release.set()
        with anyio.fail_after(10):
            while run.session.state != "stopped":
                await anyio.sleep(0.01)
        run.session.stop = stop  # type: ignore[method-assign]


# ------------------------------------------------------------------ a database without an answer


async def psycopg_error(token_sha256: str) -> bool:
    import psycopg

    raise psycopg.OperationalError("the database is gone")


async def statement_timeout(token_sha256: str) -> bool:
    raise TimeoutError("the statement took too long")


async def network_error(token_sha256: str) -> bool:
    raise ConnectionResetError("connection reset by peer")


async def other_error(token_sha256: str) -> bool:
    raise LookupError("something else")


async def no_answer(token_sha256: str) -> bool:
    await anyio.sleep_forever()
    return False


@pytest.mark.parametrize(
    "database", [psycopg_error, statement_timeout, network_error, other_error, no_answer]
)
async def test_without_an_answer_from_the_database_the_last_known_answer_stands(
    tmp_path: Path, database: Any, monkeypatch: pytest.MonkeyPatch
):
    """D30, for every way in which the database can fail to answer: an authenticated client
    keeps interrupting and deciding, the printed token still attaches, a minted token cannot
    attach and is told that this is not a wrong token."""
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    from calu.protocol import server as server_module

    monkeypatch.setattr(server_module, "END_CHECK_S", 0.2)  # the end of the test stops the session
    async with served(tmp_path, turns) as run:
        run.server.check_s = 0.2
        await run.store.add_client_token(token_hash(run.token), run.address)
        await run.store.add_client_token(token_hash("minted"), run.address)
        events = Events()
        async with connect(run.address, "minted", "zed", events) as zed:
            await zed.request("session.start", {})
            await zed.request("chat.send", {"text": "ship it"})
            request = (await events.wait_for("escalation.request"))[0]
            run.store.client_token_ok = database  # type: ignore[method-assign]
            with anyio.fail_after(5):
                await run.server.sweep()
                await zed.request("auto_escalate.set", {"edits": True})
                await zed.request(
                    "escalation.decide",
                    {"escalation_id": request["escalation_id"], "decision": "deny"},
                )
                await zed.request("interrupt")
                async with client(run, Events()) as printed:
                    await printed.request("interrupt")
                with pytest.raises(RequestFailed) as unknown:
                    async with connect(run.address, "minted", "second", Events()):
                        pass
            assert unknown.value.code == -32603
            assert not zed.closed.is_set()


async def test_an_answer_that_arrives_late_is_still_applied(tmp_path: Path):
    """A request does not wait for a slow database, but what the database then says counts."""
    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        run.server.check_s = 0.05
        await run.store.add_client_token(token_hash("minted"), run.address)
        async with connect(run.address, "minted", "zed", Events()) as zed:
            await zed.request("session.start", {})
            await run.server.sweep()
            answer = anyio.Event()
            asked = run.store.client_token_ok

            async def slow(token_sha256: str) -> bool:
                await answer.wait()
                return await asked(token_sha256)

            run.store.client_token_ok = slow  # type: ignore[method-assign]
            revoke(run.store, "minted")
            await zed.request("auto_escalate.set", {"edits": True})  # no answer yet: allowed
            answer.set()
            await wait_closed(zed)
            assert zed.close_code == 4001


# ------------------------------------------------------------------ tokens and session rows


async def test_a_minted_token_is_checked_against_the_session_that_runs_now(tmp_path: Path):
    """A token is minted for one session row. A connection that stays open while the session
    is stopped and started again holds a token of the row before, so it is closed once the
    new session runs; the client mints a token for the new session."""
    store = RowPerStart()
    async with served(tmp_path, [[{"text": "one"}], [{"text": "two"}]], store=store) as run:
        await store.add_client_token(token_hash(run.token), run.address)
        async with client(run, Events()) as printed:
            await printed.request("session.start", {})
            await store.add_client_token(token_hash("minted"), run.address)
            async with connect(run.address, "minted", "zed", Events()) as zed:
                await zed.request("session.stop")
                await run.server.sweep()
                assert not zed.closed.is_set()  # it may start the next session
                await zed.request("session.start", {})
                await wait_closed(zed)
                assert zed.close_code == 4001
            assert run.session.snapshot().auto_escalate.edits is False
            with pytest.raises(RequestFailed) as refused:
                async with connect(run.address, "minted", "zed", Events()):
                    pass
            assert refused.value.code == -32001
            await printed.request("auto_escalate.set", {"edits": True})  # untouched


@pytest.mark.parametrize("which", ["printed", "minted"])
async def test_a_token_revoked_shortly_before_its_session_ends_stays_revoked(
    tmp_path: Path, which: str
):
    """Revoking a token and the end of its session may fall between two scheduled checks. The
    token must then not be usable in the next session of the same process."""
    store = RowPerStart()
    async with served(tmp_path, [[{"text": "one"}], [{"text": "two"}]], store=store) as run:
        await store.add_client_token(token_hash(run.token), run.address)
        await store.add_client_token(token_hash("minted"), run.address)
        await store.add_client_token(token_hash("other"), run.address)
        token = run.token if which == "printed" else "minted"
        async with connect(run.address, "other", "other", Events()) as other:
            await other.request("session.start", {})
            async with connect(run.address, token, "held", Events()) as held:
                await run.server.sweep()
                revoke(store, token)
                await run.session.stop()  # no check in between
                with pytest.raises(RequestFailed) as refused:
                    await held.request("session.start", {})
                assert refused.value.code == -32001
                assert run.session.state == "stopped"
                await wait_closed(held)
                assert held.close_code == 4001
            with pytest.raises(RequestFailed) as again:
                async with connect(run.address, token, "again", Events()):
                    pass
            assert again.value.code == -32001
            await other.request("session.start", {})  # a token that was valid at the end may
            assert store.client_tokens == [] or which == "minted"
            with pytest.raises(RequestFailed):
                async with connect(run.address, token, "again", Events()):
                    pass


async def test_the_printed_token_survives_a_session_that_never_recorded_it(tmp_path: Path):
    """A start that fails leaves a session row without a row for the printed token. That is
    not a revocation: the token keeps working and is recorded for the next session."""
    store = RowPerStart()
    async with served(tmp_path, [[{"text": "one"}], [{"text": "two"}]], store=store) as run:
        async with client(run, Events()) as printed:
            await printed.request("session.start", {})
            await run.session.stop()
            await printed.request("session.start", {})
            await run.server.sweep()
            assert not printed.closed.is_set()
            assert store.client_tokens == [(token_hash(run.token), run.address)]


# ------------------------------------------------------------------ which requests are checked


def test_every_request_that_is_not_listed_as_read_only_exists():
    from calu.protocol.messages import REQUESTS
    from calu.protocol.server import READ_ONLY_METHODS

    assert READ_ONLY_METHODS <= set(REQUESTS)
    assert set(REQUESTS) - READ_ONLY_METHODS == {
        "session.start",
        "session.stop",
        "chat.send",
        "comment.add",
        "escalation.decide",
        "auto_escalate.set",
        "honesty.clear",
        "honesty.trust",
        "honesty.review",
        "interrupt",
        "side.ask",
        "model.set",
        "shutdown",
    }, "a new request is checked against the token unless it is listed as read-only: decide"


async def test_a_revoked_token_is_refused_every_request_that_changes_the_session(tmp_path: Path):
    from calu.protocol.messages import REQUESTS
    from calu.protocol.server import READ_ONLY_METHODS

    async with served(tmp_path, [[{"text": "hi"}]]) as run:
        async with client(run, Events()) as owner:
            await owner.request("session.start", {})
            for method in sorted(set(REQUESTS) - READ_ONLY_METHODS):
                token = f"minted for {method}"
                await run.store.add_client_token(token_hash(token), run.address)
                async with connect(run.address, token, "zed", Events()) as zed:
                    await run.server.sweep()
                    revoke(run.store, token)
                    with pytest.raises(RequestFailed) as refused:
                        await zed.request(method, {})
                    assert refused.value.code == -32001, method
            assert run.session.state == "idle"
            assert not run.server.shutdown.is_set()
