"""A whole session of a root calu for another uid, as a process (D206, D211, D215, D225).

`uitest/rootbox/fakecalu.py` is calu's own command line with a scripted runner and fake
checker models, so everything but Claude Code and the model calls is the real path: the
options, the credential store, `calu.asuser.prepare` and `enter`, the sockets, the log file.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from calu.credentials import CredentialStore
from tests.db.test_run import prepare
from tests.root.box import AS_ROOT, OTHER_UID, WHY_NOT

pytestmark = [
    pytest.mark.root,
    pytest.mark.db,
    pytest.mark.anyio,
    pytest.mark.skipif(not AS_ROOT, reason=WHY_NOT),
]

FAKECALU = Path(__file__).parents[2] / "uitest" / "rootbox" / "fakecalu.py"
"""calu's command line with the scripted runner."""

GREETER = 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
"""A file of the project that exists before the session."""

SCRIPT = {
    "turns": [
        [
            {"text": "Writing two files."},
            {
                "tool": "Bash",
                "input": {"command": "./generate.sh  # calu:allow"},
                "result": "ok",
                "writes": {"made.txt": "by the session\n", "sub/dir/deep.txt": "x\n"},
            },
            {"text": "All written."},
        ]
    ]
}
"""One turn: a command the fake checker allows; the runner writes its files itself, inside
the calu process, so they show whose file access that process has."""


@pytest.fixture
def project() -> Iterator[Path]:
    """A project directory that belongs to the unprivileged uid, reachable by it."""
    with tempfile.TemporaryDirectory(prefix="calu-root-project-") as name:
        base = Path(name)
        base.chmod(0o755)
        workspace = base / "project"
        workspace.mkdir()
        (workspace / "greeter.py").write_text(GREETER)
        for path in (workspace, workspace / "greeter.py"):
            os.chown(path, OTHER_UID, OTHER_UID)
        yield workspace


async def test_a_session_as_root_for_another_uid_writes_as_that_uid(
    db_url: str, project: Path, tmp_path: Path
) -> None:
    """`calu run --user UID --batch`: status only on the terminal, the user's files, root's log."""
    session_url = await prepare(db_url, project)
    config, state = tmp_path / "etc", tmp_path / "var"
    store = CredentialStore(config / "calu" / "credentials.json")
    # Without a password: accepted because only root can write this store (D228 4a).
    store.save("session", session_url, None)
    # Beside the project, where the user can read it: the runner is created after calu
    # switched its file access, and pytest's own temporary directory is root's alone.
    script = project.parent / "script.json"
    script.write_text(json.dumps(SCRIPT))
    environment = dict(os.environ) | {
        "PYTHONUNBUFFERED": "1",
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
        "FAKECALU_SCRIPT": str(script),
    }
    environment.pop("SUDO_UID", None)
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
            "write the files",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    # The terminal shows the session's life, never the conversation (D225).
    assert "[stopped]" in done.stdout
    assert "root clients, without a token: unix:/run/calu/sessions/" in done.stdout
    assert "write the files" not in done.stdout and "All written." not in done.stdout
    # The files were written by the calu process itself: with the user's file identity.
    made = [project / "made.txt", project / "sub", project / "sub" / "dir" / "deep.txt"]
    assert (project / "made.txt").read_text() == "by the session\n"
    assert [(path.stat().st_uid, path.stat().st_gid) for path in made] == [
        (OTHER_UID, OTHER_UID)
    ] * 3
    # What calu keeps for the user is the user's; what it keeps for itself is root's alone.
    for directory in (
        Path("/var/lib/calu/state") / str(OTHER_UID),
        Path("/var/lib/calu/claude") / str(OTHER_UID),
    ):
        status = directory.stat()
        assert status.st_uid == OTHER_UID and stat.S_IMODE(status.st_mode) == 0o700
    log = state / "calu" / "log" / "run.log"
    assert log.stat().st_uid == 0 and stat.S_IMODE(log.stat().st_mode) == 0o600
    sockets = Path("/run/calu/sessions")
    assert sockets.stat().st_uid == 0 and stat.S_IMODE(sockets.stat().st_mode) == 0o700
    (line,) = [text for text in done.stdout.splitlines() if "root clients, without" in text]
    own = Path(line.rpartition("unix:")[2].strip())
    assert own.parent == sockets and not own.exists(), "the socket is removed at the end"


async def test_root_without_a_named_user_starts_no_session(
    db_url: str, project: Path, tmp_path: Path
) -> None:
    """The refusal comes before the password and before anything is started."""
    done = subprocess.run(
        [sys.executable, str(FAKECALU), "run", str(project), "--batch", "-p", "x"],
        env=dict(os.environ) | {"XDG_CONFIG_HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 1
    assert "say which user Claude Code runs as" in done.stderr
    assert (project / "greeter.py").read_text() == GREETER


async def test_a_session_whose_claude_code_is_root_has_no_token_free_socket(
    db_url: str, tmp_path: Path
) -> None:
    """`--user root`: a command of Claude Code would be a root client of the root socket."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    session_url = await prepare(db_url, workspace)
    config = tmp_path / "etc"
    CredentialStore(config / "calu" / "credentials.json").save("session", session_url, None)
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    done = subprocess.run(
        [sys.executable, str(FAKECALU), "run", str(workspace), "--user", "root", "--batch", "-pgo"],
        env=dict(os.environ)
        | {
            "XDG_CONFIG_HOME": str(config),
            "XDG_STATE_HOME": str(tmp_path / "var"),
            "FAKECALU_SCRIPT": str(script),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[clients] ws://127.0.0.1:" in done.stdout and "[stopped]" in done.stdout
    assert "root clients, without a token" not in done.stdout
    assert (workspace / "made.txt").stat().st_uid == 0
