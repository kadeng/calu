"""`calu run`: starts one checked Claude Code session in the foreground (PLAN.md phase 3).

    password ──► decrypt the `session` credential ──► two database connections
                 │                                     queries + notifications
                 └► the password, the derived key and the decrypted buffer are wiped;
                    the connection string stays in one object in this process's memory
    find the project (`--project`, or the one registered for the directory)
    load configuration, rules and checker instructions (calu.store.ConfigStore)
    create the sessions row, start Claude Code (calu.session.Session, calu.claude.SdkRunner)
    serve the protocol on a WebSocket (calu.protocol.server) and print its address and token
    print events to the terminal, read further messages from stdin
    reload the configuration on `config_changed`, continue on `escalation_resolved`

The Claude Code process never gets calu's secrets: the connection string is in no environment
variable, command line or file that calu hands to it, the checker API keys are blanked in its
environment (`calu.config.load_secrets`, `SdkRunner.hidden_env`), and its sandbox hides
`~/.config/calu` (PLAN.md D15).

What "the credentials are dropped before Claude Code starts" (REQUIREMENTS 16) means here: the
password, the key derived from it and the buffer the credential was decrypted into are wiped
before Claude Code starts. The connection string itself is kept, in `ConnectionSource`, inside
the calu process and nowhere else, because an unattended run has to survive a restarted
database or a dropped connection:

    connection lost ──► statements wait (tool calls wait in the gate: fail closed)
                   ──► reconnect with backoff ──► bind the session again, LISTEN again
                   ──► re-read the configuration ──► continue
                   └─► the server refuses the login for good (the credential was replaced,
                       the role or the database is gone): say why, stop the session, exit 1

`harden_process` makes the memory of the calu process unreadable for other processes of the
same user, which is what "out of reach of the Claude Code session" (REQUIREMENTS 15) rests on.

Clients (`calu attach`, an IDE, a web interface) connect to the WebSocket address with the
token; several at once. The terminal output of this command shows the same events, so a
session can also be watched where it was started.
"""

import ctypes
import dataclasses
import functools
import logging
import os
import socket
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
import psycopg
from pydantic import ValidationError
from pydantic_ai.models import Model

from calu import herd, preflight
from calu.bus import Bus
from calu.claude.runner import ClaudeRunner, Fork
from calu.claude.sdk_runner import SdkRunner
from calu.clock import ClockWatch
from calu.config import AutoEscalate, ClaudeConfig, load_secrets
from calu.credentials import wipe
from calu.db import (
    CONFIG_CHANGED,
    ESCALATION_RESOLVED,
    ConnectionSource,
    Db,
    Dirty,
    LoginRefused,
    Notifier,
)
from calu.protocol.messages import (
    AssistantText,
    AssistantThinking,
    AutoEscalateChanged,
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    HonestyUpdate,
    Log,
    SessionState,
    State,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.protocol.server import SessionServer
from calu.session import Session, SessionError
from calu.store import ConfigStore, DatabaseSessionStore, ProjectSettings, UnknownProject
from calu.store.config import RELOAD_DEBOUNCE_S
from calu.store.projects import AmbiguousProject, project_for_directory
from calu.tokens import new_token, token_hash

log = logging.getLogger(__name__)

START_TIMEOUT_S = 10.0
"""How long `calu run` waits for the protocol server to bind its socket."""

QUIT_COMMANDS = ("/quit", "/exit")
"""Lines that end the session when they are typed instead of a message."""

IDLE_POLL_S = 0.5
"""Longest time between two evaluations of "is the run finished" (`IdleSignal.wait`)."""

QUEUED_PICKUP_S = 15.0
"""How long an idle session may list a queued message or an open turn before an unattended run
ends anyway (`IdleSignal`); Claude Code takes a queued message within moments."""

STOP_RECONNECT_S = 5.0
"""How long the last writes of an ending session wait for a database that is away."""

HOLD_AFTER_FAILED_RELOADS = 3
"""Failed configuration reloads in a row after which every checked step waits for a human.
Stored settings that cannot be applied at all (invalid values, a project row that is gone) hold
the session at the first failure, because retrying does not repair them."""

HOLD_ALL = AutoEscalate(commands=True, edits=True, subagents=True)
"""The auto-escalation categories of a session whose configuration is known to be stale."""

PR_SET_DUMPABLE = 4
"""`prctl` option that sets whether other processes of the same user may read this one."""

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
    fork_before_turn: int | None = None
    """With `resume_id`: branch the conversation off before this turn instead of continuing it
    (PLAN.md D36). The new session gets its own id, so the original stays as it was."""
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] | None = None
    interactive: bool = True
    show_thinking: bool = False
    socket_path: Path | None = None
    """Serve a Unix socket at this path instead of a TCP port (PLAN.md §6): the file system then
    decides who may connect, and `sessions.address` records it as `unix:<path>` (D29)."""
    host: str = "127.0.0.1"
    """Interface the protocol server binds; the default accepts only local clients."""
    port: int = 0
    """Port of the protocol server; 0 lets the operating system choose a free one."""
    serve: bool = True
    """Serve the protocol at all; False runs the session without clients (used by tests)."""
    background_timeout_s: float | None = None
    """Unattended runs only: end the run after the session has been idle this long while Claude
    Code's background tasks still run (D170). None (the default) waits as long as they run."""


