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


async def test_a_setup_without_passwords_never_publishes_a_typed_password_and_is_taken_back(
    db_url: str,
    store: CredentialStore,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D228, as root (the tests' uid stands for it): converting a setup that has an admin
    password publishes a generated password, never the typed one; a later setup without the
    option gives the role a new password, so the published one stops working, and the place
    file holds no secret again."""
    import json

    from calu.db import place
    from calu.setup import is_admin_password

    place_file = tmp_path / "state" / "database.json"
    monkeypatch.setenv("CALU_PLACE_FILE", str(place_file))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_only_root_writes", lambda path: True)
    really_read = place.read
    monkeypatch.setattr(place, "read", lambda: really_read(None, os.getuid()))
    store.save("admin", db_url, "typed-admin-pw")
    owner = Target.parse(db_url)

    def setup(*options: str, typed: str = "") -> int:
        """Run `calu setup` with the given options and stdin."""
        monkeypatch.setattr("sys.stdin", io.StringIO(typed))
        return cli.main(["setup", *options], store_factory=lambda: cast(CredentialStore, store))

    code = await anyio.to_thread.run_sync(
        lambda: setup("--renew-session", "--no-password", typed="typed-admin-pw\n")
    )
    assert code == 0, capsys.readouterr()
    published = json.loads(place_file.read_text())["admin_password"]
    assert published != "typed-admin-pw" and "typed-admin-pw" not in place_file.read_text()
    assert await is_admin_password(owner, published)
    assert not await is_admin_password(owner, "typed-admin-pw")

    # Without the option: nothing is asked (the entries have no password any more), the role
    # gets a new password that only the store holds, and the file loses the secret.
    code = await anyio.to_thread.run_sync(setup)
    said = capsys.readouterr()
    assert code == 0, said
    assert "the one a setup with --no-password had published stops working" in said.out
    assert "admin_password" not in json.loads(place_file.read_text())
    assert not await is_admin_password(owner, published), "the published login was revoked"
    kept = Target.parse(store.load("calu_admin"))
    assert await is_admin_password(owner, getattr(kept.auth, "password", ""))
    assert "`calu setup --set-passwords` puts typed ones back" in said.out

    # The way back to typed passwords (found missing in the root box): both entries are
    # encrypted again, the role takes the new admin password, the generated login is dropped
    # from the store, and the session login is the same one as before.
    session_before = bytes(store.load("session"))
    code = await anyio.to_thread.run_sync(
        lambda: setup("--set-passwords", typed="new-admin\nnew-admin\nnew-sess\nnew-sess\n")
    )
    said = capsys.readouterr()
    assert code == 0, said
    assert store.needs_password("admin") and store.needs_password("session")
    assert bytes(store.load("session", "new-sess")) == session_before
    assert Target.parse(store.load("admin", "new-admin")).user == owner.user
    assert "calu_admin" not in store
    assert await is_admin_password(owner, "new-admin")
    assert not await is_admin_password(owner, getattr(kept.auth, "password", ""))
    assert "admin_password" not in json.loads(place_file.read_text())
    # With passwords in place the option changes them: the old ones are asked first.
    code = await anyio.to_thread.run_sync(
        lambda: setup(
            "--set-passwords", typed="new-admin\nthird\nthird\nnew-sess\nfourth\nfourth\n"
        )
    )
    assert code == 0, capsys.readouterr()
    assert bytes(store.load("session", "fourth")) == session_before
    assert await is_admin_password(owner, "third")
    assert not await is_admin_password(owner, "new-admin")
    # Together with --no-password it is refused before anything is asked.
    assert await anyio.to_thread.run_sync(lambda: setup("--set-passwords", "--no-password")) == 1
    assert "contradict each other" in capsys.readouterr().err


async def test_a_blank_admin_password_is_accepted_and_setup_says_what_it_costs(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blank admin password protects the entry; the role `calu_admin` gets no login from it.

    Run in the root box: the report said "the `admin` credential has no password and none was
    typed", although the entry has one (a blank one) and it was typed.
    """
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, b"")
    monkeypatch.setattr("sys.stdin", io.StringIO("\nsession-pw\nsession-pw\n"))
    code = await anyio.to_thread.run_sync(
        lambda: cli.main(
            ["setup", "--renew-session"], store_factory=lambda: cast(CredentialStore, store)
        )
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert store.needs_password("admin") is True
    assert "the admin password is blank and PostgreSQL accepts no blank password" in printed
    assert "none was typed" not in printed


async def test_calu_setup_stores_the_sandbox_choice_and_keeps_it_on_a_rerun(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Requirement 42: `--no-sandbox` is stored in the global settings; a later setup without
    a terminal and without the option leaves it as it is and repeats the recommendation."""
    from calu import admin

    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)  # without a password: the recommendation is not met
    monkeypatch.setattr("sys.stdin", io.StringIO("session-pw\nsession-pw\n"))

    def setup(*options: str) -> int:
        return cli.main(["setup", *options], store_factory=lambda: cast(CredentialStore, store))

    assert await anyio.to_thread.run_sync(setup, "--renew-session", "--no-sandbox") == 0
    said = capsys.readouterr()
    assert "Claude Code's sandbox: off (stored)" in said.out
    assert "strongly recommended" in said.err and "without a password: admin" in said.err

    async def stored() -> object:
        db = Db(await connect(db_url))
        try:
            return (await admin.settings(db))["claude"]["sandbox"]["enabled"]
        finally:
            await db.aclose()

    assert await stored() is False
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert await anyio.to_thread.run_sync(setup) == 0
    said = capsys.readouterr()
    assert "Claude Code's sandbox: off (as stored;" in said.out
    assert "strongly recommended" in said.err
    assert await stored() is False
