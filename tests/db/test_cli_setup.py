"""`calu setup` through the command line, against an empty database (D121, D265).

The `setup` **function** is called by nearly every database test; the **command** was called by
none, so its own wiring was untested -- the password prompts, the order of the steps (the user
login is stored only after the database accepted the setup), the report it prints, and
`--checker-model` reaching the rows it writes. It is the second command a new user runs.

Since D265 one password, the calu user password, protects both store entries (`user`, the
login of `calu_admin`, and `db-admin`, the database owner's) and is the password of the role
`calu_admin`. These tests give the server-wide role `calu_admin` passwords; the commands run
in process, with stdin replaced and the local database's container replaced by the test
server (`local_style`).
"""

import io
import json
import os
from pathlib import Path
from typing import cast

import anyio
import pytest

from calu import cli, herd, logins
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.db import local as local_db
from calu.db import place as db_place
from calu.db.connection import open_stored
from calu.db.target import DockerBlock, PasswordAuth, Target
from calu.setup import USER_ROLE, is_user_password
from calu.store.config import ConfigStore

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def local_style(monkeypatch: pytest.MonkeyPatch, db_url: str) -> list[int]:
    """Make `calu setup --database docker` set up the test database instead of a container.

    Side effects: replaces `calu.db.local.owner_target` by one that returns the owner login
    of the test database as a record of the variant `docker` (port and data directory as
    asked), and `calu.db.local.up` by a stub that records the port and starts nothing.

    Args:
        monkeypatch: Replaces the functions for the test (also `set_restart`, a no-op).
        db_url: The owner login of the test database.

    Returns:
        The ports `up` was called with, filled in as the command runs.
    """
    test_db = Target.parse(db_url)
    assert isinstance(test_db.auth, PasswordAuth)
    started: list[int] = []

    def owner_target(_password: str, port: int, data: str, stops: bool = False) -> Target:
        """Return the test database's owner as a local database's record."""
        return Target(
            variant="docker",
            conninfo=dict(test_db.conninfo),
            auth=test_db.auth,
            docker=DockerBlock(port=port, data=data, stop_with_server=stops),
        )

    monkeypatch.setattr(local_db, "owner_target", owner_target)
    monkeypatch.setattr(local_db, "up", lambda port, _password, _data: started.append(port))
    # Never the machine's own Docker: on a developer's machine `calu-db` is their database.
    monkeypatch.setattr(local_db, "set_restart", lambda _stops: None)
    return started


def run_setup(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, argv: list[str], stdin: str
) -> int:
    """Run `calu setup ARGV` with lines on stdin (no terminal: nothing else is asked).

    `cli.main` calls `asyncio.run`, which refuses to start inside a running loop, so a test
    runs this in a thread with a loop of its own (`anyio.to_thread.run_sync`), as in reality.

    Args:
        monkeypatch: Replaces `sys.stdin`.
        store: The credential store.
        argv: The arguments after `setup`.
        stdin: What is typed, one answer per line.

    Returns:
        The exit code.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["setup", *argv], store_factory=lambda: cast(CredentialStore, store))


def docker_flags(tmp_path: Path, db_url: str) -> list[str]:
    """Return the options of a new local database: its data directory below `tmp_path`, and
    the test server's port, where calu then finds the database "running"."""
    port = Target.parse(db_url).conninfo["port"]
    return ["--database", "docker", "--data-dir", str(tmp_path / "pgdata"), "--port", port]


def stored_password(store: CredentialStore, password: str | None = None) -> str:
    """Return the password of `calu_admin` that the `user` entry holds.

    Args:
        store: The credential store.
        password: The password that opens the entry; None for an entry without one.

    Returns:
        The role's password in the stored login.
    """
    login = Target.parse(store.load(logins.USER, password))
    assert login.user == USER_ROLE and isinstance(login.auth, PasswordAuth)
    return login.auth.password


