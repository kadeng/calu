"""`calu run`: starts one checked Claude Code session in the foreground (PLAN.md phase 3).

    password ──► decrypt the `session` credential ──► two database connections
                 │                                     queries + notifications
                 └► the password, the key and the connection string are wiped
    load configuration, rules and checker instructions (calu.store.ConfigStore)
    create the sessions row, start Claude Code (calu.session.Session, calu.claude.SdkRunner)
    serve the protocol on a WebSocket (calu.protocol.server) and print its address and token
    print events to the terminal, read further messages from stdin
    reload the configuration on `config_changed`, continue on `escalation_resolved`

The Claude Code process never sees calu's secrets: the connection string is wiped before Claude
starts, the checker API keys are blanked in its environment (`calu.config.load_secrets`,
`SdkRunner.hidden_env`), and its sandbox hides `~/.config/calu` (PLAN.md D15).

Clients (`calu attach`, an IDE, a web interface) connect to the WebSocket address with the
token; several at once. The terminal output of this command shows the same events, so a
session can also be watched where it was started.
"""

import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
import psycopg
from pydantic_ai.models import Model

from calu import preflight
from calu.bus import Bus
from calu.claude.runner import ClaudeRunner
from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig, load_secrets
from calu.credentials import wipe
from calu.db import CONFIG_CHANGED, ESCALATION_RESOLVED, Db, Dirty, Notifier, connect
from calu.protocol.messages import (
    AssistantText,
    AssistantThinking,
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    Log,
    SessionState,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.protocol.server import SessionServer
from calu.session import Session, SessionError
from calu.store import ConfigStore, DatabaseSessionStore, ProjectSettings
from calu.tokens import new_token, token_hash

log = logging.getLogger(__name__)

START_TIMEOUT_S = 10.0
"""How long `calu run` waits for the protocol server to bind its socket."""

QUIT_COMMANDS = ("/quit", "/exit")
"""Lines that end the session when they are typed instead of a message."""

RunnerFactory = Callable[[Path, ClaudeConfig, set[str]], ClaudeRunner]
"""Creates the runner of one Claude session from workspace, Claude settings and the names of
the environment variables that Claude Code must not see."""

Serving = Callable[[str, str], Awaitable[None]]
"""Called with the WebSocket address and the client token once the protocol server listens."""


@dataclass(frozen=True)
class RunOptions:
    """What `calu run` was asked to do.

    `prompt` is sent as the first user message. `interactive` reads further messages from stdin
    until end of input or `/quit`; without it the command ends when Claude has nothing left to
    do, which is the form for unattended runs (`calu run -p "..." --batch`). A client that is
    attached when that moment comes keeps the run alive until it detaches or sends `shutdown`.
    """

    workspace: Path
    project: str | None = None
    prompt: str | None = None
    resume_id: str | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] | None = None
    interactive: bool = True
    show_thinking: bool = False
    host: str = "127.0.0.1"
    """Interface the protocol server binds; the default accepts only local clients."""
    port: int = 0
    """Port of the protocol server; 0 lets the operating system choose a free one."""
    serve: bool = True
    """Serve the protocol at all; False runs the session without clients (used by tests)."""


def format_event(event: Event) -> str | None:
    """Render one session event as one terminal line.

    Args:
        event: The event.

    Returns:
        The line, or None for events that the terminal does not show (token deltas, file
        links, subagent lists).
    """
    match event:
        case SessionState():
            return f"[{event.state}]"
        case UserInput():
            return f"> {one_line(event.text)}"
        case AssistantThinking():
            return f"  (thinking) {one_line(event.text)}"
        case AssistantText():
            return f"{_agent(event.agent_id)}{event.text}"
        case ToolUse():
            return f"  {_agent(event.agent_id)}· {event.name} {one_line(str(event.input))}"
        case ToolResult():
            mark = "!" if event.is_error else "="
            return f"  {mark} {one_line(event.content)}"
        case CheckResult():
            comment = f" comment: {event.comment}" if event.comment else ""
            return (
                f"  [{event.route} checker: {event.decision}] {event.step_summary}"
                f"{f' ({event.reason})' if event.reason else ''}{comment}"
            )
        case EscalationRequest():
            return (
                f"\n*** escalation {event.escalation_id}: {event.tool_name}\n"
                f"    {event.reason}\n"
                f"    resolve it with: calu resolve {event.escalation_id} allow|deny\n"
            )
        case EscalationResolved():
            return f"*** escalation {event.escalation_id}: {event.decision}"
        case TurnResult():
            return f"[turn ended: {event.subtype}]"
        case Log():
            return f"[{event.level}] {event.message}"
        case _:
            return None


def _agent(agent_id: str | None) -> str:
    """Return the prefix that names a subagent.

    Args:
        agent_id: The protocol agent id, or None for the main agent.

    Returns:
        "" for the main agent, else "<id>: ".
    """
    return "" if agent_id is None else f"{agent_id}: "


