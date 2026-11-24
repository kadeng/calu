"""`calu serve`: one process, authenticated once, that runs many sessions.

See agent/records/PLAN.md D208.

    password ──► decrypt the `user` credential ──► kept in one ConnectionSource
    loop:  notified (`session_requested`) or every POLL_S seconds
           claim the oldest pending request for this host and OS user
           ──► one task per request:
                 refuse it (too many sessions, no such directory, conversation already runs)
                 or run the session exactly as `calu run` does (`calu.run.run_from`): its own
                 two database connections, its own Claude Code, its own protocol server
                 ──► write "started" with the session id, or "failed" with the reason
    stop:  cancel ──► every session stops in order (`calu.run._run`)

A request is a database row written with the user login (`calu.session_requests`), so who
may start a session is decided by the database's logins, not by who can reach a socket of
this process. The server prints one line per event of a session's life; the conversation is
shown by the session's clients.

The server also listens on one relay port for all its sessions (`calu.relay`, D242): it records
itself in `servers` (`calu.servers`), and a client with a server token reaches every session it
runs through that one port, and may ask it for a new session (a request row filed through the
token, `calu.servers.file_request`).

The server stamps its row every `herd.HEARTBEAT_S` seconds and, before each look for
requests, ends the rows of this user's other servers in its process table whose process is
gone, so that a server that died without ending its row does not keep the requests of its
directory (agent/records/PLAN.md D244).
"""

import getpass
import logging
import os
import signal
import socket
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import psycopg

from calu import asuser, herd, projects_dir, servers, session_requests
from calu.config import load_secrets
from calu.credentials import wipe
from calu.db import SESSION_REQUESTED, ConnectionSource, Db, Dirty, LoginRefused, Notifier
from calu.launch import RunAs
from calu.preflight import LoginProtection
from calu.protocol.messages import RelayServer, RelayStartParams
from calu.run import (
    ERRORS,
    LABEL,
    RunnerFactory,
    RunOptions,
    harden_process,
    one_line,
    run_from,
    say,
)
from calu.servers import Hosted, StartRefused
from calu.session_requests import SessionRequest

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from calu.relay import Relay

log = logging.getLogger(__name__)

POLL_S = 5.0
"""Longest time between two looks for pending requests: a notification may be lost while the
listening connection is away."""

FINISH_WAIT_S = 5.0
"""How long a stopping server waits for the database to record a request's outcome."""

EFFORTS = ("low", "medium", "high", "xhigh", "max", "default")
"""The reasoning efforts a request may name (`RunOptions.effort`)."""

RELAY_START_WAIT_S = 120.0
"""How long `relay.start` waits for the session it asked for. A request is claimed within a
minute or never (`calu_claim_session_request`); the rest is the session's start."""

FINISHED_KEPT = 200
"""How many outcomes of requests the server keeps in memory for `relay.start`."""


class RelayPortTaken(Exception):
    """The relay port named with `--relay-port` cannot be bound; the message says why."""


@dataclass(frozen=True)
class ServeOptions:
    """What `calu serve` was asked to do."""

    max_sessions: int = 10
    """How many sessions run at once; a request beyond that is refused."""
    host: str = "127.0.0.1"
    """Interface the sessions' protocol servers bind; the default accepts only local clients."""
    run_as: RunAs | None = None
    """The user Claude Code runs as in every session (D206); None is calu's own user."""
    idle_timeout_s: float | None = None
    """End a session that has been idle this long with no client attached; None never does."""
    secure: bool = False
    """Every client of every session shows a token, also root on the local socket (D226)."""
    insecure: bool = False
    """Local clients of every session need no token (D227); not together with `secure`."""
    login_protection: LoginProtection | None = None
    """What keeps Claude's commands from calu's logins, passed to every session
    (`RunOptions.login_protection`); None if unknown."""
    projects: Path | None = None
    """The projects directory (D243), resolved and checked (`calu.projects_dir.
    check_projects_dir`): sessions start only in it or below it. None is the current
    directory, resolved (unchecked: the command line checks it)."""
    relay: bool = True
    """Whether the server listens on a relay port (D242); False for `--no-relay`."""
    relay_port: int | None = None
    """The relay's port on 127.0.0.1 as named with `--relay-port` (0: any free port; a named
    port that is taken ends the server); None derives it from the projects directory
    (`calu.projects_dir.relay_ports`, the next one if it is taken)."""


