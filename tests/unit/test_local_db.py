"""Starting the local PostgreSQL container (`calu db up`), without Docker.

`calu db up` is how someone with nothing set up gets a database (REQUIREMENTS 18), and the
calls it makes were untested, because testing them looked like it needed Docker. It does not: a
stub `docker` on PATH records what it was asked to do, which is what matters here -- the
compose file, the project name, the port and the password reaching the container as environment
variables, and a failure of the command becoming a `DockerError` rather than a traceback.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from calu.db import local

STUB = """#!/usr/bin/env python3
import json
import os
import sys

record = {
    "args": sys.argv[1:],
    "env": {k: v for k, v in os.environ.items() if k.startswith("CALU_DB_")},
}
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a stub `docker` on PATH and return the file it records calls in.

    Args:
        tmp_path: A directory for the stub and its log.
        monkeypatch: Used to change PATH and the stub's environment.

    Returns:
        The log file; one JSON object per call.
    """
    binary = tmp_path / "docker"
    binary.write_text(STUB, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    return log


def calls(log: Path) -> list[dict]:
    """Read the recorded calls.

    Args:
        log: The log file.

    Returns:
        One entry per call; empty if nothing was called.
    """
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_the_first_start_needs_a_password_and_passes_it_to_the_container(docker: Path) -> None:
    """Without a volume the password makes the owner, so it must not be silently skipped."""
    # The stub answers "no such volume" (exit 1) for the inspect, so this is a first start.
    os.environ["STUB_EXIT"] = "1"
    try:
        with pytest.raises(ValueError, match="needs the owner password"):
            local.up(port=55432)
        assert [call["args"][:3] for call in calls(docker)] == [["volume", "inspect", local.VOLUME]]
    finally:
        del os.environ["STUB_EXIT"]


def test_up_starts_the_compose_project_and_waits(docker: Path) -> None:
    """The command has to name calu's own compose file and project, and wait for health."""
    local.up(port=5599, owner_password="secret")
    recorded = calls(docker)
    started = [call for call in recorded if "up" in call["args"]]
    assert len(started) == 1
    args = started[0]["args"]
    assert args[:2] == ["compose", "-f"]
    assert args[2].endswith("local-compose.yml")
    assert Path(args[2]).is_file(), "the compose file must ship with the package"
    assert "-p" in args and local.PROJECT in args
    assert args[-3:] == ["up", "-d", "--wait"], "a start that does not wait is not a start"
    # The port and the password reach the container, and nothing else does.
    assert started[0]["env"] == {"CALU_DB_PORT": "5599", "CALU_DB_OWNER_PASSWORD": "secret"}


def test_a_failing_docker_becomes_an_error_with_its_output(docker: Path) -> None:
    """`calu db up` has to say what docker said, not raise a traceback at the user."""
    os.environ["STUB_EXIT"] = "1"
    os.environ["STUB_STDERR"] = "no space left on device"
    try:
        with pytest.raises(local.DockerError, match="no space left on device"):
            local.up(port=55432, owner_password="secret")
    finally:
        del os.environ["STUB_EXIT"]
        del os.environ["STUB_STDERR"]


def test_down_keeps_the_volume(docker: Path) -> None:
    """The data survives `calu db down`: no `-v` in the command."""
    local.down()
    args = calls(docker)[0]["args"]
    assert args[:2] == ["compose", "-f"]
    assert args[-1] == "down"
    assert "-v" not in args and "--volumes" not in args


def test_a_missing_docker_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first thing a new user can get wrong is not having Docker."""
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no docker
    with pytest.raises(local.DockerError, match="not installed"):
        local.volume_exists()


def test_the_owner_url_and_password_are_usable(tmp_path: Path) -> None:
    """The generated password goes into a connection string, so it must survive quoting."""
    password = local.new_owner_password()
    assert len(password) > 20
    url = local.owner_url(password, port=5599)
    assert "5599" in url and "calu_owner" in url
    # A libpq URL must not be broken by the password's characters.
    from psycopg.conninfo import conninfo_to_dict

    parsed = conninfo_to_dict(url)
    assert parsed["password"] == password
    assert parsed["port"] == "5599"
