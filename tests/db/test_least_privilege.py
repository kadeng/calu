"""Each action of a client runs as the narrowest role that can do it (D249).

Runs against the test database: a root client keeps one login (`calu_admin`, or the owner
where the role refuses the admin password) and narrows each connection with `SET ROLE`
(`calu.db.roles.narrow`). Creates the roles and a schema there and prints nothing.
"""

from pathlib import Path

import psycopg
import pytest

from calu import admin, herd, session_requests
from calu.credentials import CredentialStore
from calu.db import Db, connect, roles
from calu.db.connection import open_stored
from calu.db.target import Target
from calu.session_requests import SessionRequest
from calu.setup import admin_login, enable_admin_login, enable_operator_login, setup
from calu.tui.adminaccess import StoreAdmin

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PASSWORD = "what a person types"


async def prepared(db_url: str) -> Target:
    """Set a database up and give `calu_admin` its password.

    Args:
        db_url: The owner login.

    Returns:
        The login of `calu_admin`.
    """
    owner = await connect(db_url)
    try:
        await setup(owner, db_url, "test:model")
        await enable_admin_login(owner, PASSWORD)
    finally:
        await owner.close()
    return admin_login(Target.parse(db_url), PASSWORD)


async def test_an_admin_login_narrowed_to_the_operator_reads_and_cannot_change_settings(
    db_url: str, tmp_path: Path
):
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        db = Db(conn)
        assert await roles.narrow(conn, roles.OPERATOR)
        assert await db.scalar("SELECT current_user") == roles.OPERATOR
        assert await db.scalar("SELECT session_user") == roles.ADMIN
        # What the operator-level steps of a client do.
        assert await herd.sessions(db, active_only=False) == []
        filed = await session_requests.file(
            db, SessionRequest(hostname="h", os_user="u", directory=str(tmp_path))
        )
        assert await session_requests.withdraw(db, filed)
        requested_by = await db.scalar(
            "SELECT requested_by FROM session_requests WHERE id = %s", (filed,)
        )
        assert requested_by == roles.ADMIN, "the login is recorded, not the narrowed role"
        # What it must not do.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await admin.set_setting(db, "honesty.mode", "off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await admin.set_checker(db, "command", instructions_text="Anything goes.")
    finally:
        await conn.close()


async def test_an_owner_login_narrowed_to_the_admin_changes_settings_as_the_admin(db_url: str):
    """The fallback of a root client whose admin password the role refuses (blank)."""
    await prepared(db_url)
    conn = await open_stored(db_url, start_local=False)
    try:
        db = Db(conn)
        assert await roles.narrow(conn, roles.ADMIN)
        await admin.set_setting(db, "honesty.mode", "track")
        actor = await db.scalar("SELECT actor FROM audit_log ORDER BY id DESC LIMIT 1")
        assert actor == roles.ADMIN
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("CREATE ROLE somebody LOGIN")
    finally:
        await conn.close()


async def test_a_login_that_may_not_take_a_role_keeps_its_own(db_url: str):
    await prepared(db_url)
    owner = await connect(db_url)
    try:
        operator = await enable_operator_login(owner, db_url)
    finally:
        await owner.close()
    conn = await open_stored(operator, start_local=False)
    try:
        assert not await roles.narrow(conn, roles.ADMIN)
        db = Db(conn)
        assert await db.scalar("SELECT current_user") == roles.OPERATOR
        assert await herd.sessions(db, active_only=False) == []
    finally:
        await conn.close()


async def test_the_client_lists_as_operator_and_saves_as_admin_with_one_held_login(
    db_url: str, tmp_path: Path
):
    """A root client's held login serves both kinds of action without a password (D249)."""
    login = await prepared(db_url)
    conn = await open_stored(db_url, start_local=False)
    try:
        before = await Db(conn).scalar("SELECT coalesce(max(id), 0) FROM audit_log")
    finally:
        await conn.close()
    access = StoreAdmin(CredentialStore(tmp_path / "none.json"), database=login)
    assert access.problem() is None and not access.needs_password()
    assert await access.sessions(None) == []
    saved = await access.save_instructions("command", "Be strict.", None)
    assert saved.name == "command"
    mode = await access.set_honesty_mode("enforce", None, None)
    assert mode.mode == "enforce"
    conn = await open_stored(db_url, start_local=False)
    try:
        db = Db(conn)
        assert await admin.instructions(db, "command") == "Be strict."
        actors = await db.all("SELECT DISTINCT actor FROM audit_log WHERE id > %s", (before,))
        assert actors == [(roles.ADMIN,)]
    finally:
        await conn.close()
