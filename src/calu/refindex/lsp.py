"""The fast reference index: it asks a language server.

A language server is the program an editor uses for "find all references". It understands
one language well, including the types of variables, so its answers are exact. This module
starts such a server and asks it which code refers to a given function, method or class.
The default server is basedpyright, for Python.

How it works:

1. It starts the server as a child process and talks to it over its standard input and
   output, in the Language Server Protocol.
2. It waits until the server says that it has read the project (`LspIndex.ready_log`).
3. For a question it asks the server for the references of the unit.
4. It also asks for the references of every base-class or Protocol method that the unit
   implements. The server files a call like `store.save()` under the base method when
   `store` is only known by its base type, so those calls would otherwise be missed.

Two things were measured with basedpyright 1.40.2 on calu's own code and shaped this module
(`spikes/RESULTS.md`, "The reference index"):

- A server that has not finished reading the project answers with an empty list, and no
  error. The same happens for about two seconds after it was told about a new file. An
  empty list could be mistaken for "nothing refers to this". So this module marks an answer
  as not complete when the server never reported that it was ready, and it waits `SETTLE_S`
  after `changed`.
- The server never says "I could not work this out". A call on a variable of unknown type,
  a name inside a string, and code in another language are simply absent from its answer.
  The word search (`calu.refindex.search`) is the index that misses nothing.

Safety: the server reads files that the supervised coding agent writes. This module runs
the command it is given and nothing more. Whoever creates an `LspIndex` decides which user
and which sandbox the server runs in.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import anyio

from calu.refindex.model import PLACE_TEXT_CHARS, Place, Referrers, Resolution, Unit

BASEDPYRIGHT = ("basedpyright-langserver", "--stdio")
"""The command of the Python language server the index was measured with."""

BASEDPYRIGHT_READY = r"Found \d+ source files?"
"""The log message with which basedpyright and pyright say that they have read the project."""

START_TIMEOUT_S = 60.0
"""Longest wait for the server's answer to `initialize` and for its ready message."""

REQUEST_TIMEOUT_S = 30.0
"""Longest wait for the answer to one request."""

SETTLE_S = 2.0
"""How long the server is left alone after files were announced as changed: it rebuilds its
file list in the background and reports no end of that (measured: a new file was seen 2 s
after the notification, not at once)."""

METHOD_KINDS = frozenset({6, 12})
"""`SymbolKind` Method and Function of the protocol."""


class LspError(Exception):
    """The language server did not answer a request, or answered with an error."""


