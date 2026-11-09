"""`calu run` against the test database: one session, with an operator resolving an escalation."""

import json
from pathlib import Path

import anyio
import pytest

from calu.claude import FakeRunner, FakeScript
from calu.config import SETTINGS_KEY
from calu.db import connect
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.testing import fake_models

pytestmark = [pytest.mark.db, pytest.mark.anyio]

SCRIPT = FakeScript(
    turns=[
        [
            {"text": "I will look around."},
            {"tool": "Bash", "input": {"command": "ls"}, "result": "a.py"},
            {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "shipped"},
            {"text": "All done."},
        ]
    ]
)


async def prepare(db_url: str, workspace: Path) -> str:
    """Migrate, seed and configure a database for one run; return the session connection."""
    owner = await connect(db_url)
    try:
        _, session_url = await setup(owner, db_url, "test:model")
        # The sandbox is not available in the test environment, and the narrator would need a
        # second fake model per step; both are switched off through the stored configuration.
        await owner.execute(
            "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
            (SETTINGS_KEY, json.dumps({"claude": {"sandbox": {"enabled": False}}})),
        )
    finally:
        await owner.close()
    return session_url


async def resolve_as_operator(db_url: str, workspace: Path) -> None:
    """Wait for the open escalation of the run and allow it, as `calu resolve` would."""
    owner = await connect(db_url)
    try:
        with anyio.fail_after(20):
            while True:
                cur = await owner.execute(
                    "SELECT id FROM escalations WHERE state = 'open' ORDER BY id LIMIT 1"
                )
                row = await cur.fetchone()
                if row is not None:
                    break
                await anyio.sleep(0.05)
        await owner.execute("SELECT calu_resolve_escalation(%s, 'allowed', 'ship it')", (row[0],))
    finally:
        await owner.close()


async def test_a_batch_run_records_the_session_and_waits_for_the_operator(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_url = await prepare(db_url, tmp_path)
    options = RunOptions(workspace=tmp_path, prompt="have a look", interactive=False)

    codes: list[int] = []

    async def run() -> None:
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
    assert codes == [0]
    output = capsys.readouterr().out
    assert "All done." in output
    assert "escalation" in output and "calu resolve" in output

    owner = await connect(db_url)
    try:
        cur = await owner.execute(
            "SELECT state, claude_session_id, ended_at IS NOT NULL FROM sessions"
        )
        assert await cur.fetchall() == [("stopped", "fake-session", True)]
        cur = await owner.execute(
            "SELECT seq, tool_name, decision, result_summary FROM steps ORDER BY seq"
        )
        assert await cur.fetchall() == [
            (1, "Bash", "allow", "a.py"),
            (2, "Bash", "escalate", "shipped"),
        ]
        cur = await owner.execute("SELECT state, note, resolved_by FROM escalations ORDER BY id")
        assert await cur.fetchall() == [("allowed", "ship it", "calu_owner")]
        cur = await owner.execute("SELECT number, user_inputs, answer FROM turns")
        assert await cur.fetchall() == [(1, ["have a look"], "All done.")]
        cur = await owner.execute("SELECT checker_name, decision FROM checker_runs ORDER BY id")
        assert await cur.fetchall() == [("command", "escalate")]
    finally:
        await owner.close()
