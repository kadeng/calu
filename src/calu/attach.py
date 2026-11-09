"""`calu attach`: a terminal client for a running session (PLAN.md phase 4).

    calu attach ws://127.0.0.1:8765 --token <token>

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
    /allow <id> [note]    resolve an escalation (`escalation.decide`)
    /deny <id> [note]     resolve an escalation
    /inject <text>        send the text so Claude reads it after its running tool calls
    /state                print the state again
    /context              print the conversation as the checkers see it
    /sessions             list the stored sessions of this project
    /shutdown             stop the session and end the calu process

The events are rendered by `calu.run.format_event`, so an attached terminal shows the same
lines as the terminal that started the session.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from calu.protocol.client import ProtocolClient, RequestFailed, connect
from calu.protocol.messages import EVENTS, Event
from calu.run import format_event, one_line, read_lines

log = logging.getLogger(__name__)

EVENT_TYPES: dict[str, type[Event]] = {event.method: event for event in EVENTS}
"""Event class per method, to turn the received params back into an event for rendering."""

HELP = (
    "/quit  /stop  /start [resume id]  /interrupt  /allow <id> [note]  /deny <id> [note]  "
    "/inject <text>  /to <agent> <text>  /comment <path> <from> <to> <text>  /state  /context  "
    "/sessions  /shutdown"
)
"""One line listing the commands, printed on connect and for an unknown command."""


def render(method: str, params: dict[str, Any]) -> str | None:
    """Render one received event as a terminal line.

    Args:
        method: The notification method.
        params: Its params.

    Returns:
        The line, or None for events the terminal does not show. An event that cannot be
        turned back into its model (an older or newer session) is shown as its raw params.
    """
    model = EVENT_TYPES.get(method)
    if model is None:
        return f"[{method}] {params}"
    try:
        return format_event(model.model_validate(params))
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
            print(line, flush=True)


def describe_state(snapshot: dict[str, Any]) -> str:
    """Return a one-line description of a `state.get` result.

    Args:
        snapshot: The snapshot as the session returned it.

    Returns:
        State, workspace, model and the number of open escalations.
    """
    waiting = len(snapshot.get("escalations") or ())
    return (
        f"[state] {snapshot.get('state')} · {snapshot.get('workspace')} · "
        f"model {snapshot.get('model')}" + (f" · {waiting} escalation(s) open" if waiting else "")
    )


class Console:
    """Turns the lines a user types into requests to the session."""

    def __init__(self, client: ProtocolClient, workspace: Path | None = None) -> None:
        """Create the console for a connected client.

        Args:
            client: The connected protocol client.
            workspace: The session's project directory, which `/comment` reads files from;
                None refuses `/comment`.
        """
        self.client = client
        self.workspace = workspace
        self.quit = anyio.Event()

    async def line(self, text: str) -> None:
        """Handle one typed line: a command, or a chat message. Never raises for a failed request.

        Args:
            text: The line without its line end.
        """
        try:
            await self._line(text)
        except RequestFailed as exc:
            print(f"[error] {exc}", flush=True)
        except ConnectionError as exc:  # before OSError, of which it is a subclass
            print(f"[error] {exc}", flush=True)
            self.quit.set()
        except OSError as exc:  # /comment could not read the file
            print(f"[error] {exc}", flush=True)

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
                print(HELP, flush=True)
            case "/stop":
                await self.client.request("session.stop")
            case "/start":
                result = await self.client.request(
                    "session.start", {"resume_id": rest} if rest else {}
                )
                print(f"[started] {result}", flush=True)
            case "/interrupt":
                await self.client.request("interrupt")
            case "/inject":
                if not rest:
                    print("[error] /inject needs a text", flush=True)
                    return
                await self.client.request("chat.send", {"text": rest, "delivery": "inject"})
            case "/to":
                target, _, message = rest.partition(" ")
                if not target or not message.strip():
                    print("[error] /to needs a subagent id and a text", flush=True)
                    return
                await self.client.request(
                    "chat.send",
                    {"text": message.strip(), "to": target, "delivery": "inject"},
                )
            case "/comment":
                await self._comment(rest)
            case "/allow" | "/deny":
                await self._decide(command.removeprefix("/"), rest)
            case "/state":
                print(describe_state(await self.client.request("state.get")), flush=True)
            case "/context":
                context = await self.client.request("context.get")
                print(context.get("chat_summary", ""), flush=True)
                print(context.get("recent", ""), flush=True)
            case "/sessions":
                result = await self.client.request("session.list", {})
                for session in result.get("sessions", []):
                    print(
                        f"{session['claude_session_id'] or '(no id)'}  {session['state']}  "
                        f"{session['started_at']}  {session['first_input']}",
                        flush=True,
                    )
            case "/shutdown":
                await self.client.request("shutdown")
                self.quit.set()
            case _:
                print(f"[error] unknown command {command}. {HELP}", flush=True)

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
            print("[error] /comment needs a path, a first and last line and a text", flush=True)
            return
        path, first, last, text = parts
        if self.workspace is None:
            print("[error] the session did not say where its project is", flush=True)
            return
        try:
            start, end = int(first), int(last)
        except ValueError:
            print(f"[error] not line numbers: {first!r} {last!r}", flush=True)
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
        print(f"[comment] on {path}:{start}-{end}: {one_line(excerpt)}", flush=True)

    async def _decide(self, decision: str, rest: str) -> None:
        """Resolve an escalation from an `/allow` or `/deny` line.

        Args:
            decision: "allow" or "deny".
            rest: The escalation id and an optional note.

        Raises:
            RequestFailed: If the session refused the decision (e.g. unknown escalation).
        """
        escalation_id, _, note = rest.partition(" ")
        if not escalation_id:
            print(f"[error] /{decision} needs an escalation id", flush=True)
            return
        await self.client.request(
            "escalation.decide",
            {
                "escalation_id": escalation_id,
                "decision": decision,
                "note": note.strip() or None,
            },
        )


async def attach(address: str, token: str, after_seq: int = 0, show_thinking: bool = False) -> int:
    """Attach to a running session until the user detaches or the session closes.

    Side effects: connects to `address`, prints the state and every event, and reads lines
    from stdin as chat messages or commands.

    Args:
        address: The WebSocket address that `calu run` printed.
        token: The client token that `calu run` printed.
        after_seq: Sequence number to continue after (0 shows what the buffer still holds).
        show_thinking: Also print Claude's thinking.

    Returns:
        0 when the user detached or the session closed the connection.

    Raises:
        OSError: If the address cannot be reached.
        RequestFailed: If the token is refused.
    """
    printer = Printer(show_thinking)
    async with connect(address, token, "calu attach", printer, after_seq=after_seq) as client:
        snapshot = await client.request("state.get")
        print(describe_state(snapshot), flush=True)
        for escalation in snapshot.get("escalations") or ():
            print(
                f"*** escalation {escalation['escalation_id']} waits: {escalation['reason']}",
                flush=True,
            )
        print(f"[attached] {HELP}", flush=True)
        directory = snapshot.get("workspace")
        console = Console(client, Path(directory) if isinstance(directory, str) else None)
        async with anyio.create_task_group() as tg:

            async def input_lines() -> None:
                """Read typed lines until the user detaches or stdin ends."""
                await read_lines(console.line)  # returns at end of input or on /quit
                console.quit.set()

            async def until_closed() -> None:
                """Detach when the session closes the connection."""
                await client.closed.wait()
                print("[detached] the session closed the connection", flush=True)
                console.quit.set()

            tg.start_soon(input_lines)
            tg.start_soon(until_closed)
            await console.quit.wait()
            tg.cancel_scope.cancel()
    return 0
