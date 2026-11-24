import asyncio
import hashlib
import json
import secrets

import psycopg
import pytest

from calu import herd
from calu.context.turns import TurnRecord
from calu.db import Db, connect, migrate, migrations, roles
from calu.setup import setup
from calu.store import (
    CheckerRun,
    DatabaseSessionStore,
    SessionIdentity,
    StepDecision,
    StepRow,
)
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


async def test_a_database_of_the_replaced_migrations_is_refused_until_it_is_reset(
    db_url: str,
) -> None:
    """D266: a database whose `schema_migrations` names the 44 migrations the baseline
    replaced cannot be migrated from here: `migrate` and `setup_database` refuse it with a
    message that names `calu db reset`, change nothing, and a reset (the schema dropped and
    created again, as `calu db reset` does) makes it migrate normally."""
    from calu.db import EarlierSchema, earlier_migrations
    from calu.setup import SetupRefused, setup_database

    conn = await connect(db_url)
    try:
        await migrate(conn)
        # What the old chain left behind: its own names under the same version numbers.
        await conn.execute("DELETE FROM schema_migrations")
        await conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES"
            " (1, '0001_initial.sql'), (2, '0002_turn_records.sql')"
        )
        assert await earlier_migrations(conn) == ["0001_initial.sql", "0002_turn_records.sql"]
        with pytest.raises(EarlierSchema, match="calu db reset"):
            await migrate(conn)
        with pytest.raises(SetupRefused, match="calu db reset"):
            await setup_database(conn, "test:model")
        rows = await (await conn.execute("SELECT count(*) FROM schema_migrations")).fetchone()
        assert rows == (2,), "a refusal changes nothing"

        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        assert [m.name for m in await migrate(conn)] == ["0001_baseline.sql"]
        assert await earlier_migrations(conn) == []
    finally:
        await conn.close()


async def test_setup_seeds_defaults_once_and_gives_the_user_login(db_url: str) -> None:
    """`setup` seeds the checker types once and returns a working `calu_admin` login."""
    conn = await connect(db_url)
    try:
        _, user_url = await setup(conn, db_url, "test:model")
        cur = await conn.execute(
            "SELECT name, model, version, enabled FROM checker_types ORDER BY name"
        )
        # The honesty checker is seeded **off**: it is a feature a user switches on, and its
        # judgement needs a capable model (agent/records/PLAN.md D85, Q17).
        assert await cur.fetchall() == [
            ("changeset", "test:model", 1, True),
            ("command", "test:model", 1, True),
            ("edit", "test:model", 1, True),
            ("honesty", "test:model", 1, False),
            ("narrator", "test:model", 1, True),
            ("subagent-result", "test:model", 1, True),
        ]
        cur = await conn.execute("SELECT count(*) FROM rules")
        row = await cur.fetchone()
        assert row is not None
        n_rules = row[0]
        assert n_rules == 0, "D179: calu ships no allow or deny rules"
        report, user_url = await setup(conn, db_url, "other:model")
        assert "checker types added: 0; rules added: 0" in report
    finally:
        await conn.close()
    user = await connect(user_url)
    try:
        cur = await user.execute("SELECT current_user")
        assert await cur.fetchone() == (roles.ADMIN,)
    finally:
        await user.close()


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
    """Create a session and an open escalation on the given connection.

    It goes through `calu_start_session` and binds the connection with the session id and the
    session key, like `DatabaseSessionStore.start` does, because row-level security refuses a
    direct insert of a `sessions` row (migration 0015) and gives access only to the session
    whose key the connection holds (migration 0025).
    """
    cur = await conn.execute(
        "INSERT INTO hosts (hostname) VALUES ('h')"
        " ON CONFLICT (hostname) DO UPDATE SET last_seen = now() RETURNING id"
    )
    host = await cur.fetchone()
    assert host is not None
    key = secrets.token_urlsafe(16)
    cur = await conn.execute(
        "SELECT calu_start_session(NULL, %s, 'u', '/p', NULL, 1, NULL, %s)",
        (host[0], hashlib.sha256(key.encode()).hexdigest()),
    )
    session = await cur.fetchone()
    assert session is not None
    await conn.execute(
        "SELECT set_config('calu.session_id', %s, false),"
        " set_config('calu.session_key', %s, false)",
        (str(session[0]), key),
    )
    cur = await conn.execute(
        "INSERT INTO escalations (session_id, reason) VALUES (%s, 'why') RETURNING id",
        (session[0],),
    )
    escalation = await cur.fetchone()
    assert escalation is not None
    return escalation[0]


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


async def test_the_user_login_checks_a_client_token_of_a_session_it_runs(db_url: str) -> None:
    """D30: a session run as `calu_admin` confirms the hash it was given, and only that of a
    valid token of the session it names."""
    conn = await connect(db_url)
    try:
        _, user_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    owner = await connect(db_url)
    session = await connect(user_url)
    try:
        db = Db(owner)
        mine = await DatabaseSessionStore(Db(session)).start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=1, os="Linux")
        )
        other = await DatabaseSessionStore(db).start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=2, os="Linux")
        )
        _, token = await herd.mint_token(db, mine, "an editor")
        revoked_id, revoked = await herd.mint_token(db, mine, "an old editor")
        assert await herd.revoke_token(db, revoked_id)
        _, foreign = await herd.mint_token(db, other, "another session")
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
        await owner.close()
        await session.close()


