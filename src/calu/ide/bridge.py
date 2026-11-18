"""The requests of `calu ide`: what an editor extension asks, and who answers.

    editor ──JSON-RPC (one object per line)──► Peer ──► Bridge.handle
                                                          │
          ide.attach, ide.start, ide.detach  ─────────────┼─► SessionLink (calu.ide.link):
          session.call {method, params}      ─────────────┤     one attached session; its
                                                          │     events come back as
                                                          │     `session.event` notifications
          ide.sessions, ide.session.rename,               │
          ide.settings.*, ide.models.*, ide.checker.* ────┼─► the database (calu.ide.access,
                                                          │     calu.ide.database)
          ide.status ─────────────────────────────────────┴─► calu.doctor, PATH

The method table, the error codes and the notifications are specified in
`docs/notes/vscode-extension-spec.md` §2 and §6. One session is attached at a time: an attach
while one is attached detaches first. The bridge never asks on a terminal; a credential that
needs a password is the error `NEEDS_PASSWORD`, and the request is repeated with `password`.

`Bridge` knows nothing about stdin and stdout: `serve` takes the two line callables, so the
tests drive it through in-memory queues and `calu.ide.serve` passes the process's streams.
"""

import contextlib
import getpass
import logging
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import psycopg

from calu import __version__, admin, asuser, doctor, herd, localsocket, session_requests
from calu.acp.peer import Peer, ReadLine, RpcError, WriteLine
from calu.acp.serve import DRAIN_S, RECONNECT_WAIT_S
from calu.credentials import CredentialStore
from calu.db.connection import Db
from calu.ide import database
from calu.ide.access import INVALID_PARAMS, NEEDS_PASSWORD, NO_DATABASE, Access
from calu.ide.database import NO_SESSION, SEVERAL_SESSIONS
from calu.ide.link import Endpoint, SessionLink
from calu.ide.presets import model_of, presets
from calu.protocol.client import RequestFailed, first_set, probe
from calu.protocol.messages import PROTOCOL_VERSION
from calu.session_requests import SessionRequest
from calu.web.settings import _lookup

log = logging.getLogger(__name__)

NO_SERVER = -32013
"""`ide.start`: no `calu serve` claimed the request; it was withdrawn."""

SERVER_REFUSED = -32014
"""`ide.start`: the server refused the request or did not start the session."""

NOT_ATTACHED = -32015
"""`session.call`: no session is attached, or the connection to it is lost."""

METHOD_NOT_FOUND = -32601
"""JSON-RPC's code for a method the bridge does not have."""

KINDS = ("new", "resume", "fork")
"""The values of `ide.start {kind}`."""

VERSION_TIMEOUT_S = 5.0
"""How long `uv --version` may take before uv is reported without a version."""

Handler = Callable[[dict[str, Any]], Awaitable[Any]]
"""Answers one request: takes its params, returns its result."""


def _text(params: dict[str, Any], key: str, required: bool = False) -> str | None:
    """Read a string parameter.

    Args:
        params: The request's params.
        key: The parameter's name.
        required: Whether a missing or empty value is an error.

    Returns:
        The string, or None if it is absent, null or (unless required) empty.

    Raises:
        RpcError: `INVALID_PARAMS` if the value is not a string, or missing though required.
    """
    value = params.get(key)
    if value is None or value == "":
        if required:
            raise RpcError(INVALID_PARAMS, f"the parameter {key!r} is missing")
        return None
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"the parameter {key!r} must be a string")
    return value


def _number(params: dict[str, Any], key: str) -> int | None:
    """Read an integer parameter.

    Args:
        params: The request's params.
        key: The parameter's name.

    Returns:
        The integer, or None if it is absent or null.

    Raises:
        RpcError: `INVALID_PARAMS` if the value is not an integer.
    """
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(INVALID_PARAMS, f"the parameter {key!r} must be an integer")
    return value


