"""Would row-level security keep one session out of another session's rows? (Q3)

PLAN.md Q3 asks whether calu should give every session its own database role. The
recommendation there is **not** a role per session -- it needs the owner credential at session
start and a reaper for dead roles -- but row-level security on the one `calu_session` login,
keyed on a session id the connection sets once. That is cheap to describe and easy to get
wrong, so this spike builds it in a throwaway database and measures what it does.

    docker compose up -d caludb --wait
    uv run python spikes/spike_session_rls.py

What it tries, with the candidate policies applied to the tables a session writes:

1. a session that sets its id reads and writes its own rows;
2. it cannot see another session's rows, and an UPDATE of them changes nothing;
3. the SECURITY DEFINER functions still work for its own escalation;
4. a connection that **forgets** to set the id sees nothing and can insert nothing -- the
   failure mode to weigh against the protection;
5. `calu_operator` still sees every row, which a herd needs;
6. the **bootstrap**: a session creates its own `sessions` row (`store.start`), and at that
   moment it cannot know the id the database will generate, so a `WITH CHECK` policy on
   `sessions` refuses the insert. The spike measures that refusal and then the way around it: a
   SECURITY DEFINER function that creates the row and returns the id, which the session sets
   before it writes anything else.

Nothing here is applied to a real database: the SQL lives in this file as a proposal, and the
verdict is what Q3 needs in order to be decided.
"""

import asyncio
import sys
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu.setup import enable_operator_login, setup

TEST_SERVER = "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu"

# The proposal. `calu.session_id` is a per-connection setting that the session process sets once
# after it connects; `current_setting(..., true)` returns NULL when nobody set it, and a NULL
# comparison is false, so an unset connection matches no row.
POLICIES = """
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE checker_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_summaries ENABLE ROW LEVEL SECURITY;

CREATE POLICY own_session ON sessions TO calu_session
    USING (id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY own_turns ON turns TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY own_steps ON steps TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY own_escalations ON escalations TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY own_checker_runs ON checker_runs TO calu_session
    USING (step_id IN (SELECT id FROM steps))
    WITH CHECK (step_id IN (SELECT id FROM steps));
CREATE POLICY own_summary ON chat_summaries TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);

-- The bootstrap: a session cannot insert its own `sessions` row under the policy above,
-- because the id does not exist until the insert happens. This function runs as the owner, so
-- no policy applies to it, and hands the new id back for the session to set.
CREATE FUNCTION calu_start_session(for_host bigint, os_user text, directory text, pid int)
    RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    new_id uuid;
BEGIN
    INSERT INTO sessions (host_id, os_user, directory, pid)
    VALUES (for_host, os_user, directory, pid)
    RETURNING id INTO new_id;
    RETURN new_id;
END
$$;
GRANT EXECUTE ON FUNCTION calu_start_session(bigint, text, text, int) TO calu_session;

-- A herd has to see everything, so the operator gets a policy of its own rather than the
-- BYPASSRLS attribute, which only a superuser can grant.
CREATE POLICY operator_reads ON sessions FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON turns FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON steps FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON escalations FOR ALL TO calu_operator USING (true);
CREATE POLICY operator_reads ON checker_runs FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON chat_summaries FOR SELECT TO calu_operator USING (true);
"""


