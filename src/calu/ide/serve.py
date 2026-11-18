"""Entry point of `calu ide`: the bridge on this process's stdin and stdout.

stdin  ──► asyncio stream reader (no thread, so the process can end while the editor
           still holds the pipe open) ──► calu.ide.bridge.serve
stdout ◄── protocol lines only; `sys.stdout` points at stderr while the bridge runs, so
           a `print` anywhere in calu or in a library cannot break the JSON-RPC stream
stderr ◄── one line per warning or error (`calu.logfile`, set up by the CLI)
"""

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path
from typing import TextIO

import anyio

from calu.credentials import CredentialStore
from calu.ide.bridge import serve
from calu.protocol.messages import MAX_FRAME_BYTES

log = logging.getLogger(__name__)

LINE_LIMIT = 2 * MAX_FRAME_BYTES
"""The longest protocol line read from the editor: a `session.call` may carry a chat message
with images, which the session limits to `MAX_FRAME_BYTES`; the JSON-RPC envelope and the
escaping of a string inside a string need room on top."""


def _is_pipe(stream: TextIO) -> bool:
    """Tell whether a stream is a pipe or a socket, which the event loop can wait for.

    Args:
        stream: The stream.

    Returns:
        True for a pipe or a socket; False for anything else and for a stream without a
        file descriptor.
    """
    try:
        mode = os.fstat(stream.fileno()).st_mode
    except OSError, ValueError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


class _Lines:
    """Reads protocol lines from a text stream without blocking the event loop."""

    def __init__(self, stream: TextIO) -> None:
        """Create the reader; the stream is attached at the first read.

        Args:
            stream: The process's standard input.
        """
        self._stream = stream
        self._reader: asyncio.StreamReader | None = None
        self._threaded = False

    async def read(self) -> str:
        """Read one line.

        A pipe or a socket (what an editor gives a child process) is read through the event
        loop. Anything else (a terminal, a regular file, `/dev/null`) is read in a worker
        thread, because the event loop cannot wait for it.

        Returns:
            The line including its newline, or "" at end of input, and also for a line
            longer than `LINE_LIMIT`, after which the stream cannot be followed any more.
        """
        if self._reader is None and not self._threaded:
            self._threaded = not _is_pipe(self._stream)
        if self._reader is None and not self._threaded:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(limit=LINE_LIMIT)
            try:
                await loop.connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), self._stream
                )
                self._reader = reader
            except ValueError, OSError:
                self._threaded = True
        if self._reader is None:
            return await anyio.to_thread.run_sync(self._stream.readline)
        try:
            return (await self._reader.readline()).decode("utf-8", errors="replace")
        except ValueError:
            log.error("the editor sent a line longer than %d bytes; ending", LINE_LIMIT)
            return ""


async def serve_ide(workspace: Path, store: CredentialStore) -> int:
    """Serve an editor extension on stdin and stdout until it disconnects or shuts down.

    Side effects: reads stdin to its end; writes protocol lines to stdout; points
    `sys.stdout` at stderr for the duration; everything the editor's requests do
    (`calu.ide.bridge`).

    Args:
        workspace: The editor's workspace folder (the working directory of `calu ide`).
        store: The credential store.

    Returns:
        0 when the editor disconnected or sent `ide.shutdown`.
    """
    out = sys.stdout
    lines = _Lines(sys.stdin)

    async def write_line(line: str) -> None:
        """Write one protocol line to the real stdout and flush it.

        Args:
            line: The JSON message, without a newline.
        """

        def write() -> None:
            """Write and flush; in a worker thread, because a full pipe blocks."""
            out.write(line + "\n")
            out.flush()

        await anyio.to_thread.run_sync(write)

    sys.stdout = sys.stderr
    try:
        return await serve(lines.read, write_line, workspace, store)
    finally:
        sys.stdout = out
