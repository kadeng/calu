"""Checks of `calu setup`, `run`, `start`, `web`, `db up` and `credential set` that need no
database: option checks that come before any password or re-execution, and what the commands
store, remove or print around a refusal or an interrupt."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from calu import cli, logins, session_requests
from calu.credentials import CredentialStore
from calu.db import local as local_db
from calu.db.target import PasswordAuth, Target, from_fields
from calu.setup import SetupRefused

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


def _refusing_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `calu setup` connect to a fake database whose preflight refuses the setup.

    Side effects: replaces `cli.open_stored`, `cli.Db`, `cli._has_checker_types` and
    `cli.setup_database` for the test; nothing connects.

    Args:
        monkeypatch: Used for the replacements.
    """

    async def fake_open(target: Any, *args: Any) -> FakeConn:
        """Return a fake connection.

        Args:
            target: The login (ignored).
            *args: Further arguments (ignored).

        Returns:
            The connection.
        """
        return FakeConn()

    async def fresh(db: Any) -> bool:
        """Say that the database holds no checker types yet.

        Args:
            db: The connection (ignored).

        Returns:
            False.
        """
        return False

    async def refuse(conn: Any, model: str, update_outdated: bool = True) -> str:
        """Refuse the setup as `calu.setup.preflight` would.

        Args:
            conn: The connection (ignored).
            model: Ignored.
            update_outdated: Ignored.

        Raises:
            SetupRefused: Always.
        """
        raise SetupRefused("- owner may not create roles")

    monkeypatch.setattr(cli, "open_stored", fake_open)
    monkeypatch.setattr(cli, "Db", lambda conn: None)
    monkeypatch.setattr(cli, "_has_checker_types", fresh)
    monkeypatch.setattr(cli, "setup_database", refuse)


