"""The role `calu_admin`: settings are changed with a typed password (migration 0033, D213).

Runs against the test database; creates the roles and a schema there and prints nothing.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import psycopg
import pytest

from calu import admin, herd, session_requests
from calu.db import Db, connect
from calu.db.connection import open_stored
from calu.db.target import Target
from calu.session_requests import SessionRequest
from calu.setup import ADMIN_ROLE, admin_login, enable_admin_login, setup

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PASSWORD = "what a person types"


@pytest.fixture(autouse=True)
async def no_login_left_behind(db_url: str) -> AsyncGenerator[None]:
    """Take the login of `calu_admin` away again after each test.

    Roles belong to the whole test server, not to one test database: a login left behind
    would make `calu web` in a later test offer the password login instead of its token.
    """
    yield
    owner = await connect(db_url)
    try:
        await owner.execute("ALTER ROLE calu_admin NOLOGIN")
    finally:
        await owner.close()


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


async def test_the_admin_changes_settings_with_a_typed_password(db_url: str, tmp_path: Path):
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        db = Db(conn)
        assert await db.scalar("SELECT session_user") == ADMIN_ROLE
        # What the clients change: a setting, a checker's instructions, a rule.
        await admin.set_setting(db, "honesty.mode", "track")
        assert (await admin.settings(db))["honesty"]["mode"] == "track"
        await admin.set_checker(db, "command", instructions_text="Be strict.")
        assert await admin.instructions(db, "command") == "Be strict."
        await admin.add_rule(db, "command-deny", "^rm -rf /")
        # What an operator may do, the admin may do as well.
        assert await herd.sessions(db, active_only=False) == []
        filed = await session_requests.file(
            db, SessionRequest(hostname="h", os_user="u", directory=str(tmp_path))
        )
        assert await session_requests.withdraw(db, filed)
    finally:
        await conn.close()


async def test_the_admin_owns_nothing(db_url: str):
    """It cannot change the schema, make roles or switch the audit off."""
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        for statement in (
            "ALTER TABLE config ADD COLUMN extra text",
            "DROP TABLE rules",
            "CREATE ROLE somebody LOGIN",
            "ALTER TABLE config DISABLE TRIGGER ALL",
            "ALTER ROLE calu_session PASSWORD 'x'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(statement)
    finally:
        await conn.close()


async def test_a_wrong_or_replaced_password_is_refused(db_url: str):
    login = await prepared(db_url)
    place = Target.parse(db_url)
    with pytest.raises(psycopg.OperationalError):
        await open_stored(admin_login(place, "a guess"), start_local=False)
    owner = await connect(db_url)
    try:
        await enable_admin_login(owner, "another password")
    finally:
        await owner.close()
    with pytest.raises(psycopg.OperationalError):
        await open_stored(login, start_local=False)


async def test_every_table_with_row_level_security_lets_the_admin_through(db_url: str):
    """A later migration that switches row-level security on must add `admin_all` there."""
    await prepared(db_url)
    owner = await connect(db_url)
    try:
        cur = await owner.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
            "   AND NOT EXISTS (SELECT FROM pg_policy p"
            "                    WHERE p.polrelid = c.oid AND p.polname = 'admin_all')"
        )
        assert await cur.fetchall() == []
    finally:
        await owner.close()


async def test_the_web_interface_signs_a_browser_in_with_the_admin_password(db_url: str):
    """The gate of `calu web` with the password login (D216), against the real role.

    Serves the application in this process; one wrong password makes the test wait about a
    second, which is the gate's delay.
    """
    import functools

    import httpx

    from calu.setup import admin_can_log_in, is_admin_password
    from calu.web.app import create_app

    await prepared(db_url)
    place = Target.parse(db_url)
    assert await is_admin_password(place, PASSWORD)
    assert not await is_admin_password(place, "a guess")
    assert not await is_admin_password(place, "")
    owner = await connect(db_url)
    try:
        assert await admin_can_log_in(owner)
        app = create_app(
            Db(owner), token="printed", check_password=functools.partial(is_admin_password, place)
        )
        web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
        async with web:
            asked = await web.get("/sessions?x=1")
            assert asked.status_code == 401 and "Admin password" in asked.text
            assert 'name="onward" value="/sessions?x=1"' in asked.text
            # Without a login nothing else is served, and nothing can be changed.
            assert (await web.post("/settings", data={"a": "b"})).status_code == 401
            wrong = await web.post("/_login", data={"password": "a guess", "onward": "/"})
            assert wrong.status_code == 401 and "not the admin password" in wrong.text
            assert "set-cookie" not in wrong.headers
            # An address on another host as the place to go on to stays on this host.
            right = await web.post(
                "/_login", data={"password": PASSWORD, "onward": "//elsewhere.example/x"}
            )
            assert right.status_code == 200 and "set-cookie" in right.headers
            assert 'name="calu-onward" content="/elsewhere.example/x"' in right.text
            assert (await web.get("/")).status_code == 200, "the cookie reads"
    finally:
        await owner.close()
