"""The relay of `calu serve`: one loopback port for every session of the server (PLAN D242).

    client ──ws://127.0.0.1:<port>/───────────► Relay   initialize (server token),
                                                        relay.sessions, relay.start
    client ──ws://127.0.0.1:<port>/session/<id>► Relay ──ws://127.0.0.1:<session port>──► session
             initialize (server token)                  initialize (the session's own token,
                                                        client "<name> via relay (<label>)")
           ◄─────────── every later message copied unchanged, in both directions ───────────►

A client on another machine therefore needs one SSH forward and one server token for every
session of that server, and can follow `/new`, `/fork` and `/switch`. The relay checks the
server token itself (`initialize`, again before every request that is not in
`calu.protocol.server.READ_ONLY_METHODS`, and every `REVALIDATE_S` seconds). The session checks
its own token on the relay's connection exactly as on any other: the relay adds a check, it
takes none away. A revoked server token closes the connection with
`CLOSE_SERVER_TOKEN_REVOKED`. The close code and reason of the session's side are passed on.

With `insecure` (`calu serve --insecure`, D227) a peer on a loopback address needs no server
token to attach; `relay.start` still needs one, so that only a holder of a token or of an
operator login can make the server start a session (D208).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import anyio
from anyio.abc import TaskGroup
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import Server as WsServer
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from calu import __version__
from calu.protocol.client import RequestFailed, snapshot
from calu.protocol.messages import (
    CLOSE_SERVER_TOKEN_REVOKED,
    MAX_FRAME_BYTES,
    RELAY_SESSION_PATH,
    InitializeParams,
    RelayInitializeResult,
    RelayServer,
    RelaySession,
    RelaySessionsResult,
    RelayStartParams,
    RelayStartResult,
)
from calu.protocol.server import (
    CLOSE_POLICY,
    CLOSE_TOO_BIG,
    INITIALIZE_TIMEOUT_S,
    MAX_PRE_AUTH_CHARS,
    READ_ONLY_METHODS,
    REVALIDATE_S,
    TOKEN_CHECK_S,
    UNAUTHENTICATED,
    TokenCheckUnavailable,
    _loopback,
)
from calu.servers import Hosted, StartRefused
from calu.tokens import token_hash

__all__ = ["Hosted", "Relay", "StartRefused"]

log = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT_S = 3.0
"""How long `relay.sessions` waits for one session's `state.get`."""

UNSENDABLE_CODES = frozenset({None, 1004, 1005, 1006, 1015})
"""Close codes a WebSocket endpoint may not send itself; the relay passes 1011 on instead."""


class Sessions(Protocol):
    """What the relay needs of its server (`calu.serve.Server`)."""

    def hosted(self) -> list[Hosted]:
        """List the sessions that clients can attach to, newest first.

        Returns:
            The sessions.
        """
        ...

    async def session_details(
        self, sha: str | None, session_ids: list[str]
    ) -> dict[str, tuple[str, datetime]]:
        """Name the sessions and say since when each is in its state.

        Args:
            sha: SHA-256 of the asking client's server token; None for a client without one.
            session_ids: The sessions' row ids.

        Returns:
            Session id -> (name, "" for none; start of its current state); a session that
            cannot be described is missing. Never raises.
        """
        ...

    async def relay_start(self, sha: str, label: str, params: RelayStartParams) -> str:
        """Start a session for the holder of a server token and wait until it runs.

        Args:
            sha: SHA-256 of the client's server token.
            label: The token's label.
            params: What to start.

        Returns:
            The id of the started session's row.

        Raises:
            StartRefused: If the server refused or failed to start it, or did not answer in time.
        """
        ...


TokenCheck = Callable[[str], Awaitable[str | None]]
"""Asks the database about a server token's hash: its label if valid, None if not; raises
whatever the database raises."""


@dataclass(eq=False)
class _Peer:
    """One authenticated client connection of the relay."""

    connection: ServerConnection
    sha: str | None
    """SHA-256 of its server token; None for a loopback peer of an `insecure` relay."""
    label: str
    """The token's label, or "local" for a peer without a token."""


@dataclass
class _Check:
    """One open question to the database about one token hash."""

    done: anyio.Event = field(default_factory=anyio.Event)
    answered: bool = False
    label: str | None = None


