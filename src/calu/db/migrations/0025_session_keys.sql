-- calu schema, version 25: a session proves which session it is with a key; fork points for
-- the session role; the directory a project is registered for.
--
-- 1. Session keys.
--
-- Migration 0015 bound a `calu_session` connection to its `sessions` row with the connection
-- setting `calu.session_id`, and every policy compared against that setting. Any connection
-- can set it to any value, so whoever held the shared `calu_session` login could name another
-- session's id and then read and write that session's rows, insert a client token for it and
-- record a client decision that allowed its open escalation. Three functions that run as the
-- owner took a session id or an escalation id without asking whose it was
-- (`calu_cancel_escalation`, `calu_session_setting_change`, `calu_revoke_session_tokens`).
--
-- From this migration on the identity is proven, not asserted:
--
--     calu process                                   database
--     key = 32 random bytes
--     calu_start_session(..., sha256(key))  ───────► sessions row + session_keys row (hash only)
--     set_config('calu.session_id',  id)
--     set_config('calu.session_key', key)   ───────► calu_current_session() returns the id only
--                                                    when sha256(calu.session_key) is the hash
--                                                    stored for calu.session_id, else NULL
--
-- The key lives in the memory of the calu process and nowhere else; `session_keys` holds its
-- hash and is readable by the owner only. Every `session_own` policy compares against
-- `calu_current_session()`, and the owner-side functions that act on a session refuse a caller
-- of the role `calu_session` whose proven session is a different one. The key also survives a
-- reconnect: the process sets both values again on the new connection.
--
-- A `sessions` row from before this migration has no key, so a session process that was
-- running during the migration loses access to its rows (it fails closed and stops after
-- `store_failures_before_stop` failed writes). Stop the sessions before migrating.
--
-- What the `calu_session` login alone can still do: start new sessions, read the
-- configuration, list the recent sessions of a directory it names (`calu_recent_sessions`) and
-- read the turns of a conversation whose Claude session id it knows and claims (resume and
-- fork need that). It cannot write or decide for a session whose key it does not hold.

CREATE TABLE session_keys (
    session_id  uuid PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
    key_sha256  text NOT NULL CHECK (key_sha256 ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE session_keys IS
    'SHA-256 of the key each session process holds; only the owner reads it (migration 0025).';

REVOKE ALL ON session_keys FROM PUBLIC;
REVOKE ALL ON session_keys FROM calu_session, calu_operator;

CREATE FUNCTION calu_current_session() RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    claimed text := nullif(current_setting('calu.session_id', true), '');
    secret  text := nullif(current_setting('calu.session_key', true), '');
    found   uuid;
BEGIN
    IF claimed IS NULL OR secret IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        SELECT k.session_id INTO found
          FROM session_keys k
         WHERE k.session_id = claimed::uuid
           AND k.key_sha256 = encode(sha256(convert_to(secret, 'UTF8')), 'hex');
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN NULL;  -- calu.session_id is not a uuid
    END;
    RETURN found;
END
$$;

COMMENT ON FUNCTION calu_current_session() IS
    'The session this connection has proven to be (calu.session_id + calu.session_key), or NULL.';

REVOKE ALL ON FUNCTION calu_current_session() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_current_session() TO calu_session, calu_operator;

-- True for the roles that may act on every session: the owner (and a superuser) and members of
-- `calu_operator`. `session_user` is the role that logged in; `current_user` inside a SECURITY
-- DEFINER function is the function's owner, which is the database owner.
CREATE FUNCTION calu_caller_is_operator() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT pg_has_role(session_user, 'calu_operator', 'MEMBER')
        OR pg_has_role(session_user, current_user, 'MEMBER');
$$;

REVOKE ALL ON FUNCTION calu_caller_is_operator() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_caller_is_operator() TO calu_session, calu_operator;

-- The start now records the key's hash together with the row. The form without a key is
-- dropped: a session without a key could not touch its own rows anyway.
DROP FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text);

CREATE FUNCTION calu_start_session(
    project_id bigint,
    host_id bigint,
    os_user text,
    directory text,
    git_commit text,
    pid integer,
    model text,
    key_sha256 text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    new_id uuid;
BEGIN
    IF calu_start_session.key_sha256 IS NULL
       OR calu_start_session.key_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'key_sha256 must be the lowercase hex SHA-256 of the session key';
    END IF;
    INSERT INTO sessions (project_id, host_id, os_user, directory, git_commit, pid, model,
                          started_by)
    VALUES (calu_start_session.project_id, calu_start_session.host_id,
            calu_start_session.os_user, calu_start_session.directory,
            calu_start_session.git_commit, calu_start_session.pid, calu_start_session.model,
            session_user)
    RETURNING id INTO new_id;
    INSERT INTO session_keys (session_id, key_sha256)
    VALUES (new_id, calu_start_session.key_sha256);
    RETURN new_id;
END
$$;

REVOKE ALL ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text, text)
    FROM PUBLIC;
GRANT EXECUTE
    ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text, text)
    TO calu_session, calu_operator;

