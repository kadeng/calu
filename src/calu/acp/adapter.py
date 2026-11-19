"""Translation between the Agent Client Protocol and calu's own protocol.

See agent/records/PLAN.md D24, §6.1.

    editor ──ACP over stdio──► calu acp ──WebSocket, JSON-RPC──► calu session ──► Claude Code

The adapter is a *client* of a running calu session, not a second session runner: the session
keeps running in its terminal when the editor closes, and several editors may attach at once,
because calu's bus does the fan-out. The adapter therefore holds no database connection and
imports nothing of the session runner.

What maps onto what:

| ACP | calu |
|---|---|
| `session/new` | attaches to the running session and hands out a fresh ACP `sessionId` for it |
| `session/load` | replays `session.history` from the database as `session/update` notifications |
| `session/prompt` | `chat.send`, then waits for a `turn.result` with no background task left |
| `session/cancel` | `interrupt`; the prompt answers `stopReason: "cancelled"` |
| `agent_message_chunk`, `agent_thought_chunk` | `assistant.delta`, `assistant.thinking` |
| `tool_call`, `tool_call_update` | `tool.use`, `check.result`, `edit.diff`, `tool.result` |
| `session/request_permission` | `escalation.request` -> `escalation.decide` |

ACP session ids. An editor expects a new id from every `session/new` (PyCharm refuses a
repeated one), while this adapter serves exactly one calu session. So every `session/new`
answers with a fresh id of the form `<calu session id>~<8 hex digits>`, and all of them stand
for the same calu session:

    session/new  -> "abc~1f0c93aa" ─┐
    session/new  -> "abc~77e02b14" ─┼─► the one calu session "abc"
    session/load    "abc"          ─┘   (also "abc~<anything>", an id of an earlier `calu acp`)

`session/list` names the calu session id itself, which stays valid across restarts of
`calu acp`. While several ACP sessions are live, each of them shows the whole conversation:
every `session/update` is sent once per live id, and every open escalation is asked in each of
them; the first answer decides and the later ones are dropped. A prompt's own text is not
echoed to the ACP session that sent it, and it is to the others. `session/cancel` interrupts
the one calu session, so the waiting prompts of every ACP session answer `cancelled`.
`session/close` ends one id; Claude's turn is interrupted only if that ACP session has a prompt
waiting or was the last live one.

Escalations that are already open when the editor attaches are asked as well: the adapter keeps
every open escalation it hears of (from the events, the replay at connect and the `state.get`
snapshot) and opens the permission dialogs once the editor has a session id, that is after the
answer to `session/new` or `session/load`, and again at each `session/prompt` for those whose
dialog the editor closed without a decision.

Deliberately not translated, because ACP has no slot for them and a calu-aware client reads them
from calu's own protocol instead (§6.2): the checker verdicts as structure (they travel as tool
call content here), the herd overview, the checker definitions and the chat summary. Events
without an ACP counterpart (`chat.queue`, `tool.files`, `agents.update`) are dropped, and `log`
goes to the adapter's own log, not to the editor.
"""

import logging
import secrets
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anyio

