"""The ACP adapter: handshake, prompts, tool calls, permission requests, replay."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.acp.adapter import Adapter
from calu.acp.peer import Peer
from calu.protocol.client import connect
from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

WORKSPACE = Path("/tmp/calu-acp-test")


class FakeCalu:
    """A calu session that records the requests it was sent and answers from a table."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.results = results or {}
        self.fail: set[str] = set()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, params or {}))
        if method in self.fail:
            raise RuntimeError(f"{method} refused")
        return self.results.get(method, {})

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.requests if name == method]


class Editor:
    """The ACP client side of the pipe.

    One pump task reads every line, so that a test may wait for a response and for an inbound
    request at the same time; without it two readers would race for the same line.
    """

    def __init__(self, to_agent: Any, from_agent: Any) -> None:
        self._to_agent = to_agent
        self._from_agent = from_agent
        self._id = 0
        self.inbox: list[dict[str, Any]] = []
        self.responses: dict[int, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []

    async def pump(self) -> None:
        """Read and sort the agent's messages until the stream ends."""
        while True:
            try:
                line = await self._from_agent.receive()
            except anyio.EndOfStream, anyio.ClosedResourceError:
                return
            message: dict[str, Any] = json.loads(line)
            if message.get("method") is None:
                request_id = message.get("id")
                if isinstance(request_id, int):
                    self.responses[request_id] = message
                else:
                    self.errors.append(message)
            else:
                self.inbox.append(message)

    async def send(self, message: dict[str, Any]) -> None:
        await self._to_agent.send(json.dumps(message))

    async def send_raw(self, line: str) -> None:
        await self._to_agent.send(line)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        request_id = self._id
        await self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        with anyio.fail_after(15):
            while request_id not in self.responses:
                await anyio.sleep(0.005)
        return self.responses.pop(request_id)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def expect(self, method: str) -> dict[str, Any]:
        """Wait for an inbound request or notification of `method` and take it out of the inbox."""
        with anyio.fail_after(15):
            while True:
                for index, message in enumerate(self.inbox):
                    if message.get("method") == method:
                        return self.inbox.pop(index)
                await anyio.sleep(0.005)

    def updates(self) -> list[dict[str, Any]]:
        return [
            message["params"]["update"]
            for message in self.inbox
            if message.get("method") == "session/update"
        ]

    async def wait_updates(self, count: int) -> list[dict[str, Any]]:
        """Wait until at least `count` session updates arrived and return all of them."""
        with anyio.fail_after(15):
            while len(self.updates()) < count:
                await anyio.sleep(0.005)
        return self.updates()


async def handshake(editor: Editor, calu: FakeCalu) -> str:
    """Run initialize and session/new; return the session id the adapter handed out."""
    await editor.call("initialize", {"protocolVersion": 1})
    answer = await editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
    return str(answer["result"]["sessionId"])


@dataclass
class Harness:
    """An adapter wired to a fake session and an in-memory editor."""

    calu: FakeCalu
    adapter: Adapter
    editor: Editor


async def run_adapter(calu: FakeCalu, body: Any) -> None:
    """Run the adapter over in-memory streams and call `body(harness)`."""
    to_agent_send, to_agent_receive = anyio.create_memory_object_stream[str](100)
    from_agent_send, from_agent_receive = anyio.create_memory_object_stream[str](100)

    async def read_line() -> str:
        try:
            return await to_agent_receive.receive()
        except anyio.EndOfStream:
            return ""

    async def write_line(line: str) -> None:
        await from_agent_send.send(line)

    adapter = Adapter(calu, WORKSPACE, name="calu", version="test")
    peer = Peer(read_line, write_line, adapter.handle)
    editor = Editor(to_agent_send, from_agent_receive)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            adapter.attach(peer, tasks)
            tasks.start_soon(peer.serve)
            tasks.start_soon(editor.pump)
            await body(Harness(calu, adapter, editor))
            tasks.cancel_scope.cancel()


async def test_initialize_reports_the_protocol_version_and_what_calu_can_do() -> None:
    async def body(h: Harness) -> None:
        answer = await h.editor.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True}, "terminal": True},
            },
        )
        result = answer["result"]
        assert result["protocolVersion"] == 1
        assert result["agentCapabilities"]["loadSession"] is True
        assert result["agentCapabilities"]["promptCapabilities"]["image"] is False
        assert result["authMethods"] == []
        assert result["agentInfo"]["name"] == "calu"

    await run_adapter(FakeCalu(), body)