def format_event(event: Event) -> str | None:
    """Render one session event as one terminal line.

    Args:
        event: The event.

    Returns:
        The line, or None for events the terminal deliberately does not show: the token
        deltas (`assistant.delta`, whose completed block follows), the files and the diff of a
        step (`tool.files`, `edit.diff`), the message queue (`chat.queue`) and the subagent
        list (`agents.update`) -- a graphical client shows those better.
        `tests/unit/test_event_coverage.py` keeps the list honest: a new event is either
        shown here or named there.
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
            # A question of the agent is answered, not allowed or denied (PLAN.md D35), so the
            # line says how to answer it instead.
            how = (
                f"answer it with: calu resolve {event.escalation_id} answer 'your answer'"
                if event.questions
                else f"resolve it with: calu resolve {event.escalation_id} allow|deny"
            )
            return (
                f"\n*** escalation {event.escalation_id}: {event.tool_name}\n"
                f"    {event.reason}\n"
                f"    {how}\n"
            )
        case EscalationResolved():
            return f"*** escalation {event.escalation_id}: {event.decision}"
        case TurnResult():
            waiting = (
                f"; {event.background_tasks} background task(s) still running, Claude Code"
                " continues when they finish"
                if event.background_tasks
                else ""
            )
            return f"[turn ended: {event.subtype}{waiting}]"
        case HonestyUpdate():
            if not event.marks:
                return "*** the honesty register is clear again"
            spots = ", ".join(mark.label() for mark in event.marks[:5])
            rest = f" and {len(event.marks) - 5} more" if len(event.marks) > 5 else ""
            verb = "cannot be run" if event.mode == "enforce" else "are recorded (not blocked)"
            return f"*** dishonest code that {verb}: {spots}{rest}"
        case AutoEscalateChanged():
            on = [name for name, held in event.settings.model_dump().items() if held]
            waits = ", ".join(on) if on else "nothing beyond what the checkers escalate"
            return f"*** what waits for a human: {waits}"
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


@dataclass(frozen=True)
class Activity:
    """What the session is doing at one moment, as far as the end of a run depends on it."""

    state: State
    """The session's state."""
    queued: int
    """Messages that were sent to Claude and that Claude has not taken in yet."""
    turn_open: bool
    """Whether a turn has started that has not ended yet."""


