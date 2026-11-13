"""`calu setup` for a server that exists already, and what it refuses (PLAN.md D205).

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

from calu import cli
from calu.credentials import CredentialStore
from calu.db import connect
from calu.db.connection import open_stored
from calu.db.target import PasswordAuth, RdsIamAuth, Target
from calu.setup import enable_session_login, preflight

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
    store = CredentialStore(tmp_path / "credentials.json")
    flags, password = server_flags(db_url)
    # The role calu_session belongs to the whole server. Give it a login, as another calu
    # database on the server would have: a first setup then has to say that it replaces it.
    other = await connect(db_url)
    try:
        cur = await other.execute("SELECT 1 FROM pg_roles WHERE rolname = 'calu_session'")
        if await cur.fetchone() is None:
            await other.execute("CREATE ROLE calu_session NOLOGIN")
        await other.execute(
            sql.SQL("ALTER ROLE calu_session WITH LOGIN PASSWORD {}").format(
                sql.Literal(uuid.uuid4().hex)
            )
        )
    finally:
        await other.close()
    lines = f"{password}\nadmin-pw\nadmin-pw\nsession-pw\nsession-pw\n"
    assert await run_setup(store, monkeypatch, flags, lines) == 1
    refused = capsys.readouterr().err
    assert "calu_session has a login already" in refused and "--renew-session" in refused
    assert store.names() == ["admin"], "the checked owner login is stored, no session login"

    lines = "admin-pw\nsession-pw\nsession-pw\n"
    assert await run_setup(store, monkeypatch, ["--renew-session"], lines) == 0
    printed = capsys.readouterr().out
    assert "migrations applied" in printed and "stored credential 'session'" in printed

    owner = Target.parse(store.load("admin", "admin-pw"))
    session = Target.parse(store.load("session", "session-pw"))
    assert (owner.variant, session.variant) == ("server", "server")
    assert session.user == "calu_session" and session.conninfo["dbname"] == owner.conninfo["dbname"]
    conn = await open_stored(session)
    try:
        cur = await conn.execute("SELECT current_user")
        assert await cur.fetchone() == ("calu_session",)
    finally:
        await conn.close()

    # A second setup keeps the login: sessions that run and other machines go on working.
    assert await run_setup(store, monkeypatch, [], "admin-pw\n") == 0
    assert "session login kept" in capsys.readouterr().out
    conn = await open_stored(session)
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


async def test_a_session_login_with_iam_gets_no_password(db_url: str) -> None:
    """On RDS a role that is a member of `rds_iam` logs in with a token. calu then stores
    where the database is and which AWS profile signs, and no secret."""
    owner = await connect(db_url)
    created = False
    try:
        cur = await owner.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'")
        if await cur.fetchone() is None:
            await owner.execute("CREATE ROLE rds_iam NOLOGIN")  # what RDS provides
            created = True
        admin = Target.parse(db_url)
        iam = RdsIamAuth(region="eu-central-1", profile="calu-sessions")
        session = await enable_session_login(owner, admin, iam)
        assert session.user == "calu_session" and session.auth == iam
        assert b"password" not in session.dump()
        cur = await owner.execute("SELECT pg_has_role('calu_session', 'rds_iam', 'MEMBER')")
        assert await cur.fetchone() == (True,)
        # Back to a password (`--session-auth password`): RDS accepts no password from a
        # member of rds_iam, so the membership has to go with it.
        with_password = await enable_session_login(owner, admin)
        assert isinstance(with_password.auth, PasswordAuth)
        cur = await owner.execute("SELECT pg_has_role('calu_session', 'rds_iam', 'MEMBER')")
        assert await cur.fetchone() == (False,)
    finally:
        try:
            await owner.execute("REVOKE rds_iam FROM calu_session")
            if created:
                await owner.execute("DROP ROLE rds_iam")
        except psycopg.Error:
            pass
        await owner.close()


async def test_a_login_with_createrole_passes_and_one_that_cannot_manage_the_roles_does_not(
    db_url: str,
) -> None:
    """The preflight asked for the admin option on every version and passed a login without
    CREATEROLE that held it. What giving a role a login needs: CREATEROLE always, and from
    PostgreSQL 16 on the admin option for a role that somebody else created."""
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
        for existing in ("calu_session", "calu_operator"):
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
                assert len(problems) == 2 and all("WITH ADMIN OPTION" in p for p in problems)
                for existing in ("calu_session", "calu_operator"):
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
