-- calu schema, baseline (agent/records/PLAN.md D266).
--
-- One file instead of the 44 migrations of 2026-11-09 to 2026-11-24: it creates the schema
-- those migrations had built, generated from a database they had migrated (pg_dump
-- --schema-only, then cleaned up). Why a table, function or policy is as it is: the
-- decisions of PLAN.md, and the history of the replaced files in git (before D266). Where
-- calu's code or comments name "migration 0025" and the like, they mean those replaced files.
--
-- A database that was created from the replaced migrations is not migrated from here: `calu
-- db migrate` and `calu setup` refuse it (the names in `schema_migrations` differ), and
-- `calu db reset` recreates it empty.
--
-- Roles (server-wide; D265):
--
--   the owner      runs this file: owns the schema, runs migrations   (the `db-admin` entry)
--   calu_admin     the user login: every calu process of the person    (the `user` entry)
--   calu_operator  no login; a group: `calu_caller_is_operator()` asks for its membership
--   calu_session   no login; its grants and policies are kept, unused

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_session') THEN
        CREATE ROLE calu_session NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_operator') THEN
        CREATE ROLE calu_operator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_admin') THEN
        CREATE ROLE calu_admin NOLOGIN;
    END IF;
END
$$;

ALTER ROLE calu_session NOLOGIN PASSWORD NULL;
ALTER ROLE calu_operator NOLOGIN PASSWORD NULL;
GRANT calu_operator TO calu_admin;

-- For this transaction only: names below are schema-qualified, and function bodies may name
-- objects that are created further down.
SELECT pg_catalog.set_config('search_path', '', true);
SET LOCAL check_function_bodies = false;

CREATE FUNCTION public.calu_audit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO audit_log (table_name, action, old_row, new_row)
    VALUES (TG_TABLE_NAME, TG_OP,
            CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
            CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END);
    PERFORM pg_notify('config_changed', json_build_object('table', TG_TABLE_NAME)::text);
    RETURN NULL;
END
$$;

CREATE FUNCTION public.calu_caller_is_operator() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT pg_has_role(session_user, 'calu_operator', 'MEMBER')
        OR pg_has_role(session_user, current_user, 'MEMBER');
$$;

CREATE FUNCTION public.calu_cancel_escalation(escalation_id bigint, resolution_note text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    UPDATE escalations e
       SET state = 'cancelled', resolved_by = session_user, resolved_at = now(),
           note = coalesce(resolution_note, e.note)
     WHERE e.id = escalation_id AND e.state = 'open'
       AND (calu_caller_is_operator() OR e.session_id = calu_current_session());
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_commits;
    after      trusted_commits;
BEGIN
    IF of_session IS NULL THEN
        IF NOT calu_caller_is_operator() THEN
            RAISE EXCEPTION 'only a session that has proven its identity certifies a commit'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF calu_trust_rank(p_method) IS NULL THEN
        RAISE EXCEPTION 'the method is one of user, review, check'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO before FROM trusted_commits WHERE commit_id = p_commit FOR UPDATE;
    INSERT INTO trusted_commits AS held
           (commit_id, tree_id, certified_by, method, note, session_id)
    VALUES (p_commit, p_tree, p_by, p_method, nullif(p_note, ''), of_session)
    ON CONFLICT (commit_id) DO UPDATE
       SET tree_id = EXCLUDED.tree_id, certified_at = now(),
           certified_by = EXCLUDED.certified_by, method = EXCLUDED.method,
           note = EXCLUDED.note, session_id = EXCLUDED.session_id,
           recorded_as = session_user
     WHERE calu_trust_rank(EXCLUDED.method) > calu_trust_rank(held.method)
    RETURNING * INTO after;
    IF after.commit_id IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (p_by, of_session, 'trusted_commits',
            CASE WHEN before.commit_id IS NULL THEN 'INSERT' ELSE 'UPDATE' END,
            CASE WHEN before.commit_id IS NULL THEN NULL ELSE to_jsonb(before) END,
            to_jsonb(after));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) IS 'Records a commit as trusted, or replaces a weaker certification of it; the only way a session adds to trusted_commits (0029).';

CREATE FUNCTION public.calu_checker_version() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF (NEW.instructions, NEW.model, NEW.request_limit, NEW.output_tokens_limit, NEW.max_tokens,
        NEW.route, NEW.enabled)
       IS DISTINCT FROM
       (OLD.instructions, OLD.model, OLD.request_limit, OLD.output_tokens_limit, OLD.max_tokens,
        OLD.route, OLD.enabled) THEN
        NEW.version := OLD.version + 1;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END
$$;

CREATE TABLE public.session_requests (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    requested_by text DEFAULT SESSION_USER NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    hostname text NOT NULL,
    os_user text NOT NULL,
    directory text NOT NULL,
    kind text NOT NULL,
    claude_session_id text,
    fork_before_turn integer,
    prompt text,
    model text,
    effort text,
    project text,
    state text DEFAULT 'pending'::text NOT NULL,
    claimed_at timestamp with time zone,
    claimed_pid integer,
    finished_at timestamp with time zone,
    session_id uuid,
    message text DEFAULT ''::text NOT NULL,
    server_id uuid,
    CONSTRAINT session_requests_check CHECK (((kind = 'new'::text) = (claude_session_id IS NULL))),
    CONSTRAINT session_requests_check1 CHECK (((fork_before_turn IS NULL) OR (kind = 'resume'::text))),
    CONSTRAINT session_requests_fork_before_turn_check CHECK ((fork_before_turn >= 2)),
    CONSTRAINT session_requests_kind_check CHECK ((kind = ANY (ARRAY['new'::text, 'resume'::text, 'fork'::text]))),
    CONSTRAINT session_requests_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'claimed'::text, 'started'::text, 'failed'::text, 'withdrawn'::text])))
);

COMMENT ON TABLE public.session_requests IS 'Requests for a session that a calu serve process of that host and OS user starts (D208)';

COMMENT ON COLUMN public.session_requests.kind IS 'new: a new conversation; resume: continue claude_session_id (with fork_before_turn: branch off before that turn); fork: continue a copy of claude_session_id';

COMMENT ON COLUMN public.session_requests.session_id IS 'the sessions row of the started session; NULL until started';

COMMENT ON COLUMN public.session_requests.message IS 'why the request failed, in words for the requester';

COMMENT ON COLUMN public.session_requests.server_id IS 'The server whose relay filed the request: only that one claims it; NULL for an operator';

CREATE FUNCTION public.calu_claim_served_request(p_server uuid, p_pid integer, max_age_s double precision DEFAULT 60) RETURNS SETOF public.session_requests
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v servers%ROWTYPE;
BEGIN
    UPDATE servers s SET last_seen = now()
     WHERE s.id = p_server AND s.ended_at IS NULL
    RETURNING s.* INTO v;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE session_requests r
       SET state = 'claimed', claimed_at = now(), claimed_pid = p_pid
     WHERE r.id = (
            SELECT p.id FROM session_requests p
             WHERE p.state = 'pending'
               AND p.hostname = v.hostname
               AND p.os_user = v.os_user
               AND p.created_at > now() - make_interval(secs => max_age_s)
               AND (p.server_id = v.id
                    OR (p.server_id IS NULL
                        AND (calu_path_within(p.directory, v.projects_directory)
                             OR NOT EXISTS (
                                SELECT 1 FROM servers o
                                 WHERE calu_server_live(o) AND o.id <> v.id
                                   AND o.hostname = v.hostname AND o.os_user = v.os_user
                                   AND calu_path_within(p.directory, o.projects_directory)))))
             ORDER BY p.id
             LIMIT 1
               FOR UPDATE SKIP LOCKED)
    RETURNING r.*;