def one_line(text: str, limit: int = 300) -> str:
    """Return `text` on one line, cut to `limit` characters.

    Args:
        text: The text.
        limit: Maximum number of characters.

    Returns:
        The shortened single-line text.
    """
    joined = " ".join(text.split())
    return joined if len(joined) <= limit else joined[:limit] + " [...]"


class Console:
    """Prints session events to stdout. The stand-in client until phase 4."""

    def __init__(self, show_thinking: bool = False) -> None:
        """Create a console printer.

        Args:
            show_thinking: Also print Claude's thinking.
        """
        self.show_thinking = show_thinking

    async def __call__(self, event: Event) -> None:
        """Print one event.

        Args:
            event: The event.
        """
        if isinstance(event, AssistantThinking) and not self.show_thinking:
            return
        line = format_event(event)
        if line is not None:
            print(line, flush=True)


class IdleSignal:
    """Event sink that reports when the session has nothing left to do.

    Used for unattended runs (`calu run -p "..."` without `--interactive`): the run ends when
    the session is idle again after it has worked, or when it stopped or failed.
    """

    WORKING = ("running", "checking", "tool_call", "escalated")
    """States that count as "the session has started working"."""

    def __init__(self) -> None:
        """Create a signal that is not set."""
        self._event = anyio.Event()
        self._worked = False

    async def __call__(self, event: Event) -> None:
        """Set the signal when the session becomes idle after having worked.

        Args:
            event: The session event.
        """
        if not isinstance(event, SessionState):
            return
        if event.state in self.WORKING:
            self._worked = True
        elif event.state in ("stopped", "failed") or (self._worked and event.state == "idle"):
            self._event.set()

    async def wait(self) -> None:
        """Wait until the session is idle again, stopped or failed."""
        await self._event.wait()


def both(first: Callable[[Event], Awaitable[None]], second: Callable[[Event], Awaitable[None]]):
    """Combine two event sinks into one.

    Args:
        first: Called first with every event.
        second: Called afterwards with every event.

    Returns:
        A sink that calls both, in order.
    """

    async def sink(event: Event) -> None:
        """Pass `event` to both sinks.

        Args:
            event: The event.
        """
        await first(event)
        await second(event)

    return sink


async def read_lines(send: Callable[[str], Awaitable[None]]) -> None:
    """Read lines from stdin and pass them on until end of input or a quit command.

    Blank lines are skipped. Reading runs in a worker thread, so it does not block the session.

    Args:
        send: Called with each line (without the line end).
    """
    while True:
        line = await anyio.to_thread.run_sync(sys.stdin.readline, abandon_on_cancel=True)
        if not line:
            return
        text = line.strip()
        if not text:
            continue
        if text in QUIT_COMMANDS:
            return
        await send(text)


async def run_session(
    url: str | bytearray,
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
) -> int:
    """Run one session until it ends, and report what happened.

    Side effects: opens two database connections, creates a `sessions` row, starts a Claude
    Code process, prints events to stdout and, with `options.interactive`, reads messages from
    stdin. `url` is wiped before Claude Code starts.

    Args:
        url: The `calu_session` connection string (a `bytearray` is wiped by this function).
        options: What to run.
        runner_factory: Creates the runner from workspace, Claude settings and the names of the
            hidden environment variables; None uses `SdkRunner` (tests pass a fake runner).
        checker_models: Replacements for the configured checker models; None uses the models
            from the database (tests pass fake models).
        on_serving: Awaited with the WebSocket address and the client token once the protocol
            server listens, in addition to printing them (a supervisor or a test uses it).

    Returns:
        0 if the session ended normally, 1 if it failed (the reason was printed).

    Raises:
        psycopg.Error: If the database cannot be reached or rejects the login.
    """
    hidden = load_secrets()
    conn = await connect(url)
    listener = await connect(url)
    if isinstance(url, bytearray):
        wipe(url)  # Claude Code starts below; nothing must still hold the credential
    try:
        return await _run(
            conn, listener, hidden, options, runner_factory, checker_models, on_serving
        )
    finally:
        await conn.close()
        await listener.close()


