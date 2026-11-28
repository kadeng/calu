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
          ide.status ─────────────────────────────────────┼─► calu.doctor, PATH
                                                          │
          ide.paths, ide.fs.* ────────────────────────────┴─► FileSync (calu.ide.filesync):
                                                                the project's files, for an
                                                                editor that works on a copy

The method table, the error codes and the notifications are specified in
`agent/records/notes/vscode-extension-spec.md` §2 and §6, those of the file requests in
`agent/records/notes/jetbrains-sync-spec.md`. One session is attached at a time: an attach
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
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import psycopg

from calu import (
    __version__,
    admin,
    asuser,
    doctor,
    herd,
    localsocket,
    servers,
    session_requests,
    temp_rules,
)
from calu.acp.peer import Peer, ReadLine, RpcError, WriteLine
from calu.acp.serve import DRAIN_S, RECONNECT_WAIT_S
from calu.credentials import CredentialStore
from calu.db.connection import Db
from calu.ide import database
from calu.ide.access import INVALID_PARAMS, NEEDS_PASSWORD, NO_DATABASE, Access
from calu.ide.database import NO_SESSION, SEVERAL_SESSIONS
from calu.ide.files import CAPABILITIES
from calu.ide.filesync import FileSync
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

START_CANCELLED = -32018
"""`ide.start`: the editor took the request back with `ide.start.cancel`."""

METHOD_NOT_FOUND = -32601
"""JSON-RPC's code for a method the bridge does not have."""

KINDS = ("new", "resume", "fork")
"""The values of `ide.start {kind}`."""

REPORT_WAIT_S = 2.0
"""How long a file request waits for the session's answer to `files.user_edit`."""

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