from calu.acp import messages as acp
from calu.acp import modes
from calu.acp.peer import Peer, RpcError
from calu.config import AutoEscalate
from calu.protocol.client import RequestFailed
from calu.protocol.messages import HonestyInfo

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

    def __init__(self, text: str = "", session_id: str = "") -> None:
        """Create a turn that nothing has finished yet.

        Args:
            text: The message the prompt sent to the session, as `chat.queue` lists it while
                Claude has not taken it in.
            session_id: The ACP session id the prompt named.
        """
        self.text = text
        self.session_id = session_id
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
    and a task group, then run the peer and feed the session's events to `on_event`. Events may
    be fed before the peer runs and before `session/new`: they update what the adapter knows
    (tool calls, open escalations) and nothing is sent to the editor yet.
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
        self.calu_session_id: str | None = None
        """The calu session's own id as `state.get` reported it; None before the first answer."""
        self._sessions: list[str] = []
        """The live ACP session ids, oldest first; all stand for the one calu session."""
        self.state: str = "starting"
        """Last state the calu session reported."""
        self.auto = AutoEscalate()
        self.permission_mode: str | None = None
        """Claude Code's permission mode as the session last reported it (D247)."""
        """Auto-escalation categories of the session, which ACP shows as its session mode."""
        self.boolean_options = False
        """Whether the client takes boolean config options (set by `initialize`)."""
        self._peer: Peer | None = None
        self._tasks: anyio.abc.TaskGroup | None = None
        self._turns: list[Turn] = []
        """The prompts that wait for their turn to end, oldest first."""
        self._queued: list[str] = []
        """Texts of the messages Claude has not taken in yet (the last `chat.queue` event)."""
        self._open: dict[str, dict[str, Any]] = {}
        """Open escalations by id, each as its `escalation.request` params."""
        self._asking: set[tuple[str, str]] = set()
        """(escalation id, ACP session id) of every permission dialog open in the editor."""
        self._deltas: set[str] = set()
        self._calls: dict[str, dict[str, Any]] = {}
        self._content: dict[str, list[dict[str, Any]]] = {}
        self._resolved: set[str] = set()
        self._echo: deque[tuple[str, str]] = deque(maxlen=ECHO_MEMORY)
        """(text, ACP session id) of our own prompts whose `user.input` echo has not come."""

    @property
    def session_id(self) -> str | None:
        """The ACP session id handed out or loaded last that is still live.

        Returns:
            That id, or None before `session/new` and `session/load` and after every id was
            closed.
        """
        return self._sessions[-1] if self._sessions else None

    def attach(self, peer: Peer, tasks: anyio.abc.TaskGroup) -> None:
        """Give the adapter the peer it answers on and a task group for its own tasks.

        Args:
            peer: The ACP peer; the adapter sends notifications and permission requests on it.
            tasks: Task group for the permission requests, which must not block the event
                stream (one escalation may wait hours for a human).
        """
        self._peer = peer
        self._tasks = tasks
        peer.after_response = self._after_response

    async def _after_response(self, method: str) -> None:
        """Open the permission dialogs of the open escalations once the editor has its session.

        Called by the peer after the response to an inbound request was written. A permission
        request names the session id, which the editor only knows from the answer to
        `session/new` (or has confirmed by the answer to `session/load`), so the dialogs must
        not be sent before that answer.

        Args:
            method: The method of the request that was just answered.
        """
        if method in ("session/new", "session/load"):
            self._ask_open()

    def sync(self, snapshot: dict[str, Any], complete: bool = True) -> None:
        """Take over what a `state.get` snapshot says: state, session mode, open escalations.

        Side effects: remembers every open escalation of the snapshot and, if the editor
        already has a session, opens the permission dialogs that are not open yet. With
        `complete` False (events were lost while the adapter was reconnecting) and a session
        that is not working, the waiting prompts are answered, because the `turn.result` that
        would have ended them may be among the lost events.

        Args:
            snapshot: The result of `state.get`.
            complete: Whether the adapter has seen every event up to this snapshot.
        """
        self.state = str(snapshot.get("state", self.state))
        self._read_auto(snapshot)
        for request in snapshot.get("escalations") or ():
            if isinstance(request, dict):
                self._remember(request)
        self._ask_open()
        if not complete and self.state in ("idle", "stopped", "failed"):
            self.finish_turns("end_turn")

    def finish_turns(self, reason: acp.StopReason) -> None:
        """Answer every waiting `session/prompt`.

        Args:
            reason: The ACP stop reason to answer them with.
        """
        for turn in list(self._turns):
            turn.finish(reason)

    async def connection_lost(self, reason: str) -> None:
        """Tell the editor that the calu session is gone and answer the waiting prompts.

        Without this a prompt would wait for a `turn.result` that can no longer arrive, and the
        editor would show the agent as working for ever.

        Args:
            reason: Why the connection ended, for the message in the editor.
        """
        await self._update(
            acp.message_chunk(
                "agent_message_chunk",
                f"\n[calu: the connection to the calu session ended ({reason}). "
                "Restart the agent to connect again.]\n",
                None,
            )
        )
        self.finish_turns("end_turn")

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
                await self._close(params)
                return {}
            case "session/list":
                return await self._list_sessions(params)
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

        Every call answers with an ACP session id that was never handed out before (see the
        module docstring); the ids of earlier calls stay live.

        Returns:
            `{"sessionId": ...}` with the new id, and the mode fields.

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
        self.calu_session_id = _calu_id(snapshot)
        session_id = self._fresh_id(self.calu_session_id)
        self._sessions.append(session_id)
        self.state = str(snapshot.get("state", "starting"))
        self._read_auto(snapshot)
        self._remember_open(snapshot)  # asked after the answer, see `_after_response`
        return {"sessionId": session_id, **self._mode_fields()}

    def _fresh_id(self, calu_id: str) -> str:
        """Make an ACP session id that this adapter has not handed out and does not serve.

        Args:
            calu_id: The calu session's id.

        Returns:
            `<calu_id>~<8 hex digits>`.
        """
        while True:
            candidate = f"{calu_id}{ID_SEPARATOR}{secrets.token_hex(4)}"
            if candidate not in self._sessions:
                return candidate

    def _remember_open(self, snapshot: dict[str, Any]) -> None:
        """Remember the open escalations of a `state.get` snapshot, without asking yet.

        Args:
            snapshot: The result of `state.get`.
        """
        for request in snapshot.get("escalations") or ():
            if isinstance(request, dict):
                self._remember(request)

    async def _load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/load` by replaying the recorded conversation.

        The protocol requires the whole conversation to be sent as `session/update`
        notifications **before** this returns, so a reconnecting editor shows the history. It
        comes from the database (`session.history`), because the event buffer of the bus only
        covers the running process.

        The requested id must stand for the running calu session: its calu session id (as
        `state.get` reports it and `session/list` names it), or an id a `session/new` of this
        or an earlier `calu acp` handed out for it (`<calu session id>~<suffix>`). An id of
        another calu session is refused, also when no `session/new` came first: an editor may
        remember the id of an earlier calu run, and adopting that id would label this
        session's updates with it. An id that is live in this process loads as well (the
        session may not have reported its calu session id yet when `session/new` answered).
        A loaded id is live from then on, next to the others.

        Args:
            params: The `session/load` params.

        Returns:
            An empty result, after the replay.

        Raises:
            RpcError: If `cwd` is outside the session's project directory, or the session id
                is not the one this adapter serves (`UNKNOWN_SESSION`).
        """
        request = acp.LoadSessionParams.model_validate(params)
        self._check_cwd(request.cwd)
        snapshot = await self.calu.request("state.get")
        served = _calu_id(snapshot)
        known = (
            request.session_id == served
            or request.session_id.startswith(served + ID_SEPARATOR)
            or request.session_id in self._sessions
        )
        if not known:
            raise RpcError(
                acp.UNKNOWN_SESSION,
                f"calu does not serve {request.session_id}: this calu acp serves session {served}",
            )
        self.calu_session_id = served
        if request.session_id not in self._sessions:
            self._sessions.append(request.session_id)
        self.state = str(snapshot.get("state", self.state))
        self._read_auto(snapshot)
        self._remember_open(snapshot)  # asked after the answer, see `_after_response`
        history = await self.calu.request("session.history", {})
        await self._replay(history, request.session_id)
        return self._mode_fields()

    async def _list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/list` with the one session this adapter serves.

        An adapter is attached to one running calu session, and `session/load` takes only
        that one, so nothing else is listed: an entry the editor could not open would be a
        false offer. The entry carries the calu session id, not one of the ids `session/new`
        handed out: it is the id that a later `calu acp` process still loads. Asked for
        another directory than the session's (and not one inside it), the list is empty.

        Args:
            params: The `session/list` params: an optional `cwd` and `cursor`.

        Returns:
            `{"sessions": [...]}` with at most one entry: the session's id, its project
            directory, a title (the directory's name and the session's state) and the time
            of the answer as the last update. Empty (and a warning in the log) when the
            session does not answer `state.get`: some editors poll this list, and an error
            answer would show up there as a failure of the agent.
        """
        cwd = params.get("cwd")
        if isinstance(cwd, str) and cwd:
            try:
                self._check_cwd(cwd)
            except RpcError:
                return {"sessions": []}
        try:
            snapshot = await self.calu.request("state.get")
        except (RequestFailed, ConnectionError) as error:
            log.warning("session/list: the calu session did not answer state.get: %s", error)
            return {"sessions": []}
        session_id = _calu_id(snapshot)
        state = str(snapshot.get("state", self.state))
        return {
            "sessions": [
                {
                    "sessionId": session_id,
                    "cwd": str(self.workspace),
                    "title": f"{self.workspace.name} ({state})",
                    "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            ]
        }

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `session/prompt`: send the message to Claude and wait for the turn to end.

        Starts the Claude session first if it is not running, so that an editor can begin a
        conversation in a calu process whose session was stopped (for example after the
        escalation timeout of D27).

        Several prompts may wait at once; each waits for its own turn (`_on_turn_result`).
        Side effect: opens the permission dialog of every open escalation that has none in the
        editor, because the message would otherwise wait behind a blocked step with nothing on
        screen that says so.

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
        images = acp.prompt_images(request.prompt)
        if not text.strip() and images:
            # calu's chat needs a line of text; an image alone gets a neutral one.
            text = "See the attached image."
        if not text.strip():
            raise RpcError(acp.INVALID_PARAMS, "the prompt holds no text")
        if self.state in ("stopped", "failed"):
            await self.calu.request("session.start", {})
        self._ask_open()
        turn = Turn(text, request.session_id)
        self._turns.append(turn)
        try:
            self._echo.append((text, request.session_id))
            message: dict[str, Any] = {"text": text, "delivery": "queued"}
            if images:
                message["images"] = images
            await self.calu.request("chat.send", message)
            await turn.done.wait()
        finally:
            self._turns.remove(turn)
        return {"stopReason": turn.stop_reason}

    async def _cancel(self, params: dict[str, Any]) -> None:
        """Handle `session/cancel`: interrupt Claude's turn.

        Every pending prompt answers `cancelled`, as the protocol requires; that includes
        the prompts of the other live ACP session ids, because the interrupt ends the one
        turn they all wait for. A cancel for an id that is not live is a notification and
        gets no answer, so it is only logged.

        Args:
            params: The params holding the session id.
        """
        request = acp.SessionIdParams.model_validate(params)
        if self._sessions and request.session_id not in self._sessions:
            log.warning("ignoring a cancel for the unknown session %s", request.session_id)
            return
        self.finish_turns("cancelled")
        await self.calu.request("interrupt", {})

    async def _close(self, params: dict[str, Any]) -> None:
        """Handle `session/close`: forget one ACP session id and end its prompts.

        The prompts that named this id answer `cancelled`. Claude's turn is interrupted if
        one of them was waiting, or if this was the last live id (then every pending prompt
        answers `cancelled`); a spare ACP session that an editor closes does not stop the turn
        another one started. The calu session itself keeps running (it belongs to `calu run`);
        a later request with this id needs a `session/load` first.

        Args:
            params: The params holding the session id.

        Raises:
            RpcError: `UNKNOWN_SESSION` if no session is attached or the id is a different one.
        """
        request = acp.SessionIdParams.model_validate(params)
        self._require_session(request.session_id)
        own = [turn for turn in self._turns if turn.session_id == request.session_id]
        self._sessions.remove(request.session_id)
        if own or not self._sessions:
            self.finish_turns("cancelled")
            await self.calu.request("interrupt", {})

    def _check_cwd(self, cwd: str) -> None:
        """Check that the editor's directory is inside the project this adapter serves.

        The session's project directory itself is accepted, and so is a directory below it: an
        editor opened on one package of a repository is still working inside the project whose rules
        and checkers the session enforces, and `calu acp` with no address finds that session from a
        subdirectory for the same reason (agent/records/PLAN.md D105). A directory **outside** the
        project is refused, because the editor would then act on files the session does not cover.

        An editor on Windows that names a directory inside WSL the way Windows sees it
        (`//wsl.localhost/<distribution>/...`) means the path inside the distribution
        (`acp.local_path`).

        Args:
            cwd: The absolute path the editor sent.

        Raises:
            RpcError: If it is neither the session's project directory nor below it.
        """
        here = Path(acp.local_path(cwd)).resolve()
        workspace = self.workspace.resolve()
        if here != workspace and not here.is_relative_to(workspace):
            raise RpcError(
                acp.INVALID_PARAMS,
                f"this calu session serves {self.workspace}, which does not contain {cwd}; "
                "start a calu session in that directory and attach to it",
            )

    def _require_session(self, session_id: str) -> None:
        """Check that a request names a live ACP session id of this adapter.

        Args:
            session_id: The id from the request.

        Raises:
            RpcError: If no session was created yet, or the id is not a live one.
        """
        if not self._sessions:
            raise RpcError(acp.UNKNOWN_SESSION, "no session: call session/new first")
        if session_id not in self._sessions:
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
        mode = snapshot.get("permission_mode")
        if isinstance(mode, str) and mode:
            self.permission_mode = mode

    def _mode_fields(self) -> dict[str, Any]:
        """Build the mode fields of a `session/new` or `session/load` result.

        Both ACP APIs are answered: `configOptions` (which supersedes modes) and `modes` (which
        older clients use), as the specification asks during the transition.

        Returns:
            `{"modes": ..., "configOptions": ...}` for the session's current categories.
        """
        return {
            "modes": modes.mode_state(self.auto),
            "configOptions": self._options(),
        }

    def _options(self, auto: AutoEscalate | None = None) -> list[dict[str, Any]]:
        """Build the `configOptions` list for the editor.

        Args:
            auto: The categories to show; None uses the session's (`self.auto`).

        Returns:
            `modes.config_options` with this client's boolean support and the permission mode.
        """
        return modes.config_options(
            auto if auto is not None else self.auto, self.boolean_options, self.permission_mode
        )

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
        if request.config_id == modes.PERMISSION_OPTION_ID:
            # Claude Code's own mode: the session refuses what calu does not offer, and its
            # error reaches the editor as the error of this request.
            if not isinstance(request.value, str):
                raise RpcError(acp.INVALID_PARAMS, "the permission mode takes a mode name")
            await self.calu.request("permission_mode.set", {"mode": request.value})
            self.permission_mode = request.value
            return {"configOptions": self._options()}
        try:
            auto = modes.with_option(self.auto, request.config_id, request.value)
        except ValueError as error:
            raise RpcError(acp.INVALID_PARAMS, str(error)) from error
        await self._apply_auto(auto)
        # The session's `auto_escalate.changed` event may not have arrived yet, and the result
        # must already hold the new values, so answer from what was asked for.
        self.auto = auto
        return {"configOptions": self._options(auto)}

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
        await self._update(
            modes.config_update(self.auto, self.boolean_options, self.permission_mode)
        )

    async def _on_permission_mode(self, params: dict[str, Any]) -> None:
        """Mirror a change of Claude Code's permission mode to the editor (D247).

        Args:
            params: The `permission_mode.changed` event.
        """
        mode = params.get("mode")
        if not isinstance(mode, str) or not mode or mode == self.permission_mode:
            return
        self.permission_mode = mode
        await self._update(
            modes.config_update(self.auto, self.boolean_options, self.permission_mode)
        )

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
            case "chat.queue":
                self._on_queue(params)
            case "escalation.resolved":
                await self._on_resolved(params)
            case "turn.result":
                await self._on_turn_result(params)
            case "auto_escalate.changed":
                await self._on_auto_escalate(params)
            case "permission_mode.changed":
                await self._on_permission_mode(params)
            case "honesty.update":
                await self._on_honesty(params)
            case "log":
                log.log(
                    _LEVELS.get(str(params.get("level", "info")), logging.INFO),
                    "session: %s",
                    params.get("message", ""),
                )

    async def _on_honesty(self, params: dict[str, Any]) -> None:
        """Tell the editor the code honesty state of the session.

        ACP has no shape for this state, so it goes as a message chunk, the way the "always"
        hint does: with `honesty.mode` "enforce", a command that executes project code and a
        commit are refused or escalated while the state is not clean, and the editor's user
        has to be able to see why. The event is only emitted when the state changed.

        Args:
            params: The `honesty.update` event.
        """
        try:
            info = HonestyInfo.model_validate(params)
        except ValueError:
            return
        if info.mode == "off":
            return
        if info.clean:
            text = "[calu: code honesty: clean.]"
        else:
            parts: list[str] = []
            if info.marks:
                spots = ", ".join(mark.label() for mark in info.marks[:5])
                rest = f" and {len(info.marks) - 5} more" if len(info.marks) > 5 else ""
                parts.append(f"recorded as dishonest: {spots}{rest}")
            if info.baseline is None:
                parts.append("no trusted commit to compare with (`calu honesty trust <commit>`)")
            elif info.unreviewed_all:
                parts.append("every file is unreviewed")
            elif info.unreviewed_count:
                parts.append(f"{info.unreviewed_count} unreviewed file(s)")
            if info.review_required:
                parts.append("an honesty review is required")
            if info.review_running:
                parts.append("an honesty review is running")
            effect = (
                "Commands that execute project code and commits wait until this is settled."
                if info.mode == "enforce"
                else "Nothing is blocked: honesty.mode is 'track'."
            )
            text = f"[calu: code honesty: {'; '.join(parts) or 'not clean'}. {effect}]"
        await self._update(acp.message_chunk("agent_message_chunk", f"{text}\n", None))

    async def _on_state(self, params: dict[str, Any]) -> None:
        """Remember the session state and end the waiting prompts if the session ended.

        Args:
            params: The `session.state` event.
        """
        self.state = str(params.get("state", self.state))
        if self.state in ("stopped", "failed") and self._turns:
            await self._update(
                acp.message_chunk(
                    "agent_message_chunk", f"[the calu session is {self.state}]", None
                )
            )
            self.finish_turns("end_turn")

    def _on_queue(self, params: dict[str, Any]) -> None:
        """Remember which messages Claude has not taken in yet.

        Args:
            params: The `chat.queue` event.
        """
        messages = params.get("messages") or []
        self._queued = [
            str(message.get("text", "")) for message in messages if isinstance(message, dict)
        ]

    async def _on_user_input(self, params: dict[str, Any]) -> None:
        """Forward a user message of another client, but not the echo of our own prompt.

        The editor already shows what its user typed, so echoing our own `chat.send` back would
        duplicate it in the ACP session that sent it; the other live ACP sessions get it.
        Messages typed in another client (or relayed from another session) are forwarded to
        all, so the editor shows the whole conversation.

        Args:
            params: The `user.input` event.
        """
        text = _text(params)
        sender = next((entry for entry in self._echo if entry[0] == text), None)
        if sender is not None:
            self._echo.remove(sender)
        await self._update(
            acp.message_chunk("user_message_chunk", text),
            skip=sender[1] if sender is not None else None,
        )

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
            self._open.pop(escalation_id, None)

    async def _on_turn_result(self, params: dict[str, Any]) -> None:
        """End the prompt turns the editor is waiting for, once no background task is left.

        A prompt whose message is still in the session's queue (`chat.queue`) stays open: the
        turn that just ended was started by an earlier message, of this editor or of another
        client, and Claude has not read this one yet.

        A Claude turn can end while background tasks still run (a background command, a
        Monitor, a Workflow); Claude Code then starts another turn by itself when they finish
        (D158). ACP streams `session/update` only *during* a prompt turn (schema v1), so the
        prompt stays open until a turn ends with no background task, and the follow-up turn's
        output reaches the editor inside it. `session/cancel` still ends it at once (D159).

        Args:
            params: The `turn.result` event.
        """
        failed = bool(params.get("is_error"))
        if failed:
            detail = str(params.get("result") or params.get("subtype") or "the turn failed")
            await self._update(acp.message_chunk("agent_message_chunk", f"[{detail}]"))
        waiting = int(params.get("background_tasks") or 0)
        if waiting and not failed:
            await self._update(
                acp.message_chunk(
                    "agent_message_chunk",
                    f"\n[{waiting} background task(s) still running; Claude continues when "
                    "they finish]\n",
                )
            )
            return
        waiting_texts = Counter(text.strip() for text in self._queued)
        for turn in reversed(self._turns):  # of two equal texts, the newer one is the queued one
            if waiting_texts[turn.text.strip()] > 0:
                waiting_texts[turn.text.strip()] -= 1
            else:
                turn.finish("end_turn")

    # ------------------------------------------------------------------ escalations

    def _remember(self, params: dict[str, Any]) -> None:
        """Keep an open escalation, so that it can be asked now or once the editor is ready.

        Args:
            params: The `escalation.request` event, or the same shape from a snapshot.
        """
        escalation_id = str(params.get("escalation_id", ""))
        if escalation_id and escalation_id not in self._resolved:
            self._open.setdefault(escalation_id, params)

    def _ask(self, params: dict[str, Any]) -> None:
        """Remember an escalated step and ask the editor's user about it as soon as possible.

        Args:
            params: The `escalation.request` event.
        """
        self._remember(params)
        self._ask_open()

    def _ask_open(self) -> None:
        """Open a permission dialog for every open escalation, in every live ACP session.

        Nothing is asked before `session/new` or `session/load`, because a permission request
        names the session id and an editor refuses one it does not know; the escalations stay
        remembered and are asked then. Each question runs in its own task: a human may take
        hours, while the event stream and `session/cancel` must keep working. A pair of
        escalation and ACP session whose dialog is already open is not asked again.
        """
        if self._tasks is None or self._peer is None:
            return
        for escalation_id, params in list(self._open.items()):
            for session_id in list(self._sessions):
                if (escalation_id, session_id) not in self._asking:
                    self._asking.add((escalation_id, session_id))
                    self._tasks.start_soon(self._ask_permission, params, session_id)

    async def _ask_permission(self, params: dict[str, Any], session_id: str) -> None:
        """Ask the editor's user about an escalated step and pass the answer to calu.

        Nothing is decided if the user's client answers `cancelled`, or if the escalation was
        already resolved elsewhere: calu refuses a second decision anyway, and a cancelled
        dialog is not a decision. An escalation that is still open after its dialog ended
        without a decision is asked again at the next `session/prompt`.

        Args:
            params: The `escalation.request` event.
            session_id: The ACP session id the dialog is opened in.
        """
        try:
            await self._ask_and_decide(params, session_id)
        finally:
            self._asking.discard((str(params.get("escalation_id", "")), session_id))

    async def _ask_and_decide(self, params: dict[str, Any], session_id: str) -> None:
        """Send one permission request and turn its answer into `escalation.decide`.

        With several live ACP sessions the same escalation is asked once in each; the answer
        that arrives after the escalation was resolved is dropped here.

        Args:
            params: The `escalation.request` event.
            session_id: The ACP session id the dialog is opened in.
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
        content = self._question_content(call_id, question)
        try:
            answer = await self._ask_client(call_id, content, session_id)
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

    def _question_content(self, call_id: str, question: str) -> list[dict[str, Any]]:
        """Add an escalation's question to its tool call's content, once.

        The same escalation is asked in every live ACP session and again after a closed
        dialog, and each of those must not append the question another time.

        Args:
            call_id: The tool call the question is about.
            question: The reason and the rendered step.

        Returns:
            All content entries of the call, oldest first.
        """
        entry = acp.text_content(question)
        entries = self._content.setdefault(call_id, [])
        if entry not in entries:
            entries.append(entry)
        return entries

    async def _ask_client(
        self, call_id: str, content: list[dict[str, Any]], session_id: str
    ) -> Any:
        """Send the `session/request_permission` request to the editor.

        Args:
            call_id: The tool call the question is about.
            content: What the checker said, as tool call content.
            session_id: The ACP session id the dialog is opened in.

        Returns:
            The result of the request.

        Raises:
            RpcError: If the editor answered with an error.
            ConnectionError: If the connection ended before the answer arrived.
        """
        if self._peer is None:
            raise ConnectionError("the adapter is not attached to a peer")
        # The dialog shows what it is about: an editor that draws only this request (and
        # not the tool call it announced before) had a question without a subject.
        known = self._calls.get(call_id) or {}
        name = str(known.get("name") or "")
        tool_call: dict[str, Any] = {
            "toolCallId": call_id,
            "title": _title(name, known.get("input") or {}) if name else "A step of Claude",
            "kind": acp.TOOL_KINDS.get(name, "other"),
            "status": "pending",
            "content": content,
        }
        if known.get("input"):
            tool_call["rawInput"] = known["input"]
        return await self._peer.request(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": tool_call,
                "options": acp.permission_options(),
            },
        )

    # ------------------------------------------------------------------ replay and sending

    async def _replay(self, history: dict[str, Any], session_id: str) -> None:
        """Send a recorded conversation as `session/update` notifications of one ACP session.

        The turns carry the user's messages and Claude's answers, the steps the tool calls; a
        step is shown with its checker summary and the beginning of its result, which is what
        the database keeps (the full output is not recorded, D3). Only the loaded ACP session
        gets the replay: the other live ones already show the conversation.

        Args:
            history: The result of `session.history`.
            session_id: The ACP session id that was loaded.
        """
        steps = history.get("steps") or []
        by_turn: dict[Any, list[dict[str, Any]]] = {}
        for step in steps:
            by_turn.setdefault(step.get("turn"), []).append(step)
        for turn in history.get("turns") or []:
            for text in turn.get("user_inputs") or []:
                await self._update(
                    acp.message_chunk("user_message_chunk", str(text)), only=session_id
                )
            for step in by_turn.pop(turn.get("index"), []):
                await self._replay_step(step, session_id)
            answer = str(turn.get("answer") or "")
            if answer:
                await self._update(
                    acp.message_chunk("agent_message_chunk", answer), only=session_id
                )
        for remaining in by_turn.values():
            for step in remaining:
                await self._replay_step(step, session_id)

    async def _replay_step(self, step: dict[str, Any], session_id: str) -> None:
        """Send one recorded step as a finished tool call.

        Args:
            step: A `StoredStep` as `session.history` returned it.
            session_id: The ACP session id that was loaded.
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
            ),
            only=session_id,
        )
        parts = [
            f"{key}: {step.get(key)}"
            for key in ("decision", "reason", "result_summary")
            if str(step.get(key) or "").strip()
        ]
        if parts:
            await self._update(
                acp.tool_call_update(call_id, content=[acp.text_content("\n".join(parts))]),
                only=session_id,
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

    async def _update(
        self, update: dict[str, Any], only: str | None = None, skip: str | None = None
    ) -> None:
        """Send one `session/update` notification to the live ACP sessions.

        Every live ACP session id stands for the same calu session, so each gets the update,
        oldest id first. Nothing is sent before `session/new` or `session/load`, because an
        update needs a session id.

        Args:
            update: The `update` object.
            only: Send to this ACP session id alone (the replay of `session/load`).
            skip: Leave this ACP session id out (the echo of its own prompt).
        """
        if self._peer is None:
            return
        for session_id in [only] if only is not None else list(self._sessions):
            if session_id != skip:
                await self._peer.notify(
                    "session/update", {"sessionId": session_id, "update": update}
                )


ID_SEPARATOR = "~"
"""Between the calu session id and the per-`session/new` suffix of an ACP session id."""


def _calu_id(snapshot: dict[str, Any]) -> str:
    """Return the calu session's id from a `state.get` snapshot.

    Args:
        snapshot: The result of `state.get`.

    Returns:
        Its `calu_session_id`, or "calu" when the snapshot has none.
    """
    return str(snapshot.get("calu_session_id") or "calu")


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
