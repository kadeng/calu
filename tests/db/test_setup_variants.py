"""`calu setup` for a server that exists already, and what it refuses (agent/records/PLAN.md D205,
D265).

The local Docker variant is tested without Docker in tests/unit/test_setup_dialog.py; RDS with
IAM authentication needs an RDS instance and is on the manual checklist (docs/TESTING.md),
except for what the database side of it does, which is tested here.
"""

import io
import uuid
from typing import cast

import anyio
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from calu import cli, logins
from calu.credentials import CredentialStore
from calu.db import connect, roles
from calu.db.connection import open_stored
from calu.db.target import PasswordAuth, Target
from calu.setup import MANAGED_ROLES, generated_password, preflight, set_user_password

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def run_setup(
    store: CredentialStore, monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: str
) -> int:
    """Run `calu setup` in a thread with a loop of its own, with lines on stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return await anyio.to_thread.run_sync(
        lambda: cli.main(["setup", *argv], store_factory=lambda: cast(CredentialStore, store))
    )


def server_flags(db_url: str) -> tuple[list[str], str]:
    """Return the options that name the test database, and the owner's password."""
    params = conninfo_to_dict(db_url)
    flags = ["--database", "server", "--host", str(params["host"]), "--port", str(params["port"])]
    flags += ["--dbname", str(params["dbname"]), "--user", str(params["user"])]
    return flags, str(params["password"])


async def test_an_existing_server_is_set_up_from_host_port_user_password_and_database(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The database user's password, then the new user password twice: both entries are
    stored under the user password, and `calu_admin` logs in with it. Gives the server-wide
    role `calu_admin` the password "user-pw"."""
    store = CredentialStore(tmp_path / "credentials.json")
    flags, password = server_flags(db_url)
    lines = f"{password}\nuser-pw\nuser-pw\n"
    assert await run_setup(store, monkeypatch, flags, lines) == 0
    said = capsys.readouterr()
    assert "migrations applied" in said.out, said.err
    assert logins.choose_intro(False) in said.out

    assert store.names() == [logins.DB_ADMIN, logins.USER]
    owner = Target.parse(store.load(logins.DB_ADMIN, "user-pw"))
    user = Target.parse(store.load(logins.USER, "user-pw"))
    assert (owner.variant, user.variant) == ("server", "server")
    assert user.user == roles.ADMIN and user.conninfo["dbname"] == owner.conninfo["dbname"]
    assert user.auth == PasswordAuth(password="user-pw")
    # Only where the database is travels with the user login, not the owner's other
    # parameters.
    assert sorted(user.conninfo) == ["dbname", "host", "port", "user"]
    conn = await open_stored(user)
    try:
        cur = await conn.execute("SELECT current_user")
        assert await cur.fetchone() == (roles.ADMIN,)
    finally:
        await conn.close()

    # A second setup asks the user password once and keeps the login: sessions that run and
    # other machines go on working.
    assert await run_setup(store, monkeypatch, [], "user-pw\n") == 0
    capsys.readouterr()
    conn = await open_stored(user)
    await conn.close()

    assert await run_setup(store, monkeypatch, flags, "") == 1
    assert "a database is set up already" in capsys.readouterr().err


async def test_a_wrong_password_stores_nothing(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    flags, _ = server_flags(db_url)
    assert await run_setup(store, monkeypatch, flags, "not-the-password\nadmin\nadmin\n") == 1
    assert "cannot connect to" in capsys.readouterr().err
    assert store.names() == []


async def test_a_login_that_cannot_set_calu_up_is_told_what_to_change(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the rights, setup used to end with a database error in the middle of the first
    migration. It now stops before anything changes and names the SQL for an administrator."""
    role = f"calu_t_{uuid.uuid4().hex[:10]}"
    owner = await connect(db_url)
    try:
        await owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'plain-pw' NOCREATEROLE").format(
                sql.Identifier(role)
            )
        )
        # As PostgreSQL 15 and later create a database: nobody but its owner creates in public.
        await owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        store = CredentialStore(tmp_path / "credentials.json")
        flags, _ = server_flags(db_url)
        flags[flags.index("--user") + 1] = role
        assert await run_setup(store, monkeypatch, flags, "plain-pw\nadmin\nadmin\n") == 1
        told = capsys.readouterr().err
        assert "calu cannot be set up in" in told
        assert f"ALTER ROLE {role} CREATEROLE" in told
        assert "may not create tables in the schema public" in told
        assert f"OWNER TO {role}" in told
        assert store.names() == [], "nothing is stored for a login that cannot be used"
        cur = await owner.execute("SELECT to_regclass('public.schema_migrations') IS NULL")
        assert await cur.fetchone() == (True,), "nothing was migrated"
    finally:
        await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        await owner.close()


