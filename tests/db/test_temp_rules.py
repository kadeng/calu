"""Temporary rules in the database: the table's limits, adding, listing by scope, removing.

The design is `agent/records/notes/temporary-rules-design.md`; the parts without a database
are in `tests/unit/test_temp_rules.py`.
"""

from datetime import timedelta

import psycopg
import pytest

from calu import temp_rules
from calu.db import Db, connect
from calu.setup import setup
from calu.temp_rules import RuleError

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def prepared(db_url: str) -> tuple[psycopg.AsyncConnection, Db]:
    """Set up a database with one registered project, "demo"."""
    conn = await connect(db_url)
    await setup(conn, db_url, "test:model")
    db = Db(conn)
    await db.execute("INSERT INTO projects (name) VALUES ('demo')")
    return conn, db


async def test_rules_are_listed_by_scope_until_they_expire_or_are_removed(db_url: str) -> None:
    conn, db = await prepared(db_url)
    try:
        everywhere = await temp_rules.add(
            db, "Ask before any push.", created_by="cli:test", scope="global"
        )
        project = await temp_rules.add(
            db,
            "Claude may run the database tests.",
            created_by="cli:test",
            scope="project",
            project="demo",
            duration=timedelta(days=3),
        )
        session = await temp_rules.add(
            db, "Claude may edit /etc/calu.", created_by="cli:test", conversation="conv-1"
        )
        assert session.scope == "session" and session.conversation == "conv-1"
        assert project.project == "demo" and project.where() == "project demo"
        assert project.expires_at - project.created_at == timedelta(days=3)
        assert everywhere.expires_at - everywhere.created_at == timedelta(days=1)

        ids = [rule.id for rule in await temp_rules.active(db, "demo", "conv-1")]
        assert ids == [everywhere.id, project.id, session.id]
        assert [rule.id for rule in await temp_rules.active(db, "other", "conv-2")] == [
            everywhere.id
        ]
        assert [rule.id for rule in await temp_rules.active(db)] == [everywhere.id]

        removed = await temp_rules.remove(db, project.id, "cli:test")
        assert removed is not None and removed.removed_by == "cli:test"
        assert await temp_rules.remove(db, project.id, "cli:test") is None  # only once
        assert project.id not in [rule.id for rule in await temp_rules.active(db, "demo")]

        # An expired rule no longer counts and cannot be removed; it stays for the record.
        await db.execute(
            "UPDATE temporary_rules SET created_at = created_at - interval '2 days',"
            " expires_at = expires_at - interval '2 days' WHERE id = %s",
            (everywhere.id,),
        )
        assert await temp_rules.active(db) == []
        assert await temp_rules.remove(db, everywhere.id, "cli:test") is None
        assert [rule.id for rule in await temp_rules.listed(db)] == [session.id]
        assert len(await temp_rules.listed(db, include_ended=True)) == 3

        # Every change is audited, as settings are.
        audited = await db.scalar(
            "SELECT count(*) FROM audit_log WHERE table_name = 'temporary_rules'"
        )
        assert audited == 5  # three inserts, one removal, the test's own update
    finally:
        await conn.close()


async def test_the_module_refuses_what_the_design_does_not_allow(db_url: str) -> None:
    conn, db = await prepared(db_url)
    try:
        with pytest.raises(RuleError, match="needs a text"):
            await temp_rules.add(db, "  ", created_by="cli:test", scope="global")
        with pytest.raises(RuleError, match="no project 'gone'"):
            await temp_rules.add(db, "x", created_by="cli:test", scope="project", project="gone")
        with pytest.raises(RuleError, match="conversation"):
            await temp_rules.add(db, "x", created_by="cli:test")
        with pytest.raises(RuleError, match="between one minute and 7 days"):
            await temp_rules.add(
                db, "x", created_by="cli:test", scope="global", duration=timedelta(days=8)
            )
        week = await temp_rules.add(
            db, "x", created_by="cli:test", scope="global", duration=timedelta(days=7)
        )
        assert week.expires_at - week.created_at == timedelta(days=7)
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        # eight days
        ("scope, expires_at", "'global', now() + interval '8 days'"),
        # below a minute
        ("scope, expires_at", "'global', now() + interval '30 seconds'"),
        # a session rule without its conversation
        ("scope, expires_at", "'session', now() + interval '1 hour'"),
        # a global rule with a conversation
        ("scope, conversation, expires_at", "'global', 'c', now() + interval '1 hour'"),
        # an unknown scope
        ("scope, expires_at", "'team', now() + interval '1 hour'"),
    ],
)
async def test_the_table_refuses_a_rule_outside_the_limits(
    db_url: str, columns: str, values: str
) -> None:
    """A client that skips `calu.temp_rules` cannot store such a rule either."""
    conn, db = await prepared(db_url)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await db.execute(
                f"INSERT INTO temporary_rules (text, created_by, {columns})"
                f" VALUES ('x', 'cli:test', {values})"
            )
    finally:
        await conn.close()
