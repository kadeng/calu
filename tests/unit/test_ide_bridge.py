"""`calu ide` in-process against a served fake session: no stdio, no database, no `claude`."""

import json
import os
import sys
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from calu import __version__, doctor
from calu.bus import Bus
from calu.claude import FakeRunner, FakeScript
from calu.config import BEDROCK_EU_HAIKU
from calu.credentials import CredentialStore
from calu.ide import presets as ide_presets
from calu.model_choice import FIREWORKS_MODEL
from calu.protocol.schema import protocol_schema
from calu.protocol.server import SessionServer
from calu.session import Session
from calu.store import MemorySessionStore
from calu.testing import fake_models
from calu.tokens import new_token, token_hash
from tests.unit.helpers import default_settings
from tests.unit.ide_helpers import Failed, bridged
from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

ESCALATING = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]


@pytest.fixture(autouse=True)
def own_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the token-free sockets at an empty directory with a short path.

    The bridge looks for sessions in `$XDG_RUNTIME_DIR/calu/sessions`; a test must neither
    find nor contact the sessions of whoever runs the tests. The path is short because a
    Unix socket's path is limited to about 100 characters.
    """
    with tempfile.TemporaryDirectory(prefix="calu-ide-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        yield Path(directory)


async def test_initialize_says_who_the_bridge_is(tmp_path: Path, store: CredentialStore):
    async with bridged(tmp_path, store) as editor:
        answer = await editor.request(
            "ide.initialize", {"client": "test", "client_version": "0.0.1"}
        )
        assert answer == {
            "bridge_version": __version__,
            "protocol_version": 1,
            "workspace": str(tmp_path.resolve()),
        }
        with pytest.raises(Failed) as unknown:
            await editor.request("ide.nothing")
        assert unknown.value.code == -32601


async def test_a_session_request_without_an_attached_session_is_refused(
    tmp_path: Path, store: CredentialStore
):
    async with bridged(tmp_path, store) as editor:
        with pytest.raises(Failed) as refused:
            await editor.call("state.get")
        assert refused.value.code == -32015
        assert await editor.request("ide.detach") == {}


async def test_attach_relays_requests_and_forwards_events_in_order(
    tmp_path: Path, store: CredentialStore
):
    async with served(tmp_path, [[{"text": "first"}, {"text": "second"}]]) as run:
        async with bridged(tmp_path, store) as editor:
            attached = await editor.request(
                "ide.attach", {"address": run.address, "token": run.token}
            )
            assert attached["attached"] is True and attached["complete"] is True
            assert attached["initialize"]["workspace"] == str(tmp_path)
            assert attached["snapshot"]["state"] == "stopped"
            assert attached["events"] == []
            assert (await editor.call("state.get"))["workspace"] == str(tmp_path)

            await editor.call("session.start")
            await editor.call("chat.send", {"text": "please work"})
            await editor.wait_event("turn.result")
            texts = [event["text"] for event in await editor.wait_event("assistant.text", 2)]
            assert texts == ["first", "second"]
            assert [e["text"] for e in await editor.wait_event("user.input")] == ["please work"]
            # Every event once, in the session's order, and none before the attach's answer.
            seqs = [event["params"]["seq"] for event in editor.events()]
            assert seqs == sorted(set(seqs)) and seqs[0] >= 1
            kinds = ["method" in message for message in editor.wire]
            assert kinds.index(True) > 0, "a notification was written before the first answer"
            first_note = next(m for m in editor.wire if "method" in m)
            assert (first_note["method"], first_note["params"]["state"]) == ("ide.link", "attached")

            # The session's own errors pass through with their code.
            with pytest.raises(Failed) as refused:
                await editor.call("escalation.context", {"escalation_id": "999999"})
            assert refused.value.code not in (-32015, -32603)

            assert await editor.request("ide.detach") == {}
            seen = len(editor.notifications)
            with pytest.raises(Failed) as detached:
                await editor.call("state.get")
            assert detached.value.code == -32015
            await anyio.sleep(0.1)
            assert len(editor.notifications) == seen


async def test_an_attach_after_events_returns_them_and_continues_after_seq(
    tmp_path: Path, store: CredentialStore
):
    async with served(tmp_path, [[{"text": "an answer"}]]) as run:
        async with bridged(tmp_path, store) as editor:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            await editor.call("session.start")
            await editor.call("chat.send", {"text": "hello"})
            await editor.wait_event("turn.result")
            # The turn's last events (the state after it) follow its result: let them arrive.
            await anyio.sleep(0.3)
            seen = [event["params"]["seq"] for event in editor.events()]
            await editor.request("ide.detach")

            again = await editor.request("ide.attach", {"address": run.address, "token": run.token})
            replayed = [event["params"]["seq"] for event in again["events"]]
            assert replayed == sorted(replayed) and replayed[: len(seen)] == seen
            assert "assistant.text" in [event["method"] for event in again["events"]]
            assert again["snapshot"]["state"] != "stopped"
            last = replayed[-1]

            later = await editor.request(
                "ide.attach", {"address": run.address, "token": run.token, "after_seq": last}
            )
            assert later["events"] == []


async def test_an_escalation_is_decided_through_the_bridge(tmp_path: Path, store: CredentialStore):
    async with served(tmp_path, ESCALATING) as run:
        async with bridged(tmp_path, store) as editor:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            await editor.call("session.start")
            await editor.call("chat.send", {"text": "ship it"})
            asked = (await editor.wait_event("escalation.request"))[0]
            snapshot = await editor.call("state.get")
            assert [e["escalation_id"] for e in snapshot["escalations"]] == [asked["escalation_id"]]
            await editor.call(
                "escalation.decide",
                {"escalation_id": asked["escalation_id"], "decision": "allow", "note": "go"},
            )
            await editor.wait_event("turn.result")
            assert (await editor.wait_event("escalation.resolved"))[0]["decision"] == "allow"
            assert [p["content"] for p in await editor.wait_event("tool.result")] == ["ok"]


async def test_a_second_attach_detaches_from_the_first_session(
    tmp_path: Path, store: CredentialStore
):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    async with served(one, [[{"text": "from one"}]]) as first:
        async with served(two, [[{"text": "from two"}]]) as second:
            async with bridged(tmp_path, store) as editor:
                await editor.request("ide.attach", {"address": first.address, "token": first.token})
                await editor.request(
                    "ide.attach", {"address": second.address, "token": second.token}
                )
                assert (await editor.call("state.get"))["workspace"] == str(two)
                await editor.call("session.start")
                await editor.call("chat.send", {"text": "hello"})
                await editor.wait_event("turn.result")
                assert [e["text"] for e in await editor.wait_event("assistant.text")] == [
                    "from two"
                ]


async def test_a_refused_token_and_an_unreachable_address_are_errors(
    tmp_path: Path, store: CredentialStore
):
    async with served(tmp_path) as run:
        async with bridged(tmp_path, store) as editor:
            with pytest.raises(Failed) as refused:
                await editor.request("ide.attach", {"address": run.address, "token": "wrong"})
            assert refused.value.code == -32001
            with pytest.raises(Failed) as unreachable:
                await editor.request("ide.attach", {"address": "ws://127.0.0.1:9"})
            assert unreachable.value.code == -32011
            assert "cannot attach" in unreachable.value.message
            with pytest.raises(Failed) as still:
                await editor.call("state.get")
            assert still.value.code == -32015


async def test_a_session_that_ends_is_reported_as_lost(tmp_path: Path, store: CredentialStore):
    async with bridged(tmp_path, store, reconnect_wait_s=0.5) as editor:
        async with served(tmp_path) as run:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            await editor.wait_link("attached")
        await editor.wait_link("lost")
        assert editor.links() == ["attached", "reconnecting", *editor.links()[2:]]
        assert set(editor.links()[1:-1]) == {"reconnecting"} and editor.links()[-1] == "lost"
        with pytest.raises(Failed) as gone:
            await editor.call("state.get")
        assert gone.value.code == -32015
        # After the loss the editor attaches again; here nothing listens any more.
        with pytest.raises(Failed) as again:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
        assert again.value.code == -32011


@asynccontextmanager
async def served_on_socket(workspace: Path, path: Path) -> AsyncGenerator[Session]:
    """Run a fake session that lets local clients in without a token, on a Unix socket.

    Args:
        workspace: The session's project directory.
        path: The socket's path.

    Yields:
        The session.
    """
    bus = Bus()
    session = Session(
        workspace,
        default_settings(workspace),
        MemorySessionStore(),
        lambda: FakeRunner(FakeScript(turns=[]), workspace),
        bus.publish,
        checker_models=fake_models(),
        snapshot_root=workspace.parent / f"{workspace.name}-snapshots",
    )
    async with anyio.create_task_group() as tasks:
        server = SessionServer(session, bus, token_hash(new_token()), tasks, insecure=True)
        tasks.start_soon(server.listen, "127.0.0.1", 0, path)
        with anyio.fail_after(10):
            await server.ready.wait()
        try:
            yield session
        finally:
            await session.stop()
            tasks.cancel_scope.cancel()


async def test_the_session_of_the_workspace_is_found_through_its_token_free_socket(
    tmp_path: Path, store: CredentialStore, own_runtime_directory: Path
):
    sockets = own_runtime_directory / "calu" / "sessions"
    project, other = tmp_path / "project", tmp_path / "other"
    (project / "src").mkdir(parents=True)
    other.mkdir()
    async with served_on_socket(project, sockets / "aaaa1111.sock"):
        async with served_on_socket(other, sockets / "bbbb2222.sock"):
            # From a subdirectory of the project: the project's session, without a token.
            async with bridged(project / "src", store) as editor:
                attached = await editor.request("ide.attach", {"session": None})
                assert attached["snapshot"]["workspace"] == str(project)
                assert attached["session_id"] == "aaaa1111"
                # By the beginning of an id: the other one.
                named = await editor.request("ide.attach", {"session": "bbbb"})
                assert named["snapshot"]["workspace"] == str(other)
            # A workspace without a session, and no database credential to ask further.
            async with bridged(tmp_path, store) as editor:
                with pytest.raises(Failed) as nothing:
                    await editor.request("ide.attach", {})
                assert nothing.value.code == -32017
                assert "calu setup" in nothing.value.message


async def test_the_presets_need_no_database_and_hold_no_large_mistral_model(
    tmp_path: Path, store: CredentialStore
):
    async with bridged(tmp_path, store) as editor:
        answer = await editor.request("ide.models.presets")
    assert answer["roles"] == [] and answer["claude"] is None
    assert "no database credential" in answer["problem"]
    by_id = {preset["id"]: preset for preset in answer["presets"]}
    assert list(by_id) == [
        "default",
        "anthropic-haiku",
        "anthropic-sonnet",
        "bedrock-haiku",
        "bedrock-sonnet",
        "bedrock-eu-haiku",
        "fireworks-nemotron",
        "mistral-small",
        "claude-cli-haiku",
    ]
    assert by_id["default"]["model"] == ""
    assert by_id["anthropic-haiku"]["model"] == "anthropic:claude-haiku-5-5"
    assert by_id["bedrock-eu-haiku"]["model"] == BEDROCK_EU_HAIKU
    assert by_id["fireworks-nemotron"]["model"] == FIREWORKS_MODEL
    assert by_id["claude-cli-haiku"]["model"] == "claude:haiku"
    assert "small" in by_id["mistral-small"]["model"]
    for preset in answer["presets"]:
        assert set(preset) == {"id", "label", "group", "model", "note"}
        assert preset["label"] and preset["group"] and preset["note"]
        assert "medium" not in preset["model"] and "large" not in preset["model"]
    assert ide_presets.model_of("default") == ""
    assert ide_presets.model_of("mistral-small") == by_id["mistral-small"]["model"]
    assert ide_presets.model_of("openai:gpt-x") == "openai:gpt-x"


async def test_the_status_reports_the_tools_and_a_missing_database(
    tmp_path: Path, store: CredentialStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(doctor, "claude_version", lambda: (True, "2.1.0 (test)"))
    async with bridged(tmp_path, store) as editor:
        status = await editor.request("ide.status")
    findings = {finding["name"]: finding for finding in status["findings"]}
    assert {"claude", "git", "uv", "tmux", "screen"} <= set(findings)
    assert findings["claude"] == {
        "name": "claude",
        "ok": True,
        "level": "ok",
        "message": "2.1.0 (test)",
        "advice": "",
    }
    for name in ("uv", "tmux", "screen"):
        assert findings[name]["level"] in ("ok", "note")
    for finding in status["findings"]:
        assert set(finding) == {"name", "ok", "level", "message", "advice"}
        assert finding["level"] in ("ok", "missing", "note")
    assert status["database"]["reachable"] is False
    assert "no database credential" in status["database"]["problem"]
    assert status["server"] == {"sessions_running": 0}


async def test_the_bridge_ends_when_asked_or_when_the_editor_closes_its_end(
    tmp_path: Path, store: CredentialStore
):
    with anyio.fail_after(10):
        async with bridged(tmp_path, store) as editor:
            assert await editor.request("ide.shutdown") == {}
            # The block ends without closing the editor's end first: `serve` returned.
            editor.close = lambda: None  # type: ignore[method-assign]
    with anyio.fail_after(10):
        async with bridged(tmp_path, store) as editor:
            await editor.request("ide.initialize")


def test_the_protocol_schema_describes_the_side_question_and_the_usage():
    document = protocol_schema()
    assert document["results"]["side.ask"] == {"$ref": "#/$defs/SideAskResult"}
    assert document["results"]["usage.get"] == {"$ref": "#/$defs/UsageResult"}
    assert "ContextUsage" in document["$defs"]


async def test_calu_ide_as_a_process_keeps_stdout_for_the_protocol(
    tmp_path: Path, own_runtime_directory: Path
):
    """The entry point on real pipes: only protocol lines on stdout, an end on `ide.shutdown`.

    The process gets its own configuration and state directories, so it reads no credential
    store and writes no log of whoever runs the tests.
    """
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(own_runtime_directory),
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "ide.initialize", "params": {"client": "test"}},
        {"jsonrpc": "2.0", "id": 2, "method": "session.call", "params": {"method": "state.get"}},
        {"jsonrpc": "2.0", "id": 3, "method": "ide.shutdown", "params": {}},
    ]
    with anyio.fail_after(60):
        async with await anyio.open_process(
            [sys.executable, "-m", "calu", "ide"], cwd=tmp_path, env=environment
        ) as process:
            assert process.stdin is not None and process.stdout is not None
            for request in requests:
                await process.stdin.send((json.dumps(request) + "\n").encode())
            # stdin stays open: the process has to end because it was asked to.
            written = b""
            async for chunk in process.stdout:
                written += chunk
            assert await process.wait() == 0
    answers = [json.loads(line) for line in written.decode().splitlines()]
    assert [answer["id"] for answer in answers] == [1, 2, 3]
    assert answers[0]["result"]["workspace"] == str(tmp_path.resolve())
    assert answers[1]["error"]["code"] == -32015
    assert answers[2]["result"] == {}
    assert (tmp_path / "state" / "calu" / "log" / "ide.log").exists()
