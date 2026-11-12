"""The ACP adapter: handshake, prompts, tool calls, permission requests, replay."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.acp import modes
from calu.acp.adapter import Adapter
from calu.acp.peer import Peer
from calu.config import AutoEscalate
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
        # D152: a method the adapter handles has to be offered, or a client never calls it.
        assert result["agentCapabilities"]["sessionCapabilities"] == {"close": {}}
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
        assert answer["result"]["sessionId"] == "abc"
        assert h.calu.sent("state.get")
        # A directory **inside** the project is the same project: an editor opened on one
        # package of a repository works under the same rules, and `calu acp` with no address
        # finds the session from there too (D105).
        inside = await h.editor.call(
            "session/new", {"cwd": str(WORKSPACE / "src" / "package"), "mcpServers": []}
        )
        assert inside["result"]["sessionId"] == "abc"
        # One above it is not: the editor would act on files the session does not cover.
        above = await h.editor.call("session/new", {"cwd": str(WORKSPACE.parent), "mcpServers": []})
        assert above["error"]["code"] == -32602
        assert "does not contain" in above["error"]["message"]

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


async def test_a_prompt_stays_open_while_background_tasks_run() -> None:
    """D159: ACP streams updates only during a prompt turn, and Claude Code continues by itself
    when background tasks finish (D158), so the prompt is answered after that follow-up turn."""
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})
    answers: list[dict] = []

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
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
            await h.adapter.on_event("turn.result", {"background_tasks": 1})
            await anyio.sleep(0.05)
            assert answers == [], "answered while a background task was still running"
            await h.adapter.on_event("turn.result", {"background_tasks": 0})
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
        # The result carries the session's mode, like the result of `session/new`.
        assert answer["result"]["modes"]["currentModeId"] == "checkers"
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
        # `session/set_mode` used to stand here; it is implemented now, so this needs a
        # method calu really does not have.
        unknown = await h.editor.call("session/select_model", {"sessionId": "s1", "modelId": "x"})
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


# ------------------------------------------------------------------ session modes


IDLE = {"state": "idle", "calu_session_id": "abc"}
"""A `state.get` snapshot of an idle session whose categories are all off."""


def test_the_mode_ladder_maps_each_rung_to_its_categories() -> None:
    """Every rung round-trips, and a combination off the ladder is "custom"."""
    for mode in modes.LADDER:
        assert modes.mode_of(mode.settings) == mode.id
        assert modes.settings_for(mode.id) == mode.settings
    # Each rung is stricter than the one before, which is what makes the list a ladder.
    for stricter, looser in zip(modes.LADDER[1:], modes.LADDER[:-1], strict=True):
        for field in ("commands", "edits", "subagents"):
            if getattr(looser.settings, field):
                assert getattr(stricter.settings, field), f"{stricter.id} is not stricter"
    assert modes.mode_of(AutoEscalate(commands=True)) == modes.CUSTOM
    assert modes.mode_of(AutoEscalate(subagents=True)) == modes.CUSTOM


def test_custom_is_offered_only_while_it_is_the_state_and_cannot_be_chosen() -> None:
    """ACP needs the current mode in the list, but "custom" is not a target."""
    on_ladder = modes.mode_state(AutoEscalate(edits=True))
    assert modes.CUSTOM not in [entry["id"] for entry in on_ladder["availableModes"]]
    off_ladder = modes.mode_state(AutoEscalate(commands=True))
    assert off_ladder["currentModeId"] == modes.CUSTOM
    assert modes.CUSTOM in [entry["id"] for entry in off_ladder["availableModes"]]
    with pytest.raises(ValueError, match="cannot be chosen"):
        modes.settings_for(modes.CUSTOM)
    with pytest.raises(ValueError, match="unknown mode"):
        modes.settings_for("yolo")


def test_a_boolean_option_is_offered_only_to_a_client_that_takes_one() -> None:
    """An agent must not send boolean config options unless the client advertised them."""
    without = modes.config_options(AutoEscalate(), booleans=False)
    assert [option["id"] for option in without] == [modes.MODE_OPTION_ID]
    assert without[0]["type"] == "select"
    with_booleans = modes.config_options(AutoEscalate(edits=True), booleans=True)
    kinds = {option["id"]: option["type"] for option in with_booleans}
    assert kinds == {
        modes.MODE_OPTION_ID: "select",
        "auto_escalate_commands": "boolean",
        "auto_escalate_edits": "boolean",
        "auto_escalate_subagents": "boolean",
    }
    values = {option["id"]: option["currentValue"] for option in with_booleans}
    assert values["auto_escalate_edits"] is True
    assert values["auto_escalate_commands"] is False
    assert values[modes.MODE_OPTION_ID] == "edits"


def test_setting_one_option_changes_only_that_switch() -> None:
    """A boolean option is one switch; the select option replaces all three."""
    start = AutoEscalate(edits=True)
    assert modes.with_option(start, "auto_escalate_commands", True) == AutoEscalate(
        commands=True, edits=True
    )
    assert modes.with_option(start, modes.MODE_OPTION_ID, "checkers") == AutoEscalate()
    assert start == AutoEscalate(edits=True), "the switches were changed in place"
    with pytest.raises(ValueError, match="unknown config option"):
        modes.with_option(start, "nonsense", True)
    with pytest.raises(ValueError, match="takes true or false"):
        modes.with_option(start, "auto_escalate_edits", "yes")
    with pytest.raises(ValueError, match="takes a mode id"):
        modes.with_option(start, modes.MODE_OPTION_ID, True)


async def test_a_new_session_reports_the_mode_in_both_apis() -> None:
    """`session/new` carries `modes` and `configOptions`, as the transition asks."""
    calu = FakeCalu({"state.get": {**IDLE, "auto_escalate": {"edits": True}}})

    async def body(h: Harness) -> None:
        await h.editor.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"session": {"configOptions": {"boolean": {}}}},
            },
        )
        answer = await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        result = answer["result"]
        assert result["modes"]["currentModeId"] == "edits"
        assert [entry["id"] for entry in result["modes"]["availableModes"]] == [
            mode.id for mode in modes.LADDER
        ]
        # The client advertised boolean options, so the three switches are offered as such.
        assert len(result["configOptions"]) == 4

    await run_adapter(calu, body)


async def test_a_client_that_is_silent_about_booleans_gets_only_the_mode_select() -> None:
    """The capability is the presence of an object, so saying nothing means no support."""
    calu = FakeCalu({"state.get": IDLE})

    async def body(h: Harness) -> None:
        await h.editor.call("initialize", {"protocolVersion": 1})
        answer = await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        options = answer["result"]["configOptions"]
        assert [option["id"] for option in options] == [modes.MODE_OPTION_ID]

    await run_adapter(calu, body)


async def test_set_mode_asks_the_session_and_refuses_an_unknown_mode() -> None:
    """A mode becomes one `auto_escalate.set`; a bad mode changes nothing."""
    calu = FakeCalu({"state.get": IDLE})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answer = await h.editor.call(
            "session/set_mode", {"sessionId": session, "modeId": "everything"}
        )
        assert answer["result"] == {}
        assert h.calu.sent("auto_escalate.set") == [
            {"commands": True, "edits": True, "subagents": True}
        ]
        refused = await h.editor.call("session/set_mode", {"sessionId": session, "modeId": "nope"})
        assert refused["error"]["code"] == -32602
        assert "unknown mode" in refused["error"]["message"]
        wrong = await h.editor.call("session/set_mode", {"sessionId": "other", "modeId": "edits"})
        assert wrong["error"]["code"] in (-32002, -32602)
        # Neither the unknown mode nor the unknown session reached the session.
        assert len(h.calu.sent("auto_escalate.set")) == 1

    await run_adapter(calu, body)


async def test_set_config_option_answers_with_every_option_and_its_new_value() -> None:
    """ACP wants the complete state back, so a dependent change could be reported."""
    calu = FakeCalu({"state.get": IDLE})

    async def body(h: Harness) -> None:
        await h.editor.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"session": {"configOptions": {"boolean": {}}}},
            },
        )
        answer = await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        session = answer["result"]["sessionId"]
        set_answer = await h.editor.call(
            "session/set_config_option",
            {
                "sessionId": session,
                "configId": "auto_escalate_commands",
                "type": "boolean",
                "value": True,
            },
        )
        options = set_answer["result"]["configOptions"]
        assert len(options) == 4
        values = {option["id"]: option["currentValue"] for option in options}
        assert values["auto_escalate_commands"] is True
        # Only commands are on, which is not a rung of the ladder.
        assert values[modes.MODE_OPTION_ID] == modes.CUSTOM
        assert h.calu.sent("auto_escalate.set") == [
            {"commands": True, "edits": False, "subagents": False}
        ]
        refused = await h.editor.call(
            "session/set_config_option",
            {"sessionId": session, "configId": "auto_escalate_edits", "value": "yes"},
        )
        assert refused["error"]["code"] == -32602
        assert "true or false" in refused["error"]["message"]

    await run_adapter(calu, body)


async def test_a_mode_change_by_another_client_reaches_the_editor_in_both_apis() -> None:
    """The session announces the change, so a second editor is not left showing the old mode."""
    calu = FakeCalu({"state.get": IDLE})

    async def body(h: Harness) -> None:
        await h.editor.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"session": {"configOptions": {"boolean": {}}}},
            },
        )
        await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        await h.adapter.on_event(
            "auto_escalate.changed",
            {"settings": {"commands": True, "edits": True, "subagents": False}},
        )
        updates = await h.editor.wait_updates(2)
        mode_update = next(u for u in updates if u["sessionUpdate"] == "current_mode_update")
        assert mode_update["currentModeId"] == "writes"
        config = next(u for u in updates if u["sessionUpdate"] == "config_option_update")
        values = {option["id"]: option["currentValue"] for option in config["configOptions"]}
        assert values["auto_escalate_commands"] is True
        assert values["auto_escalate_subagents"] is False
        assert h.adapter.auto == AutoEscalate(commands=True, edits=True)

    await run_adapter(calu, body)


async def test_a_mode_set_in_the_editor_reaches_a_real_session_and_comes_back(
    tmp_path: Path,
) -> None:
    """End to end except for stdio: the mode travels ACP -> calu -> session -> event -> ACP.

    The editor sets a boolean config option. The adapter turns it into `auto_escalate.set` on
    the real WebSocket protocol, the session applies it and announces `auto_escalate.changed` to
    all its clients, and the adapter turns that back into both ACP notifications -- so what the
    editor shows comes from the session, not from the adapter's own hope.
    """
    async with served(tmp_path) as run:
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
                    await editor.call(
                        "initialize",
                        {
                            "protocolVersion": 1,
                            "clientCapabilities": {"session": {"configOptions": {"boolean": {}}}},
                        },
                    )
                    created = await editor.call(
                        "session/new", {"cwd": str(tmp_path), "mcpServers": []}
                    )
                    session = created["result"]["sessionId"]
                    assert created["result"]["modes"]["currentModeId"] == "checkers"
                    await editor.call(
                        "session/set_config_option",
                        {
                            "sessionId": session,
                            "configId": "auto_escalate_edits",
                            "type": "boolean",
                            "value": True,
                        },
                    )
                    # The session itself now escalates every edit ...
                    with anyio.fail_after(10):
                        while not run.session.config.auto_escalate.edits:
                            await anyio.sleep(0.01)
                    assert run.session.config.auto_escalate.commands is False
                    # ... and told the editor so, through the event rather than the response.
                    with anyio.fail_after(10):
                        while not any(
                            update["sessionUpdate"] == "current_mode_update"
                            for update in editor.updates()
                        ):
                            await anyio.sleep(0.01)
                    mode_update = next(
                        update
                        for update in editor.updates()
                        if update["sessionUpdate"] == "current_mode_update"
                    )
                    assert mode_update["currentModeId"] == "edits"
                    tasks.cancel_scope.cancel()


async def test_an_always_choice_is_answered_once_and_the_editor_is_told(tmp_path: Path) -> None:
    """A button labelled "remember" that silently forgets would look like a bug.

    calu answers "always" like "once", because a standing rule is a database row and a session
    client has no admin credential (§6.2). The person who pressed the button learns that at
    once, in the editor, instead of by being asked again.
    """
    calu = FakeCalu({"state.get": {**IDLE, "workspace": str(WORKSPACE)}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        await h.adapter.on_event(
            "escalation.request",
            {
                "escalation_id": "7",
                "reason": "deletes a directory",
                "rendered": "$ rm -rf build",
                "tool_use_id": "call-1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf build"},
            },
        )
        question = await h.editor.expect("session/request_permission")
        assert question["params"]["sessionId"] == session
        # The editor is offered the four ACP options, including the two "always" ones.
        kinds = [option["kind"] for option in question["params"]["options"]]
        assert kinds == ["allow_once", "allow_always", "reject_once", "reject_always"]

        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow-always"}},
            }
        )
        with anyio.fail_after(10):
            while not h.calu.sent("escalation.decide"):
                await anyio.sleep(0.005)
        decided = h.calu.sent("escalation.decide")[0]
        # Allowed once, with the intent in the note for the audit trail.
        assert decided["decision"] == "allow"
        assert "allow always" in decided["note"]

        # And the editor is told, in its own stream, what calu did with the choice.
        updates = await h.editor.wait_updates(1)
        texts = [
            update["content"]["text"]
            for update in updates
            if update["sessionUpdate"] == "agent_message_chunk"
        ]
        assert any("allowed this step only" in text for text in texts), f"not said: {texts}"
        assert any("calu rules add" in text for text in texts)

    await run_adapter(calu, body)


# ------------------------------------------------------------ escalations open at attach (pc2)

OPEN_ESCALATION = {
    "escalation_id": "7",
    "tool_use_id": "t1",
    "tool_name": "Bash",
    "tool_input": {"command": "sudo ls"},
    "reason": "the deny list matched",
    "rendered": "## Step\n\n    sudo ls",
}
"""An `escalation.request`, as an event carries it and as `state.get` lists it."""


async def test_an_escalation_that_is_open_at_session_new_is_asked() -> None:
    """The session stopped on an escalation overnight; the editor is opened in the morning."""
    calu = FakeCalu(
        {
            "state.get": {
                "state": "escalated",
                "calu_session_id": "s1",
                "escalations": [OPEN_ESCALATION],
            }
        }
    )

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        question = await h.editor.expect("session/request_permission")
        assert question["params"]["sessionId"] == session
        assert question["params"]["toolCall"]["toolCallId"] == "t1"
        assert "the deny list matched" in json.dumps(question["params"]["toolCall"]["content"])
        # The tool call the dialog refers to was announced first.
        assert [u["sessionUpdate"] for u in h.editor.updates()] == ["tool_call"]
        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
            }
        )
        while not h.calu.sent("escalation.decide"):
            await anyio.sleep(0.01)
        assert h.calu.sent("escalation.decide")[0]["decision"] == "deny"

    await run_adapter(calu, body)


async def test_an_escalation_that_arrives_before_session_new_is_asked_once_the_session_exists() -> (
    None
):
    """Before `session/new` there is no session id to ask under; the question must wait, not
    be sent with `sessionId: null` and lost."""
    calu = FakeCalu({"state.get": {"state": "escalated", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await h.editor.call("initialize", {"protocolVersion": 1})
        await h.adapter.on_event("escalation.request", OPEN_ESCALATION)
        await anyio.sleep(0.05)
        assert h.editor.inbox == [], "asked before the editor had a session"
        answer = await h.editor.call("session/new", {"cwd": str(WORKSPACE), "mcpServers": []})
        question = await h.editor.expect("session/request_permission")
        assert question["params"]["sessionId"] == answer["result"]["sessionId"]
        # The same escalation in the snapshot and in the events is one dialog, not two.
        await h.adapter.on_event("escalation.request", OPEN_ESCALATION)
        await anyio.sleep(0.05)
        assert not [m for m in h.editor.inbox if m["method"] == "session/request_permission"]

    await run_adapter(calu, body)


async def test_an_escalation_resolved_before_session_new_is_not_asked() -> None:
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        await h.adapter.on_event("escalation.request", OPEN_ESCALATION)
        await h.adapter.on_event("escalation.resolved", {"escalation_id": "7", "decision": "allow"})
        await handshake(h.editor, h.calu)
        await anyio.sleep(0.05)
        assert not [m for m in h.editor.inbox if m["method"] == "session/request_permission"]

    await run_adapter(calu, body)


async def test_session_load_asks_the_open_escalations_after_the_replay() -> None:
    calu = FakeCalu(
        {
            "state.get": {"state": "escalated", "escalations": [OPEN_ESCALATION]},
            "session.history": {"turns": [{"index": 1, "user_inputs": ["hello"]}], "steps": []},
        }
    )

    async def body(h: Harness) -> None:
        await h.editor.call("initialize", {"protocolVersion": 1})
        await h.editor.call(
            "session/load", {"sessionId": "s1", "cwd": str(WORKSPACE), "mcpServers": []}
        )
        question = await h.editor.expect("session/request_permission")
        assert question["params"]["sessionId"] == "s1"
        kinds = [u["sessionUpdate"] for u in h.editor.updates()]
        assert kinds == ["user_message_chunk", "tool_call"]  # the replay, then the dialog's call

    await run_adapter(calu, body)


async def test_a_dialog_the_editor_closed_is_asked_again_with_the_next_prompt() -> None:
    """A prompt would wait behind the blocked step with nothing on screen that says so."""
    calu = FakeCalu({"state.get": {"state": "escalated", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        await h.adapter.on_event("escalation.request", OPEN_ESCALATION)
        question = await h.editor.expect("session/request_permission")
        await h.editor.send(
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        )
        await anyio.sleep(0.05)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                h.editor.call,
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "go"}]},
            )
            again = await h.editor.expect("session/request_permission")
            assert again["params"]["toolCall"]["toolCallId"] == "t1"
            await h.adapter.on_event("turn.result", {})

    await run_adapter(calu, body)


# ------------------------------------------------------------------ several prompts (pc5)


async def test_two_prompts_are_each_answered_by_their_own_turn() -> None:
    """A second prompt must not orphan the first, and a turn that ends while a prompt's
    message is still queued is not that prompt's turn."""
    calu = FakeCalu({"state.get": {"state": "idle", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answers: dict[str, dict[str, Any]] = {}
        async with anyio.create_task_group() as tasks:

            async def prompt(text: str) -> None:
                answers[text] = await h.editor.call(
                    "session/prompt",
                    {"sessionId": session, "prompt": [{"type": "text", "text": text}]},
                )

            tasks.start_soon(prompt, "first")
            while len(h.calu.sent("chat.send")) < 1:
                await anyio.sleep(0.01)
            tasks.start_soon(prompt, "second")
            while len(h.calu.sent("chat.send")) < 2:
                await anyio.sleep(0.01)
            # Claude took "first" in; "second" still waits in the session's queue.
            await h.adapter.on_event("chat.queue", {"messages": [{"id": "q2", "text": "second"}]})
            await h.adapter.on_event("turn.result", {})
            with anyio.fail_after(5):
                while "first" not in answers:
                    await anyio.sleep(0.01)
            await anyio.sleep(0.05)
            assert "second" not in answers, "answered by the turn of another message"
            await h.adapter.on_event("chat.queue", {"messages": []})
            await h.adapter.on_event("turn.result", {})
        assert answers["first"]["result"] == {"stopReason": "end_turn"}
        assert answers["second"]["result"] == {"stopReason": "end_turn"}

    await run_adapter(calu, body)


async def test_the_turn_of_another_client_does_not_answer_a_queued_prompt() -> None:
    calu = FakeCalu({"state.get": {"state": "running", "calu_session_id": "s1"}})

    async def body(h: Harness) -> None:
        session = await handshake(h.editor, h.calu)
        answers: list[dict[str, Any]] = []
        async with anyio.create_task_group() as tasks:

            async def prompt() -> None:
                answers.append(
                    await h.editor.call(
                        "session/prompt",
                        {"sessionId": session, "prompt": [{"type": "text", "text": "mine"}]},
                    )
                )

            tasks.start_soon(prompt)
            while not h.calu.sent("chat.send"):
                await anyio.sleep(0.01)
            await h.adapter.on_event("chat.queue", {"messages": [{"id": "q1", "text": "mine"}]})
            await h.adapter.on_event("turn.result", {})  # the other client's turn
            await anyio.sleep(0.05)
            assert answers == []
            await h.adapter.on_event("user.input", {"text": "mine"})
            await h.adapter.on_event("chat.queue", {"messages": []})
            await h.adapter.on_event("turn.result", {})
        assert answers[0]["result"] == {"stopReason": "end_turn"}

    await run_adapter(calu, body)


# ------------------------------------------------------------------ the bridge (pc2, pc3, pc5)


class Bridged:
    """`calu acp` without stdio: `bridge` between a real session and an in-memory editor."""

    def __init__(self, editor: Editor, adapter: Adapter) -> None:
        self.editor = editor
        self.adapter = adapter
        self.code: int | None = None


async def run_bridge(address: str, token: str, workspace: Path, body: Any) -> Bridged:
    """Run `bridge` against a served session and call `body(bridged)` next to it."""
    from calu.acp.serve import Link, bridge

    to_agent_send, to_agent_receive = anyio.create_memory_object_stream[str](100)
    from_agent_send, from_agent_receive = anyio.create_memory_object_stream[str](1000)

    async def read_line() -> str:
        try:
            return await to_agent_receive.receive()
        except anyio.EndOfStream:
            return ""

    async def write_line(line: str) -> None:
        await from_agent_send.send(line)

    link = Link()
    adapter = Adapter(link, workspace, name="calu", version="test")
    peer = Peer(read_line, write_line, adapter.handle)
    bridged = Bridged(Editor(to_agent_send, from_agent_receive), adapter)
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tasks:

            async def serve() -> None:
                bridged.code = await bridge(address, token, link, adapter, peer)

            tasks.start_soon(serve)
            tasks.start_soon(bridged.editor.pump)
            await body(bridged)
            await to_agent_send.aclose()  # the editor ends its input
            while bridged.code is None:
                await anyio.sleep(0.01)
            tasks.cancel_scope.cancel()
    return bridged


async def test_an_editor_that_attaches_to_an_escalated_session_gets_the_dialog(
    tmp_path: Path,
) -> None:
    """End to end: the session waits on an escalation before any editor exists. The adapter
    that attaches then shows the dialog, and its answer lets the session go on."""
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with served(tmp_path, turns) as run:
        from tests.unit.test_protocol_server import Events

        events = Events()
        async with connect(run.address, run.token, "the night before", events) as first:
            await first.request("session.start", {})
            await first.request("chat.send", {"text": "ship it"})
            await events.wait_for("escalation.request")

            async def body(b: Bridged) -> None:
                await b.editor.call("initialize", {"protocolVersion": 1})
                created = await b.editor.call(
                    "session/new", {"cwd": str(tmp_path), "mcpServers": []}
                )
                question = await b.editor.expect("session/request_permission")
                assert question["params"]["sessionId"] == created["result"]["sessionId"]
                await b.editor.send(
                    {
                        "jsonrpc": "2.0",
                        "id": question["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
                    }
                )
                await events.wait_for("turn.result")

            bridged = await run_bridge(run.address, run.token, tmp_path, body)
        assert bridged.code == 0
        assert [row.state for row in run.store.escalations.values()] == ["allowed"]


async def test_the_adapter_ends_and_answers_the_prompt_when_the_session_goes_away(
    tmp_path: Path,
) -> None:
    """The calu process is killed while the editor waits on a prompt: the prompt must be
    answered and the adapter must end, so the editor can start a new one."""
    from calu.protocol.server import SessionServer
    from calu.tokens import new_token, token_hash

    # The turn never ends by itself: its step waits for a decision nobody gives.
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with served(tmp_path, turns) as run:
        token = new_token()
        going_away = anyio.CancelScope()
        async with anyio.create_task_group() as outer:
            # A second server on the same session, which this test can take down by itself.
            server = SessionServer(run.session, run.server.bus, token_hash(token), outer)

            async def listen() -> None:
                with going_away:
                    await server.listen("127.0.0.1", 0)

            outer.start_soon(listen)
            with anyio.fail_after(10):
                await server.ready.wait()
            answers: list[dict[str, Any]] = []

            async def body(b: Bridged) -> None:
                await b.editor.call("initialize", {"protocolVersion": 1})
                created = await b.editor.call(
                    "session/new", {"cwd": str(tmp_path), "mcpServers": []}
                )
                session = created["result"]["sessionId"]

                async def prompt() -> None:
                    answers.append(
                        await b.editor.call(
                            "session/prompt",
                            {"sessionId": session, "prompt": [{"type": "text", "text": "go"}]},
                        )
                    )

                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(prompt)
                    await b.editor.expect("session/request_permission")
                    going_away.cancel()  # the session's server is gone
                    with anyio.fail_after(10):
                        while b.code is None:
                            await anyio.sleep(0.01)
                assert answers[0]["result"] == {"stopReason": "end_turn"}
                texts = json.dumps(b.editor.updates())
                assert "the connection to the calu session ended" in texts

            bridged = await run_bridge(server.address, token, tmp_path, body)
            assert bridged.code == 1
            outer.cancel_scope.cancel()


async def test_the_adapter_connects_again_after_falling_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that closes the connection because the adapter fell behind still runs: the
    adapter connects again, replays, and the editor's prompt ends with its turn."""
    from calu import bus as bus_module
    from calu.protocol.messages import Log

    async with served(tmp_path, [[{"text": "done"}]]) as run:

        async def body(b: Bridged) -> None:
            await b.editor.call("initialize", {"protocolVersion": 1})
            created = await b.editor.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session = created["result"]["sessionId"]
            with anyio.fail_after(5):
                while run.server.bus.clients != 1:
                    await anyio.sleep(0.01)
            monkeypatch.setattr(bus_module, "CLIENT_QUEUE", 3)
            for number in range(10):
                await run.server.bus.publish(Log(level="info", message=f"line {number}"))
            assert run.server.bus.clients == 0
            monkeypatch.setattr(bus_module, "CLIENT_QUEUE", 1000)
            with anyio.fail_after(10):  # ... and it is back
                while run.server.bus.clients != 1:
                    await anyio.sleep(0.01)
            answer = await b.editor.call(
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "go"}]},
            )
            assert answer["result"] == {"stopReason": "end_turn"}
            assert "done" in json.dumps(b.editor.updates())

        bridged = await run_bridge(run.address, run.token, tmp_path, body)
    assert bridged.code == 0
