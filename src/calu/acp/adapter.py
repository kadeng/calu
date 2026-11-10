"""Translation between the Agent Client Protocol and calu's own protocol (PLAN.md D24, §6.1).

    editor ──ACP over stdio──► calu acp ──WebSocket, JSON-RPC──► calu session ──► Claude Code

The adapter is a *client* of a running calu session, not a second session runner: the session
keeps running in its terminal when the editor closes, and several editors may attach at once,
because calu's bus does the fan-out. The adapter therefore holds no database connection and
imports nothing of the session runner.

What maps onto what:

| ACP | calu |
|---|---|
| `session/new` | attaches to the running session; its calu session id becomes the ACP `sessionId` |
| `session/load` | replays `session.history` from the database as `session/update` notifications |
| `session/prompt` | `chat.send`, then waits for `turn.result` |
| `session/cancel` | `interrupt`; the prompt answers `stopReason: "cancelled"` |
| `agent_message_chunk`, `agent_thought_chunk` | `assistant.delta`, `assistant.thinking` |
| `tool_call`, `tool_call_update` | `tool.use`, `check.result`, `edit.diff`, `tool.result` |
| `session/request_permission` | `escalation.request` -> `escalation.decide` |

Deliberately not translated, because ACP has no slot for them and a calu-aware client reads them
from calu's own protocol instead (§6.2): the checker verdicts as structure (they travel as tool
call content here), the herd overview, the checker definitions and the chat summary. Events
without an ACP counterpart (`chat.queue`, `tool.files`, `agents.update`) are dropped, and `log`
goes to the adapter's own log, not to the editor.
"""

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import anyio

from calu.acp import messages as acp
from calu.acp import modes
from calu.acp.peer import Peer, RpcError
from calu.config import AutoEscalate

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 20_000
"""Characters of a tool result that are passed on to the editor."""

TITLE_CHARS = 200
"""Characters of a tool call title."""

ECHO_MEMORY = 8
"""How many of our own prompts are remembered, to drop the session's echo of them."""

DECISIONS: dict[str, tuple[str, str]] = {
    "allow-once": ("allow", ""),
    "allow-always": ("allow", 'the client chose "allow always"'),
    "reject-once": ("deny", ""),
    "reject-always": ("deny", 'the client chose "reject always"'),
}
"""Option id of `session/request_permission` -> (calu decision, note).

ACP leaves the scope of "always" to the agent. calu answers it like "once" and records the
intent in the note, because a rule row needs the admin credential, which a session client does
not have (§6.2). The note is part of the audit trail, so the choice is not lost.
"""

ALWAYS_HINT = (
    "[calu allowed this step only. Your choice to remember it is recorded with the escalation, "
    "but a standing rule is a database row that needs the admin credential, which an editor "
    "does not have: add it with `calu rules add command-allow '<pattern>'`.]"
)
"""What the editor is told after an "always" choice, because calu answered it like "once".

A person who presses a button labelled "remember" and is asked the same thing again next time
would reasonably think calu is broken. Saying it at the moment of the choice costs one line.
"""