def activity(session: Session) -> Activity:
    """Read from `session` what the end of an unattended run depends on.

    Args:
        session: The running session.

    Returns:
        Its state, the number of messages Claude has not taken in, and whether a turn is open.
    """
    return Activity(
        state=session.state,
        queued=len(session.snapshot().queue),
        turn_open=session.context.current is not None,
    )


class IdleSignal:
    """Decides when the session of an unattended run has nothing left to do.

    Used for runs without `--interactive` and for runs whose stdin ended. The answer is read
    from the session each time it is needed (`view`), not collected from the order of earlier
    events, so a message sent at any moment cannot make the run miss its end:

        finished  <=>  state in {stopped, failed}
                   or  (the session has worked  and  state = idle
                        and  no message is queued  and  no turn is open
                        and  the last turn left no background task)

    Events only wake a waiting `wait` and supply the two facts the session does not keep: that
    it has worked at all (a latch that is never reset) and how many background tasks the last
    turn left. A turn that ends while Claude Code still has background tasks running (a
    background command, a Monitor, a Workflow, a background subagent) does not end the run:
    Claude Code starts another turn by itself when they finish, and their steps still pass the
    gate -- ending the run there stopped the session under them (D158).

    Two waits are bounded, so that the run ends even when something else went wrong:

    - `background_timeout_s` (optional): idle that long with background tasks still running.
    - `pickup_timeout_s`: idle that long while a message still counts as queued or a turn as
      open. Claude Code takes a queued message within moments of the end of a turn; one that
      is still listed after this time will not start a turn.
    """

    WORKING = ("running", "checking", "tool_call", "escalated")
    """States that count as "the session has started working"."""

    def __init__(
        self,
        view: Callable[[], Activity],
        background_timeout_s: float | None = None,
        pickup_timeout_s: float = QUEUED_PICKUP_S,
    ) -> None:
        """Create a signal for the session that `view` reads.

        Args:
            view: Returns what the session is doing now; called at every evaluation.
            background_timeout_s: How long to wait while the session is idle but background
                tasks still run before the run is finished anyway (`calu run
                --background-timeout`, D170); None waits for them as long as they run.
            pickup_timeout_s: How long an idle session may keep a queued message or an open
                turn before the run is finished anyway.
        """
        self._view = view
        self._changed = anyio.Event()
        self._worked = False
        self._background = 0
        self.background_timeout_s = background_timeout_s
        self.pickup_timeout_s = pickup_timeout_s
        self._idle_since: float | None = None
        self._reported = False
        self.unfinished = False
        """True once a bounded wait ended the run while a message was still queued; the run
        then exits with a non-zero code."""

    async def __call__(self, event: Event) -> None:
        """Note what only events tell, and wake a waiting `wait`.

        Args:
            event: The session event.
        """
        if isinstance(event, TurnResult):
            self._background = event.background_tasks
        elif isinstance(event, SessionState):
            self._note(event.state)
        else:
            return
        changed, self._changed = self._changed, anyio.Event()
        changed.set()

    def _note(self, state: State) -> None:
        """Keep the "has worked" latch and the start of the current idleness up to date.

        Args:
            state: A state the session is or was in.
        """
        if state in self.WORKING:
            self._worked = True
        if state != "idle":
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()

    def finished(self) -> bool:
        """Return whether the run has nothing left to wait for, as of now.

        Side effect: prints one line the first time one of the two bounded waits ends the run.

        Returns:
            True if the session stopped or failed, or is idle after having worked with no
            queued message, no open turn and no background task -- or if one of the bounded
            waits described in the class docstring ran out.
        """
        now = self._view()
        if now.state in ("stopped", "failed"):
            return True
        self._note(now.state)
        if now.state != "idle" or not self._worked or self._idle_since is None:
            return False
        idle_for = time.monotonic() - self._idle_since
        if self._background:
            limit = self.background_timeout_s
            if limit is None or idle_for < limit:
                return False
            self._report(
                f"[background task(s) still running after {limit:g} s; ending the run, which"
                " stops them]"
            )
            return True
        if now.queued or now.turn_open:
            if idle_for < self.pickup_timeout_s:
                return False
            what = (
                f"{now.queued} message(s) still count as queued"
                if now.queued
                else "a turn still counts as open"
            )
            # A queued message that was never picked up is work that did not happen.
            self.unfinished = self.unfinished or bool(now.queued)
            self._report(
                f"calu: the session has been idle for {self.pickup_timeout_s:g} s although"
                f" {what}; ending the run",
                error=True,
            )
        return True

    def _report(self, line: str, error: bool = False) -> None:
        """Print `line` once per signal.

        Args:
            line: The line to print.
            error: Print to stderr instead of stdout, for an end that may have lost work.
        """
        if not self._reported:
            self._reported = True
            print(line, file=sys.stderr if error else sys.stdout, flush=True)

    async def wait(self) -> None:
        """Wait until `finished()` holds.

        Returns at once if it already holds. Otherwise the condition is evaluated again at
        every state change and turn result and at least every `IDLE_POLL_S` seconds, so the
        wait does not depend on any single event being seen.
        """
        while True:
            changed = self._changed
            if self.finished():
                return
            with anyio.move_on_after(IDLE_POLL_S):
                await changed.wait()


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


