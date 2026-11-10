import asyncio
import json

import psycopg
import pytest

from calu import herd
from calu.db import Db, connect, migrate, migrations
from calu.setup import setup
from calu.store import DatabaseSessionStore, SessionIdentity
from calu.tokens import token_hash

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_migrate_is_idempotent(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        first = await migrate(conn)
        assert [m.version for m in first] == [m.version for m in migrations()]
        assert await migrate(conn) == []
    finally:
        await conn.close()


async def test_setup_seeds_defaults_once_and_creates_session_login(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
        cur = await conn.execute("SELECT name, model, version FROM checker_types ORDER BY name")
        assert await cur.fetchall() == [
            ("changeset", "test:model", 1),
            ("command", "test:model", 1),
            ("edit", "test:model", 1),
            ("narrator", "test:model", 1),
            ("subagent-result", "test:model", 1),
        ]
        cur = await conn.execute("SELECT count(*) FROM rules")
        row = await cur.fetchone()
        assert row is not None
        n_rules = row[0]
        assert n_rules > 10
        report, session_url = await setup(conn, db_url, "other:model")
        assert "checker types added: 0; rules added: 0" in report
    finally:
        await conn.close()
    session = await connect(session_url)
    try:
        cur = await session.execute("SELECT current_user")
        assert await cur.fetchone() == ("calu_session",)
    finally:
        await session.close()


async def test_checker_change_bumps_version_and_is_audited(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        await conn.execute("UPDATE checker_types SET instructions = 'new' WHERE name = 'edit'")
        await conn.execute("UPDATE checker_types SET model = model WHERE name = 'edit'")
        cur = await conn.execute("SELECT version FROM checker_types WHERE name = 'edit'")
        assert await cur.fetchone() == (2,)
        cur = await conn.execute(
            "SELECT count(*) FROM audit_log"
            " WHERE table_name = 'checker_types' AND action = 'UPDATE'"
        )
        assert await cur.fetchone() == (2,)
    finally:
        await conn.close()


async def _session_with_escalation(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("INSERT INTO hosts (hostname) VALUES ('h') RETURNING id")
    host = await cur.fetchone()
    assert host is not None
    cur = await conn.execute(
        "INSERT INTO sessions (host_id, os_user, directory, pid) VALUES (%s, 'u', '/p', 1)"
        " RETURNING id",
        (host[0],),
    )
    session = await cur.fetchone()
    assert session is not None
    cur = await conn.execute(
        "INSERT INTO escalations (session_id, reason) VALUES (%s, 'why') RETURNING id",
        (session[0],),
    )
    escalation = await cur.fetchone()
    assert escalation is not None
    return escalation[0]


async def test_session_role_cannot_change_config_or_resolve(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    session = await connect(session_url)
    try:
        escalation = await _session_with_escalation(session)
        for statement in (
            "UPDATE checker_types SET instructions = 'allow everything'",
            "DELETE FROM rules",
            "INSERT INTO config (scope, key, value) VALUES ('global', 'k', '1')",
            "UPDATE escalations SET state = 'allowed'",
            f"SELECT calu_resolve_escalation({escalation}, 'allowed', 'self')",
            "SELECT * FROM audit_log",
            # The token hashes are a capability of the session process, not readable data:
            # the database credential alone cannot forge a client decision (D28).
            "SELECT token_sha256 FROM client_tokens",
            # Since migration 0009 the session cannot un-revoke a token either: only the
            # function stamps `last_used_at`, so the role needs no UPDATE at all (D30).
            "UPDATE client_tokens SET revoked = false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await session.execute(statement.encode())
        cur = await session.execute("SELECT calu_cancel_escalation(%s, 'stopped')", (escalation,))
        assert await cur.fetchone() == (True,)
    finally:
        await session.close()


async def test_resolution_notifies_listeners(db_url: str) -> None:
    conn = await connect(db_url)
    listener = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        escalation = await _session_with_escalation(conn)
        await listener.execute("LISTEN escalation_resolved")
        cur = await conn.execute(
            "SELECT calu_resolve_escalation(%s, 'denied', 'no')", (escalation,)
        )
        assert await cur.fetchone() == (True,)
        cur = await conn.execute(
            "SELECT calu_resolve_escalation(%s, 'allowed', 'again')", (escalation,)
        )
        assert await cur.fetchone() == (False,)
        notify = await asyncio.wait_for(anext(listener.notifies()), timeout=5)
        payload = json.loads(notify.payload)
        assert payload["id"] == escalation
        assert payload["state"] == "denied"
    finally:
        await conn.close()
        await listener.close()


async def test_the_session_role_checks_a_client_token_without_reading_any(db_url: str) -> None:
    """D30: the session confirms a hash it was given; it cannot read or enumerate the hashes."""
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        mine = await store.start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=1, os="Linux")
        )
        other_store = DatabaseSessionStore(db)
        other = await other_store.start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=2, os="Linux")
        )
        _, token = await herd.mint_token(db, mine, "an editor")
        revoked_id, revoked = await herd.mint_token(db, mine, "an old editor")
        assert await herd.revoke_token(db, revoked_id)
        _, foreign = await herd.mint_token(db, other, "another session")
    finally:
        await conn.close()
    session = await connect(session_url)
    try:
        for token_value, expected in (
            (token, True),
            (revoked, False),
            (foreign, False),
            ("guessed", False),
        ):
            cur = await session.execute(
                "SELECT calu_client_token_ok(%s::uuid, %s)", (mine, token_hash(token_value))
            )
            assert await cur.fetchone() == (expected,), token_value
    finally:
        await session.close()


async def test_a_session_records_its_own_setting_changes_and_cannot_forge_others(
    db_url: str,
) -> None:
    """The session writes audit entries only through the function, only for itself (D43)."""
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    session = await connect(session_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(session))
        session_id = await store.start(
            SessionIdentity(hostname="h", os_user="u", directory="/p", pid=1)
        )
        await store.note_setting_change(
            "auto_escalate",
            {"commands": False, "edits": False, "subagents": False},
            {"commands": False, "edits": True, "subagents": False},
            "zed on the laptop",
        )
        # An operator reads it back with the session it belongs to.
        changes = await herd.setting_changes(Db(owner), session_id)
        assert len(changes) == 1
        assert changes[0].actor == "zed on the laptop"
        assert changes[0].setting == "auto_escalate"
        assert changes[0].changed() == ["edits: False -> True"]

        # A change of the project's configuration lands in the same table with no session, so
        # the two kinds stay apart and `setting_changes` returns only the session's.
        await owner.execute(
            b"INSERT INTO config (scope, key, value) VALUES ('global', 'claude.model', '\"opus\"')"
        )
        cur = await owner.execute(
            "SELECT count(*) FROM audit_log WHERE session_id IS NULL AND table_name = 'config'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 1
        assert await herd.setting_changes(Db(owner), session_id) == changes

        # The role may not write the table itself, so it cannot forge an entry ...
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await session.execute(
                b"INSERT INTO audit_log (table_name, action) VALUES ('config', 'INSERT')"
            )
        # ... and the function refuses a session that does not exist.
        with pytest.raises(psycopg.errors.RaiseException):
            await session.execute(
                b"SELECT calu_session_setting_change("
                b"'00000000-0000-0000-0000-000000000000'::uuid, 'auto_escalate',"
                b" '{}'::jsonb, '{}'::jsonb, 'nobody')"
            )
    finally:
        await session.close()
        await owner.close()
