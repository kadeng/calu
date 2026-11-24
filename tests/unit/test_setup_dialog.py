"""`calu setup` and `calu run` for the local Docker database, without Docker.

See agent/records/PLAN.md D205.
"""

import io
from pathlib import Path

import pytest

from calu import cli, logins
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
    assert run_cli(monkeypatch, store, argv, "user-pw\nuser-pw\n") == 1
    said = capsys.readouterr()
    assert logins.choose_intro(True) in said.out, "the new user password is explained first"
    assert "the local database is not running" in said.err

    owner = Target.parse(store.load(logins.DB_ADMIN, "user-pw"))
    assert owner.variant == "docker" and owner.docker == local.DockerBlock(
        port=5599, data=str(data)
    )
    assert isinstance(owner.auth, PasswordAuth) and len(owner.auth.password) >= 43
    assert started == [(5599, owner.auth.password, str(data))]
    assert (data / "pg").is_dir()
    assert store.names() == [logins.DB_ADMIN], "`user` is stored only after a setup succeeded"


def test_db_up_without_a_stored_database_creates_the_local_one_with_a_user_password(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[tuple[int, str | None, str]] = []
    monkeypatch.setattr(local, "up", lambda port, pw, data: started.append((port, pw, data)))
    data = tmp_path / "pgdata"
    argv = ["db", "up", "--data-dir", str(data), "--port", "5598"]
    assert run_cli(monkeypatch, store, argv, "user-pw\nuser-pw\n") == 0
    assert "next: calu setup" in capsys.readouterr().out
    owner = Target.parse(store.load(logins.DB_ADMIN, "user-pw"))
    assert isinstance(owner.auth, PasswordAuth)
    assert started == [(5598, owner.auth.password, str(data))]
    # A store of before D265 is not given a second database.
    store.remove(logins.DB_ADMIN)
    store.save("admin", owner.dump(), "old-pw")
    assert run_cli(monkeypatch, store, argv, "") == 1
    assert logins.old_logins(True) in capsys.readouterr().err


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
    store.save(logins.DB_ADMIN, local.owner_target("owner-pw", 5599, data).dump(), "user-pw")

    assert run_cli(monkeypatch, store, ["db", "up"], "user-pw\n") == 0
    assert started == [(5599, "owner-pw", data)], "nothing initialized yet: the first start"
    (Path(data) / "pg" / "18").mkdir()
    assert run_cli(monkeypatch, store, ["db", "up"], "user-pw\n") == 0
    assert started[-1] == (5599, None, data), "an existing cluster gets no password"
    assert run_cli(monkeypatch, store, ["db", "up", "--port", "5433"], "user-pw\n") == 1
    assert "was set up on port 5599" in capsys.readouterr().err

    store.save(logins.DB_ADMIN, "postgresql://me:pw@db.example.org/calu", None)
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
    user = owner.for_role("calu_admin", PasswordAuth(password="user-pw"))
    store.save(logins.USER, user.dump(), "user-pw")

    def ensure(block: local.DockerBlock) -> bool:
        order.append(f"docker {block.port} {block.data}")
        return True

    async def fake_session(url: bytearray, options: object) -> int:
        order.append(f"session as {Target.parse(url).user}")
        return 0

    monkeypatch.setattr(local, "ensure_running", ensure)
    monkeypatch.setattr(cli, "run_session", fake_session)
    monkeypatch.setattr(cli, "harden_process", lambda: order.append("hardened"))
    argv = ["run", str(tmp_path), "--batch", "-p", "x"]
    assert run_cli(monkeypatch, store, argv, "wrong\n" * logins.MAX_ATTEMPTS) == 1
    assert order == ["hardened"], "a wrong password starts nothing"
    assert logins.wrong(0) in capsys.readouterr().err
    order.clear()
    assert run_cli(monkeypatch, store, argv, "user-pw\n") == 0
    assert order == ["hardened", f"docker 5599 {tmp_path / 'db'}", "session as calu_admin"]
    assert "started the local database on 127.0.0.1:5599" in capsys.readouterr().out

    # A server that exists already is never started by calu.
    order.clear()
    store.save(logins.USER, "postgresql://calu_admin:pw@db.example.org/calu", "user-pw")
    assert run_cli(monkeypatch, store, argv, "user-pw\n") == 0
    assert order == ["hardened", "session as calu_admin"]


# --------------------------------------------- requirement 42: the sandbox question of setup


def _terminal(monkeypatch: pytest.MonkeyPatch, answers: str) -> None:
    """Make stdin a terminal that answers with the given lines."""
    stdin = io.StringIO(answers)
    monkeypatch.setattr(stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdin", stdin)


def test_the_sandbox_question_explains_itself_and_names_missing_programs() -> None:
    present = cli.sandbox_intro([])
    assert "bubblewrap and socat: both are installed." in present
    missing = cli.sandbox_intro(["bwrap"])
    assert "bubblewrap (bwrap) is not installed" in missing
    assert "sudo apt install bubblewrap socat" in missing and "refuses to start" in missing
    assert "bubblewrap (bwrap) and socat are not installed" in cli.sandbox_intro(["bwrap", "socat"])


def test_the_sandbox_options_answer_without_a_question(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _terminal(monkeypatch, "")
    assert cli.choose_sandbox(True, False, ["bwrap"]) is True
    assert cli.choose_sandbox(False, True, []) is False
    assert capsys.readouterr().out == ""
    assert cli.build_parser().parse_args(["setup", "--no-sandbox"]).sandbox is False
    assert cli.build_parser().parse_args(["setup", "--sandbox"]).sandbox is True
    assert cli.build_parser().parse_args(["setup"]).sandbox is None


def test_without_a_terminal_the_stored_sandbox_setting_stays(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.choose_sandbox(None, False, []) is None
    assert cli.choose_sandbox(None, None, ["bwrap", "socat"]) is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("stored", "missing", "answer", "shown_default", "chosen"),
    [
        (None, [], "", "(on)", True),  # the programs are there: on
        (None, ["socat"], "", "(off)", False),  # one is missing: off
        (False, [], "", "(off)", False),  # a second setup offers what is stored
        (True, ["bwrap"], "", "(on)", True),
        (None, [], "off", "(on)", False),
        (False, ["bwrap"], "yes", "(off)", True),
    ],
)
def test_the_sandbox_question_offers_the_stored_value_or_what_the_machine_has(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stored: bool | None,
    missing: list[str],
    answer: str,
    shown_default: str,
    chosen: bool,
) -> None:
    _terminal(monkeypatch, answer + "\n")
    assert cli.choose_sandbox(None, stored, missing) is chosen
    out = capsys.readouterr().out
    assert out.startswith("Claude Code's sandbox runs Claude's Bash commands")
    assert f"Switch Claude Code's sandbox on? on/off {shown_default}: " in out
    assert ("Stored now:" in out) == (stored is not None)


def test_an_unclear_sandbox_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _terminal(monkeypatch, "maybe\n")
    with pytest.raises(cli.CliError, match="neither on nor off"):
        cli.choose_sandbox(None, None, [])


def _settings_db(monkeypatch: pytest.MonkeyPatch, stored: dict[str, object]) -> list[object]:
    """Replace the global settings by `stored`; return the list the writes are recorded in."""
    written: list[object] = []

    async def settings(_db: object, project: str | None = None) -> dict[str, object]:
        return stored

    async def set_setting(_db: object, key: str, value: object, project: str | None = None):
        written.append((key, value))
        return {}

    monkeypatch.setattr(cli.admin, "settings", settings)
    monkeypatch.setattr(cli.admin, "set_setting", set_setting)
    return written


@pytest.mark.anyio
async def test_setup_stores_the_sandbox_answer_and_never_changes_it_unasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "missing_sandbox_tools", lambda: [])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    # No terminal, no option: nothing is written, the report says where the value comes from.
    written = _settings_db(monkeypatch, {"claude": {"sandbox": {"enabled": False}}})
    assert await cli._apply_sandbox_choice(None, None) == (  # type: ignore[arg-type]
        False,
        "Claude Code's sandbox: off (as stored; `calu setup --sandbox` or `--no-sandbox` sets it)",
    )
    written = _settings_db(monkeypatch, {})
    enabled, line = await cli._apply_sandbox_choice(None, None)  # type: ignore[arg-type]
    assert enabled and "on (the default;" in line and written == []
    # An option is stored, and the same value again is not written twice.
    enabled, line = await cli._apply_sandbox_choice(None, False)  # type: ignore[arg-type]
    assert not enabled and line == "Claude Code's sandbox: off (stored)"
    assert written == [("claude.sandbox.enabled", False)]
    written = _settings_db(monkeypatch, {"claude": {"sandbox": {"enabled": False}}})
    enabled, line = await cli._apply_sandbox_choice(None, False)  # type: ignore[arg-type]
    assert line.endswith("(as stored)") and written == []


def test_setup_warns_about_the_sandbox_only_where_it_matters(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALU_PLACE_FILE", str(tmp_path / "no-place.json"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "missing_sandbox_tools", lambda: ["bwrap"])
    (note,) = cli.sandbox_setup_notes(True, store)
    assert "sessions refuse to start" in note and "--no-sandbox" in note
    monkeypatch.setattr(cli, "missing_sandbox_tools", lambda: [])
    assert cli.sandbox_setup_notes(True, store) == []
    # Off, with both logins behind the user password: the recommendation is met.
    store.save(logins.DB_ADMIN, "postgresql://a@db/calu", "pw")
    store.save(logins.USER, "postgresql://calu_admin:pw@db/calu", "pw")
    assert cli.sandbox_setup_notes(False, store) == []
    # Off, after --no-password in one's own account: it is not.
    store.save(logins.DB_ADMIN, "postgresql://a@db/calu", None)
    store.save(logins.USER, "postgresql://calu_admin:gen@db/calu", None)
    (note,) = cli.sandbox_setup_notes(False, store)
    assert "strongly recommended" in note
    assert f"without a password: {logins.DB_ADMIN}, {logins.USER}" in note
    # As root the store is root's alone and sessions run with --sudo.
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.sandbox_setup_notes(False, store) == []