async def test_a_local_setup_stores_both_logins_under_the_one_user_password(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (1): the user password is asked once, after its explanation; it protects `user`
    and `db-admin` and is the password `calu_admin` logs in with. `--checker-model` reaches
    every checker row."""
    started = local_style(monkeypatch, db_url)
    store = CredentialStore(tmp_path / "credentials.json")
    argv = [*docker_flags(tmp_path, db_url), "--checker-model", "anthropic:claude-haiku-4-5"]
    code = await anyio.to_thread.run_sync(
        lambda: run_setup(monkeypatch, store, argv, "user-pw\nuser-pw\n")
    )
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert started == [int(Target.parse(db_url).conninfo["port"])], "the container was started"
    assert logins.choose_intro(True) in said.out
    assert "migrations applied" in said.out and "checker types added" in said.out
    assert "protected by the calu user password" in said.out
    assert logins.EMPTY_MEANS not in said.err

    assert store.names() == [logins.DB_ADMIN, logins.USER]
    assert store.needs_password(logins.DB_ADMIN) and store.needs_password(logins.USER)
    owner = Target.parse(store.load(logins.DB_ADMIN, "user-pw"))
    assert owner.variant == "docker" and owner.user == "calu_owner"
    assert stored_password(store, "user-pw") == "user-pw"
    assert await is_user_password(owner, "user-pw")
    assert not await is_user_password(owner, "a guess")
    conn = await open_stored(Target.parse(store.load(logins.USER, "user-pw")), start_local=False)
    try:
        assert await Db(conn).scalar("SELECT current_user") == USER_ROLE
    finally:
        await conn.close()

    conn = await connect(db_url)
    try:
        db = Db(conn)
        settings = await ConfigStore(db, tmp_path, None).load()
        assert settings.config.command_checker.model == "anthropic:claude-haiku-4-5"
        assert settings.config.edit_checker.model == "anthropic:claude-haiku-4-5"
        assert [row.label for row in await herd.list_tokens(db)] == [], "no clients yet"
        assert "command" in settings.checkers and "edit" in settings.checkers
    finally:
        await conn.close()

    # A second setup asks the user password once, for both entries, and keeps it.
    code = await anyio.to_thread.run_sync(lambda: run_setup(monkeypatch, store, [], "user-pw\n"))
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert "user login (calu_admin): its password is the calu user password" in said.out
    assert await is_user_password(owner, "user-pw")


async def test_an_empty_user_password_asks_nothing_and_keeps_the_generated_role_password(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (2): `--no-password` stores both entries without encryption; `calu_admin` gets a
    generated password, which a second setup keeps, so that running servers reconnect."""
    local_style(monkeypatch, db_url)
    store = CredentialStore(tmp_path / "credentials.json")
    code = await anyio.to_thread.run_sync(
        lambda: run_setup(
            monkeypatch, store, [*docker_flags(tmp_path, db_url), "--no-password"], ""
        )
    )
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert logins.choose_intro(True) not in said.out, "nothing is asked"
    assert "protected by no password (the file's mode)" in said.out
    assert f"WARNING: {logins.EMPTY_MEANS}" in said.err
    assert not store.needs_password(logins.DB_ADMIN) and not store.needs_password(logins.USER)
    generated = stored_password(store)
    assert len(generated) >= 43
    owner = Target.parse(store.load(logins.DB_ADMIN))
    assert await is_user_password(owner, generated)
    # Commands find the login without asking (stdin is empty: a prompt would fail).
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli._user_target(store).user == USER_ROLE

    code = await anyio.to_thread.run_sync(lambda: run_setup(monkeypatch, store, [], ""))
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert "the role has a generated password, kept in the store" in said.out
    assert stored_password(store) == generated
    assert await is_user_password(owner, generated)


async def test_set_password_changes_the_password_of_the_role(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (3): `--set-password` chooses a new user password; the present one is asked
    first; an empty answer removes it again. The old password stops working each time."""
    local_style(monkeypatch, db_url)
    store = CredentialStore(tmp_path / "credentials.json")

    def setup(argv: list[str], stdin: str) -> int:
        """Run `calu setup` in a thread."""
        return run_setup(monkeypatch, store, argv, stdin)

    assert (
        await anyio.to_thread.run_sync(
            lambda: setup([*docker_flags(tmp_path, db_url), "--no-password"], "")
        )
        == 0
    )
    generated = stored_password(store)
    owner = Target.parse(store.load(logins.DB_ADMIN))
    capsys.readouterr()

    # From none to one: nothing to ask first, then the new password twice.
    assert (
        await anyio.to_thread.run_sync(lambda: setup(["--set-password"], "new-pw\nnew-pw\n")) == 0
    )
    said = capsys.readouterr()
    assert logins.choose_intro(True) in said.out
    assert store.needs_password(logins.DB_ADMIN) and store.needs_password(logins.USER)
    assert stored_password(store, "new-pw") == "new-pw"
    assert await is_user_password(owner, "new-pw")
    assert not await is_user_password(owner, generated)

    # From one to another: the present one first.
    assert (
        await anyio.to_thread.run_sync(lambda: setup(["--set-password"], "new-pw\nthird\nthird\n"))
        == 0
    )
    capsys.readouterr()
    assert stored_password(store, "third") == "third"
    assert await is_user_password(owner, "third")
    assert not await is_user_password(owner, "new-pw")

    # An empty answer: no user password again, and a newly generated role password.
    assert await anyio.to_thread.run_sync(lambda: setup(["--set-password"], "third\n\n\n")) == 0
    capsys.readouterr()
    assert not store.needs_password(logins.USER) and not store.needs_password(logins.DB_ADMIN)
    again = stored_password(store)
    assert again not in ("third", generated)
    assert await is_user_password(owner, again)
    assert not await is_user_password(owner, "third")

    # Together with --no-password it is refused before anything is asked.
    assert (
        await anyio.to_thread.run_sync(lambda: setup(["--set-password", "--no-password"], "")) == 1
    )
    assert "contradict each other" in capsys.readouterr().err


async def test_a_store_of_before_d265_is_converted_to_the_two_entries(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (5): the earlier admin password opens `admin` (the owner) and is kept as the user
    password (the default answer without a terminal); `user` and `db-admin` are stored under
    it, and `admin`, `session`, `operator` and `calu_admin` are removed."""
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, "old-admin-pw")
    store.save("session", "host=127.0.0.1 user=calu_session password=x", "old-session-pw")
    store.save("operator", "host=127.0.0.1 user=calu_operator password=y", None)
    store.save("calu_admin", "host=127.0.0.1 user=calu_admin password=z", None)

    # Another command says what to do instead of using the old entries.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(cli.CliError, match="Run `calu setup` once"):
        cli._user_target(store)

    code = await anyio.to_thread.run_sync(
        lambda: run_setup(monkeypatch, store, [], "old-admin-pw\n")
    )
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert "removed the logins of the earlier calu: admin, session, operator, calu_admin" in (
        said.out
    )
    assert store.names() == [logins.DB_ADMIN, logins.USER]
    owner = Target.parse(store.load(logins.DB_ADMIN, "old-admin-pw"))
    assert owner.user == "calu_owner"
    assert stored_password(store, "old-admin-pw") == "old-admin-pw"
    assert await is_user_password(owner, "old-admin-pw")


async def test_a_mismatched_new_password_leaves_the_database_alone(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The password is asked **before** the database is touched, so a typo changes nothing.

    The order matters for a reason a user can feel: `calu setup` on a database that already
    holds rules must not rewrite them and then fail on the credential.
    """
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.DB_ADMIN, db_url, None)
    code = await anyio.to_thread.run_sync(
        lambda: run_setup(monkeypatch, store, ["--set-password"], "one\ntwo\n")
    )
    assert code == 1
    assert logins.MISMATCH in capsys.readouterr().err
    assert store.names() == [logins.DB_ADMIN], "nothing was stored"

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
        _, user_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.USER, user_url, None)
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


async def test_as_root_an_empty_user_password_publishes_only_a_generated_one(
    db_url: str,
    store: CredentialStore,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D228 with D265 and D279, as root (the tests' uid stands for it): `--no-password` writes
    the generated password of `calu_admin` into the login file next to the place file, never
    a typed one and never into the place file itself; a later `--set-password` takes it back:
    the login file is gone and the published one stops working.

    Writes the place file below `tmp_path` (`CALU_PLACE_FILE`)."""
    place_file = tmp_path / "state" / "database.json"
    monkeypatch.setenv("CALU_PLACE_FILE", str(place_file))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    really_read = db_place.read
    monkeypatch.setattr(db_place, "read", lambda: really_read(None, os.getuid()))
    store.save(logins.DB_ADMIN, db_url, "typed-pw")
    owner = Target.parse(db_url)

    def setup(argv: list[str], stdin: str) -> int:
        """Run `calu setup` in a thread."""
        return run_setup(monkeypatch, store, argv, stdin)

    code = await anyio.to_thread.run_sync(lambda: setup(["--no-password"], "typed-pw\n"))
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert "user-login.json, which only root can read" in said.err  # no sudo user here
    login_file = db_place.login_path(place_file)
    assert "admin_password" not in json.loads(place_file.read_text())
    assert login_file.stat().st_mode & 0o777 == 0o600
    published = json.loads(login_file.read_text())["admin_password"]
    assert published != "typed-pw" and "typed-pw" not in login_file.read_text()
    assert published == stored_password(store)
    assert await is_user_password(owner, published)
    assert not await is_user_password(owner, "typed-pw")

    # A setup without options keeps it published: nothing is asked.
    assert await anyio.to_thread.run_sync(lambda: setup([], "")) == 0
    capsys.readouterr()
    assert json.loads(login_file.read_text())["admin_password"] == published

    code = await anyio.to_thread.run_sync(lambda: setup(["--set-password"], "new-pw\nnew-pw\n"))
    said = capsys.readouterr()
    assert code == 0, said.out + said.err
    assert "admin_password" not in json.loads(place_file.read_text())
    assert not login_file.exists()
    assert not await is_user_password(owner, published), "the published login was revoked"
    assert await is_user_password(owner, "new-pw")
    assert store.needs_password(logins.USER) and store.needs_password(logins.DB_ADMIN)


async def test_calu_setup_stores_the_sandbox_choice_and_keeps_it_on_a_rerun(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Requirement 42: `--no-sandbox` is stored in the global settings; a later setup without
    a terminal and without the option leaves it as it is and repeats the recommendation."""
    from calu import admin

    store = CredentialStore(tmp_path / "credentials.json")
    # An empty user password: the recommendation is not met.
    store.save(logins.DB_ADMIN, db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def setup(*options: str) -> int:
        return cli.main(["setup", *options], store_factory=lambda: cast(CredentialStore, store))

    assert await anyio.to_thread.run_sync(setup, "--no-sandbox") == 0
    said = capsys.readouterr()
    assert "Claude Code's sandbox: off (stored)" in said.out
    assert "strongly recommended" in said.err
    assert "without a password: db-admin, user" in said.err

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


async def test_calu_setup_offers_to_update_an_older_shipped_text_and_keeps_an_edited_one(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D248, D250: `--no-update-instructions` keeps an old shipped text and says so; without
    a terminal it is kept and listed too, and `--update-instructions` replaces it; an edited
    text is reported and never replaced."""
    from calu import admin
    from calu.defaults.instructions import default_instructions, superseded_instructions

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.DB_ADMIN, db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def setup(*options: str) -> int:
        return cli.main(["setup", *options], store_factory=lambda: cast(CredentialStore, store))

    assert await anyio.to_thread.run_sync(setup) == 0
    capsys.readouterr()
    old = superseded_instructions("edit")[-1]
    db = Db(await connect(db_url))
    try:
        await admin.set_checker(db, "edit", instructions_text=old)
        await admin.set_checker(db, "command", instructions_text="Own rules.\n")
    finally:
        await db.aclose()

    async def edit_text() -> str:
        db = Db(await connect(db_url))
        try:
            return await admin.instructions(db, "edit")
        finally:
            await db.aclose()

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert await anyio.to_thread.run_sync(setup, "--no-update-instructions") == 0
    said = capsys.readouterr().out
    assert "the edit checker's instructions: an older version calu shipped" in said
    assert "`calu checker update edit`" in said
    assert "the command checker's instructions: your own text" in said
    assert await edit_text() == old
    assert await anyio.to_thread.run_sync(setup) == 0
    said = capsys.readouterr().out
    assert "the edit checker's instructions: an older version calu shipped" in said
    assert "1 outdated shipped text(s) kept; `calu setup --update-instructions`" in said
    assert await edit_text() == old, "a setup without a terminal changes no instructions"
    assert await anyio.to_thread.run_sync(setup, "--update-instructions") == 0
    said = capsys.readouterr().out
    assert "the edit checker's instructions: updated to the present shipped version" in said
    assert await edit_text() == default_instructions("edit")


async def test_checker_show_diff_and_update_say_how_the_text_relates_to_the_shipped_one(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D248: `show` notes on stderr, `diff` shows the difference, `update` refuses own text."""
    from calu import admin
    from calu.defaults.instructions import superseded_instructions

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.DB_ADMIN, db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def calu(*argv: str) -> int:
        return cli.main(list(argv), store_factory=lambda: cast(CredentialStore, store))

    assert await anyio.to_thread.run_sync(calu, "setup") == 0
    db = Db(await connect(db_url))
    try:
        await admin.set_checker(db, "edit", instructions_text=superseded_instructions("edit")[-1])
        await admin.set_checker(db, "command", instructions_text="Own rules.\n")
    finally:
        await db.aclose()
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert await anyio.to_thread.run_sync(calu, "checker", "show", "command") == 0
    said = capsys.readouterr()
    assert said.out.strip() == "Own rules." and "your own text" in said.err
    assert await anyio.to_thread.run_sync(calu, "checker", "diff", "command") == 0
    assert "+Own rules." in capsys.readouterr().out
    assert await anyio.to_thread.run_sync(calu, "checker", "update", "command") == 1
    assert "never replaces" in capsys.readouterr().err
    assert await anyio.to_thread.run_sync(calu, "checker", "update", "edit") == 0
    assert "updated to the present shipped version" in capsys.readouterr().out


async def test_checker_template_lists_shows_and_loads_a_template(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D250: `calu checker template list|show|use`; `use` keeps the user's own text."""
    from calu import admin
    from calu.defaults import library

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.DB_ADMIN, db_url, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def calu(*argv: str) -> int:
        return cli.main(list(argv), store_factory=lambda: cast(CredentialStore, store))

    assert await anyio.to_thread.run_sync(calu, "setup") == 0
    capsys.readouterr()
    assert await anyio.to_thread.run_sync(calu, "checker", "template", "list") == 0
    assert "review-findings          subagent-result" in capsys.readouterr().out
    assert (
        await anyio.to_thread.run_sync(calu, "checker", "template", "show", "review-findings") == 0
    )
    assert capsys.readouterr().out == library.template("review-findings").text
    assert (
        await anyio.to_thread.run_sync(calu, "checker", "template", "use", "review-findings") == 0
    )
    assert "loaded into the subagent-result checker" in capsys.readouterr().out
    db = Db(await connect(db_url))
    try:
        assert await admin.instructions(db, "subagent-result") == (
            library.template("review-findings").text
        )
        await admin.set_checker(db, "edit", instructions_text="Own rules.\n")
    finally:
        await db.aclose()
    assert (
        await anyio.to_thread.run_sync(calu, "checker", "template", "use", "edit-project-rules")
        != 0
    )
    assert "your own text" in capsys.readouterr().err
