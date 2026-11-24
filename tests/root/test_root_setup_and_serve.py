"""`calu setup` and `calu serve` as real root (D206, D208, D215, D228, D239, D265).

The place file is the machine's own `/var/lib/calu/database.json` here, as root's calu writes
it; the tests keep a file that was there before and put it back afterwards. `calu serve`
starts Claude Code through calu's launcher; a stand-in program answers the Agent SDK's
start-up requests and records who it runs as.
"""

import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import anyio
import pytest

from calu import cli, localsocket, logins, session_requests
from calu.config import SETTINGS_KEY
from calu.credentials import CredentialStore
from calu.db import Db, connect, place
from calu.db.target import Target
from calu.preflight import SANDBOX_OFF_ADVICE
from calu.session_requests import SessionRequest
from tests.db.test_run import prepare
from tests.root.box import AS_ROOT, OTHER_UID, THIRD_UID, WHY_NOT
from tests.root.test_privileges import as_user

pytestmark = [pytest.mark.root, pytest.mark.skipif(not AS_ROOT, reason=WHY_NOT)]

FAKECALU = Path(__file__).parents[2] / "uitest" / "rootbox" / "fakecalu.py"
"""calu's command line with the scripted runner."""

QUIET_SCRIPT = {"turns": [[{"text": "Nothing to change."}]]}
"""One turn without a tool call: no checker runs, the session only starts and ends."""

ADVICE_START = SANDBOX_OFF_ADVICE[:40]
"""The beginning of the sandbox-off recommendation, as every place prints it."""


@pytest.fixture
def real_place_file(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Let this test and the processes it starts use the machine's place file.

    Side effects (root box only): removes `CALU_PLACE_FILE` from the environment for the
    test; keeps the content of an existing `/var/lib/calu/database.json` and writes it back
    afterwards, or removes the file if there was none.

    Yields:
        `calu.db.place.PATH`.
    """
    monkeypatch.delenv(place.PATH_ENV, raising=False)
    before = place.PATH.read_bytes() if place.PATH.exists() else None
    try:
        yield place.PATH
    finally:
        if before is None:
            place.PATH.unlink(missing_ok=True)
        else:
            place.PATH.write_bytes(before)
            place.PATH.chmod(0o644)


@pytest.fixture
def project() -> Iterator[Path]:
    """A project directory of the unprivileged uid, in a directory every user can enter."""
    with tempfile.TemporaryDirectory(prefix="calu-root-project-") as name:
        base = Path(name)
        base.chmod(0o755)
        workspace = base / "project"
        workspace.mkdir()
        os.chown(workspace, OTHER_UID, OTHER_UID)
        yield workspace


def test_storing_the_logins_as_root_publishes_the_user_login_only_without_a_user_password(
    real_place_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D228 (2), (4b), D265 (2): `cli._store_logins` as root writes `/var/lib/calu/database.json`.

    Root's file, 0644, in a directory only root writes, read and trusted by another user;
    never the owner's password. With a user password both entries are encrypted and the file
    holds no secret. With an empty one (`--no-password`) both are stored without encryption
    and the file publishes the generated password of `calu_admin`; a later setup with a user
    password takes it out again and removes the entries of before D265 it was given.

    Side effects (root box only): the place file (kept and restored by `real_place_file`);
    a store below `tmp_path`.
    """
    store = CredentialStore(tmp_path / "etc" / "calu" / "credentials.json")
    owner = Target.parse(b"postgresql://calu_owner:owner-pw@127.0.0.1:5434/calu")
    reader = """
        from calu.db import place
        target = place.read()
        print(target.user, target.conninfo["port"], getattr(target.auth, "password", "") or "-")
    """

    def published() -> dict[str, object]:
        """Check the file's owner, mode and trust; return its content."""
        status = real_place_file.stat()
        assert (status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode)) == (0, 0, 0o644)
        assert place.problems() == []
        assert [p.name for p in real_place_file.parent.glob(".database.json.*")] == []
        text = real_place_file.read_text()
        assert "owner-pw" not in text and "calu_owner" not in text
        return json.loads(text)

    cli._store_logins(store, owner, bytearray(b"user-pw"), "user-pw", [])
    assert "admin_password" not in published()
    assert "WARNING" not in capsys.readouterr().err
    assert as_user(THIRD_UID, reader).stdout.split() == ["calu_admin", "5434", "-"]
    # Both logins are in root's store, encrypted under the user password, 0600.
    assert store.needs_password(logins.USER) and store.needs_password(logins.DB_ADMIN)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    cli._store_logins(store, owner, bytearray(), "generated-2", [])
    assert published()["admin_password"] == "generated-2"
    warned = capsys.readouterr().err
    assert f"also in {place.PATH}, which every user" in warned
    seen = as_user(THIRD_UID, reader)
    assert seen.stdout.split() == ["calu_admin", "5434", "generated-2"], seen.stderr
    assert not store.needs_password(logins.USER) and not store.needs_password(logins.DB_ADMIN)

    # A store of before D265 converted with a user password: the old entry goes.
    store.save("session", bytearray(b"postgresql://calu_session@127.0.0.1:5434/calu"), None)
    cli._store_logins(store, owner, bytearray(b"user-pw"), "user-pw", ["session"])
    assert "admin_password" not in published()
    assert "session" not in store
    assert store.needs_password(logins.USER)
    assert as_user(THIRD_UID, reader).stdout.split() == ["calu_admin", "5434", "-"]


