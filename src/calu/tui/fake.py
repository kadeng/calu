"""A scripted session for the full-screen client: no process, no socket, no Claude.

`FakeSession` stands for the session side of the protocol. It keeps the events it sent (for
replay), a snapshot that follows them, and every request a client made. Tests, the screenshot
script and the demo (`python -m calu.tui`) attach the real client to it through
`fake_connector`:

    session = FakeSession()
    await session.emit("session.state", {"state": "idle"})   # live, to every attached client
    session.record("user.input", {"text": "hello"})          # history, replayed on attach
    app = CaluApp(fake_connector(session), ...)
    assert session.sent("chat.send") == [{"text": "hi", "delivery": "queued", "to": "main"}]

It answers the requests the way a session does where the client depends on it: `chat.send`
puts the message into the queue, `escalation.decide` resolves the escalation, `interrupt`
cancels the open escalations and ends the turn, `auto_escalate.set` announces the change. A
method listed in `fail` is refused with that message, which is how a test sees the client's
error handling. `drop` closes the connections with a close code.
"""

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import anyio

from calu.protocol.client import OnEvent, RequestFailed
from calu.protocol.messages import CLOSE_FELL_BEHIND, REQUESTS
from calu.tui.connection import Connection, Connector

REFUSED = -32000
"""The JSON-RPC error code of a request the session refuses."""


