"""Starting the full-screen client: whether it can run here, and running it.

This module imports nothing from Textual at import time, so `calu.cli` can import it in an
installation whose Textual is missing or of another version and ask `available()`.
"""

import importlib.metadata
import importlib.util
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from calu.tui.app import TuiOptions

NOT_INSTALLED = (
    "the full-screen client's package textual is missing (install calu again: "
    "uv tool install --reinstall calu); using the line client"
)
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


SUPPORTED = ((8, 2), (9, 0))
"""The Textual versions the client is written for: from the first, below the second (the
range of `textual` in `pyproject.toml`)."""


def problem() -> str | None:
    """Say why the full-screen client cannot run in this installation.

    Returns:
        None if a supported Textual is installed; `NOT_INSTALLED` without Textual; a line
        that names the installed and the supported versions for a Textual outside
        `SUPPORTED` (one installed beside calu, not as calu's dependency).
    """
    if importlib.util.find_spec("textual") is None:
        return NOT_INSTALLED
    try:
        found = importlib.metadata.version("textual")
    except importlib.metadata.PackageNotFoundError:
        return NOT_INSTALLED
    numbers: list[int] = []
    for part in found.split(".")[:2]:
        digits = "".join(char for char in part if char.isdigit())
        numbers.append(int(digits) if digits else 0)
    low, high = SUPPORTED
    if not low <= tuple(numbers) < high:
        return (
            f"the full-screen client needs textual >= {low[0]}.{low[1]} and < {high[0]} "
            f"(found {found}; install calu again: uv tool install --reinstall calu); "
            "using the line client"
        )
    return None


def available() -> bool:
    """Tell whether the full-screen client can run in this installation.

    Returns:
        True if a supported Textual is installed.
    """
    return problem() is None


def wants_full_screen(plain: bool) -> bool:
    """Decide which client `calu attach` starts.

    The line client is used, in this order: with `--plain`; when standard input or output is
    not a terminal (pipes and scripts therefore see the line client unchanged); with
    `TERM=dumb` or `CALU_ATTACH=plain` in the environment; and when Textual is not installed
    or is a version the client is not written for, after one line on stderr that says so.

    Args:
        plain: Whether `--plain` was given.

    Returns:
        True for the full-screen client.
    """
    if plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.environ.get("TERM") == "dumb" or os.environ.get("CALU_ATTACH") == "plain":
        return False
    missing = problem()
    if missing is not None:
        print(missing, file=sys.stderr)
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


def options_from(config: Path, history: Path, environ: Mapping[str, str]) -> TuiOptions:
    """Build the client's options from the settings file and the environment.

    Args:
        config: The settings file (`tui.toml`); it does not have to exist.
        history: The file the sent texts are kept in.
        environ: The environment; `CALU_TUI_THEME` overrides the theme of the file.

    Returns:
        The options: theme, bell, toasts, thinking, panel width and keys from the file (its
        problems are kept for the client to show), and both paths, so that the client stores
        later choices and the sent texts.
    """
    from calu.tui import settings
    from calu.tui.app import TuiOptions
    from calu.tui.theme import DARK

    stored = settings.load(config)
    return TuiOptions(
        show_thinking=stored.thinking,
        theme=environ.get("CALU_TUI_THEME") or stored.theme or DARK,
        bell=stored.bell,
        toasts=stored.toasts,
        panel_width=stored.panel_width,
        git_tool=stored.git_tool,
        keys=stored.keys,
        settings_path=config,
        history_path=history,
        problems=stored.problems,
    )


def run_tui(address: str, token: str, after_seq: int = 0, show_thinking: bool = True) -> TuiExit:
    """Run the full-screen client until the user detaches or the session closes the connection.

    Blocks, and takes over the terminal while it runs. The session keeps running when the
    client ends. The settings come from `tui.toml` (`calu.tui.settings`); the environment
    variable `CALU_TUI_THEME` overrides the theme, and `NO_COLOR` draws without colours.

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
    from calu.tui import settings
    from calu.tui.app import CaluApp
    from calu.tui.connection import protocol_connector

    options = options_from(settings.config_path(), settings.history_path(), os.environ)
    options.after_seq = after_seq
    options.show_thinking = options.show_thinking and show_thinking
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
