"""`calu db reset`: all is dropped and created again, behind a password and a typed phrase."""

import io

import anyio
import pytest

from calu import admin, cli
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.setup import setup

pytestmark = [pytest.mark.db, pytest.mark.anyio]

CONFIRM = cli.confirm_reset
"""The real confirmation, kept because the tests replace `cli.confirm_reset`."""


async def run(store: CredentialStore) -> int:
    """Run `calu db reset` in a thread, as the command line does.

    Args:
        store: The credential store.

    Returns:
        The exit code.
    """
    return await anyio.to_thread.run_sync(lambda: cli.main(["db", "reset"], lambda: store))


class Typist:
    """Stands in for the person at the prompt: answers a line after a given time."""

    def __init__(self, line: str, seconds: float) -> None:
        """Remember what is typed and how long it takes.

        Args:
            line: The line the person types.
            seconds: How long typing it takes.
        """
        self.line = line
        self.seconds = seconds
        self.now = 100.0

    def read(self, prompt: str) -> str:
        """Type the line, which takes `seconds`.

        Args:
            prompt: The prompt shown.

        Returns:
            The line.
        """
        self.now += self.seconds
        return self.line

    def clock(self) -> float:
        """Return the current time of this typist's clock.

        Returns:
            Seconds.
        """
        return self.now


async def _reset(store: CredentialStore, typist: Typist, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run `calu db reset` with the password "pw" and the given typist.

    Args:
        store: The credential store.
        typist: Who answers the confirmation.
        monkeypatch: Replaces stdin and the confirmation's reader and clock.

    Returns:
        The exit code.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    monkeypatch.setattr(cli, "confirm_reset", lambda: CONFIRM(typist.read, typist.clock))
    return await run(store)


async def test_a_reset_needs_the_password_and_a_phrase_typed_by_hand(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner's ask: tables dropped and recreated, a password, and a phrase no script types."""
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, "pw")
    conn = await connect(db_url)
    try:
        _report, session_url = await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.add_project(db, "demo")
        await admin.set_setting(db, "claude.effort", "high")
        await admin.replace_rule_list(db, "command-deny", "^sudo( |$)\n")
        await admin.set_checker(db, "edit", instructions_text="Our own rules.")
        table_before = await db.scalar("SELECT 'projects'::regclass::oid")

        async def untouched() -> bool:
            """Tell whether the project, the setting and the rule are still there."""
            return (
                [p.name for p in await admin.projects(db)] == ["demo"]
                and (await admin.settings(db))["claude"]["effort"] == "high"
                and len(await admin.rules(db)) == 1
            )

        # The wrong phrase, the phrase too fast, and no input at all delete nothing.
        for typist in (Typist("yes", 5.0), Typist(cli.RESET_PHRASE, 0.3)):
            assert await _reset(store, typist, monkeypatch) == 1
            assert "nothing was deleted" in capsys.readouterr().err
            assert await untouched()
        monkeypatch.setattr("sys.stdin", io.StringIO("wrong\n"))
        assert await run(store) == 1  # a wrong password
        assert await untouched()

        # The password and the phrase, typed in more than two seconds: everything is new.
        assert await _reset(store, Typist(cli.RESET_PHRASE, 3.0), monkeypatch) == 0
        said = capsys.readouterr().out
        assert "1 project(s)" in said and "the database is empty again" in said
    finally:
        await conn.close()

    conn = await connect(db_url)
    try:
        db = Db(conn)
        assert await db.scalar("SELECT 'projects'::regclass::oid") != table_before, (
            "the table is a new one, not the old one emptied"
        )
        assert await admin.projects(db) == []
        assert await admin.settings(db) == {}
        assert await admin.rules(db) == []
        assert await admin.instructions(db, "edit") != "Our own rules."
        assert {row.name for row in await admin.checkers(db)} >= {"command", "edit", "changeset"}
    finally:
        await conn.close()

    # The roles are not part of the schema: the session credential from before still works.
    session = await connect(session_url)
    try:
        assert await Db(session).scalar("SELECT count(*) FROM checker_types") >= 3
    finally:
        await session.close()


async def test_a_reset_is_refused_for_an_admin_credential_without_a_password(
    db_url: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a password to type, one of the two confirmations would be missing."""
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)
    assert await run(store) == 1
    assert "stored without a password" in capsys.readouterr().err
