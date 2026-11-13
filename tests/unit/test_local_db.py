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
    assert len(started) == 2, "the first start, then once more without the password"
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


def test_up_removes_trust_from_the_containers_authentication(docker: Path) -> None:
    """D163: the image trusts connections from inside the container, so `docker exec ... psql`
    got in without a password. Every start converts pg_hba.conf, after the container is up."""
    local.up(port=5599, owner_password="secret")
    recorded = [call["args"] for call in calls(docker)]
    started = next(i for i, args in enumerate(recorded) if args[-3:] == ["up", "-d", "--wait"])
    hardened = next(i for i, args in enumerate(recorded) if "exec" in args)
    assert hardened > started
    args = recorded[hardened]
    assert local.PROJECT in args and "postgres" in args
    script = args[-1]
    assert "scram-sha-256" in script and "pg_ctl reload" in script
    assert script.startswith("grep -qE"), "a no-op once no trust line is left"


def test_new_volumes_are_created_without_trust() -> None:
    """D163: the compose file asks initdb for passwords on every connection."""
    text = local.compose_file().read_text(encoding="utf-8")
    assert "--auth-local=scram-sha-256 --auth-host=scram-sha-256" in text


def test_a_new_database_gets_a_private_directory_with_a_mountable_one_inside(
    tmp_path: Path,
) -> None:
    """The cluster's files go into a host directory the user chose (PLAN.md D205). The
    container's PostgreSQL has a user id of its own and must reach the mount, so the mounted
    directory is a subdirectory and the chosen one stays private to the host user (measured
    with postgres:18: the cluster ends up owned by the container's user, mode 0700)."""
    chosen = local.prepare_data_dir(tmp_path / "state" / "db")
    assert chosen == tmp_path / "state" / "db"
    assert stat.S_IMODE(chosen.stat().st_mode) == 0o700
    assert stat.S_IMODE((chosen / "pg").stat().st_mode) == 0o755
    assert (
        local.mount_of(str(chosen)) == chosen / "pg" and local.mount_of(local.VOLUME_DATA) is None
    )
    assert not local.initialized(str(chosen))
    (chosen / "pg" / "18").mkdir()
    assert local.initialized(str(chosen))
    # A directory that holds a database is never taken for a new one.
    with pytest.raises(local.DockerError, match="not an empty directory"):
        local.prepare_data_dir(chosen)
    # What an unfinished setup left behind (a mistyped password) is taken again.
    again = local.prepare_data_dir(tmp_path / "retry")
    assert local.prepare_data_dir(tmp_path / "retry") == again
    for bad in (Path("relative/dir"), Path("/"), Path.home(), Path.home().parent):
        with pytest.raises(local.DockerError):
            local.prepare_data_dir(bad)
    with pytest.raises(local.DockerError, match="colon"):
        local.prepare_data_dir(tmp_path / "a:b")


def test_the_container_mounts_the_chosen_directory(docker: Path, tmp_path: Path) -> None:
    data = str(local.prepare_data_dir(tmp_path / "db"))
    with pytest.raises(ValueError, match="owner password"):
        local.up(5544, None, data)  # nothing initialized there: never create a database unasked
    local.up(5544, "owner-pw", data)
    first, second = calls(docker)[:2]
    assert first["env"] == {
        "CALU_DB_PORT": "5544",
        "CALU_DB_OWNER_PASSWORD": "owner-pw",
        "CALU_DB_DATA": f"{data}/pg",
    }
    # Once the cluster exists the container is created again without the password, so that
    # `docker inspect` does not show it for as long as the container lives.
    assert second["args"] == first["args"]
    assert second["env"] == {**first["env"], "CALU_DB_OWNER_PASSWORD": ""}
    compose = local.compose_file().read_text(encoding="utf-8")
    assert "${CALU_DB_DATA:-data}:/var/lib/postgresql" in compose


def test_a_stopped_local_database_is_started_when_it_is_needed(
    docker: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calu run` starts the local database itself (the owner's request of 2026-11-13)."""
    data = str(local.prepare_data_dir(tmp_path / "db"))
    block = local.DockerBlock(port=5544, data=data)
    monkeypatch.setattr(local, "listening", lambda port, timeout_s=0.5: True)
    assert local.ensure_running(block) is False and calls(docker) == [], "it answers: no docker"
    monkeypatch.setattr(local, "listening", lambda port, timeout_s=0.5: False)
    # Nothing was ever initialized there: an on-demand start does not create a database.
    with pytest.raises(local.DockerError, match="`calu db up` creates it"):
        local.ensure_running(block)
    assert calls(docker) == []
    # A record of the old form on a volume that does not exist may be somebody's own server:
    # nothing is started, and the connection attempt reports what is wrong.
    monkeypatch.setenv("STUB_EXIT", "1")  # `docker volume inspect` finds no volume
    assert local.ensure_running(local.DockerBlock(port=5544, data=local.VOLUME_DATA)) is False
    monkeypatch.delenv("STUB_EXIT")
    docker.unlink()
    (Path(data) / "pg" / "18").mkdir()
    assert local.ensure_running(block) is True
    started = calls(docker)[0]
    assert started["args"][-3:] == ["up", "-d", "--wait"]
    assert started["env"]["CALU_DB_DATA"] == f"{data}/pg"
    assert started["env"]["CALU_DB_OWNER_PASSWORD"] == "", "no secret goes to docker"


def test_the_owner_record_of_the_local_database_requires_proof_from_the_server() -> None:
    """A session refuses another listener on the port, or a cluster in a replaced directory:
    neither can complete SCRAM without knowing the password (measured with libpq 18)."""
    owner = local.owner_target("pw", 5544, "/var/lib/calu/db")
    assert owner.variant == "docker" and owner.user == "calu_owner"
    assert owner.conninfo["require_auth"] == "scram-sha-256"
    assert owner.docker == local.DockerBlock(port=5544, data="/var/lib/calu/db")
    session = owner.for_role("calu_session", local.PasswordAuth(password="s"))
    assert session.conninfo["require_auth"] == "scram-sha-256" and session.docker == owner.docker
    assert local.default_data_dir().parts[-2:] == ("calu", "db")