async def read_lines(send: Callable[[str], Awaitable[None]]) -> Literal["eof", "quit"]:
    """Read lines from stdin and pass them on until end of input or a quit command.

    Blank lines are skipped. Reading runs in a worker thread, so it does not block the session.

    Args:
        send: Called with each line (without the line end).

    Returns:
        "quit" if a quit command ended the reading, "eof" if stdin was closed. The caller
        treats them differently: a quit command means "stop now", a closed stdin only means
        that no further message will come.
    """
    while True:
        line = await anyio.to_thread.run_sync(sys.stdin.readline, abandon_on_cancel=True)
        if not line:
            return "eof"
        text = line.strip()
        if not text:
            continue
        if text in QUIT_COMMANDS:
            return "quit"
        await send(text)


def harden_process() -> bool:
    """Make this process unreadable for other processes of the same user (Linux).

    Clears the "dumpable" flag: `/proc/<pid>/mem`, `environ` and `fd` then belong to root, a
    process of the same user cannot attach with ptrace, and no core dump is written. The calu
    process holds the database connection string and the session key in memory; an unsandboxed
    process that Claude Code starts as the same user (a hook, an MCP server) cannot read them
    out of it this way. The flag is reset by `execve`, so the Claude Code process calu starts
    is not affected. root can still read the process.

    Returns:
        True if the flag was cleared, False if the call is not available or failed (a debug
        message is logged; the session runs either way).
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        done = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError) as exc:
        log.debug("prctl(PR_SET_DUMPABLE) is not available: %s", exc)
        return False
    if not done:
        log.debug("prctl(PR_SET_DUMPABLE, 0) failed: errno %d", ctypes.get_errno())
    return done


async def run_session(
    url: str | bytearray,
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
) -> int:
    """Run one session until it ends, and report what happened.

    Side effects: clears the process's "dumpable" flag (`harden_process`; `calu run` has done
    that already before it read the password, this call covers a caller that embeds the
    function, and the flag then stays cleared for the rest of that process), opens two database
    connections, creates a `sessions` row, starts a Claude Code process, prints events to stdout
    and, with `options.interactive`, reads messages from stdin. A `bytearray` passed as `url` is
    wiped before Claude Code starts; a copy of the connection string stays in a
    `ConnectionSource` inside this process until the function returns, so that a lost
    connection can be reopened (see the module docstring).

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
        0 if the session ended normally, 1 if it failed or could not start (the reason was
        printed): among others an unknown `--project`, or a fork point that does not exist.

    Raises:
        psycopg.Error: If the database cannot be reached at the start or rejects the login.
    """
    hidden = load_secrets()
    harden_process()
    source = ConnectionSource(url)
    if isinstance(url, bytearray):
        # The caller's buffer is not needed any more. The copy in `source` stays in this
        # process's memory; nothing of it reaches Claude Code, which starts below.
        wipe(url)
    db = Db(await source.open(), source=source)
    try:
        notifier = Notifier(await source.open(), source=source)
        try:
            return await _run(
                db, notifier, hidden, options, runner_factory, checker_models, on_serving
            )
        finally:
            with anyio.CancelScope(shield=True):
                await notifier.aclose()
    finally:
        with anyio.CancelScope(shield=True):
            await db.aclose()


async def _project(db: Db, options: RunOptions) -> RunOptions | None:
    """Decide which project the session belongs to, and say so on stdout.

        --project NAME given ──► NAME (whether the row exists is checked by the first load)
        no --project         ──► the project registered for the workspace or the nearest
                                 directory above it on this host, else none

    Side effects: prints one line that names the project or says that only the global settings
    apply; prints an error to stderr when the lookup is ambiguous.

    Args:
        db: The session's connection.
        options: What `calu run` was asked to do.

    Returns:
        The options with `project` filled in, or None if several projects are registered for
        the directory (the session must not start under rules picked by chance).

    Raises:
        psycopg.Error: On database errors.
    """
    if options.project is not None:
        print(f"[project] {options.project}", flush=True)
        return options
    try:
        found = await project_for_directory(db, options.workspace, socket.gethostname())
    except AmbiguousProject as exc:
        print(f"calu: {exc}", file=sys.stderr)
        return None
    if found is None:
        print(
            "[project] none: no project is registered for this directory, so only the global "
            "settings and rules apply (register one with `calu project add .`, or pass "
            "--project)",
            flush=True,
        )
        return options
    print(f"[project] {found} (registered for this directory)", flush=True)
    return dataclasses.replace(options, project=found)


async def _run(
    db: Db,
    notifier: Notifier,
    hidden: set[str],
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
) -> int:
    """Load the settings, start the session and drive it until it ends.

    Args:
        db: Connection for queries; it reconnects by itself.
        notifier: Notifier on the connection for notifications; it reconnects by itself.
        hidden: Names of environment variables Claude Code must not see.
        options: What to run.
        runner_factory: Creates the runner; None uses `SdkRunner`.
        checker_models: Replacements for the configured checker models, or None.
        on_serving: Awaited with the address and the token once the server listens, or None.

    Returns:
        0 if the session ended normally, 1 if it failed or could not start.

    Raises:
        psycopg.Error: On database errors while loading the settings.
    """
    resolved = await _project(db, options)
    if resolved is None:
        return 1
    options = resolved
    config_store = ConfigStore(db, options.workspace, options.project)
    try:
        settings = await config_store.load()
    except UnknownProject as exc:
        # Not a fallback to the global settings: the session would run without the project's
        # rules and nobody would be told.
        print(f"calu: {exc}; nothing was started", file=sys.stderr)
        return 1
    console = Console(options.show_thinking)
    idle = IdleSignal(lambda: activity(session), options.background_timeout_s)
    bus = Bus()
    store = DatabaseSessionStore(db)
    emit = both(bus.publish, both(console, idle))
    session = Session(
        options.workspace,
        settings,
        store,
        lambda: (
            runner_factory or functools.partial(SdkRunner, tool_routes=session.config.tool_routes)
        )(options.workspace, session.config.claude, hidden | set(session.config.hidden_env)),
        emit,
        project=options.project,
        checker_models=checker_models,
        startup_warnings=preflight.warnings(
            settings.config, checkers=settings.checkers, workspace=options.workspace
        ),
    )

    async def on_settings(new: ProjectSettings) -> None:
        """Apply reloaded settings and report it.

        Args:
            new: The newly loaded settings.
        """
        before = session.config.auto_escalate
        session.apply_settings(new)
        # A client may offer the categories as a setting of its own (the ACP adapter maps them
        # to ACP's session modes), so it has to hear about a change made elsewhere -- and the
        # change belongs in the audit log either way (D43).
        try:
            await session.announce_auto_escalate(before)
        except psycopg.Error, TimeoutError:
            log.exception("recording the changed auto-escalation failed")
        # The chat summary is a row like the rules and the instructions, and an operator may
        # have corrected it (D40), so the same notification re-reads it.
        await session.reload_summary()
        # A changed honesty.mode has to reach the clients (D113).
        await session.announce_honesty()
        await console(Log(level="info", message="configuration reloaded"))

    async def on_reload_error(exc: Exception, failures: int) -> None:
        """Report a failed reload, and hold the session when its settings cannot be trusted.

        The session keeps the settings it has, which may be laxer than what the database holds
        now. Invalid stored settings and a vanished project row hold the session at once, any
        other error after `HOLD_AFTER_FAILED_RELOADS` failures in a row: every checked step
        then waits for a human until a reload succeeds (fail closed).

        Args:
            exc: Why the reload failed.
            failures: How many reloads have failed in a row.
        """
        hold = failures >= HOLD_AFTER_FAILED_RELOADS or isinstance(
            exc, (ValidationError, UnknownProject)
        )
        text = one_line(str(exc))
        if not hold:
            message = (
                f"the configuration could not be reloaded ({text}); the session keeps the "
                "settings it has and tries again"
            )
        else:
            message = (
                f"the configuration could not be reloaded ({text}); the session may run on "
                "stale settings, so every checked step waits for a human until a reload succeeds"
            )
        if failures == 1 or failures == HOLD_AFTER_FAILED_RELOADS:
            await emit(Log(level="error", message=message))
        if not hold or session.config.auto_escalate == HOLD_ALL:
            return
        try:
            await session.set_auto_escalate(HOLD_ALL, "session: configuration reload failed")
        except Exception:
            # The audit entry needs the database, which may be the thing that is failing; the
            # hold must not depend on it.
            log.exception("recording the hold failed; holding anyway")
            session.config.auto_escalate = HOLD_ALL
            await emit(AutoEscalateChanged(settings=HOLD_ALL))

    notifier.on(ESCALATION_RESOLVED, lambda payload: _note(session, payload))
    dirty = Dirty()
    notifier.on(CONFIG_CHANGED, lambda _payload: dirty.set())
    # A change committed before LISTEN took effect, or while the listening connection was away,
    # was never announced to this session: look once each time the subscription is in force.
    notifier.on_listening(dirty.hint)
    failed = False
    # Before anything starts: a fork that cannot be made must not turn into a plain resume,
    # which would continue the conversation instead of branching it (PLAN.md D36).
    fork = await _fork_point(db, options, console)
    if options.fork_before_turn and fork is None:
        return 1

    clock = settings.config.clock
    watch = ClockWatch(
        session.clock_jumped,
        interval_s=clock.check_interval_s,
        max_backward_s=clock.max_backward_s,
        max_forward_s=clock.max_forward_s,
    )
    async with anyio.create_task_group() as tg:

        def report(level: Literal["info", "warning", "error"], message: str) -> None:
            """Send a `log` event from a callback that cannot await.

            Args:
                level: The level of the event.
                message: Its text.
            """
            tg.start_soon(emit, Log(level=level, message=message))

        db.on_lost(
            lambda reason: report(
                "error", f"{reason}; steps wait until the database answers (calu reconnects)"
            )
        )

        def restored() -> None:
            """Report the new connection and have the configuration re-read."""
            report("info", "the database connection is back")
            dirty.hint()

        work = anyio.CancelScope()

        def refused(exc: LoginRefused) -> None:
            """End the run: the database refuses this session's login, and waiting is no use.

            Args:
                exc: The server's last refusal.
            """
            nonlocal failed
            if failed:
                return
            failed = True
            message = (
                f"the database refuses the session's login ({one_line(str(exc))}), and calu"
                " does not expect that to change by waiting: the session credential was"
                " probably replaced by a later `calu setup`, or the role or the database was"
                " removed. The session stops; the end of this session and anything it had"
                " not written yet are not recorded. Start a new one with `calu run`"
                " (`--resume` continues the conversation)."
            )
            print(f"calu: {message}", file=sys.stderr, flush=True)
            report("error", message)
            work.cancel()

        db.on_refused(refused)
        notifier.on_refused(refused)
        db.on_restored(restored)
        notifier.on_lost(
            lambda exc: report(
                "warning",
                f"the notification connection is lost ({one_line(str(exc))}); calu reconnects, "
                "and meanwhile polls for answers to escalations and for configuration changes",
            )
        )
        tg.start_soon(notifier.run)
        # The chat summary is re-read with every periodic look at the settings, not only when
        # they changed: an operator's correction of it must not depend on a notification.
        tg.start_soon(
            functools.partial(
                config_store.reload_loop,
                dirty,
                on_settings,
                RELOAD_DEBOUNCE_S,
                on_reload_error,
                on_unchanged=session.reload_summary,
            )
        )
        if clock.check_interval_s > 0:
            # Watches calu's own clock: a backward jump breaks the attribution of file changes
            # to steps, so the session escalates its next tool call (D32).
            tg.start_soon(watch.run)
        # "This process is still running", so a herd can tell a working session from one whose
        # process is gone (D56). The interval is a constant of its own: tied to the clock
        # watch's setting, a long or switched-off watch made live sessions look dead.
        tg.start_soon(session.beat, herd.HEARTBEAT_S)
        server: SessionServer | None = None
        try:
            with work:
                await session.start(tg, options.resume_id, options.model, options.effort, fork)
                if options.serve:
                    # After the start, so that the session row and its id exist when the first
                    # client asks; the events of the start are in the bus and are replayed.
                    token = new_token()
                    server = SessionServer(session, bus, token_hash(token), tg)
                    tg.start_soon(_listen, server, options, token, store, on_serving)
                sent = bool(options.prompt)
                if options.prompt:
                    await session.send_chat(ChatSendParams(text=options.prompt))
                ended = "eof"
                if options.interactive:

                    async def send(text: str) -> None:
                        """Send one typed line to Claude.

                        Args:
                            text: The line.
                        """
                        nonlocal sent
                        sent = True
                        await session.send_chat(ChatSendParams(text=text))

                    ended = await read_lines(send)
                    if ended == "eof" and sent:
                        # A closed stdin only means that no further message comes (a password
                        # piped in, a service unit, nohup). Stopping here ended the session
                        # seconds into its first turn, with exit code 0.
                        print(
                            "calu: stdin closed; waiting for Claude to finish (as with --batch)",
                            file=sys.stderr,
                            flush=True,
                        )
                if not options.interactive or (ended == "eof" and sent):
                    # `idle` reads the session each time: a message that is still queued behind a
                    # turn, or was sent a moment ago, keeps the run going until its turn is over.
                    if server is not None:
                        await _until_done(idle, server, bus)
                    else:
                        await idle.wait()
        except SessionError as exc:
            print(f"calu: {exc}", file=sys.stderr)
            failed = True
        finally:
            # The last writes are worth a short wait, but a shutdown must not wait for a
            # database that stays away.
            db.limit_reconnect(STOP_RECONNECT_S)
            with anyio.CancelScope(shield=True):
                await session.stop()
            tg.cancel_scope.cancel()
    return 1 if failed or idle.unfinished or session.state == "failed" else 0


async def _fork_point(db: Db, options: RunOptions, console: Console) -> Fork | None:
    """Resolve `--fork-before-turn` into the uuids Claude Code needs (PLAN.md D36).

    Side effect: reports why a fork cannot be made, as an error on the console.

    Args:
        db: The database connection.
        options: What `calu run` was asked to do.
        console: Where to report a problem.

    Returns:
        The fork point, or None — both when no fork was asked for and when it cannot be made.
        The caller distinguishes the two by `options.fork_before_turn`.

    Raises:
        psycopg.Error: On database errors.
    """
    turn = options.fork_before_turn
    if not turn:
        return None
    if not options.resume_id:
        await console(Log(level="error", message="--fork-before-turn needs --resume <session id>"))
        return None
    if turn < 2:
        await console(
            Log(
                level="error",
                message="a fork before turn 1 would keep nothing; pick turn 2 or later",
            )
        )
        return None
    point = await herd.fork_point(db, options.resume_id, turn)
    if point is None:
        await console(
            Log(
                level="error",
                message=(
                    f"no fork point for turn {turn} of {options.resume_id}: either the database "
                    "knows no conversation with that Claude session id or no such turn in it, "
                    "or the turn's transcript uuids were not recorded (calu records them from "
                    "migration 0012 on, and only for turns it saw itself)"
                ),
            )
        )
        return None
    await console(
        Log(
            level="info",
            message=(
                f"forking before turn {turn}: keeping the conversation up to {point.resume_at} "
                "and discarding that turn; the fork gets its own session id"
            ),
        )
    )
    return Fork(resume_at=point.resume_at, drops_turn=point.drops_turn)


def default_socket_path() -> Path:
    """Return where a session puts its Unix socket when `--socket` is given without a path.

    `$XDG_RUNTIME_DIR` is the right place for a socket: it belongs to this user, is cleaned up
    at logout, and is usually a tmpfs. Without it, the temporary directory has to do.

    Returns:
        `<runtime dir>/calu/<pid>.sock`. The pid makes it unique per session process, and
        `sessions.address` records the path, so clients and `calu sessions` find it (D29).
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "calu" / f"{os.getpid()}.sock"


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
        store: The session store, for the address and the token's hash.
        on_serving: Awaited with the address and the token as well, or None.
    """
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.listen, options.host, options.port, options.socket_path)
            with anyio.move_on_after(START_TIMEOUT_S):
                await server.ready.wait()
            if not server.ready.is_set():
                print("calu: the protocol server did not start", flush=True)
                return
            print(f"[clients] {server.address}", flush=True)
            # The short form first, because it is the one a user here will use (D105); the
            # explicit one is for a client on another machine and needs no database. Written
            # `--token=` and not `--token `, so that a token from before D102, which may start
            # with a dash, is not read as an option.
            print("[clients] attach from this directory with: calu attach", flush=True)
            print(
                f"[clients] from another machine: calu attach {server.address} --token={token}",
                flush=True,
            )
            if store.session_id is not None:
                try:
                    await store.set_address(server.address)
                    await store.add_client_token(token_hash(token), server.address)
                except Exception:
                    log.exception("recording the address and the client token failed")
            if on_serving is not None:
                await on_serving(server.address, token)
    except OSError as exc:
        print(f"calu: the protocol server could not start: {exc}", flush=True)


async def _until_done(idle: IdleSignal, server: SessionServer, bus: Bus) -> None:
    """Wait until an unattended run is finished, or a client ends it.

    A run without `--interactive` ends when Claude has nothing left to do. A client that is
    attached at that moment keeps the session alive, because it may want to send the next
    message: the run then ends when that client sends `shutdown`, or when the last client has
    detached and the session is finished with what the clients sent.

    Args:
        idle: The idle signal of the session.
        server: The protocol server, for its `shutdown` event.
        bus: The bus, for the number of attached clients.
    """
    async with anyio.create_task_group() as tg:

        async def wait_idle() -> None:
            """End the wait when the session is idle and nobody is watching."""
            while True:
                await idle.wait()
                if not bus.clients:
                    break
                log.info("the session is idle; waiting for its %d client(s)", bus.clients)
                await bus.no_clients.wait()
                # The client that left may have sent a message first; its turn is waited for.
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
