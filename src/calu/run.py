"""`calu run`: starts one checked Claude Code session in the foreground.

See agent/records/PLAN.md phase 3.

    password ──► decrypt the `user` credential ──► two database connections
                 │                                     queries + notifications
                 └► the password, the derived key and the decrypted buffer are wiped;
                    the connection string stays in one object in this process's memory
    find the project (`--project`, or the one registered for the directory)
    load configuration, rules and checker instructions (calu.store.ConfigStore)
    create the sessions row, start Claude Code (calu.session.Session, calu.claude.SdkRunner)
    serve the protocol on a WebSocket (calu.protocol.server) and print its address and token
    print the session's status to the terminal (never the conversation), read further
    messages from stdin
    reload the configuration on `config_changed`, continue on `escalation_resolved`

The Claude Code process never gets calu's secrets: the connection string is in no environment
variable, command line or file that calu hands to it, the checker API keys are blanked in its
environment (`calu.config.load_secrets`, `SdkRunner.hidden_env`), and its sandbox hides
`~/.config/calu` (agent/records/PLAN.md D15).

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
token; several at once. They show the conversation. The terminal of this command shows only the
session's status (`STATUS_EVENTS`) and one line per error; the tracebacks go to the log file
(`calu.logfile`, D225).
"""

import concurrent.futures
import contextvars
import ctypes
import dataclasses
import functools
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import anyio
import anyio.to_thread
import psycopg
from pydantic import ValidationError

from calu import asuser, herd, localsocket, preflight
from calu.bus import Bus
from calu.claude.runner import ClaudeRunner, Fork
from calu.claude.transcripts import user_inputs
from calu.clock import ClockWatch
from calu.config import AutoEscalate, ClaudeConfig, load_secrets
from calu.credentials import wipe
from calu.db import (
    CONFIG_CHANGED,
    ESCALATION_RESOLVED,
    TRUST_CHANGED,
    ConnectionSource,
    Db,
    Dirty,
    LoginRefused,
    Notifier,
)
from calu.launch import RunAs
from calu.protocol.messages import (
    AssistantText,
    AssistantThinking,
    AutoEscalateChanged,
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    HonestyInfo,
    HonestyUpdate,
    Log,
    PauseChanged,
    PermissionModeChanged,
    RateLimit,
    SessionState,
    State,
    ToolResult,
    ToolUse,
    TurnResult,
    UsageUpdate,
    UserInput,
)
from calu.store import ConfigStore, DatabaseSessionStore, ProjectSettings, UnknownProject
from calu.store.config import RELOAD_DEBOUNCE_S
from calu.store.projects import AmbiguousProject, project_for_directory
from calu.tokens import new_token, token_hash

if TYPE_CHECKING:
    # The session, its server and the Agent SDK runner are imported in `_run`, which is where
    # a session starts: this module is also imported by the clients (`calu attach` renders
    # events with `format_event`), and those imports take about two seconds.
    from pydantic_ai.models import Model

    from calu.protocol.server import SessionServer
    from calu.session import Session

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

Ready = Callable[[str | None, str], Awaitable[None]]
"""Called with the id of the `sessions` row (None if the row could not be written) and the
address, once the protocol server listens and the address is recorded: a client that looks
the session up in the database finds it from then on."""

EventSink = Callable[[Event], Awaitable[None]]
"""Receives the events of a session, one at a time."""

LABEL: contextvars.ContextVar[str] = contextvars.ContextVar("calu_run_label", default="")
"""What `say` puts in front of each line. `calu run` leaves it empty; `calu serve` sets it per
session, because the lines of several sessions share one terminal."""

ERRORS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "calu_run_errors", default=None
)
"""Where `say` also collects the error lines of one session, or None. `calu serve` reads the
reason of a failed start from it."""