async def _run(
    conn: psycopg.AsyncConnection,
    listener: psycopg.AsyncConnection,
    hidden: set[str],
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
) -> int:
    """Load the settings, start the session and drive it until it ends.

    Args:
        conn: Connection for queries.
        listener: Connection for notifications only.
        hidden: Names of environment variables Claude Code must not see.
        options: What to run.
        runner_factory: Creates the runner; None uses `SdkRunner`.
        checker_models: Replacements for the configured checker models, or None.
        on_serving: Awaited with the address and the token once the server listens, or None.

    Returns:
        0 if the session ended normally, 1 if it failed.

    Raises:
        psycopg.Error: On database errors while loading the settings.
    """
    db = Db(conn)
    config_store = ConfigStore(db, options.workspace, options.project)
    settings = await config_store.load()
    console = Console(options.show_thinking)
    idle = IdleSignal()
    bus = Bus()
    store = DatabaseSessionStore(db)
    session = Session(
        options.workspace,
        settings,
        store,
        lambda: (runner_factory or SdkRunner)(options.workspace, session.config.claude, hidden),
        both(bus.publish, both(console, idle)),
        project=options.project,
        checker_models=checker_models,
        startup_warnings=preflight.warnings(settings.config),
    )

    async def on_settings(new: ProjectSettings) -> None:
        """Apply reloaded settings and report it.

        Args:
            new: The newly loaded settings.
        """
        session.apply_settings(new)
        await console(Log(level="info", message="configuration reloaded"))

    notifier = Notifier(listener)
    notifier.on(ESCALATION_RESOLVED, lambda payload: _note(session, payload))
    dirty = Dirty()
    notifier.on(CONFIG_CHANGED, lambda _payload: dirty.set())
    failed = False
    async with anyio.create_task_group() as tg:
        tg.start_soon(notifier.run)
        tg.start_soon(config_store.reload_loop, dirty, on_settings)
        server: SessionServer | None = None
        try:
            await session.start(tg, options.resume_id, options.model, options.effort)
            if options.serve:
                # After the start, so that the session row and its id exist when the first
                # client asks; the events of the start are in the bus and are replayed.
                token = new_token()
                server = SessionServer(session, bus, token_hash(token), tg)
                tg.start_soon(_listen, server, options, token, store, on_serving)
            if options.prompt:
                await session.send_chat(ChatSendParams(text=options.prompt))
            if options.interactive:
                await read_lines(lambda text: session.send_chat(ChatSendParams(text=text)))
            elif server is not None:
                await _until_done(idle, server, bus)
            else:
                await idle.wait()
        except SessionError as exc:
            print(f"calu: {exc}", file=sys.stderr)
            failed = True
        finally:
            with anyio.CancelScope(shield=True):
                await session.stop()
            tg.cancel_scope.cancel()
    return 1 if failed or session.state == "failed" else 0


async def _listen(
    server: SessionServer,
    options: RunOptions,
    token: str,
    store: DatabaseSessionStore,
    on_serving: Serving | None = None,
) -> None:
    """Serve the protocol and print the address and the token once; never raises.

    The token's hash is recorded with the session (`client_tokens`), so an operator can see
    which sessions accept clients and revoke one; the token itself is only printed here.

    Args:
        server: The protocol server.
        options: Host and port to bind.
        token: The client token to print.
        store: The session store, for the token's hash.
        on_serving: Awaited with the address and the token as well, or None.
    """
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.listen, options.host, options.port)
            with anyio.move_on_after(START_TIMEOUT_S):
                await server.ready.wait()
            if not server.ready.is_set():
                print("calu: the protocol server did not start", flush=True)
                return
            print(f"[clients] {server.address}", flush=True)
            print(
                f"[clients] attach with: calu attach {server.address} --token {token}", flush=True
            )
            if store.session_id is not None:
                try:
                    await store.add_client_token(token_hash(token), server.address)
                except Exception:
                    log.exception("recording the client token failed")
            if on_serving is not None:
                await on_serving(server.address, token)
    except OSError as exc:
        print(f"calu: the protocol server could not start: {exc}", flush=True)


async def _until_done(idle: IdleSignal, server: SessionServer, bus: Bus) -> None:
    """Wait until an unattended run is finished, or a client ends it.

    A run without `--interactive` ends when Claude has nothing left to do. A client that is
    attached at that moment keeps the session alive, because it may want to send the next
    message: the run then ends when that client sends `shutdown` or the last client detaches.

    Args:
        idle: The idle signal of the session.
        server: The protocol server, for its `shutdown` event.
        bus: The bus, for the number of attached clients.
    """
    async with anyio.create_task_group() as tg:

        async def wait_idle() -> None:
            """End the wait when the session is idle and nobody is watching."""
            await idle.wait()
            if bus.clients:
                log.info("the session is idle; waiting for its %d client(s)", bus.clients)
                await bus.no_clients.wait()
            tg.cancel_scope.cancel()

        async def wait_shutdown() -> None:
            """End the wait when a client asked the process to stop."""
            await server.shutdown.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(wait_idle)
        tg.start_soon(wait_shutdown)


def _note(session: Session, payload: object) -> None:
    """Pass an `escalation_resolved` notification on to the session.

    Args:
        session: The running session.
        payload: The notification payload; it must hold an integer "id".
    """
    if isinstance(payload, dict):
        try:
            session.note_resolution(int(payload["id"]))
        except KeyError, TypeError, ValueError:
            log.warning("escalation_resolved payload without a usable id: %r", payload)