class CaluSession(Protocol):
    """The part of `calu.protocol.client.ProtocolClient` that the adapter uses."""

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request to the calu session and wait for its result.

        Args:
            method: A calu request method, e.g. "chat.send".
            params: Its params.

        Returns:
            The result of the request.
        """
        ...


class Turn:
    """The prompt turn a `session/prompt` call waits for."""

    def __init__(self) -> None:
        """Create a turn that nothing has finished yet."""
        self.done = anyio.Event()
        self.stop_reason: acp.StopReason = "end_turn"

    def finish(self, reason: acp.StopReason) -> None:
        """Finish the turn, unless it is already finished.

        Args:
            reason: The ACP stop reason to answer the prompt with.
        """
        if not self.done.is_set():
            self.stop_reason = reason
            self.done.set()


class Adapter:
    """Translates one editor's ACP connection to one calu session.

    Lifecycle: create it, create the `Peer` with `handle` as its handler, then `attach` the peer
    and a task group, then run the peer and feed the session's events to `on_event`.
    """

    def __init__(
        self, calu: CaluSession, workspace: Path, name: str = "calu", version: str = ""
    ) -> None:
        """Create the adapter.

        Args:
            calu: The connected calu session client.
            workspace: The session's project directory; `session/new` checks that the editor's
                `cwd`
                against it, because this adapter can only serve that one directory.
            name: Name of the agent as the editor shows it.
            version: Version of the adapter.
        """
        self.calu = calu
        self.workspace = workspace
        self.name = name
        self.version = version
        self.session_id: str | None = None
        """The ACP session id handed to the client; None before `session/new`."""
        self.state: str = "starting"
        """Last state the calu session reported."""
        self.auto = AutoEscalate()
        """Auto-escalation categories of the session, which ACP shows as its session mode."""
        self.boolean_options = False
        """Whether the client takes boolean config options (set by `initialize`)."""
        self._peer: Peer | None = None
        self._tasks: anyio.abc.TaskGroup | None = None
        self._turn: Turn | None = None
        self._deltas: set[str] = set()
        self._calls: dict[str, dict[str, Any]] = {}
        self._content: dict[str, list[dict[str, Any]]] = {}
        self._resolved: set[str] = set()
        self._echo: deque[str] = deque(maxlen=ECHO_MEMORY)

    def attach(self, peer: Peer, tasks: anyio.abc.TaskGroup) -> None:
        """Give the adapter the peer it answers on and a task group for its own tasks.

        Args:
            peer: The ACP peer; the adapter sends notifications and permission requests on it.
            tasks: Task group for the permission requests, which must not block the event
                stream (one escalation may wait hours for a human).
        """
        self._peer = peer
        self._tasks = tasks

    # ------------------------------------------------------------------ ACP -> calu

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        """Handle one inbound ACP request or notification.

        Args:
            method: The ACP method name.
            params: Its params.

        Returns:
            The result of the request (None for a notification).

        Raises:
            RpcError: If the method is unknown, the params do not fit, or the session id is not
                the one this adapter serves.
        """
        match method:
            case "initialize":
                return self._initialize(params)
            case "authenticate":
                return {}
            case "session/new":
                return await self._new_session(params)
            case "session/load":
                return await self._load_session(params)
            case "session/prompt":
                return await self._prompt(params)
            case "session/set_mode":
                return await self._set_mode(params)
            case "session/set_config_option":
                return await self._set_config_option(params)
            case "session/cancel":
                await self._cancel(params)
                return None
            case "session/close":
                await self._cancel(params)
                return {}
            case _:
                raise RpcError(acp.METHOD_NOT_FOUND, f"calu does not implement {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `initialize` and note what the client offers.

        The client's file system and terminal capabilities are logged and then ignored: using
        them would route tool calls around the checkers and the sandbox (§6.1).

        Args:
            params: The `initialize` params.

        Returns:
            The `initialize` result.
        """
        request = acp.InitializeParams.model_validate(params)
        if request.protocol_version != acp.PROTOCOL_VERSION:
            log.info(
                "the client speaks ACP %d, this adapter %d; answering with ours",
                request.protocol_version,
                acp.PROTOCOL_VERSION,
            )
        offered = request.client_capabilities
        if offered.fs.read_text_file or offered.fs.write_text_file or offered.terminal:
            log.info(
                "ignoring the client's fs and terminal capabilities: Claude Code does its own "
                "file and command work inside the sandbox"
            )
        self.boolean_options = offered.boolean_config_options
        return acp.initialize_result(self.name, self.version)

    async def _new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/new` by attaching to the running calu session.

        No Claude session is created here: the adapter serves the session that `calu run`
        started. The editor's `cwd` must be that session's project directory or a directory
        inside it.

        Args:
            params: The `session/new` params.

        Returns:
            `{"sessionId": ...}` with the calu session id.

        Raises:
            RpcError: If `cwd` is outside the session's project directory.
        """
        request = acp.NewSessionParams.model_validate(params)
        self._check_cwd(request.cwd)
        if request.mcp_servers:
            log.warning(
                "ignoring %d MCP server(s) from the editor: calu does not add tools to the "
                "session that the project's configuration does not have",
                len(request.mcp_servers),
            )
        snapshot = await self.calu.request("state.get")
        self.state = str(snapshot.get("state", "starting"))
        self.session_id = str(snapshot.get("calu_session_id") or "calu")
        self._read_auto(snapshot)
        return {"sessionId": self.session_id, **self._mode_fields()}

    async def _load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/load` by replaying the recorded conversation.

        The protocol requires the whole conversation to be sent as `session/update`
        notifications **before** this returns, so a reconnecting editor shows the history. It
        comes from the database (`session.history`), because the event buffer of the bus only
        covers the running process.

        Args:
            params: The `session/load` params.

        Returns:
            An empty result, after the replay.

        Raises:
            RpcError: If `cwd` is outside the session's project directory, or the session id
                is not
                the one this adapter serves.
        """
        request = acp.LoadSessionParams.model_validate(params)
        self._check_cwd(request.cwd)
        if self.session_id is not None and request.session_id != self.session_id:
            raise RpcError(acp.UNKNOWN_SESSION, f"calu does not serve {request.session_id}")
        self.session_id = request.session_id
        snapshot = await self.calu.request("state.get")
        self.state = str(snapshot.get("state", self.state))
        self._read_auto(snapshot)
        history = await self.calu.request("session.history", {})
        await self._replay(history)
        return self._mode_fields()

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/prompt`: send the message to Claude and wait for the turn to end.

        Starts the Claude session first if it is not running, so that an editor can begin a
        conversation in a calu process whose session was stopped (for example after the
        escalation timeout of D27).

        Args:
            params: The `session/prompt` params.

        Returns:
            `{"stopReason": ...}` once the turn ended.

        Raises:
            RpcError: If the session id is unknown or the prompt carries no text calu can send.
        """
        request = acp.PromptParams.model_validate(params)
        self._require_session(request.session_id)
        text = acp.prompt_text(request.prompt)
        if not text.strip():
            raise RpcError(acp.INVALID_PARAMS, "the prompt holds no text")
        if self.state in ("stopped", "failed"):
            await self.calu.request("session.start", {})
        turn = Turn()
        self._turn = turn
        self._echo.append(text)
        await self.calu.request("chat.send", {"text": text, "delivery": "queued"})
        await turn.done.wait()
        self._turn = None
        return {"stopReason": turn.stop_reason}

    async def _cancel(self, params: dict[str, Any]) -> None:
        """Handle `session/cancel` (and `session/close`): interrupt Claude's turn.

        The pending prompt answers `cancelled`, as the protocol requires.

        Args:
            params: The params holding the session id.
        """
        request = acp.SessionIdParams.model_validate(params)
        if self.session_id is not None and request.session_id != self.session_id:
            log.warning("ignoring a cancel for the unknown session %s", request.session_id)
            return
        if self._turn is not None:
            self._turn.finish("cancelled")
        await self.calu.request("interrupt", {})

    def _check_cwd(self, cwd: str) -> None:
        """Check that the editor's directory is inside the project this adapter serves.

        The session's project directory itself is accepted, and so is a directory below it: an
        editor opened on one package of a repository is still working inside the project whose
        rules and checkers the session enforces, and `calu acp` with no address finds that
        session from a subdirectory for the same reason (PLAN.md D105). A directory **outside**
        the project is refused, because the editor would then act on files the session does not
        cover.

        Args:
            cwd: The absolute path the editor sent.

        Raises:
            RpcError: If it is neither the session's project directory nor below it.
        """
        here = Path(cwd).resolve()
        workspace = self.workspace.resolve()
        if here != workspace and not here.is_relative_to(workspace):
            raise RpcError(
                acp.INVALID_PARAMS,
                f"this calu session serves {self.workspace}, which does not contain {cwd}; "
                "start a calu session in that directory and attach to it",
            )

    def _require_session(self, session_id: str) -> None:
        """Check that a request names the session this adapter serves.

        Args:
            session_id: The id from the request.

        Raises:
            RpcError: If no session was created yet, or the id is a different one.
        """
        if self.session_id is None:
            raise RpcError(acp.UNKNOWN_SESSION, "no session: call session/new first")
        if session_id != self.session_id:
            raise RpcError(acp.UNKNOWN_SESSION, f"calu does not serve {session_id}")

    # ------------------------------------------------------------------ session modes

    def _read_auto(self, snapshot: dict[str, Any]) -> None:
        """Take the auto-escalation categories out of a `state.get` snapshot.

        A snapshot from an older session process may not carry them; the categories then stay
        as they are, which is the default "the checkers decide".

        Args:
            snapshot: The result of `state.get`.
        """
        found = snapshot.get("auto_escalate")
        if isinstance(found, dict):
            self.auto = AutoEscalate.model_validate(found)

    def _mode_fields(self) -> dict[str, Any]:
        """Build the mode fields of a `session/new` or `session/load` result.

        Both ACP APIs are answered: `configOptions` (which supersedes modes) and `modes` (which
        older clients use), as the specification asks during the transition.

        Returns:
            `{"modes": ..., "configOptions": ...}` for the session's current categories.
        """
        return {
            "modes": modes.mode_state(self.auto),
            "configOptions": modes.config_options(self.auto, self.boolean_options),
        }

    async def _apply_auto(self, auto: AutoEscalate) -> None:
        """Send new auto-escalation categories to the calu session.

        The session answers every attached client with `auto_escalate.changed`, and this
        adapter's own `on_event` turns that into the ACP notifications -- so nothing is
        notified here, and the editor hears the change the same way another editor does.

        Args:
            auto: The categories the client asked for.

        Raises:
            RpcError: If the session refuses the request.
        """
        await self.calu.request("auto_escalate.set", auto.model_dump())

    async def _set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/set_mode` (the older of the two APIs).

        Args:
            params: The `session/set_mode` params.

        Returns:
            An empty result; the mode change arrives as a notification.

        Raises:
            RpcError: If the session id is not the one this adapter serves, or the mode is not
                one calu offers.
        """
        request = acp.SetModeParams.model_validate(params)
        self._require_session(request.session_id)
        try:
            auto = modes.settings_for(request.mode_id)
        except ValueError as error:
            raise RpcError(acp.INVALID_PARAMS, str(error)) from error
        await self._apply_auto(auto)
        return {}

    async def _set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/set_config_option` (the API that supersedes modes).

        ACP requires the complete option list in the result, not only the option that changed,
        so that an agent can report options a change pulled along with it.

        Args:
            params: The `session/set_config_option` params.

        Returns:
            `{"configOptions": [...]}` with every option and its new value.

        Raises:
            RpcError: If the session id is not the one this adapter serves, or the option or its
                value is not one calu offers.
        """
        request = acp.SetConfigOptionParams.model_validate(params)
        self._require_session(request.session_id)
        try:
            auto = modes.with_option(self.auto, request.config_id, request.value)
        except ValueError as error:
            raise RpcError(acp.INVALID_PARAMS, str(error)) from error
        await self._apply_auto(auto)
        # The session's `auto_escalate.changed` event may not have arrived yet, and the result
        # must already hold the new values, so answer from what was asked for.
        self.auto = auto
        return {"configOptions": modes.config_options(auto, self.boolean_options)}

    async def _on_auto_escalate(self, params: dict[str, Any]) -> None:
        """Mirror a change of the auto-escalation categories to the editor.

        The change may come from this editor, from another client, or from a reloaded
        configuration; all of them reach every client as `auto_escalate.changed`. Both ACP
        notifications are sent, because a client may understand only one of the two APIs.

        Args:
            params: The `auto_escalate.changed` event.
        """
        found = params.get("settings")
        if not isinstance(found, dict):
            return
        self.auto = AutoEscalate.model_validate(found)
        await self._update(modes.mode_update(self.auto))
        await self._update(modes.config_update(self.auto, self.boolean_options))

    # ------------------------------------------------------------------ calu -> ACP

    async def on_event(self, method: str, params: dict[str, Any]) -> None:
        """Translate one event of the calu session into ACP updates.

        Events without an ACP counterpart are dropped (see the module docstring).

        Args:
            method: The calu event method, e.g. "tool.use".
            params: The event's params.
        """
        match method:
            case "session.state":
                await self._on_state(params)
            case "assistant.delta":
                message_id = str(params.get("message_id") or "")
                if message_id:
                    self._deltas.add(message_id)
                await self._update(
                    acp.message_chunk("agent_message_chunk", _text(params), message_id)
                )
            case "assistant.text":
                message_id = str(params.get("message_id") or "")
                if message_id not in self._deltas:
                    await self._update(
                        acp.message_chunk("agent_message_chunk", _text(params), message_id)
                    )
            case "assistant.thinking":
                await self._update(acp.message_chunk("agent_thought_chunk", _text(params)))
            case "user.input":
                await self._on_user_input(params)
            case "tool.use":
                await self._on_tool_use(params)
            case "check.result":
                await self._on_check(params)
            case "edit.diff":
                await self._on_diff(params)
            case "tool.result":
                await self._on_result(params)
            case "escalation.request":
                self._ask(params)
            case "escalation.resolved":
                await self._on_resolved(params)
            case "turn.result":
                await self._on_turn_result(params)
            case "auto_escalate.changed":
                await self._on_auto_escalate(params)
            case "honesty.update":
                await self._on_honesty(params)
            case "log":
                log.log(
                    _LEVELS.get(str(params.get("level", "info")), logging.INFO),
                    "session: %s",
                    params.get("message", ""),
                )

    async def _on_honesty(self, params: dict[str, Any]) -> None:
        """Tell the editor what the honesty register holds (PLAN.md D86).

        ACP has no shape for "this code may not be run", so it goes as a message chunk, the way
        the "always" hint does: while the register stands, commands that would run that code are
        refused, and a refusal the editor cannot explain looks arbitrary. Only a change is sent,
        because the event is only emitted when a checker changed the register.

        Args:
            params: The `honesty.update` event.
        """
        marks = params.get("marks") or []
        if not isinstance(marks, list):
            return
        if not marks:
            text = "[calu: the honesty register is clear again.]"
        else:
            spots = ", ".join(
                str(mark.get("path", "")) + (f":{mark['symbol']}" if mark.get("symbol") else "")
                for mark in marks
                if isinstance(mark, dict)
            )
            verb = (
                "cannot be run until the code and its documentation agree"
                if params.get("mode") == "enforce"
                else "is recorded as dishonest (not blocked: honesty.mode is 'track')"
            )
            text = f"[calu: {spots} {verb}. `calu honesty list` shows why.]"
        await self._update(acp.message_chunk("agent_message_chunk", f"{text}\n", None))

    async def _on_state(self, params: dict[str, Any]) -> None:
        """Remember the session state and end a waiting prompt if the session ended.

        Args:
            params: The `session.state` event.
        """
        self.state = str(params.get("state", self.state))
        if self.state in ("stopped", "failed") and self._turn is not None:
            await self._update(
                acp.message_chunk(
                    "agent_message_chunk", f"[the calu session is {self.state}]", None
                )
            )
            self._turn.finish("end_turn")

    async def _on_user_input(self, params: dict[str, Any]) -> None:
        """Forward a user message of another client, but not the echo of our own prompt.

        The editor already shows what its user typed, so echoing our own `chat.send` back would
        duplicate it. Messages typed in another client (or relayed from another session) are
        forwarded, so the editor shows the whole conversation.

        Args:
            params: The `user.input` event.
        """
        text = _text(params)
        if text in self._echo:
            self._echo.remove(text)
            return
        await self._update(acp.message_chunk("user_message_chunk", text))

    async def _on_tool_use(self, params: dict[str, Any]) -> None:
        """Announce a tool call to the editor, with whatever content already arrived for it.

        `edit.diff` may arrive before `tool.use`, so content buffered under this call's id is
        sent right after the announcement.

        Args:
            params: The `tool.use` event.
        """
        call_id = str(params.get("tool_use_id") or "")
        if not call_id:
            return
        tool_input = params.get("input") or {}
        tool_name = str(params.get("name", ""))
        self._calls[call_id] = {"name": tool_name, "input": tool_input}
        await self._update(
            acp.tool_call(
                call_id,
                _title(tool_name, tool_input),
                tool_name,
                raw_input=tool_input if isinstance(tool_input, dict) else None,
                locations=_locations(tool_input),
            )
        )
        if self._content.get(call_id):
            await self._update(acp.tool_call_update(call_id, content=self._content[call_id]))

    async def _on_check(self, params: dict[str, Any]) -> None:
        """Show the checker's verdict on a tool call.

        The step summary becomes the call's title, because it says what the step does better
        than the command does; the verdict, its reason and any remark become content, since ACP
        has no field for a second opinion.

        Args:
            params: The `check.result` event.
        """
        call_id = str(params.get("tool_use_id") or "")
        if not call_id:
            return
        decision = str(params.get("decision", ""))
        lines = [f"checker: {decision}"]
        for key in ("reason", "comment"):
            value = str(params.get(key) or "").strip()
            if value:
                lines.append(f"{key}: {value}")
        summary = str(params.get("step_summary") or "").strip()
        status: acp.ToolCallStatus = "failed" if decision == "DENY" else "in_progress"
        await self._update(
            acp.tool_call_update(
                call_id,
                title=_cut(summary) if summary else None,
                status=status,
                content=self._add_content(call_id, acp.text_content("\n".join(lines))),
            )
        )

    async def _on_diff(self, params: dict[str, Any]) -> None:
        """Add the before/after content of an edit, which the editor shows as a diff.

        Args:
            params: The `edit.diff` event.
        """
        call_id = str(params.get("tool_use_id") or "")
        path = str(params.get("path", ""))
        if not call_id or not path:
            return
        content = self._add_content(
            call_id,
            acp.diff_content(path, str(params.get("before", "")), str(params.get("after", ""))),
        )
        if call_id in self._calls:
            await self._update(acp.tool_call_update(call_id, content=content))

    async def _on_result(self, params: dict[str, Any]) -> None:
        """Finish a tool call in the editor's view.

        Args:
            params: The `tool.result` event.
        """
        call_id = str(params.get("tool_use_id") or "")
        if not call_id:
            return
        text = str(params.get("content", ""))[:MAX_RESULT_CHARS]
        failed = bool(params.get("is_error"))
        content = self._add_content(call_id, acp.text_content(text)) if text else None
        await self._update(
            acp.tool_call_update(
                call_id, status="failed" if failed else "completed", content=content
            )
        )

    async def _on_resolved(self, params: dict[str, Any]) -> None:
        """Note that an escalation ended, whoever ended it.

        A decision made in another client reaches us here. ACP cannot retract a permission
        request, so a dialog that is already open in this editor stays open and its late answer
        is dropped (`_ask`).

        Args:
            params: The `escalation.resolved` event.
        """
        escalation_id = str(params.get("escalation_id", ""))
        if escalation_id:
            self._resolved.add(escalation_id)

    async def _on_turn_result(self, params: dict[str, Any]) -> None:
        """End the prompt turn the editor is waiting for.

        Args:
            params: The `turn.result` event.
        """
        if bool(params.get("is_error")):
            detail = str(params.get("result") or params.get("subtype") or "the turn failed")
            await self._update(acp.message_chunk("agent_message_chunk", f"[{detail}]"))
        if self._turn is not None:
            self._turn.finish("end_turn")

    # ------------------------------------------------------------------ escalations

    def _ask(self, params: dict[str, Any]) -> None:
        """Start asking the editor's user about an escalated step.

        The question runs in its own task: a human may take hours, while the event stream and
        `session/cancel` must keep working.

        Args:
            params: The `escalation.request` event.
        """
        if self._tasks is None:
            log.warning(
                "no task group: cannot ask about escalation %s", params.get("escalation_id")
            )
            return
        self._tasks.start_soon(self._ask_permission, params)

    async def _ask_permission(self, params: dict[str, Any]) -> None:
        """Ask the editor's user about an escalated step and pass the answer to calu.

        Nothing is decided if the user's client answers `cancelled`, or if the escalation was
        already resolved elsewhere: calu refuses a second decision anyway, and a cancelled
        dialog is not a decision.

        Args:
            params: The `escalation.request` event.
        """
        escalation_id = str(params.get("escalation_id", ""))
        call_id = str(params.get("tool_use_id") or f"escalation-{escalation_id}")
        tool_name = str(params.get("tool_name", ""))
        if call_id not in self._calls:
            self._calls[call_id] = {"name": tool_name, "input": params.get("tool_input") or {}}
            await self._update(
                acp.tool_call(
                    call_id,
                    _title(tool_name, params.get("tool_input") or {}),
                    tool_name,
                    raw_input=params.get("tool_input") or None,
                )
            )
        reason = str(params.get("reason") or "")
        rendered = str(params.get("rendered") or "")
        question = "\n\n".join(part for part in (f"escalated: {reason}", rendered) if part.strip())
        content = self._add_content(call_id, acp.text_content(question))
        try:
            answer = await self._ask_client(call_id, content)
        except (RpcError, ConnectionError) as error:
            log.warning("the editor did not answer the permission request: %s", error)
            return
        outcome = (answer or {}).get("outcome") or {}
        if str(outcome.get("outcome")) != "selected":
            log.info("the editor cancelled the permission request for %s", escalation_id)
            return
        decision, note = DECISIONS.get(str(outcome.get("optionId")), ("", ""))
        if not decision:
            log.warning("the editor chose the unknown option %r", outcome.get("optionId"))
            return
        if escalation_id in self._resolved:
            log.info("escalation %s was already resolved elsewhere", escalation_id)
            return
        try:
            await self.calu.request(
                "escalation.decide",
                {"escalation_id": escalation_id, "decision": decision, "note": note or None},
            )
        except Exception as error:  # the session refuses a decided or cancelled escalation
            log.warning("calu refused the decision for %s: %s", escalation_id, error)
            return
        if note:
            # "Always" was answered like "once" (see `DECISIONS`). The person clicked a button
            # that says "remember", so they are told what really happened, in the editor, at
            # the moment they chose -- otherwise they find out by being asked again.
            await self._update(acp.message_chunk("agent_message_chunk", f"{ALWAYS_HINT}\n", None))

    async def _ask_client(self, call_id: str, content: list[dict[str, Any]]) -> Any:
        """Send the `session/request_permission` request to the editor.

        Args:
            call_id: The tool call the question is about.
            content: What the checker said, as tool call content.

        Returns:
            The result of the request.

        Raises:
            RpcError: If the editor answered with an error.
            ConnectionError: If the connection ended before the answer arrived.
        """
        if self._peer is None:
            raise ConnectionError("the adapter is not attached to a peer")
        return await self._peer.request(
            "session/request_permission",
            {
                "sessionId": self.session_id,
                "toolCall": {"toolCallId": call_id, "content": content},
                "options": acp.permission_options(),
            },
        )

    # ------------------------------------------------------------------ replay and sending

    async def _replay(self, history: dict[str, Any]) -> None:
        """Send a recorded conversation as `session/update` notifications.

        The turns carry the user's messages and Claude's answers, the steps the tool calls; a
        step is shown with its checker summary and the beginning of its result, which is what
        the database keeps (the full output is not recorded, D3).

        Args:
            history: The result of `session.history`.
        """
        steps = history.get("steps") or []
        by_turn: dict[Any, list[dict[str, Any]]] = {}
        for step in steps:
            by_turn.setdefault(step.get("turn"), []).append(step)
        for turn in history.get("turns") or []:
            for text in turn.get("user_inputs") or []:
                await self._update(acp.message_chunk("user_message_chunk", str(text)))
            for step in by_turn.pop(turn.get("index"), []):
                await self._replay_step(step)
            answer = str(turn.get("answer") or "")
            if answer:
                await self._update(acp.message_chunk("agent_message_chunk", answer))
        for remaining in by_turn.values():
            for step in remaining:
                await self._replay_step(step)

    async def _replay_step(self, step: dict[str, Any]) -> None:
        """Send one recorded step as a finished tool call.

        Args:
            step: A `StoredStep` as `session.history` returned it.
        """
        tool_name = str(step.get("tool_name", ""))
        tool_input = step.get("tool_input") or {}
        call_id = str(step.get("tool_use_id") or f"step-{step.get('seq')}")
        summary = str(step.get("summary") or "").strip()
        await self._update(
            acp.tool_call(
                call_id,
                _cut(summary) if summary else _title(tool_name, tool_input),
                tool_name,
                status="completed",
                raw_input=tool_input if isinstance(tool_input, dict) else None,
                locations=_locations(tool_input),
            )
        )
        parts = [
            f"{key}: {step.get(key)}"
            for key in ("decision", "reason", "result_summary")
            if str(step.get(key) or "").strip()
        ]
        if parts:
            await self._update(
                acp.tool_call_update(call_id, content=[acp.text_content("\n".join(parts))])
            )

    def _add_content(self, call_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Add one content entry to a tool call and return the call's whole content.

        A `tool_call_update` replaces the content collection rather than appending to it, so
        every update has to carry everything the call has shown so far.

        Args:
            call_id: The tool call.
            entry: The new content entry.

        Returns:
            All content entries of the call, oldest first.
        """
        entries = self._content.setdefault(call_id, [])
        entries.append(entry)
        return entries

    async def _update(self, update: dict[str, Any]) -> None:
        """Send one `session/update` notification.

        Nothing is sent before `session/new`, because an update needs a session id.

        Args:
            update: The `update` object.
        """
        if self._peer is None or self.session_id is None:
            return
        await self._peer.notify("session/update", {"sessionId": self.session_id, "update": update})


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
"""Level name of a `log` event -> logging level."""