CREATE OR REPLACE FUNCTION calu_same_conversation(other uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
          FROM sessions mine, sessions theirs
         WHERE mine.id = calu_current_session()
           AND theirs.id = calu_same_conversation.other
           AND mine.claude_session_id IS NOT NULL
           AND mine.claude_session_id = theirs.claude_session_id
    );
$$;

-- The policies of migration 0015, with the proven session in place of the asserted one. The
-- scalar subquery makes PostgreSQL evaluate the function once per statement, not per row.
DROP POLICY session_own ON sessions;
DROP POLICY session_own ON turns;
DROP POLICY session_own ON steps;
DROP POLICY session_own ON escalations;
DROP POLICY session_own ON chat_summaries;
DROP POLICY session_own ON comments;
DROP POLICY session_own ON agents;
DROP POLICY session_own ON client_tokens;

CREATE POLICY session_own ON sessions TO calu_session
    USING (id = (SELECT calu_current_session()) OR calu_same_conversation(id))
    WITH CHECK (id = (SELECT calu_current_session()));
CREATE POLICY session_own ON turns TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON steps TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON escalations TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON chat_summaries TO calu_session
    USING (session_id = (SELECT calu_current_session()) OR calu_same_conversation(session_id))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON comments TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON agents TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));
CREATE POLICY session_own ON client_tokens TO calu_session
    USING (session_id = (SELECT calu_current_session()))
    WITH CHECK (session_id = (SELECT calu_current_session()));

-- The owner-side functions that act on a session: an operator may name any session, a session
-- only itself.

