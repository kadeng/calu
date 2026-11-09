import asyncio
import json

import psycopg
import pytest

from calu.db import connect, migrate, migrations
from calu.setup import setup

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