def os_user() -> str:
    """Name the OS user this process runs as, as a session records it.

    Returns:
        The user name, or "unknown" if the system does not tell it.
    """
    try:
        return getpass.getuser() or "unknown"
    except OSError, KeyError:
        return "unknown"


class Refused(Exception):
    """A request the server does not start; the message is the reason for the requester."""


class Server:
    """Answers the session requests of one host and one OS user.

    `sessions` maps the id of each request whose session runs (or is starting) to the
    request. The server changes it; a caller may read it.
    """

    def __init__(
        self,
        source: ConnectionSource,
        hidden: set[str],
        options: ServeOptions | None = None,
        runner_factory: RunnerFactory | None = None,
        checker_models: dict[str, str | Model] | None = None,
        hostname: str | None = None,
        user: str | None = None,
    ) -> None:
        """Prepare a server; nothing is opened until `run`.

        Args:
            source: Where database connections with the user login come from.
            hidden: Names of environment variables Claude Code must not see.
            options: Limits and addresses; None uses the defaults.
            runner_factory: Creates each session's runner; None uses the Agent SDK runner
                (tests pass a fake runner).
            checker_models: Replacements for the configured checker models, or None.
            hostname: The host whose requests are answered; None uses this machine's name.
            user: The OS user whose requests are answered; None uses this process's user.
        """
        self.source = source
        self.hidden = hidden
        self.options = options or ServeOptions()
        self.runner_factory = runner_factory
        self.checker_models = checker_models
        self.hostname = hostname or socket.gethostname()
        self.user = user or os_user()
        self.projects = self.options.projects or Path(os.path.realpath(Path.cwd()))
        """The resolved projects directory: sessions start only in it or below it (D243)."""
        self.sessions: dict[int, SessionRequest] = {}
        self.listening = anyio.Event()
        """Set once the server is subscribed to new requests and has looked for old ones."""
        self.running: dict[str, Hosted] = {}
        """Session row id -> the session as the relay reaches it, for every session that a
        client can attach to."""
        self.relay: Relay | None = None
        """The relay, once it is bound (`_open_relay`); None without one."""
        self.server_id: str | None = None
        """Id of this server's `servers` row; None until recorded, or if it could not be."""
        self._db: Db | None = None
        self._finished: OrderedDict[int, tuple[str | None, str]] = OrderedDict()
        """Request id -> (session id or None, message) of the requests this server finished."""
        self._finished_changed = anyio.Event()

    async def run(self) -> None:
        """Answer requests until the caller cancels; then stop every session.

        Side effects: opens two database connections (requests, notifications) and closes
        them at the end; starts one task per claimed request; prints the lines of the
        server's and the sessions' life.

        Raises:
            psycopg.Error: If the database cannot be reached at the start or refuses the
                user login (then or later: a refused login ends the server).
        """
        db = Db(await self.source.open(), source=self.source)
        try:
            notifier = Notifier(await self.source.open(), source=self.source)
            try:
                await self._serve(db, notifier)
            finally:
                with anyio.CancelScope(shield=True):
                    await notifier.aclose()
        finally:
            with anyio.CancelScope(shield=True):
                await db.aclose()

    async def _serve(self, db: Db, notifier: Notifier) -> None:
        """Claim requests and start their sessions until cancelled.

        Args:
            db: The connection for claiming and finishing requests.
            notifier: The connection that hears of new requests.

        Raises:
            LoginRefused: If the database refuses the user login for good.
        """
        wake = Dirty()
        refused: list[LoginRefused] = []
        notifier.on(SESSION_REQUESTED, lambda _payload: wake.set())
        notifier.on_listening(wake.hint)
        self._db = db
        await self._start_up(db)
        async with anyio.create_task_group() as tg:

            def stop(exc: LoginRefused) -> None:
                """End the server: waiting does not bring a refused login back.

                Args:
                    exc: The server's refusal.
                """
                refused.append(exc)
                tg.cancel_scope.cancel()

            db.on_refused(stop)
            notifier.on_refused(stop)
            tg.start_soon(notifier.run)
            if self.relay is not None:
                tg.start_soon(self.relay.run)
            if self.server_id is not None:
                tg.start_soon(self._beat, db)
            say(
                f"[serve] answering session requests for {self.user}@{self.hostname} in "
                f"{self.projects}; start one with `calu start` in a project directory there"
            )
            try:
                while True:
                    try:
                        await self._end_gone_peers(db)
                        while (request := await self._claim(db)) is not None:
                            self.sessions[request.id] = request
                            tg.start_soon(self._host, db, request)
                    except (psycopg.Error, TimeoutError) as exc:
                        log.warning("looking for session requests failed: %s", exc)
                    self.listening.set()
                    with anyio.move_on_after(POLL_S):
                        await wake.wait()
            finally:
                if self.sessions:
                    say(f"[serve] stopping {len(self.sessions)} session(s)")
                if self.server_id is not None:
                    with anyio.CancelScope(shield=True), anyio.move_on_after(FINISH_WAIT_S):
                        try:
                            await servers.record_end(db, self.server_id)
                        except (psycopg.Error, TimeoutError) as exc:
                            log.warning("recording the end of the server failed: %s", exc)
        if refused:
            raise refused[0]

    async def _beat(self, db: Db) -> None:
        """Stamp this server's heartbeat every `herd.HEARTBEAT_S` seconds until cancelled.

        A server that stops stamping counts as not running after `herd.STALE_AFTER_S`
        (agent/records/PLAN.md D244). If the row has ended while the server runs (an operator
        ended it, or another server took its process for gone), the server records a new row,
        so that it claims requests again; the tokens of the old row stay revoked.

        Side effects: writes `servers.last_seen`; may insert a new `servers` row and change
        `server_id`; prints a line when it does. A failed stamp is logged and tried again at
        the next beat.

        Args:
            db: The server's connection.
        """
        while True:
            await anyio.sleep(herd.HEARTBEAT_S)
            server_id = self.server_id
            if server_id is None:
                return
            try:
                with anyio.fail_after(FINISH_WAIT_S):
                    if await servers.beat(db, server_id):
                        continue
                    self.server_id = await servers.record_start(
                        db,
                        self.hostname,
                        self.user,
                        self.relay.address if self.relay is not None else "",
                        str(self.projects),
                    )
            except (psycopg.Error, TimeoutError) as exc:
                log.warning("the heartbeat of the server failed: %s", exc)
                continue
            if self.relay is not None:
                self.relay.server = self._relay_server()
            say(
                f"[serve] the row of this server ({server_id}) was ended while it ran; it "
                f"recorded itself again as {self.server_id}. Its server tokens were revoked: "
                "`calu token mint --server` prints a new one",
                error=True,
            )

    async def _end_gone_peers(self, db: Db) -> None:
        """End the rows of this host's and user's other servers whose process is gone.

        So a request in the directory of a server that died without ending its row is
        claimed at once (`calu.servers.end_gone_peers`) rather than after the row is stale.
        Errors are logged only (a schema before migration 0040 has no such function).

        Args:
            db: The server's connection.
        """
        if self.server_id is None:
            return
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                ended = await servers.end_gone_peers(db, self.server_id)
        except (psycopg.Error, TimeoutError) as exc:
            log.info("looking for gone servers failed: %s", exc)
            return
        for other in ended:
            say(f"[serve] the server {other} of this user is gone; its row is ended")

    async def session_details(
        self, sha: str | None, session_ids: list[str]
    ) -> dict[str, tuple[str, datetime]]:
        """Read the names and state times of sessions of this server for `relay.sessions`.

        Args:
            sha: SHA-256 of the asking client's server token; None for a client without one
                (`--insecure`), which gets nothing.
            session_ids: The sessions' row ids.

        Returns:
            Session id -> (name, when it entered its current state); empty when the database
            cannot be asked (no token, no row, a schema before migration 0040, an error).
        """
        if sha is None or self._db is None or self.server_id is None:
            return {}
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                return await servers.session_details(self._db, self.server_id, sha, session_ids)
        except (psycopg.Error, TimeoutError) as exc:
            log.info("the names of the sessions could not be read: %s", exc)
            return {}

    async def _claim(self, db: Db) -> SessionRequest | None:
        """Claim the next request meant for this server.

        A recorded server claims through its row (`session_requests.claim_served`, D243): its
        relay's requests, and the operators' requests in its projects directory or in no
        other server's. A server without a row (a schema before migration 0039) claims every
        request of its host and user; `_options` refuses those outside its directory.

        Args:
            db: The server's connection.

        Returns:
            The claimed request, or None if nothing is pending.

        Raises:
            psycopg.Error: On database errors.
        """
        if self.server_id is not None:
            return await session_requests.claim_served(db, self.server_id, os.getpid())
        return await session_requests.claim(db, self.hostname, self.user, os.getpid())

    async def _start_up(self, db: Db) -> None:
        """Bind the relay port (unless `--no-relay`) and record this server (D242, D243).

        Side effects: binds a port on 127.0.0.1 (the named one, or the first free one of the
        ports derived from the projects directory); inserts the `servers` row with the relay's
        address and the projects directory; prints where the relay listens and how a client
        on another machine reaches it. Without the row (a schema older than migration 0039)
        the relay runs, but no server token can be checked, and requests are claimed as
        before (`_claim`).

        Args:
            db: The server's connection.

        Raises:
            RelayPortTaken: If the port named with `--relay-port` cannot be bound, or every
                port of the derived range is taken.
        """
        relay = await self._bind_relay() if self.options.relay else None
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                self.server_id = await servers.record_start(
                    db,
                    self.hostname,
                    self.user,
                    relay.address if relay is not None else "",
                    str(self.projects),
                )
        except (psycopg.Error, TimeoutError) as exc:
            log.info("recording the server failed", exc_info=True)
            say(
                f"[serve] this server could not record itself ({one_line(str(exc))}); its relay "
                "checks no server token, so only `--insecure` clients of this machine use it. "
                "`calu setup` brings the database up to date",
                error=True,
            )
        if relay is None:
            return
        relay.server = self._relay_server()
        self.relay = relay
        relay_port = relay.address.rsplit(":", 1)[-1]
        say(f"[serve] relay for every session of this server: {relay.address}")
        say(
            "[serve] from another machine: `calu token mint --server` here prints a token; "
            f"there: ssh -N -L {relay_port}:127.0.0.1:{relay_port} {self.hostname}, then "
            f"calu attach ws://127.0.0.1:{relay_port}"
        )

    def _relay_server(self) -> RelayServer:
        """Describe this server for the relay's `initialize`.

        Returns:
            Its row id (None if not recorded), host, user and projects directory.
        """
        return RelayServer(
            server_id=self.server_id,
            hostname=self.hostname,
            os_user=self.user,
            projects_directory=str(self.projects),
        )

    async def _bind_relay(self) -> Relay:
        """Bind the relay's port.

            --relay-port N  ──► N, or RelayPortTaken
            (none)          ──► the first free port of relay_ports(projects); each taken one
                                is printed

        Returns:
            The bound relay (not yet running).

        Raises:
            RelayPortTaken: If the named port, or every derived one, cannot be bound.
        """
        # Imported here: the relay imports the protocol server and with it the session, which
        # `calu.cli` must not pay for at its start (`tests/unit/test_startup_imports.py`).
        from calu.relay import Relay

        db = self._db

        async def check(sha: str) -> str | None:
            """Ask the database about a server token of this server.

            Args:
                sha: The token's hash.

            Returns:
                Its label if valid, else None.

            Raises:
                psycopg.Error: On database errors.
            """
            if self.server_id is None or db is None:
                return None
            return await servers.token_label(db, self.server_id, sha)

        relay = Relay(self, check, self._relay_server(), insecure=self.options.insecure)
        named = self.options.relay_port
        if named is not None:
            try:
                await relay.open("127.0.0.1", named)
            except OSError as exc:
                raise RelayPortTaken(
                    f"the relay port {named} cannot be bound ({exc}); name another one with "
                    "--relay-port, or 0 for any free port"
                ) from exc
            return relay
        ports = projects_dir.relay_ports(self.projects)
        for index, port in enumerate(ports):
            try:
                await relay.open("127.0.0.1", port)
            except OSError as exc:
                if index == 0:
                    say(
                        f"[serve] the relay port {port} of {self.projects} is taken ({exc}); "
                        "taking the next free one",
                        error=True,
                    )
                continue
            if index > 0:
                say(
                    f"[serve] the relay takes port {port} instead of {ports[0]}; an SSH forward "
                    f"of {ports[0]} does not reach it",
                    error=True,
                )
            return relay
        raise RelayPortTaken(
            f"every relay port from {projects_dir.RELAY_PORT_FIRST} to "
            f"{projects_dir.RELAY_PORT_LAST} is taken; name one with --relay-port, or 0 for any "
            "free port"
        )

    def hosted(self) -> list[Hosted]:
        """List the sessions a relay client can attach to, newest first.

        Returns:
            The sessions.
        """
        return sorted(self.running.values(), key=lambda one: one.started_at, reverse=True)

    async def relay_start(self, sha: str, label: str, params: RelayStartParams) -> str:
        """Ask this server for a session through a server token, and wait until it runs.

        The request is a `session_requests` row filed through the token
        (`calu.servers.file_request`), which this server then claims like any other.

        Args:
            sha: SHA-256 of the client's server token.
            label: The token's label, for the request's label.
            params: What to start.

        Returns:
            The id of the started session's row.

        Raises:
            StartRefused: If the directory is not in the projects directory
                (`calu.projects_dir.served_path`), the request cannot be filed, the server
                refused or failed to start the session, or nothing happened within
                `RELAY_START_WAIT_S`.
        """
        if self._db is None or self.server_id is None:
            raise StartRefused("this server has no record in the database to file a request with")
        try:
            workspace = projects_dir.served_path(self.projects, params.directory, self.hostname)
        except projects_dir.NotServed as exc:
            raise StartRefused(str(exc)) from None
        wanted = SessionRequest(
            hostname=self.hostname,
            os_user=self.user,
            directory=str(workspace),
            kind=params.kind,
            claude_session_id=params.claude_session_id,
            fork_before_turn=params.fork_before_turn,
            prompt=params.prompt,
            model=params.model,
            effort=params.effort,
            project=params.project,
            label=f"relay: {label}" if label else "relay",
        )
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                request_id = await servers.file_request(self._db, self.server_id, sha, wanted)
        except psycopg.errors.InsufficientPrivilege as exc:
            raise StartRefused("the server token is not valid any more") from exc
        except (psycopg.Error, TimeoutError) as exc:
            raise StartRefused(f"the request could not be filed: {one_line(str(exc))}") from exc
        with anyio.move_on_after(RELAY_START_WAIT_S):
            while request_id not in self._finished:
                changed = self._finished_changed
                await changed.wait()
        if request_id not in self._finished:
            raise StartRefused("calu serve did not start the session in time")
        session_id, message = self._finished[request_id]
        if session_id is None:
            raise StartRefused(message or "the session did not start")
        return session_id

    def _record_outcome(self, request_id: int, session_id: str | None, message: str) -> None:
        """Remember a request's outcome for a `relay_start` that waits for it.

        Args:
            request_id: The request.
            session_id: The started session's row id, or None if it failed.
            message: The reason of a failure.
        """
        self._finished[request_id] = (session_id, message)
        while len(self._finished) > FINISHED_KEPT:
            self._finished.popitem(last=False)
        changed, self._finished_changed = self._finished_changed, anyio.Event()
        changed.set()

    def _options(self, request: SessionRequest) -> RunOptions:
        """Translate a request into what `calu.run` runs, or refuse it.

        Args:
            request: The claimed request.

        Returns:
            The options of a session that stays until a client shuts it down.

        Raises:
            Refused: If the server's limit is reached, the directory is not in the projects
                directory (D243) or does not exist, an id is
                no UUID, the effort is unknown, or this server already runs the conversation
                that is to be resumed.
        """
        others = [other for key, other in self.sessions.items() if key != request.id]
        if len(others) >= self.options.max_sessions:
            raise Refused(
                f"this server already runs {len(others)} sessions, its limit (`calu serve "
                "--max-sessions`); shut one down first"
            )
        try:
            workspace = projects_dir.served_path(self.projects, request.directory, self.hostname)
        except projects_dir.NotServed as exc:
            raise Refused(str(exc)) from None
        if not workspace.is_dir():
            raise Refused(f"{request.directory} is not a directory on {self.hostname}")
        if request.claude_session_id is not None:
            try:
                uuid.UUID(request.claude_session_id)
            except ValueError:
                raise Refused(
                    f"{request.claude_session_id!r} is no Claude session id (a UUID)"
                ) from None
        if request.effort is not None and request.effort not in EFFORTS:
            raise Refused(f"unknown effort {request.effort!r}; one of {', '.join(EFFORTS)}")
        continues = request.kind == "resume" and request.fork_before_turn is None
        if continues and any(
            other.kind == "resume"
            and other.fork_before_turn is None
            and other.claude_session_id == request.claude_session_id
            for other in others
        ):
            raise Refused(
                f"this server already runs the conversation {request.claude_session_id}; "
                "attach to it, or fork it"
            )
        return RunOptions(
            workspace=workspace,
            project=request.project,
            prompt=request.prompt,
            resume_id=request.claude_session_id if request.kind == "resume" else None,
            fork_id=request.claude_session_id if request.kind == "fork" else None,
            fork_before_turn=request.fork_before_turn,
            model=request.model,
            effort=request.effort,  # type: ignore[arg-type]  # checked against EFFORTS above
            interactive=False,
            host=self.options.host,
            stay=True,
            stay_idle_s=self.options.idle_timeout_s,
            quiet=True,
            run_as=self.options.run_as,
            secure=self.options.secure,
            insecure=self.options.insecure,
            login_protection=self.options.login_protection,
        )

    async def _host(self, db: Db, request: SessionRequest) -> None:
        """Run the session of one claimed request until it ends; never raises.

        Side effects: writes the request's outcome ("started" once a client can find the
        session, "failed" with the reason otherwise), prints the session's lines with a label
        of its own, and removes the request from `sessions` at the end. A session whose start
        cannot be reported (no session row, or "started" not written) is stopped at once: its
        requester would never learn of it, and no client would ever attach to it.

        Args:
            db: The server's connection, for the request's outcome.
            request: The claimed request.
        """
        LABEL.set(f"[{Path(request.directory).name}#{request.id}] ")
        errors: list[str] = []
        ERRORS.set(errors)
        answered = False
        session_scope = anyio.CancelScope()
        own_token: list[str] = []
        registered: list[str] = []

        async def serving(_address: str, token: str) -> None:
            """Keep the session's own client token for the relay (never printed).

            Args:
                _address: Where the session listens (`ready` gets it again).
                token: The session's client token.
            """
            own_token[:] = [token]

        async def ready(session_id: str | None, address: str) -> None:
            """Tell the requester that the session can be attached to, or stop the session.

            Side effects: writes "started" for the request; if there is no session row or
            that write fails, prints the reason as an error (which collects it in `errors`)
            and cancels the session, so that `_host` writes "failed" instead.

            Args:
                session_id: The id of the session's row, or None if it could not be written.
                address: Where the session's protocol server listens.
            """
            nonlocal answered
            if session_id is None:
                reason = "the session's row could not be written to the database"
            else:
                if own_token:
                    # Before "started" is written: a relay client told of the session may
                    # attach at once.
                    self.running[session_id] = Hosted(
                        session_id,
                        address,
                        own_token[0],
                        request.directory,
                        datetime.now(UTC),
                    )
                    registered.append(session_id)
                answered = await self._finish(db, request.id, "started", session_id)
                if answered:
                    say(f"[serve] started: session {session_id} in {request.directory}")
                    return
                reason = f"the start of session {session_id} could not be recorded for the request"
            # An error line is also collected in `errors`; its last entry is the reason
            # `_host` writes with "failed".
            say(f"[serve] {reason}", error=True)
            say("[serve] stopping the session: nobody can learn that it runs")
            session_scope.cancel()

        what = request.kind + (f" {request.claude_session_id}" if request.claude_session_id else "")
        say(f"[serve] requested by {request.label or 'a client'}: {what}")
        try:
            options = self._options(request)
            # `ready` cancels this scope only; a stop of the whole server still reaches the
            # caller as a cancellation.
            with session_scope:
                await run_from(
                    self.source,
                    self.hidden,
                    options,
                    self.runner_factory,
                    self.checker_models,
                    on_serving=serving,
                    on_ready=ready,
                )
        except Refused as exc:
            say(f"[serve] refused: {exc}", error=True)
        except Exception as exc:
            # One session's failure must not end the server and the other sessions.
            # The traceback to the log file only; the line below is what stderr shows.
            log.info("the session of request %d failed", request.id, exc_info=True)
            say(f"[serve] failed: {exc}", error=True)
        finally:
            with anyio.CancelScope(shield=True):
                for session_id in registered:
                    self.running.pop(session_id, None)
                if not answered:
                    reason = errors[-1] if errors else "the session ended before it was ready"
                    await self._finish(db, request.id, "failed", None, reason)
                self.sessions.pop(request.id, None)
                say("[serve] ended")

    async def _finish(
        self, db: Db, request_id: int, outcome: str, session_id: str | None, message: str = ""
    ) -> bool:
        """Write a request's outcome; a database that stays away is logged, not waited for.

        Side effect: remembers the outcome for a `relay_start` that waits for it, whether or
        not it was written.

        Args:
            db: The server's connection.
            request_id: The request.
            outcome: "started" or "failed".
            session_id: The session's row id, or None.
            message: The reason of a failure.

        Returns:
            True if the outcome was written.
        """
        text = message.removeprefix("calu: ").removeprefix("[serve] ")
        written = False
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                written = await session_requests.finish(
                    db,
                    request_id,
                    outcome,  # type: ignore[arg-type]
                    session_id,
                    text,
                )
        except (psycopg.Error, TimeoutError) as exc:
            log.warning("recording the outcome of session request %d failed: %s", request_id, exc)
        self._record_outcome(
            request_id, session_id if written and outcome == "started" else None, text
        )
        return written