def say(text: str, error: bool = False) -> None:
    """Print one line about the session that runs in this task.

    Side effects: writes to stdout, or to stderr for an error; appends an error to `ERRORS`
    when a list is set there.

    Args:
        text: The line, without the label.
        error: Whether the line reports a failure; such a line is put on one line.
    """
    if error:
        # stderr gets one line per error (D225); the full story is in the log file.
        text = one_line(text, 500)
        if (collected := ERRORS.get()) is not None:
            collected.append(text)
    print(f"{LABEL.get()}{text}", file=sys.stderr if error else sys.stdout, flush=True)


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
    fork_id: str | None = None
    """Claude session id of a conversation to copy: the session continues the copy under a new
    session id and leaves the original as it was (D203). Not together with `resume_id`."""
    fork_before_turn: int | None = None
    """With `resume_id`: branch the conversation off before this turn instead of continuing it
    (agent/records/PLAN.md D36). The new session gets its own id, so the original stays as it
    was."""
    continue_last: bool = False
    """Resume the conversation of the newest session of `workspace` on this host (D280), as
    `resume_id` would; a new conversation if the directory has none. Not together with
    `resume_id` or `fork_id`."""
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] | None = None
    interactive: bool = True
    socket_path: Path | None = None
    """Serve a Unix socket at this path instead of a TCP port (agent/records/PLAN.md §6): the file
    system then decides who may connect, and `sessions.address` records it as `unix:<path>` (D29).
    """
    host: str = "127.0.0.1"
    """Interface the protocol server binds; the default accepts only local clients."""
    port: int = 0
    """Port of the protocol server; 0 lets the operating system choose a free one."""
    serve: bool = True
    """Serve the protocol at all; False runs the session without clients (used by tests)."""
    background_timeout_s: float | None = None
    """Unattended runs only: end the run after the session has been idle this long while Claude
    Code's background tasks still run (D170). None (the default) waits as long as they run."""
    stay: bool = False
    """A session of `calu serve` (D208): it does not end when Claude has nothing left to do,
    because it waits for its user. It ends when a client sends `shutdown`, when the caller
    cancels it, or when its protocol server cannot start. Needs `serve`, and no `interactive`."""
    stay_idle_s: float | None = None
    """With `stay`: end the session after it has been idle this long with no client attached.
    None (the default) lets it wait for ever."""
    quiet: bool = False
    """Print the lines about the session's life (project, ids, address, errors) but not its
    status events: `calu serve` shares one terminal among its sessions. The conversation is
    never printed (`STATUS_EVENTS`)."""
    run_as: RunAs | None = None
    """The user Claude Code runs as, when that is not the user calu runs as (D206); None
    starts it as calu's own user."""
    insecure: bool = False
    """Every client on a Unix socket or on a loopback TCP address needs no token (`--insecure`,
    D227). A session that does not run as root then also listens on a socket of its own user
    (`calu.localsocket.user_root`), where `calu attach` finds it without the database. Not
    together with `secure`."""
    secure: bool = False
    """Every client shows a token (`--secure`, D226). Without it, a session that runs as root
    for another user (`run_as`) also listens on a root-owned Unix socket, where a root client
    needs no token (D215). A root session whose Claude Code is root too (`--user root`) binds
    no such socket: its own commands would be root clients."""
    login_protection: preflight.LoginProtection | None = None
    """What keeps Claude's commands from calu's logins (`calu.preflight.login_protection`),
    collected by the caller before the user switch; None if unknown, which makes a session
    with the sandbox off show the recommendation of requirement 42."""


def honesty_lines(info: HonestyInfo) -> list[str]:
    """Render the code honesty state of a session as terminal lines.

    Args:
        info: The state, from `honesty.update` or from the snapshot of `state.get`.

    Returns:
        One line with the mode, whether the state is clean, the baseline (12 characters of its
        id and the source of its trust) and the counts; then one line per open mark with its
        id, one line naming the first unreviewed files, and one naming the project's worktrees
        (for `/trust <worktree> <ref>`) if it has any. For the mode "off": one line, and the
        worktrees.
    """
    worktrees = (
        [
            "    worktrees (/trust <worktree> <ref>): "
            + ", ".join(one_line(n) for n in info.worktrees)
        ]
        if info.worktrees
        else []
    )
    if info.mode == "off":
        return ["*** code honesty: off", *worktrees]
    base = (
        f"baseline {info.baseline[:12]} ({info.baseline_source})"
        if info.baseline
        else "no baseline (certify a commit with /trust)"
    )
    unreviewed = (
        "every file unreviewed"
        if info.unreviewed_all
        else f"{info.unreviewed_count} unreviewed file(s)"
    )
    parts = [base, f"{len(info.marks)} open mark(s)", unreviewed]
    if info.review_required:
        parts.append("review required")
    if info.review_running:
        parts.append("a review is running")
    lines = [
        f"*** code honesty ({info.mode}): {'clean' if info.clean else 'not clean'}: "
        + ", ".join(parts)
    ]
    lines += [
        f"    mark {mark.id}: {one_line(mark.label())}: {one_line(mark.reason)}"
        for mark in info.marks
    ]
    if info.unreviewed:
        rest = info.unreviewed_count - len(info.unreviewed)
        names = ", ".join(one_line(path) for path in info.unreviewed)
        lines.append(f"    unreviewed: {names}" + (f", and {rest} more" if rest > 0 else ""))
    return lines + worktrees


