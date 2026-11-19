"""Checks of `calu setup`, `run`, `start`, `web`, `db up` and `credential set` that need no
database: option checks that come before any password or re-execution, and what the commands
store, remove or print around a refusal or an interrupt."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from calu import cli, session_requests
from calu.credentials import CredentialStore
from calu.db import local as local_db
from calu.db.target import PasswordAuth, Target, from_fields

SERVER = from_fields("db.example.org", 5432, "calu", "owner", PasswordAuth(password="o"))
"""An owner login of an existing server, as `_setup_server` returns it."""


def run_cli(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, argv: list[str], stdin: str
) -> int:
    """Run `calu` with `stdin` as the input of its prompts.

    Args:
        monkeypatch: Used to replace stdin.
        store: The credential store the command uses.
        argv: The arguments.
        stdin: The lines the prompts read (stdin is no terminal, so nothing is echoed).

    Returns:
        The exit code.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv, store_factory=lambda: store)


class FakeConn:
    """A connection that `cmd_setup` opens and closes and nothing else uses."""

    async def close(self) -> None:
        """Do nothing, as closing a real connection would leave nothing to check."""


def test_a_refused_setup_of_a_new_database_does_not_keep_the_old_session_entry(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-run must renew the session login, not report the old database's entry as kept."""
    store.save("session", Target.parse("host=old dbname=calu user=calu_session").dump(), b"s")
    monkeypatch.setattr(cli, "_setup_server", lambda args, store: SERVER)

    async def fake_open(target: Any, *args: Any) -> FakeConn:
        """Return a fake connection.

        Args:
            target: The login (ignored).
            *args: Further arguments (ignored).

        Returns:
            The connection.
        """
        return FakeConn()

    async def can_log_in(conn: Any) -> bool:
        """Say that `calu_session` has a login, so the setup refuses.

        Args:
            conn: The connection (ignored).

        Returns:
            True.
        """
        return True

    monkeypatch.setattr(cli, "open_stored", fake_open)
    monkeypatch.setattr(cli, "session_can_log_in", can_log_in)
    monkeypatch.setattr(cli, "Db", lambda conn: None)

    # admin password twice, session password twice
    argv = ["setup", "--database", "server"]
    assert run_cli(monkeypatch, store, argv, "apw\napw\nspw\nspw\n") == 1
    said = capsys.readouterr()
    assert "--renew-session" in said.err
    assert "removed the stored 'session' credential" in said.out
    assert "admin" in store and "session" not in store
    assert cli._typed == {}, "the typed admin password is forgotten after the setup"

    # The second run finds no session entry, so it renews (and is refused the same way)
    # instead of printing "session login kept".
    assert run_cli(monkeypatch, store, ["setup"], "apw\nspw\nspw\n") == 1
    said = capsys.readouterr()
    assert "--renew-session" in said.err and "session login kept" not in said.out


def test_main_forgets_an_admin_password_of_an_earlier_command(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """`_typed` lives in the module, so a second `main` in one process must start empty."""
    cli._typed["admin"] = "earlier"
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert cli._typed == {}


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--database", "docker", "--host", "h"], "--host does not apply to the database docker"),
        (["--database", "server", "--data-dir", "/d"], "--data-dir does not apply"),
        (["--database", "server", "--region", "r"], "--region does not apply"),
        (["--database", "rds-iam", "--sslmode", "require"], "--sslmode does not apply"),
        (["--database", "server", "--session-auth", "password"], "are for the database rds-iam"),
        (["--database", "docker", "--session-aws-profile", "p"], "are for the database rds-iam"),
        (
            ["--database", "rds-iam", "--session-auth", "password", "--session-aws-profile", "p"],
            "is for --session-auth iam",
        ),
    ],
)
def test_setup_refuses_an_option_the_chosen_database_would_ignore(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    """Refused before any question, password or connection; nothing is stored."""
    assert run_cli(monkeypatch, store, ["setup", *argv], "") == 1
    assert message in capsys.readouterr().err
    assert store.names() == []


def test_setup_of_a_stored_database_refuses_options_of_a_new_one(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """With an `admin` entry the options of a new database would be ignored silently."""
    store.save("admin", SERVER.dump(), b"apw")
    # Empty stdin: a password prompt would fail with "empty password" instead.
    assert run_cli(monkeypatch, store, ["setup", "--port", "5433"], "") == 1
    assert "--port describes a new database" in capsys.readouterr().err
    # A session option for a database without IAM is refused once the login is read.
    assert run_cli(monkeypatch, store, ["setup", "--session-auth", "iam"], "apw\n") == 1
    assert "for a database with IAM authentication" in capsys.readouterr().err


def test_db_up_compares_the_data_directory_as_a_normalised_path(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`DIR/./db/` and `DIR/db` are the same directory; another one is still refused."""
    data = str(tmp_path / "db")
    store.save("admin", local_db.owner_target("o", 5544, data).dump(), b"apw")
    started: list[tuple] = []
    monkeypatch.setattr(local_db, "initialized", lambda data: True)
    monkeypatch.setattr(local_db, "up", lambda *args: started.append(args))
    same = f"{tmp_path}/./db/"
    assert run_cli(monkeypatch, store, ["db", "up", "--data-dir", same], "apw\n") == 0
    assert started == [(5544, None, data)]
    other = str(tmp_path / "other")
    assert run_cli(monkeypatch, store, ["db", "up", "--data-dir", other], "apw\n") == 1
    assert "was set up in" in capsys.readouterr().err


def test_run_checks_its_options_before_it_may_run_again_through_sudo(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--sudo` replaces the process and asks a password; a typo must be reported first."""
    called: list[object] = []
    monkeypatch.setattr(cli, "_run_as", lambda args: called.append(args))
    monkeypatch.setattr(cli, "_client_user", lambda args: called.append(args))
    for argv, message in (
        (["run", str(tmp_path), "--sudo", "--background-timeout", "5"], "needs --batch"),
        (["run", str(tmp_path), "--sudo", "--resume", "x"], "takes a Claude session id"),
        (["start", str(tmp_path), "--sudo", "--no-attach", "--plain"], "--no-attach"),
        (["start", str(tmp_path), "--sudo", "--no-attach", "--thinking"], "--no-attach"),
        (["web", "--sudo", "--port", "70000"], "from 1 to 65535"),
        (["web", "--sudo", "--port", "0"], "from 1 to 65535"),
    ):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert message in capsys.readouterr().err
    assert called == []


def test_a_prompt_comes_from_one_place_and_a_prompt_file_is_not_empty(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--prompt` and `--prompt-file` together, or an empty file, are refused."""
    empty = tmp_path / "empty.txt"
    empty.write_text(" \n", encoding="utf-8")
    full = tmp_path / "full.txt"
    full.write_text("go", encoding="utf-8")
    for command in ("run", "start"):
        argv = [command, str(tmp_path), "-p", "x", "--prompt-file", str(full)]
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert "exclude each other" in capsys.readouterr().err
        argv = [command, str(tmp_path), "--prompt-file", str(empty)]
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert "the file is empty" in capsys.readouterr().err


def _fake_start(monkeypatch: pytest.MonkeyPatch, wait: Any, claude_id: str = "") -> list[int]:
    """Let `calu start` run against fake request functions and a fake connection.

    Args:
        monkeypatch: Used for the replacements.
        wait: Replaces `session_requests.wait_for`.
        claude_id: The Claude session id the fake `sessions` row holds.

    Returns:
        The list the fake `withdraw` appends the request ids to.
    """
    withdrawn: list[int] = []

    async def file(db: Any, request: Any) -> int:
        """File the request.

        Args:
            db: The connection (ignored).
            request: The request (ignored).

        Returns:
            The request id 5.
        """
        return 5

    async def withdraw(db: Any, request_id: int) -> bool:
        """Record the withdrawal.

        Args:
            db: The connection (ignored).
            request_id: The request.

        Returns:
            True: the request was still pending.
        """
        withdrawn.append(request_id)
        return True

    async def one(query: str, params: Any) -> tuple[str, str]:
        """Answer the query for the session's address and Claude session id.

        Args:
            query: The SQL (ignored).
            params: Its parameters (ignored).

        Returns:
            The address and the Claude session id.
        """
        return ("ws://127.0.0.1:1", claude_id)

    def herd_value(store: Any, work: Any, may_ask: bool = True, role: Any = None) -> Any:
        """Run `work` with the fake connection; Ctrl-C is simulated by `wait`.

        Args:
            store: The store (ignored).
            work: The coroutine function.
            may_ask: Ignored.
            role: Ignored.

        Returns:
            What `work` returned.
        """
        return asyncio.run(work(SimpleNamespace(one=one)))

    monkeypatch.setattr(session_requests, "file", file)
    monkeypatch.setattr(session_requests, "withdraw", withdraw)
    monkeypatch.setattr(session_requests, "wait_for", wait)
    monkeypatch.setattr(cli, "_herd_value", herd_value)
    return withdrawn


def test_ctrl_c_while_start_waits_withdraws_the_request(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A request left pending would start a session for a client that has gone."""

    async def interrupted(db: Any, request_id: int) -> Any:
        """Be cancelled as `asyncio.run` cancels its task on Ctrl-C.

        Args:
            db: The connection (ignored).
            request_id: The request (ignored).

        Raises:
            asyncio.CancelledError: Always.
        """
        raise asyncio.CancelledError

    withdrawn = _fake_start(monkeypatch, interrupted)
    with pytest.raises(asyncio.CancelledError):
        cli.cmd_start(cli.build_parser().parse_args(["start", str(tmp_path)]), store)
    assert withdrawn == [5]
    assert "withdrew the session request" in capsys.readouterr().err


@pytest.mark.parametrize(("claude_id", "shown"), [("", "not known yet"), ("abc", "abc")])
def test_start_without_attach_prints_the_claude_id_only_when_it_is_known(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    claude_id: str,
    shown: str,
) -> None:
    """Never `[session] ` with nothing after it."""

    async def started(db: Any, request_id: int) -> Any:
        """Report the session as started.

        Args:
            db: The connection (ignored).
            request_id: The request (ignored).

        Returns:
            A finished request.
        """
        return SimpleNamespace(state="started", session_id="sid", message="")

    _fake_start(monkeypatch, started, claude_id)
    argv = ["start", str(tmp_path), "--no-attach"]
    assert run_cli(monkeypatch, store, argv, "") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("[session] ") and shown in first


def test_credential_set_refuses_a_login_entry_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo is reported now, not at the next command; other entries are not checked."""
    for name in ("admin", "session", "operator"):
        assert run_cli(monkeypatch, store, ["credential", "set", name], "no = string\n") == 1
        assert f"credential {name!r} not stored" in capsys.readouterr().err
    assert store.names() == []
    assert run_cli(monkeypatch, store, ["credential", "set", "other"], "x\npw\npw\n") == 0
    assert store.names() == ["other"]
