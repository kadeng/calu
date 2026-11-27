"""A code navigator that asks a language server (`calu.refindex.navigator.CodeNavigator`).

A language server is the program an editor uses for "find all references" and "go to
definition". It understands one language well, including the types of variables. The
default here is basedpyright, for Python.

`LspNavigator` starts the server as a child process and talks to it in the Language Server
Protocol over its standard input and output. It answers the navigator's questions with these
requests:

| Question | Request of the protocol |
|---|---|
| `references` | `textDocument/references`, without the declaration |
| `definitions` | `textDocument/definition` |
| `implementations` | `textDocument/implementation` |
| `symbols` | `workspace/symbol`, keeping functions and methods of exactly that name |
| `changed` | `workspace/didChangeWatchedFiles` |

Before each question about a file the server gets the file's present text
(`textDocument/didOpen`, and `didClose` after the answer), so the answer is about the file as
it is on disk now, also when it changed since the server read it.

Two things were measured with basedpyright 1.40.2 on calu's own code and shaped this module
(`spikes/RESULTS.md`, "The reference index"):

- A server that has not finished reading the project answers with an empty list and no
  error. So the navigator is only `ready` after the server's log message that it has read
  the project (`ready_log`).
- For about two seconds after it was told about a new file, the server still answers without
  it. So after `changed` the next question waits `SETTLE_S`.

`lsp_index` builds the reference index on this navigator (`NavigatorIndex`).

Safety: the server reads files that the supervised coding agent writes. This module runs the
command it is given and nothing more. Whoever creates the navigator decides which user and
which sandbox the server runs in.
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

from calu.refindex.navigator import NavigatorError, NavigatorIndex, Position, name_column

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


class LspError(NavigatorError):
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
class LspNavigator:
    """A code navigator on a language server (see the module docstring).

    The server is started by `start` and kept until `close`. The methods block; they must not
    be called from two threads at once (`NavigatorIndex` calls them one at a time).
    """

    root: Path
    """The project directory."""
    command: tuple[str, ...] = BASEDPYRIGHT
    """The server's command line; it must speak the protocol on stdin and stdout."""
    ready_log: str = BASEDPYRIGHT_READY
    """A regular expression for the log message with which the server says that it has read
    the project. "" for a server without such a message: it is then never `ready`."""
    language: str = "python"
    """The protocol's language id of the files the server reads."""
    suffixes: tuple[str, ...] = (".py", ".pyi")
    """The endings of the file names the server understands."""
    _client: LspClient | None = field(default=None, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)
    _quiet_until: float = field(default=0.0, init=False, repr=False)

    @property
    def ready(self) -> bool:
        """Say whether the server runs and has reported that it has read the project.

        Returns:
            True when both hold.
        """
        return self._client is not None and self._ready

    def covers(self, path: str) -> bool:
        """Say whether the server understands a file, by the ending of its name.

        Args:
            path: The file, relative to the project directory.

        Returns:
            True if the name ends with one of `suffixes`.
        """
        return path.endswith(self.suffixes)

    def start(self) -> None:
        """Start and initialize the server if it does not run, then wait for it to be ready.

        Side effects: starts the process, sends `initialize` and `initialized`, waits up to
        `START_TIMEOUT_S` for the ready message. A server that does not send it keeps
        running and is not `ready`. Also waits until `SETTLE_S` after the last `changed`.

        Raises:
            LspError: If the server is not installed, cannot be started or does not answer
                `initialize`.
        """
        if self._client is None:
            if shutil.which(self.command[0]) is None:
                raise LspError(f"The language server {self.command[0]} is not installed.")
            client = LspClient(self.command, self.root)
            client.start()
            try:
                client.request("initialize", self._initialize(), timeout=START_TIMEOUT_S)
                client.notify("initialized", {})
            except LspError:
                client.close()
                raise
            self._client = client
            self._ready = bool(self.ready_log) and client.wait_for_log(
                self.ready_log, START_TIMEOUT_S
            )
        wait = self._quiet_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def references(self, at: Position) -> list[Position]:
        """List the uses of the name at `at`, without its definition.

        Args:
            at: The position of a name.

        Returns:
            The positions inside the project.

        Raises:
            LspError: If the file cannot be read or the server fails.
        """
        answer = self._ask(
            "textDocument/references", at, {"context": {"includeDeclaration": False}}
        )
        return [spot for spot in self._positions(answer) if not spot.outside]

    def definitions(self, at: Position) -> list[Position]:
        """List where the name at `at` is defined.

        Args:
            at: The position of a name.

        Returns:
            The definitions, inside or outside the project.

        Raises:
            LspError: If the file cannot be read or the server fails.
        """
        return self._positions(self._ask("textDocument/definition", at))

    def implementations(self, at: Position) -> list[Position]:
        """List the methods that implement or override the method defined at `at`.

        Args:
            at: The position of a method's name in its definition.

        Returns:
            The positions inside the project.

        Raises:
            LspError: If the file cannot be read or the server fails.
        """
        answer = self._ask("textDocument/implementation", at)
        return [spot for spot in self._positions(answer) if not spot.outside]

    def symbols(self, name: str) -> list[Position]:
        """List the functions and methods whose name is exactly `name`.

        The server answers with the line of each definition. The column of the name on that
        line is looked up in the file.

        Args:
            name: The bare name.

        Returns:
            The positions of their names inside the project.

        Raises:
            LspError: If the server fails.
        """
        found: list[Position] = []
        for symbol in self._client_or_fail().request("workspace/symbol", {"query": name}) or []:
            if symbol.get("name") != name or symbol.get("kind") not in METHOD_KINDS:
                continue
            spots = self._positions([symbol.get("location") or {}])
            if not spots or spots[0].outside:
                continue
            column = name_column(self.root, spots[0].path, spots[0].line, name)
            if column is not None:
                found.append(Position(spots[0].path, spots[0].line, column))
        return found

    def changed(self, paths: list[str]) -> None:
        """Tell the server that files were written, created or deleted.

        Side effects: sends `workspace/didChangeWatchedFiles` if the server runs; the next
        question waits until `SETTLE_S` after now. A server that cannot be told is stopped
        and started again with the next question. Never raises.

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

    def close(self) -> None:
        """Stop the server, if it runs. Never raises."""
        if self._client is not None:
            self._client.close()
        self._client = None
        self._ready = False

    def _initialize(self) -> dict[str, Any]:
        """Build the parameters of the `initialize` request.

        Returns:
            The process id, the project directory and the client's capabilities: the four
            requests the navigator uses.
        """
        return {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
            "capabilities": {
                "textDocument": {"references": {}, "implementation": {}, "definition": {}},
                "workspace": {"symbol": {}, "workspaceFolders": True},
            },
        }

    def _client_or_fail(self) -> LspClient:
        """Return the running server.

        Returns:
            The client.

        Raises:
            LspError: If the server was not started.
        """
        if self._client is None:
            raise LspError("The language server is not running.")
        return self._client

    def _ask(self, method: str, at: Position, extra: dict[str, Any] | None = None) -> Any:
        """Ask one question about a position, with the file's present text sent first.

        Side effects: `textDocument/didOpen` with the file's text before the request and
        `textDocument/didClose` after it.

        Args:
            method: The protocol method.
            at: The position.
            extra: Further parameters of the request.

        Returns:
            The `result` of the answer.

        Raises:
            LspError: If the file cannot be read or the server fails.
        """
        client = self._client_or_fail()
        uri = (self.root / at.path).as_uri()
        try:
            text = (self.root / at.path).read_text("utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"{at.path} cannot be read ({exc}).") from exc
        document = {"uri": uri, "languageId": self.language, "version": 1, "text": text}
        client.notify("textDocument/didOpen", {"textDocument": document})
        try:
            return client.request(
                method,
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": at.line - 1, "character": at.column},
                    **(extra or {}),
                },
            )
        finally:
            client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def _positions(self, answer: Any) -> list[Position]:
        """Turn the locations of an answer into positions.

        Args:
            answer: A `Location`, a `LocationLink`, a list of them, or null.

        Returns:
            One position per location with a file URI; a location outside the project gets
            its absolute path and `outside` set.
        """
        items = answer if isinstance(answer, list) else [answer] if answer else []
        found: list[Position] = []
        for item in items:
            uri = item.get("uri") or item.get("targetUri") or ""
            span = item.get("range") or item.get("targetSelectionRange") or {}
            parsed = urlparse(uri)
            if parsed.scheme != "file" or "start" not in span:
                continue
            line, column = int(span["start"]["line"]) + 1, int(span["start"]["character"])
            path = Path(unquote(parsed.path)).resolve()
            try:
                found.append(Position(str(path.relative_to(self.root.resolve())), line, column))
            except ValueError:
                found.append(Position(str(path), line, column, outside=True))
        return found


def lsp_index(
    root: Path,
    command: tuple[str, ...] = BASEDPYRIGHT,
    ready_log: str = BASEDPYRIGHT_READY,
) -> NavigatorIndex:
    """Build the reference index on a language server.

    Args:
        root: The project directory.
        command: The server's command line.
        ready_log: Its ready message (`LspNavigator.ready_log`).

    Returns:
        The index; the server starts with its first question.
    """
    return NavigatorIndex(root, LspNavigator(root, command, ready_log))