async def test_a_session_is_the_running_one_and_only_for_its_own_directory() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "abc", "workspace": "/x"}})

    async def body(h: Harness) -> None:
        await h.editor.call("initialize", {"protocolVersion": 1})
        wrong = await h.editor.call("session/new", {"cwd": "/somewhere/else", "mcpServers": []})
        assert wrong["error"]["code"] == -32602
        assert "serves" in wrong["error"]["message"]
        answer = await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        assert answer["result"] == {"sessionId": "abc"}
        assert h.calu.sent("state.get")

    await run_adapter(calu, body)


async def test_mcp_servers_from_the_editor_are_not_forwarded() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "abc"}})

    async def body(h: Harness) -> None:
        await h.editor.call("initialize", {"protocolVersion": 1})
        await h.editor.call(
            "session/new",
            {
                "cwd": str(WORKSPACE),
                "mcpServers": [{"name": "x", "command": "/bin/x", "args": [], "env": []}],
            },
        )
        assert all("mcp" not in json.dumps(params).lower() for _, params in h.calu.requests)

    await run_adapter(calu, body)


async def test_a_prompt_becomes_a_chat_message_and_ends_with_the_turn() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answers: list[dict[str, Any]] = []
        async with anyio.create_task_group() as tasks:

            async def prompt() -> None:
                answers.append(
                    await h.editor.call(
                        "session/prompt",
                        {
                            "sessionId": session,
                            "prompt": [
                                {"type": "text", "text": "fix the test"},
                                {"type": "resource_link", "uri": "file:///a.py", "name": "a.py"},
                            ],
                        },
                    )
                )

            tasks.start_soon(prompt)
            while not h.calu.sent("chat.send"):
                await anyio.sleep(0.01)
            sent = h.calu.sent("chat.send")[0]
            assert sent["text"] == "fix the test\n\nfile:///a.py"
            assert sent["delivery"] == "queued"
            await h.adapter.on_event("turn.result", {"is_error": False, "result": "done"})
        assert answers[0]["result"] == {"stopReason": "end_turn"}

    await run_adapter(calu, body)


async def test_a_prompt_starts_a_session_that_was_stopped() -> None:
    calu = FakeCalu({"state.get": {"state": "stopped", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                h.editor.call,
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "go"}]},
            )
            while not h.calu.sent("chat.send"):
                await anyio.sleep(0.01)
            assert [name for name, _ in h.calu.requests].index("session.start") < [
                name for name, _ in h.calu.requests
            ].index("chat.send")
            await h.adapter.on_event("turn.result", {})

    await run_adapter(calu, body)


async def test_cancel_interrupts_the_session_and_the_prompt_reports_it() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answers: list[dict[str, Any]] = []
        async with anyio.create_task_group() as tasks:

            async def prompt() -> None:
                answers.append(
                    await h.editor.call(
                        "session/prompt",
                        {"sessionId": session, "prompt": [{"type": "text", "text": "go"}]},
                    )
                )

            tasks.start_soon(prompt)
            while not h.calu.sent("chat.send"):
                await anyio.sleep(0.01)
            await h.editor.notify("session/cancel", {"sessionId": session})
            while not answers:
                await anyio.sleep(0.01)
        assert answers[0]["result"] == {"stopReason": "cancelled"}
        assert h.calu.sent("interrupt")

    await run_adapter(calu, body)


