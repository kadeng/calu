"""`calu run` as a process: SIGTERM stops the session in order; who gets a token-free socket.

Both were found by running calu in the root box (`uitest/rootbox`, 2026-11-18).
"""

import json
import os
import signal
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest

from calu import localsocket
from calu import run as run_module
from calu.claude import FakeRunner
from calu.credentials import CredentialStore
from calu.db import connect
from calu.run import RunOptions, run_session
from calu.testing import fake_models
from tests.db.test_attach_discovery import _read_until
from tests.db.test_run import SCRIPT, prepare, resolve_as_operator

pytestmark = [pytest.mark.db, pytest.mark.anyio]

FAKECALU = Path(__file__).parents[2] / "uitest" / "rootbox" / "fakecalu.py"
"""calu's command line with a scripted runner in place of Claude Code."""


@pytest.fixture
def runtime() -> Iterator[Path]:
    """A short `XDG_RUNTIME_DIR`: a socket's path may be 107 bytes long at most."""
    with tempfile.TemporaryDirectory(prefix="crt-") as name:
        yield Path(name)


async def test_sigterm_stops_calu_run_in_order(db_url: str, tmp_path: Path, runtime: Path) -> None:
    """A `kill` or `docker stop` ended the process at once: the row stayed "escalated"."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    session_url = await prepare(db_url, workspace)
    config = tmp_path / "config"
    CredentialStore(config / "calu" / "credentials.json").save(
        "user", session_url, bytearray(b"a long enough password")
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT.model_dump()))
    environment = dict(os.environ) | {
        "PYTHONUNBUFFERED": "1",
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(runtime),
        "FAKECALU_SCRIPT": str(script),
    }
    errors = tmp_path / "stderr.log"
    with errors.open("wb") as log:
        process = await anyio.open_process(
            [sys.executable, str(FAKECALU), "run", str(workspace), "--batch", "--insecure", "-pgo"],
            env=environment,
            stderr=log.fileno(),
        )
    seen: list[str] = []
    try:
        assert process.stdin is not None
        await process.stdin.send(b"a long enough password\n")
        with anyio.fail_after(60):
            # The scripted turn stops at an escalation: the session is in the middle of it.
            await _read_until(process, "calu resolve", seen, errors)
            if not any("local clients, without a token" in line for line in seen):
                await _read_until(process, "local clients, without a token", seen, errors)
            sockets = runtime / "calu" / "sessions"
            assert len(list(sockets.glob("*.sock"))) == 1
            process.send_signal(signal.SIGTERM)
            await process.wait()
        said = errors.read_text(encoding="utf-8", errors="replace")
        assert process.returncode == 130, said
        assert "calu: interrupted" in said and "Traceback" not in said
        assert list(sockets.glob("*.sock")) == [], "the session's socket is removed"
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(10):
                await process.wait()
    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT state, ended_at IS NOT NULL FROM sessions")
        assert await cur.fetchall() == [("stopped", True)]
        cur = await owner.execute("SELECT state FROM escalations")
        assert await cur.fetchall() == [("cancelled",)]
    finally:
        await owner.close()


async def test_a_root_session_whose_claude_code_is_root_binds_no_token_free_socket(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """D226: no token only where the kernel tells the client apart from Claude Code.

    `calu run --user root` runs everything as root (`RunOptions.run_as` is None), so a
    command of Claude Code would be a root client of `/run/calu/sessions/<id>.sock`. The
    tests' own uid stands in for root here: with `os.geteuid` answering 0, the session must
    not try to bind that socket.
    """
    session_url = await prepare(db_url, tmp_path)
    bound: list[Path] = []
    monkeypatch.setattr(run_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(localsocket, "bind", lambda path, owner=0: bound.append(path))
    options = RunOptions(workspace=tmp_path, prompt="have a look", interactive=False)
    codes: list[int] = []

    async def run() -> None:
        """Run the scripted session to its end."""
        codes.append(
            await run_session(
                session_url,
                options,
                runner_factory=lambda workspace, _config, _hidden: FakeRunner(SCRIPT, workspace),
                checker_models=fake_models(),
            )
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(resolve_as_operator, db_url, tmp_path)
        tg.start_soon(run)
    output = capsys.readouterr()
    assert codes == [0], output.out + output.err
    assert bound == []
    assert "root clients, without a token" not in output.out
    assert "no socket for root clients" not in output.err
