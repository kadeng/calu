"""`calu attach` with no arguments, as a real process against a real session (PLAN.md D105).

The pieces are tested apart: `_pick_session` chooses (tests/unit/test_endpoint.py), the lookup
and the minting work against the database (tests/db/test_herd.py) and the client speaks the
protocol (tests/unit/test_attach_process.py, with an address and a token). What is only tested
here is the composition a user actually runs -- `calu attach` and nothing else, in the project
directory -- because a wiring mistake between those pieces would pass every one of them.
"""

import os
import sys
from pathlib import Path

import anyio
import pytest

from calu import herd
from calu.credentials import CredentialStore
from calu.db import Db, connect
from tests.db.test_web_live import running

pytestmark = [pytest.mark.db, pytest.mark.anyio]

TIMEOUT_S = 60


async def _read_until(process, wanted: str, collected: list[str], errors: Path) -> str:
    """Read the child's stdout until `wanted` appears.

    Args:
        process: The running process.
        wanted: The text to wait for.
        collected: Every chunk read so far, appended to.
        errors: The file the child's stderr went to; its content goes into the failure message,
            because a process that dies before printing says why only there.

    Returns:
        Everything read so far.

    Raises:
        AssertionError: If stdout ends first.
    """
    stdout = process.stdout
    text = "".join(collected)
    while wanted not in text:
        try:
            chunk = await stdout.receive(4096)
        except anyio.EndOfStream:
            chunk = b""
        if not chunk:
            said = errors.read_text(encoding="utf-8", errors="replace") if errors.exists() else ""
            pytest.fail(
                f"stdout ended before {wanted!r} appeared (exit {process.returncode}).\n"
                f"stdout was:\n{text or '(empty)'}\nstderr was:\n{said or '(empty)'}"
            )
        collected.append(chunk.decode(errors="replace"))
        text = "".join(collected)
    return text


async def test_attach_with_no_arguments_finds_the_session_and_gives_its_token_back(
    db_url: str, tmp_path: Path
) -> None:
    """No address, no token, no password -- and the token is revoked when the client ends."""
    workspace = tmp_path / "project"
    (workspace / "src").mkdir(parents=True)
    async with running(db_url, workspace, [[{"text": "the answer"}]]) as (_base, session_id, _s):
        config = tmp_path / "config"
        store = CredentialStore(config / "calu" / "credentials.json")
        store.save("operator", db_url, None)  # file-mode protection, so nothing is asked (D104)

        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["XDG_CONFIG_HOME"] = str(config)
        errors = tmp_path / "attach-stderr.log"
        with errors.open("wb") as log:
            process = await anyio.open_process(
                [sys.executable, "-m", "calu", "attach"],
                env=environment,
                # A subdirectory of the project: that is where one works, and the session of the
                # directory above has to be found from here.
                cwd=str(workspace / "src"),
                stderr=log.fileno(),
            )
        seen: list[str] = []
        try:
            with anyio.fail_after(TIMEOUT_S):
                shown = await _read_until(process, "[attached]", seen, errors)
                assert str(workspace) in shown, "it attached to the session of this project"
                assert process.stdin is not None
                # One request over the connection, to show the token was really accepted and
                # not only sent: `/checkers` is answered by the session whatever state it is
                # in (`running` has already started it).
                await process.stdin.send(b"/checkers\n")
                await _read_until(process, "route command", seen, errors)
                await process.stdin.aclose()
                await process.wait()
            assert process.returncode == 0, errors.read_text(encoding="utf-8", errors="replace")
        finally:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(10):
                    await process.wait()

        watcher = await connect(db_url)
        try:
            rows = [
                row
                for row in await herd.list_tokens(Db(watcher), session_id)
                if row.label == "calu attach"
            ]
            assert len(rows) == 1, "one token minted for this client"
            assert rows[0].revoked, "and given back when it detached"
            assert rows[0].last_used_at is not None, "it was really used to authenticate"
        finally:
            await watcher.close()


async def test_attach_in_a_directory_without_a_session_says_what_to_do(
    db_url: str, tmp_path: Path
) -> None:
    """The common mistake: the right command in the wrong directory."""
    config = tmp_path / "config"
    store = CredentialStore(config / "calu" / "credentials.json")
    store.save("operator", db_url, None)
    conn = await connect(db_url)
    try:
        from calu.setup import setup

        await setup(conn, db_url, "test:model")
    finally:
        await conn.close()

    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(config)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = await anyio.run_process(
        [sys.executable, "-m", "calu", "attach"],
        env=environment,
        cwd=str(elsewhere),
        check=False,
    )
    said = result.stderr.decode(errors="replace")
    assert result.returncode == 1, said
    assert "no running session of" in said and str(elsewhere) in said
    assert "calu sessions" in said, "it has to say where the sessions are"
