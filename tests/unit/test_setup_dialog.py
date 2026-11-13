"""`calu setup` and `calu run` for the local Docker database, without Docker (PLAN.md D205)."""

import io
from pathlib import Path

import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db import local
from calu.db.target import PasswordAuth, Target


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    """An empty credential store in a temporary directory."""
    return CredentialStore(tmp_path / "config" / "credentials.json")


def run_cli(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, argv: list[str], stdin: str
) -> int:
    """Run a command with lines on stdin (no terminal: nothing is asked interactively)."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv, store_factory=lambda: store)


def test_the_local_database_is_created_in_the_chosen_directory_before_anything_connects(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[tuple[int, str | None, str]] = []
    monkeypatch.setattr(local, "up", lambda port, pw, data: started.append((port, pw, data)))
    monkeypatch.setattr(local, "listening", lambda port, timeout_s=0.5: False)
    data = tmp_path / "pgdata"
    argv = ["setup", "--database", "docker", "--data-dir", str(data), "--port", "5599"]
    # The stub starts no server, so the command ends when it tries to connect -- after the
    # credential was stored and the container was asked for, which is what is checked here.
    assert run_cli(monkeypatch, store, argv, "admin-pw\nadmin-pw\nsess\nsess\n") == 1
    assert "the local database is not running" in capsys.readouterr().err

    owner = Target.parse(store.load("admin", "admin-pw"))
    assert owner.variant == "docker" and owner.docker == local.DockerBlock(
        port=5599, data=str(data)
    )
    assert isinstance(owner.auth, PasswordAuth) and len(owner.auth.password) >= 43
    assert started == [(5599, owner.auth.password, str(data))]
    assert (data / "pg").is_dir()
    assert "session" not in store


def test_setup_without_a_terminal_names_the_option_it_needs(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["setup", "--database", "server"], "") == 1
    assert "--host is needed" in capsys.readouterr().err
    argv = ["setup", "--database", "rds-iam", "--host", "x.eu-west-1.rds.amazonaws.com"]
    assert run_cli(monkeypatch, store, [*argv, "--user", "calu_admin"], "") == 1
    assert "--aws-profile is needed" in capsys.readouterr().err
    assert store.names() == []


def test_db_up_takes_port_and_directory_from_the_stored_record(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`calu db up --port` after a first start on another port started a second, empty
    database. The record decides now, and a contradicting option is refused."""
    started: list[tuple[int, str | None, str]] = []
    monkeypatch.setattr(local, "up", lambda port, pw, data: started.append((port, pw, data)))
    data = str(local.prepare_data_dir(tmp_path / "db"))
    store.save("admin", local.owner_target("owner-pw", 5599, data).dump(), "admin-pw")

    assert run_cli(monkeypatch, store, ["db", "up"], "admin-pw\n") == 0
    assert started == [(5599, "owner-pw", data)], "nothing initialized yet: the first start"
    (Path(data) / "pg" / "18").mkdir()
    assert run_cli(monkeypatch, store, ["db", "up"], "admin-pw\n") == 0
    assert started[-1] == (5599, None, data), "an existing cluster gets no password"
    assert run_cli(monkeypatch, store, ["db", "up", "--port", "5433"], "admin-pw\n") == 1
    assert "was set up on port 5599" in capsys.readouterr().err

    store.save("admin", "postgresql://me:pw@db.example.org/calu", None)
    assert run_cli(monkeypatch, store, ["db", "up"], "") == 1
    assert "not a local one that calu starts" in capsys.readouterr().err


def test_calu_run_starts_a_stopped_local_database_after_the_password(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    order: list[str] = []
    owner = local.owner_target("owner-pw", 5599, str(tmp_path / "db"))
    session = owner.for_role("calu_session", PasswordAuth(password="session-db-pw"))
    store.save("session", session.dump(), "session-pw")

    def ensure(block: local.DockerBlock) -> bool:
        order.append(f"docker {block.port} {block.data}")
        return True

    async def fake_session(url: bytearray, options: object) -> int:
        order.append(f"session as {Target.parse(url).user}")
        return 0

    monkeypatch.setattr(local, "ensure_running", ensure)
    monkeypatch.setattr(cli, "run_session", fake_session)
    monkeypatch.setattr(cli, "harden_process", lambda: order.append("hardened"))
    assert run_cli(monkeypatch, store, ["run", str(tmp_path), "--batch", "-p", "x"], "wrong\n") == 1
    assert order == ["hardened"], "a wrong password starts nothing"
    order.clear()
    assert (
        run_cli(monkeypatch, store, ["run", str(tmp_path), "--batch", "-p", "x"], "session-pw\n")
        == 0
    )
    assert order == ["hardened", f"docker 5599 {tmp_path / 'db'}", "session as calu_session"]
    assert "started the local database on 127.0.0.1:5599" in capsys.readouterr().out

    # A server that exists already is never started by calu.
    order.clear()
    store.save("session", "postgresql://calu_session:pw@db.example.org/calu", "session-pw")
    assert (
        run_cli(monkeypatch, store, ["run", str(tmp_path), "--batch", "-p", "x"], "session-pw\n")
        == 0
    )
    assert order == ["hardened", "session as calu_session"]
