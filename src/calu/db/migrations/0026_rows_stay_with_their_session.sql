-- calu schema, version 26: a row stays with the session that wrote it.
--
-- Migration 0025 lets the sessions of one conversation read each other's history (a resume or
-- a fork continues a conversation under a new `sessions` row and needs the earlier turns). Its
-- policies on `sessions`, `turns`, `steps` and `chat_summaries` were single policies for all
-- commands. PostgreSQL applies such a policy's USING clause to the row an UPDATE finds and its
-- WITH CHECK clause to the row the UPDATE leaves, so "readable for the conversation" also
-- decided which rows an UPDATE could start from.
--
-- From this migration on, reading and writing have separate policies:
--
--     command   which existing rows             which resulting rows
--     SELECT    own session, same conversation  --
--     INSERT    --                              own session
--     UPDATE    own session                     own session
--     DELETE    none (the role has no DELETE privilege either)
--
-- "own session" is `calu_current_session()`, the session the connection has proven to be
-- (migration 0025). An UPDATE therefore only ever touches rows of the proven session and
-- cannot change which session a row belongs to. A new `sessions` row is created by
-- `calu_start_session` only, so that table gets no INSERT policy.
--
-- `checker_runs` and `escalations` point at a step. A session adds them for its own steps
-- only; it still reads the checker runs of the conversation's steps.

DROP POLICY session_own ON sessions;
DROP POLICY session_own ON turns;
DROP POLICY session_own ON steps;
DROP POLICY session_own ON chat_summaries;
DROP POLICY session_own ON checker_runs;
DROP POLICY session_own ON escalations;

CREATE POLICY session_reads ON sessions FOR SELECT TO calu_session
    USING (id = (SELECT calu_current_session()) OR calu_same_conversation(id));
CREATE POLICY session_updates ON sessions FOR UPDATE TO calu_session
    USING (id = (SELECT calu_current_session()))
    WITH CHECK (id = (SELECT calu_current_session()));

CREATE POLICY session_reads ON turns FOR SELECT TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id));
CREATE POLICY session_inserts ON turns FOR INSERT TO calu_session
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_updates ON turns FOR UPDATE TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));

CREATE POLICY session_reads ON steps FOR SELECT TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id));
CREATE POLICY session_inserts ON steps FOR INSERT TO calu_session
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_updates ON steps FOR UPDATE TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));

CREATE POLICY session_reads ON chat_summaries FOR SELECT TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id));
CREATE POLICY session_inserts ON chat_summaries FOR INSERT TO calu_session
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_updates ON chat_summaries FOR UPDATE TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));

-- True if `step` is a step of the session this connection has proven to be. A function, so
-- that the answer does not depend on which rows of `steps` the caller may read.
CREATE FUNCTION calu_own_step(step bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM steps s
         WHERE s.id = calu_own_step.step AND s.session_id = calu_current_session()
    );
$$;

COMMENT ON FUNCTION calu_own_step(bigint) IS
    'Whether the step belongs to the session this connection has proven to be (migration 0026).';

REVOKE ALL ON FUNCTION calu_own_step(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_own_step(bigint) TO calu_session;

CREATE POLICY session_reads ON checker_runs FOR SELECT TO calu_session
    USING (step_id IN (SELECT id FROM steps));
CREATE POLICY session_inserts ON checker_runs FOR INSERT TO calu_session
    WITH CHECK (calu_own_step(step_id));

CREATE POLICY session_reads ON escalations FOR SELECT TO calu_session
    USING (session_id = (SELECT calu_current_session()));
CREATE POLICY session_inserts ON escalations FOR INSERT TO calu_session
    WITH CHECK (session_id = (SELECT calu_current_session())
                AND (step_id IS NULL OR calu_own_step(step_id)));

-- No escalation stays open for a session that is gone.
--
-- An open escalation is a question to a human, and only a running session process acts on
-- the answer. Two functions close the ones nobody waits for any more. Both record
-- 'cancelled', which is not a decision: nobody allowed or denied the step (PLAN.md D57).
--
--     calu_close_session_escalations(session, note)   every open escalation of one session;
--         the session calls it for itself when it ends, an operator may call it for any
--         (for a session whose process it found to be gone).
--     calu_close_gone_escalations(silent_s)           every open escalation of a session that
--         has ended, or that has not stamped `sessions.last_seen` for more than `silent_s`
--         seconds although the database server was up all that time; for operators, called
--         when the herd is listed.
--
-- Silence alone does not prove that a process is gone (a suspended machine, a network that
-- was away), and a session process that is still alive reads the cancellation when it is
-- connected again and treats the step as not allowed. `silent_s` is therefore meant to be
-- long, and a restart of the database server does not count as silence of its sessions.

CREATE FUNCTION calu_close_session_escalations(p_session uuid, resolution_note text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    closed integer;
BEGIN
    IF NOT calu_caller_is_operator()
       AND p_session IS DISTINCT FROM calu_current_session() THEN
        RAISE EXCEPTION 'a session closes its own escalations only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    WITH done AS (
        UPDATE escalations e
           SET state = 'cancelled', resolved_by = session_user, resolved_at = now(),
               note = coalesce(resolution_note, e.note)
         WHERE e.session_id = p_session AND e.state = 'open'
        RETURNING e.id, e.session_id
    ), told AS (
        SELECT pg_notify('escalation_resolved', json_build_object(
                   'id', done.id, 'session_id', done.session_id, 'state', 'cancelled')::text)
          FROM done
    )
    SELECT count(*) INTO closed FROM told;
    RETURN closed;
END
$$;

COMMENT ON FUNCTION calu_close_session_escalations(uuid, text) IS
    'Cancels every open escalation of one session; a session may name only itself (0026).';

REVOKE ALL ON FUNCTION calu_close_session_escalations(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_close_session_escalations(uuid, text)
    TO calu_session, calu_operator;

CREATE FUNCTION calu_close_gone_escalations(silent_s double precision)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    closed integer;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator closes the escalations of other sessions'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF silent_s IS NULL OR silent_s <= 0 THEN
        RAISE EXCEPTION 'silent_s must be a positive number of seconds';
    END IF;
    WITH done AS (
        UPDATE escalations e
           SET state = 'cancelled', resolved_by = 'calu: the session is gone',
               resolved_at = now(),
               note = CASE WHEN s.ended_at IS NOT NULL
                           THEN 'the session ended before anybody answered'
                           ELSE 'the session''s process is gone: no heartbeat for '
                                || round(extract(epoch FROM (now() - s.last_seen)))::text
                                || ' s' END
          FROM sessions s
         WHERE s.id = e.session_id AND e.state = 'open'
           AND (s.ended_at IS NOT NULL
                OR (s.last_seen < now() - make_interval(secs => silent_s)
                    AND pg_postmaster_start_time() < now() - make_interval(secs => silent_s)))
        RETURNING e.id, e.session_id
    ), told AS (
        SELECT pg_notify('escalation_resolved', json_build_object(
                   'id', done.id, 'session_id', done.session_id, 'state', 'cancelled')::text)
          FROM done
    )
    SELECT count(*) INTO closed FROM told;
    RETURN closed;
END
$$;

COMMENT ON FUNCTION calu_close_gone_escalations(double precision) IS
    'Cancels the open escalations of sessions that ended, or were silent that long while the '
    'server was up (0026).';

REVOKE ALL ON FUNCTION calu_close_gone_escalations(double precision) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_close_gone_escalations(double precision) TO calu_operator;