class FakeConnection:
    """One attached client of a `FakeSession` (fits `calu.tui.connection.Connection`)."""

    def __init__(self, session: FakeSession, on_event: OnEvent) -> None:
        """Create the connection; `fake_connector` does this.

        Args:
            session: The session it belongs to.
            on_event: The client's event callback.
        """
        self.session = session
        self.on_event = on_event
        self.last_seq = 0
        self.complete = True
        self.closed = anyio.Event()
        self.close_code: int | None = None
        self.close_reason = ""

    @property
    def fell_behind(self) -> bool:
        """Whether the session closed this connection with `CLOSE_FELL_BEHIND`.

        Returns:
            True after `FakeSession.drop(CLOSE_FELL_BEHIND, ...)`.
        """
        return self.closed.is_set() and self.close_code == CLOSE_FELL_BEHIND

    def describe_close(self) -> str:
        """Say why the connection ended.

        Returns:
            The reason given to `FakeSession.drop`, or "no reason given".
        """
        return self.close_reason or "no reason given"

    async def deliver(self, method: str, params: dict[str, Any]) -> None:
        """Pass one event to the client.

        Args:
            method: The notification method.
            params: Its params, with `seq`.
        """
        self.last_seq = max(self.last_seq, int(params.get("seq") or 0))
        await self.on_event(method, params)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Record a request and answer it as the session would.

        Args:
            method: The request method.
            params: Its params.

        Returns:
            The result (`FakeSession.handle`).

        Raises:
            RequestFailed: If the session refuses the request.
            ConnectionError: If the connection is closed.
        """
        if self.closed.is_set():
            raise ConnectionError("the connection closed before the response arrived")
        return await self.session.handle(method, dict(params or {}))


class FakeSession:
    """The session side of the protocol, scripted (see the module docstring)."""

    def __init__(self, workspace: str = "/work/myproject", project: str = "myproject") -> None:
        """Create a session without events, in the state "idle".

        Args:
            workspace: The project directory the snapshot names.
            project: The project name the snapshot names.
        """
        self.workspace = workspace
        self.project = project
        self.state = "idle"
        self.model: str | None = "opus"
        self.effort: str | None = "high"
        self.claude_session_id: str | None = "5f0c2a9e-claude"
        self.calu_session_id: str | None = "calu-7d41"
        self.agents: list[dict[str, Any]] = []
        self.escalations: dict[str, dict[str, Any]] = {}
        self.queue: list[dict[str, Any]] = []
        self.auto_escalate: dict[str, Any] = {"commands": False, "edits": False}
        self.honesty_mode = "off"
        self.honesty_marks: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        """Every event sent so far, with its `seq`, for replay."""
        self.requests: list[tuple[str, dict[str, Any]]] = []
        """Every request a client made, oldest first."""
        self.results: dict[str, Any] = {}
        """Method -> the result to answer with, for requests without a built-in reaction."""
        self.fail: dict[str, str] = {}
        """Method -> the message to refuse the request with."""
        self.connections: list[FakeConnection] = []
        self.connects = 0
        """How often a client connected."""
        self.buffer_start = 0
        """Events up to this sequence number are no longer replayed."""
        self.seq = 0
        self._next_message = 1

    # ------------------------------------------------------------------ the session's side

    def snapshot(self) -> dict[str, Any]:
        """Build the result of `state.get`.

        Returns:
            A `SessionSnapshot` as a dict.
        """
        return {
            "state": self.state,
            "claude_session_id": self.claude_session_id,
            "calu_session_id": self.calu_session_id,
            "workspace": self.workspace,
            "project": self.project,
            "model": self.model,
            "effort": self.effort,
            "last_seq": self.seq,
            "agents": [dict(agent) for agent in self.agents],
            "escalations": [dict(request) for request in self.escalations.values()],
            "queue": [dict(message) for message in self.queue],
            "auto_escalate": dict(self.auto_escalate),
            "honesty_mode": self.honesty_mode,
            "honesty_marks": [dict(mark) for mark in self.honesty_marks],
        }

    def record(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Add an event to the history without sending it, and let the snapshot follow it.

        Args:
            method: The notification method.
            params: Its params; `seq` is set here.

        Returns:
            The params with `seq`.
        """
        self.seq += 1
        stamped = {**params, "seq": self.seq}
        self.events.append((method, stamped))
        match method:
            case "session.state":
                self.state = stamped["state"]
            case "agents.update":
                self.agents = list(stamped["agents"])
            case "escalation.request":
                self.escalations[stamped["escalation_id"]] = stamped
            case "escalation.resolved":
                self.escalations.pop(stamped["escalation_id"], None)
            case "chat.queue":
                self.queue = list(stamped["messages"])
            case "auto_escalate.changed":
                self.auto_escalate = dict(stamped["settings"])
            case "honesty.update":
                self.honesty_marks = list(stamped["marks"])
                self.honesty_mode = stamped["mode"]
            case _:
                pass
        return stamped

    async def emit(self, method: str, params: dict[str, Any]) -> None:
        """Send an event to every attached client and add it to the history.

        Args:
            method: The notification method.
            params: Its params.
        """
        stamped = self.record(method, params)
        for connection in list(self.connections):
            if not connection.closed.is_set():
                await connection.deliver(method, stamped)

    def drop(self, code: int, reason: str = "") -> None:
        """Close every connection, as the session does for a slow client or a revoked token.

        Args:
            code: The WebSocket close code (`CLOSE_FELL_BEHIND`, `CLOSE_TOKEN_REVOKED`, ...).
            reason: The close reason.
        """
        for connection in self.connections:
            connection.close_code = code
            connection.close_reason = reason
            connection.closed.set()
        self.connections = []

    def sent(self, method: str) -> list[dict[str, Any]]:
        """Return the params of every request of one method.

        Args:
            method: The request method.

        Returns:
            The params, oldest first.
        """
        return [params for name, params in self.requests if name == method]

    async def take(self) -> None:
        """Let Claude take in the oldest queued message: `user.input`, then the shorter queue.

        A message for a subagent arrives as a request to relay it. Does nothing with an empty
        queue.
        """
        if not self.queue:
            return
        message, rest = self.queue[0], self.queue[1:]
        text = message["text"]
        if message["to"] != "main":
            text = f"Relay this message to the subagent {message['to']} with SendMessage:\n{text}"
        await self.emit("user.input", {"text": text, "source": "user"})
        await self.emit("chat.queue", {"messages": rest})

    # ------------------------------------------------------------------ requests

    def _refuse(self, message: str) -> RequestFailed:
        """Build the error of a refused request.

        Args:
            message: What the session says.

        Returns:
            The exception to raise.
        """
        return RequestFailed({"code": REFUSED, "message": message})

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        """Answer one request.

        Args:
            method: The request method.
            params: Its params.

        Returns:
            The result as the session would send it.

        Raises:
            RequestFailed: For a method in `fail`, an unknown method, params that do not fit
                the protocol's model, or a request the session's rules refuse (an unknown
                escalation, a message to an agent that takes none).
        """
        self.requests.append((method, params))
        if method in self.fail:
            raise self._refuse(self.fail[method])
        model = REQUESTS.get(method)
        if model is None:
            raise RequestFailed({"code": -32601, "message": f"unknown method {method}"})
        try:
            model.model_validate(params)
        except ValueError as error:
            raise RequestFailed({"code": -32602, "message": str(error)}) from error
        match method:
            case "state.get":
                return self.snapshot()
            case "chat.send":
                return await self._chat_send(params)
            case "escalation.decide":
                return await self._decide(params)
            case "escalation.context":
                return self._context(params["escalation_id"])
            case "auto_escalate.set":
                await self.emit("auto_escalate.changed", {"settings": params})
            case "interrupt":
                for escalation_id in list(self.escalations):
                    await self.emit(
                        "escalation.resolved",
                        {"escalation_id": escalation_id, "decision": "cancelled"},
                    )
                await self.emit("turn.result", {"subtype": "interrupted", "num_turns": 1})
                await self.emit("session.state", {"state": "idle"})
            case "session.stop":
                await self.emit("chat.queue", {"messages": []})
                await self.emit("session.state", {"state": "stopped"})
            case "session.start":
                self.model = params.get("model") or self.model
                self.effort = params.get("effort") or self.effort
                await self.emit("session.state", {"state": "starting"})
                await self.emit("session.state", {"state": "idle"})
            case "honesty.clear":
                kept = [mark for mark in self.honesty_marks if mark["id"] != params["mark_id"]]
                if len(kept) == len(self.honesty_marks):
                    raise self._refuse(f"no open mark {params['mark_id']}")
                await self.emit("honesty.update", {"marks": kept, "mode": self.honesty_mode})
            case "shutdown":
                self.drop(1000, "calu is shutting down")
            case _:
                return self.results.get(method, {})
        return {}

    async def _chat_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Queue a chat message.

        Args:
            params: The `chat.send` params.

        Returns:
            `{}`.

        Raises:
            RequestFailed: If no session runs or the target is not a running background
                subagent.
        """
        if self.state in {"stopped", "failed"}:
            raise self._refuse("no Claude session is running")
        target = params.get("to", "main")
        if target != "main":
            agent = next((a for a in self.agents if a["agent_id"] == target), None)
            if agent is None or not agent.get("background") or agent.get("status") != "running":
                raise self._refuse(f"{target} is not a running background subagent")
        message = {
            "id": f"q{self._next_message}",
            "text": params["text"],
            "delivery": params.get("delivery", "queued"),
            "to": target,
        }
        self._next_message += 1
        await self.emit("chat.queue", {"messages": [*self.queue, message]})
        return {}

    async def _decide(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve an escalation.

        Args:
            params: The `escalation.decide` params.

        Returns:
            `{}`.

        Raises:
            RequestFailed: If the escalation is not open.
        """
        escalation_id = params["escalation_id"]
        if escalation_id not in self.escalations:
            raise self._refuse(f"no open escalation {escalation_id}")
        answer = params.get("answer")
        resolved: dict[str, Any] = {
            "escalation_id": escalation_id,
            "decision": "deny" if answer else params.get("decision", "deny"),
            "note": f"The user answered: {answer}" if answer else params.get("note"),
        }
        await self.emit("escalation.resolved", resolved)
        if not self.escalations and self.state == "escalated":
            await self.emit("session.state", {"state": "running"})
        return {}

    def _context(self, escalation_id: str) -> dict[str, Any]:
        """Build the result of `escalation.context` for an escalation of this session.

        Args:
            escalation_id: The escalation.

        Returns:
            What `results["escalation.context"]` holds, laid over the stored request.

        Raises:
            RequestFailed: If the session never sent that escalation.
        """
        request = next(
            (
                params
                for method, params in self.events
                if method == "escalation.request" and params["escalation_id"] == escalation_id
            ),
            None,
        )
        if request is None:
            raise self._refuse(f"no escalation {escalation_id}")
        return {
            "escalation_id": escalation_id,
            "state": "open" if escalation_id in self.escalations else "allowed",
            "reason": request["reason"],
            "rendered": request["rendered"],
            "tool_name": request["tool_name"],
            "tool_input": request["tool_input"],
            "agent_id": request.get("agent_id"),
            **self.results.get("escalation.context", {}),
        }


