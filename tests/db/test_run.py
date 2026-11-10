"""`calu run` against the test database: one session, with an operator resolving an escalation."""

import json
from pathlib import Path

import anyio
import pytest

from calu.claude import FakeRunner, FakeScript
from calu.config import SETTINGS_KEY
from calu.db import connect
from calu.db import connect as connect_db
from calu.protocol.client import connect as connect_client
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
            (1, "Bash", "pass", "a.py"),
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


CHAT_SCRIPT = FakeScript(
    turns=[
        [
            {"tool": "Bash", "input": {"command": "ls"}, "result": "a.py"},
            {"text": "One file."},
        ]
    ]
)


async def test_a_client_attaches_to_a_running_session(db_url: str, tmp_path: Path) -> None:
    session_url = await prepare(db_url, tmp_path)
    options = RunOptions(workspace=tmp_path, interactive=False)
    serving: list[tuple[str, str]] = []
    ready = anyio.Event()

    async def on_serving(address: str, token: str) -> None:
        serving.append((address, token))
        ready.set()

    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                session_url,
                options,
                runner_factory=lambda workspace, _c, _h: FakeRunner(CHAT_SCRIPT, workspace),
                checker_models=fake_models(),
                on_serving=on_serving,
            )
        )

    received: list[tuple[str, dict]] = []

    async def on_event(method: str, params: dict) -> None:
        received.append((method, params))

    async def client_work() -> None:
        with anyio.fail_after(20):
            await ready.wait()
        address, token = serving[0]
        async with connect_client(address, token, "test-client", on_event) as client:
            snapshot = await client.request("state.get")
            assert snapshot["workspace"] == str(tmp_path)
            assert snapshot["calu_session_id"] is not None
            await client.request("chat.send", {"text": "how many files?"})
            with anyio.fail_after(20):
                while not [m for m, _ in received if m == "turn.result"]:
                    await anyio.sleep(0.02)
            assert [p["content"] for m, p in received if m == "tool.result"] == ["a.py"]
            assert client.last_seq > 0
            await client.request("shutdown")

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        tg.start_soon(client_work)
    assert codes == [0]

    owner = await connect_db(db_url)
    try:
        cur = await owner.execute(
            "SELECT count(*) FROM client_tokens WHERE address = %s", (serving[0][0],)
        )
        assert await cur.fetchone() == (1,)
        cur = await owner.execute("SELECT state FROM sessions")
        assert await cur.fetchall() == [("stopped",)]
    finally:
        await owner.close()


async def test_the_session_credential_is_wiped_before_claude_starts(
    db_url: str, tmp_path: Path
) -> None:
    """REQUIREMENTS 16: the credential that reaches the database is gone before Claude runs.

    `run_session` wipes it after it has opened its two connections, which no test exercised --
    the other tests pass the connection string as a `str`, where there is nothing to wipe. Here
    it arrives as the `bytearray` that `calu run` really hands over, and the runner checks the
    buffer at the moment it is asked to start Claude: that is the instant the guarantee is
    about.
    """
    session_url = await prepare(db_url, tmp_path)
    credential = bytearray(session_url.encode())
    seen: list[bytes] = []

    def runner_factory(workspace: Path, _config: object, _hidden: object) -> FakeRunner:
        """Record the credential's bytes when the runner is built.

        Args:
            workspace: The session's workspace.
            _config: The Claude configuration (unused).
            _hidden: The hidden environment names (unused).

        Returns:
            The fake runner.
        """
        seen.append(bytes(credential))
        return FakeRunner(CHAT_SCRIPT, workspace)

    code = await run_session(
        credential,
        RunOptions(workspace=tmp_path, prompt="have a look", interactive=False),
        runner_factory=runner_factory,
        checker_models=fake_models(),
    )
    assert code == 0
    assert seen, "the runner was never built, so the test proves nothing"
    assert seen[0] == bytes(len(session_url)), "the credential was still readable"
    # And it stays wiped afterwards, so nothing in the process holds it.
    assert bytes(credential) == bytes(len(session_url))
    # The session really ran with that credential: its row is in the database.
    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT count(*) FROM sessions")
        assert (await cur.fetchone()) == (1,)
    finally:
        await owner.close()
