"""Migration 0035 against the test database: expired claims, narrower grants, search path.

Runs against the test database; creates the roles and a schema there and prints nothing.
"""

from pathlib import Path

import psycopg
import pytest

from calu import session_requests
from calu.db import Db, connect
from calu.db.connection import open_stored
from calu.session_requests import SessionRequest
from calu.setup import enable_admin_login
from tests.db.test_admin_role import PASSWORD, prepared

pytestmark = [pytest.mark.db, pytest.mark.anyio]

SESSION_COLUMNS = {
    "last_seen",
    "state",
    "state_since",
    "ended_at",
    "claude_session_id",
    "model",
    "address",
    "forked_from",
    "forked_before_turn",
}
"""The `sessions` columns the session login may still update."""


async def test_a_claim_older_than_the_limit_expires(db_url: str, tmp_path: Path):
    login = await prepared(db_url)
    owner = await connect(db_url)
    admin = await open_stored(login, start_local=False)
    try:
        db = Db(admin)
        filed = await session_requests.file(
            db, SessionRequest(hostname="h", os_user="u", directory=str(tmp_path))
        )
        await owner.execute(
            "UPDATE session_requests SET state = 'claimed', claimed_at = now(), claimed_pid = 1"
            " WHERE id = %s",
            (filed,),
        )
        # A young claim stays with its server.
        assert not await session_requests.expire(db, filed)
        assert (await session_requests.read(db, filed)).state == "claimed"  # type: ignore[union-attr]
        await owner.execute(
            "UPDATE session_requests SET claimed_at = now() - interval '181 seconds' WHERE id = %s",
            (filed,),
        )
        assert await session_requests.expire(db, filed)
        expired = await session_requests.read(db, filed)
        assert expired is not None
        assert expired.state == "failed"
        assert expired.message == "the server that claimed it did not answer"
        # A finished request is not expired again.
        assert not await session_requests.expire(db, filed)
    finally:
        await admin.close()
        await owner.close()


async def test_the_session_login_cannot_expire_requests(db_url: str):
    owner = await connect(db_url)
    try:
        await prepared(db_url)
        cur = await owner.execute(
            "SELECT has_function_privilege('calu_session',"
            " 'calu_expire_session_request(bigint)', 'EXECUTE'),"
            " has_function_privilege('calu_operator',"
            " 'calu_expire_session_request(bigint)', 'EXECUTE')"
        )
        assert await cur.fetchone() == (False, True)
    finally:
        await owner.close()


async def test_the_session_login_updates_only_the_columns_the_store_writes(db_url: str):
    await prepared(db_url)
    owner = await connect(db_url)
    try:
        cur = await owner.execute(
            "SELECT attname, has_column_privilege('calu_session', 'sessions', attname, 'UPDATE')"
            " FROM pg_attribute WHERE attrelid = 'sessions'::regclass AND attnum > 0"
            " AND NOT attisdropped"
        )
        allowed = {name for name, granted in await cur.fetchall() if granted}
        assert allowed == SESSION_COLUMNS
    finally:
        await owner.close()


async def test_the_admin_cannot_change_or_delete_audit_entries(db_url: str):
    login = await prepared(db_url)
    conn = await open_stored(login, start_local=False)
    try:
        for statement in (
            "DELETE FROM audit_log",
            "UPDATE audit_log SET actor = 'nobody'",
            "TRUNCATE audit_log",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(statement)
        # Reading stays allowed.
        await conn.execute("SELECT count(*) FROM audit_log")
    finally:
        await conn.close()


async def test_migration_0035_runs_again_without_harm(db_url: str):
    await prepared(db_url)
    owner = await connect(db_url)
    try:
        migration = (
            Path(__file__).parents[2]
            / "src"
            / "calu"
            / "db"
            / "migrations"
            / "0035_review_fixes.sql"
        )
        await owner.execute(migration.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        await enable_admin_login(owner, PASSWORD)
    finally:
        await owner.close()


async def test_revoke_session_tokens_ends_its_search_path_with_pg_temp(db_url: str):
    await prepared(db_url)
    owner = await connect(db_url)
    try:
        cur = await owner.execute(
            "SELECT proconfig FROM pg_proc WHERE proname = 'calu_revoke_session_tokens'"
        )
        (config,) = await cur.fetchone()  # type: ignore[misc]
        assert "search_path=pg_catalog, public, pg_temp" in config
    finally:
        await owner.close()
