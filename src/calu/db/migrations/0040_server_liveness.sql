-- calu schema, version 40: a `calu serve` that died without ending its row stops counting as
-- running (agent/records/PLAN.md D244).
--
-- Servers get the liveness rules of sessions (D56, D237):
--
--   heartbeat   the server stamps `servers.last_seen` every 10 s (`calu_server_beat`; a claim
--               stamps it too)
--   stale       no stamp for 60 s: the row counts as not running, though it has not ended
--   gone        another server of the same host, OS user and process table (`servers.machine`)
--               finds no process with the row's pid and ends the row (`calu_server_gone`)
--
-- A server that is stale or ended claims no request (`calu_claim_served_request`), does not
-- keep another server from claiming the requests in its directory, and its tokens are refused
-- (`calu_server_token_ok`, `calu_relay_session_request`). The session login cannot read
-- `servers`, so these tests are inside the SECURITY DEFINER functions. The database cannot see
-- processes: "gone" is judged by a server in the same process table, which then asks the
-- database to end the row; the database checks that both rows record the same process table.
--
-- `calu_server_sessions` gives the relay the names and state times of its own sessions
-- (`relay.sessions`), which the session login cannot read either.

ALTER TABLE servers ADD COLUMN last_seen timestamptz NOT NULL DEFAULT now();

COMMENT ON COLUMN servers.last_seen IS
    'Last heartbeat of the server process (every 10 s); silent for 60 s means not running';

-- Whether a server row counts as running: not ended and stamped within the last 60 seconds
-- (`calu.herd.STALE_AFTER_S`).
CREATE FUNCTION calu_server_live(s servers) RETURNS boolean
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT s.ended_at IS NULL AND s.last_seen > now() - interval '60 seconds';
$$;

REVOKE ALL ON FUNCTION calu_server_live(servers) FROM PUBLIC;

-- A server says it is still running. Returns false if its row has ended (then it claims
-- nothing and its tokens are refused).
CREATE FUNCTION calu_server_beat(p_server uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE servers SET last_seen = now() WHERE id = p_server AND ended_at IS NULL;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION calu_server_beat(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_beat(uuid) TO calu_session;

-- The other servers that have not ended and share the caller's host, OS user and recorded
-- process table: the only ones whose process the caller can look up. Nothing is returned for
-- a caller whose row has ended or that recorded no process table.
CREATE FUNCTION calu_server_peers(p_server uuid)
RETURNS TABLE (id uuid, pid integer, machine text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT o.id, o.pid, o.machine
      FROM servers me JOIN servers o
        ON o.hostname = me.hostname AND o.os_user = me.os_user AND o.machine = me.machine
     WHERE me.id = p_server AND me.ended_at IS NULL AND coalesce(me.machine, '') <> ''
       AND o.id <> me.id AND o.ended_at IS NULL;
$$;

REVOKE ALL ON FUNCTION calu_server_peers(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_peers(uuid) TO calu_session;

-- A server ends the row of a peer whose process it found gone, and revokes that peer's
-- tokens. Refused (false, nothing changed) unless both rows are open and record the same host,
-- OS user and process table.
CREATE FUNCTION calu_server_gone(p_server uuid, p_other uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

REVOKE ALL ON FUNCTION calu_server_gone(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_gone(uuid, uuid) TO calu_session;

-- As in migration 0039, with the liveness rule: the claim stamps the claiming server's
-- heartbeat, and a stale server no longer keeps the requests in its directory from the
-- other servers.
CREATE OR REPLACE FUNCTION calu_claim_served_request(
    p_server uuid,
    p_pid integer,
    max_age_s double precision DEFAULT 60
) RETURNS SETOF session_requests
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

-- As in migration 0038: a token of a server that is stale counts as not valid.
CREATE OR REPLACE FUNCTION calu_server_token_ok(p_server uuid, p_hash text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

-- As in migration 0039: a stale server's token files no request.
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

-- The relay lists its server's sessions with their names and since when they are in their
-- state (`relay.sessions`). Only for the holder of a valid token of a running server, and only
-- for the sessions of that server's process (same pid and process table) among `p_ids`.
CREATE FUNCTION calu_server_sessions(p_server uuid, p_hash text, p_ids uuid[])
RETURNS TABLE (id uuid, name text, state_since timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT x.id, x.name, x.state_since
      FROM servers s
      JOIN server_tokens t ON t.server_id = s.id
      JOIN sessions x ON x.pid = s.pid AND x.machine IS NOT DISTINCT FROM s.machine
                     AND x.id = ANY (p_ids)
     WHERE s.id = p_server AND calu_server_live(s)
       AND t.token_sha256 = p_hash AND NOT t.revoked
       AND x.ended_at IS NULL;
$$;

REVOKE ALL ON FUNCTION calu_server_sessions(uuid, text, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_sessions(uuid, text, uuid[]) TO calu_session;