def finding_json(finding: doctor.Finding) -> dict[str, Any]:
    """Render one finding of the environment check for the editor.

    Args:
        finding: The finding.

    Returns:
        `{name, ok, level, message, advice}` with the level "ok", "missing" (something no
        session runs without) or "note".
    """
    level = "ok" if finding.ok else "missing" if finding.essential else "note"
    return {
        "name": finding.name,
        "ok": finding.ok,
        "level": level,
        "message": finding.detail,
        "advice": "" if finding.ok else finding.advice,
    }


def program_findings() -> list[doctor.Finding]:
    """Check the programs only an editor's setup cares about: uv, tmux and screen.

    Side effect: runs `uv --version` if uv is on PATH.

    Returns:
        One finding each. None is essential: a missing uv matters for installing and
        updating calu, and tmux or screen only for keeping `calu serve` running after the
        editor's terminal closes.
    """
    found: list[doctor.Finding] = []
    uv = shutil.which("uv")
    detail = "not on PATH"
    if uv is not None:
        detail = uv
        try:
            answer = subprocess.run(
                [uv, "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if answer.returncode == 0 and answer.stdout.strip():
                detail = f"{answer.stdout.strip().splitlines()[0]} ({uv})"
        except OSError, subprocess.SubprocessError:
            pass
    found.append(
        doctor.Finding(
            "uv",
            uv is not None,
            detail,
            "Install it with https://astral.sh/uv/install.sh; calu is installed and updated "
            "with `uv tool install`.",
        )
    )
    for tool, detach in (("tmux", "Ctrl+B D"), ("screen", "Ctrl+A D")):
        path = shutil.which(tool)
        found.append(
            doctor.Finding(
                tool,
                path is not None,
                path or "not on PATH",
                f"apt install {tool} — `calu serve` started inside {tool} keeps running "
                f"when the editor's terminal closes (detach with {detach}).",
            )
        )
    return found


class Bridge:
    """Answers the requests of one editor (see the module docstring)."""

    def __init__(
        self,
        workspace: Path,
        store: CredentialStore,
        tasks: anyio.abc.TaskGroup,
        reconnect_wait_s: float = RECONNECT_WAIT_S,
        claim_wait_s: float | None = None,
        start_wait_s: float | None = None,
        hostname: str | None = None,
        user: str | None = None,
    ) -> None:
        """Create the bridge; `Peer` calls `handle` and `after_response`.

        Args:
            workspace: The editor's workspace folder: the working directory of `calu ide`.
            store: The credential store of the user the bridge runs as.
            tasks: The task group the attached session's connection runs in.
            reconnect_wait_s: How long a lost session connection is tried again.
            claim_wait_s: How long `ide.start` waits for a server to claim its request; None
                uses `calu.session_requests.CLAIM_WAIT_S`.
            start_wait_s: How long `ide.start` waits for the claimed request; None uses
                `calu.session_requests.START_WAIT_S`.
            hostname: The host `ide.start` asks a session for; None uses this host's name.
            user: The OS user `ide.start` asks a session for; None uses this process's.
        """
        self.workspace = workspace.resolve()
        self.access = Access(store)
        self.store = store
        self.reconnect_wait_s = reconnect_wait_s
        self.claim_wait_s = claim_wait_s
        self.start_wait_s = start_wait_s
        self.hostname = hostname or socket.gethostname()
        self.user = user
        self.peer: Peer | None = None
        """The peer the notifications go to; set by `serve` before the first request."""
        self.shutdown = anyio.Event()
        """Set after the response to `ide.shutdown` was written."""
        self._tasks = tasks
        self._link: SessionLink | None = None
        self._attaching = anyio.Lock()
        self._handlers: dict[str, Handler] = {
            "ide.initialize": self._initialize,
            "ide.status": self._status,
            "ide.sessions": self._sessions,
            "ide.session.rename": self._rename,
            "ide.attach": self._attach,
            "ide.detach": self._detach_request,
            "ide.start": self._start,
            "session.call": self._call,
            "ide.settings.get": self._settings_get,
            "ide.settings.set": self._settings_set,
            "ide.settings.unset": self._settings_unset,
            "ide.models.presets": self._models_presets,
            "ide.models.set_all": self._models_set_all,
            "ide.models.set": self._models_set,
            "ide.checker.get": self._checker_get,
            "ide.checker.save": self._checker_save,
            "ide.shutdown": self._shutdown,
        }

    # ------------------------------------------------------------------------- the peer

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        """Answer one request of the editor.

        Args:
            method: The request method (the table in the specification).
            params: Its params.

        Returns:
            The result, a JSON object.

        Raises:
            RpcError: With the code the specification names for what went wrong;
                `METHOD_NOT_FOUND` for an unknown method.
        """
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"calu ide has no method {method!r}")
        return await handler(params)

    async def after_response(self, method: str) -> None:
        """Do what has to follow a written response.

        After `ide.attach` and `ide.start`: the attached session's notifications start to
        flow, so that the editor has the snapshot before the first event. After
        `ide.shutdown`: sets `shutdown`.

        Args:
            method: The method whose response was just written.
        """
        if method in ("ide.attach", "ide.start") and self._link is not None:
            self._link.release()
        if method == "ide.shutdown":
            self.shutdown.set()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification to the editor.

        Args:
            method: "session.event", "ide.link" or "ide.log".
            params: Its params.
        """
        if self.peer is not None:
            await self.peer.notify(method, params)

    async def close(self) -> None:
        """Detach from the session, if one is attached. Side effect: revokes a minted token."""
        async with self._attaching:
            await self._detach()

    # --------------------------------------------------------------------- plain answers

    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.initialize`: who the bridge is.

        Args:
            params: `client` and `client_version` of the editor extension; logged.

        Returns:
            `bridge_version` (calu's version), `protocol_version` (of the session protocol)
            and `workspace`.
        """
        log.info(
            "calu ide %s for %s %s in %s",
            __version__,
            params.get("client") or "an editor",
            params.get("client_version") or "",
            self.workspace,
        )
        return {
            "bridge_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "workspace": str(self.workspace),
        }

    async def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.shutdown`; the bridge ends once the response is written.

        Args:
            params: Unused.

        Returns:
            `{}`.
        """
        return {}

    async def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.status`: what is installed, and whether the database answers.

        Side effects: runs `claude --version` and `uv --version`; opens one database
        connection if a login is readable without a password (a stopped local Docker
        database is not started).

        Args:
            params: Unused.

        Returns:
            `findings` (`calu.doctor.local_findings` plus uv, tmux and screen, each as
            `finding_json`), `database` (`{reachable, place, problem}`, and `needs` with the
            credential's name when only a password is missing) and `server`
            (`{sessions_running}`: the sessions of this host that say they are running;
            whether a `calu serve` answers is not known without asking it for a session).
        """
        local = await anyio.to_thread.run_sync(doctor.local_findings, self.store)
        programs = await anyio.to_thread.run_sync(program_findings)

        async def count(db: Db) -> int:
            """Count the sessions of this host that still say they are running.

            Args:
                db: The connection.

            Returns:
                The number.
            """
            rows = await herd.sessions(db, active_only=True)
            return len([r for r in rows if r.hostname == socket.gethostname() and not r.stale])

        place = await anyio.to_thread.run_sync(self.access.place)
        state: dict[str, Any] = {"reachable": False, "place": place, "problem": ""}
        running = 0
        try:
            running = await self.access.read(count, start_local=False)
            state["reachable"] = True
        except RpcError as error:
            state["problem"] = error.message
            if error.code == NEEDS_PASSWORD and isinstance(error.data, dict):
                state["needs"] = error.data.get("needs")
            running = len(await self._socket_sessions())
        return {
            "findings": [finding_json(finding) for finding in [*local, *programs]],
            "database": state,
            "server": {"sessions_running": running},
        }

    # --------------------------------------------------------------------------- sessions

    async def _sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.sessions` (`calu.ide.database.sessions_view`).

        Args:
            params: `all` (also the sessions of other hosts), `password`.

        Returns:
            `running` and `stored`.

        Raises:
            RpcError: `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        every_host = bool(params.get("all"))
        return await self.access.read(
            lambda db: database.sessions_view(db, self.workspace, every_host),
            _text(params, "password"),
        )

    async def _rename(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.session.rename`: give a session a name (`calu.herd.rename_session`).

        Args:
            params: `session_id` (the full id), `name` ("" removes the name), `password`.

        Returns:
            `{name}`: the stored name.

        Raises:
            RpcError: `INVALID_PARAMS` if the id is not a session id or the name is longer
                than 80 characters; `NO_SESSION` if no session has the id; `NEEDS_PASSWORD`
                or `NO_DATABASE`.
        """
        session_id = _text(params, "session_id", required=True) or ""
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "the parameter 'name' must be a string")
        try:
            uuid.UUID(session_id)
        except ValueError:
            raise RpcError(INVALID_PARAMS, f"{session_id!r} is not a session id") from None

        async def work(db: Db) -> dict[str, Any]:
            """Store the name.

            Args:
                db: The connection.

            Returns:
                `{name}`.

            Raises:
                RpcError: `INVALID_PARAMS` or `NO_SESSION`.
            """
            try:
                return {"name": await herd.rename_session(db, session_id, name)}
            except ValueError as error:
                raise RpcError(INVALID_PARAMS, str(error)) from error
            except LookupError as error:
                raise RpcError(NO_SESSION, str(error)) from error

        return await self.access.read(work, _text(params, "password"))

    async def _attach(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.attach`: attach to a running session.

            `address` given  ─► that address with `token` (no database is asked)
            else             ─► a local socket that needs no token (a session started with
                                `--insecure`, or root's own for a root bridge)
            else             ─► the database: the session is picked and a token minted

        Side effects: detaches first if a session is attached; may write a `client_tokens`
        row; starts the connection, whose events flow after the response.

        Args:
            params: `session` (an id or its beginning; null for the workspace's session),
                `after_seq`, `password`, and for a session that is not found through this
                machine's sockets or database `address` and `token`.

        Returns:
            `{attached: true, session_id, initialize, snapshot, events, complete}`.

        Raises:
            RpcError: `NO_SESSION` if no session matches or it cannot be reached;
                `SEVERAL_SESSIONS` if several match; `NEEDS_PASSWORD` or `NO_DATABASE`; the
                session's own error if it refuses the token.
        """
        hint = _text(params, "session")
        after_seq = _number(params, "after_seq") or 0
        address = _text(params, "address")
        password = _text(params, "password")
        async with self._attaching:
            if address:
                endpoint = Endpoint(address, _text(params, "token") or "")
            else:
                endpoint = await self._find(hint, password)
            return await self._attach_to(endpoint, after_seq)

    async def _detach_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.detach`. Side effect: revokes a token the bridge minted.

        Args:
            params: Unused.

        Returns:
            `{}`, also when no session was attached.
        """
        async with self._attaching:
            await self._detach()
        return {}

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.start`: ask the running `calu serve` for a session and attach to it.

        Side effects: writes a `session_requests` row for this host, OS user and workspace
        and waits for its outcome (withdrawn if no server claims it, or if the editor goes
        away meanwhile); sets the session's name if one was given; mints a client token;
        detaches from the session attached before.

        Args:
            params: `kind` ("new", "resume" or "fork"; default "new"), `claude_session_id`
                (for resume and fork), `fork_before_turn`, `prompt`, `model`, `effort`,
                `project`, `name`, `password`.

        Returns:
            As `ide.attach`.

        Raises:
            RpcError: `NO_SERVER` if no server answered (the message names the command);
                `SERVER_REFUSED` if the server did not start the session (its message);
                `INVALID_PARAMS` for parameters that contradict each other;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        kind = _text(params, "kind") or "new"
        if kind not in KINDS:
            raise RpcError(INVALID_PARAMS, f"kind is one of {', '.join(KINDS)}")
        conversation = _text(params, "claude_session_id")
        if kind != "new" and not conversation:
            raise RpcError(INVALID_PARAMS, f"kind {kind!r} needs claude_session_id")
        before_turn = _number(params, "fork_before_turn")
        if before_turn is not None and kind != "resume":
            raise RpcError(INVALID_PARAMS, "fork_before_turn goes with kind 'resume'")
        name = (_text(params, "name") or "").strip()
        if len(name) > herd.MAX_NAME_CHARS:
            raise RpcError(
                INVALID_PARAMS,
                f"a session's name has at most {herd.MAX_NAME_CHARS} characters",
            )
        user = self.user or _os_user()
        wanted = SessionRequest(
            hostname=self.hostname,
            os_user=user,
            directory=str(self.workspace),
            kind=kind,  # type: ignore[arg-type]  # checked against KINDS above
            claude_session_id=conversation if kind != "new" else None,
            fork_before_turn=before_turn,
            prompt=_text(params, "prompt"),
            model=_text(params, "model"),
            effort=_text(params, "effort"),
            project=_text(params, "project"),
            label=f"calu ide ({user})",
        )
        password = _text(params, "password")

        async def work(db: Db) -> Endpoint:
            """File the request, wait for the session, name it and mint a token.

            Args:
                db: The connection.

            Returns:
                The endpoint of the started session.

            Raises:
                RpcError: `NO_SERVER` or `SERVER_REFUSED`.
            """
            request_id = await session_requests.file(db, wanted)
            try:
                done = await session_requests.wait_for(
                    db, request_id, self.claim_wait_s, self.start_wait_s
                )
            except anyio.get_cancelled_exc_class():
                # The editor went away: take the request back, so that a server does not
                # start a session nobody waits for.
                with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                    with contextlib.suppress(Exception):
                        await session_requests.withdraw(db, request_id)
                raise
            except session_requests.NoServer:
                raise RpcError(
                    NO_SERVER,
                    f"no `calu serve` answered for {user} on {self.hostname}: start it in a "
                    "terminal with `calu serve`",
                ) from None
            except (TimeoutError, LookupError) as error:
                raise RpcError(SERVER_REFUSED, str(error)) from None
            if done.state != "started" or done.session_id is None:
                raise RpcError(
                    SERVER_REFUSED, f"the server did not start the session: {done.message}"
                )
            if name:
                try:
                    await herd.rename_session(db, done.session_id, name)
                except (ValueError, LookupError, psycopg.Error) as error:
                    log.warning("the new session could not be named: %s", error)
            row = await db.one(
                "SELECT coalesce(address, '') FROM sessions WHERE id = %s::uuid",
                (done.session_id,),
            )
            token_id, token = await herd.mint_token(db, done.session_id, "calu ide")
            return Endpoint(str(row[0]) if row else "", token, token_id, done.session_id, password)

        async with self._attaching:
            endpoint = await self.access.read(work, password)
            return await self._attach_to(endpoint, 0)

    async def _call(self, params: dict[str, Any]) -> Any:
        """Answer `session.call`: forward one request to the attached session.

        Args:
            params: `method` (a request of the session protocol) and `params`.

        Returns:
            The session's result.

        Raises:
            RpcError: `NOT_ATTACHED` if no session is attached or the connection is lost;
                the session's own error, with its code, message and data, if it refuses.
        """
        method = _text(params, "method", required=True) or ""
        inner = params.get("params")
        if inner is not None and not isinstance(inner, dict):
            raise RpcError(INVALID_PARAMS, "the parameter 'params' must be an object")
        link = self._link
        if link is None:
            raise RpcError(NOT_ATTACHED, "no session is attached (ide.attach or ide.start)")
        try:
            return await link.request(method, inner or {})
        except RequestFailed as error:
            raise RpcError(
                error.code, str(error.error.get("message") or ""), error.error.get("data")
            ) from error
        except ConnectionError as error:
            raise RpcError(NOT_ATTACHED, f"the session did not answer: {error}") from error

    async def _socket_sessions(self) -> list[tuple[Path, str, str]]:
        """Ask the local sockets that need no token which sessions they belong to.

        Side effects: one short connection per socket; each session logs it. A socket that
        does not answer, or that wants a token, is left out.

        Returns:
            (socket, session id, project directory) per answering session.
        """
        if os.geteuid() == 0:
            paths = await anyio.to_thread.run_sync(asuser.as_root, localsocket.listed)
        else:
            paths = await anyio.to_thread.run_sync(
                localsocket.listed, localsocket.user_root(), os.geteuid()
            )
        found: list[tuple[Path, str, str]] = []
        for path in paths:
            try:
                answer = await probe(f"unix:{path}", "", "calu ide (finding sessions)")
            except OSError, RequestFailed, ConnectionError, TimeoutError:
                continue
            session_id = str(answer.get("calu_session_id") or path.stem)
            found.append((path, session_id, str(answer.get("workspace") or "")))
        return found

    async def _find(self, hint: str | None, password: str | None) -> Endpoint:
        """Find the session `ide.attach` means: through a local socket, else the database.

        Side effect: mints a client token when the session is found through the database.

        Args:
            hint: A session id or its beginning, or None for the workspace's session.
            password: The password of a protected credential, if the request brought one.

        Returns:
            The endpoint.

        Raises:
            RpcError: `NO_SESSION`, `SEVERAL_SESSIONS`, `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        found = await self._socket_sessions()
        if hint:
            chosen = [entry for entry in found if entry[1].startswith(hint)]
        else:
            inside = [
                entry
                for entry in found
                if entry[2] and self.workspace.is_relative_to(Path(entry[2]))
            ]
            deepest = max((len(Path(entry[2]).parts) for entry in inside), default=0)
            chosen = [entry for entry in inside if len(Path(entry[2]).parts) == deepest]
        if len(chosen) > 1:
            raise RpcError(
                SEVERAL_SESSIONS,
                f"{len(chosen)} sessions match; name one by its id",
                {"sessions": [{"id": entry[1], "directory": entry[2]} for entry in chosen]},
            )
        if chosen:
            path, session_id, _directory = chosen[0]
            return Endpoint(f"unix:{path}", "", None, session_id)

        async def work(db: Db) -> Endpoint:
            """Pick the session and mint a token for it.

            Args:
                db: The connection.

            Returns:
                The endpoint.
            """
            rows = await herd.sessions(db, active_only=True)
            row = database.pick_session(rows, hint, self.workspace)
            token_id, token = await herd.mint_token(db, row.id, "calu ide")
            return Endpoint(row.address, token, token_id, row.id, password)

        return await self.access.read(work, password)

    async def _attach_to(self, endpoint: Endpoint, after_seq: int) -> dict[str, Any]:
        """Replace the attached session by the one at `endpoint`.

        The caller holds `_attaching`. Side effects: detaches first; starts the link's task;
        if the connection fails, revokes the token minted for it.

        Args:
            endpoint: Where to connect.
            after_seq: The sequence number the editor has already seen.

        Returns:
            The result of `ide.attach`.

        Raises:
            RpcError: `NO_SESSION` if the address cannot be reached; the session's own error
                if it refuses the token.
        """
        await self._detach()
        link = SessionLink(endpoint, self.notify, self.reconnect_wait_s)
        self._tasks.start_soon(link.run, after_seq)
        try:
            await link.started.wait()
        except anyio.get_cancelled_exc_class():
            link.stop()
            raise
        if link.error is not None:
            await self._revoke(endpoint)
            if isinstance(link.error, RequestFailed):
                failed = link.error
                raise RpcError(
                    failed.code,
                    f"the session at {endpoint.address} refused the client: "
                    f"{failed.error.get('message') or ''}",
                    failed.error.get("data"),
                )
            raise RpcError(NO_SESSION, f"cannot attach to {endpoint.address}: {link.error}")
        self._link = link
        return {"attached": True, "session_id": link.session_id, **link.first}

    async def _detach(self) -> None:
        """Close the attached session's connection and revoke its minted token.

        The caller holds `_attaching`. Does nothing if no session is attached.
        """
        link, self._link = self._link, None
        if link is None:
            return
        link.stop()
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(DRAIN_S):
                await link.ended.wait()
            await self._revoke(link.endpoint)

    async def _revoke(self, endpoint: Endpoint) -> None:
        """Revoke the token the bridge minted for an endpoint. Never raises.

        A failure is logged and reported to the editor as `ide.log`: the token then stays
        usable until the session ends, and `calu token revoke` removes it.

        Args:
            endpoint: The endpoint; one without a minted token is left alone.
        """
        token_id = endpoint.token_id
        if token_id is None:
            return
        try:
            with anyio.move_on_after(DRAIN_S * 5):
                await self.access.read(
                    lambda db: herd.revoke_token(db, token_id), endpoint.password
                )
        except RpcError as error:
            message = (
                f"the client token {token_id} could not be revoked ({error.message}); "
                f"`calu token revoke {token_id}` does it"
            )
            log.warning(message)
            with contextlib.suppress(Exception):
                await self.notify("ide.log", {"level": "warning", "message": message})

    # --------------------------------------------------------- settings, models, checkers

    async def _settings_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.settings.get` (`calu.ide.database.settings_view`).

        Args:
            params: `project` (null for the global scope), `password`.

        Returns:
            `schema`, `rows`, `checkers` and `projects`.

        Raises:
            RpcError: `INVALID_PARAMS` if the project is unknown; `NEEDS_PASSWORD` or
                `NO_DATABASE`.
        """
        project = _text(params, "project")
        return await self.access.read(
            lambda db: database.settings_view(db, project), _text(params, "password")
        )

    async def _settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.settings.set`: store one setting (`calu.admin.set_setting`).

        Args:
            params: `key` (dotted), `value` (the JSON value to store), `project`, `password`.

        Returns:
            `{value}`: the value as it is stored after the change.

        Raises:
            RpcError: `INVALID_PARAMS` if `Config` refuses the value (the message names the
                field) or the key or project is unknown; `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        key = _text(params, "key", required=True) or ""
        if "value" not in params:
            raise RpcError(INVALID_PARAMS, "the parameter 'value' is missing")
        value, project = params["value"], _text(params, "project")

        async def work(db: Db) -> dict[str, Any]:
            """Store the value.

            Args:
                db: The connection.

            Returns:
                `{value}`.
            """
            stored = await admin.set_setting(db, key, value, project)
            return {"value": _lookup(stored, key)}

        return await self.access.change(work, _text(params, "password"))

    async def _settings_unset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.settings.unset`: remove one setting, so that the default applies.

        Args:
            params: `key`, `project`, `password`.

        Returns:
            `{}`.

        Raises:
            RpcError: `INVALID_PARAMS` if the key is malformed or the project is unknown;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        key, project = _text(params, "key", required=True) or "", _text(params, "project")

        async def work(db: Db) -> dict[str, Any]:
            """Remove the value.

            Args:
                db: The connection.

            Returns:
                `{}`.
            """
            await admin.unset_setting(db, key, project)
            return {}

        return await self.access.change(work, _text(params, "password"))

    async def _models_presets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.models.presets`: the presets, and the model each role has now.

        The presets need no database. Without a database login the result still holds them,
        with empty `roles`, `claude: null` and the reason in `problem`.

        Args:
            params: `project` (whose effective models are shown), `password`.

        Returns:
            `presets` (`calu.ide.presets.presets`), `roles`, `claude` and `default_model`
            (`calu.ide.database.model_roles`); `problem` if the roles could not be read.

        Raises:
            RpcError: `NEEDS_PASSWORD` if only a password is missing; `INVALID_PARAMS` if
                the project is unknown.
        """
        project = _text(params, "project")
        result: dict[str, Any] = {"presets": presets(), "roles": [], "claude": None}
        try:
            result.update(
                await self.access.read(
                    lambda db: database.model_roles(db, project), _text(params, "password")
                )
            )
        except RpcError as error:
            if error.code != NO_DATABASE:
                raise
            result["problem"] = error.message
        return result

    async def _models_set_all(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.models.set_all` (`calu.ide.database.set_all_models`).

        Args:
            params: `model` (a Pydantic AI model string, a preset id, or "default" for no
                stored model), `effort` (the checkers' effort; absent removes a stored
                one), `project`, `password`.

        Returns:
            `{changed}`: the role keys that were written.

        Raises:
            RpcError: `INVALID_PARAMS` if a value is refused or the project is unknown;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        model = model_of(_text(params, "model", required=True) or "")
        effort, project = _text(params, "effort"), _text(params, "project")

        async def work(db: Db) -> dict[str, Any]:
            """Store the model for every role.

            Args:
                db: The connection.

            Returns:
                `{changed}`.
            """
            return {"changed": await database.set_all_models(db, model, effort, project)}

        return await self.access.change(work, _text(params, "password"))

    async def _models_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.models.set`: store the model of one role.

        Args:
            params: `role` (a key of `ide.models.presets`' `roles`), `model` (a model
                string, a preset id, or "default" / "" for no stored model), `project`,
                `password`.

        Returns:
            `{}`.

        Raises:
            RpcError: `INVALID_PARAMS` if the role is unknown or the value is refused;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        role = _text(params, "role", required=True) or ""
        model, project = model_of(_text(params, "model") or ""), _text(params, "project")

        async def work(db: Db) -> dict[str, Any]:
            """Store the model.

            Args:
                db: The connection.

            Returns:
                `{}`.
            """
            await database.set_role_model(db, role, model, project)
            return {}

        return await self.access.change(work, _text(params, "password"))

    async def _checker_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.checker.get` (`calu.ide.database.checker_view`).

        Args:
            params: `name`, `password`.

        Returns:
            `{name, route, model, version, enabled, instructions}`.

        Raises:
            RpcError: `INVALID_PARAMS` if no checker has that name; `NEEDS_PASSWORD` or
                `NO_DATABASE`.
        """
        name = _text(params, "name", required=True) or ""
        return await self.access.read(
            lambda db: database.checker_view(db, name), _text(params, "password")
        )

    async def _checker_save(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.checker.save` (`calu.ide.database.save_instructions`).

        Args:
            params: `name`, `instructions`, `base_version` (the version the text was read
                at; null stores whatever is there), `password`.

        Returns:
            `{version}`.

        Raises:
            RpcError: `VERSION_CONFLICT` if the stored version differs from `base_version`
                (`data.version`, `data.instructions`); `INVALID_PARAMS` if the checker is
                unknown or the text is empty; `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        name = _text(params, "name", required=True) or ""
        text = params.get("instructions")
        if not isinstance(text, str):
            raise RpcError(INVALID_PARAMS, "the parameter 'instructions' must be a string")
        base_version = _number(params, "base_version")
        return await self.access.change(
            lambda db: database.save_instructions(db, name, text, base_version),
            _text(params, "password"),
        )


def _os_user() -> str:
    """Name the OS user this process runs as, as a session records it.

    Returns:
        The user name, or "unknown" if the system does not tell it.
    """
    try:
        return getpass.getuser() or "unknown"
    except OSError, KeyError:
        return "unknown"


async def serve(
    read_line: ReadLine,
    write_line: WriteLine,
    workspace: Path,
    store: CredentialStore,
    **options: Any,
) -> int:
    """Serve one editor on a pair of line callables until it disconnects or shuts down.

    Side effects: everything the editor's requests do; at the end the attached session is
    detached and a minted token revoked.

    Args:
        read_line: Reads the next protocol line; "" means the editor closed its end.
        write_line: Writes one protocol line.
        workspace: The editor's workspace folder.
        store: The credential store.
        **options: Further arguments of `Bridge` (the waits, the host and the user).

    Returns:
        0: the editor disconnected or sent `ide.shutdown`.
    """
    async with anyio.create_task_group() as tasks:
        bridge = Bridge(workspace, store, tasks, **options)
        peer = Peer(read_line, write_line, bridge.handle)
        peer.after_response = bridge.after_response
        bridge.peer = peer
        tasks.start_soon(peer.serve)
        await first_set(peer.closed, bridge.shutdown)
        with anyio.move_on_after(DRAIN_S):
            await peer.idle()
        await bridge.close()
        tasks.cancel_scope.cancel()
    return 0