def _run_fakecalu(
    project: Path, config: Path, state: Path, script: Path, password: str | None = None
) -> str:
    """Run one scripted `calu run --user UID --batch` session as root; return its output.

    Args:
        project: The project directory, the user's.
        config: `XDG_CONFIG_HOME` with the credential store.
        state: `XDG_STATE_HOME` for the log file.
        script: The runner's script, readable by the user.
        password: The calu user password, given as the one line of standard input; None
            gives no input (a login without a password, or one the place file publishes).

    Returns:
        stdout and stderr together.
    """
    environment = dict(os.environ) | {
        "PYTHONUNBUFFERED": "1",
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
        "FAKECALU_SCRIPT": str(script),
    }
    environment.pop("SUDO_UID", None)
    environment.pop(place.PATH_ENV, None)
    done = subprocess.run(
        [
            sys.executable,
            str(FAKECALU),
            "run",
            str(project),
            "--user",
            f"{OTHER_UID}:{OTHER_UID}",
            "--batch",
            "-p",
            "look around",
        ],
        env=environment,
        input="" if password is None else f"{password}\n",
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert "[stopped]" in done.stdout, output
    return output


@pytest.mark.db
@pytest.mark.anyio
async def test_the_sandbox_off_advice_follows_what_protects_the_logins_of_a_root_setup(
    db_url: str,
    real_place_file: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D239 (2), D265 (2) with real root: `sudo calu setup --no-password`, `--user` sessions.

    After the setup with an empty user password the place file publishes the user login
    (`calu_admin`), so the setup itself and a `--user` session both show the recommendation
    and say why. `calu setup --set-password` with a user password takes the password out of
    the file; then neither shows it: calu runs as root and Claude Code as another user.

    Side effects (root box only): the place file (kept and restored by `real_place_file`);
    the test database's roles; `/var/lib/calu/{state,claude}/<uid>`.
    """
    config, state = tmp_path / "etc", tmp_path / "var"
    store = CredentialStore(config / "calu" / "credentials.json")
    store.save(logins.DB_ADMIN, db_url, None)
    script = project.parent / "script.json"
    script.write_text(json.dumps(QUIET_SCRIPT))
    published = "the place file publishes the user login (calu_admin)"

    def setup(*options: str, answers: str = "") -> str:
        """Run `calu setup` in this process with `answers` as its standard input (no
        terminal); return its stderr."""
        monkeypatch.setattr("sys.stdin", io.StringIO(answers))
        code = cli.main(["setup", *options], store_factory=lambda: cast(CredentialStore, store))
        said = capsys.readouterr()
        assert code == 0, said.out + said.err
        return said.err

    warned = await anyio.to_thread.run_sync(lambda: setup("--no-password", "--no-sandbox"))
    assert json.loads(real_place_file.read_text()).get("admin_password")
    assert f"WARNING: {ADVICE_START}" in warned
    assert published in warned
    shown = _run_fakecalu(project, config, state, script)
    assert ADVICE_START in shown
    assert published in shown
    # The logins are stored without a password; with --user that is no finding.
    assert "stored without a password" not in shown

    warned = await anyio.to_thread.run_sync(
        lambda: setup("--set-password", answers="user-pw\nuser-pw\n")
    )
    assert "admin_password" not in json.loads(real_place_file.read_text())
    assert store.needs_password(logins.USER)
    assert ADVICE_START not in warned
    assert ADVICE_START not in _run_fakecalu(project, config, state, script, "user-pw")


STANDIN = '''#!{python}
"""Claude Code's stand-in: records who runs it, answers the SDK's control requests."""
import json, os, sys, time
if sys.argv[1:] in (["-v"], ["--version"]):
    print("2.1.319 (Claude Code)")
    sys.exit(0)
record = {{
    "uid": os.getresuid(), "gid": os.getresgid(), "groups": sorted(os.getgroups()),
    "home": os.environ.get("HOME"), "cwd": os.getcwd(),
}}
with open(os.path.join({records!r}, f"{{time.time_ns()}}-{{os.getpid()}}.json"), "w") as out:
    json.dump(record, out)
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if message.get("type") == "control_request":
        answer = {{"hooks_applied": True, "hook_on_failure_supported": True}}
        print(json.dumps({{"type": "control_response", "response": {{
            "subtype": "success", "request_id": message["request_id"], "response": answer,
        }}}}), flush=True)
'''
"""The stand-in program; `{python}` and `{records}` are filled in."""


@pytest.mark.db
@pytest.mark.anyio
async def test_calu_serve_as_root_runs_claude_code_as_the_user_behind_a_root_socket(
    db_url: str, project: Path, tmp_path: Path
) -> None:
    """D206, D208, D215: `calu serve --user UID` as root, a session requested through the DB.

    Claude Code (the stand-in, started through calu's launcher) runs with the user's uid and
    gid on all three slots, in the project, with the home calu gives a bare uid; the session
    listens on `/run/calu/sessions/<id>.sock`, root's, 0600. After SIGTERM the server ends
    with status 0 and the socket is gone.

    Side effects (root box only): `/var/lib/calu/{state,claude}/<uid>`, `/run/calu/sessions`;
    rows in the test database.
    """
    user_url = await prepare(db_url, project)
    standin_dir = project.parent / "standin"
    records = standin_dir / "records"
    records.mkdir(parents=True)
    standin_dir.chmod(0o755)
    records.chmod(0o1777)
    standin = standin_dir / "claude"
    standin.write_text(STANDIN.format(python=sys.executable, records=str(records)))
    standin.chmod(0o755)
    owner = await connect(db_url)
    try:
        await owner.execute(
            "UPDATE config SET value = %s WHERE scope = 'global' AND key = %s",
            (
                json.dumps({"claude": {"sandbox": {"enabled": False}, "cli_path": str(standin)}}),
                SETTINGS_KEY,
            ),
        )
    finally:
        await owner.close()
    config, state = tmp_path / "etc", tmp_path / "var"
    CredentialStore(config / "calu" / "credentials.json").save(logins.USER, user_url, None)
    environment = dict(os.environ) | {
        "PYTHONUNBUFFERED": "1",
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
    }
    environment.pop("SUDO_UID", None)
    # The projects directory is the one that holds the project (D243); the default, the
    # current directory, is the checkout.
    command = [
        str(Path(sys.executable).parent / "calu"),
        "serve",
        "--user",
        f"{OTHER_UID}",
        "--projects",
        str(project.parent),
    ]
    server = await anyio.to_thread.run_sync(
        lambda: subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    )
    output = ""
    try:
        conn = await connect(db_url)
        try:
            db = Db(conn)
            filed = await session_requests.file(
                db,
                SessionRequest(
                    hostname=socket.gethostname(),
                    os_user=str(OTHER_UID),
                    directory=str(project),
                    label="root test",
                ),
            )
            with anyio.fail_after(120):
                done = await session_requests.wait_for(db, filed, claim_wait_s=60)
        finally:
            await conn.close()
        assert done.state == "started", done.message
        assert done.session_id is not None
        path = localsocket.path_of(done.session_id)
        status = path.lstat()
        assert stat.S_ISSOCK(status.st_mode)
        assert (status.st_uid, stat.S_IMODE(status.st_mode)) == (0, 0o600)
        assert localsocket.problems(path) == []
        (started,) = [json.loads(p.read_text()) for p in records.glob("*.json")]
        assert started == {
            "uid": [OTHER_UID] * 3,
            "gid": [OTHER_UID] * 3,
            "groups": [],
            "home": f"/var/lib/calu/claude/{OTHER_UID}",
            "cwd": str(project),
        }
        server.terminate()
        output, _ = await anyio.to_thread.run_sync(lambda: server.communicate(timeout=60))
        assert server.returncode == 0, output
        assert not path.exists(), "the socket is removed when the session ends"
    finally:
        if server.poll() is None:
            server.kill()
            output += server.communicate(timeout=30)[0]
        print(textwrap.indent(output, "serve| "))