async def test_a_database_that_holds_other_tables_is_not_taken_over(db_url: str) -> None:
    """calu grants its operator role read access to every table of the schema, and
    `calu db reset` drops the schema: it needs a database of its own."""
    owner = await connect(db_url)
    try:
        assert await preflight(owner) == []
        await owner.execute("CREATE TABLE invoices (id int)")
        (problem,) = await preflight(owner)
        assert "already holds 1 table(s) that are not calu's" in problem
        assert "CREATE DATABASE calu OWNER" in problem
    finally:
        await owner.close()


async def test_the_user_login_leaves_rds_iam_to_log_in_with_its_password(db_url: str) -> None:
    """D265 (3): on RDS the user login uses a password, not IAM. A member of `rds_iam` is
    asked for a token there, so `set_user_password` takes `calu_admin` out of the role.
    Side effects on the test server: creates the role `rds_iam` if it is missing, and drops it
    again; creates the server-wide role `calu_admin` if it is missing and leaves it in place;
    sets a generated password on `calu_admin`."""
    owner = await connect(db_url)
    created = False
    try:
        cur = await owner.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'")
        if await cur.fetchone() is None:
            await owner.execute("CREATE ROLE rds_iam NOLOGIN")  # what RDS provides
            created = True
        cur = await owner.execute("SELECT 1 FROM pg_roles WHERE rolname = 'calu_admin'")
        if await cur.fetchone() is None:
            await owner.execute("CREATE ROLE calu_admin NOLOGIN")
        await owner.execute("GRANT rds_iam TO calu_admin")
        await set_user_password(owner, generated_password())
        cur = await owner.execute("SELECT pg_has_role('calu_admin', 'rds_iam', 'MEMBER')")
        assert await cur.fetchone() == (False,)
        with pytest.raises(ValueError):
            await set_user_password(owner, "")
    finally:
        try:
            await owner.execute("REVOKE rds_iam FROM calu_admin")
            if created:
                await owner.execute("DROP ROLE rds_iam")
        except psycopg.Error:
            pass
        await owner.close()


async def test_a_login_with_createrole_passes_and_one_that_cannot_manage_the_roles_does_not(
    db_url: str,
) -> None:
    """The preflight asked for the admin option on every version and passed a login without
    CREATEROLE that held it. What changing calu's roles needs: CREATEROLE always, and from
    PostgreSQL 16 on the admin option for each role of `MANAGED_ROLES` that somebody else
    created."""
    role = f"calu_t_{uuid.uuid4().hex[:10]}"
    owner = await connect(db_url)
    try:
        cur = await owner.execute("SHOW server_version_num")
        row = await cur.fetchone()
        assert row is not None
        version = int(row[0])
        await owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'plain-pw' CREATEROLE").format(
                sql.Identifier(role)
            )
        )
        await owner.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(str(conninfo_to_dict(db_url)["dbname"])), sql.Identifier(role)
            )
        )
        await owner.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(role)))
        for existing in MANAGED_ROLES:
            cur = await owner.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (existing,))
            if await cur.fetchone() is None:
                await owner.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(existing))
                )
        login = Target.parse(db_url).for_role(role, PasswordAuth(password="plain-pw"))
        conn = await open_stored(login)
        try:
            problems = await preflight(conn)
            if version >= 160000:
                assert len(problems) == len(MANAGED_ROLES)
                assert all("WITH ADMIN OPTION" in p for p in problems)
                for existing in MANAGED_ROLES:
                    await owner.execute(
                        sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                            sql.Identifier(existing), sql.Identifier(role)
                        )
                    )
            assert await preflight(conn) == []
        finally:
            await conn.close()
    finally:
        await owner.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(
                sql.Identifier(str(conninfo_to_dict(db_url)["dbname"]))
            )
        )
        await owner.execute("ALTER SCHEMA public OWNER TO CURRENT_USER")
        await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        await owner.close()
