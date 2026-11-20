"""`calu attach`: a terminal client for a running session (agent/records/PLAN.md phase 4).

    calu attach                                      # the session running in this directory
    calu attach ws://127.0.0.1:8765 --token=<token>  # a session elsewhere, through a tunnel

It connects to the session's WebSocket, prints the current state and then every event, and
sends what you type as a chat message. Several clients may be attached at the same time, and
detaching leaves the session running.

Commands typed instead of a message:

    /quit                 detach (the session keeps running)
    /comment PATH FROM TO TEXT
                          comment on lines FROM..TO of a project file (ported from sitl's
                          `sitl chat`); the client must run on the session's machine, because
                          it reads the file itself
    /to AGENT TEXT        message for a running background subagent
    /stop                 stop the Claude session (`session.stop`)
    /start [resume id]    start a Claude session (`session.start`)
    /interrupt            end Claude's current turn
    /mode [MODE]          Claude Code's permission mode: show it, or switch to default,
                          acceptEdits or auto (`permission_mode.set`)
    /why <id>             what the checker saw and said when it escalated this step
    /checkers             the checker definitions this session uses (name, model, version)
    /allow <id> [note]    resolve an escalation (`escalation.decide`)
    /deny <id> [note]     resolve an escalation
    /answer <id> TEXT     answer a question Claude asked (its options are in the event)
    /inject <text>        send the text so Claude reads it after its running tool calls
    /state                print the state again
    /honesty              print the code honesty state (baseline, marks, unreviewed files)
    /trust [REV]          certify a commit or a branch's present head as honest (default HEAD)
    /trust-file PATH      certify the present content of one file as honest
    /honesty-review       ask for an honesty review of the unreviewed and marked files
    /clear-mark <id>      close one open honesty mark by hand
    /checker-context      print the conversation as the checkers see it
    /usage                the usage limits Claude Code reported, Claude Code's own `/usage`
                          (read without a turn), cost and context fill (`usage.panel`)
    /sessions             list the stored sessions of this project
    /history [CLAUDE ID]  the recorded conversation of a session, from the database
    /running              the sessions of the `calu serve` whose relay this client uses
    /switch ID            attach to another of them
    /new [TEXT]           ask that server for a new session here, and attach to it
    /fork [TEXT]          ask it for a fork of this conversation, and attach to it
    /shutdown             stop the session and end the calu process

Any other `/word` is a command of Claude Code (`/compact`, `/context`, `/cost`, a skill): the
line is sent to Claude as chat text, and Claude Code runs it.

The four relay commands work only through a relay of `calu serve` (agent/records/PLAN.md
D242): the full-screen client offers them with the database too.

The events are rendered by `calu.run.format_event`, so an attached terminal shows the same
lines as the terminal that started the session.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import TaskGroup

from calu.controls import visible
from calu.herd import age_text
from calu.homes import short_path
from calu.protocol.client import (
    ProtocolClient,
    RelayTarget,
    RequestFailed,
    connect,
    first_set,
    relay_call,
    single_error,
)
from calu.protocol.messages import (
    CLOSE_SERVER_TOKEN_REVOKED,
    ENDED_WITH_SESSION,
    EVENTS,
    SWITCHABLE_PERMISSION_MODES,
    EscalationRequest,
    Event,
    HonestyInfo,
    RateLimit,
    UsagePanelResult,
)
from calu.run import format_event, honesty_lines, one_line, read_lines

log = logging.getLogger(__name__)

CLAUDE_WORD = re.compile(r"/[\w:.-]+")
"""A slash word that is not calu's and can name a command of Claude Code; such a line is sent
as chat text (the same rule as `calu.tui.commands.CLAUDE_WORD`)."""

EVENT_TYPES: dict[str, type[Event]] = {event.method: event for event in EVENTS}
"""Event class per method, to turn the received params back into an event for rendering."""

HELP = (
    "/help  /quit (or /exit)  /stop  /start [resume id]  /interrupt  /mode [mode]  /why <id>  "
    "/allow <id> [note]  /deny <id> [note]  /answer <id> <text>  "
    "/inject <text>  /to <agent> <text>  /comment <path> <from> <to> <text>  /state  "
    "/checker-context  /checkers  /usage  /sessions  /history [id]  /honesty  "
    "/trust [commit-or-branch]  /trust-file <path>  /honesty-review  /clear-mark <id>  "
    "/running  /switch <id>  "
    "/new [text]  /fork [text]  /shutdown  (any other /word goes to Claude Code)"
)
"""One line listing the commands, printed on connect and for an unknown command.

