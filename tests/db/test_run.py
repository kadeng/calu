"""`calu run` against the test database: one session, with an operator resolving an escalation."""

import functools
import json
from pathlib import Path

import anyio
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu import admin
from calu.claude import FakeRunner, FakeScript
from calu.config import SETTINGS_KEY
from calu.db import Db, connect
from calu.db import connect as connect_db
from calu.protocol.client import connect as connect_client
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.testing import _last_prompt, _rule_based, fake_models

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
        # D179: no rules are shipped; these runs are written against the recommended set.
        await admin.add_rule_set(Db(owner), "recommended")
        # calu ships no command allow list (D198); these runs let harmless commands through
        # with the list the rules engine's tests use.
        fixture = Path(__file__).parents[1] / "unit" / "fixtures" / "command-allow.txt"
        await admin.replace_rule_list(
            Db(owner), "command-allow", fixture.read_text(encoding="utf-8")
        )
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


def one_turn(command: str) -> FakeScript:
    """Return a script with a single turn that runs `command`.

    Args:
        command: What the fake Claude runs in that turn.

    Returns:
        The script; one run of `calu run --print` consumes one turn.
    """
    return FakeScript(
        turns=[
            [
                {"text": f"Running {command}."},
                {"tool": "Bash", "input": {"command": command}, "result": "ok"},
            ]
        ]
    )