async def test_a_tool_call_keeps_its_content_across_updates() -> None:
    calu = FakeCalu({"state.get": {"state": "running", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "tool.use",
            {"tool_use_id": "t1", "name": "Edit", "input": {"file_path": "/tmp/a.py"}},
        )
        await h.adapter.on_event(
            "edit.diff",
            {"tool_use_id": "t1", "path": "/tmp/a.py", "before": "old", "after": "new"},
        )
        await h.adapter.on_event(
            "check.result",
            {
                "tool_use_id": "t1",
                "tool_name": "Edit",
                "decision": "PASS",
                "step_summary": "Renames the helper.",
                "comment": "keep the docstring honest",
                "route": "edit",
            },
        )
        await h.adapter.on_event(
            "tool.result", {"tool_use_id": "t1", "is_error": False, "content": "ok"}
        )
        updates = await h.editor.wait_updates(4)
        announce = updates[0]
        assert announce["sessionUpdate"] == "tool_call"
        assert announce["kind"] == "edit"
        assert announce["title"] == "Edit: /tmp/a.py"
        assert announce["locations"] == [{"path": "/tmp/a.py"}]
        diff = updates[1]["content"][0]
        assert diff == {"type": "diff", "path": "/tmp/a.py", "oldText": "old", "newText": "new"}
        checked = updates[2]
        assert checked["title"] == "Renames the helper."
        assert checked["status"] == "in_progress"
        assert "keep the docstring honest" in checked["content"][1]["content"]["text"]
        finished = updates[3]
        assert finished["status"] == "completed"
        # The content collection is replaced, not appended to, so every update carries the diff.
        assert finished["content"][0]["type"] == "diff"
        assert len(finished["content"]) == 3

    await run_adapter(calu, body)


async def test_a_denied_step_is_shown_as_a_failed_tool_call() -> None:
    calu = FakeCalu({"state.get": {"state": "running", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "tool.use", {"tool_use_id": "t1", "name": "Bash", "input": {"command": "rm -rf /"}}
        )
        await h.adapter.on_event(
            "check.result",
            {
                "tool_use_id": "t1",
                "tool_name": "Bash",
                "decision": "DENY",
                "reason": "that deletes everything",
                "route": "command",
            },
        )
        updates = await h.editor.wait_updates(2)
        assert updates[0]["title"] == "rm -rf /"
        assert updates[0]["kind"] == "execute"
        assert updates[1]["status"] == "failed"
        assert "that deletes everything" in updates[1]["content"][0]["content"]["text"]

    await run_adapter(calu, body)


async def test_an_escalation_asks_the_editor_and_passes_the_answer_on() -> None:
    calu = FakeCalu({"state.get": {"state": "escalated", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "escalation.request",
            {
                "escalation_id": "7",
                "tool_use_id": "t1",
                "tool_name": "Bash",
                "tool_input": {"command": "sudo ls"},
                "reason": "the deny list matched",
                "rendered": "## Step\n\n    sudo ls",
            },
        )
        question = await h.editor.expect("session/request_permission")
        params = question["params"]
        assert params["toolCall"]["toolCallId"] == "t1"
        assert [option["kind"] for option in params["options"]] == [
            "allow_once",
            "allow_always",
            "reject_once",
            "reject_always",
        ]
        assert "the deny list matched" in json.dumps(params["toolCall"]["content"])
        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow-always"}},
            }
        )
        while not h.calu.sent("escalation.decide"):
            await anyio.sleep(0.01)
        decision = h.calu.sent("escalation.decide")[0]
        assert decision["escalation_id"] == "7"
        assert decision["decision"] == "allow"
        assert decision["note"] == 'the client chose "allow always"'

    await run_adapter(calu, body)


async def test_a_cancelled_permission_dialog_decides_nothing() -> None:
    calu = FakeCalu({"state.get": {"state": "escalated", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "escalation.request",
            {"escalation_id": "7", "tool_use_id": None, "tool_name": "Bash", "reason": "r"},
        )
        question = await h.editor.expect("session/request_permission")
        assert question["params"]["toolCall"]["toolCallId"] == "escalation-7"
        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        )
        await anyio.sleep(0.05)
        assert not h.calu.sent("escalation.decide")

    await run_adapter(calu, body)


async def test_an_escalation_resolved_elsewhere_is_not_decided_twice() -> None:
    calu = FakeCalu({"state.get": {"state": "escalated", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "escalation.request",
            {"escalation_id": "7", "tool_use_id": "t1", "tool_name": "Bash", "reason": "r"},
        )
        question = await h.editor.expect("session/request_permission")
        await h.adapter.on_event(
            "escalation.resolved", {"escalation_id": "7", "decision": "allow", "note": "by phone"}
        )
        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
            }
        )
        await anyio.sleep(0.05)
        assert not h.calu.sent("escalation.decide")

    await run_adapter(calu, body)


async def test_the_text_of_one_message_is_sent_once_and_our_own_prompt_is_not_echoed() -> None:
    calu = FakeCalu({"state.get": {"state": "running", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        # One prompt turn, so that the adapter remembers the text it sent.
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                h.editor.call,
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]},
            )
            while not h.calu.sent("chat.send"):
                await anyio.sleep(0.01)
            await h.adapter.on_event("turn.result", {})
        # The session echoes our own message back, and another client sends one of its own.
        await h.adapter.on_event("user.input", {"text": "hello", "source": "user"})
        await h.adapter.on_event("user.input", {"text": "from the terminal", "source": "user"})
        await h.adapter.on_event("assistant.delta", {"text": "par", "message_id": "m1"})
        await h.adapter.on_event("assistant.delta", {"text": "tial", "message_id": "m1"})
        # The completed text repeats what the deltas already said, so it is not sent again.
        await h.adapter.on_event("assistant.text", {"text": "partial", "message_id": "m1"})
        await h.adapter.on_event("assistant.thinking", {"text": "hmm"})
        updates = await h.editor.wait_updates(4)
        kinds = [(u["sessionUpdate"], u["content"]["text"]) for u in updates]
        assert kinds == [
            ("user_message_chunk", "from the terminal"),
            ("agent_message_chunk", "par"),
            ("agent_message_chunk", "tial"),
            ("agent_thought_chunk", "hmm"),
        ]

    await run_adapter(calu, body)


async def test_session_load_replays_the_recorded_conversation_before_it_answers() -> None:
    calu = FakeCalu(
        {
            "state.get": {"state": "idle", "calu_session_id": "s1"},
            "session.history": {
                "turns": [
                    {"index": 1, "user_inputs": ["fix it"], "answer": "fixed"},
                    {"index": 2, "user_inputs": ["thanks"], "answer": ""},
                ],
                "steps": [
                    {
                        "seq": 1,
                        "turn": 1,
                        "tool_use_id": "t1",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "decision": "pass",
                        "summary": "Runs the tests.",
                        "result_summary": "1 passed",
                    }
                ],
            },
        }
    )

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answer = await h.editor.call(
            "session/load", {"sessionId": session, "cwd": str(WORKSPACE), "mcpServers": []}
        )
        assert answer["result"] == {}
        updates = await h.editor.wait_updates(5)
        kinds = [update["sessionUpdate"] for update in updates]
        # Everything was replayed before the response, in conversation order.
        assert kinds == [
            "user_message_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
            "user_message_chunk",
        ]
        assert updates[1]["title"] == "Runs the tests."
        assert updates[1]["status"] == "completed"
        assert "1 passed" in updates[2]["content"][0]["content"]["text"]

    await run_adapter(calu, body)


async def test_a_prompt_for_another_session_and_an_unknown_method_are_refused() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await handshake(h.editor, h.calu)
        wrong = await h.editor.call(
            "session/prompt", {"sessionId": "other", "prompt": [{"type": "text", "text": "x"}]}
        )
        assert wrong["error"]["code"] == -32002
        empty = await h.editor.call("session/prompt", {"sessionId": "s1", "prompt": []})
        assert empty["error"]["code"] == -32602
        unknown = await h.editor.call("session/set_mode", {"sessionId": "s1", "modeId": "x"})
        assert unknown["error"]["code"] == -32601

    await run_adapter(calu, body)


async def test_a_line_that_is_not_json_does_not_end_the_connection() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await h.editor.send_raw("this is not json")
        with anyio.fail_after(5):
            while not h.editor.errors:
                await anyio.sleep(0.005)
        assert h.editor.errors[0]["error"]["code"] == -32700
        answer = await h.editor.call("initialize", {"protocolVersion": 1})
        assert answer["result"]["protocolVersion"] == 1

    await run_adapter(calu, body)


async def test_the_adapter_serves_a_real_session_over_the_real_protocol(tmp_path: Path) -> None:
    """End to end except for stdio: a real session, a real WebSocket, a scripted Claude.

    The step carries the fake checker's escalate marker, so the whole chain runs: Claude asks
    for a tool call, the checker escalates, the editor is asked for permission, its answer
    becomes a calu decision, the step runs and the prompt turn ends.
    """
    turns = [
        [
            {"text": "I will deploy."},
            {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"},
        ]
    ]
    async with served(tmp_path, turns) as run:
        to_agent_send, to_agent_receive = anyio.create_memory_object_stream[str](100)
        from_agent_send, from_agent_receive = anyio.create_memory_object_stream[str](100)

        async def read_line() -> str:
            try:
                return await to_agent_receive.receive()
            except anyio.EndOfStream:
                return ""

        async def write_line(line: str) -> None:
            await from_agent_send.send(line)

        holder: list[Adapter] = []

        async def on_event(method: str, params: dict[str, Any]) -> None:
            if holder:
                await holder[0].on_event(method, params)

        editor = Editor(to_agent_send, from_agent_receive)
        async with connect(run.address, run.token, "calu acp", on_event) as calu_client:
            adapter = Adapter(calu_client, tmp_path, name="calu", version="test")
            holder.append(adapter)
            peer = Peer(read_line, write_line, adapter.handle)
            with anyio.fail_after(20):
                async with anyio.create_task_group() as tasks:
                    adapter.attach(peer, tasks)
                    tasks.start_soon(peer.serve)
                    tasks.start_soon(editor.pump)
                    await editor.call("initialize", {"protocolVersion": 1})
                    created = await editor.call(
                        "session/new", {"cwd": str(tmp_path), "mcpServers": []}
                    )
                    session = created["result"]["sessionId"]
                    answers: list[dict[str, Any]] = []

                    async def prompt() -> None:
                        answers.append(
                            await editor.call(
                                "session/prompt",
                                {
                                    "sessionId": session,
                                    "prompt": [{"type": "text", "text": "ship it"}],
                                },
                            )
                        )

                    tasks.start_soon(prompt)
                    question = await editor.expect("session/request_permission")
                    assert question["params"]["sessionId"] == session
                    await editor.send(
                        {
                            "jsonrpc": "2.0",
                            "id": question["id"],
                            "result": {
                                "outcome": {"outcome": "selected", "optionId": "allow-once"}
                            },
                        }
                    )
                    with anyio.fail_after(15):
                        while not answers:
                            await anyio.sleep(0.01)
                    assert answers[0]["result"] == {"stopReason": "end_turn"}
                    updates = await editor.wait_updates(1)
                    kinds = [update["sessionUpdate"] for update in updates]
                    assert "tool_call" in kinds
                    assert "agent_message_chunk" in kinds
                    completed = [
                        update
                        for update in updates
                        if update.get("status") == "completed" and "content" in update
                    ]
                    assert completed, "the tool call was never completed in the editor's view"
                    assert "ok" in json.dumps(completed[-1]["content"])
                    escalations = run.store.escalations
                    assert [row.state for row in escalations.values()] == ["allowed"]
                    tasks.cancel_scope.cancel()
