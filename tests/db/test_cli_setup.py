"""`calu setup` through the command line, against an empty database (D121).

The `setup` **function** is called by nearly every database test; the **command** was called by
none, so its own wiring was untested -- the two password prompts, the order of the steps (the
session credential is stored only after the database accepted the setup), the report it prints,
and `--checker-model` reaching the rows it writes. It is the second command a new user runs.
"""

import io
from typing import cast

import anyio
import pytest

from calu import cli, herd
from calu.credentials import CredentialStore
from calu.db import Db, connect
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
            ["setup", "--checker-model", "claude:haiku"],
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
    assert b"user=calu_session" in secret and b"calu_owner" not in secret

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
