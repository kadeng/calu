"""`calu serve`: one process, authenticated once, that runs many sessions (PLAN.md D208).

    password ──► decrypt the `session` credential ──► kept in one ConnectionSource
    loop:  notified (`session_requested`) or every POLL_S seconds
           claim the oldest pending request for this host and OS user
           ──► one task per request:
                 refuse it (too many sessions, no such directory, conversation already runs)
                 or run the session exactly as `calu run` does (`calu.run.run_from`): its own
                 two database connections, its own Claude Code, its own protocol server
                 ──► write "started" with the session id, or "failed" with the reason
    stop:  cancel ──► every session stops in order (`calu.run._run`)

A request is a database row written with the operator login (`calu.session_requests`), so who
may start a session is decided by the database's logins, not by who can reach a socket of
this process. The server prints one line per event of a session's life; the conversation is
shown by the session's clients.
"""

import getpass
import logging
import os
import signal
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import psycopg
from pydantic_ai.models import Model

from calu import asuser, session_requests
from calu.config import load_secrets
from calu.credentials import wipe
from calu.db import SESSION_REQUESTED, ConnectionSource, Db, Dirty, LoginRefused, Notifier
from calu.launch import RunAs
from calu.run import ERRORS, LABEL, RunnerFactory, RunOptions, harden_process, run_from, say
from calu.session_requests import SessionRequest

log = logging.getLogger(__name__)

POLL_S = 5.0
"""Longest time between two looks for pending requests: a notification may be lost while the
listening connection is away."""

FINISH_WAIT_S = 5.0
"""How long a stopping server waits for the database to record a request's outcome."""

EFFORTS = ("low", "medium", "high", "xhigh", "max", "default")
"""The reasoning efforts a request may name (`RunOptions.effort`)."""


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
            source: Where database connections with the session login come from.
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
        self.sessions: dict[int, SessionRequest] = {}
        self.listening = anyio.Event()
        """Set once the server is subscribed to new requests and has looked for old ones."""

    async def run(self) -> None:
        """Answer requests until the caller cancels; then stop every session.

        Side effects: opens two database connections (requests, notifications) and closes
        them at the end; starts one task per claimed request; prints the lines of the
        server's and the sessions' life.

        Raises:
            psycopg.Error: If the database cannot be reached at the start or refuses the
                session login (then or later: a refused login ends the server).
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
            LoginRefused: If the database refuses the session login for good.
        """
        wake = Dirty()
        refused: list[LoginRefused] = []
        notifier.on(SESSION_REQUESTED, lambda _payload: wake.set())
        notifier.on_listening(wake.hint)
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
            say(
                f"[serve] answering session requests for {self.user}@{self.hostname}; start "
                "one with `calu start` in a project directory"
            )
            try:
                while True:
                    try:
                        while (
                            request := await session_requests.claim(
                                db, self.hostname, self.user, os.getpid()
                            )
                        ) is not None:
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
        if refused:
            raise refused[0]

    def _options(self, request: SessionRequest) -> RunOptions:
        """Translate a request into what `calu.run` runs, or refuse it.

        Args:
            request: The claimed request.

        Returns:
            The options of a session that stays until a client shuts it down.

        Raises:
            Refused: If the server's limit is reached, the directory does not exist, an id is
                no UUID, the effort is unknown, or this server already runs the conversation
                that is to be resumed.
        """
        others = [other for key, other in self.sessions.items() if key != request.id]
        if len(others) >= self.options.max_sessions:
            raise Refused(
                f"this server already runs {len(others)} sessions, its limit (`calu serve "
                "--max-sessions`); shut one down first"
            )
        workspace = Path(request.directory)
        if not workspace.is_absolute() or not workspace.is_dir():
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
        )

    async def _host(self, db: Db, request: SessionRequest) -> None:
        """Run the session of one claimed request until it ends; never raises.

        Side effects: writes the request's outcome ("started" once a client can find the
        session, "failed" with the reason otherwise), prints the session's lines with a label
        of its own, and removes the request from `sessions` at the end.

        Args:
            db: The server's connection, for the request's outcome.
            request: The claimed request.
        """
        LABEL.set(f"[{Path(request.directory).name}#{request.id}] ")
        errors: list[str] = []
        ERRORS.set(errors)
        answered = False

        async def ready(session_id: str | None, address: str) -> None:
            """Tell the requester that the session can be attached to.

            Args:
                session_id: The id of the session's row, or None if it could not be written.
                address: Where the session's protocol server listens.
            """
            nonlocal answered
            if session_id is None:
                errors.append("the session's row could not be written to the database")
                return
            answered = await self._finish(db, request.id, "started", session_id)
            say(f"[serve] started: session {session_id} in {request.directory}")

        what = request.kind + (f" {request.claude_session_id}" if request.claude_session_id else "")
        say(f"[serve] requested by {request.label or 'a client'}: {what}")
        try:
            options = self._options(request)
            await run_from(
                self.source,
                self.hidden,
                options,
                self.runner_factory,
                self.checker_models,
                on_ready=ready,
            )
        except Refused as exc:
            say(f"[serve] refused: {exc}", error=True)
        except Exception as exc:
            # One session's failure must not end the server and the other sessions.
            log.exception("the session of request %d failed", request.id)
            say(f"[serve] failed: {exc}", error=True)
        finally:
            with anyio.CancelScope(shield=True):
                if not answered:
                    reason = errors[-1] if errors else "the session ended before it was ready"
                    await self._finish(db, request.id, "failed", None, reason)
                self.sessions.pop(request.id, None)
                say("[serve] ended")

    @staticmethod
    async def _finish(
        db: Db, request_id: int, outcome: str, session_id: str | None, message: str = ""
    ) -> bool:
        """Write a request's outcome; a database that stays away is logged, not waited for.

        Args:
            db: The server's connection.
            request_id: The request.
            outcome: "started" or "failed".
            session_id: The session's row id, or None.
            message: The reason of a failure.

        Returns:
            True if the outcome was written.
        """
        try:
            with anyio.fail_after(FINISH_WAIT_S):
                return await session_requests.finish(
                    db,
                    request_id,
                    outcome,  # type: ignore[arg-type]
                    session_id,
                    message.removeprefix("calu: ").removeprefix("[serve] "),
                )
        except (psycopg.Error, TimeoutError) as exc:
            log.warning("recording the outcome of session request %d failed: %s", request_id, exc)
            return False


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
        url: The `calu_session` connection string or record.
        options: Limits and addresses; None uses the defaults.
        runner_factory: Creates each session's runner; None uses the Agent SDK runner.
        checker_models: Replacements for the configured checker models, or None.
        user: The OS user whose requests are answered; None is the user of this process, or
            with `options.run_as` the name or the number of that user.

    Returns:
        0 after a stop by signal; 1 if the database refused the session login for good.

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
                f"calu: the database refuses the session login ({exc}); the session "
                "credential was probably replaced by a later `calu setup`. The server stops.",
                error=True,
            )
            code = 1
        tg.cancel_scope.cancel()
    return code
