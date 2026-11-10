-- calu schema, version 15: a session may read and write only its own rows (PLAN.md D49, Q3).
--
-- Until now one `calu_session` login could read and write **every** session's rows: the grants
-- of migration 0001 are per table, not per session. A compromised session process could
-- therefore rewrite another session's history -- its steps, its turns, its escalations -- and
-- so change what a human reviewing that other session sees.
--
-- Row-level security closes that without a role per session (which would need the owner
-- credential at every session start and a reaper for the roles of sessions that are gone). The
-- session process sets one connection setting after it connects:
--
--     SELECT set_config('calu.session_id', '<its session id>', false);
--
-- and every policy below compares against it. `current_setting(..., true)` returns NULL when
-- nobody set it, and a NULL comparison is false, so a connection that forgets the setting sees
-- nothing and writes nothing -- a loud failure at the session's first write rather than a quiet
-- one later (measured: `spikes/RESULTS.md`, "Row-level security instead of a role per session").
--
-- Two things do not work under the policies and get a function that runs as the owner instead:
--
--   * a session creates its own `sessions` row, and at that moment the id does not exist yet,
--     so `WITH CHECK` would refuse the insert -- `calu_start_session` creates it and returns
--     the id for the session to set;
--   * `session.list` and `calu run --resume` show the recent sessions of a directory with the
--     first prompt of each, which spans other sessions' rows -- `calu_recent_sessions` returns
--     exactly those columns and nothing else of them.
--
-- The database owner is the tables' owner, so no policy applies to it (the tables are not
-- `FORCE`d): `calu setup`, the configuration commands and the web interface are unaffected.
-- `calu_operator` gets read policies, because a herd has to see everything.

-- ---------------------------------------------------------------- the two owner-side helpers

CREATE FUNCTION calu_start_session(
    project_id bigint,
    host_id bigint,
    os_user text,
    directory text,
    git_commit text,
    pid integer,
    model text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    new_id uuid;
BEGIN
    -- `started_by` defaults to `current_user`, which inside a SECURITY DEFINER function is the
    -- owner; `session_user` is the role that logged in, which is what the column is for.
    INSERT INTO sessions (project_id, host_id, os_user, directory, git_commit, pid, model,
                          started_by)
    VALUES (calu_start_session.project_id, calu_start_session.host_id,
            calu_start_session.os_user, calu_start_session.directory,
            calu_start_session.git_commit, calu_start_session.pid, calu_start_session.model,
            session_user)
    RETURNING id INTO new_id;
    RETURN new_id;
END
$$;

REVOKE ALL ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text)
    TO calu_session, calu_operator;

CREATE FUNCTION calu_recent_sessions(for_directory text, max_rows integer)
RETURNS TABLE (
    id text,
    claude_session_id text,
    state text,
    directory text,
    started_at timestamptz,
    ended_at timestamptz,
    first_input text
) LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.id::text, s.claude_session_id, s.state, s.directory, s.started_at, s.ended_at,
           coalesce((SELECT t.user_inputs->>0 FROM turns t
                      WHERE t.session_id = s.id ORDER BY t.number LIMIT 1), '')
      FROM sessions s
     WHERE s.directory = for_directory
     ORDER BY s.started_at DESC
     LIMIT max_rows;
$$;

REVOKE ALL ON FUNCTION calu_recent_sessions(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_recent_sessions(text, integer) TO calu_session, calu_operator;

-- A conversation can span several `sessions` rows: `calu run --resume` and a fork continue a
-- Claude session under a new row (D27, D36), and the new session has to read the turns and the
-- chat summary of its predecessors -- that is what carries the conversation over. The policies
-- below therefore let a session **read** the rows of any session that shares its Claude session
-- id, while it may still write only its own. Both helpers run as the owner, because the
-- `sessions` policy would otherwise hide the sibling rows from the lookup itself.

-- Rows that hold turns come first, then the newest: a session that is about to read a
-- conversation has written its id to its own row (`_claim_conversation`) and is itself the
-- newest row of that conversation while still being empty, so ordering by time alone would
-- answer with the empty row rather than with the conversation.
CREATE FUNCTION calu_conversation_sessions(claude_session_id text)
RETURNS TABLE (id uuid, started_at timestamptz, turn_count bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.id, s.started_at, (SELECT count(*) FROM turns t WHERE t.session_id = s.id)
      FROM sessions s
     WHERE s.claude_session_id = calu_conversation_sessions.claude_session_id
     ORDER BY (SELECT count(*) FROM turns t WHERE t.session_id = s.id) > 0 DESC,
              s.started_at DESC;
$$;

REVOKE ALL ON FUNCTION calu_conversation_sessions(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_conversation_sessions(text) TO calu_session, calu_operator;

CREATE FUNCTION calu_same_conversation(other uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
          FROM sessions mine, sessions theirs
         WHERE mine.id = current_setting('calu.session_id', true)::uuid
           AND theirs.id = calu_same_conversation.other
           AND mine.claude_session_id IS NOT NULL
           AND mine.claude_session_id = theirs.claude_session_id
    );
$$;

REVOKE ALL ON FUNCTION calu_same_conversation(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_same_conversation(uuid) TO calu_session;

-- ---------------------------------------------------------------- the policies

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE checker_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_tokens ENABLE ROW LEVEL SECURITY;

CREATE POLICY session_own ON sessions TO calu_session
    USING (id = current_setting('calu.session_id', true)::uuid OR calu_same_conversation(id))
    WITH CHECK (id = current_setting('calu.session_id', true)::uuid);
-- Readable for the whole conversation (a resume or a fork continues it under a new row),
-- writable only for this session: an UPDATE of a sibling's row fails the WITH CHECK.
CREATE POLICY session_own ON turns TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid
           OR calu_same_conversation(session_id))
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
-- Readable for the whole conversation (a resume or a fork continues it under a new row),
-- writable only for this session: an UPDATE of a sibling's row fails the WITH CHECK.
CREATE POLICY session_own ON steps TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid
           OR calu_same_conversation(session_id))
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY session_own ON escalations TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
-- Readable for the whole conversation (a resume or a fork continues it under a new row),
-- writable only for this session: an UPDATE of a sibling's row fails the WITH CHECK.
CREATE POLICY session_own ON chat_summaries TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid
           OR calu_same_conversation(session_id))
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY session_own ON comments TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY session_own ON agents TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);
CREATE POLICY session_own ON client_tokens TO calu_session
    USING (session_id = current_setting('calu.session_id', true)::uuid)
    WITH CHECK (session_id = current_setting('calu.session_id', true)::uuid);

-- `checker_runs` hangs off a step rather than a session, so its policy goes through `steps`,
-- whose own policy narrows the subquery to this session's steps.
CREATE POLICY session_own ON checker_runs TO calu_session
    USING (step_id IN (SELECT id FROM steps))
    WITH CHECK (step_id IN (SELECT id FROM steps));

-- A herd reads every row. `calu_operator` gets SELECT everywhere and, on `client_tokens`, the
-- insert and update that minting and revoking need (D30, migrations 0004 and 0009).
CREATE POLICY operator_reads ON sessions FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON turns FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON steps FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON escalations FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON checker_runs FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON chat_summaries FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON comments FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_reads ON agents FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_tokens ON client_tokens TO calu_operator USING (true) WITH CHECK (true);