async def serve(
    url: str | bytearray,
    options: ServeOptions | None = None,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    user: str | None = None,
) -> int:
    """Answer session requests until SIGINT or SIGTERM, then stop every session.

    Side effects: with `options.run_as`, switches the process's file access and the programs
    it starts to that user for good (`calu.asuser.enter`). Clears the process's "dumpable"
    flag (`calu.run.harden_process`), loads
    the checker keys, installs handlers for SIGINT and SIGTERM for the time it runs, and
    everything `Server.run` does. A `bytearray` passed as `url` is wiped; a copy of the
    connection string stays in this process's memory, so that sessions can be started and
    lost connections reopened.

    Args:
        url: The user login's connection string or record.
        options: Limits and addresses; None uses the defaults.
        runner_factory: Creates each session's runner; None uses the Agent SDK runner.
        checker_models: Replacements for the configured checker models, or None.
        user: The OS user whose requests are answered; None is the user of this process, or
            with `options.run_as` the name or the number of that user.

    Returns:
        0 after a stop by signal; 1 if the database refused the user login for good, or the
        relay port named with `--relay-port` cannot be bound.

    Raises:
        psycopg.Error: If the database cannot be reached at the start.
    """
    hidden = load_secrets()
    harden_process()
    if options is not None and options.run_as is not None:
        asuser.enter(options.run_as)
    source = ConnectionSource(url)
    if isinstance(url, bytearray):
        wipe(url)
    run_as = options.run_as if options is not None else None
    if user is None and run_as is not None:
        user = run_as.user or str(run_as.uid)
    server = Server(source, hidden, options, runner_factory, checker_models, user=user)
    code = 0
    async with anyio.create_task_group() as tg:

        async def until_signal() -> None:
            """Cancel the server at the first SIGINT or SIGTERM."""
            with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                async for _ in signals:
                    tg.cancel_scope.cancel()
                    return

        tg.start_soon(until_signal)
        try:
            await server.run()
        except LoginRefused as exc:
            say(
                f"calu: the database refuses the user login ({exc}); the session "
                "credential was probably replaced by a later `calu setup`. The server stops.",
                error=True,
            )
            code = 1
        except RelayPortTaken as exc:
            say(f"calu: {exc}", error=True)
            code = 1
        tg.cancel_scope.cancel()
    return code
