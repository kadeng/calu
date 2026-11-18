"""`calu setup` through the command line, against an empty database (D121).

The `setup` **function** is called by nearly every database test; the **command** was called by
none, so its own wiring was untested -- the two password prompts, the order of the steps (the
session credential is stored only after the database accepted the setup), the report it prints,
and `--checker-model` reaching the rows it writes. It is the second command a new user runs.
"""

import io
import os
from typing import cast

import anyio
import pytest

from calu import cli, herd
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.db.connection import open_stored
from calu.db.target import Target
from calu.store.config import ConfigStore

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_calu_setup_fills_an_empty_database_and_stores_the_session_credential(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One command on an empty database: migrations, checkers, rules, and a credential."""
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)  # file-mode protection, so only the session is asked for
    monkeypatch.setattr("sys.stdin", io.StringIO("session-pw\nsession-pw\n"))

    # `cli.main` calls `asyncio.run`, which refuses to start inside a running loop, so the
    # command runs in a thread with a loop of its own -- as it does in reality.
    code = await anyio.to_thread.run_sync(
        lambda: cli.main(
            ["setup", "--renew-session", "--checker-model", "claude:haiku"],
            store_factory=lambda: cast(CredentialStore, store),
        )
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "migrations applied" in printed and "checker types added" in printed
    assert "stored credential 'session'" in printed

    # The session credential is readable with the password that was typed, and with no other.
    assert store.needs_password("session") is True
    secret = bytes(store.load("session", "session-pw"))
    # The session gets its **own** login, not the admin one it was set up with (D48).
    assert b'"user": "calu_session"' in secret and b"calu_owner" not in secret
    # ... and only where the database is travels with it, not the owner's other parameters.
    login = Target.parse(secret)
    assert login.variant == "server" and sorted(login.conninfo) == [
        "dbname",
        "host",
        "port",
        "user",
    ]

    conn = await connect(db_url)
    try:
        db = Db(conn)
        settings = await ConfigStore(db, tmp_path, None).load()
        # `--checker-model` reached every checker row, which is what the flag is for.
        assert settings.config.command_checker.model == "claude:haiku"
        assert settings.config.edit_checker.model == "claude:haiku"
        assert [row.label for row in await herd.list_tokens(db)] == [], "no clients yet"
        assert settings.rules, "the default rule lists are in"
        assert "command" in settings.checkers and "edit" in settings.checkers
    finally:
        await conn.close()


async def test_calu_setup_without_passwords_asks_nothing_and_stores_the_admin_login(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D228: `calu setup --no-password` in a store only root writes (here: the tests' own
    uid stands for root) asks no password; the logins are in the store, protected by its
    file mode, and the stored `calu_admin` login works. In any other store the session
    password stays."""
    from calu.setup import ADMIN_ROLE

    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # a prompt would fail on this
    really = cli._only_root_writes
    monkeypatch.setattr(cli, "_only_root_writes", lambda path: really(path, os.getuid()))

    code = await anyio.to_thread.run_sync(
        lambda: cli.main(
            ["setup", "--renew-session", "--no-password"],
            store_factory=lambda: cast(CredentialStore, store),
        )
    )
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "stored credential 'calu_admin' (no password)" in captured.out
    assert "WARNING: no password protects the logins" in captured.err  # not root here
    for name in ("admin", "session", "calu_admin"):
        assert store.needs_password(name) is False
    login = Target.parse(store.load("calu_admin"))
    assert login.user == ADMIN_ROLE
    conn = await open_stored(login)
    try:
        assert await Db(conn).scalar("SELECT current_user") == ADMIN_ROLE
    finally:
        await conn.close()
    # What changes settings from the command line uses that login and asks nothing.
    assert Target.parse(cli._admin_secret(store)).user == ADMIN_ROLE
    # `calu run` accepts the session entry without a password from such a store ...
    assert Target.parse(cli._session_login(store)).user == "calu_session"
    # ... and from no other: a store that somebody else could have written.
    monkeypatch.setattr(cli, "_only_root_writes", really)
    with pytest.raises(cli.CliError, match="stored without a password"):
        cli._session_login(store)
    # There, `--no-password` keeps asking for the session password.
    monkeypatch.setattr("sys.stdin", io.StringIO("session-pw\nsession-pw\n"))
    code = await anyio.to_thread.run_sync(
        lambda: cli.main(
            ["setup", "--renew-session", "--no-password"],
            store_factory=lambda: cast(CredentialStore, store),
        )
    )
    assert code == 0 and "the session password stays" in capsys.readouterr().out
    assert store.needs_password("session") is True


async def test_a_mismatched_session_password_leaves_the_database_alone(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The password is asked **before** the database is touched, so a typo changes nothing.

    The order matters for a reason a user can feel: `calu setup` on a database that already
    holds rules must not rewrite them and then fail on the credential.
    """
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO("one\ntwo\n"))

    assert cli.main(["setup"], store_factory=lambda: cast(CredentialStore, store)) == 1, (
        capsys.readouterr().out
    )
    assert "do not match" in capsys.readouterr().err
    assert store.names() == ["admin"], "nothing was stored"

    conn = await connect(db_url)
    try:
        cur = await conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'checker_types'"
        )
        row = await cur.fetchone()
        assert row == (0,), "the database was not touched"
    finally:
        await conn.close()


async def test_project_add_records_the_directory_it_was_given(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ga3: `calu project add DIR` is what lets `calu run` in DIR find its project."""
    import socket

    from calu.setup import setup
    from calu.store.projects import project_for_directory

    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)
    project_dir = tmp_path / "demo-src"
    project_dir.mkdir()

    def add(*argv: str) -> int:
        return cli.main(
            ["project", "add", *argv], store_factory=lambda: cast(CredentialStore, store)
        )

    assert await anyio.to_thread.run_sync(lambda: add(str(project_dir), "--name", "demo")) == 0
    assert f"`calu run` in {project_dir} or below uses it" in capsys.readouterr().out
    # A project named without a directory is not tied to wherever the command happened to run.
    assert await anyio.to_thread.run_sync(lambda: add("--name", "loose")) == 0
    assert "use it with `calu run --project loose`" in capsys.readouterr().out

    conn = await connect(db_url)
    try:
        db = Db(conn)
        host = socket.gethostname()
        assert await project_for_directory(db, project_dir / "src", host) == "demo"
        cur = await conn.execute("SELECT directory FROM projects WHERE name = 'loose'")
        assert await cur.fetchone() == (None,)
    finally:
        await conn.close()