def limit_text(limit: RateLimit) -> str:
    """Put the state of one usage limit into a few words.

    Args:
        limit: The limit as Claude Code reported it.

    Returns:
        For example `5-hour limit 82 % used (close to the limit), resets 14:00 UTC`.
    """
    names = {
        "five_hour": "5-hour limit",
        "seven_day": "weekly limit",
        "seven_day_opus": "weekly Opus limit",
        "seven_day_sonnet": "weekly Sonnet limit",
        "overage": "extra usage",
    }
    text = names.get(limit.type, limit.type or "usage limit")
    if limit.utilization is not None:
        text += f" {round(limit.utilization * 100)} % used"
    if limit.status == "allowed_warning":
        text += " (close to the limit)"
    elif limit.status == "rejected":
        text += " (reached)"
    if limit.resets_at is not None:
        text += f", resets {limit.resets_at:%a %H:%M} UTC"
    return text


def check_honesty_lines(event: CheckResult) -> list[str]:
    """Render the honesty checker's result of one step as terminal lines.

    The honesty checker does not approve or refuse a step (agent/records/PLAN.md D224), so its
    result is shown as a status per file, never as a decision like ALLOW or PASS.

    Args:
        event: The `check.result` of the step.

    Returns:
        [] if the honesty checker did not judge the step. Otherwise one line per file
        ("    honesty: a.py honest", or "    honesty: b.py DISHONEST: <reason>"), one line per
        dishonest unit of a file below it, and a line with the reason when the honesty
        checker asked for a person. Texts are shortened by `one_line`.
    """
    lines: list[str] = []
    for status in event.honesty:
        if status.verdict == "HONEST":
            lines.append(f"    honesty: {one_line(status.path)} honest")
            continue
        reason = f": {one_line(status.reason)}" if status.reason else ""
        lines.append(f"    honesty: {one_line(status.path)} DISHONEST{reason}")
        lines += [
            f"      {one_line(unit.symbol, 80)}: {one_line(unit.reason)}"
            for unit in status.units
            if not unit.honest
        ]
    if event.honesty_escalation:
        lines.append(
            f"    honesty checker asks a person to look: {one_line(event.honesty_escalation)}"
        )
    return lines