def test_a_refused_setup_of_a_new_database_stores_no_user_login(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """`db-admin` is kept for the next try; `user` is stored only after a setup succeeded."""
    monkeypatch.setattr(cli, "_setup_server", lambda args, store: SERVER)
    _refusing_database(monkeypatch)

    argv = ["setup", "--database", "server"]
    assert run_cli(monkeypatch, store, argv, "upw\nupw\n") == 1
    said = capsys.readouterr()
    assert logins.choose_intro(False) in said.out
    assert "cannot be set up in db.example.org:5432/calu" in said.err
    assert store.names() == [logins.DB_ADMIN]
    assert Target.parse(store.load(logins.DB_ADMIN, "upw")) == SERVER
    assert cli._typed == {}, "the typed user password is forgotten after the setup"

    # The second run opens the stored `db-admin` with the user password, asked once.
    assert run_cli(monkeypatch, store, ["setup"], "upw\n") == 1
    assert "cannot be set up" in capsys.readouterr().err
    assert run_cli(monkeypatch, store, ["setup"], "") == 1
    assert logins.NOT_ENTERED in capsys.readouterr().err


def test_setup_refuses_a_machine_that_has_only_the_user_login(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `user` login of a database set up elsewhere must not be hidden by a new database."""
    store.save(logins.USER, SERVER.for_role("calu_admin", PasswordAuth(password="u")).dump(), None)
    for argv in (["setup"], ["setup", "--database", "docker"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        said = capsys.readouterr().err
        assert "only the user login" in said and "`calu credential set db-admin`" in said
    assert store.names() == [logins.USER]


OLD_OWNER = Target.parse(b"postgresql://calu_owner:own@db.example.org:5432/calu")
"""The owner login of a store written before D265 (its `admin` entry)."""


def _old_store(store: CredentialStore) -> None:
    """Fill `store` with the entries of before D265; `admin` has the password "old-pw".

    Args:
        store: The empty store.
    """
    store.save("admin", OLD_OWNER.dump(), b"old-pw")
    store.save("session", b"postgresql://calu_session:s@db.example.org/calu", b"old-pw")
    store.save("operator", b"postgresql://calu_operator:o@db.example.org/calu", None)


def _setup_steps_pass(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Let the database steps of `calu setup` succeed without a database.

    Side effects: replaces `cli._setup_steps` and `cli.missing_sandbox_tools` for the test.

    Args:
        monkeypatch: Used for the replacements.

    Returns:
        The user passwords the steps were given, in order.
    """
    given: list[str] = []

    async def steps(
        args: Any, store: Any, owner: Any, password: str, models: Any
    ) -> tuple[str, str, bool]:
        """Report success; `calu_admin` gets the user password, or "gen" for an empty one.

        Args:
            args: Ignored.
            store: Ignored.
            owner: Ignored.
            password: The user password.
            models: Ignored.

        Returns:
            (report, the password of `calu_admin`, the sandbox is on).
        """
        given.append(password)
        return "set up", password or "gen", True

    monkeypatch.setattr(cli, "_setup_steps", steps)
    monkeypatch.setattr(cli, "missing_sandbox_tools", lambda: [])
    return given


def test_setup_converts_an_old_store_and_keeps_the_old_admin_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (5): one prompt; without a terminal the offer to keep the password is taken."""
    _old_store(store)
    given = _setup_steps_pass(monkeypatch)
    assert run_cli(monkeypatch, store, ["setup"], "old-pw\n") == 0
    assert given == ["old-pw"]
    assert sorted(store.names()) == sorted([logins.USER, logins.DB_ADMIN])
    assert Target.parse(store.load(logins.DB_ADMIN, "old-pw")) == OLD_OWNER
    user = Target.parse(store.load(logins.USER, "old-pw"))
    assert user == cli.user_login(OLD_OWNER, "old-pw")
    out = capsys.readouterr().out
    assert "removed the logins of the earlier calu: admin, session, operator" in out


def test_setup_converts_an_old_store_with_a_new_password_when_asked_to(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a terminal "no" to the offer asks a new user password (twice)."""
    _old_store(store)
    given = _setup_steps_pass(monkeypatch)
    stdin = io.StringIO("")
    monkeypatch.setattr(stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdin", stdin)
    typed = ["old-pw", "new-pw", "new-pw"]
    prompts: list[str] = []

    def getpass(prompt: str) -> str:
        """Answer the hidden prompts in order.

        Args:
            prompt: The prompt, recorded.

        Returns:
            The next answer.
        """
        prompts.append(prompt)
        return typed.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    monkeypatch.setattr("builtins.input", lambda shown: "no")
    assert cli.main(["setup"], store_factory=lambda: store) == 0
    assert prompts == [logins.OLD_ADMIN_PROMPT, logins.NEW_PROMPT, logins.REPEAT_PROMPT]
    assert given == ["new-pw"]
    assert Target.parse(store.load(logins.USER, "new-pw")).user == "calu_admin"
    assert "admin" not in store
    assert logins.choose_intro(False) in capsys.readouterr().out


def test_setup_of_an_old_store_with_no_password_stores_both_logins_without_one(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    _old_store(store)
    given = _setup_steps_pass(monkeypatch)
    assert run_cli(monkeypatch, store, ["setup", "--no-password"], "old-pw\n") == 0
    assert given == [""]
    assert not store.needs_password(logins.USER) and not store.needs_password(logins.DB_ADMIN)
    assert Target.parse(store.load(logins.USER)) == cli.user_login(OLD_OWNER, "gen")
    assert logins.EMPTY_MEANS in capsys.readouterr().err


def test_setup_cannot_convert_an_old_store_without_the_owner_login(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.save("operator", b"postgresql://calu_operator:o@db.example.org/calu", None)
    assert run_cli(monkeypatch, store, ["setup"], "") == 1
    assert logins.old_logins(False) in capsys.readouterr().err
    assert store.names() == ["operator"]


def test_main_forgets_a_user_password_of_an_earlier_command(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """`_typed` lives in the module, so a second `main` in one process must start empty."""
    cli._typed["user"] = "earlier"
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert cli._typed == {}


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--database", "docker", "--host", "h"], "--host does not apply to the database docker"),
        (["--database", "server", "--data-dir", "/d"], "--data-dir does not apply"),
        (["--database", "server", "--region", "r"], "--region does not apply"),
        (["--database", "rds-iam", "--sslmode", "require"], "--sslmode does not apply"),
        (["--database", "docker", "--set-password", "--no-password"], "contradict each other"),
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
    """With a `db-admin` entry (or an old `admin` one) a new database's options are refused.

    Empty stdin: a password prompt would end with `logins.NOT_ENTERED` instead.
    """
    store.save(logins.DB_ADMIN, SERVER.dump(), b"upw")
    assert run_cli(monkeypatch, store, ["setup", "--port", "5433"], "") == 1
    assert "--port describes a new database" in capsys.readouterr().err
    assert run_cli(monkeypatch, store, ["setup", "--database", "server"], "") == 1
    assert "a database is set up already" in capsys.readouterr().err
    store.remove(logins.DB_ADMIN)
    store.save("admin", SERVER.dump(), b"apw")
    assert run_cli(monkeypatch, store, ["setup", "--host", "h"], "") == 1
    assert "--host describes a new database" in capsys.readouterr().err


def test_db_up_compares_the_data_directory_as_a_normalised_path(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`DIR/./db/` and `DIR/db` are the same directory; another one is still refused."""
    data = str(tmp_path / "db")
    store.save(logins.DB_ADMIN, local_db.owner_target("o", 5544, data).dump(), b"apw")
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

    def herd_value(store: Any, work: Any, may_ask: bool = True) -> Any:
        """Run `work` with the fake connection; Ctrl-C is simulated by `wait`.

        Args:
            store: The store (ignored).
            work: The coroutine function.
            may_ask: Ignored.

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
    for name in cli.LOGIN_ENTRIES:
        assert run_cli(monkeypatch, store, ["credential", "set", name], "no = string\n") == 1
        assert f"credential {name!r} not stored" in capsys.readouterr().err
    assert store.names() == []
    assert run_cli(monkeypatch, store, ["credential", "set", "other"], "x\npw\npw\n") == 0
    assert store.names() == ["other"]


def test_a_claude_cli_model_is_refused_as_a_checker_model() -> None:
    """D258: checkers run only through Pydantic AI; the `claude` CLI runs the coding agent."""
    with pytest.raises(cli.CliError, match="no longer runs checkers"):
        cli._chosen_model("claude:haiku", None)
    assert cli._chosen_model("anthropic:claude-haiku-5-5", "low") == (
        "anthropic:claude-haiku-5-5",
        "low",
        "as given",
    )


def test_serve_with_web_hands_its_child_the_login_through_a_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D268: the child `calu web` gets the login through an inherited pipe (never the
    command line or the environment), is told `--user` when calu runs as root for a user,
    `--login-typed` when the password was typed, and is asked to die with its parent."""
    import os
    import subprocess

    from calu.launch import RunAs

    seen: dict[str, object] = {}

    class FakeChild:
        pid = 4242

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def popen(argv: list[str], pass_fds: tuple[int, ...], preexec_fn: object) -> FakeChild:
        seen.update(argv=argv, fds=pass_fds, preexec=preexec_fn)
        seen["copy"] = os.dup(pass_fds[0])  # the parent closes its end right after this
        return FakeChild()

    monkeypatch.setattr(subprocess, "Popen", popen)
    login = bytearray(b"postgresql://calu_admin:secret@db.example:5432/calu")
    child = cli._start_web_child(
        8811, login, True, RunAs(uid=1000, gid=1000, home="/h", user="ada")
    )

    assert child.pid == 4242
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[2:6] == ["calu", "web", "--port", "8811"]
    assert "--login-typed" in argv and argv[-2:] == ["--user", "ada"]
    assert "secret" not in " ".join(argv), "the login is not on the command line"
    assert seen["preexec"] is cli._die_with_parent
    read = cli._read_login_fd(int(str(seen["copy"])))
    assert bytes(read) == bytes(login)
