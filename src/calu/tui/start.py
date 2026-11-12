"""Starting the full-screen client: whether it can run here, and running it.

This module imports nothing from Textual at import time, so `calu.cli` can import it in an
installation without the extra `tui` and ask `available()`.
"""

import importlib.util
import os
import sys
from dataclasses import dataclass

NOT_INSTALLED = "full-screen client not installed (pip install 'calu[tui]'); using the line client"
"""What `calu attach` says on stderr when Textual is missing."""


@dataclass
class TuiExit:
    """What the full-screen client knew when it ended, for the lines printed after it.

    The client draws on the terminal's alternate screen, so nothing of it stays visible;
    `calu attach` prints these facts instead.
    """

    code: int
    """0 after a detach by the user; 1 if the session closed the connection for good."""
    state: str
    """The last known state of the session."""
    claude_session_id: str | None
    open_decisions: int
    """How many steps or questions waited for a decision."""
    message: str = ""
    """Why the connection ended, if the session closed it."""


def available() -> bool:
    """Tell whether the full-screen client can run in this installation.

    Returns:
        True if Textual is installed (the extra `tui`).
    """
    return importlib.util.find_spec("textual") is not None


def wants_full_screen(plain: bool) -> bool:
    """Decide which client `calu attach` starts.

    The line client is used, in this order: with `--plain`; when standard input or output is
    not a terminal (pipes and scripts therefore see the line client unchanged); with
    `TERM=dumb` or `CALU_ATTACH=plain` in the environment; and when Textual is not installed,
    after one line on stderr that says so.

    Args:
        plain: Whether `--plain` was given.

    Returns:
        True for the full-screen client.
    """
    if plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.environ.get("TERM") == "dumb" or os.environ.get("CALU_ATTACH") == "plain":
        return False
    if not available():
        print(NOT_INSTALLED, file=sys.stderr)
        return False
    return True


def exit_lines(ended: TuiExit) -> list[str]:
    """Say what the terminal should show after the full-screen client has gone.

    Args:
        ended: What the client knew when it ended.

    Returns:
        The lines: why the connection ended (if the session closed it), the session and its
        state, how many decisions wait, and how to attach again.
    """
    lines = []
    if ended.message:
        lines.append(f"the session closed the connection: {ended.message}")
    else:
        lines.append("detached; the session keeps running")
    session = ended.claude_session_id or "no Claude session yet"
    lines.append(f"session {session}: {ended.state}")
    if ended.open_decisions:
        count = ended.open_decisions
        what = "1 step waits" if count == 1 else f"{count} steps wait"
        lines.append(f"{what} for a decision")
    lines.append("attach again with: calu attach   (the line client: calu attach --plain)")
    return lines


def run_tui(address: str, token: str, after_seq: int = 0, show_thinking: bool = True) -> TuiExit:
    """Run the full-screen client until the user detaches or the session closes the connection.

    Blocks, and takes over the terminal while it runs. The session keeps running when the
    client ends.

    Args:
        address: The address `calu run` printed (`ws://host:port` or `unix:<path>`).
        token: The client token.
        after_seq: The first subscription continues after this sequence number.
        show_thinking: Whether thinking is shown in full at the start.

    Returns:
        What the client knew when it ended.

    Raises:
        OSError: If the address cannot be reached.
        calu.protocol.client.RequestFailed: If the session refuses the token.
    """
    from calu.tui.app import CaluApp, TuiOptions
    from calu.tui.connection import protocol_connector
    from calu.tui.theme import DARK

    options = TuiOptions(
        after_seq=after_seq,
        show_thinking=show_thinking,
        theme=os.environ.get("CALU_TUI_THEME") or DARK,
    )
    app = CaluApp(protocol_connector(address, token), options)
    app.run()
    if app.link.error is not None:
        raise app.link.error
    model = app.model
    return TuiExit(
        code=1 if app.link.closed_reason else 0,
        state=model.state,
        claude_session_id=model.claude_session_id,
        open_decisions=len(model.decisions),
        message=app.link.closed_reason,
    )