def _password(params: dict[str, Any]) -> str | None:
    """Read the `password` parameter; a blank password is a password like any other.

    Args:
        params: The request's params.

    Returns:
        The password, also an empty one, or None if the parameter is absent or null.

    Raises:
        RpcError: `INVALID_PARAMS` if the value is not a string.
    """
    value = params.get("password")
    if value is not None and not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, "the parameter 'password' must be a string")
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
        A finding for each of them. No finding is essential: a missing uv matters for installing and
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
        **file_options: Any,
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
            **file_options: Further arguments of `calu.ide.filesync.FileSync` (`poll_s`,
                `quiet_s`, `upload_idle_s`, `max_file_bytes`).
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
        self._start_waits: list[anyio.CancelScope] = []
        """One scope per `ide.start` that waits for a server; `ide.start.cancel` cancels them."""
        self._report_failed = False
        self.files = FileSync(
            self.workspace, self.notify, tasks, self._report_user_edit, **file_options
        )
        """The file requests (`ide.paths`, `ide.fs.*`) and the change notifications."""
        self._handlers: dict[str, Handler] = {
            **self.files.handlers,
            "ide.ping": self._ping,
            "ide.initialize": self._initialize,
            "ide.status": self._status,
            "ide.unlock": self._unlock,
            "ide.sessions": self._sessions,
            "ide.session.rename": self._rename,
            "ide.attach": self._attach,
            "ide.detach": self._detach_request,
            "ide.start": self._start,
            "ide.start.cancel": self._start_cancel,
            "session.call": self._call,
            "ide.settings.get": self._settings_get,
            "ide.settings.set": self._settings_set,
            "ide.settings.unset": self._settings_unset,
            "ide.models.presets": self._models_presets,
            "ide.models.set_all": self._models_set_all,
            "ide.models.set": self._models_set,
            "ide.checker.get": self._checker_get,
            "ide.checker.save": self._checker_save,
            "ide.rules.list": self._rules_list,
            "ide.rules.add": self._rules_add,
            "ide.rules.remove": self._rules_remove,
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
        `ide.fs.subscribe`: the change notifications start to flow. After `ide.shutdown`:
        sets `shutdown`.

        Args:
            method: The method whose response was just written.
        """
        if method in ("ide.attach", "ide.start") and self._link is not None:
            self._link.release()
        await self.files.after_response(method)
        if method == "ide.shutdown":
            self.shutdown.set()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification to the editor.

        Args:
            method: "session.event", "ide.link", "ide.log" or "ide.fs.changed".
            params: Its params.
        """
        if self.peer is not None:
            await self.peer.notify(method, params)

    async def close(self) -> None:
        """End the bridge's work: detach from the session and end the file requests.

        Side effects: revokes a minted token; removes the temporary files of uploads that
        were still open and stops the file watcher.
        """
        await self.files.close()
        async with self._attaching:
            await self._detach()

    async def _from_session(self, method: str, params: dict[str, Any]) -> None:
        """Take a notification of the session link: for the file requests, then the editor.

        Side effects: the session's events tell `FileSync` which files the agent edits
        (held files, and a look at the files after a tool call); after a reconnect that
        missed events the session's state is read again.

        Args:
            method: "session.event" or "ide.link".
            params: Its params.
        """
        if method == "session.event":
            inner = params.get("params")
            self.files.session_event(
                str(params.get("method") or ""), inner if isinstance(inner, dict) else {}
            )
        elif method == "ide.link":
            self.files.link_state(str(params.get("state") or ""))
            if params.get("state") == "attached" and params.get("complete") is False:
                self._tasks.start_soon(self._follow_again)
        await self.notify(method, params)

    async def _follow_again(self) -> None:
        """Read the attached session's state after a reconnect that missed events.

        Side effect: the held files start empty from the state the session reports. Never
        raises: without an answer nothing is held.
        """
        link = self._link
        if link is None:
            return
        try:
            with anyio.fail_after(REPORT_WAIT_S * 5):
                snapshot = await link.request("state.get")
        except RequestFailed, ConnectionError, TimeoutError:
            self.files.turn.detach()
            return
        state = snapshot.get("state") if isinstance(snapshot, dict) else None
        self.files.turn.attach(link.session_id, state, [])

    async def _report_user_edit(self, paths: list[str]) -> None:
        """Tell the attached session that the user writes files through the editor.

        The session then leaves the files out of the write set of a command that runs at
        that moment and counts them as unreviewed (`files.user_edit`). Side effect: the
        session's own directories (its answer) are excluded from the file requests. Never
        raises: without an attached session nothing is sent, and a session that does not
        answer within `REPORT_WAIT_S` or does not know the request is logged once.

        Args:
            paths: The files, relative to the workspace; empty only asks for the session's
                directories.
        """
        link = self._link
        if link is None or link.is_lost:
            return
        try:
            with anyio.fail_after(REPORT_WAIT_S):
                answer = await link.request("files.user_edit", {"paths": paths})
        except (RequestFailed, ConnectionError, TimeoutError) as error:
            if paths and not self._report_failed:
                self._report_failed = True
                log.warning(
                    "the session was not told about the editor's file changes (%s); a "
                    "command that runs at the same time may count them as its own",
                    str(error) or type(error).__name__,
                )
            return
        if isinstance(answer, dict):
            self.files.exclude(answer.get("excluded"))

    # --------------------------------------------------------------------- plain answers

    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.initialize`: who the bridge is.

        Args:
            params: `client` and `client_version` of the editor extension; logged.

        Returns:
            `bridge_version` (calu's version), `protocol_version` (of the session protocol),
            `workspace` and `capabilities` (`calu.ide.files.CAPABILITIES`: what this bridge
            has beyond the first version of the protocol).
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
            "capabilities": list(CAPABILITIES),
        }

    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.ping`: the bridge is alive.

        Args:
            params: `nonce` (any JSON value; optional).

        Returns:
            `{nonce, time}`: the request's nonce (null without one) and the bridge's clock
            in seconds since the epoch.
        """
        return {"nonce": params.get("nonce"), "time": time.time()}

    async def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.shutdown`; the bridge ends once the response is written.

        Args:
            params: Unused.

        Returns:
            `{}`.
        """
        return {}

    async def _unlock(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.unlock`: try the user password on the user login, and hold it.

        Side effects: `calu.ide.access.Access.unlock`. From then on the requests use the
        held login and need no `password`; nothing is written to disk or to the log.

        Args:
            params: `password` (required; a blank one is tried like any other).

        Returns:
            `{unlocked, reading, changing}`.

        Raises:
            RpcError: `INVALID_PARAMS` without `password`; `NEEDS_PASSWORD` if the password
                opened nothing (`data.needs`, `data.unlocked`); `NO_DATABASE` if there is no
                login to open (`calu.logins.no_login`, `calu.logins.old_logins`), the store
                cannot be read or the database cannot be reached.
        """
        password = _password(params)
        if password is None:
            raise RpcError(INVALID_PARAMS, "the parameter 'password' is missing")
        return await self.access.unlock(password)

    async def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.status`: what is installed, and whether the database answers.

        Side effects:
            - Runs `claude --version`, `uv --version` and the sandbox test of `calu.doctor`
              (a start of `bwrap`).
            - Reads Claude Code's settings files.
            - Opens one database connection if a login is readable without a password. A
              stopped local Docker database is not started.
            - Through that connection, closes the open escalations of sessions that are
              gone (`herd.sessions`, `herd.close_gone`).
            - Without a database, connects once to each token-free local socket and counts
              those sessions instead.

        Args:
            params: Unused.

        Returns:
            `findings` (`calu.doctor.local_findings` plus uv, tmux and screen, each as
            `finding_json`), `database` (`{reachable, place, problem}`, and `needs` with the
            credential's name when only a password is missing), `server`
            (`{sessions_running, servers_running}`: the sessions of this host that say they
            are running, and the `calu serve` processes of this host that run). A server
            runs (`calu.servers.running`) if its row is not ended, its heartbeat is within the
            last minute and its process is not known to be gone. `servers_running` is None
            when that is not known: no database, a login that may not read `servers`, or a
            schema older than migration 0040,
            `locked` (a protected credential exists and no login is held: the editor asks
            for the one password, `ide.unlock`) and `unlocked` (the held logins' names).
        """
        local = await anyio.to_thread.run_sync(doctor.local_findings, self.store)
        programs = await anyio.to_thread.run_sync(program_findings)

        async def count(db: Db) -> tuple[int, int | None]:
            """Count the sessions and the servers of this host that still say they run.

            Args:
                db: The connection.

            Returns:
                The number of sessions, and the number of servers (None if the `servers`
                rows cannot be read with this login or schema).
            """
            host = socket.gethostname()
            rows = await herd.sessions(db, active_only=True)
            sessions = len([r for r in rows if r.hostname == host and not r.stale])
            try:
                alive = await servers.running(db, hostname=host)
            except psycopg.Error as error:
                log.info("the servers of this host were not read: %s", error)
                return sessions, None
            return sessions, len(alive)

        place = await anyio.to_thread.run_sync(self.access.place)
        state: dict[str, Any] = {"reachable": False, "place": place, "problem": ""}
        running = 0
        servers_running: int | None = None
        try:
            running, servers_running = await self.access.read(count, start_local=False)
            state["reachable"] = True
        except RpcError as error:
            state["problem"] = error.message
            if error.code == NEEDS_PASSWORD and isinstance(error.data, dict):
                state["needs"] = error.data.get("needs")
            running = len(await self._socket_sessions())
        return {
            "findings": [finding_json(finding) for finding in [*local, *programs]],
            "database": state,
            "server": {"sessions_running": running, "servers_running": servers_running},
            "locked": self.access.locked(),
            "unlocked": self.access.unlocked(),
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
            _password(params),
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

        return await self.access.read(work, _password(params))

    async def _attach(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.attach`: attach to a session; resume a named one that is dormant.

            `address` given  ─► that address with `token` (no database is asked)
            else             ─► a local socket that needs no token (a session started with
                                `--insecure`, or root's own for a root bridge)
            else             ─► the database: the session is picked and a token minted
            `session` named, none of these runs it (D280: dormant, e.g. after a restart
            of `calu serve`):
                a running session continues its conversation ─► attach to that one
                else ─► resume the conversation through `calu serve`, as `ide.start`
                        {kind: "resume"} does, in the session's directory

        Side effects:
            - Detaches first if a session is attached.
            - Address given: connects to it.
            - Otherwise, a local socket: connects once to each token-free local socket to
              find the session (each session logs that).
            - Otherwise, the database: closes the open escalations of gone sessions
              (`herd.sessions`) and may write a `client_tokens` row.
            - Session named and dormant: a resume writes a `session_requests` row and waits
              for the server (`_request_session`).
            - Starts the connection, whose events flow after the response.

        Args:
            params: `session` (an id or its beginning; null for the workspace's session),
                `after_seq`, `password`, and for a session that is not found through this
                machine's sockets or database `address` and `token`.

        Returns:
            `{attached: true, session_id, initialize, snapshot, events, complete}`. After a
            resume or for the session that continues the conversation, `session_id` is that
            session's (not the one asked for) and the events start at 0, whatever
            `after_seq` said.

        Raises:
            RpcError: `NO_SESSION` if no session matches (and none named is dormant) or it
                cannot be reached; `SEVERAL_SESSIONS` if several match; `NEEDS_PASSWORD` or
                `NO_DATABASE`; the session's own error if it refuses the token; for a resume
                the errors of `_request_session` (`NO_SERVER`, `SERVER_REFUSED`,
                `START_CANCELLED`).
        """
        hint = _text(params, "session")
        after_seq = _number(params, "after_seq") or 0
        address = _text(params, "address")
        password = _password(params)
        async with self._attaching:
            if address:
                endpoint = Endpoint(address, _text(params, "token") or "")
                return await self._attach_to(endpoint, after_seq)
            try:
                endpoint = await self._find(hint, password)
                return await self._attach_to(endpoint, after_seq)
            except RpcError as error:
                if error.code != NO_SESSION or not hint:
                    raise
                woken = await self._dormant(hint, password)
                if woken is None:
                    raise
                if isinstance(woken, Endpoint):
                    return await self._attach_to(woken, 0)
        # Outside `_attaching`: `_request_session` takes it itself once the server answered.
        log.info("resuming the dormant session %s (Claude %s)", hint, woken.claude_session_id)
        return await self._request_session(woken, "", password)

    async def _dormant(self, hint: str, password: str | None) -> Endpoint | SessionRequest | None:
        """Find out how to continue the session `hint` names when it does not run (D280).

        The session is looked up among those of this host and of the OS user this bridge runs
        as that have a Claude session id (`herd.dormant_session`). If a session that serves
        clients already continues its conversation, that session is meant. Such a session
        can be one that another editor started or one that calu resumed after an end.
        Otherwise the conversation is resumed in the session's directory.
        A bridge that runs as root for root itself resumes nothing: root's own sessions are
        `calu run` processes, which no `calu serve` resumes. A bridge in a root process that
        works for a user (`asuser.active`, the chat pane of `calu web --sudo`) resumes that
        user's sessions through the user's `calu serve`, like a bridge of the user.

        Side effects: reads `sessions`; for a running successor, writes a `client_tokens` row.

        Args:
            hint: The session id, or its beginning, that `ide.attach` named.
            password: The password of a protected credential, if the request brought one.

        Returns:
            The endpoint of the running successor (token minted); else the resume request
            to file; None if no such session exists, it has no Claude session id, or it
            still runs (and was not found for another reason, which the caller reports).

        Raises:
            RpcError: `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        if os.geteuid() == 0 and asuser.active() is None:
            return None
        # The sessions are recorded with the OS user they work for (`session_os_user`), whose
        # home holds the transcripts: this process's. The request goes to the server that
        # `ide.start` asks (`self.user`, which only tests set).
        recorded, user = _os_user(), self.user or _os_user()

        async def work(db: Db) -> Endpoint | SessionRequest | None:
            """Look the session and its successors up.

            Args:
                db: The connection.

            Returns:
                As `_dormant`.
            """
            row = await herd.dormant_session(db, self.hostname, recorded, None, hint)
            if row is None or row.claude_session_id is None:
                return None
            if row.ended_at is None and not row.stale:
                return None
            successors = [
                other
                for other in await herd.sessions(db, active_only=True)
                if other.claude_session_id == row.claude_session_id
                and other.hostname == self.hostname
                and other.address
                and not other.stale
            ]
            if successors:
                newest = max(successors, key=lambda other: other.started_at)
                token_id, token = await herd.mint_token(db, newest.id, "calu ide")
                return Endpoint(newest.address, token, token_id, newest.id, password)
            return SessionRequest(
                hostname=self.hostname,
                os_user=user,
                directory=row.directory,
                kind="resume",
                claude_session_id=row.claude_session_id,
                label=f"calu ide ({user})",
            )

        return await self.access.read(work, password)

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
        return await self._request_session(wanted, name, _password(params))

    async def _request_session(
        self, wanted: SessionRequest, name: str, password: str | None
    ) -> dict[str, Any]:
        """File a session request, wait until `calu serve` started it, and attach to it.

        Side effects: writes a `session_requests` row and waits for its outcome (withdrawn if
        no server claims it, if the editor goes away meanwhile, or on `ide.start.cancel`);
        sets the session's name if one is given; mints a client token; detaches from the
        session attached before. Takes `_attaching` once the server answered.

        Args:
            wanted: The request.
            name: The new session's name; "" leaves it unnamed.
            password: The password of a protected credential, if the request brought one.

        Returns:
            As `ide.attach`.

        Raises:
            RpcError: `NO_SERVER` if no server answered (the message names the command);
                `SERVER_REFUSED` if the server did not start the session (its message);
                `START_CANCELLED` if `ide.start.cancel` took the request back;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        user = wanted.os_user
        # Only the wait for the server can be taken back (`ide.start.cancel`): `work` drops
        # this scope from `_start_waits` in the step in which the server's answer arrives, so
        # a cancel after that finds nothing to take back and the request attaches.
        waiting = anyio.CancelScope()

        async def work(db: Db) -> Endpoint:
            """File the request, wait for the session, name it and mint a token.

            Side effect: as soon as the request is finished (started or failed), the request
            can no longer be taken back (`waiting` leaves `_start_waits`).

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
                # No await between the answer and this line: a cancel either came before the
                # answer (and ended the wait) or finds the scope gone.
                if waiting in self._start_waits:
                    self._start_waits.remove(waiting)
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

        with waiting:
            self._start_waits.append(waiting)
            try:
                async with self._attaching:
                    endpoint = await self.access.read(work, password)
                    if waiting in self._start_waits:
                        self._start_waits.remove(waiting)
                    return await self._attach_to(endpoint, 0)
            finally:
                if waiting in self._start_waits:
                    self._start_waits.remove(waiting)
        raise RpcError(START_CANCELLED, "the request for a session was taken back")

    async def _start_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.start.cancel`: take back the `ide.start` requests that still wait.

        Side effects: each waiting `ide.start` withdraws its `session_requests` row (so that
        no server starts a session nobody waits for) and fails with `START_CANCELLED`. A
        request whose outcome the server has reported is not affected. Once its wait returns,
        the request is no longer counted here. It then names the session if a name was given,
        mints a token and attaches to the started session. Only a cancel that arrives while
        the wait is still reading the row that says "started" ends the request; the session
        then runs with nobody attached (`calu sessions` lists it).

        Args:
            params: Unused.

        Returns:
            `{cancelled: n}`: how many requests were taken back.
        """
        waits = list(self._start_waits)
        for waiting in waits:
            waiting.cancel()
        return {"cancelled": len(waits)}

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
            raise RpcError(
                NOT_ATTACHED, "no session is attached: attach to a session or start one first"
            )
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

        Side effects: connects once to each token-free local socket (each session logs
        that); through the database it closes the open escalations of gone sessions
        (`herd.sessions`) and mints a client token for the session it finds.

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
        link = SessionLink(endpoint, self._from_session, self.reconnect_wait_s)
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
        self._report_failed = False
        self.files.turn.attach(
            link.session_id, link.first["snapshot"].get("state"), link.first["events"]
        )
        await self._report_user_edit([])
        return {"attached": True, "session_id": link.session_id, **link.first}

    async def _detach(self) -> None:
        """Close the attached session's connection and revoke its minted token.

        The caller holds `_attaching`. Does nothing if no session is attached.
        """
        link, self._link = self._link, None
        if link is None:
            return
        self.files.turn.detach()
        self.files.exclude(None)
        link.stop()
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(DRAIN_S):
                await link.ended.wait()
            await self._revoke(link.endpoint)

    async def _revoke(self, endpoint: Endpoint) -> None:
        """Revoke the token the bridge minted for an endpoint. Never raises.

        A failure, or no answer within `DRAIN_S * 5` seconds, is logged and reported to the
        editor as `ide.log`: the token then stays
        usable until the session ends, and `calu token revoke` removes it.

        Args:
            endpoint: The endpoint; one without a minted token is left alone.
        """
        token_id = endpoint.token_id
        if token_id is None:
            return
        problem = ""
        try:
            with anyio.move_on_after(DRAIN_S * 5) as scope:
                await self.access.read(
                    lambda db: herd.revoke_token(db, token_id), endpoint.password
                )
            if scope.cancelled_caught:
                problem = f"no answer within {DRAIN_S * 5:g} s"
        except RpcError as error:
            problem = error.message
        if problem:
            message = (
                f"the client token {token_id} could not be revoked ({problem}); "
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
            lambda db: database.settings_view(db, project), _password(params)
        )

    async def _settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.settings.set`: store one setting (`calu.admin.set_setting`).

        Args:
            params: `key` (dotted), `value` (the JSON value to store), `project`, `password`.

        Returns:
            `{value}`: the value as it is stored after the change; `offer` (`{key, text}`,
            `calu.admin.enable_offer`) when the editor should offer to switch a related
            setting on as well (it asks the user and sends its own `ide.settings.set`).

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
                `{value}`, and `offer` (`calu.admin.enable_offer`) when a related switch is
                still off.
            """
            stored = await admin.set_setting(db, key, value, project)
            answer: dict[str, Any] = {"value": _lookup(stored, key)}
            offer = await admin.enable_offer(db, key, value, project)
            if offer is not None:
                answer["offer"] = offer
            return answer

        return await self.access.change(work, _password(params))

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

        return await self.access.change(work, _password(params))

    def _attached_id(self) -> str | None:
        """Return the id of the attached session, for the temporary rules' scope.

        Returns:
            The session's id; None if no session is attached or its id is not known yet.
        """
        return (self._link.session_id or None) if self._link is not None else None

    async def _rules_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.rules.list`: the temporary rules that count now (`calu.temp_rules`).

        Args:
            params: `password`.

        Returns:
            `{rules, conversation, project}` (`database.rules_view`): every active rule,
            each with `applies` for the attached session.

        Raises:
            RpcError: `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        session_id = self._attached_id()

        async def work(db: Db) -> dict[str, Any]:
            """Read the rules.

            Args:
                db: The connection.

            Returns:
                The view.
            """
            return await database.rules_view(db, session_id)

        return await self.access.read(work, _password(params))

    async def _rules_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.rules.add`: store a temporary rule for the checkers.

        The rule is given either as `command`, the arguments of the chat's `/rule` as typed
        (`[duration] [--session | --project | --global] text`, `temp_rules.split_command`),
        or as `text` with `duration` and `scope`. The scope "session" (the default) binds it
        to the attached session's conversation and "project" to its project.

        Side effects: writes `temporary_rules` with the user login; running sessions read
        the change at once.

        Args:
            params: `command`, or `text` with optional `duration` and `scope`; `password`.

        Returns:
            `{rule}`: the stored rule with its `line`.

        Raises:
            RpcError: `INVALID_PARAMS` if neither `command` nor `text` is given or the rule
                is refused (the message says why); `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        command = _text(params, "command")
        if command is not None:
            duration, scope, text = temp_rules.split_command(command)
        else:
            text = _text(params, "text", required=True) or ""
            duration, scope = _text(params, "duration"), _text(params, "scope")
        session_id, by = self._attached_id(), f"calu ide:{_os_user()}"

        async def work(db: Db) -> dict[str, Any]:
            """Store the rule.

            Args:
                db: The connection.

            Returns:
                `{rule}`.
            """
            return await database.add_rule(db, session_id, text, duration, scope, by)

        return await self.access.change(work, _password(params))

    async def _rules_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.rules.remove`: end a temporary rule before it expires.

        Side effects: marks the rule as removed with the user login; running sessions read
        the change at once.

        Args:
            params: `id` (the rule's number, or text like "12" or "#12"), `password`.

        Returns:
            `{rule}`: the removed rule with its `line`.

        Raises:
            RpcError: `INVALID_PARAMS` if `id` is no rule number or no active rule has it;
                `NEEDS_PASSWORD` or `NO_DATABASE`.
        """
        raw = str(params.get("id", "")).strip().lstrip("#")
        if isinstance(params.get("id"), bool) or not raw.isdigit():
            raise RpcError(INVALID_PARAMS, "the parameter 'id' must be a rule's number")
        rule_id, session_id, by = int(raw), self._attached_id(), f"calu ide:{_os_user()}"

        async def work(db: Db) -> dict[str, Any]:
            """Remove the rule.

            Args:
                db: The connection.

            Returns:
                `{rule}`.
            """
            return await database.remove_rule(db, session_id, rule_id, by)

        return await self.access.change(work, _password(params))

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
                    lambda db: database.model_roles(db, project), _password(params)
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

        return await self.access.change(work, _password(params))

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

        return await self.access.change(work, _password(params))

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
        return await self.access.read(lambda db: database.checker_view(db, name), _password(params))

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
            _password(params),
        )


def _os_user() -> str:
    """Name the OS user this bridge works for, as a session records it.

    That is the user the process switched to (`asuser.active`: `calu web --sudo` or `calu
    serve --web` as root for a user, whose chat pane runs this bridge), else the user the
    process runs as.

    Returns:
        The user name (a uid as text for a switched-to uid without a name), or "unknown" if
        the system does not tell it.
    """
    switched = asuser.active()
    if switched is not None:
        return switched.user or str(switched.uid)
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