async def test_the_user_login_runs_a_session_through_every_statement_the_store_uses(
    db_url: str,
) -> None:
    """D265: sessions log in as `calu_admin`. Two sessions of one conversation (a resume): the
    second reads the first one's summary, and each writes its rows through every statement of
    `DatabaseSessionStore`."""
    conn = await connect(db_url)
    try:
        _, user_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    owner = await connect(db_url)
    first_conn = await connect(user_url)
    second_conn = await connect(user_url)
    try:
        first = DatabaseSessionStore(Db(first_conn))
        first_id = await first.start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=1, os="Linux")
        )
        await first.set_claude_session("conversation-1")
        await first.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "deploy"}, turn=1)
        )
        await first.save_turn(TurnRecord(index=1, user_inputs=["deploy it"]))
        await first.save_summary("what happened so far", 1)

        second = DatabaseSessionStore(Db(second_conn))
        second_id = await second.start(
            SessionIdentity(hostname="box", os_user="u", directory="/w", pid=2, os="Linux")
        )
        assert second_id != first_id
        turns, summary = await second.load_previous("conversation-1")
        assert [t.user_inputs for t in turns] == [] and summary == "what happened so far"

        own_step = await second.add_step(StepRow(seq=1, tool_name="Bash", tool_input={}, turn=2))
        await second.decide_step(own_step, StepDecision(decision="allow", summary="Lists."))
        await second.add_checker_run(
            own_step,
            CheckerRun(checker_name="command", checker_version=1, model="m", decision="pass"),
        )
        await second.finish_step(own_step, "ok")
        await second.save_turn(TurnRecord(index=2, user_inputs=["next"]))
        await second.save_turn(TurnRecord(index=2, user_inputs=["next"], answer="done"))
        await second.mark_folded(2)
        await second.save_summary("more happened", 2)
        await second.open_escalation("deploy?", {"rendered": "x"}, own_step)
        await second.heartbeat()
        await second.end("stopped")
        await first.end("stopped")

        cur = await owner.execute("SELECT count(*) FROM checker_runs")
        assert await cur.fetchone() == (1,)
        cur = await owner.execute(
            "SELECT state, started_by FROM sessions WHERE id = %s", (second_id,)
        )
        assert await cur.fetchone() == ("stopped", roles.ADMIN)
    finally:
        await owner.close()
        await first_conn.close()
        await second_conn.close()


async def test_a_session_records_its_own_setting_changes(db_url: str) -> None:
    """D43: a session run as `calu_admin` writes the audit entry of a change of its own
    settings through the function; an operator reads it back with the session."""
    conn = await connect(db_url)
    try:
        _, user_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    session = await connect(user_url)
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
    finally:
        await session.close()
        await owner.close()


async def test_migration_43_leaves_one_user_login(db_url: str) -> None:
    """D265: after the migrations `calu_session` and `calu_operator` cannot log in and have no
    password; `calu_admin` is a member of `calu_operator`. The roles are server-wide: a test
    that gave one of them a login before is undone by migration 0043 itself (re-applied here
    on a fresh database). Reads `pg_authid`, which needs the test server's owner to be a
    superuser, as the test container's is."""
    conn = await connect(db_url)
    try:
        await migrate(conn)
        cur = await conn.execute(
            "SELECT rolname, rolcanlogin, rolpassword IS NULL FROM pg_authid"
            " WHERE rolname = ANY(%s) ORDER BY rolname",
            ([roles.SESSION, roles.OPERATOR],),
        )
        assert await cur.fetchall() == [(roles.OPERATOR, False, True), (roles.SESSION, False, True)]
        cur = await conn.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER')", (roles.ADMIN, roles.OPERATOR)
        )
        assert await cur.fetchone() == (True,)
    finally:
        await conn.close()


async def test_the_honesty_register_is_not_a_table(db_url: str) -> None:
    """D180: dirty and clean marks are kept in memory only; the schema has no table for them."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        row = await (
            await conn.execute("SELECT to_regclass('public.honesty_marks') IS NULL")
        ).fetchone()
        assert row == (True,)
    finally:
        await conn.close()


async def test_no_usage_limits_are_stored(db_url: str) -> None:
    """D250: the usage limits are not stored: no table, no functions for them."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        row = await (
            await conn.execute(
                "SELECT to_regclass('public.usage_limits') IS NULL,"
                " (SELECT count(*) FROM pg_proc WHERE proname IN"
                "  ('calu_usage_limits', 'calu_note_usage_limit', 'calu_usage_account'))"
            )
        ).fetchone()
        assert row == (True, 0)
    finally:
        await conn.close()