Every command `Console.line` answers belongs here, aliases included: a command that is not in
this line is a command nobody finds. `tests/unit/test_attach.py` reads the commands out of the
`match` statement and fails when one is missing, which is how `/help` and the `/exit` alias
turned out to be absent from it."""


def say(text: str, flush: bool = True) -> None:
    """Print one piece of the line client's output, safe for a terminal.

    Text of the session (Claude's words, commands, tool results, file content) may hold
    characters a terminal obeys instead of showing them; each is replaced by a visible sign
    (`calu.controls.visible`), so that no output can clear the screen, overwrite a line or
    draw a command in another order than it would run.

    Args:
        text: What to print, followed by a line break.
        flush: Whether to flush stdout afterwards.
    """
    print(visible(text), flush=flush)


def render(method: str, params: dict[str, Any]) -> str | None:
    """Render one received event as a terminal line.

    Args:
        method: The notification method.
        params: Its params.

    Returns:
        The line, or None for events the terminal does not show. An escalation is followed
        by the commands of this client that show and decide it. An event that cannot be
        turned back into its model (an older or newer session) is shown as its raw params.
    """
    model = EVENT_TYPES.get(method)
    if model is None:
        return f"[{method}] {params}"
    try:
        event = model.model_validate(params)
        if isinstance(event, EscalationRequest):
            # `format_event` names `calu resolve`, the command of another terminal. Here the
            # step is decided with the line client's own commands.
            number = event.escalation_id
            how = (
                f"/answer {number} <your answer>"
                if event.questions
                else f"/allow {number} [note]  ·  /deny {number} [note]"
            )
            return (
                f"\n*** escalation {number}: {event.tool_name}\n"
                f"    {event.reason}\n"
                f"    /why {number} shows everything  ·  {how}\n"
            )
        return format_event(event)
    except Exception:
        log.debug("could not render %s", method, exc_info=True)
        return f"[{method}] {params}"


@dataclass
class Printer:
    """Prints the events of the session as they arrive."""

    show_thinking: bool = False

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        """Print one event.

        Args:
            method: The notification method.
            params: Its params.
        """
        if method == "assistant.thinking" and not self.show_thinking:
            return
        line = render(method, params)
        if line is not None:
            say(line, flush=True)


def describe_state(snapshot: dict[str, Any]) -> str:
    """Return a one-line description of a `state.get` result.

    Args:
        snapshot: The snapshot as the session returned it.

    Returns:
        State, workspace, model, Claude Code's permission mode (when known) and the number of
        open escalations.
    """
    waiting = len(snapshot.get("escalations") or ())
    mode = snapshot.get("permission_mode")
    return (
        f"[state] {snapshot.get('state')} · {snapshot.get('workspace')} · "
        f"model {snapshot.get('model')}"
        + (f" · permission mode {mode}" if mode else "")
        + (f" · {waiting} escalation(s) open" if waiting else "")
    )


def running_lines(
    sessions: list[dict[str, Any]],
    current: str | None,
    relay: RelayTarget,
    now: datetime | None = None,
) -> list[str]:
    """Render the sessions of `relay.sessions` for `/running`, one aligned line each.

    Args:
        sessions: The `sessions` of the relay's answer (`RelaySession` as dicts).
        current: The id of the session this client shows, marked with `*`; None for none.
        relay: The relay, for the server's user and host (the session user's home is `~`).
        now: The present moment; None for the clock.

    Returns:
        `* <id>  <state[ · N open]> for <time>  "<name>"  <directory>` per session, the state
        column as wide as the longest state of the list; without a name or a known time in
        state those parts are left out.
    """
    now = now or datetime.now(UTC)
    states: list[str] = []
    for entry in sessions:
        waiting = entry.get("open_escalations") or 0
        state = str(entry.get("state") or "unknown") + (f" · {waiting} open" if waiting else "")
        since = entry.get("state_since")
        if since:
            moment = datetime.fromisoformat(str(since))
            state += f" for {age_text((now - moment).total_seconds())}"
        states.append(state)
    width = max((len(state) for state in states), default=0)
    lines = []
    for entry, state in zip(sessions, states, strict=True):
        mark = "*" if entry["calu_session_id"] == current else " "
        name = f'"{visible(str(entry["name"]))}"  ' if entry.get("name") else ""
        place = short_path(str(entry.get("directory") or ""), relay.os_user, relay.hostname or None)
        lines.append(f"{mark} {entry['calu_session_id']}  {state:<{width}}  {name}{place}")
    return lines


def usage_text(result: dict[str, Any]) -> str:
    """Render the result of `usage.panel` as lines for the terminal (agent/records/PLAN.md D248).

    Args:
        result: The result as the session sent it.

    Returns:
        `[usage]` lines: the account, each limit with when Claude Code reported it and
        whether it is a stored figure of an earlier session (or that none was reported yet),
        the plan's limits Claude Code read now (or why they could not be read), Claude Code's
        `/usage` text (or why it is missing), cost and context fill.
    """
    panel = UsagePanelResult.model_validate(result)
    now = datetime.now(UTC)
    lines: list[str] = []

    def limit_line(limit: RateLimit, stamped: bool) -> str:
        """Render one limit.

        Args:
            limit: The limit.
            stamped: Whether to say when it was reported.

        Returns:
            One line.
        """
        used = (
            f"{round(limit.utilization * 100)}% used"
            if limit.utilization is not None
            else limit.status.replace("_", " ")
        )
        resets = f", resets {limit.resets_at:%a %H:%M} UTC" if limit.resets_at else ""
        when = ""
        if stamped and limit.reported_at:
            stored = ", stored" if limit.stored else ""
            when = f" (reported {age_text((now - limit.reported_at).total_seconds())} ago{stored})"
        return f"[usage] {limit.type or 'limit'}: {used}{resets}{when}"

    if panel.account:
        lines.append(f"[usage] account: {visible(panel.account)}")
    if not panel.limits:
        lines.append("[usage] limits: not reported yet by Claude Code")
    lines += [limit_line(limit, True) for limit in panel.limits]
    if panel.account_limits:
        plan = f" ({panel.subscription})" if panel.subscription else ""
        lines.append(f"[usage] your plan now{plan}:")
        lines += [limit_line(limit, False) for limit in panel.account_limits]
    elif panel.account_limits_problem:
        lines.append(f"[usage] your plan now: {visible(panel.account_limits_problem)}")
    if panel.claude_usage:
        lines.append("[usage] Claude Code's /usage (read without a turn):")
        lines += [f"  {visible(row)}" for row in panel.claude_usage.splitlines()]
    else:
        lines.append(f"[usage] Claude Code's /usage: {panel.claude_usage_problem}")
    cost = f"${panel.cost_usd:.4f}" if panel.cost_usd is not None else "no turn has ended yet"
    lines.append(f"[usage] cost {cost} · {panel.num_turns} turn(s)")
    if panel.context is not None:
        context = panel.context
        lines.append(
            f"[usage] context {context.tokens:,} of {context.max_tokens:,} tokens "
            f"({context.percent:.0f}%)"
        )
    return "\n".join(lines)


def describe_honesty(info: dict[str, Any]) -> str:
    """Render the code honesty state of a `state.get` result for the terminal.

    Args:
        info: The `honesty` object of the snapshot (`calu.protocol.messages.HonestyInfo`).

    Returns:
        The lines `calu.run.honesty_lines` renders for that state; one line saying so if the
        object cannot be read.
    """
    try:
        return "\n".join(honesty_lines(HonestyInfo.model_validate(info)))
    except ValueError:
        return f"[honesty] {info}"


class Console:
    """Turns the lines a user types into requests to the session."""

    def __init__(
        self,
        client: ProtocolClient,
        workspace: Path | None = None,
        relay: RelayTarget | None = None,
        current: str | None = None,
    ) -> None:
        """Create the console for a connected client.

        Args:
            client: The connected protocol client. `attach` replaces `client` when it had to
                connect again.
            workspace: The session's project directory, which `/comment` reads files from;
                None refuses `/comment`.
            relay: The `calu serve` relay the client is attached through, which `/running`,
                `/switch`, `/new` and `/fork` use; None refuses them.
            current: The id of the attached session's row, if known.
        """
        self.client = client
        self.workspace = workspace
        self.relay = relay
        self.current = current
        self.first = current
        """The row id the client attached to first; `current` changes with a `/start`."""
        self.switch_to: str | None = None
        """The session `/switch`, `/new` or `/fork` named; the console quits for it."""
        self.quit = anyio.Event()
        self.background: TaskGroup | None = None
        """Where a request that can take minutes runs (`/honesty-review`), so that further
        lines can be typed meanwhile. None waits for such a request in place."""

    async def line(self, text: str) -> None:
        """Handle one typed line: a command, or a chat message. Never raises for a failed request.

        A request that fails because the connection is gone ends the console (`quit`), unless
        the session closed the connection because this client fell behind: `attach` is then
        connecting again, and the user is told that the line was not sent.

        Args:
            text: The line without its line end.
        """
        await self._reported(lambda: self._line(text))

    async def _reported(self, work: Callable[[], Awaitable[None]]) -> None:
        """Run one piece of the console's work and print its failure instead of raising.

        Args:
            work: Handles a typed line, or waits for a request that was started in the
                background.
        """
        try:
            await work()
        except RequestFailed as exc:
            # The session's sentence without the JSON-RPC code, which tells a person nothing.
            say(f"[error] {exc.error.get('message') or exc}", flush=True)
        except ConnectionError as exc:  # before OSError, of which it is a subclass
            say(f"[error] {exc}", flush=True)
            if getattr(self.client, "fell_behind", False):
                # `attach` is connecting again (the session closed the connection because this
                # client fell behind); the line was not sent and can be typed once more.
                say("[error] not sent: connecting again, type it once more", flush=True)
            else:
                self.quit.set()
        except OSError as exc:  # /comment could not read the file
            reason = exc.strerror or type(exc).__name__
            say(f"[error] the file cannot be read ({reason}): {exc.filename or exc}", flush=True)

    async def _review(self) -> None:
        """Ask the session for an honesty review and print its answer (`/honesty-review`).

        Raises:
            RequestFailed: If the session refused the request.
            ConnectionError: If the connection is gone.
        """
        done = await self.client.request("honesty.review")
        problem = done.get("problem")
        if done.get("ran"):
            say("[honesty] the review ran" + (f": {problem}" if problem else ""), flush=True)
        else:
            say(f"[honesty] no review ran: {problem}", flush=True)

    async def _line(self, text: str) -> None:
        """Handle one typed line (see `line`).

        Args:
            text: The line without its line end.

        Raises:
            RequestFailed: If the session refused the request.
            ConnectionError: If the connection is gone.
            OSError: If `/comment` cannot read the file.
        """
        if not text.startswith("/"):
            await self.client.request("chat.send", {"text": text})
            return
        command, _, rest = text.partition(" ")
        rest = rest.strip()
        match command:
            case "/quit" | "/exit":
                self.quit.set()
            case "/help":
                say(HELP, flush=True)
            case "/stop":
                await self.client.request("session.stop")
            case "/start":
                result = await self.client.request(
                    "session.start", {"resume_id": rest} if rest else {}
                )
                say(f"[started] {result}", flush=True)
                # A start after a stop runs a new session row; `attach` follows it when the
                # session closes this client's connection because the token was the old row's.
                row = result.get("calu_session_id") if isinstance(result, dict) else None
                if isinstance(row, str):
                    self.current = row
            case "/interrupt":
                await self.client.request("interrupt")
            case "/mode":
                if not rest:
                    snapshot = await self.client.request("state.get")
                    say(
                        f"[mode] Claude Code's permission mode: "
                        f"{snapshot.get('permission_mode') or 'not known yet'}; "
                        f"/mode {' | '.join(SWITCHABLE_PERMISSION_MODES)} switches it",
                        flush=True,
                    )
                    return
                # The session answers with `permission_mode.changed`, which is printed.
                await self.client.request("permission_mode.set", {"mode": rest})
            case "/inject":
                if not rest:
                    say("[error] /inject needs a text", flush=True)
                    return
                await self.client.request("chat.send", {"text": rest, "delivery": "inject"})
            case "/to":
                target, _, message = rest.partition(" ")
                if not target or not message.strip():
                    say("[error] /to needs a subagent id and a text", flush=True)
                    return
                await self.client.request(
                    "chat.send",
                    {"text": message.strip(), "to": target, "delivery": "inject"},
                )
            case "/comment":
                await self._comment(rest)
            case "/answer":
                await self._answer(rest)
            case "/allow" | "/deny":
                await self._decide(command.removeprefix("/"), rest)
            case "/trust":
                # A person certifies a commit; a branch means the commit it points at now.
                done = await self.client.request("honesty.trust", {"rev": rest or "HEAD"})
                state = "certified" if done.get("recorded") else "was already certified"
                say(f"[honesty] commit {str(done.get('commit_id'))[:12]} {state}", flush=True)
            case "/trust-file":
                if not rest:
                    say("[error] /trust-file needs a path", flush=True)
                    return
                done = await self.client.request("honesty.trust", {"path": rest})
                state = "certified" if done.get("recorded") else "was already certified"
                say(
                    f"[honesty] the content of {done.get('path')} "
                    f"({str(done.get('content_sha256'))[:12]}) {state}",
                    flush=True,
                )
            case "/honesty-review":
                # A review can take minutes: its progress arrives as events, and its answer
                # is printed when it comes, while further lines can be typed.
                if self.background is None:
                    await self._review()
                else:
                    say("[honesty] review requested", flush=True)
                    self.background.start_soon(self._reported, self._review)
            case "/honesty":
                snapshot = await self.client.request("state.get")
                say(describe_honesty(snapshot.get("honesty") or {}), flush=True)
            case "/clear-mark":
                # The marks are in the session's memory: clearing one is a request to the
                # session, a person's override of a checker.
                if not rest.strip().isdigit():
                    say("[error] /clear-mark needs a mark id", flush=True)
                else:
                    await self.client.request("honesty.clear", {"mark_id": int(rest.strip())})
            case "/why":
                if not rest:
                    say("[error] /why needs an escalation id", flush=True)
                    return
                found = await self.client.request(
                    "escalation.context", {"escalation_id": rest.removeprefix("#")}
                )
                say(f"--- escalation {found['escalation_id']} ({found['state']})", flush=True)
                say(found.get("reason", ""), flush=True)
                for run in found.get("checker_runs", []):
                    say(
                        f"  [{run['checker_name']} v{run['checker_version']}, {run['model']}]"
                        f" {run['decision']}: {one_line(run.get('reason', ''))}",
                        flush=True,
                    )
                say(found.get("rendered", ""), flush=True)
                for path, content in (found.get("files") or {}).items():
                    say(f"--- {path} before the change\n{content}", flush=True)
                say(f"--- the conversation the checker saw\n{found.get('recent', '')}", flush=True)
            case "/checkers":
                result = await self.client.request("checkers.get")
                for checker in result.get("checkers", []):
                    say(
                        f"{checker['name']}  route {checker['route']}  {checker['model']}"
                        f"  v{checker['version']}  "
                        f"{len(checker.get('instructions', ''))} chars of instructions",
                        flush=True,
                    )
            case "/state":
                say(describe_state(await self.client.request("state.get")), flush=True)
            case "/checker-context":
                context = await self.client.request("context.get")
                say(context.get("chat_summary", ""), flush=True)
                say(context.get("recent", ""), flush=True)
            case "/usage":
                say(usage_text(await self.client.request("usage.panel")), flush=True)
            case "/history":
                history = await self.client.request(
                    "session.history", {"claude_session_id": rest} if rest else {}
                )
                for turn in history.get("turns", []):
                    for text in turn.get("user_inputs", []):
                        say(f"> {one_line(text)}", flush=True)
                    for step in turn.get("steps", []):
                        decision = step.get("decision") or "?"
                        say(
                            f"  [{step.get('tool_name')}, {decision}] {step.get('summary')}",
                            flush=True,
                        )
                    answer = turn.get("answer_narration") or turn.get("answer") or ""
                    if answer:
                        say(one_line(answer), flush=True)
                say(
                    f"[history] {len(history.get('turns', []))} turn(s), "
                    f"{len(history.get('steps', []))} step(s) of "
                    f"{history.get('claude_session_id') or 'this session'}",
                    flush=True,
                )
            case "/sessions":
                result = await self.client.request("session.list", {})
                for session in result.get("sessions", []):
                    say(
                        f"{session['claude_session_id'] or '(no id)'}  {session['state']}  "
                        f"{session['started_at']}  {session['first_input']}",
                        flush=True,
                    )
            case "/running" | "/switch" | "/new" | "/fork":
                await self._relayed(command, rest)
            case "/shutdown":
                await self.client.request("shutdown")
                self.quit.set()
            case _ if CLAUDE_WORD.fullmatch(command):
                # Not calu's: Claude Code's own command (`/compact`, `/context`, `/cost`, a
                # skill), sent as chat text; Claude Code runs it (agent/records/PLAN.md D248).
                await self.client.request("chat.send", {"text": text})
            case _:
                say(f"[error] unknown command {command}. {HELP}", flush=True)

    async def _relayed(self, command: str, rest: str) -> None:
        """Handle `/running`, `/switch`, `/new` and `/fork` through the relay (D242).

            /running        ──► one line per session of the server, "*" for this one
            /switch ID      ──► the one session whose id begins with ID: quit for it
            /new [TEXT]     ──► the server starts a session in this directory: quit for it
            /fork [TEXT]    ──► the server starts a fork of this conversation: quit for it

        Args:
            command: The command.
            rest: Its argument.

        Raises:
            RequestFailed: If the session refused `state.get`.
            ConnectionError: If the connection to the session is gone.
        """
        relay = self.relay
        if relay is None:
            say(
                f"[error] {command} works through the relay of `calu serve` (calu attach "
                "ws://127.0.0.1:<relay port>) or in the full-screen client",
                flush=True,
            )
            return
        try:
            if command in ("/running", "/switch"):
                _hello, listed = await relay_call(relay.address, relay.token, "relay.sessions")
                sessions = (listed or {}).get("sessions") or []
                if command == "/running":
                    for text in running_lines(sessions, self.current, relay):
                        say(text, flush=True)
                    say(f"[running] {len(sessions)} session(s); /switch <id> attaches", flush=True)
                    return
                found = [e for e in sessions if str(e["calu_session_id"]).startswith(rest)]
                if not rest or len(found) != 1:
                    say(f"[error] /switch needs the beginning of one id ({len(found)} match)")
                    return
                self.switch_to = str(found[0]["calu_session_id"])
                self.quit.set()
                return
            snapshot = await self.client.request("state.get")
            params: dict[str, Any] = {
                "kind": "new",
                "directory": snapshot.get("workspace") or str(self.workspace or ""),
                "prompt": rest or None,
                "model": snapshot.get("model"),
                "project": snapshot.get("project"),
            }
            if command == "/fork":
                if not snapshot.get("claude_session_id"):
                    say("[error] nothing to fork yet: the session has no Claude session id")
                    return
                params.update(kind="fork", claude_session_id=snapshot["claude_session_id"])
            say("[session] asked calu serve for the session …", flush=True)
            _hello, started = await relay_call(
                relay.address, relay.token, "relay.start", params, 150.0
            )
            self.switch_to = str((started or {}).get("calu_session_id") or "")
            self.quit.set()
        except (OSError, TimeoutError) as exc:
            say(f"[error] the relay {relay.address} cannot be reached: {exc}", flush=True)

    async def _comment(self, rest: str) -> None:
        """Send a comment on a range of lines of a project file.

        The client reads the file itself, because the protocol carries the text of the
        document, not a path the session would open (`comment.add`).

        Args:
            rest: "PATH FROM TO TEXT".

        Raises:
            RequestFailed: If the session refused the comment (no session runs, or the
                selection lies outside the file).
            OSError: If the file cannot be read.
        """
        parts = rest.split(maxsplit=3)
        if len(parts) < 4:
            say("[error] /comment needs a path, a first and last line and a text", flush=True)
            return
        path, first, last, text = parts
        if self.workspace is None:
            say("[error] the session did not say where its project is", flush=True)
            return
        try:
            start, end = int(first), int(last)
        except ValueError:
            say(f"[error] not line numbers: {first!r} {last!r}", flush=True)
            return
        content = (self.workspace / path).read_text(encoding="utf-8")
        result = await self.client.request(
            "comment.add",
            {
                "kind": "file",
                "path": path,
                "start_line": start,
                "end_line": end,
                "after_text": content,
                "text": text,
            },
        )
        excerpt = (result.get("comment") or {}).get("after", {}).get("text", "")
        say(f"[comment] on {path}:{start}-{end}: {one_line(excerpt)}", flush=True)

    async def _answer(self, rest: str) -> None:
        """Answer a question of the agent from an `/answer` line (agent/records/PLAN.md D35).

        The answer reaches Claude as the result of its `AskUserQuestion` call, which does not
        run; the escalation is recorded as "answered".

        Args:
            rest: The escalation id (a leading "#" is dropped) and the answer.

        Raises:
            RequestFailed: If the session refused it (e.g. unknown escalation).
        """
        escalation_id, _, answer = rest.partition(" ")
        escalation_id = escalation_id.removeprefix("#")  # as the full-screen client shows it
        if not escalation_id or not answer.strip():
            say("[error] /answer needs an escalation id and an answer", flush=True)
            return
        await self.client.request(
            "escalation.decide",
            {"escalation_id": escalation_id, "answer": answer.strip()},
        )

    async def _decide(self, decision: str, rest: str) -> None:
        """Resolve an escalation from an `/allow` or `/deny` line.

        Args:
            decision: "allow" or "deny".
            rest: The escalation id (a leading "#" is dropped) and an optional note.

        Raises:
            RequestFailed: If the session refused the decision (e.g. unknown escalation).
        """
        escalation_id, _, note = rest.partition(" ")
        escalation_id = escalation_id.removeprefix("#")  # as the full-screen client shows it
        if not escalation_id:
            say(f"[error] /{decision} needs an escalation id", flush=True)
            return
        await self.client.request(
            "escalation.decide",
            {
                "escalation_id": escalation_id,
                "decision": decision,
                "note": note.strip() or None,
            },
        )


async def attach(
    address: str,
    token: str,
    after_seq: int = 0,
    show_thinking: bool = False,
    relay: RelayTarget | None = None,
    gone: Callable[[], bool] | None = None,
    switch_to: list[str] | None = None,
) -> int:
    """Attach to a running session until the user detaches or the session closes.

    Side effects: connects to `address`, prints the state and every event, and reads lines
    from stdin as chat messages or commands.

    If the session closes the connection because this client fell behind
    (`CLOSE_FELL_BEHIND`: the terminal did not take the output fast enough), the client
    connects again, continues after the last event it printed and says so. If the session can
    no longer replay all the events in between, the state and the open escalations are printed
    again. At most `MAX_RECONNECTS` such reconnects follow each other without an event in
    between. If the connection breaks (`BROKEN_CODES`: a dropped SSH tunnel, a network that went
    away), the client tries again after 1, 2, 4, 8, then every 15 seconds for `REDIAL_FOR_S`
    seconds, says so on every attempt, and continues after the last event it printed; a
    session that refuses the token at such an attempt has ended, which ends the client. A
    session whose process is known to be gone (`gone`) ends the client at once. A session that
    closes the connection because the client's minted token belonged to the row that ended
    (`ENDED_WITH_SESSION`), after this client's `/start` told it the new row, ends the client
    with that row in `switch_to` (when given), so that the caller attaches to it.

    Args:
        address: The WebSocket address that `calu run` printed.
        token: The client token that `calu run` printed.
        after_seq: Sequence number to continue after (0 shows what the buffer still holds).
        show_thinking: Also print Claude's thinking.
        relay: The `calu serve` relay the address goes through, for `/running`, `/switch`,
            `/new` and `/fork`; None for a session's own address.
        gone: Asked when the connection breaks: True if the session's process is known to
            be gone (a session of this machine). None tries again for `REDIAL_FOR_S`.
        switch_to: Gets the id of the session `/switch`, `/new` or `/fork` named appended,
            when the client ends for it; the caller attaches to that one next.

    Returns:
        0 when the user detached, the session closed the connection, or a broken connection
        could not be opened again in time.

    Raises:
        OSError: If the address cannot be reached.
        ConnectionError: If the session closes the connection before it answered the first
            request of the first connection, for another reason than this client falling
            behind, or after `MAX_RECONNECTS` reconnects.
        RequestFailed: If the token is refused at the first connection.
    """
    printer = Printer(show_thinking)
    quit_event = anyio.Event()
    console: Console | None = None
    reconnects = 0
    redials = 0
    broken_since: float | None = None
    try:
        async with anyio.create_task_group() as tg:

            async def input_lines(console: Console) -> None:
                """Read typed lines until the user detaches or stdin ends.

                Args:
                    console: The console that handles the lines.
                """
                await read_lines(console.line)  # returns at end of input or on /quit
                quit_event.set()

            while not quit_event.is_set():
                broken = False
                try:
                    async with connect(
                        address, token, "calu attach", printer, after_seq=after_seq
                    ) as client:
                        try:
                            snapshot = await client.request("state.get")
                        except ConnectionError:
                            # The session replays what this client missed before it answers;
                            # a client that falls behind during the replay is closed while
                            # this request waits. That counts like falling behind afterwards.
                            if not (client.fell_behind and reconnects < MAX_RECONNECTS):
                                raise
                            reconnects += 1
                            after_seq = max(after_seq, client.last_seq)
                            say(FELL_BEHIND, flush=True)
                            continue
                        if console is None or not client.complete:
                            print_snapshot(snapshot)
                        if console is None:
                            say(f"[attached] {HELP}", flush=True)
                            directory = snapshot.get("workspace")
                            current = snapshot.get("calu_session_id")
                            console = Console(
                                client,
                                Path(directory) if isinstance(directory, str) else None,
                                relay,
                                current if isinstance(current, str) else None,
                            )
                            console.quit = quit_event
                            console.background = tg
                            tg.start_soon(input_lines, console)
                        else:
                            console.client = client
                            say(
                                "[attached again] continuing after the last event shown",
                                flush=True,
                            )
                        redials, broken_since = 0, None
                        await first_set(quit_event, client.closed)
                        if client.last_seq > after_seq:
                            reconnects = 0
                        after_seq = client.last_seq
                        if quit_event.is_set():
                            break
                        if client.fell_behind and reconnects < MAX_RECONNECTS:
                            reconnects += 1
                            say(FELL_BEHIND, flush=True)
                            continue
                        broken = client.close_code in BROKEN_CODES
                        reason = client.describe_close()
                        if (
                            ENDED_WITH_SESSION in reason
                            and switch_to is not None
                            and console.current is not None
                            and console.current != console.first
                        ):
                            # The session was started again under a new row, for which this
                            # client's minted token is not valid: `calu attach` attaches to
                            # the new row with a token of its own, as after `/switch`.
                            console.switch_to = console.current
                            quit_event.set()
                            break
                        if not broken:
                            say(
                                f"[detached] {closer(client.close_code)} closed the connection: "
                                f"{'the session ended' if reason == NO_CLOSE_REASON else reason}",
                                flush=True,
                            )
                            quit_event.set()
                            break
                        reason = "the connection broke"
                except OSError as error:
                    if console is None:
                        raise  # the first connection: the caller says why
                    broken, reason = True, unreachable(error)
                except RequestFailed as error:
                    if console is None or broken_since is None:
                        raise
                    say(
                        f"[detached] the session refused this client when it connected again "
                        f"({error}): the session has ended or the token was revoked",
                        flush=True,
                    )
                    quit_event.set()
                    break
                if not broken:
                    continue
                if gone is not None and gone():
                    say(f"[detached] {ENDED_GONE}", flush=True)
                    quit_event.set()
                    break
                now = anyio.current_time()
                broken_since = now if broken_since is None else broken_since
                if now - broken_since >= REDIAL_FOR_S:
                    say(
                        f"[detached] connection lost: {reason}; no connection for "
                        f"{REDIAL_FOR_S / 60:.0f} minutes, so this client stopped trying. "
                        "The session may still run: attach again when the address is "
                        "reachable",
                        flush=True,
                    )
                    quit_event.set()
                    break
                delay = REDIAL_DELAYS_S[min(redials, len(REDIAL_DELAYS_S) - 1)]
                redials += 1
                say(
                    f"[reconnecting] connection lost: {reason}; try {redials} in "
                    f"{delay:.0f} s (/quit or Ctrl-D leaves)",
                    flush=True,
                )
                with anyio.move_on_after(delay):
                    await quit_event.wait()
            tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        # The task group wraps every error in a group; the caller handles the error itself
        # (a refused token, an unreachable address), as this function's contract says.
        error = single_error(group)
        raise error from None
    if console is not None and console.switch_to and switch_to is not None:
        switch_to.append(console.switch_to)
    return 0


FELL_BEHIND = "[reconnecting] the session closed the connection because this client fell behind"
"""What the line client prints before it connects again after falling behind."""

MAX_RECONNECTS = 3
"""How often `attach` connects again after falling behind without having received an event in
between; after that it detaches instead of looping against a session it cannot keep up with."""


REDIAL_FOR_S = 300.0
"""How long the client keeps trying to connect again after its connection broke (a closed
connection without a close frame, a keepalive that got no answer, an address that refuses):
long enough for a dropped SSH tunnel to be opened again or a laptop to find its network."""

REDIAL_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 15.0)
"""The waits between the attempts after a broken connection; the last one repeats."""

NO_CLOSE_REASON = "no reason given"
"""What `ProtocolClient.describe_close` says of a connection the session closed without a
reason."""

ENDED_GONE = "the session ended: its process is gone"
"""Why a client stops at once when its connection broke and the session's process, on this
machine, is known to be gone (`attach`, `calu.tui.connection.Link`)."""

BROKEN_CODES = frozenset({None, 1006, 1011})
"""Close codes of a connection that broke rather than being closed by the session: none (no
close frame), 1006 (closed abnormally) and 1011 (the keepalive ping got no answer)."""


def closer(close_code: int | None) -> str:
    """Name the side that closed a connection, as far as its close code tells.

    A relay of `calu serve` passes the session's close code and reason on unchanged
    (agent/records/PLAN.md D242 (6)); its own close for a server token that is no longer
    valid has a code of its own, `CLOSE_SERVER_TOKEN_REVOKED`, which no session sends.

    Args:
        close_code: The WebSocket close code; None for a connection without a close frame.

    Returns:
        "the relay of the `calu serve`" for `CLOSE_SERVER_TOKEN_REVOKED`; "the session"
        otherwise.
    """
    if close_code == CLOSE_SERVER_TOKEN_REVOKED:
        return "the relay of the `calu serve`"
    return "the session"


def unreachable(error: OSError) -> str:
    """Say in words why an address could not be reached.

    Args:
        error: What the attempt to connect raised.

    Returns:
        A short sentence: "nothing listens at the address" for a refused connection, "no
        answer" for a timeout, else the error's own text.
    """
    if isinstance(error, ConnectionRefusedError):
        return "nothing listens at the address (connection refused)"
    if isinstance(error, TimeoutError):
        return "no answer from the address (timed out)"
    return str(error) or type(error).__name__


def print_snapshot(snapshot: dict[str, Any]) -> None:
    """Print the state line of a `state.get` result and one line per open escalation.

    Args:
        snapshot: The snapshot as the session returned it.
    """
    say(describe_state(snapshot), flush=True)
    for escalation in snapshot.get("escalations") or ():
        say(
            f"*** escalation {escalation['escalation_id']} waits: {escalation['reason']}",
            flush=True,
        )