@dataclass
class LspClient:
    """One language server process and the JSON-RPC conversation with it.

    A thread reads the server's messages. Requests of the server to the client are answered
    with empty results (`workspace/configuration`: one null per item), so that the server
    does not wait for an editor.
    """

    command: tuple[str, ...]
    root: Path
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)
    _answers: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _changed: threading.Condition = field(default_factory=threading.Condition, init=False)
    _logs: list[str] = field(default_factory=list, init=False, repr=False)
    _ended: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        """Start the server process and the thread that reads its messages.

        Side effects: starts `command` in `root` with its error output discarded.

        Raises:
            LspError: If the command cannot be started.
        """
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LspError(f"{self.command[0]} could not be started ({exc}).") from exc
        threading.Thread(target=self._read, daemon=True).start()

    def close(self) -> None:
        """End the server process: `shutdown`, `exit`, then kill it if it is still there.

        Side effects: ends the child process. Never raises.
        """
        process = self._process
        if process is None:
            return
        self._process = None
        try:
            self.request("shutdown", None, timeout=3.0, process=process)
            self._send({"jsonrpc": "2.0", "method": "exit", "params": None}, process)
            process.wait(timeout=3.0)
        except LspError, OSError, subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def request(
        self,
        method: str,
        params: Any,
        timeout: float = REQUEST_TIMEOUT_S,
        process: subprocess.Popen[bytes] | None = None,
    ) -> Any:
        """Send one request and wait for its answer.

        Args:
            method: The protocol method.
            params: Its parameters.
            timeout: Longest wait in seconds.
            process: The process to write to; None for the running one.

        Returns:
            The `result` of the answer.

        Raises:
            LspError: If the server is not running, ends, answers with an error or does not
                answer in time.
        """
        process = process or self._process
        if process is None:
            raise LspError("The language server is not running.")
        with self._changed:
            self._next_id += 1
            number = self._next_id
        self._send({"jsonrpc": "2.0", "id": number, "method": method, "params": params}, process)
        end = time.monotonic() + timeout
        with self._changed:
            while number not in self._answers:
                left = end - time.monotonic()
                if self._ended or left <= 0:
                    why = "ended" if self._ended else "did not answer in time"
                    raise LspError(f"The language server {why} ({method}).")
                self._changed.wait(left)
            answer = self._answers.pop(number)
        if "error" in answer:
            raise LspError(f"{method}: {json.dumps(answer['error'])[:300]}")
        return answer.get("result")

    def notify(self, method: str, params: Any) -> None:
        """Send one notification.

        Args:
            method: The protocol method.
            params: Its parameters.

        Raises:
            LspError: If the server is not running or cannot be written to.
        """
        if self._process is None:
            raise LspError("The language server is not running.")
        self._send({"jsonrpc": "2.0", "method": method, "params": params}, self._process)

    def wait_for_log(self, pattern: str, timeout: float) -> bool:
        """Wait until the server has logged a message that matches `pattern`.

        Args:
            pattern: A regular expression, searched in each `window/logMessage`.
            timeout: Longest wait in seconds.

        Returns:
            True if such a message came, also before the call; False if the time ran out or
            the server ended.
        """
        wanted = re.compile(pattern)
        end = time.monotonic() + timeout
        with self._changed:
            while not any(wanted.search(text) for text in self._logs):
                left = end - time.monotonic()
                if self._ended or left <= 0:
                    return False
                self._changed.wait(left)
        return True

    def _send(self, message: dict[str, Any], process: subprocess.Popen[bytes]) -> None:
        """Write one framed message to the server.

        Args:
            message: The JSON-RPC message.
            process: The server process.

        Raises:
            LspError: If the server's input is closed.
        """
        body = json.dumps(message).encode()
        try:
            assert process.stdin is not None
            process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LspError(f"The language server cannot be written to ({exc}).") from exc

    def _read(self) -> None:
        """Read the server's messages until its output ends (the reader thread).

        Side effects: stores answers and log messages, answers the server's own requests,
        and sets `_ended` at the end of the output or at a message that cannot be read.
        """
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                length = 0
                while line := process.stdout.readline().strip():
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if not length:
                    break
                message = json.loads(process.stdout.read(length))
                if "method" in message and "id" in message:
                    items = (message.get("params") or {}).get("items") or []
                    result = [None] * len(items) if items else None
                    self._send({"jsonrpc": "2.0", "id": message["id"], "result": result}, process)
                elif message.get("method") == "window/logMessage":
                    with self._changed:
                        self._logs.append(str(message["params"].get("message", "")))
                        self._changed.notify_all()
                elif "id" in message:
                    with self._changed:
                        self._answers[message["id"]] = message
                        self._changed.notify_all()
        except OSError, ValueError, LspError:
            pass
        with self._changed:
            self._ended = True
            self._changed.notify_all()


