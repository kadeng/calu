"""`calu acp` with no arguments, driven the way an editor drives it (PLAN.md D105, D106).

This is the configuration CALU_GUIDE.md tells an editor to use --
`{"command": "calu", "args": ["acp"]}` with the project as the working directory -- and nothing
else covers it: `tests/unit/test_acp_process.py` spawns the adapter with an address and a
token, which is the remote path. What is tested here is the editor path: the adapter looks the
session up in the database, mints its own token, and answers the handshake on its pipes without
ever asking for a password.

**Coverage does not see this**, because the adapter runs in another process. That is the point:
no mock can check that a real process carries the protocol on its real pipes.
"""

import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu import herd
from calu.credentials import CredentialStore
from calu.db import Db, connect
from tests.db.test_web_live import running
from tests.unit.test_acp_process import Editor

pytestmark = [pytest.mark.db, pytest.mark.anyio]

TIMEOUT_S = 60


async def _spawn(workspace: Path, config: Path, errors: Path, arguments: list[str]) -> Any:
    """Start `calu acp` as an editor would, in the project directory.

    Args:
        workspace: The directory the "editor" has open; also the child's working directory.
        config: What `XDG_CONFIG_HOME` points at, so the child reads the test's credentials.
        errors: File for the child's stderr, quoted in a failure message.
        arguments: Arguments after `acp`.

    Returns:
        The running process.
    """
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["XDG_CONFIG_HOME"] = str(config)
    with errors.open("wb") as log:
        return await anyio.open_process(
            [sys.executable, "-m", "calu", "acp", *arguments],
            env=environment,
            cwd=str(workspace),
            stderr=log.fileno(),
        )


async def test_an_editor_configured_with_calu_acp_alone_reaches_the_session(
    db_url: str, tmp_path: Path
) -> None:
    """No address, no token, no password: `initialize` and `session/new` are answered."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    async with running(db_url, workspace, [[{"text": "I looked."}]]) as (_base, session_id, _s):
        config = tmp_path / "config"
        CredentialStore(config / "calu" / "credentials.json").save("operator", db_url, None)
        errors = tmp_path / "acp-stderr.log"
        process = await _spawn(workspace, config, errors, [])
        editor = Editor(process, errors)
        try:
            with anyio.fail_after(TIMEOUT_S):
                first = await editor.send("initialize", {"protocolVersion": 1})
                answer = await editor.read_until(lambda m: m.get("id") == first)
                assert answer["result"]["agentInfo"]["name"] == "calu"

                # The editor's own open folder, which is the session's project directory here.
                second = await editor.send("session/new", {"cwd": str(workspace), "mcpServers": []})
                created = await editor.read_until(lambda m: m.get("id") == second)
                assert created["result"]["sessionId"], created
                await process.stdin.aclose()
                await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()

        # Every line it wrote to stdout was protocol (`Editor.read_until` fails otherwise), and
        # the token it minted for itself was given back.
        watcher = await connect(db_url)
        try:
            rows = [
                row
                for row in await herd.list_tokens(Db(watcher), session_id)
                if row.label == "calu acp"
            ]
            assert len(rows) == 1 and rows[0].revoked, rows
            assert rows[0].last_used_at is not None, "the session really authenticated it"
        finally:
            await watcher.close()


async def test_a_credential_with_a_password_is_refused_without_reading_the_protocol(
    db_url: str, tmp_path: Path
) -> None:
    """D106, as the editor would see it: a message on stderr, not a hang and not a bad read.

    `read_password` reads a line from standard input when that is not a terminal, so the
    editor's first request would have become the password. The in-process test asserts the
    request is still unread; here the point is what an editor gets: the process ends, says why
    on stderr, and writes nothing to stdout.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = tmp_path / "config"
    CredentialStore(config / "calu" / "credentials.json").save("operator", db_url, "a password")
    errors = tmp_path / "acp-stderr.log"
    process = await _spawn(workspace, config, errors, [])
    try:
        with anyio.fail_after(TIMEOUT_S):
            assert process.stdin is not None
            await process.stdin.send(
                (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n").encode()
            )
            out = b""
            assert process.stdout is not None
            with anyio.move_on_after(TIMEOUT_S), suppress(anyio.EndOfStream):
                while True:
                    out += await process.stdout.receive(4096)
            await process.wait()
        assert out == b"", f"stdout must carry protocol or nothing, not {out!r}"
        assert process.returncode == 1
        said = errors.read_text(encoding="utf-8", errors="replace")
        assert "no terminal" in said and "--no-password" in said, said
    finally:
        if process.returncode is None:
            process.terminate()
            with anyio.move_on_after(10):
                await process.wait()