async def make_session(owner: psycopg.AsyncConnection, directory: str) -> str:
    """Create a host and a session row as the owner, and return the session id.

    Args:
        owner: An owner connection (row-level security does not apply to it).
        directory: The project directory of the session row.

    Returns:
        The id of the new `sessions` row.
    """
    cur = await owner.execute(
        "INSERT INTO hosts (hostname) VALUES ('spike') ON CONFLICT (hostname) DO UPDATE"
        " SET hostname = 'spike' RETURNING id"
    )
    host = await cur.fetchone()
    assert host is not None
    cur = await owner.execute(
        "INSERT INTO sessions (host_id, os_user, directory, pid) VALUES (%s, 'u', %s, 1)"
        " RETURNING id",
        (host[0], directory),
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def rows(conn: psycopg.AsyncConnection, query: str) -> int:
    """Count the rows a query returns on one connection.

    Args:
        conn: The connection.
        query: A query.

    Returns:
        How many rows came back.
    """
    cur = await conn.execute(query.encode())
    return len(await cur.fetchall())


async def main() -> int:
    """Build the policies in a throwaway database and print what they do.

    Returns:
        0 if the separation holds and the operator still sees everything, 1 otherwise.
    """
    name = f"calu_rls_{uuid.uuid4().hex[:8]}"
    server = await psycopg.AsyncConnection.connect(TEST_SERVER, autocommit=True)
    await server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(TEST_SERVER)
    params["dbname"] = name
    url = make_conninfo("", **params)
    verdicts: dict[str, bool] = {}
    try:
        owner = await psycopg.AsyncConnection.connect(url, autocommit=True)
        _, session_url = await setup(owner, url, "test:model")
        operator_url = await enable_operator_login(owner, url)
        first = await make_session(owner, "/one")
        second = await make_session(owner, "/two")
        await owner.execute(POLICIES.encode())

        a = await psycopg.AsyncConnection.connect(session_url, autocommit=True)
        b = await psycopg.AsyncConnection.connect(session_url, autocommit=True)
        blind = await psycopg.AsyncConnection.connect(session_url, autocommit=True)
        operator = await psycopg.AsyncConnection.connect(operator_url, autocommit=True)
        try:
            await a.execute("SELECT set_config('calu.session_id', %s, false)", (first,))
            await b.execute("SELECT set_config('calu.session_id', %s, false)", (second,))

            # 1. each session writes and reads its own rows
            for conn, session in ((a, first), (b, second)):
                await conn.execute(
                    "INSERT INTO steps (session_id, seq, tool_name, tool_input)"
                    " VALUES (%s, 1, 'Bash', '{}'::jsonb)",
                    (session,),
                )
            verdicts["a session writes and reads its own rows"] = (
                await rows(a, "SELECT id FROM steps") == 1
            )

            # 2. and nothing of the other's
            verdicts["it cannot see the other session's rows"] = (
                await rows(a, "SELECT id FROM steps WHERE session_id = '" + second + "'") == 0
            )
            cur = await a.execute(
                "UPDATE steps SET tool_name = 'Tampered' WHERE session_id = %s", (second,)
            )
            verdicts["it cannot update the other session's rows"] = cur.rowcount == 0
            try:
                await a.execute(
                    "INSERT INTO steps (session_id, seq, tool_name, tool_input)"
                    " VALUES (%s, 2, 'Bash', '{}'::jsonb)",
                    (second,),
                )
                verdicts["it cannot write a row for the other session"] = False
            except psycopg.errors.InsufficientPrivilege:
                verdicts["it cannot write a row for the other session"] = True

            # 3. the SECURITY DEFINER path still works for its own escalation
            cur = await a.execute(
                "INSERT INTO escalations (session_id, reason) VALUES (%s, 'why') RETURNING id",
                (first,),
            )
            row = await cur.fetchone()
            assert row is not None
            cur = await a.execute("SELECT calu_cancel_escalation(%s, 'stopped')", (row[0],))
            verdicts["its own escalation can still be cancelled"] = (await cur.fetchone()) == (
                True,
            )

            # 4. a connection that forgets to set the id
            verdicts["a connection without the id sees nothing"] = (
                await rows(blind, "SELECT id FROM steps") == 0
            )
            try:
                await blind.execute(
                    "INSERT INTO steps (session_id, seq, tool_name, tool_input)"
                    " VALUES (%s, 9, 'Bash', '{}'::jsonb)",
                    (first,),
                )
                verdicts["a connection without the id cannot write"] = False
            except psycopg.errors.InsufficientPrivilege:
                verdicts["a connection without the id cannot write"] = True

            # 6. the bootstrap: the plain insert a session does today is refused ...
            cur = await a.execute("SELECT id FROM hosts LIMIT 1")
            host = await cur.fetchone()
            assert host is not None
            try:
                await a.execute(
                    "INSERT INTO sessions (host_id, os_user, directory, pid)"
                    " VALUES (%s, 'u', '/three', 3) RETURNING id",
                    (host[0],),
                )
                verdicts["a session cannot insert its own row directly"] = False
            except psycopg.errors.InsufficientPrivilege:
                verdicts["a session cannot insert its own row directly"] = True
            # ... and the SECURITY DEFINER starter is the way around it.
            fresh = await psycopg.AsyncConnection.connect(session_url, autocommit=True)
            try:
                cur = await fresh.execute(
                    "SELECT calu_start_session(%s, 'u', '/three', 3)", (host[0],)
                )
                row = await cur.fetchone()
                third = str(row[0]) if row else ""
                await fresh.execute("SELECT set_config('calu.session_id', %s, false)", (third,))
                await fresh.execute(
                    "INSERT INTO steps (session_id, seq, tool_name, tool_input)"
                    " VALUES (%s, 1, 'Bash', '{}'::jsonb)",
                    (third,),
                )
                verdicts["a starter function creates the row and the session takes it over"] = (
                    bool(third) and await rows(fresh, "SELECT id FROM steps") == 1
                )
            finally:
                await fresh.close()

            # 5. the herd still sees everything
            verdicts["the operator still sees every session's rows"] = (
                await rows(operator, "SELECT id FROM steps") == 3
                and await rows(operator, "SELECT id FROM sessions") == 3
            )
        finally:
            for conn in (a, b, blind, operator):
                await conn.close()
            await owner.close()
    finally:
        await server.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await server.close()

    print("verdict:")
    for said, ok in verdicts.items():
        print(f"  {'yes' if ok else 'NO '}  {said}")
    return 0 if all(verdicts.values()) else 1


sys.exit(asyncio.run(main()))