@dataclass
class LspIndex:
    """A reference index that asks a language server (see the module docstring).

    The server is started with the first question and kept until `close`. Use it as a context
    manager, or call `close`.
    """

    root: Path
    """The project directory."""
    command: tuple[str, ...] = BASEDPYRIGHT
    """The server's command line; it must speak the protocol on stdin and stdout."""
    ready_log: str = BASEDPYRIGHT_READY
    """A regular expression for the log message with which the server says that it has read
    the project; "" for a server without such a message, whose answers are then never called
    complete."""
    language: str = "python"
    """The protocol's language id of the files the server reads."""
    suffixes: tuple[str, ...] = (".py", ".pyi")
    """The file names the server covers; a place in another file is never resolved."""
    _client: LspClient | None = field(default=None, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)
    _quiet_until: float = field(default=0.0, init=False, repr=False)
    _problem: str = field(default="", init=False, repr=False)
    _one_at_a_time: anyio.Lock = field(default_factory=anyio.Lock, init=False, repr=False)

    def __enter__(self) -> LspIndex:
        """Enter the context; the server starts with the first question.

        Returns:
            The index.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        """End the server process.

        Args:
            *_exc: The exception of the block, unused.
        """
        self.close()

    def close(self) -> None:
        """End the server process, if one runs. Never raises."""
        if self._client is not None:
            self._client.close()
        self._client = None
        self._ready = False

    def changed(self, paths: list[str]) -> None:
        """Tell the server that files were written, created or deleted.

        Side effects: sends `workspace/didChangeWatchedFiles` if the server runs, and makes
        the next question wait until `SETTLE_S` after now. Never raises: a server that
        cannot be told is started again with the next question.

        Args:
            paths: The files, relative to the project directory.
        """
        client = self._client
        if client is None or not paths:
            return
        changes = [
            {"uri": (self.root / name).as_uri(), "type": 2 if (self.root / name).exists() else 3}
            for name in paths
        ]
        try:
            client.notify("workspace/didChangeWatchedFiles", {"changes": changes})
        except LspError:
            self.close()
        self._quiet_until = time.monotonic() + SETTLE_S

    async def referrers(self, unit: Unit) -> Referrers:
        """Ask the server for the places that reference `unit`, in a worker thread.

        Side effects: those of `find`. One question at a time: the conversation with the
        server is not shared between threads.

        Args:
            unit: The unit.

        Returns:
            As `find`.
        """
        async with self._one_at_a_time:
            return await anyio.to_thread.run_sync(self.find, unit)

    def find(self, unit: Unit) -> Referrers:
        """Ask the server for the places that reference `unit`; blocks while it waits.

        Side effects: starts the server if it does not run, waits for it to be ready and for
        `SETTLE_S` after the last `changed`, opens the unit's file in the server with its
        present text, and reads files of the project for the text of each place.

        Args:
            unit: The unit; its `line` must be known.

        Returns:
            The places the server reports as references, also through a base class or
            Protocol method that the unit implements; none unclear. Not `complete`, with the
            reason, when the server is not installed, did not start, did not report ready,
            failed, or the unit's name is not on its line.
        """
        try:
            client = self._started()
            wait = self._quiet_until - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            column = self._column(unit)
            found = self._references(client, unit.path, unit.line, column)
            for path, line, col in self._bases(client, unit, column):
                found |= self._references(client, path, line, col)
        except LspError as exc:
            return Referrers(complete=False, problem=str(exc))
        found.discard((unit.path, unit.line, column))
        places = tuple(self._place(*spot) for spot in sorted(found))
        if not self._ready:
            return Referrers(referrers=places, complete=False, problem=self._problem)
        return Referrers(referrers=places)

    async def resolve(self, place: Place, unit: Unit) -> Resolution:
        """Say what the name at `place` resolves to, in a worker thread.

        Side effects: those of `resolve_blocking`. One question at a time.

        Args:
            place: An occurrence of the unit's name.
            unit: The unit that was asked about.

        Returns:
            As `resolve_blocking`.
        """
        async with self._one_at_a_time:
            return await anyio.to_thread.run_sync(self.resolve_blocking, place, unit)

    def resolve_blocking(self, place: Place, unit: Unit) -> Resolution:
        """Ask the server where the name at `place` is defined, and compare with `unit`.

        Side effects: starts the server if it does not run, and sends the file's present
        text to it (`textDocument/didOpen`, then `didClose`).

        Args:
            place: An occurrence of the unit's name.
            unit: The unit that was asked about; its `line` must be known.

        Returns:
            `UNIT` if a definition the server names is the unit's; `OTHER` if it names
            definitions and none is the unit's (also one outside the project); `UNKNOWN` if
            it names none, the file is not one the server covers, the server is not ready
            or fails. A call through a base class resolves to the base method and is
            therefore `OTHER` here: `find` is what reports those calls.
        """
        if not place.path.endswith(self.suffixes):
            return Resolution.UNKNOWN
        try:
            client = self._started()
            if not self._ready:
                return Resolution.UNKNOWN
            uri = (self.root / place.path).as_uri()
            text = (self.root / place.path).read_text("utf-8", errors="replace")
            document = {"uri": uri, "languageId": self.language, "version": 1, "text": text}
            client.notify("textDocument/didOpen", {"textDocument": document})
            try:
                answer = client.request(
                    "textDocument/definition",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": place.line - 1, "character": place.column},
                    },
                )
            finally:
                client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        except LspError, OSError:
            return Resolution.UNKNOWN
        items = answer if isinstance(answer, list) else [answer] if answer else []
        if not items:
            return Resolution.UNKNOWN
        for item in items:
            spot = self._spot(item)
            if spot is not None and spot[:2] == (unit.path, unit.line):
                return Resolution.UNIT
        return Resolution.OTHER

    def _started(self) -> LspClient:
        """Return the running server, starting and initializing it first if needed.

        Side effects: starts the process, sends `initialize` and `initialized`, waits for the
        ready message; sets `_ready` and `_problem`.

        Returns:
            The client.

        Raises:
            LspError: If the server is not installed, cannot be started or does not answer
                `initialize`.
        """
        if self._client is not None:
            return self._client
        if shutil.which(self.command[0]) is None:
            raise LspError(f"No fast index: {self.command[0]} is not installed.")
        client = LspClient(self.command, self.root)
        client.start()
        try:
            client.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                    "capabilities": {
                        "textDocument": {
                            "references": {},
                            "implementation": {},
                            "definition": {},
                        },
                        "workspace": {"symbol": {}, "workspaceFolders": True},
                    },
                },
                timeout=START_TIMEOUT_S,
            )
            client.notify("initialized", {})
        except LspError:
            client.close()
            raise
        self._client = client
        self._ready = bool(self.ready_log) and client.wait_for_log(self.ready_log, START_TIMEOUT_S)
        self._problem = (
            ""
            if self._ready
            else "The language server did not report that it has read the project, so "
            "references may be missing."
        )
        return client

    def _column(self, unit: Unit) -> int:
        """Find the column of the unit's name on the line of its definition.

        Args:
            unit: The unit.

        Returns:
            The 0-based column.

        Raises:
            LspError: If the file cannot be read or the name is not on that line.
        """
        try:
            lines = (self.root / unit.path).read_text("utf-8", errors="replace").splitlines()
            found = re.search(rf"(?<![\w]){re.escape(unit.name)}(?![\w])", lines[unit.line - 1])
        except OSError, IndexError:
            found = None
        if found is None or unit.line < 1:
            raise LspError(f"{unit.name} is not on line {unit.line} of {unit.path}.")
        return found.start()

    def _references(
        self, client: LspClient, path: str, line: int, column: int
    ) -> set[tuple[str, int, int]]:
        """Ask for the references of the name at one position.

        Side effects: sends the file's present text to the server (`textDocument/didOpen`,
        then `didClose`), so that the answer is about the file as it is now.

        Args:
            client: The server.
            path: The file, relative to the project directory.
            line: The 1-based line.
            column: The 0-based column.

        Returns:
            (path, 1-based line, 0-based column) of each reference inside the project.

        Raises:
            LspError: If the file cannot be read or the server fails.
        """
        uri = (self.root / path).as_uri()
        try:
            text = (self.root / path).read_text("utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"{path} cannot be read ({exc}).") from exc
        document = {"uri": uri, "languageId": self.language, "version": 1, "text": text}
        client.notify("textDocument/didOpen", {"textDocument": document})
        try:
            answer = client.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line - 1, "character": column},
                    "context": {"includeDeclaration": False},
                },
            )
        finally:
            client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        return {spot for item in answer or [] if (spot := self._spot(item)) is not None}

    def _bases(self, client: LspClient, unit: Unit, column: int) -> list[tuple[str, int, int]]:
        """Find the base-class and Protocol methods that the unit implements.

        The server counts a call through a base class or a Protocol as a reference of the
        base method. So every other method or function of the same bare name is asked for
        its implementations, and one that names the unit is such a base.

        Args:
            client: The server.
            unit: The unit.
            column: The column of the unit's name on its line.

        Returns:
            (path, 1-based line, 0-based column) of each such base method.

        Raises:
            LspError: If the server fails.
        """
        own = (unit.path, unit.line, column)
        bases: list[tuple[str, int, int]] = []
        for symbol in client.request("workspace/symbol", {"query": unit.name}) or []:
            if symbol.get("name") != unit.name or symbol.get("kind") not in METHOD_KINDS:
                continue
            spot = self._spot(symbol.get("location") or {})
            if spot is None or spot == own:
                continue
            path, line, _ = spot
            try:
                at = self._column(Unit(path, unit.name, line))
            except LspError:
                continue
            answer = client.request(
                "textDocument/implementation",
                {
                    "textDocument": {"uri": (self.root / path).as_uri()},
                    "position": {"line": line - 1, "character": at},
                },
            )
            if any(self._spot(item) == own for item in answer or []):
                bases.append((path, line, at))
        return bases

    def _spot(self, location: dict[str, Any]) -> tuple[str, int, int] | None:
        """Turn a protocol location into a place inside the project.

        Args:
            location: A `Location` or `LocationLink` of the protocol.

        Returns:
            (path relative to the project directory, 1-based line, 0-based column); None for
            a location outside the project or without a file URI.
        """
        uri = location.get("uri") or location.get("targetUri") or ""
        span = location.get("range") or location.get("targetSelectionRange") or {}
        parsed = urlparse(uri)
        if parsed.scheme != "file" or "start" not in span:
            return None
        try:
            path = Path(unquote(parsed.path)).resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return str(path), int(span["start"]["line"]) + 1, int(span["start"]["character"])

    def _place(self, path: str, line: int, column: int) -> Place:
        """Build the place of one reference, with the text of its line.

        Args:
            path: The file, relative to the project directory.
            line: The 1-based line.
            column: The 0-based column.

        Returns:
            The place; its text is "" if the file or the line cannot be read.
        """
        try:
            lines = (self.root / path).read_text("utf-8", errors="replace").splitlines()
            text = lines[line - 1].strip()[:PLACE_TEXT_CHARS]
        except OSError, IndexError:
            text = ""
        return Place(path, line, column, text)