class Relay:
    """Serves the relay address of one `calu serve` (see the module docstring)."""

    def __init__(
        self,
        sessions: Sessions,
        check: TokenCheck,
        server: RelayServer,
        insecure: bool = False,
        revalidate_s: float = REVALIDATE_S,
        check_s: float = TOKEN_CHECK_S,
    ) -> None:
        """Prepare the relay; `open` binds it and `run` serves it.

        Args:
            sessions: The server whose sessions it reaches.
            check: Asks the database about a server token.
            server: Which server this is, for `initialize`; may be replaced before `run`.
            insecure: Let peers on a loopback address attach without a server token.
            revalidate_s: Seconds between two checks of the tokens of the open connections.
            check_s: Seconds a check waits for the database's answer.
        """
        self.sessions = sessions
        self.check = check
        self.server = server
        self.insecure = insecure
        self.revalidate_s = revalidate_s
        self.check_s = check_s
        self.address = ""
        """`ws://127.0.0.1:<port>` once `open` has bound it."""
        self._ws: WsServer | None = None
        self._tg: TaskGroup | None = None
        self._peers: set[_Peer] = set()
        self._known: dict[str, str] = {}
        """Token hash -> label, for the tokens the database last called valid."""
        self._revoked: set[str] = set()
        """Hashes seen revoked after they were valid; refused for the rest of the process."""
        self._checks: dict[str, _Check] = {}

    async def open(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Bind the relay's port.

        Side effect: sets `address`.

        Args:
            host: The interface; the loopback interface, as for the sessions.
            port: The port (`calu serve` derives one from its projects directory,
                `calu.projects_dir.relay_ports`); 0 lets the operating system choose a free one.

        Raises:
            OSError: If the port cannot be bound (taken, or not allowed).
        """
        self._ws = await serve(self._handle, host, port, max_size=MAX_FRAME_BYTES)
        bound = self._ws.sockets[0].getsockname()
        self.address = f"ws://{bound[0]}:{bound[1]}"
        log.info("the relay listens on %s", self.address)

    async def run(self) -> None:
        """Serve the bound port until cancelled; then close every connection.

        Raises:
            RuntimeError: If `open` was not called first.
        """
        if self._ws is None:
            raise RuntimeError("Relay.open first")
        async with self._ws, anyio.create_task_group() as tg:
            self._tg = tg
            tg.start_soon(self._revalidate)
            await anyio.sleep_forever()

    # ------------------------------------------------------------------------- server tokens

    async def label(self, sha: str, new: bool) -> str | None:
        """Ask whether a server token is valid, waiting at most `check_s` seconds.

        The question runs as a task of the relay, so it is not cut short when the caller
        stops waiting. While the database gives no answer, the last known one stands for a
        connection that is already open (`new` False); a token seen revoked stays refused.

        Args:
            sha: SHA-256 of the token.
            new: Whether the answer decides a new connection.

        Returns:
            The token's label if it is valid; None if it is not, or if it is unknown and the
            database gives no answer while `new` is False.

        Raises:
            TokenCheckUnavailable: If `new` is True and the database gave no answer.
        """
        if sha in self._revoked:
            return None
        check = self._checks.get(sha)
        if check is None:
            check = self._checks[sha] = _Check()
            if self._tg is None:
                await self._ask(sha, check)
            else:
                self._tg.start_soon(self._ask, sha, check)
        with anyio.move_on_after(self.check_s):
            await check.done.wait()
        if not check.answered:
            if new:
                raise TokenCheckUnavailable("the database gives no answer about the server token")
            return self._known.get(sha)
        return check.label

    async def _ask(self, sha: str, check: _Check) -> None:
        """Ask the database once and remember the answer. Never raises.

        Args:
            sha: SHA-256 of the token.
            check: The shared record of the question.
        """
        try:
            label = await self.check(sha)
            check.answered = True
            check.label = label
            if label is None:
                if self._known.pop(sha, None) is not None:
                    self._revoked.add(sha)
                    log.warning("a server token is refused from now on: it was revoked")
            else:
                self._known[sha] = label
        except Exception as exc:
            log.warning("the database cannot be asked about a server token: %s", exc)
        finally:
            self._checks.pop(sha, None)
            check.done.set()

    async def _revalidate(self) -> None:
        """Check the token of every open connection every `revalidate_s` seconds; never raises."""
        while True:
            await anyio.sleep(self.revalidate_s)
            for peer in list(self._peers):
                if peer.sha is None:
                    continue
                try:
                    if await self.label(peer.sha, new=False) is None:
                        await _close(peer.connection, CLOSE_SERVER_TOKEN_REVOKED, REVOKED)
                except Exception:
                    log.exception("checking a server token failed")

    # ------------------------------------------------------------------------------ connections

    async def _handle(self, connection: ServerConnection) -> None:
        """Serve one connection until it closes. Never raises.

        Args:
            connection: The accepted connection.
        """
        request = connection.request
        path = urlsplit(request.path).path if request is not None else "/"
        target: str | None = None
        if path.startswith(RELAY_SESSION_PATH):
            target = path[len(RELAY_SESSION_PATH) :].strip("/")
        if (target is None and path not in ("", "/")) or target == "":
            await _close(connection, CLOSE_POLICY, f"unknown path {path[:60]}")
            return
        trusted = self.insecure and _loopback(connection.remote_address)
        peer: _Peer | None = None
        try:
            opened = await self._initialize(connection, trusted)
            if opened is None:
                return
            peer, request_id, params = opened
            self._peers.add(peer)
            if target is None:
                await self._control(peer, request_id)
            else:
                await self._proxy(peer, target, request_id, params)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("a relay connection failed")
        finally:
            if peer is not None:
                self._peers.discard(peer)

    async def _initialize(
        self, connection: ServerConnection, trusted: bool
    ) -> tuple[_Peer, Any, InitializeParams] | None:
        """Wait for an `initialize` with a valid server token.

        Every other message before it is answered with an error; a message larger than
        `MAX_PRE_AUTH_CHARS`, or no valid `initialize` within `INITIALIZE_TIMEOUT_S`, closes
        the connection.

        Args:
            connection: The connection.
            trusted: Whether the peer needs no token.

        Returns:
            The peer, the id of its `initialize` and its params; None if the connection was
            closed.

        Raises:
            ConnectionClosed: If the client disconnected.
        """
        deadline = anyio.current_time() + INITIALIZE_TIMEOUT_S
        while True:
            message: str | bytes | None = None
            with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
                message = await connection.recv()
            if message is None:
                await _close(connection, CLOSE_POLICY, "no valid initialize in time")
                return None
            if len(message) > MAX_PRE_AUTH_CHARS:
                await _close(connection, CLOSE_TOO_BIG, "message too big before initialize")
                return None
            text = message if isinstance(message, str) else message.decode()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                await _error(connection, None, -32700, f"parse error: {exc}")
                continue
            if not isinstance(data, dict) or "method" not in data:
                await _error(connection, None, -32600, "invalid request")
                continue
            request_id = data.get("id")
            if data["method"] != "initialize":
                await _error(
                    connection,
                    request_id,
                    UNAUTHENTICATED,
                    "send initialize with a valid server token first",
                )
                continue
            try:
                params = InitializeParams.model_validate(data.get("params") or {})
            except ValidationError as exc:
                await _error(
                    connection, request_id, -32602, "invalid params", json.loads(exc.json())
                )
                continue
            if trusted:
                return _Peer(connection, None, "local"), request_id, params
            sha = token_hash(params.token)
            try:
                label = await self.label(sha, new=True)
            except TokenCheckUnavailable as exc:
                await _error(connection, request_id, -32603, f"internal error: {exc}; try again")
                continue
            if label is None:
                await _error(
                    connection,
                    request_id,
                    UNAUTHENTICATED,
                    "invalid server token (`calu token mint --server` on the server's machine "
                    "prints one; a token ends with its server)",
                )
                continue
            return _Peer(connection, sha, label), request_id, params

    async def _control(self, peer: _Peer, request_id: Any) -> None:
        """Serve the relay's own requests on its bare address until the client disconnects.

        Args:
            peer: The authenticated client.
            request_id: The id of its `initialize`, answered here first.

        Raises:
            ConnectionClosed: If the client disconnected while an answer was sent.
        """
        await _result(
            peer.connection,
            request_id,
            RelayInitializeResult(core_version=__version__, relay=self.server),
        )
        async with anyio.create_task_group() as tg:
            try:
                async for message in peer.connection:
                    text = message if isinstance(message, str) else message.decode()
                    tg.start_soon(self._control_request, peer, text)
            except ConnectionClosed:
                pass
            tg.cancel_scope.cancel()

    async def _control_request(self, peer: _Peer, text: str) -> None:
        """Answer one request on the relay's bare address. Never raises.

        Args:
            peer: The client.
            text: The message.
        """
        connection = peer.connection
        try:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                await _error(connection, None, -32700, f"parse error: {exc}")
                return
            if not isinstance(data, dict) or "method" not in data:
                await _error(connection, None, -32600, "invalid request")
                return
            request_id, method = data.get("id"), data["method"]
            if method == "initialize":
                await _result(
                    connection,
                    request_id,
                    RelayInitializeResult(core_version=__version__, relay=self.server),
                )
            elif method == "relay.sessions":
                await _result(
                    connection, request_id, RelaySessionsResult(sessions=await self.listing(peer))
                )
            elif method == "relay.start":
                await self._start(peer, request_id, data.get("params") or {})
            else:
                await _error(connection, request_id, -32601, f"unknown method {method!r}")
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("a relay request failed")

    async def _start(self, peer: _Peer, request_id: Any, raw: Any) -> None:
        """Answer `relay.start`: ask the server for a session and wait for it.

        Args:
            peer: The client.
            request_id: The request's id.
            raw: Its params.

        Raises:
            ConnectionClosed: If the client disconnected.
        """
        connection = peer.connection
        if peer.sha is None:
            await _error(
                connection,
                request_id,
                UNAUTHENTICATED,
                "asking for a session needs a server token, also from this machine "
                "(`calu token mint --server`), or `calu start` with a database login",
            )
            return
        if await self.label(peer.sha, new=False) is None:
            await _error(connection, request_id, UNAUTHENTICATED, REVOKED)
            await _close(connection, CLOSE_SERVER_TOKEN_REVOKED, REVOKED)
            return
        try:
            params = RelayStartParams.model_validate(raw)
        except ValidationError as exc:
            await _error(connection, request_id, -32602, "invalid params", json.loads(exc.json()))
            return
        try:
            session_id = await self.sessions.relay_start(peer.sha, peer.label, params)
        except StartRefused as exc:
            await _error(connection, request_id, -32000, str(exc))
            return
        await _result(connection, request_id, RelayStartResult(calu_session_id=session_id))

    async def listing(self, peer: _Peer | None = None) -> list[RelaySession]:
        """Describe the server's sessions, each asked for its state at the same time.

        Args:
            peer: The asking client; its server token lets the server read the sessions'
                names and state times from the database (`Sessions.session_details`). None,
                or a client without a token, gets neither.

        Returns:
            One entry per session, newest first; a session that does not answer within
            `SNAPSHOT_TIMEOUT_S` is listed with the state "unknown".
        """
        hosted = self.sessions.hosted()
        found: dict[str, dict[str, Any]] = {}
        details: dict[str, tuple[str, datetime]] = {}

        async def describe() -> None:
            """Ask the server for the names and state times."""
            details.update(
                await self.sessions.session_details(
                    peer.sha if peer is not None else None,
                    [one.calu_session_id for one in hosted],
                )
            )

        async def ask(one: Hosted) -> None:
            """Take one session's snapshot.

            Args:
                one: The session.
            """
            try:
                found[one.calu_session_id] = await snapshot(
                    one.address, one.token, "calu relay (listing)", SNAPSHOT_TIMEOUT_S
                )
            except (OSError, RequestFailed, ConnectionError, TimeoutError) as exc:
                log.info("session %s did not answer the relay: %s", one.calu_session_id, exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(describe)
            for one in hosted:
                tg.start_soon(ask, one)
        listed = []
        for one in hosted:
            state = found.get(one.calu_session_id, {})
            name, since = details.get(one.calu_session_id, ("", None))
            listed.append(
                RelaySession(
                    calu_session_id=one.calu_session_id,
                    directory=one.directory,
                    state=str(state.get("state") or "unknown"),
                    claude_session_id=state.get("claude_session_id"),
                    model=state.get("model"),
                    open_escalations=len(state.get("escalations") or ()),
                    started_at=one.started_at,
                    name=name,
                    state_since=since,
                )
            )
        return listed

    def find(self, target: str) -> Hosted | None:
        """Find a session by its id or a beginning of it that names only one.

        Args:
            target: The id from the path.

        Returns:
            The session, or None.
        """
        hosted = self.sessions.hosted()
        exact = [one for one in hosted if one.calu_session_id == target]
        if exact:
            return exact[0]
        begins = [one for one in hosted if one.calu_session_id.startswith(target)]
        return begins[0] if len(begins) == 1 and len(target) >= 4 else None

    async def _proxy(
        self, peer: _Peer, target: str, request_id: Any, params: InitializeParams
    ) -> None:
        """Connect a client to one session and copy messages until either side ends.

        Args:
            peer: The authenticated client.
            target: The session's id from the path.
            request_id: The id of the client's `initialize`; the session answers it.
            params: The client's `initialize` params.

        Raises:
            ConnectionClosed: If the client disconnected.
        """
        down = peer.connection
        hosted = self.find(target)
        if hosted is None:
            await _error(
                down,
                request_id,
                -32000,
                f"no session {target} runs on this server: it has ended, or the id is wrong",
            )
            await _close(down, 1000, "no such session")
            return
        via = f" via relay ({peer.label})" if peer.label else " via relay"
        name = params.client[: 100 - len(via)] + via
        try:
            upstream_cm = ws_connect(hosted.address, max_size=MAX_FRAME_BYTES, open_timeout=10)
            upstream = await upstream_cm.__aenter__()
        except (OSError, TimeoutError) as exc:
            await _error(down, request_id, -32000, f"the session does not answer the relay: {exc}")
            await _close(down, 1011, "the session does not answer")
            return
        try:
            await upstream.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "initialize",
                        "params": {
                            "client": name,
                            "client_version": params.client_version,
                            "token": hosted.token,
                        },
                    },
                    ensure_ascii=False,
                )
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._downstream, peer, upstream, tg)
                tg.start_soon(_upstream, upstream, down, tg)
        finally:
            with anyio.CancelScope(shield=True):
                await upstream_cm.__aexit__(None, None, None)

    async def _downstream(self, peer: _Peer, upstream: Any, tg: TaskGroup) -> None:
        """Copy the client's messages to the session, checking the token where it must.

        Side effect: cancels `tg` at the end, which closes the session's side.

        Args:
            peer: The client.
            upstream: The connection to the session.
            tg: The task group of the two directions.
        """
        try:
            async for message in peer.connection:
                text = message if isinstance(message, str) else message.decode()
                if peer.sha is not None and _changes_something(text):
                    if await self.label(peer.sha, new=False) is None:
                        await _error(peer.connection, _id_of(text), UNAUTHENTICATED, REVOKED)
                        await _close(peer.connection, CLOSE_SERVER_TOKEN_REVOKED, REVOKED)
                        break
                await upstream.send(message)
        except ConnectionClosed:
            pass
        tg.cancel_scope.cancel()


REVOKED = "the server token was revoked, or its server ended"
"""Close reason and error message for a client whose server token is no longer valid."""


async def _upstream(upstream: Any, down: ServerConnection, tg: TaskGroup) -> None:
    """Copy the session's messages to the client; pass the session's close on.

    Side effect: cancels `tg` at the end.

    Args:
        upstream: The connection to the session.
        down: The client's connection.
        tg: The task group of the two directions.
    """
    try:
        async for message in upstream:
            await down.send(message)
    except ConnectionClosed:
        pass
    code = upstream.close_code
    reason = upstream.close_reason or ""
    if code in UNSENDABLE_CODES:
        code, reason = 1011, reason or "the session's connection ended"
    await _close(down, code, reason)
    tg.cancel_scope.cancel()


def _changes_something(text: str) -> bool:
    """Tell whether a client message is a request the relay checks the token for first.

    Args:
        text: One message of the client.

    Returns:
        True for a request whose method is not in `READ_ONLY_METHODS`, and for a message that
        cannot be read (the session refuses it; checking costs nothing then).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return True
    if not isinstance(data, dict):
        return True
    return str(data.get("method")) not in READ_ONLY_METHODS


def _id_of(text: str) -> Any:
    """Return the "id" of a JSON-RPC message, or None.

    Args:
        text: The message.

    Returns:
        Its id, or None if it has none or cannot be read.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data.get("id") if isinstance(data, dict) else None


async def _result(connection: ServerConnection, request_id: Any, result: BaseModel) -> None:
    """Send a JSON-RPC result.

    Args:
        connection: The client's connection.
        request_id: The request's id.
        result: The result model.

    Raises:
        ConnectionClosed: If the client disconnected.
    """
    await connection.send(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result.model_dump(mode="json")},
            ensure_ascii=False,
        )
    )


async def _error(
    connection: ServerConnection, request_id: Any, code: int, message: str, data: Any = None
) -> None:
    """Send a JSON-RPC error.

    Args:
        connection: The client's connection.
        request_id: The request's id, or None.
        code: The JSON-RPC error code.
        message: The message.
        data: Details, or None.

    Raises:
        ConnectionClosed: If the client disconnected.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    await connection.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}))


async def _close(connection: Any, code: int, reason: str) -> None:
    """Close a connection with a code and a reason; a connection that is gone is no error.

    Args:
        connection: The connection.
        code: The WebSocket close code.
        reason: The reason, cut to the 123 bytes a close frame holds.
    """
    data = reason.encode()[:123].decode(errors="ignore")
    try:
        await connection.close(code, data)
    except ConnectionClosed, OSError:
        pass
