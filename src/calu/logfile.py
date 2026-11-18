"""Where `calu run` and `calu serve` write their log, and how errors reach stderr (D225).

    log record ──► the log file   every record at INFO or above, with full tracebacks
               └─► stderr         WARNING and above only, as one line without a traceback;
                                  a line whose record has a traceback names the log file

    $XDG_STATE_HOME/calu/log/<command>.log    (~/.local/state/calu/log/<command>.log, or
                                               /var/lib/calu/log/<command>.log as root)

The directory is created with mode 0700 and the file with mode 0600. Both lie in calu's state
directory, which Claude Code's sandbox may not read (`calu.paths.calu_private_dirs`). The file
is opened before the process switches its file access to another user (`calu.asuser.enter`)
and stays open, so the records of a session that runs as that user still reach a file only root
can read. The file is appended to and never rotated or truncated by calu.

At import time this module imports nothing from calu, so that `calu.cli` can set the log up
before it imports the rest.
"""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

LINE_LIMIT = 300
"""Most characters of the message part of a line on stderr."""

CALU_HANDLER = "_calu_logfile"
"""Attribute that marks the handlers `setup` added, so that a second call replaces them."""


def log_path(command: str, environ: Mapping[str, str] | None = None) -> Path:
    """Return the log file of one calu command.

    Args:
        command: The command's name, for example "run" or "serve".
        environ: The environment to read `XDG_STATE_HOME` from; None uses `os.environ`.

    Returns:
        `$XDG_STATE_HOME/calu/log/<command>.log`, or the same below `~/.local/state` when the
        variable is not set. The path is not created.
    """
    env = os.environ if environ is None else environ
    state = (
        Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else Path.home() / ".local/state"
    )
    return Path(os.path.abspath(state / "calu" / "log" / f"{command}.log"))


def innermost(exc: BaseException) -> BaseException:
    """Return the exception a person needs to read, out of nested exception groups.

    Args:
        exc: The exception that was raised.

    Returns:
        `exc` itself, or, for an exception group, the first exception in it that is not a
        group (a task group wraps the one error that ended it in a group).
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def one_line(text: str, limit: int | None = LINE_LIMIT) -> str:
    """Return `text` on one line, cut to `limit` characters.

    Args:
        text: The text; line ends and runs of spaces become one space each.
        limit: Most characters of the result before " [...]" is appended; None cuts nothing
            (a message calu wrote itself, whose end may be the command to run next).

    Returns:
        The single line.
    """
    joined = " ".join(text.split())
    return joined if limit is None or len(joined) <= limit else joined[:limit] + " [...]"


def describe(exc: BaseException) -> str:
    """Put an exception into one line for stderr.

    Args:
        exc: The exception; an exception group is described by `innermost`.

    Returns:
        "<type name>: <message>" on one line, or the type name alone when the message is
        empty.
    """
    inner = innermost(exc)
    text = one_line(str(inner))
    return f"{type(inner).__name__}: {text}" if text else type(inner).__name__


class OneLineFormatter(logging.Formatter):
    """Formats a record as one line: "calu: <message>", never with a traceback.

    A record with an exception gets the exception in one line and the log file's path:
    "calu: <message> (<type>: <exception>; details in <path>)".
    """

    def __init__(self, path: Path | None) -> None:
        """Create the formatter.

        Args:
            path: The log file that holds the tracebacks, or None if there is none.
        """
        super().__init__()
        self.path = path

    def format(self, record: logging.LogRecord) -> str:
        """Render one record.

        Args:
            record: The record.

        Returns:
            The line, without a line end.
        """
        text = f"calu: {one_line(record.getMessage())}"
        if record.exc_info and record.exc_info[1] is not None:
            where = f"; details in {self.path}" if self.path is not None else ""
            text += f" ({describe(record.exc_info[1])}{where})"
        return text


def setup(command: str, stderr: TextIO | None = None) -> Path | None:
    """Send the process's log records to the command's log file and, in short, to stderr.

    Side effects: creates the log directory (mode 0700) and the file (mode 0600) if they are
    missing, opens the file for appending, adds to the root logger one handler for the file
    and one for stderr (replacing those of an earlier call; other handlers stay), sets the
    root logger's level to INFO, and routes Python's warnings into the log
    (`logging.captureWarnings`). If the file cannot be opened, prints one line saying so to
    stderr; the records then reach stderr only, still as one line each.

    Args:
        command: The command's name; it names the file (`log_path`).
        stderr: Where the short lines go; None uses `sys.stderr` at the time of the call.

    Returns:
        The log file's path, or None if it could not be opened.
    """
    stream = sys.stderr if stderr is None else stderr
    wanted = log_path(command)
    path: Path | None = None
    handlers: list[logging.Handler] = []
    try:
        wanted.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(wanted, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        to_file = logging.FileHandler(wanted, mode="a", encoding="utf-8")
    except OSError as exc:
        print(f"calu: no log file ({describe(exc)}); errors are shown here only", file=stream)
    else:
        path = wanted
        to_file.setLevel(logging.INFO)
        to_file.setFormatter(
            logging.Formatter("%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s")
        )
        handlers.append(to_file)
    short = logging.StreamHandler(stream)
    short.setLevel(logging.WARNING)
    short.setFormatter(OneLineFormatter(path))
    handlers.append(short)
    root = logging.getLogger()
    # Only the handlers an earlier call added are replaced: a test's capturing handler, or an
    # embedding program's own, stays.
    for old in [h for h in root.handlers if getattr(h, CALU_HANDLER, False)]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        setattr(handler, CALU_HANDLER, True)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    return path