async def test_the_checkers_still_see_the_conversation_after_two_resumes(
    db_url: str, tmp_path: Path
) -> None:
    """D65 end to end: four runs of one conversation, the last reading the second's fold.

    The shape that used to lose everything older is a resume that folds nothing: the third run
    leaves a session with turns and no summary behind, and the fourth run read that session
    alone -- so the summary came back empty and the turns before it were never read again.
    """
    session_url = await prepare(db_url, tmp_path)

    async def configure(fold_every: int) -> None:
        """Set how many unfolded turns trigger a fold."""
        owner = await connect(db_url)
        try:
            await owner.execute(
                "UPDATE config SET value = %s WHERE key = %s",
                (
                    json.dumps(
                        {
                            "claude": {"sandbox": {"enabled": False}},
                            "context": {"fold_every": fold_every, "fold_count": 1},
                        }
                    ),
                    SETTINGS_KEY,
                ),
            )
        finally:
            await owner.close()

    prompts: list[str] = []

    def recording_models() -> dict[str, str | Model]:
        """Fake models whose command checker keeps every prompt it was given."""

        def record(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            """Record the prompt, then decide as the rule-based checker would.

            Args:
                messages: The message history.
                info: Agent information, passed on.

            Returns:
                The rule-based checker's response.
            """
            prompts.append(_last_prompt(messages))
            return _rule_based(messages, info)

        models = fake_models()
        models["command"] = FunctionModel(record, model_name="recording-checker")
        return models

    async def run(command: str, resume: bool) -> int:
        """Run one session of the conversation, which runs `command` in its single turn."""
        return await run_session(
            session_url,
            RunOptions(
                workspace=tmp_path,
                prompt="go on",
                interactive=False,
                resume_id="fake-session" if resume else None,
            ),
            runner_factory=lambda workspace, _c, _h: FakeRunner(one_turn(command), workspace),
            checker_models=recording_models(),
        )

    async def stored_summary() -> tuple[str, int]:
        """Return the newest stored summary and the turn number it covers."""
        owner = await connect(db_url)
        try:
            cur = await owner.execute(
                "SELECT summary, covered_turns FROM chat_summaries ORDER BY covered_turns DESC"
            )
            rows = await cur.fetchall()
        finally:
            await owner.close()
        return (rows[0][0], rows[0][1]) if rows else ("", 0)

    # Two turns, one per run, and a fold as soon as two are unfolded: the second run folds the
    # first turn into the summary.
    await configure(fold_every=2)
    assert await run("deploy one", resume=False) == 0
    assert await run("deploy two", resume=True) == 0
    summary_text, covered = await stored_summary()
    assert covered == 1, "the second run was supposed to fold the first turn"

    # From here on nothing folds, which is the normal case (the default is every 30 turns), so
    # the third run leaves turns without a summary behind.
    await configure(fold_every=30)
    assert await run("deploy three", resume=True) == 0
    prompts.clear()
    assert await run("deploy four", resume=True) == 0

    assert prompts, "the checker never ran in the fourth session"
    seen = "\n".join(prompts)
    assert summary_text in seen, "the summary of the fold two sessions ago was lost"
    assert "deploy two" in seen, "the turn recorded by the second session was lost"
    assert "deploy three" in seen, "the turn recorded by the third session was lost"


async def test_a_batch_run_waits_for_claude_codes_background_tasks(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D158 end to end: the first turn ends with a background task running; the run goes on
    to Claude Code's own follow-up turn instead of stopping the session under it."""
    session_url = await prepare(db_url, tmp_path)
    options = RunOptions(workspace=tmp_path, prompt="build it", interactive=False)
    script = FakeScript(
        turns=[
            [{"text": "Started the build in the background."}, {"background_tasks": 1}],
            [{"text": "The build finished."}],
        ]
    )
    code = await run_session(
        session_url,
        options,
        runner_factory=lambda workspace, _c, _h: FakeRunner(script, workspace),
        checker_models=fake_models(),
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "1 background task(s) still running" in output
    assert "The build finished." in output


# ------------------------------------------------------------------ lost connections (sd1, cs3)


async def test_a_session_survives_the_loss_of_both_database_connections(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """sd1/cs3: a restarted database must not end an unattended run.

    Both connections of the session are ended by the server while the session waits for an
    operator, which is what a restart of PostgreSQL does to every session of a herd. The
    session reconnects, binds the new connection to its row again, sees the operator's answer
    and finishes; nothing of its history is missing.
    """
    monkeypatch.setattr("calu.db.connection.RECONNECT_FIRST_S", 0.05)
    monkeypatch.setattr("calu.db.notify.RECONNECT_FIRST_S", 0.05)
    session_url = await prepare(db_url, tmp_path)
    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                bytearray(session_url.encode()),
                RunOptions(workspace=tmp_path, prompt="have a look", interactive=False),
                runner_factory=lambda workspace, _config, _hidden: FakeRunner(SCRIPT, workspace),
                checker_models=fake_models(),
            )
        )

    async def restart_then_resolve() -> None:
        owner = await connect(db_url)
        try:
            with anyio.fail_after(20):
                while True:
                    cur = await owner.execute("SELECT id FROM escalations WHERE state = 'open'")
                    row = await cur.fetchone()
                    if row is not None:
                        break
                    await anyio.sleep(0.05)
            cur = await owner.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                " WHERE usename = 'calu_session' AND datname = current_database()"
            )
            assert await cur.fetchone() == (2,), "the session holds two connections"
            await anyio.sleep(0.3)
            # A rule tightened while the session could not hear about it.
            await owner.execute(
                "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)"
                " ON CONFLICT (scope, scope_id, key) DO UPDATE SET value = excluded.value",
                (
                    SETTINGS_KEY,
                    json.dumps(
                        {
                            "claude": {"sandbox": {"enabled": False}},
                            "auto_escalate": {"edits": True},
                        }
                    ),
                ),
            )
            await anyio.sleep(1.5)
            await owner.execute(
                "SELECT calu_resolve_escalation(%s, 'allowed', 'ship it')", (row[0],)
            )
        finally:
            await owner.close()

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(restart_then_resolve)
            tg.start_soon(run)
    assert codes == [0]
    output = capsys.readouterr().out
    assert "the database connection is lost" in output
    assert "the database connection is back" in output
    assert "All done." in output
    # The change made during the outage reached the session without a notification.
    assert "what waits for a human: edits" in output

    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT state, ended_at IS NOT NULL FROM sessions")
        assert await cur.fetchall() == [("stopped", True)]
        cur = await owner.execute(
            "SELECT seq, tool_name, decision, result_summary FROM steps ORDER BY seq"
        )
        assert await cur.fetchall() == [
            (1, "Bash", "pass", "a.py"),
            (2, "Bash", "escalate", "shipped"),
        ]
        cur = await owner.execute("SELECT number, answer FROM turns")
        assert await cur.fetchall() == [(1, "All done.")]
    finally:
        await owner.close()


async def test_a_session_whose_login_is_refused_for_good_stops_and_says_why(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattended run must neither hang for ever nor end silently.

    The session waits for an operator. `calu setup` runs again, which replaces the session
    credential, and then the session's connections are lost. No waiting brings that login
    back, so the run ends with exit code 1 and names the cause; the step it was waiting for
    never ran.
    """
    from calu.setup import enable_session_login

    monkeypatch.setattr("calu.db.connection.RECONNECT_FIRST_S", 0.05)
    monkeypatch.setattr("calu.db.notify.RECONNECT_FIRST_S", 0.05)
    session_url = await prepare(db_url, tmp_path)
    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                session_url,
                RunOptions(workspace=tmp_path, prompt="have a look", interactive=False),
                runner_factory=lambda workspace, _config, _hidden: FakeRunner(SCRIPT, workspace),
                checker_models=fake_models(),
            )
        )

    async def replace_credential_then_restart() -> None:
        owner = await connect(db_url)
        try:
            with anyio.fail_after(20):
                while True:
                    cur = await owner.execute("SELECT id FROM escalations WHERE state = 'open'")
                    if await cur.fetchone() is not None:
                        break
                    await anyio.sleep(0.05)
            await enable_session_login(owner, db_url)
            await owner.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                " WHERE usename = 'calu_session' AND datname = current_database()"
            )
        finally:
            await owner.close()

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(replace_credential_then_restart)
            tg.start_soon(run)
    assert codes == [1]
    captured = capsys.readouterr()
    assert "the database refuses the session's login" in captured.err
    assert "password authentication failed" in captured.err
    assert "calu setup" in captured.err
    assert "the database refuses the session's login" in captured.out, "clients are told too"
    assert "shipped" not in captured.out, "the step that waited for a human did not run"

    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT seq, decision, result_summary FROM steps ORDER BY seq")
        assert await cur.fetchall() == [(1, "pass", "a.py"), (2, "escalate", None)]
    finally:
        await owner.close()


def test_the_calu_process_cannot_be_read_by_other_processes_of_the_user() -> None:
    """cs2: what calu keeps in memory to reconnect is out of reach of same-user processes."""
    import subprocess
    import sys

    code = (
        "import ctypes, os\n"
        "from calu.run import harden_process\n"
        "libc = ctypes.CDLL(None)\n"
        "before = libc.prctl(3, 0, 0, 0, 0)\n"
        "assert harden_process()\n"
        "print(before, libc.prctl(3, 0, 0, 0, 0), os.stat('/proc/self/environ').st_uid)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    before, after, owner_uid = done.stdout.split()
    # PR_GET_DUMPABLE: 1 before, 0 after; the /proc files then belong to root.
    assert (before, after, owner_uid) == ("1", "0", "0")


# ------------------------------------------------------------------ the project (ga3)


async def test_an_unknown_project_is_an_error_at_the_start(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ga3: a typo in `--project` must not run the session on the global settings alone."""
    session_url = await prepare(db_url, tmp_path)
    owner = await connect(db_url)
    try:
        await admin.add_project(Db(owner), "payments")
    finally:
        await owner.close()
    built: list[Path] = []

    def runner_factory(workspace: Path, _config: object, _hidden: object) -> FakeRunner:
        built.append(workspace)
        return FakeRunner(CHAT_SCRIPT, workspace)

    code = await run_session(
        session_url,
        RunOptions(workspace=tmp_path, project="payment", prompt="go", interactive=False),
        runner_factory=runner_factory,
        checker_models=fake_models(),
    )
    assert code == 1 and built == []
    said = capsys.readouterr().err
    assert "no project named 'payment'" in said and "payments" in said
    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT count(*) FROM sessions")
        assert await cur.fetchone() == (0,)
    finally:
        await owner.close()


async def test_a_run_in_a_registered_directory_gets_its_project(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ga3: the quick start (`calu project add .`, then `calu run`) runs with the project."""
    import socket

    from calu.store.projects import register_directory

    session_url = await prepare(db_url, tmp_path)
    workspace = tmp_path / "backend"
    workspace.mkdir()
    owner = await connect(db_url)
    try:
        db = Db(owner)
        await admin.add_project(db, "demo")
        assert await register_directory(db, "demo", tmp_path, socket.gethostname())
        # A setting only the project has: every command waits for a human.
        await admin.set_setting(db, "auto_escalate.commands", True, project="demo")
    finally:
        await owner.close()

    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                session_url,
                RunOptions(workspace=workspace, prompt="go", interactive=False),
                runner_factory=lambda w, _c, _h: FakeRunner(CHAT_SCRIPT, w),
                checker_models=fake_models(),
            )
        )

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(resolve_as_operator, db_url, tmp_path)
            tg.start_soon(run)
    assert codes == [0]
    output = capsys.readouterr().out
    assert "[project] demo (registered for this directory)" in output
    owner = await connect(db_url)
    try:
        cur = await owner.execute(
            "SELECT p.name FROM sessions s JOIN projects p ON p.id = s.project_id"
        )
        assert await cur.fetchall() == [("demo",)]
        # The project's setting was in force: `ls`, which the checker passes, was escalated.
        cur = await owner.execute("SELECT count(*) FROM escalations")
        assert await cur.fetchone() == (1,)
    finally:
        await owner.close()


async def test_a_run_outside_every_registered_directory_says_that_no_project_applies(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_url = await prepare(db_url, tmp_path)
    code = await run_session(
        session_url,
        RunOptions(workspace=tmp_path, prompt="go", interactive=False),
        runner_factory=lambda w, _c, _h: FakeRunner(CHAT_SCRIPT, w),
        checker_models=fake_models(),
    )
    assert code == 0
    assert "[project] none" in capsys.readouterr().out


async def test_projects_are_found_by_the_nearest_registered_directory(
    db_url: str, tmp_path: Path
) -> None:
    from calu.store.projects import AmbiguousProject, project_for_directory, register_directory

    session_url = await prepare(db_url, tmp_path)
    owner = await connect(db_url)
    session = await connect(session_url)
    try:
        db = Db(owner)
        for name in ("shop", "backend", "twin", "elsewhere"):
            await admin.add_project(db, name)
        assert await register_directory(db, "shop", Path("/src/shop"), "host-a")
        assert await register_directory(db, "backend", Path("/src/shop/backend"), "host-a")
        assert await register_directory(db, "elsewhere", Path("/src/other"), "host-b")
        assert not await register_directory(db, "missing", Path("/x"), "host-a")
        # The session role reads the registrations; it needs no more than that.
        as_session = Db(session)
        find = project_for_directory
        assert await find(as_session, Path("/src/shop"), "host-a") == "shop"
        assert await find(as_session, Path("/src/shop/web/app"), "host-a") == "shop"
        assert await find(as_session, Path("/src/shop/backend/api"), "host-a") == "backend"
        assert await find(as_session, Path("/src/shopping"), "host-a") is None
        assert await find(as_session, Path("/src/other"), "host-a") is None  # another host
        await register_directory(db, "twin", Path("/src/shop"), "host-a")
        with pytest.raises(AmbiguousProject, match="shop, twin"):
            await find(as_session, Path("/src/shop/web"), "host-a")
        # The nearer registration still wins, unambiguously.
        assert await find(as_session, Path("/src/shop/backend"), "host-a") == "backend"
    finally:
        await owner.close()
        await session.close()


# ------------------------------------------------------------------ fork points (sd3)


async def test_the_session_role_finds_a_fork_point(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """sd3: `calu run --resume X --fork-before-turn N` asks on a `calu_session` connection.

    Row-level security hides every row from that connection before its own session exists, so
    the direct query found nothing and the command the web interface offers always failed.
    """
    from calu.context.turns import TurnRecord
    from calu.run import Console, _fork_point
    from calu.store import DatabaseSessionStore, SessionIdentity

    session_url = await prepare(db_url, tmp_path)
    earlier = await connect(session_url)
    try:
        store = DatabaseSessionStore(Db(earlier))
        await store.start(SessionIdentity(hostname="box", os_user="u", directory="/w", pid=1))
        await store.set_claude_session("claude-xyz")
        for number, (prompt, last) in enumerate([("p1", "a1"), ("p2", "a2")], start=1):
            await store.save_turn(
                TurnRecord(
                    index=number,
                    user_inputs=[f"turn {number}"],
                    answer="ok",
                    prompt_uuid=prompt,
                    last_entry_uuid=last,
                )
            )
    finally:
        await earlier.close()

    fresh = await connect(session_url)
    try:
        options = RunOptions(workspace=tmp_path, resume_id="claude-xyz", fork_before_turn=2)
        fork = await _fork_point(Db(fresh), options, Console())
        assert fork is not None
        assert fork.resume_at == "a1" and fork.drops_turn == "p2"
        # A turn that does not exist is reported with both possible causes.
        missing = RunOptions(workspace=tmp_path, resume_id="claude-xyz", fork_before_turn=7)
        assert await _fork_point(Db(fresh), missing, Console()) is None
        assert "knows no conversation with that Claude session id or no such turn" in (
            capsys.readouterr().out
        )
    finally:
        await fresh.close()


# ------------------------------------------------------------------ end of input (cs5)


async def test_a_closed_stdin_does_not_stop_a_session_that_has_work(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cs5: `echo "$PW" | calu run -p "..."` without `--batch` must finish the prompt.

    stdin is at its end as soon as the password was read. The session used to be stopped at
    that moment, seconds into its first turn, and the command exited 0.
    """
    import io

    session_url = await prepare(db_url, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                session_url,
                RunOptions(workspace=tmp_path, prompt="have a look", interactive=True),
                runner_factory=lambda w, _c, _h: FakeRunner(SCRIPT, w),
                checker_models=fake_models(),
            )
        )

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(resolve_as_operator, db_url, tmp_path)
            tg.start_soon(run)
    assert codes == [0]
    captured = capsys.readouterr()
    assert "stdin closed; waiting for Claude to finish" in captured.err
    assert "All done." in captured.out
    owner = await connect(db_url)
    try:
        cur = await owner.execute("SELECT seq, decision, result_summary FROM steps ORDER BY seq")
        assert await cur.fetchall() == [(1, "pass", "a.py"), (2, "escalate", "shipped")]
    finally:
        await owner.close()


async def test_a_closed_stdin_ends_a_session_that_was_given_nothing(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-D in an interactive session without a message still ends it at once."""
    import io

    session_url = await prepare(db_url, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with anyio.fail_after(30):
        code = await run_session(
            session_url,
            RunOptions(workspace=tmp_path, interactive=True, serve=False),
            runner_factory=lambda w, _c, _h: FakeRunner(CHAT_SCRIPT, w),
            checker_models=fake_models(),
        )
    assert code == 0


# ------------------------------------------------------------------ the heartbeat (sd4)


async def test_the_heartbeat_does_not_depend_on_the_clock_watch(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sd4: with the clock watch switched off a live session still says that it is running."""
    from calu import herd

    session_url = await prepare(db_url, tmp_path)
    owner = await connect(db_url)
    await owner.execute(
        "UPDATE config SET value = value || %s::jsonb WHERE key = %s",
        (json.dumps({"clock": {"check_interval_s": 0}}), SETTINGS_KEY),
    )
    monkeypatch.setattr(herd, "HEARTBEAT_S", 0.1)
    beats: list[bool] = []

    async def watch_then_resolve() -> None:
        with anyio.fail_after(20):
            while True:
                cur = await owner.execute("SELECT id FROM escalations WHERE state = 'open'")
                row = await cur.fetchone()
                if row is not None:
                    break
                await anyio.sleep(0.05)
        await anyio.sleep(0.6)
        cur = await owner.execute("SELECT last_seen > started_at FROM sessions")
        stamped = await cur.fetchone()
        assert stamped is not None
        beats.append(bool(stamped[0]))
        await owner.execute("SELECT calu_resolve_escalation(%s, 'allowed', '')", (row[0],))

    try:
        with anyio.fail_after(60):
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch_then_resolve)
                tg.start_soon(
                    functools.partial(
                        run_session,
                        session_url,
                        RunOptions(workspace=tmp_path, prompt="go", interactive=False),
                        runner_factory=lambda w, _c, _h: FakeRunner(SCRIPT, w),
                        checker_models=fake_models(),
                    )
                )
    finally:
        await owner.close()
    assert beats == [True]
    assert herd.STALE_AFTER_S >= 3 * 30.0


async def test_settings_that_cannot_be_reloaded_hold_every_checked_step(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """sd2: a session that knows its settings are stale waits for a human instead of going on.

    An operator changes the settings while the session runs, and the stored value cannot be
    applied (here: invalid). The session used to log a warning on its host and keep judging
    steps by the old settings. Now it says so to its clients and escalates every checked step
    until a reload succeeds.
    """
    monkeypatch.setattr("calu.store.config.RELOAD_RETRY_FIRST_S", 0.05)
    session_url = await prepare(db_url, tmp_path)
    codes: list[int] = []

    async def run() -> None:
        codes.append(
            await run_session(
                session_url,
                RunOptions(workspace=tmp_path, prompt="have a look", interactive=False),
                runner_factory=lambda workspace, _config, _hidden: FakeRunner(SCRIPT, workspace),
                checker_models=fake_models(),
            )
        )

    async def break_then_repair() -> None:
        owner = await connect(db_url)
        try:
            with anyio.fail_after(20):
                while True:
                    cur = await owner.execute("SELECT id FROM escalations WHERE state = 'open'")
                    row = await cur.fetchone()
                    if row is not None:
                        break
                    await anyio.sleep(0.05)
            await owner.execute(
                "UPDATE config SET value = %s WHERE key = %s",
                (json.dumps({"context": {"fold_every": 0}}), SETTINGS_KEY),
            )
            await anyio.sleep(1.0)
            held = capsys.readouterr().out
            assert "every checked step waits for a human until a reload succeeds" in held
            assert "what waits for a human: commands, edits, subagents" in held
            await owner.execute(
                "UPDATE config SET value = %s WHERE key = %s",
                (json.dumps({"claude": {"sandbox": {"enabled": False}}}), SETTINGS_KEY),
            )
            await anyio.sleep(1.0)
            released = capsys.readouterr().out
            assert "what waits for a human: nothing beyond what the checkers escalate" in released
            await owner.execute("SELECT calu_resolve_escalation(%s, 'allowed', '')", (row[0],))
        finally:
            await owner.close()

    with anyio.fail_after(60):
        async with anyio.create_task_group() as tg:
            tg.start_soon(break_then_repair)
            tg.start_soon(run)
    assert codes == [0]


# ------------------------------------------------------------------ the end of an unattended run


SLOW_TEXT_TURNS = FakeScript(turns=[[{"sleep": 0.5}, {"text": f"Answer {n}."}] for n in (1, 2, 3)])
"""Turns that outlast any short pause of the run loop, so a message sent meanwhile queues."""


async def text_only_run(
    db_url: str, workspace: Path, options: RunOptions, narrate: bool, on_serving=None
) -> int:
    """Run a session whose turns take a moment and call no tool; return its exit code."""
    session_url = await prepare(db_url, workspace)
    owner = await connect(db_url)
    try:
        await owner.execute(
            "UPDATE config SET value = %s WHERE scope = 'global' AND key = %s",
            (
                json.dumps(
                    {"claude": {"sandbox": {"enabled": False}}, "context": {"narrate": narrate}}
                ),
                SETTINGS_KEY,
            ),
        )
    finally:
        await owner.close()
    return await run_session(
        session_url,
        options,
        runner_factory=lambda w, _c, _h: FakeRunner(SLOW_TEXT_TURNS, w),
        checker_models=fake_models(),
        on_serving=on_serving,
    )


@pytest.mark.parametrize("narrate", [False, True])
async def test_a_run_whose_stdin_ended_finishes_after_its_queued_messages(
    db_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    narrate: bool,
) -> None:
    """An unattended run always ends: a prompt, two piped lines, three turns without a tool
    call. Every message gets its turn, and the command returns after the last one."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("second\nthird\n"))
    with anyio.fail_after(25):
        code = await text_only_run(
            db_url,
            tmp_path,
            RunOptions(workspace=tmp_path, prompt="first", interactive=True, serve=False),
            narrate,
        )
    assert code == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("Answer ")] == [
        "Answer 1.",
        "Answer 2.",
        "Answer 3.",
    ]


async def _ignore(_method: str, _params: dict) -> None:
    """Event handler of a client that only sends."""


@pytest.mark.parametrize("narrate", [False, True])
async def test_a_batch_run_finishes_after_a_message_a_client_queued(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], narrate: bool
) -> None:
    """An unattended run always ends: a client queues a message behind the prompt's turn and
    detaches; the run does the second turn and then returns."""

    async def queue_one(address: str, token: str) -> None:
        async with connect_client(address, token, "test-client", _ignore) as client:
            await client.request("chat.send", {"text": "second"})

    with anyio.fail_after(25):
        code = await text_only_run(
            db_url,
            tmp_path,
            RunOptions(workspace=tmp_path, prompt="first", interactive=False),
            narrate,
            on_serving=queue_one,
        )
    assert code == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("Answer ")] == [
        "Answer 1.",
        "Answer 2.",
    ]