END
$$;

CREATE FUNCTION public.calu_claim_session_request(hostname text, os_user text, pid integer, max_age_s double precision DEFAULT 60) RETURNS SETOF public.session_requests
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    RETURN QUERY
    UPDATE session_requests r
       SET state = 'claimed', claimed_at = now(), claimed_pid = calu_claim_session_request.pid
     WHERE r.id = (
            SELECT p.id FROM session_requests p
             WHERE p.state = 'pending'
               AND p.hostname = calu_claim_session_request.hostname
               AND p.os_user = calu_claim_session_request.os_user
               AND p.created_at > now() - make_interval(secs => calu_claim_session_request.max_age_s)
             ORDER BY p.id
             LIMIT 1
               FOR UPDATE SKIP LOCKED)
    RETURNING r.*;
END
$$;

COMMENT ON FUNCTION public.calu_claim_session_request(hostname text, os_user text, pid integer, max_age_s double precision) IS 'Claims the oldest pending request for a host and OS user, as the caller names them. Any holder of the session login may claim the requests of every host and user (intended)';

CREATE FUNCTION public.calu_client_decision(escalation_id bigint, for_session uuid, token_sha256 text, decision text, resolution_note text, actor text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.calu_client_token_ok(p_session uuid, p_hash text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

COMMENT ON FUNCTION public.calu_client_token_ok(p_session uuid, p_hash text) IS 'True if the hash belongs to a non-revoked client token of that session; stamps last_used_at.';

CREATE FUNCTION public.calu_close_gone_escalations(silent_s double precision) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

COMMENT ON FUNCTION public.calu_close_gone_escalations(silent_s double precision) IS 'Cancels the open escalations of sessions that ended, or were silent that long while the server was up (0026).';

CREATE FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

COMMENT ON FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) IS 'Cancels every open escalation of one session; a session may name only itself (0026).';

CREATE FUNCTION public.calu_conversation_sessions(claude_session_id text) RETURNS TABLE(id uuid, started_at timestamp with time zone, turn_count bigint)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT s.id, s.started_at, (SELECT count(*) FROM turns t WHERE t.session_id = s.id)
      FROM sessions s
     WHERE s.claude_session_id = calu_conversation_sessions.claude_session_id
     ORDER BY (SELECT count(*) FROM turns t WHERE t.session_id = s.id) > 0 DESC,
              s.started_at DESC;
$$;

CREATE FUNCTION public.calu_current_session() RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

COMMENT ON FUNCTION public.calu_current_session() IS 'The session this connection has proven to be (calu.session_id + calu.session_key), or NULL.';

CREATE FUNCTION public.calu_distrust_file(p_sha256 text, p_by text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    of_session uuid := calu_current_session();
    removed    trusted_files;
BEGIN
    IF of_session IS NULL THEN
        RAISE EXCEPTION 'only a session that has proven its identity withdraws a trusted file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_by IS NULL OR p_by = '' THEN
        RAISE EXCEPTION 'a withdrawal names who withdrew'
            USING ERRCODE = 'check_violation';
    END IF;
    DELETE FROM trusted_files
     WHERE content_sha256 = p_sha256 AND method IN ('check', 'review')
    RETURNING * INTO removed;
    IF removed.content_sha256 IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row)
    VALUES (p_by, of_session, 'trusted_files', 'DELETE', to_jsonb(removed));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION public.calu_distrust_file(p_sha256 text, p_by text) IS 'Withdraws a check or review row of trusted_files for a session whose checker or review found that content dishonest; never a user row (0030).';

CREATE FUNCTION public.calu_expire_session_request(request_id bigint) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    UPDATE session_requests r
       SET state = 'failed',
           finished_at = now(),
           message = 'the server that claimed it did not answer'
     WHERE r.id = calu_expire_session_request.request_id
       AND r.state = 'claimed'
       AND r.claimed_at < now() - interval '180 seconds';
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.calu_finish_session_request(request_id bigint, outcome text, session_id uuid, message text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    IF calu_finish_session_request.outcome NOT IN ('started', 'failed') THEN
        RAISE EXCEPTION 'outcome must be started or failed';
    END IF;
    UPDATE session_requests r
       SET state = calu_finish_session_request.outcome,
           finished_at = now(),
           session_id = calu_finish_session_request.session_id,
           message = coalesce(calu_finish_session_request.message, '')
     WHERE r.id = calu_finish_session_request.request_id AND r.state = 'claimed';
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.calu_fork_point(p_claude_session_id text, p_turn integer) RETURNS TABLE(last_entry_uuid text, prompt_uuid text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.calu_own_step(step bigint) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT EXISTS (
        SELECT 1 FROM steps s
         WHERE s.id = calu_own_step.step AND s.session_id = calu_current_session()
    );
$$;

COMMENT ON FUNCTION public.calu_own_step(step bigint) IS 'Whether the step belongs to the session this connection has proven to be (migration 0026).';

CREATE FUNCTION public.calu_path_within(path text, base text) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT base <> '' AND (path = base OR starts_with(path, rtrim(base, '/') || '/'));
$$;

CREATE FUNCTION public.calu_prune_trust(p_days integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    before_time timestamptz;
    commits     integer;
    files       integer;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator prunes stored trust'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_days IS NULL OR p_days < 0 THEN
        RAISE EXCEPTION 'the age in days must be 0 or more'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    before_time := now() - make_interval(days => p_days);
    WITH gone AS (
        DELETE FROM trusted_commits
         WHERE method IN ('check', 'review') AND certified_at < before_time
        RETURNING 1
    )
    SELECT count(*) INTO commits FROM gone;
    WITH gone AS (
        DELETE FROM trusted_files
         WHERE method IN ('check', 'review') AND certified_at < before_time
        RETURNING 1
    )
    SELECT count(*) INTO files FROM gone;
    IF commits > 0 THEN
        INSERT INTO audit_log (actor, table_name, action, old_row)
        VALUES (session_user, 'trusted_commits', 'DELETE',
                jsonb_build_object('pruned', commits, 'older_than_days', p_days));
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    END IF;
    IF files > 0 THEN
        INSERT INTO audit_log (actor, table_name, action, old_row)
        VALUES (session_user, 'trusted_files', 'DELETE',
                jsonb_build_object('pruned', files, 'older_than_days', p_days));
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN commits + files;
END
$$;

COMMENT ON FUNCTION public.calu_prune_trust(p_days integer) IS 'Removes check and review rows older than a number of days from trusted_commits and trusted_files, never user rows; operators and the owner only (0029).';

CREATE FUNCTION public.calu_recent_sessions(for_directory text, max_rows integer) RETURNS TABLE(id text, claude_session_id text, state text, directory text, started_at timestamp with time zone, ended_at timestamp with time zone, first_input text)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT s.id::text, s.claude_session_id, s.state, s.directory, s.started_at, s.ended_at,
           coalesce((SELECT t.user_inputs->>0 FROM turns t
                      WHERE t.session_id = s.id ORDER BY t.number LIMIT 1), '')
      FROM sessions s
     WHERE s.directory = for_directory
     ORDER BY s.started_at DESC
     LIMIT max_rows;
$$;

CREATE FUNCTION public.calu_relay_session_request(p_server uuid, p_hash text, directory text, kind text, claude_session_id text, fork_before_turn integer, prompt text, model text, effort text, project text, label text) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_host text;
    v_user text;
    v_id bigint;
BEGIN
    SELECT s.hostname, s.os_user INTO v_host, v_user
      FROM servers s JOIN server_tokens t ON t.server_id = s.id
     WHERE s.id = p_server AND calu_server_live(s)
       AND t.token_sha256 = p_hash AND NOT t.revoked;
    IF v_host IS NULL THEN
        RAISE EXCEPTION 'not a valid token of this server' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO session_requests (requested_by, label, hostname, os_user, directory, kind,
                                  claude_session_id, fork_before_turn, prompt, model, effort,
                                  project, server_id)
    VALUES ('relay', coalesce(calu_relay_session_request.label, ''), v_host, v_user,
            calu_relay_session_request.directory, calu_relay_session_request.kind,
            calu_relay_session_request.claude_session_id,
            calu_relay_session_request.fork_before_turn, calu_relay_session_request.prompt,
            calu_relay_session_request.model, calu_relay_session_request.effort,
            calu_relay_session_request.project, p_server)
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;

CREATE FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    row escalations%ROWTYPE;
BEGIN
    IF decision NOT IN ('allowed', 'denied', 'answered') THEN
        RAISE EXCEPTION 'decision must be allowed, denied or answered, not %', decision;
    END IF;
    UPDATE escalations
       SET state = decision, resolved_by = session_user, resolved_at = now(),
           note = resolution_note
     WHERE id = escalation_id AND state = 'open'
    RETURNING * INTO row;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_notify('escalation_resolved', json_build_object(
        'id', row.id, 'session_id', row.session_id, 'state', row.state)::text);
    RETURN true;
END
$$;

CREATE FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text, actor text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    row escalations%ROWTYPE;
    who text := left(btrim(regexp_replace(coalesce(actor, ''), '[[:cntrl:]]', '', 'g')), 200);
    role text := regexp_replace(session_user::text, '^calu_', '');
BEGIN
    IF decision NOT IN ('allowed', 'denied', 'answered') THEN
        RAISE EXCEPTION 'decision must be allowed, denied or answered, not %', decision;
    END IF;
    UPDATE escalations
       SET state = decision,
           resolved_by = CASE WHEN who = '' THEN session_user::text ELSE role || ':' || who END,
           resolved_at = now(),
           note = resolution_note
     WHERE id = escalation_id AND state = 'open'
    RETURNING * INTO row;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_notify('escalation_resolved', json_build_object(
        'id', row.id, 'session_id', row.session_id, 'state', row.state)::text);
    RETURN true;
END
$$;

CREATE FUNCTION public.calu_revoke_session_tokens(p_session uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.calu_same_conversation(other uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT EXISTS (
        SELECT 1
          FROM sessions mine, sessions theirs
         WHERE mine.id = calu_current_session()
           AND theirs.id = calu_same_conversation.other
           AND mine.claude_session_id IS NOT NULL
           AND mine.claude_session_id = theirs.claude_session_id
    );
$$;

CREATE FUNCTION public.calu_server_beat(p_server uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    UPDATE servers SET last_seen = now() WHERE id = p_server AND ended_at IS NULL;
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.calu_server_ended(p_server uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    UPDATE server_tokens SET revoked = true WHERE server_id = p_server AND NOT revoked;
    UPDATE servers SET ended_at = now() WHERE id = p_server AND ended_at IS NULL;
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.calu_server_gone(p_server uuid, p_other uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    PERFORM 1
       FROM servers me JOIN servers o
         ON o.hostname = me.hostname AND o.os_user = me.os_user AND o.machine = me.machine
      WHERE me.id = p_server AND me.ended_at IS NULL AND coalesce(me.machine, '') <> ''
        AND o.id = p_other AND o.id <> me.id AND o.ended_at IS NULL;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE server_tokens SET revoked = true WHERE server_id = p_other AND NOT revoked;
    UPDATE servers SET ended_at = now() WHERE id = p_other AND ended_at IS NULL;
    RETURN FOUND;
END
$$;

CREATE TABLE public.servers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    hostname text NOT NULL,
    os_user text NOT NULL,
    pid integer NOT NULL,
    machine text,
    relay_address text DEFAULT ''::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    projects_directory text DEFAULT ''::text NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

COMMENT ON TABLE public.servers IS 'One row per calu serve process (D242)';

COMMENT ON COLUMN public.servers.machine IS 'machine id|boot id|pid namespace of the process (calu.machine), as sessions.machine';

COMMENT ON COLUMN public.servers.relay_address IS 'ws://127.0.0.1:<port> of the relay on the server''s machine; empty without a relay';

COMMENT ON COLUMN public.servers.projects_directory IS 'The resolved directory the server starts sessions in (or below); empty if not recorded';

COMMENT ON COLUMN public.servers.last_seen IS 'Last heartbeat of the server process (every 10 s); silent for 60 s means not running';

CREATE FUNCTION public.calu_server_live(s public.servers) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT s.ended_at IS NULL AND s.last_seen > now() - interval '60 seconds';
$$;

CREATE FUNCTION public.calu_server_peers(p_server uuid) RETURNS TABLE(id uuid, pid integer, machine text)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT o.id, o.pid, o.machine
      FROM servers me JOIN servers o
        ON o.hostname = me.hostname AND o.os_user = me.os_user AND o.machine = me.machine
     WHERE me.id = p_server AND me.ended_at IS NULL AND coalesce(me.machine, '') <> ''
       AND o.id <> me.id AND o.ended_at IS NULL;
$$;

CREATE FUNCTION public.calu_server_sessions(p_server uuid, p_hash text, p_ids uuid[]) RETURNS TABLE(id uuid, name text, state_since timestamp with time zone)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    SELECT x.id, x.name, x.state_since
      FROM servers s
      JOIN server_tokens t ON t.server_id = s.id
      JOIN sessions x ON x.pid = s.pid AND x.machine IS NOT DISTINCT FROM s.machine
                     AND x.id = ANY (p_ids)
     WHERE s.id = p_server AND calu_server_live(s)
       AND t.token_sha256 = p_hash AND NOT t.revoked
       AND x.ended_at IS NULL;
$$;

CREATE FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    INSERT INTO servers (hostname, os_user, pid, machine, relay_address)
    VALUES (hostname, os_user, pid, machine, coalesce(relay_address, ''))
    RETURNING id;
$$;

CREATE FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text, projects_directory text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    INSERT INTO servers (hostname, os_user, pid, machine, relay_address, projects_directory)
    VALUES (hostname, os_user, pid, machine, coalesce(relay_address, ''),
            coalesce(projects_directory, ''))
    RETURNING id;
$$;

CREATE FUNCTION public.calu_server_token_ok(p_server uuid, p_hash text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_id bigint;
    v_label text;
BEGIN
    SELECT t.id, t.label INTO v_id, v_label
      FROM server_tokens t JOIN servers s ON s.id = t.server_id
     WHERE t.server_id = p_server AND t.token_sha256 = p_hash
       AND NOT t.revoked AND calu_server_live(s);
    IF v_id IS NULL THEN
        RETURN NULL;
    END IF;
    UPDATE server_tokens SET last_used_at = now() WHERE id = v_id;
    RETURN v_label;
END
$$;

CREATE FUNCTION public.calu_session_requested() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('session_requested', json_build_object(
        'id', NEW.id, 'hostname', NEW.hostname, 'os_user', NEW.os_user)::text);
    RETURN NULL;
END
$$;

CREATE FUNCTION public.calu_session_setting_change(for_session uuid, setting text, old_value jsonb, new_value jsonb, client text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.calu_start_session(project_id bigint, host_id bigint, os_user text, directory text, git_commit text, pid integer, model text, key_sha256 text, machine text DEFAULT NULL::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $_$
DECLARE
    new_id uuid;
BEGIN
    IF calu_start_session.key_sha256 IS NULL
       OR calu_start_session.key_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'key_sha256 must be the lowercase hex SHA-256 of the session key';
    END IF;
    INSERT INTO sessions (project_id, host_id, os_user, directory, git_commit, pid, model,
                          started_by, machine)
    VALUES (calu_start_session.project_id, calu_start_session.host_id,
            calu_start_session.os_user, calu_start_session.directory,
            calu_start_session.git_commit, calu_start_session.pid, calu_start_session.model,
            session_user, NULLIF(calu_start_session.machine, ''))
    RETURNING id INTO new_id;
    INSERT INTO session_keys (session_id, key_sha256)
    VALUES (new_id, calu_start_session.key_sha256);
    RETURN new_id;
END
$_$;

CREATE FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_files;
    after      trusted_files;
BEGIN
    IF of_session IS NULL THEN
        IF NOT calu_caller_is_operator() THEN
            RAISE EXCEPTION 'only a session that has proven its identity records a trusted file'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF calu_trust_rank(p_method) IS NULL THEN
        RAISE EXCEPTION 'the method is one of user, review, check'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO before FROM trusted_files WHERE content_sha256 = p_sha256 FOR UPDATE;
    INSERT INTO trusted_files AS held
           (content_sha256, path, certified_by, method, note, session_id)
    VALUES (p_sha256, p_path, p_by, p_method, nullif(p_note, ''), of_session)
    ON CONFLICT (content_sha256) DO UPDATE
       SET path = EXCLUDED.path, certified_at = now(),
           certified_by = EXCLUDED.certified_by, method = EXCLUDED.method,
           note = EXCLUDED.note, session_id = EXCLUDED.session_id,
           recorded_as = session_user
     WHERE calu_trust_rank(EXCLUDED.method) > calu_trust_rank(held.method)
    RETURNING * INTO after;
    IF after.content_sha256 IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (p_by, of_session, 'trusted_files',
            CASE WHEN before.content_sha256 IS NULL THEN 'INSERT' ELSE 'UPDATE' END,
            CASE WHEN before.content_sha256 IS NULL THEN NULL ELSE to_jsonb(before) END,
            to_jsonb(after));
    IF p_method = 'user' THEN
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN true;
END
$$;

COMMENT ON FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) IS 'Records a file content as trusted, or replaces a weaker verification of it; the only way a session adds to trusted_files (0029).';

CREATE FUNCTION public.calu_trust_rank(p_method text) RETURNS integer
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT CASE p_method WHEN 'user' THEN 3 WHEN 'review' THEN 2 WHEN 'check' THEN 1 END
$$;

COMMENT ON FUNCTION public.calu_trust_rank(p_method text) IS 'Strength of a trust method: user 3, review 2, check 1, anything else NULL (0029).';

CREATE FUNCTION public.calu_untrust_commit(p_commit text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    removed trusted_commits;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator removes a trusted commit'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    DELETE FROM trusted_commits WHERE commit_id = p_commit RETURNING * INTO removed;
    IF removed.commit_id IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, table_name, action, old_row)
    VALUES (session_user, 'trusted_commits', 'DELETE', to_jsonb(removed));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION public.calu_untrust_commit(p_commit text) IS 'Removes a trusted commit; operators and the owner only (0029).';

CREATE FUNCTION public.calu_untrust_file(p_hash_or_path text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    removed integer;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator removes a trusted file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    WITH gone AS (
        DELETE FROM trusted_files
         WHERE content_sha256 = p_hash_or_path OR path = p_hash_or_path
        RETURNING *
    ), logged AS (
        INSERT INTO audit_log (actor, table_name, action, old_row)
        SELECT session_user, 'trusted_files', 'DELETE', to_jsonb(gone) FROM gone
        RETURNING 1
    )
    SELECT count(*) INTO removed FROM logged;
    IF removed > 0 THEN
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN removed;
END
$$;

COMMENT ON FUNCTION public.calu_untrust_file(p_hash_or_path text) IS 'Removes trusted file contents by hash or path; operators and the owner only (0029).';

CREATE FUNCTION public.calu_withdraw_session_request(request_id bigint) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
    UPDATE session_requests r
       SET state = 'withdrawn', finished_at = now()
     WHERE r.id = calu_withdraw_session_request.request_id AND r.state = 'pending';
    RETURN FOUND;
END
$$;

CREATE TABLE public.agents (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    agent_id text NOT NULL,
    parent_agent_id text,
    sdk_agent_id text,
    description text DEFAULT ''::text NOT NULL,
    subagent_type text,
    background boolean DEFAULT false NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    CONSTRAINT agents_status_check CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'failed'::text, 'stopped'::text])))
);

COMMENT ON COLUMN public.agents.agent_id IS 'Id of the Agent tool call that started this subagent; `steps.agent_id` refers to it.';

COMMENT ON COLUMN public.agents.parent_agent_id IS 'The agent that started this one; NULL means the session''s main agent.';

ALTER TABLE public.agents ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.agents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor text DEFAULT CURRENT_USER NOT NULL,
    table_name text NOT NULL,
    action text NOT NULL,
    old_row jsonb,
    new_row jsonb,
    session_id uuid
);

ALTER TABLE public.audit_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.chat_summaries (
    session_id uuid NOT NULL,
    summary text NOT NULL,
    covered_turns integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.checker_runs (
    id bigint NOT NULL,
    step_id bigint NOT NULL,
    checker_name text NOT NULL,
    checker_version integer NOT NULL,
    model text NOT NULL,
    decision text NOT NULL,
    reason text,
    input_tokens integer,
    output_tokens integer,
    cache_read_tokens integer,
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT checker_runs_decision_check CHECK ((decision = ANY (ARRAY['pass'::text, 'allow'::text, 'deny'::text, 'escalate'::text])))
);

ALTER TABLE public.checker_runs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.checker_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.checker_types (
    name text NOT NULL,
    route text NOT NULL,
    instructions text NOT NULL,
    model text NOT NULL,
    request_limit integer DEFAULT 5 NOT NULL,
    output_tokens_limit integer DEFAULT 24000 NOT NULL,
    max_tokens integer DEFAULT 8000 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT checker_types_route_check CHECK ((route = ANY (ARRAY['command'::text, 'edit'::text, 'subagent'::text, 'narrator'::text, 'changeset'::text])))
);

CREATE TABLE public.client_tokens (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    token_sha256 text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    address text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL
);

ALTER TABLE public.client_tokens ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.client_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.comments (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    turn integer,
    kind text NOT NULL,
    path text NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    ref text,
    comment text NOT NULL,
    content jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT comments_kind_check CHECK ((kind = ANY (ARRAY['diff'::text, 'file'::text])))
);

ALTER TABLE public.comments ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.comments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.config (
    scope text NOT NULL,
    scope_id text DEFAULT ''::text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    CONSTRAINT config_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'project'::text, 'session'::text])))
);

CREATE TABLE public.escalations (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    step_id bigint,
    reason text NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    state text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_by text,
    resolved_at timestamp with time zone,
    note text,
    CONSTRAINT escalations_state_check CHECK ((state = ANY (ARRAY['open'::text, 'allowed'::text, 'denied'::text, 'answered'::text, 'cancelled'::text])))
);

COMMENT ON COLUMN public.escalations.state IS 'open, allowed, denied, answered (a question of the agent, note holds the answer) or cancelled.';

ALTER TABLE public.escalations ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.escalations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.hosts (
    id bigint NOT NULL,
    hostname text NOT NULL,
    os text DEFAULT ''::text NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.hosts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.hosts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.projects (
    id bigint NOT NULL,
    name text NOT NULL,
    git_remote text,
    network_policy text DEFAULT 'proxy'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    directory text,
    hostname text,
    CONSTRAINT projects_network_policy_check CHECK ((network_policy = ANY (ARRAY['none'::text, 'proxy'::text, 'full'::text])))
);

COMMENT ON COLUMN public.projects.directory IS 'Absolute path the project was registered for on `hostname`; NULL if none was recorded.';

ALTER TABLE public.projects ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.projects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.rules (
    id bigint NOT NULL,
    project_id bigint,
    list text NOT NULL,
    pattern text NOT NULL,
    comment text DEFAULT ''::text NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    CONSTRAINT rules_list_check CHECK ((list = ANY (ARRAY['command-allow'::text, 'command-deny'::text, 'read-allow'::text, 'read-deny'::text, 'write-allow'::text, 'write-deny'::text])))
);

ALTER TABLE public.rules ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.server_tokens (
    id bigint NOT NULL,
    server_id uuid NOT NULL,
    token_sha256 text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL
);

COMMENT ON TABLE public.server_tokens IS 'Tokens for the relay of one calu serve: attach to its sessions and ask it for new ones';

CREATE SEQUENCE public.server_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.server_tokens_id_seq OWNED BY public.server_tokens.id;

CREATE TABLE public.session_keys (
    session_id uuid NOT NULL,
    key_sha256 text NOT NULL,
    CONSTRAINT session_keys_key_sha256_check CHECK ((key_sha256 ~ '^[0-9a-f]{64}$'::text))
);

COMMENT ON TABLE public.session_keys IS 'SHA-256 of the key each session process holds; only the owner reads it (migration 0025).';

CREATE SEQUENCE public.session_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.session_requests_id_seq OWNED BY public.session_requests.id;

CREATE TABLE public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id bigint,
    host_id bigint NOT NULL,
    os_user text NOT NULL,
    directory text NOT NULL,
    git_commit text,
    pid integer NOT NULL,
    claude_session_id text,
    model text,
    state text DEFAULT 'starting'::text NOT NULL,
    state_since timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    started_by text DEFAULT CURRENT_USER NOT NULL,
    address text,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    machine text,
    forked_from text,
    forked_before_turn integer,
    name text DEFAULT ''::text NOT NULL,
    CONSTRAINT sessions_forked_before_turn_check CHECK ((forked_before_turn >= 2)),
    CONSTRAINT sessions_name_length CHECK ((char_length(name) <= 80)),
    CONSTRAINT sessions_state_check CHECK ((state = ANY (ARRAY['starting'::text, 'idle'::text, 'running'::text, 'checking'::text, 'tool_call'::text, 'escalated'::text, 'stopped'::text, 'failed'::text])))
);

COMMENT ON COLUMN public.sessions.address IS 'Where the session process listens for clients: "ws://127.0.0.1:<port>" or a Unix socket path. NULL while the session serves no clients.';

COMMENT ON COLUMN public.sessions.last_seen IS 'When the session process last said it was running (agent/records/PLAN.md D56).';

COMMENT ON COLUMN public.sessions.machine IS 'machine id|boot id|pid namespace of the calu process (calu.machine); NULL if not recorded';

COMMENT ON COLUMN public.sessions.forked_from IS 'Claude session id of the conversation this session is a fork of; NULL if it is none';

COMMENT ON COLUMN public.sessions.forked_before_turn IS 'the first turn of that conversation the fork dropped; NULL if it kept all of it';

COMMENT ON COLUMN public.sessions.name IS 'A name a person gave the session, at most 80 characters; '''' for none';

CREATE TABLE public.steps (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    turn integer,
    seq integer NOT NULL,
    agent_id text,
    tool_use_id text,
    tool_name text NOT NULL,
    tool_input jsonb NOT NULL,
    git_commit text,
    rules_outcome text,
    decision text,
    reason text,
    summary text,
    narration text,
    result_summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    finished_at timestamp with time zone,
    snapshot_before text,
    snapshot_after text,
    wrote jsonb,
    CONSTRAINT steps_decision_check CHECK ((decision = ANY (ARRAY['pass'::text, 'allow'::text, 'deny'::text, 'escalate'::text])))
);

COMMENT ON COLUMN public.steps.snapshot_before IS 'Git tree of the project directory before the step (calu''s own snapshot store, D31).';

COMMENT ON COLUMN public.steps.snapshot_after IS 'Git tree of the project directory after the step.';

COMMENT ON COLUMN public.steps.wrote IS 'What the step wrote: {"paths": [...], "undeclared": [...], "overlapping": bool}.';

ALTER TABLE public.steps ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.trusted_commits (
    commit_id text NOT NULL,
    tree_id text NOT NULL,
    certified_at timestamp with time zone DEFAULT now() NOT NULL,
    certified_by text NOT NULL,
    method text NOT NULL,
    note text,
    session_id uuid,
    recorded_as text DEFAULT SESSION_USER NOT NULL,
    CONSTRAINT trusted_commits_certified_by_check CHECK ((certified_by <> ''::text)),
    CONSTRAINT trusted_commits_commit_id_check CHECK ((commit_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'::text)),
    CONSTRAINT trusted_commits_method_check CHECK ((method = ANY (ARRAY['user'::text, 'review'::text, 'check'::text]))),
    CONSTRAINT trusted_commits_tree_id_check CHECK ((tree_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'::text))
);

COMMENT ON TABLE public.trusted_commits IS 'Commits certified as honest. method: user (a person, certified_by names the client or OS user), review or check (a session certified a commit made in a clean state; the weakest source among the verifications of its changes). session_id: the session that recorded it, if one did. recorded_as: the database login (0029).';

CREATE TABLE public.trusted_files (
    content_sha256 text NOT NULL,
    path text NOT NULL,
    certified_at timestamp with time zone DEFAULT now() NOT NULL,
    certified_by text NOT NULL,
    method text NOT NULL,
    note text,
    session_id uuid,
    recorded_as text DEFAULT SESSION_USER NOT NULL,
    CONSTRAINT trusted_files_certified_by_check CHECK ((certified_by <> ''::text)),
    CONSTRAINT trusted_files_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT trusted_files_method_check CHECK ((method = ANY (ARRAY['user'::text, 'review'::text, 'check'::text]))),
    CONSTRAINT trusted_files_path_check CHECK ((path <> ''::text))
);

COMMENT ON TABLE public.trusted_files IS 'File contents verified as honest, by SHA-256, one row per content. method: user (a person), review (an honesty review), check (an edit the step checkers judged). path is a hint for people (0029).';

CREATE TABLE public.turns (
    session_id uuid NOT NULL,
    number integer NOT NULL,
    user_inputs jsonb DEFAULT '[]'::jsonb NOT NULL,
    narrated_answer text,
    folded boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    answer text DEFAULT ''::text NOT NULL,
    narrated boolean DEFAULT false NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    prompt_uuid text,
    last_entry_uuid text
);

COMMENT ON COLUMN public.turns.prompt_uuid IS 'Transcript uuid of the prompt that started this turn; `resume_drops_turn` of a fork that drops it.';

COMMENT ON COLUMN public.turns.last_entry_uuid IS 'Transcript uuid of the last assistant entry of this turn; `resume_session_at` of a fork that keeps it.';

ALTER TABLE ONLY public.server_tokens ALTER COLUMN id SET DEFAULT nextval('public.server_tokens_id_seq'::regclass);

ALTER TABLE ONLY public.session_requests ALTER COLUMN id SET DEFAULT nextval('public.session_requests_id_seq'::regclass);

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_session_id_agent_id_key UNIQUE (session_id, agent_id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chat_summaries
    ADD CONSTRAINT chat_summaries_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.checker_runs
    ADD CONSTRAINT checker_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.checker_types
    ADD CONSTRAINT checker_types_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.client_tokens
    ADD CONSTRAINT client_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.client_tokens
    ADD CONSTRAINT client_tokens_session_id_token_sha256_key UNIQUE (session_id, token_sha256);

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT comments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.config
    ADD CONSTRAINT config_pkey PRIMARY KEY (scope, scope_id, key);

ALTER TABLE ONLY public.escalations
    ADD CONSTRAINT escalations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.hosts
    ADD CONSTRAINT hosts_hostname_key UNIQUE (hostname);

ALTER TABLE ONLY public.hosts
    ADD CONSTRAINT hosts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_name_key UNIQUE (name);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.rules
    ADD CONSTRAINT rules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.server_tokens
    ADD CONSTRAINT server_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.server_tokens
    ADD CONSTRAINT server_tokens_token_sha256_key UNIQUE (token_sha256);

ALTER TABLE ONLY public.servers
    ADD CONSTRAINT servers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.session_keys
    ADD CONSTRAINT session_keys_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.session_requests
    ADD CONSTRAINT session_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.steps
    ADD CONSTRAINT steps_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.steps
    ADD CONSTRAINT steps_session_id_seq_key UNIQUE (session_id, seq);

ALTER TABLE ONLY public.trusted_commits
    ADD CONSTRAINT trusted_commits_pkey PRIMARY KEY (commit_id);

ALTER TABLE ONLY public.trusted_files
    ADD CONSTRAINT trusted_files_pkey PRIMARY KEY (content_sha256);

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_pkey PRIMARY KEY (session_id, number);

CREATE INDEX agents_by_parent ON public.agents USING btree (session_id, parent_agent_id);

CREATE INDEX audit_log_session_idx ON public.audit_log USING btree (session_id, at DESC) WHERE (session_id IS NOT NULL);

CREATE INDEX comments_by_session ON public.comments USING btree (session_id, created_at);

CREATE INDEX escalations_open ON public.escalations USING btree (session_id) WHERE (state = 'open'::text);

CREATE INDEX rules_by_list ON public.rules USING btree (list, project_id, "position");

CREATE INDEX session_requests_pending ON public.session_requests USING btree (hostname, os_user, id) WHERE (state = 'pending'::text);

CREATE INDEX sessions_active ON public.sessions USING btree (state) WHERE (ended_at IS NULL);

CREATE INDEX sessions_claude_session ON public.sessions USING btree (claude_session_id, started_at DESC) WHERE (claude_session_id IS NOT NULL);

CREATE INDEX trusted_commits_tree ON public.trusted_commits USING btree (tree_id);

CREATE INDEX trusted_files_age ON public.trusted_files USING btree (certified_at) WHERE (method <> 'user'::text);

CREATE TRIGGER checker_types_audit AFTER INSERT OR DELETE OR UPDATE ON public.checker_types FOR EACH ROW EXECUTE FUNCTION public.calu_audit();

CREATE TRIGGER checker_types_version BEFORE UPDATE ON public.checker_types FOR EACH ROW EXECUTE FUNCTION public.calu_checker_version();

CREATE TRIGGER config_audit AFTER INSERT OR DELETE OR UPDATE ON public.config FOR EACH ROW EXECUTE FUNCTION public.calu_audit();

CREATE TRIGGER rules_audit AFTER INSERT OR DELETE OR UPDATE ON public.rules FOR EACH ROW EXECUTE FUNCTION public.calu_audit();

CREATE TRIGGER session_requested AFTER INSERT ON public.session_requests FOR EACH ROW EXECUTE FUNCTION public.calu_session_requested();

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_summaries
    ADD CONSTRAINT chat_summaries_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.checker_runs
    ADD CONSTRAINT checker_runs_step_id_fkey FOREIGN KEY (step_id) REFERENCES public.steps(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.client_tokens
    ADD CONSTRAINT client_tokens_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT comments_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.escalations
    ADD CONSTRAINT escalations_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.escalations
    ADD CONSTRAINT escalations_step_id_fkey FOREIGN KEY (step_id) REFERENCES public.steps(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.rules
    ADD CONSTRAINT rules_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.server_tokens
    ADD CONSTRAINT server_tokens_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.servers(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.session_keys
    ADD CONSTRAINT session_keys_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.session_requests
    ADD CONSTRAINT session_requests_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.servers(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.session_requests
    ADD CONSTRAINT session_requests_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.hosts(id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.steps
    ADD CONSTRAINT steps_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.trusted_commits
    ADD CONSTRAINT trusted_commits_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.trusted_files
    ADD CONSTRAINT trusted_files_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;

CREATE POLICY admin_all ON public.agents TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.chat_summaries TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.checker_runs TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.client_tokens TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.comments TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.escalations TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.server_tokens TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.servers TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.session_requests TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.sessions TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.steps TO calu_admin USING (true) WITH CHECK (true);

CREATE POLICY admin_all ON public.turns TO calu_admin USING (true) WITH CHECK (true);

ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.chat_summaries ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.checker_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.client_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.comments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.escalations ENABLE ROW LEVEL SECURITY;

CREATE POLICY operator_all ON public.session_requests TO calu_operator USING (true) WITH CHECK (true);

CREATE POLICY operator_reads ON public.agents FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.chat_summaries FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.checker_runs FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.comments FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.escalations FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.servers FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.sessions FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.steps FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_reads ON public.turns FOR SELECT TO calu_operator USING (true);

CREATE POLICY operator_renames ON public.sessions FOR UPDATE TO calu_operator USING (true) WITH CHECK (true);

CREATE POLICY operator_tokens ON public.client_tokens TO calu_operator USING (true) WITH CHECK (true);

CREATE POLICY operator_tokens ON public.server_tokens TO calu_operator USING (true) WITH CHECK (true);

ALTER TABLE public.server_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.servers ENABLE ROW LEVEL SECURITY;

CREATE POLICY session_inserts ON public.chat_summaries FOR INSERT TO calu_session WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_inserts ON public.checker_runs FOR INSERT TO calu_session WITH CHECK (public.calu_own_step(step_id));

CREATE POLICY session_inserts ON public.escalations FOR INSERT TO calu_session WITH CHECK (((session_id = ( SELECT public.calu_current_session() AS calu_current_session)) AND ((step_id IS NULL) OR public.calu_own_step(step_id))));

CREATE POLICY session_inserts ON public.steps FOR INSERT TO calu_session WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_inserts ON public.turns FOR INSERT TO calu_session WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_own ON public.agents TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_own ON public.client_tokens TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_own ON public.comments TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_reads ON public.chat_summaries FOR SELECT TO calu_session USING (((session_id = ( SELECT public.calu_current_session() AS calu_current_session)) OR public.calu_same_conversation(session_id)));

CREATE POLICY session_reads ON public.checker_runs FOR SELECT TO calu_session USING ((step_id IN ( SELECT steps.id
   FROM public.steps)));

CREATE POLICY session_reads ON public.escalations FOR SELECT TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_reads ON public.sessions FOR SELECT TO calu_session USING (((id = ( SELECT public.calu_current_session() AS calu_current_session)) OR public.calu_same_conversation(id)));

CREATE POLICY session_reads ON public.steps FOR SELECT TO calu_session USING (((session_id = ( SELECT public.calu_current_session() AS calu_current_session)) OR public.calu_same_conversation(session_id)));

CREATE POLICY session_reads ON public.turns FOR SELECT TO calu_session USING (((session_id = ( SELECT public.calu_current_session() AS calu_current_session)) OR public.calu_same_conversation(session_id)));

ALTER TABLE public.session_requests ENABLE ROW LEVEL SECURITY;

CREATE POLICY session_updates ON public.chat_summaries FOR UPDATE TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_updates ON public.sessions FOR UPDATE TO calu_session USING ((id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_updates ON public.steps FOR UPDATE TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

CREATE POLICY session_updates ON public.turns FOR UPDATE TO calu_session USING ((session_id = ( SELECT public.calu_current_session() AS calu_current_session))) WITH CHECK ((session_id = ( SELECT public.calu_current_session() AS calu_current_session)));

ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.steps ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.turns ENABLE ROW LEVEL SECURITY;

GRANT ALL ON FUNCTION public.calu_audit() TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_caller_is_operator() FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_caller_is_operator() TO calu_session;
GRANT ALL ON FUNCTION public.calu_caller_is_operator() TO calu_operator;
GRANT ALL ON FUNCTION public.calu_caller_is_operator() TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_cancel_escalation(escalation_id bigint, resolution_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_cancel_escalation(escalation_id bigint, resolution_note text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_cancel_escalation(escalation_id bigint, resolution_note text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_cancel_escalation(escalation_id bigint, resolution_note text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_certify_commit(p_commit text, p_tree text, p_method text, p_by text, p_note text) TO calu_admin;

GRANT ALL ON FUNCTION public.calu_checker_version() TO calu_admin;

GRANT SELECT,INSERT ON TABLE public.session_requests TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_requests TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_claim_served_request(p_server uuid, p_pid integer, max_age_s double precision) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_claim_served_request(p_server uuid, p_pid integer, max_age_s double precision) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_claim_served_request(p_server uuid, p_pid integer, max_age_s double precision) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_claim_session_request(hostname text, os_user text, pid integer, max_age_s double precision) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_claim_session_request(hostname text, os_user text, pid integer, max_age_s double precision) TO calu_session;
GRANT ALL ON FUNCTION public.calu_claim_session_request(hostname text, os_user text, pid integer, max_age_s double precision) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_client_decision(escalation_id bigint, for_session uuid, token_sha256 text, decision text, resolution_note text, actor text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_client_decision(escalation_id bigint, for_session uuid, token_sha256 text, decision text, resolution_note text, actor text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_client_decision(escalation_id bigint, for_session uuid, token_sha256 text, decision text, resolution_note text, actor text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_client_decision(escalation_id bigint, for_session uuid, token_sha256 text, decision text, resolution_note text, actor text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_client_token_ok(p_session uuid, p_hash text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_client_token_ok(p_session uuid, p_hash text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_client_token_ok(p_session uuid, p_hash text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_close_gone_escalations(silent_s double precision) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_close_gone_escalations(silent_s double precision) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_close_gone_escalations(silent_s double precision) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_close_session_escalations(p_session uuid, resolution_note text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_conversation_sessions(claude_session_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_conversation_sessions(claude_session_id text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_conversation_sessions(claude_session_id text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_conversation_sessions(claude_session_id text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_current_session() FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_current_session() TO calu_session;
GRANT ALL ON FUNCTION public.calu_current_session() TO calu_operator;
GRANT ALL ON FUNCTION public.calu_current_session() TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_distrust_file(p_sha256 text, p_by text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_distrust_file(p_sha256 text, p_by text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_distrust_file(p_sha256 text, p_by text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_expire_session_request(request_id bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_expire_session_request(request_id bigint) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_expire_session_request(request_id bigint) TO calu_operator;

REVOKE ALL ON FUNCTION public.calu_finish_session_request(request_id bigint, outcome text, session_id uuid, message text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_finish_session_request(request_id bigint, outcome text, session_id uuid, message text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_finish_session_request(request_id bigint, outcome text, session_id uuid, message text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_fork_point(p_claude_session_id text, p_turn integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_fork_point(p_claude_session_id text, p_turn integer) TO calu_session;
GRANT ALL ON FUNCTION public.calu_fork_point(p_claude_session_id text, p_turn integer) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_fork_point(p_claude_session_id text, p_turn integer) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_own_step(step bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_own_step(step bigint) TO calu_session;
GRANT ALL ON FUNCTION public.calu_own_step(step bigint) TO calu_admin;

GRANT ALL ON FUNCTION public.calu_path_within(path text, base text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_prune_trust(p_days integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_prune_trust(p_days integer) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_prune_trust(p_days integer) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_recent_sessions(for_directory text, max_rows integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_recent_sessions(for_directory text, max_rows integer) TO calu_session;
GRANT ALL ON FUNCTION public.calu_recent_sessions(for_directory text, max_rows integer) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_recent_sessions(for_directory text, max_rows integer) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_relay_session_request(p_server uuid, p_hash text, directory text, kind text, claude_session_id text, fork_before_turn integer, prompt text, model text, effort text, project text, label text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_relay_session_request(p_server uuid, p_hash text, directory text, kind text, claude_session_id text, fork_before_turn integer, prompt text, model text, effort text, project text, label text) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_relay_session_request(p_server uuid, p_hash text, directory text, kind text, claude_session_id text, fork_before_turn integer, prompt text, model text, effort text, project text, label text) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text, actor text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text, actor text) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text, actor text) TO calu_operator;

REVOKE ALL ON FUNCTION public.calu_revoke_session_tokens(p_session uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_revoke_session_tokens(p_session uuid) TO calu_session;
GRANT ALL ON FUNCTION public.calu_revoke_session_tokens(p_session uuid) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_revoke_session_tokens(p_session uuid) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_same_conversation(other uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_same_conversation(other uuid) TO calu_session;
GRANT ALL ON FUNCTION public.calu_same_conversation(other uuid) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_server_beat(p_server uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_beat(p_server uuid) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_beat(p_server uuid) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_ended(p_server uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_ended(p_server uuid) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_ended(p_server uuid) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_gone(p_server uuid, p_other uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_gone(p_server uuid, p_other uuid) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_gone(p_server uuid, p_other uuid) TO calu_session;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.servers TO calu_admin;
GRANT SELECT ON TABLE public.servers TO calu_operator;

REVOKE ALL ON FUNCTION public.calu_server_live(s public.servers) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_live(s public.servers) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_server_peers(p_server uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_peers(p_server uuid) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_peers(p_server uuid) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_sessions(p_server uuid, p_hash text, p_ids uuid[]) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_sessions(p_server uuid, p_hash text, p_ids uuid[]) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_sessions(p_server uuid, p_hash text, p_ids uuid[]) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text, projects_directory text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text, projects_directory text) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_started(hostname text, os_user text, pid integer, machine text, relay_address text, projects_directory text) TO calu_session;

REVOKE ALL ON FUNCTION public.calu_server_token_ok(p_server uuid, p_hash text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_server_token_ok(p_server uuid, p_hash text) TO calu_admin;
GRANT ALL ON FUNCTION public.calu_server_token_ok(p_server uuid, p_hash text) TO calu_session;

GRANT ALL ON FUNCTION public.calu_session_requested() TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_session_setting_change(for_session uuid, setting text, old_value jsonb, new_value jsonb, client text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_session_setting_change(for_session uuid, setting text, old_value jsonb, new_value jsonb, client text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_session_setting_change(for_session uuid, setting text, old_value jsonb, new_value jsonb, client text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_session_setting_change(for_session uuid, setting text, old_value jsonb, new_value jsonb, client text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_start_session(project_id bigint, host_id bigint, os_user text, directory text, git_commit text, pid integer, model text, key_sha256 text, machine text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_start_session(project_id bigint, host_id bigint, os_user text, directory text, git_commit text, pid integer, model text, key_sha256 text, machine text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_start_session(project_id bigint, host_id bigint, os_user text, directory text, git_commit text, pid integer, model text, key_sha256 text, machine text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_start_session(project_id bigint, host_id bigint, os_user text, directory text, git_commit text, pid integer, model text, key_sha256 text, machine text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_trust_file(p_sha256 text, p_path text, p_method text, p_by text, p_note text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_trust_rank(p_method text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_trust_rank(p_method text) TO calu_session;
GRANT ALL ON FUNCTION public.calu_trust_rank(p_method text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_trust_rank(p_method text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_untrust_commit(p_commit text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_untrust_commit(p_commit text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_untrust_commit(p_commit text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_untrust_file(p_hash_or_path text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_untrust_file(p_hash_or_path text) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_untrust_file(p_hash_or_path text) TO calu_admin;

REVOKE ALL ON FUNCTION public.calu_withdraw_session_request(request_id bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.calu_withdraw_session_request(request_id bigint) TO calu_operator;
GRANT ALL ON FUNCTION public.calu_withdraw_session_request(request_id bigint) TO calu_admin;

GRANT SELECT,INSERT,UPDATE ON TABLE public.agents TO calu_session;
GRANT SELECT ON TABLE public.agents TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.agents_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.audit_log TO calu_operator;
GRANT SELECT,INSERT ON TABLE public.audit_log TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.audit_log_id_seq TO calu_admin;

GRANT SELECT,INSERT,UPDATE ON TABLE public.chat_summaries TO calu_session;
GRANT SELECT ON TABLE public.chat_summaries TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_summaries TO calu_admin;

GRANT SELECT,INSERT ON TABLE public.checker_runs TO calu_session;
GRANT SELECT ON TABLE public.checker_runs TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checker_runs TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.checker_runs_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.checker_types TO calu_session;
GRANT SELECT ON TABLE public.checker_types TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checker_types TO calu_admin;

GRANT INSERT ON TABLE public.client_tokens TO calu_session;
GRANT SELECT,INSERT,UPDATE ON TABLE public.client_tokens TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.client_tokens TO calu_admin;

GRANT SELECT(id) ON TABLE public.client_tokens TO calu_session;

GRANT SELECT,USAGE ON SEQUENCE public.client_tokens_id_seq TO calu_admin;

GRANT SELECT,INSERT ON TABLE public.comments TO calu_session;
GRANT SELECT ON TABLE public.comments TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comments TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.comments_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.config TO calu_session;
GRANT SELECT ON TABLE public.config TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.config TO calu_admin;

GRANT SELECT,INSERT ON TABLE public.escalations TO calu_session;
GRANT SELECT ON TABLE public.escalations TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.escalations TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.escalations_id_seq TO calu_admin;

GRANT SELECT,INSERT,UPDATE ON TABLE public.hosts TO calu_session;
GRANT SELECT ON TABLE public.hosts TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hosts TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.hosts_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.projects TO calu_session;
GRANT SELECT ON TABLE public.projects TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.projects TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.projects_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.rules TO calu_session;
GRANT SELECT ON TABLE public.rules TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rules TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.rules_id_seq TO calu_admin;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.server_tokens TO calu_admin;
GRANT SELECT,INSERT ON TABLE public.server_tokens TO calu_operator;

GRANT UPDATE(revoked) ON TABLE public.server_tokens TO calu_operator;

GRANT SELECT,USAGE ON SEQUENCE public.server_tokens_id_seq TO calu_admin;
GRANT SELECT,USAGE ON SEQUENCE public.server_tokens_id_seq TO calu_operator;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_keys TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.session_requests_id_seq TO calu_operator;
GRANT SELECT,USAGE ON SEQUENCE public.session_requests_id_seq TO calu_admin;

GRANT SELECT,INSERT ON TABLE public.sessions TO calu_session;
GRANT SELECT ON TABLE public.sessions TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sessions TO calu_admin;

GRANT UPDATE(claude_session_id) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(model) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(state) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(state_since) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(ended_at) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(address) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(last_seen) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(forked_from) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(forked_before_turn) ON TABLE public.sessions TO calu_session;

GRANT UPDATE(name) ON TABLE public.sessions TO calu_operator;

GRANT SELECT,INSERT,UPDATE ON TABLE public.steps TO calu_session;
GRANT SELECT ON TABLE public.steps TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.steps TO calu_admin;

GRANT SELECT,USAGE ON SEQUENCE public.steps_id_seq TO calu_admin;

GRANT SELECT ON TABLE public.trusted_commits TO calu_session;
GRANT SELECT ON TABLE public.trusted_commits TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trusted_commits TO calu_admin;

GRANT SELECT ON TABLE public.trusted_files TO calu_session;
GRANT SELECT ON TABLE public.trusted_files TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trusted_files TO calu_admin;

GRANT SELECT,INSERT,UPDATE ON TABLE public.turns TO calu_session;
GRANT SELECT ON TABLE public.turns TO calu_operator;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.turns TO calu_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,USAGE ON SEQUENCES TO calu_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO calu_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO calu_admin;

-- Back to the defaults for the rest of the transaction (the migration's row in
-- schema_migrations is written next, unqualified).
SET LOCAL search_path TO DEFAULT;
SET LOCAL check_function_bodies TO DEFAULT;