def fake_connector(session: FakeSession) -> Connector:
    """Return a connector that attaches a client to a `FakeSession`.

    Args:
        session: The scripted session.

    Returns:
        A connector whose context manager replays the session's events after `after_seq`
        (those after `session.buffer_start`; `complete` is False if some are missing) and then
        yields the connection. Leaving the context closes the connection.
    """

    @asynccontextmanager
    async def attach(on_event: OnEvent, after_seq: int) -> AsyncGenerator[Connection]:
        """Open one fake connection.

        Args:
            on_event: The client's event callback.
            after_seq: Sequence number to continue after.

        Yields:
            The connection.
        """
        connection = FakeConnection(session, on_event)
        session.connects += 1
        connection.complete = after_seq >= session.buffer_start
        for method, params in list(session.events):
            if params["seq"] > max(after_seq, session.buffer_start):
                await connection.deliver(method, params)
        connection.last_seq = max(connection.last_seq, after_seq)
        session.connections.append(connection)
        try:
            yield connection
        finally:
            connection.closed.set()
            if connection in session.connections:
                session.connections.remove(connection)

    def open_connection(
        on_event: OnEvent, after_seq: int
    ) -> AbstractAsyncContextManager[Connection]:
        """Open one fake connection (the `Connector` signature).

        Args:
            on_event: The client's event callback.
            after_seq: Sequence number to continue after.

        Returns:
            The context manager of the connection.
        """
        return attach(on_event, after_seq)

    return open_connection
