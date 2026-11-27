"""A scripted language server for the tests of `calu.refindex.lsp`.

It speaks the Language Server Protocol on stdin and stdout and answers from a JSON file named
by its first argument:

    {"ready": true,                      log "Found 3 source files" after `initialized`
     "references": {"a.py:5": [["b.py", 2, 6], ...]},      by "<file>:<1-based line>"
     "symbols": [["Base.save", "base.py", 3, 8], ...],     name is the part after the last dot
     "implementations": {"base.py:3": [["a.py", 5, 8]]},
     "definitions": {"b.py:2": [["a.py", 5, 8]]}}        where the name on that line is defined

It reads no file of the project and writes only its answers.
"""

import json
import sys
from pathlib import Path
from typing import Any


def send(message: dict[str, Any]) -> None:
    """Write one framed JSON-RPC message to stdout.

    Args:
        message: The message.
    """
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read() -> dict[str, Any] | None:
    """Read one framed JSON-RPC message from stdin.

    Returns:
        The message; None at the end of the input.
    """
    length = 0
    while line := sys.stdin.buffer.readline().strip():
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return json.loads(sys.stdin.buffer.read(length)) if length else None


def location(root: str, spot: list[Any]) -> dict[str, Any]:
    """Build a protocol `Location`.

    Args:
        root: The URI of the project directory.
        spot: [file, 1-based line, 0-based column].

    Returns:
        The location.
    """
    position = {"line": spot[1] - 1, "character": spot[2]}
    return {"uri": f"{root}/{spot[0]}", "range": {"start": position, "end": position}}


def main() -> None:
    """Answer requests from the script until `exit` or the end of the input."""
    script = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    root = ""
    while (message := read()) is not None:
        method, params = message.get("method"), message.get("params") or {}
        result: Any = None
        if method == "initialize":
            root = params["rootUri"]
            result = {"capabilities": {}}
        elif method == "initialized" and script.get("ready"):
            note = {"type": 3, "message": "Found 3 source files"}
            send({"jsonrpc": "2.0", "method": "window/logMessage", "params": note})
        elif method in (
            "textDocument/references",
            "textDocument/implementation",
            "textDocument/definition",
        ):
            name = params["textDocument"]["uri"].removeprefix(root + "/")
            key = f"{name}:{params['position']['line'] + 1}"
            table = method.rpartition("/")[2].removesuffix("s") + "s"
            result = [location(root, spot) for spot in script.get(table, {}).get(key, [])]
        elif method == "workspace/symbol":
            result = [
                {"name": name.rpartition(".")[2], "kind": 6, "location": location(root, spot)}
                for name, *spot in script.get("symbols", [])
            ]
        elif method == "exit":
            return
        if "id" in message:
            send({"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    main()
