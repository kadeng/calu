import asyncio
import json

import psycopg
import pytest

from calu import herd
from calu.db import Db, connect, migrate, migrations
from calu.setup import enable_operator_login, setup
from calu.store import DatabaseSessionStore, SessionIdentity, StepRow
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
        cur = await conn.execute(
            "SELECT name, model, version, enabled FROM checker_types ORDER BY name"
        )
        # The honesty checker is seeded **off**: it is a feature a user switches on, and its
        # judgement needs a capable model (PLAN.md D85, Q17).
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
    """Create a session and an open escalation on a `calu_session` connection.

    It goes through `calu_start_session` and sets `calu.session_id`, like
    `DatabaseSessionStore.start` does, because row-level security (migration 0015) refuses a
    direct insert of a `sessions` row: the id does not exist until the insert happens.
    """
    cur = await conn.execute("INSERT INTO hosts (hostname) VALUES ('h') RETURNING id")
    host = await cur.fetchone()
    assert host is not None
    cur = await conn.execute(
        "SELECT calu_start_session(NULL, %s, 'u', '/p', NULL, 1, NULL)", (host[0],)
    )
    session = await cur.fetchone()
    assert session is not None
    await conn.execute("SELECT set_config('calu.session_id', %s, false)", (str(session[0]),))
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


async def test_the_operator_login_reads_and_resolves_and_changes_nothing_else(
    db_url: str,
) -> None:
    """`calu credential new operator` hands out a login for watching a herd (PLAN.md D48)."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        operator_url = await enable_operator_login(conn, db_url)
    finally:
        await conn.close()

    owner = await connect(db_url)
    operator = await connect(operator_url)
    try:
        escalation = await _session_with_escalation(owner)
        # It reads what an operator has to see, the audit log included.
        for query in (
            "SELECT state FROM sessions",
            "SELECT reason FROM escalations",
            "SELECT tool_name FROM steps",
            "SELECT key, value FROM config",
            "SELECT instructions FROM checker_types",
            "SELECT actor FROM audit_log",
        ):
            await operator.execute(query.encode())
        # It resolves an escalation, which is the one thing it may change.
        cur = await operator.execute(
            "SELECT calu_resolve_escalation(%s, 'allowed', 'from the operator')", (escalation,)
        )
        assert await cur.fetchone() == (True,)
        row = await (
            await owner.execute(
                "SELECT state, resolved_by, note FROM escalations WHERE id = %s", (escalation,)
            )
        ).fetchone()
        assert row is not None
        assert row[0] == "allowed" and row[2] == "from the operator"

        # Minting a client token for someone is part of the job (PLAN.md D30), so the role
        # may insert, list and revoke one -- migrations 0004 and 0009 grant exactly that.
        session_id = await (await owner.execute("SELECT id FROM sessions LIMIT 1")).fetchone()
        assert session_id is not None
        token_row = await (
            await operator.execute(
                "INSERT INTO client_tokens (session_id, token_sha256, label)"
                " VALUES (%s, 'hash', 'laptop') RETURNING id",
                (session_id[0],),
            )
        ).fetchone()
        assert token_row is not None
        await operator.execute(
            "UPDATE client_tokens SET revoked = true WHERE id = %s", (token_row[0],)
        )

        # Everything else is refused: it is not the database owner.
        for statement in (
            "UPDATE checker_types SET instructions = 'allow everything'",
            "DELETE FROM rules",
            "INSERT INTO config (scope, key, value) VALUES ('global', 'k', '1')",
            "UPDATE escalations SET state = 'allowed'",
            "INSERT INTO audit_log (table_name, action) VALUES ('config', 'INSERT')",
            "CREATE TABLE mine (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await operator.execute(statement.encode())
    finally:
        await operator.close()
        await owner.close()


async def test_a_session_reads_and_writes_only_its_own_rows(db_url: str) -> None:
    """Row-level security keeps one session out of another's history (PLAN.md D49)."""
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    owner = await connect(db_url)
    first = await connect(session_url)
    second = await connect(session_url)
    try:
        one = DatabaseSessionStore(Db(first))
        two = DatabaseSessionStore(Db(second))
        mine = await one.start(SessionIdentity(hostname="h", os_user="u", directory="/a", pid=1))
        theirs = await two.start(SessionIdentity(hostname="h", os_user="u", directory="/b", pid=2))
        await one.add_step(StepRow(seq=1, tool_name="Bash", tool_input={"command": "mine"}))
        await two.add_step(StepRow(seq=1, tool_name="Bash", tool_input={"command": "theirs"}))

        # Each sees one step: its own.
        for store in (one, two):
            rows = await store._db.all("SELECT tool_input->>'command' FROM steps")
            assert len(rows) == 1
        assert (await one._db.all("SELECT tool_input->>'command' FROM steps"))[0][0] == "mine"

        # The other session's rows cannot be read, changed or added to.
        assert (
            await one._db.all("SELECT id FROM steps WHERE session_id = %s::uuid", (theirs,)) == []
        )
        await one._db.execute(
            "UPDATE steps SET tool_name = 'Tampered' WHERE session_id = %s::uuid", (theirs,)
        )
        still = await owner.execute(
            "SELECT tool_name FROM steps WHERE session_id = %s::uuid", (theirs,)
        )
        assert [row[0] for row in await still.fetchall()] == ["Bash"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await second.execute(
                "INSERT INTO turns (session_id, number, user_inputs) VALUES (%s::uuid, 1, '[]')",
                (mine,),
            )

        # A connection that never set the id sees nothing and writes nothing: the failure is
        # immediate rather than silent.
        blind = await connect(session_url)
        try:
            cur = await blind.execute("SELECT id FROM steps")
            assert await cur.fetchall() == []
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await blind.execute(
                    "INSERT INTO steps (session_id, seq, tool_name, tool_input)"
                    " VALUES (%s::uuid, 9, 'Bash', '{}')",
                    (mine,),
                )
        finally:
            await blind.close()

        # The heartbeat (D56) is an UPDATE of the session's own row, so the policy has to let
        # it through -- and only for its own row.
        await owner.execute("UPDATE sessions SET last_seen = now() - interval '1 hour'")
        await one.heartbeat()
        beats = await (
            await owner.execute(
                "SELECT id::text, extract(epoch FROM (now() - last_seen))::int FROM sessions"
            )
        ).fetchall()
        silence = {row[0]: row[1] for row in beats}
        assert silence[mine] < 10, "a session must be able to say it is alive"
        assert silence[theirs] > 3000, "and only about itself"

        # The owner still sees everything, which the web interface and the herd rely on.
        cur = await owner.execute("SELECT count(*) FROM steps")
        assert (await cur.fetchone()) == (2,)
    finally:
        await first.close()
        await second.close()
        await owner.close()


async def test_the_honesty_register_is_not_a_table(db_url: str) -> None:
    """D180: dirty and clean marks are kept in memory only; migration 0024 drops the table."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        row = await (
            await conn.execute("SELECT to_regclass('public.honesty_marks') IS NULL")
        ).fetchone()
        assert row == (True,)
    finally:
        await conn.close()