def format_event(event: Event) -> str | None:
    """Render one session event as terminal text.

    Most events are one line. An escalation, the honesty state and a `check.result` that
    carries the honesty checker's result (`check_honesty_lines`) are several lines.

    Args:
        event: The event.

    Returns:
        The text, or None for events the terminal deliberately does not show: the token
        deltas (`assistant.delta`, whose completed block follows), the files and the diff of a
        step (`tool.files`, `edit.diff`), the message queue (`chat.queue`) and the subagent
        list (`agents.update`) -- a graphical client shows those better -- and the start and
        end of a hold (`step.held`), which the `log` line before it says in words.
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
            return "\n".join(
                [
                    f"  [{event.route} checker: {event.decision}] {event.step_summary}"
                    f"{f' ({event.reason})' if event.reason else ''}{comment}",
                    *check_honesty_lines(event),
                ]
            )
        case EscalationRequest():
            # A question of the agent is answered, not allowed or denied (agent/records/PLAN.md
            # D35), so the line says how to answer it instead.
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
            return "\n".join(honesty_lines(event))
        case AutoEscalateChanged():
            on = [name for name, held in event.settings.model_dump().items() if held]
            waits = ", ".join(on) if on else "nothing beyond what the checkers escalate"
            return f"*** what waits for a human: {waits}"
        case PauseChanged():
            by = f" by {event.client}" if event.client else ""
            if event.paused and event.until is not None:
                until = event.until.astimezone()
                return f"*** paused{by} until {until:%H:%M}: approved steps wait"
            return f"*** pause ended{by}" if by else "*** pause ended: the time is up"
        case PermissionModeChanged():
            by = f" (switched by {event.client})" if event.client else ""
            return f"*** Claude Code's permission mode: {event.mode}{by}"
        case UsageUpdate():
            if not event.rate_limits:
                return None
            return "*** usage limits: " + ", ".join(
                limit_text(limit) for limit in event.rate_limits
            )
        case Log():
            return f"[{event.level}] {one_line(event.message, 500)}"
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


STATUS_EVENTS = (
    SessionState,
    EscalationRequest,
    EscalationResolved,
    AutoEscalateChanged,
    PauseChanged,
    PermissionModeChanged,
    UsageUpdate,
    Log,
)
"""The events `calu run` and `calu serve` print (D225): what a person at the terminal has to
know or act on. A `SessionState` is printed only for the end of the session (`END_STATES`).
Not printed: the conversation (the user's input, Claude's text and thinking, the tool calls,
their results and the checkers' verdicts) and the course of the work (the other states, the
end of each turn, the honesty state); the clients show those."""

END_STATES = ("stopped", "failed")
"""The session states the terminal shows."""


class Console:
    """Prints the status of a session to stdout: the events in `STATUS_EVENTS`, one line each."""

    def __init__(self, quiet: bool = False) -> None:
        """Create a console printer.

        Args:
            quiet: Print only `log` events of the level "error" (`RunOptions.quiet`).
        """
        self.quiet = quiet

    async def __call__(self, event: Event) -> None:
        """Print one event if it is a status event.

        Args:
            event: The event; the conversation's events are dropped.
        """
        if not isinstance(event, STATUS_EVENTS):
            return
        if isinstance(event, SessionState) and event.state not in END_STATES:
            return
        if self.quiet and not (isinstance(event, Log) and event.level == "error"):
            return
        line = format_event(event)
        if line is not None:
            say(line)


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
            say(line, error)

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


STDIN_POLL_S = 0.05
"""How often a reader looks whether the stdin thread has delivered its line."""

_pending_line: tuple[object, concurrent.futures.Future[str]] | None = None
"""The stdin object and the line its reading thread will deliver, while nobody took it yet."""


async def _next_stdin_line() -> str:
    """Wait for the next line of stdin, taking over a read a cancelled caller left pending.

    Side effect: starts a daemon thread that reads one line when no read of the current
    `sys.stdin` is pending; a pending read of another (replaced) stdin is dropped.

    The wait polls the read's future every `STDIN_POLL_S` instead of blocking a worker thread
    on it: AnyIO's worker threads are not daemon threads, so a wait abandoned by a cancelled
    caller kept the process alive at its exit until a line was typed (after a client's
    `/shutdown` of `calu run`, test plan M20).

    Returns:
        The line with its line end, or "" at end of input.

    Raises:
        OSError: If stdin cannot be read.
    """
    global _pending_line
    stdin = sys.stdin
    if _pending_line is None or _pending_line[0] is not stdin:
        future: concurrent.futures.Future[str] = concurrent.futures.Future()

        def read() -> None:
            """Read one line into `future` (its result, or the error)."""
            try:
                future.set_result(stdin.readline())
            except BaseException as exc:  # handed to the waiting caller
                future.set_exception(exc)

        threading.Thread(target=read, name="calu-stdin", daemon=True).start()
        _pending_line = (stdin, future)
    pending = _pending_line[1]
    # Polling on purpose: the future outlives its caller (the next reader takes it over, also
    # under another event loop), and no worker thread may wait on it (see above).
    while not pending.done():  # ruff: ignore[async-busy-wait]
        await anyio.sleep(STDIN_POLL_S)
    line = pending.result()
    if _pending_line is not None and _pending_line[1] is pending:
        _pending_line = None
    return line


async def read_lines(send: Callable[[str], Awaitable[None]]) -> Literal["eof", "quit"]:
    """Read lines from stdin and pass them on until end of input or a quit command.

    Blank lines are skipped. Reading runs in a thread, so it does not block the session. A
    read that is still waiting when the caller is cancelled stays pending for the same stdin,
    and the next call takes its line: the line client stops reading when it switches to
    another session, and the line typed next belongs to the client that reads on.

    Args:
        send: Called with each line (without the line end).

    Returns:
        "quit" if a quit command ended the reading, "eof" if stdin was closed. The caller
        treats them differently: a quit command means "stop now", a closed stdin only means
        that no further message will come.
    """
    while True:
        line = await _next_stdin_line()
        if not line:
            return "eof"
        text = line.strip()
        if not text:
            continue
        if text in QUIT_COMMANDS:
            return "quit"
        await send(text)


async def read_lines_until(
    send: Callable[[str], Awaitable[None]], shutdown: anyio.Event | None
) -> Literal["eof", "quit"]:
    """Read lines from stdin as `read_lines` does, and stop early when `shutdown` is set.

    `calu run` reads its own terminal and serves clients at the same time. A client's
    `shutdown` request (`/shutdown`) sets the protocol server's event; the run must then end
    without waiting for a line typed in its own terminal. The stdin read that is still waiting
    stays pending (see `read_lines`) and does not keep the process alive (daemon thread).

    Args:
        send: Called with each line (without the line end).
        shutdown: The protocol server's `shutdown` event, or None when the run serves no
            clients (then this is `read_lines`).

    Returns:
        "quit" if a quit command was typed or `shutdown` was set, "eof" if stdin was closed.
    """
    if shutdown is None:
        return await read_lines(send)
    ended: Literal["eof", "quit"] = "quit"
    async with anyio.create_task_group() as tg:

        async def read() -> None:
            """Read until end of input or a quit command, then stop the wait for `shutdown`."""
            nonlocal ended
            ended = await read_lines(send)
            tg.cancel_scope.cancel()

        async def wait() -> None:
            """Stop the reading when a client asked the process to stop."""
            await shutdown.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(read)
        tg.start_soon(wait)
    return ended


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
    on_event: EventSink | None = None,
) -> int:
    """Run one session until it ends, and report what happened.

    Side effects: with `options.run_as`, switches the process's file access and the programs
    it starts to that user for good (`calu.asuser.enter`; the caller has run
    `calu.asuser.prepare`). Clears the process's "dumpable" flag (`harden_process`; `calu run`
    has done that already before it read the password, this call covers a caller that embeds the
    function, and the flag then stays cleared for the rest of that process), opens two database
    connections, creates a `sessions` row, starts a Claude Code process, prints the session's
    status to stdout (`Console`) and, with `options.interactive`, reads messages from stdin. A
    `bytearray` passed as `url` is wiped before Claude Code starts; a copy of the connection
    string stays in a `ConnectionSource` inside this process until the function returns, so
    that a lost connection can be reopened (see the module docstring).

    Args:
        url: The user login's connection string (a `bytearray` is wiped by this function).
        options: What to run.
        runner_factory: Creates the runner from workspace, Claude settings and the names of the
            hidden environment variables; None uses `SdkRunner` (tests pass a fake runner).
        checker_models: Replacements for the configured checker models; None uses the models
            from the database (tests pass fake models).
        on_serving: Awaited with the WebSocket address and the client token once the protocol
            server listens, in addition to printing them (a supervisor or a test uses it).
        on_event: Awaited with every event of the session, the conversation included, after
            the clients and the console got it (a supervisor or a test uses it); None for none.

    Returns:
        0 if the session ended normally, 1 if it failed or could not start (the reason was
        printed): among others an unknown `--project`, or a fork point that does not exist.

    Raises:
        psycopg.Error: If the database cannot be reached at the start or rejects the login.
    """
    hidden = load_secrets()
    harden_process()
    if options.run_as is not None:
        # After the checker keys were read as root; from here on, calu's own file access
        # and the programs it starts in the project are the unprivileged user's (D206).
        asuser.enter(options.run_as)
    source = ConnectionSource(url)
    if isinstance(url, bytearray):
        # The caller's buffer is not needed any more. The copy in `source` stays in this
        # process's memory; nothing of it reaches Claude Code, which starts below.
        wipe(url)
    return await run_from(
        source, hidden, options, runner_factory, checker_models, on_serving, on_event=on_event
    )


async def run_from(
    source: ConnectionSource,
    hidden: set[str],
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
    on_ready: Ready | None = None,
    on_event: EventSink | None = None,
) -> int:
    """Run one session on connections of its own, opened from `source`, until it ends.

    This is the part of `run_session` that `calu serve` repeats for every session it starts:
    the login is read once, each session gets its own two connections.

    Side effects: opens and closes two database connections; everything `_run` does.

    Args:
        source: Where connections come from; it is not closed or changed.
        hidden: Names of environment variables Claude Code must not see.
        options: What to run.
        runner_factory: Creates the runner; None uses `SdkRunner`.
        checker_models: Replacements for the configured checker models, or None.
        on_serving: Awaited with the address and the token once the server listens, or None.
        on_ready: Awaited with the session row's id and the address once a client can find the
            session in the database, or None.
        on_event: Awaited with every event of the session, or None.

    Returns:
        0 if the session ended normally, 1 if it failed or could not start.

    Raises:
        psycopg.Error: If the database cannot be reached at the start or rejects the login.
    """
    db = Db(await source.open(), source=source)
    try:
        notifier = Notifier(await source.open(), source=source)
        try:
            return await _run(
                db,
                notifier,
                hidden,
                options,
                runner_factory,
                checker_models,
                on_serving,
                on_ready,
                on_event,
                login_password=getattr(source.target.auth, "password", ""),
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
        say(f"[project] {options.project}")
        return options
    try:
        found = await project_for_directory(db, options.workspace, socket.gethostname())
    except AmbiguousProject as exc:
        say(f"calu: {exc}", error=True)
        return None
    if found is None:
        say(
            "[project] none: no project is registered for this directory, so only the global "
            "settings and rules apply (register one with `calu project add .`, or pass "
            "--project)"
        )
        return options
    say(f"[project] {found} (registered for this directory)")
    return dataclasses.replace(options, project=found)


async def _run(
    db: Db,
    notifier: Notifier,
    hidden: set[str],
    options: RunOptions,
    runner_factory: RunnerFactory | None = None,
    checker_models: dict[str, str | Model] | None = None,
    on_serving: Serving | None = None,
    on_ready: Ready | None = None,
    on_event: EventSink | None = None,
    login_password: str = "",
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
        on_ready: Awaited with the session row's id and the address once a client can find the
            session in the database, or None.
        on_event: Awaited with every event of the session, or None.
        login_password: The password of the session's database login, or "" if it has none.
            It is only named to the session as a value its trace must never hold (D232).

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
        say(f"calu: {exc}; nothing was started", error=True)
        return 1
    from calu.claude.sdk_runner import SdkRunner
    from calu.protocol.server import SessionServer
    from calu.session import Session, SessionError

    console = Console(options.quiet)
    idle = IdleSignal(lambda: activity(session), options.background_timeout_s)
    bus = Bus()
    store = DatabaseSessionStore(db)
    emit = both(bus.publish, both(console, idle))
    if on_event is not None:
        emit = both(emit, on_event)
    session = Session(
        options.workspace,
        settings,
        store,
        lambda: (
            runner_factory
            or functools.partial(
                SdkRunner, tool_routes=session.config.tool_routes, run_as=options.run_as
            )
        )(options.workspace, session.config.claude, hidden | set(session.config.hidden_env)),
        emit,
        project=options.project,
        checker_models=checker_models,
        startup_warnings=preflight.warnings(
            settings.config,
            checkers=settings.checkers,
            workspace=options.workspace,
            protection=options.login_protection,
        ),
        # A conversation that was started with `claude` itself: the checkers are told what
        # its transcript says the user asked for (D209).
        transcript_inputs=functools.partial(user_inputs, directory=options.workspace),
        # A session trace never holds the values of these variables (D232).
        secret_env=hidden,
        # Where a person can come back, a run that calu did not end itself (an escalation
        # timeout, Claude Code ending or failing on its own) is resumed at once and waits for
        # the user (D282): a session of `calu serve`, or `calu run` that reads a terminal. An
        # unattended run ends as before.
        auto_resume=options.stay or (options.interactive and sys.stdin.isatty()),
    )
    session.add_trace_secret(login_password, "password")

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
        # A changed honesty.mode, or a changed honesty.sufficient_trust, changes the honesty
        # state that the clients show.
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
    # A commit or a file content that was certified or removed elsewhere changes the baseline
    # of this session; the session reads the stored trust again at its next step.
    notifier.on(TRUST_CHANGED, lambda _payload: session.trust_changed())
    notifier.on_listening(session.trust_changed)
    # A change committed before LISTEN took effect, or while the listening connection was away,
    # was never announced to this session: look once each time the subscription is in force.
    notifier.on_listening(dirty.hint)
    failed = False
    if options.continue_last:
        continued = await _continued(db, options, console)
        if continued is None:
            return 1
        options = continued
    # Before anything starts: a fork that cannot be made must not turn into a plain resume,
    # which would continue the conversation instead of branching it (agent/records/PLAN.md D36).
    fork = await _fork_point(db, options, console)
    if (options.fork_before_turn is not None or options.fork_id) and fork is None:
        return 1
    resume_id = options.resume_id or options.fork_id
    # calu chooses the id of a new conversation and of a fork, so that it is known before the
    # first turn; a plain resume keeps its id (D203).
    claude_session_id = (
        options.resume_id if options.resume_id and fork is None else str(uuid.uuid4())
    )

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
            say(f"calu: {message}", error=True)
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
                await session.start(
                    tg, resume_id, options.model, options.effort, fork, claude_session_id
                )
                # On a line of its own and first, for a person and for a script: this is the
                # id `calu run --resume` and `calu run --fork` take.
                say(f"[session] {claude_session_id}")
                if options.serve:
                    # After the start, so that the session row and its id exist when the first
                    # client asks; the events of the start are in the bus and are replayed.
                    token = new_token()
                    session.add_trace_secret(token, "client token")
                    server = SessionServer(
                        session, bus, token_hash(token), tg, insecure=options.insecure
                    )
                    tg.start_soon(_listen, server, options, token, store, on_serving, on_ready)
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

                    ended = await read_lines_until(
                        send, server.shutdown if server is not None else None
                    )
                    if ended == "eof" and sent:
                        # A closed stdin only means that no further message comes (a password
                        # piped in, a service unit, nohup). Stopping here ended the session
                        # seconds into its first turn, with exit code 0.
                        say(
                            "calu: stdin closed; waiting for Claude to finish (as with --batch)",
                            error=True,
                        )
                if options.stay:
                    if server is not None:
                        await _until_shut_down(server, bus, session, options.stay_idle_s)
                elif not options.interactive or (ended == "eof" and sent):
                    # `idle` reads the session each time: a message that is still queued behind a
                    # turn, or was sent a moment ago, keeps the run going until its turn is over.
                    if server is not None:
                        await _until_done(idle, server, bus)
                    else:
                        await idle.wait()
        except SessionError as exc:
            say(f"calu: {exc}", error=True)
            failed = True
        finally:
            # The last writes are worth a short wait, but a shutdown must not wait for a
            # database that stays away.
            db.limit_reconnect(STOP_RECONNECT_S)
            with anyio.CancelScope(shield=True):
                await session.stop()
            tg.cancel_scope.cancel()
    return 1 if failed or idle.unfinished or session.state == "failed" else 0


async def _continued(db: Db, options: RunOptions, console: Console) -> RunOptions | None:
    """Resolve `--continue` into the conversation to resume (D280).

    The newest session of `options.workspace` on this host with a Claude session id is
    continued, whether it ended or its process is gone; a directory without one starts a new
    conversation. A newest session that still runs is not continued a second time.

    Side effect: reports the choice, or why it cannot be made, on the console.

    Args:
        db: The database connection.
        options: What `calu run` was asked to do, with `continue_last`.
        console: Where to report.

    Returns:
        The options with `resume_id` set (or unchanged for a new conversation), or None if
        the run must not start: `--continue` came with `--resume` or `--fork`, or the newest
        session still runs.

    Raises:
        psycopg.Error: On database errors.
    """
    if options.resume_id or options.fork_id:
        await console(
            Log(level="error", message="--continue takes neither --resume nor --fork with it")
        )
        return None
    from calu.session import session_os_user  # imported late like `Session` (startup time)

    row = await herd.dormant_session(
        db, socket.gethostname(), session_os_user(), [str(options.workspace)]
    )
    if row is None or row.claude_session_id is None:
        await console(
            Log(
                level="info",
                message=f"no earlier session of {options.workspace}: a new conversation starts",
            )
        )
        return dataclasses.replace(options, continue_last=False)
    if row.ended_at is None and not row.stale:
        await console(
            Log(
                level="error",
                message=f"the newest session of {options.workspace}, {row.id}, is still "
                "running; attach to it with `calu attach`",
            )
        )
        return None
    await console(
        Log(
            level="info",
            message=f"continuing the conversation {row.claude_session_id} of the session {row.id}",
        )
    )
    return dataclasses.replace(options, continue_last=False, resume_id=row.claude_session_id)


async def _fork_point(db: Db, options: RunOptions, console: Console) -> Fork | None:
    """Say where the session branches off: `--fork`, or `--fork-before-turn` (D36, D203).

    `--fork ID` keeps the whole conversation and needs nothing from the database.
    `--resume ID --fork-before-turn N` is resolved into the uuids Claude Code needs.

    Side effect: reports the fork, or why it cannot be made, on the console.

    Args:
        db: The database connection.
        options: What `calu run` was asked to do.
        console: Where to report a problem.

    Returns:
        The fork, or None — both when no fork was asked for and when it cannot be made. The
        caller distinguishes the two by `options.fork_id` and `options.fork_before_turn`.

    Raises:
        psycopg.Error: On database errors.
    """
    turn = options.fork_before_turn
    if options.fork_id:
        if options.resume_id or turn is not None:
            await console(
                Log(
                    level="error",
                    message="--fork takes neither --resume nor --fork-before-turn with it",
                )
            )
            return None
        await console(
            Log(
                level="info",
                message=(
                    f"forking {options.fork_id}: this session continues a copy of that "
                    "conversation under its own session id; the original stays as it is"
                ),
            )
        )
        return Fork()
    if turn is None:
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
    return Fork(resume_at=point.resume_at, drops_turn=point.drops_turn, before_turn=turn)


async def _serve_root_clients(server: SessionServer, listener: socket.socket, path: Path) -> None:
    """Serve a local socket (root's, or an `--insecure` session's); never raises.

    A failure costs that socket, not the session.

    Args:
        server: The protocol server.
        listener: The listening socket.
        path: Its path.
    """
    try:
        await server.listen_local(listener, path)
    except OSError as exc:
        say(f"calu: the local socket stopped ({exc}); its clients use the address", True)


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
    on_ready: Ready | None = None,
) -> None:
    """Serve the protocol and print the address and the token once; never raises.

    A session that stays until a client shuts it down (`RunOptions.stay`) would wait for ever
    without a protocol server, so a server that cannot start ends such a session.

    The token's hash is recorded with the session (`client_tokens`), so an operator can see
    which sessions accept clients and revoke one; the token itself is only printed here.

    Args:
        server: The protocol server.
        options: Host and port to bind.
        token: The client token to print.
        store: The session store, for the address and the token's hash.
        on_serving: Awaited with the address and the token as well, or None.
        on_ready: Awaited with the session row's id and the address after both are recorded,
            or None.
    """

    def failed() -> None:
        """End a session that only a client could end, since no client can reach it."""
        if options.stay:
            server.shutdown.set()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.listen, options.host, options.port, options.socket_path)
            with anyio.move_on_after(START_TIMEOUT_S):
                await server.ready.wait()
            if not server.ready.is_set():
                say("calu: the protocol server did not start", error=True)
                failed()
                return
            say(f"[clients] {server.address}")
            # The short form first, because it is the one a user here will use (D105); the
            # explicit one is for a client on another machine and needs no database. The token
            # goes into the environment, not onto the command line, where every user of that
            # machine could read it in the process list (D279).
            say("[clients] attach from this directory with: calu attach")
            if not options.quiet:
                # Not in a server's shared output: a token there would be readable long after
                # its session, and a client of a served session mints its own (D105).
                say(
                    "[clients] from another machine: "
                    f"CALU_TOKEN={token} calu attach {server.address}"
                )
            if store.session_id is not None:
                try:
                    await store.set_address(server.address)
                    await store.add_client_token(token_hash(token), server.address)
                except Exception:
                    log.exception("recording the address and the client token failed")
            if options.secure:
                say("[clients] --secure: every client shows a client token")
            if options.insecure:
                say(
                    "[clients] --insecure: clients on this machine (a Unix socket or a "
                    "loopback address) need no token; every process here that can reach "
                    "them can act as a client"
                )
            # The user whose `calu attach` should find an `--insecure` session: calu's own user,
            # or, for a root session, the user Claude Code runs as. This process already has
            # that user's file identity (`asuser.enter`), so the socket and its directories
            # are created as that user; `bind` refuses them otherwise.
            local_uid = (
                os.geteuid()
                if os.geteuid() != 0
                else (options.run_as.uid if options.run_as is not None else None)
            )
            if options.insecure and local_uid is not None and store.session_id is not None:
                root = (
                    localsocket.user_root()
                    if os.geteuid() != 0
                    else localsocket.user_root(uid=local_uid)
                )
                path = localsocket.path_of(str(store.session_id), root)
                try:
                    listener = await anyio.to_thread.run_sync(
                        functools.partial(localsocket.bind, path, owner=local_uid)
                    )
                except OSError as exc:
                    say(f"calu: no local socket ({exc}); clients use the address", True)
                else:
                    tg.start_soon(_serve_root_clients, server, listener, path)
                    say(f"[clients] local clients, without a token: unix:{path}")
            if (
                not options.secure
                and os.geteuid() == 0
                and options.run_as is not None
                and store.session_id is not None
            ):
                # A root session whose Claude Code runs as another user also accepts root
                # clients on a socket only root can reach, without a token (D215): there
                # the kernel tells the client apart from Claude Code (D226). With `--user
                # root` everything Claude Code starts is root as well and would pass that
                # check, so such a session binds no socket and every client shows a token.
                # Created with root's file access, which this process has given up for its
                # own work.
                path = localsocket.path_of(str(store.session_id))
                try:
                    listener = await anyio.to_thread.run_sync(
                        asuser.as_root, localsocket.bind, path
                    )
                except (OSError, asuser.UserError) as exc:
                    say(f"calu: no socket for root clients ({exc}); they use the token", True)
                else:
                    tg.start_soon(_serve_root_clients, server, listener, path)
                    say(f"[clients] root clients, without a token: unix:{path}")
            if on_serving is not None:
                await on_serving(server.address, token)
            if on_ready is not None:
                await on_ready(store.session_id, server.address)
    except OSError as exc:
        say(f"calu: the protocol server could not start: {exc}", error=True)
        failed()


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


async def _until_shut_down(
    server: SessionServer, bus: Bus, session: Session, idle_s: float | None
) -> None:
    """Wait until a session that stays (`RunOptions.stay`) is to end.

        a client sent `shutdown`, or the protocol server could not start  ──► return
        idle_s given, and for that long: no client attached and the
        session idle, stopped or failed                                   ──► say so, return

    Args:
        server: The protocol server, for its `shutdown` event.
        bus: The bus, for the number of attached clients.
        session: The session, for its state.
        idle_s: The idle time after which the session ends, or None for no limit.
    """
    if idle_s is None:
        await server.shutdown.wait()
        return
    quiet_since: float | None = None
    while not server.shutdown.is_set():
        busy = bus.clients > 0 or session.state not in ("idle", "stopped", "failed")
        now = time.monotonic()
        quiet_since = None if busy else (quiet_since if quiet_since is not None else now)
        if quiet_since is not None and now - quiet_since >= idle_s:
            say(
                f"[session] idle for {idle_s:.0f} s with no client attached: ending it "
                "(`calu start --resume` continues the conversation)"
            )
            return
        with anyio.move_on_after(min(5.0, max(0.05, idle_s / 4))):
            await server.shutdown.wait()


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