EDIT_PATHS = ("file_path", "notebook_path", "path")
"""Keys of a tool input that name the file a call works on."""


def _text(params: dict[str, Any]) -> str:
    """Return the text of an event that carries one.

    Args:
        params: The event's params.

    Returns:
        The "text" field as a string ("" if absent).
    """
    return str(params.get("text", ""))


def _cut(text: str, limit: int = TITLE_CHARS) -> str:
    """Put text on one line and cut it to a title's length.

    The adapter does this itself instead of reusing `calu.run.one_line`, because importing the
    session runner would pull the database and the Claude Agent SDK into a client process.

    Args:
        text: The text.
        limit: Maximum number of characters.

    Returns:
        The single-line text, with an ellipsis if it was cut.
    """
    joined = " ".join(text.split())
    return joined if len(joined) <= limit else joined[: limit - 4] + " ..."


def _title(tool_name: str, tool_input: Any) -> str:
    """Build the title of a tool call, before a checker has summarized it.

    Args:
        tool_name: Claude Code's tool name.
        tool_input: The tool input as Claude sent it.

    Returns:
        One line: the command for Bash, the path for a file tool, else the tool's name.
    """
    if not isinstance(tool_input, dict):
        return tool_name or "tool call"
    if tool_name == "Bash" and tool_input.get("command"):
        return _cut(str(tool_input["command"]))
    for key in EDIT_PATHS:
        if tool_input.get(key):
            return _cut(f"{tool_name}: {tool_input[key]}")
    if tool_name in ("Agent", "Task") and tool_input.get("description"):
        return _cut(f"subagent: {tool_input['description']}")
    return tool_name or "tool call"


def _locations(tool_input: Any) -> list[str]:
    """Return the absolute paths a tool call refers to, for the editor to follow along.

    Args:
        tool_input: The tool input as Claude sent it.

    Returns:
        The paths of the input's file keys that are absolute (ACP requires absolute paths).
    """
    if not isinstance(tool_input, dict):
        return []
    found = [str(tool_input[key]) for key in EDIT_PATHS if tool_input.get(key)]
    return [path for path in found if path.startswith("/")]


Serve = Callable[[], Awaitable[None]]
"""A callable that runs an ACP peer until its input ends."""