CREATE OR REPLACE FUNCTION calu_cancel_escalation(escalation_id bigint, resolution_note text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE escalations e
       SET state = 'cancelled', resolved_by = session_user, resolved_at = now(),
           note = coalesce(resolution_note, e.note)
     WHERE e.id = escalation_id AND e.state = 'open'
       AND (calu_caller_is_operator() OR e.session_id = calu_current_session());
    RETURN FOUND;
END
$$;

CREATE OR REPLACE FUNCTION calu_client_decision(
    escalation_id bigint,
    for_session uuid,
    token_sha256 text,
    decision text,
    resolution_note text,
    actor text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    row escalations%ROWTYPE;
BEGIN
    IF decision NOT IN ('allowed', 'denied', 'answered') THEN
        RAISE EXCEPTION 'decision must be allowed, denied or answered, not %', decision;
    END IF;
    IF NOT calu_caller_is_operator()
       AND for_session IS DISTINCT FROM calu_current_session() THEN
        RETURN false;  -- a session records client decisions for itself only
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM client_tokens t
         WHERE t.session_id = for_session AND t.token_sha256 = calu_client_decision.token_sha256
           AND NOT t.revoked
    ) THEN
        RETURN false;  -- no client of this session may decide for it
    END IF;
    UPDATE escalations
       SET state = decision, resolved_by = 'client:' || actor, resolved_at = now(),
           note = resolution_note
     WHERE id = escalation_id AND session_id = for_session AND state = 'open'
    RETURNING * INTO row;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_notify('escalation_resolved', json_build_object(
        'id', row.id, 'session_id', row.session_id, 'state', row.state)::text);
    RETURN true;
END
$$;

CREATE OR REPLACE FUNCTION calu_session_setting_change(
    for_session uuid,
    setting text,
    old_value jsonb,
    new_value jsonb,
    client text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT calu_caller_is_operator()
       AND for_session IS DISTINCT FROM calu_current_session() THEN
        RAISE EXCEPTION 'a session records setting changes for itself only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = for_session) THEN
        RAISE EXCEPTION 'no session %', for_session;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (client, for_session, 'session_settings', setting, old_value, new_value);
END
$$;

CREATE OR REPLACE FUNCTION calu_revoke_session_tokens(p_session uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    revoked_rows integer;
BEGIN
    IF NOT public.calu_caller_is_operator()
       AND p_session IS DISTINCT FROM public.calu_current_session() THEN
        RAISE EXCEPTION 'a session revokes its own client tokens only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE client_tokens SET revoked = true
     WHERE session_id = p_session AND NOT revoked;
    GET DIAGNOSTICS revoked_rows = ROW_COUNT;
    RETURN revoked_rows;
END
$$;

CREATE OR REPLACE FUNCTION calu_client_token_ok(p_session uuid, p_hash text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_id bigint;
BEGIN
    IF NOT calu_caller_is_operator()
       AND p_session IS DISTINCT FROM calu_current_session() THEN
        RETURN false;  -- a session confirms tokens of itself only
    END IF;
    SELECT id INTO v_id
      FROM client_tokens
     WHERE session_id = p_session AND token_sha256 = p_hash AND NOT revoked;
    IF v_id IS NULL THEN
        RETURN false;
    END IF;
    UPDATE client_tokens SET last_used_at = now() WHERE id = v_id;
    RETURN true;
END;
$$;

-- 2. Fork points for the session role.
--
-- `calu run --resume <claude id> --fork-before-turn N` looks the fork point up before its own
-- session row exists, on a `calu_session` connection, where the policies hide every row. The
-- function returns the two transcript uuids of a conversation whose Claude session id the
-- caller already knows, and nothing else of it: the same exposure as
-- `calu_conversation_sessions`. A conversation has one `sessions` row per `calu run`, and its
-- turns are spread over them, so the lookup spans every row with that Claude session id.
CREATE FUNCTION calu_fork_point(p_claude_session_id text, p_turn integer)
RETURNS TABLE (last_entry_uuid text, prompt_uuid text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    WITH conversation AS (
        SELECT s.id FROM sessions s WHERE s.claude_session_id = p_claude_session_id
    )
    SELECT previous.last_entry_uuid, dropped.prompt_uuid
      FROM turns dropped JOIN turns previous ON previous.number = dropped.number - 1
     WHERE dropped.session_id IN (SELECT id FROM conversation)
       AND previous.session_id IN (SELECT id FROM conversation)
       AND dropped.number = p_turn
       AND previous.last_entry_uuid IS NOT NULL AND dropped.prompt_uuid IS NOT NULL
     ORDER BY dropped.started_at DESC
     LIMIT 1;
$$;

REVOKE ALL ON FUNCTION calu_fork_point(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_fork_point(text, integer) TO calu_session, calu_operator;

-- 3. The directory a project is registered for.
--
-- `calu project add DIR` records the directory and the host, so that `calu run` in that
-- directory (or below it) finds its project without `--project`. Both are NULL for a project
-- registered without a directory or before this migration; such a project is used with
-- `--project` only.
ALTER TABLE projects ADD COLUMN directory text;
ALTER TABLE projects ADD COLUMN hostname  text;

COMMENT ON COLUMN projects.directory IS
    'Absolute path the project was registered for on `hostname`; NULL if none was recorded.';
