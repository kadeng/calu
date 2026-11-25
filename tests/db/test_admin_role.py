"""The role `calu_admin`, the user login: settings, the fleet and the web interface's sign-in
with the calu user password (migrations 0033 and 0043, agent/records/PLAN.md D213, D265).

Runs against the test database; creates the roles and a schema there, sets the password of
`calu_admin` (a server-wide role) and prints nothing.
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
from calu.setup import USER_ROLE, set_user_password, setup, user_login
from calu.tui.adminaccess import StoreAdmin

pytestmark = [pytest.mark.db, pytest.mark.anyio]

PASSWORD = "what a person types"


async def prepared(db_url: str) -> Target:
    """Set a database up and give `calu_admin` the password `PASSWORD`.

    Args:
        db_url: The owner login.

    Returns:
        The login of `calu_admin`.
    """
    owner = await connect(db_url)
    try:
        await setup(owner, db_url, "test:model")
        await set_user_password(owner, PASSWORD)
    finally:
        await owner.close()
    return user_login(Target.parse(db_url), PASSWORD)


async def test_the_user_login_changes_settings_with_a_typed_password(db_url: str, tmp_path: Path):
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        db = Db(conn)
        assert await db.scalar("SELECT session_user") == USER_ROLE
        # What the clients change: a setting, a checker's instructions, a rule.
        await admin.set_setting(db, "honesty.mode", "track")
        assert (await admin.settings(db))["honesty"]["mode"] == "track"
        await admin.set_checker(db, "command", instructions_text="Be strict.")
        assert await admin.instructions(db, "command") == "Be strict."
        await admin.add_rule(db, "command-deny", "^rm -rf /")
        # What an operator may do, the user login may do as well.
        assert await herd.sessions(db, active_only=False) == []
        filed = await session_requests.file(
            db, SessionRequest(hostname="h", os_user="u", directory=str(tmp_path))
        )
        assert await session_requests.withdraw(db, filed)
    finally:
        await conn.close()


async def test_the_user_login_owns_nothing(db_url: str):
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
        await open_stored(user_login(place, "a guess"), start_local=False)
    owner = await connect(db_url)
    try:
        await set_user_password(owner, "another password")
    finally:
        await owner.close()
    with pytest.raises(psycopg.OperationalError):
        await open_stored(login, start_local=False)


async def test_every_table_with_row_level_security_lets_the_user_login_through(db_url: str):
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


async def test_the_web_interface_signs_a_browser_in_with_the_user_password(db_url: str):
    """The gate of `calu web` with the password login (D216), against the real role.

    Serves the application in this process; one wrong password makes the test wait about a
    second, which is the gate's delay.
    """
    import functools

    import httpx

    from calu.setup import is_user_password, user_can_log_in
    from calu.web.app import create_app

    await prepared(db_url)
    place = Target.parse(db_url)
    assert await is_user_password(place, PASSWORD)
    assert not await is_user_password(place, "a guess")
    assert not await is_user_password(place, "")
    owner = await connect(db_url)
    try:
        assert await user_can_log_in(owner)
        app = create_app(Db(owner), check_password=functools.partial(is_user_password, place))
        web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
        async with web:
            asked = await web.get("/sessions?x=1")
            assert asked.status_code == 401 and "calu user password" in asked.text
            assert 'name="onward" value="/sessions?x=1"' in asked.text
            # Without a login nothing else is served, and nothing can be changed.
            assert (await web.post("/settings", data={"a": "b"})).status_code == 401
            wrong = await web.post("/_login", data={"password": "a guess", "onward": "/"})
            assert wrong.status_code == 401 and "not the calu user password" in wrong.text
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


async def test_the_user_login_counts_as_operator(db_url: str):
    """`calu_caller_is_operator()` is what the fleet functions ask; `calu_admin` passes it as
    a member of `calu_operator` (migration 0033)."""
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        db = Db(conn)
        assert await db.scalar("SELECT calu_caller_is_operator()") is True
        assert await db.scalar(
            "SELECT pg_has_role(%s, %s, 'MEMBER')", (roles.ADMIN, roles.OPERATOR)
        )
    finally:
        await conn.close()


async def test_the_client_lists_and_saves_with_one_held_user_login(db_url: str, tmp_path: Path):
    """A root client's held login (D249) lists sessions and saves settings without a password;
    the audit log names `calu_admin` as the actor of the changes."""
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
