-- calu schema, version 39: the projects directory of a `calu serve` (agent/records/PLAN.md
-- D243).
--
-- A server serves one projects directory and starts sessions only in it or below it. Its
-- `servers` row records the directory, so that clients can show it. A server claims a request
-- through its row:
--
--   request filed through its relay (server_id = the server)   ──► only that server claims it
--   request filed by an operator (server_id NULL)               ──► a server of the request's
--       host and OS user claims it if the directory is inside its projects directory, or if no
--       other running server of that host and user has the directory inside its own; the
--       claiming server checks the resolved path and refuses the request with the reason
--
-- So a request outside every server's directory is still answered (refused, with a message
-- that names the directory served), and of two servers of one user the one that serves the
-- directory starts the session.

ALTER TABLE servers ADD COLUMN projects_directory text NOT NULL DEFAULT '';

COMMENT ON COLUMN servers.projects_directory IS
    'The resolved directory the server starts sessions in (or below); empty if not recorded';

ALTER TABLE session_requests ADD COLUMN server_id uuid REFERENCES servers (id) ON DELETE SET NULL;

COMMENT ON COLUMN session_requests.server_id IS
    'The server whose relay filed the request: only that one claims it; NULL for an operator';

-- True if `path` is `base` or lies below it, compared as text. The server checks the resolved
-- path itself; this only decides which server claims a request.
CREATE FUNCTION calu_path_within(path text, base text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT base <> '' AND (path = base OR starts_with(path, rtrim(base, '/') || '/'));
$$;

-- A server records itself with its projects directory. Returns the new row's id.
CREATE FUNCTION calu_server_started(
    hostname text, os_user text, pid integer, machine text, relay_address text,
    projects_directory text
) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    INSERT INTO servers (hostname, os_user, pid, machine, relay_address, projects_directory)
    VALUES (hostname, os_user, pid, machine, coalesce(relay_address, ''),
            coalesce(projects_directory, ''))
    RETURNING id;
$$;

REVOKE ALL ON FUNCTION calu_server_started(text, text, integer, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_started(text, text, integer, text, text, text)
    TO calu_session;

-- A server claims the oldest pending request it is meant for (see the top of this file).
CREATE FUNCTION calu_claim_served_request(
    p_server uuid,
    p_pid integer,
    max_age_s double precision DEFAULT 60
) RETURNS SETOF session_requests
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v servers%ROWTYPE;
BEGIN
    SELECT * INTO v FROM servers s WHERE s.id = p_server AND s.ended_at IS NULL;
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
                                 WHERE o.ended_at IS NULL AND o.id <> v.id
                                   AND o.hostname = v.hostname AND o.os_user = v.os_user
                                   AND calu_path_within(p.directory, o.projects_directory)))))
             ORDER BY p.id
             LIMIT 1
               FOR UPDATE SKIP LOCKED)
    RETURNING r.*;
END
$$;

REVOKE ALL ON FUNCTION calu_claim_served_request(uuid, integer, double precision) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_claim_served_request(uuid, integer, double precision)
    TO calu_session;

-- As in migration 0038, and the request now names the server that filed it, which alone
-- claims it.
CREATE OR REPLACE FUNCTION calu_relay_session_request(
    p_server uuid,
    p_hash text,
    directory text,
    kind text,
    claude_session_id text,
    fork_before_turn integer,
    prompt text,
    model text,
    effort text,
    project text,
    label text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_host text;
    v_user text;
    v_id bigint;
BEGIN
    SELECT s.hostname, s.os_user INTO v_host, v_user
      FROM servers s JOIN server_tokens t ON t.server_id = s.id
     WHERE s.id = p_server AND s.ended_at IS NULL
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
