-- calu schema, version 38: one relay port per `calu serve`, with tokens per server
-- (agent/records/PLAN.md D242).
--
-- A `calu serve` process records itself in `servers` when it starts (with the address of its
-- relay) and ends its row when it stops. A server token lets a client on the relay attach to
-- every session of that one server and ask it for new sessions; it is valid until it is revoked
-- or its server ends.
--
--   operator ──mint──► server_tokens (hash only) ──► the relay asks calu_server_token_ok
--   server   ──calu_server_started──► servers row ──calu_server_ended──► ended, tokens revoked
--
-- The session role (held by `calu serve`) cannot read a token's hash; it can only ask whether
-- a hash it was given is a valid token of a server, as for client tokens (migration 0009).

CREATE TABLE servers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname text NOT NULL,
    os_user text NOT NULL,
    pid integer NOT NULL,
    machine text,
    relay_address text NOT NULL DEFAULT '',
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);

COMMENT ON TABLE servers IS 'One row per calu serve process (D242)';
COMMENT ON COLUMN servers.machine IS
    'machine id|boot id|pid namespace of the process (calu.machine), as sessions.machine';
COMMENT ON COLUMN servers.relay_address IS
    'ws://127.0.0.1:<port> of the relay on the server''s machine; empty without a relay';

CREATE TABLE server_tokens (
    id bigserial PRIMARY KEY,
    server_id uuid NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    token_sha256 text NOT NULL UNIQUE,
    label text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked boolean NOT NULL DEFAULT false
);

COMMENT ON TABLE server_tokens IS
    'Tokens for the relay of one calu serve: attach to its sessions and ask it for new ones';

ALTER TABLE servers ENABLE ROW LEVEL SECURITY;
ALTER TABLE server_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY operator_reads ON servers FOR SELECT TO calu_operator USING (true);
CREATE POLICY operator_tokens ON server_tokens TO calu_operator USING (true) WITH CHECK (true);
-- The database owner's login (D213) sees and changes everything, as on every table with
-- row-level security (migration 0033).
CREATE POLICY admin_all ON servers TO calu_admin USING (true) WITH CHECK (true);
CREATE POLICY admin_all ON server_tokens TO calu_admin USING (true) WITH CHECK (true);
GRANT SELECT ON servers TO calu_operator;
GRANT SELECT, INSERT ON server_tokens TO calu_operator;
GRANT UPDATE (revoked) ON server_tokens TO calu_operator;
GRANT USAGE, SELECT ON SEQUENCE server_tokens_id_seq TO calu_operator;

-- A server records itself. Returns the new row's id.
CREATE FUNCTION calu_server_started(
    hostname text, os_user text, pid integer, machine text, relay_address text
) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    INSERT INTO servers (hostname, os_user, pid, machine, relay_address)
    VALUES (hostname, os_user, pid, machine, coalesce(relay_address, ''))
    RETURNING id;
$$;

REVOKE ALL ON FUNCTION calu_server_started(text, text, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_started(text, text, integer, text, text) TO calu_session;

-- A server ends its row and revokes its tokens. Like `calu_revoke_session_tokens`, this only
-- moves rows in the one safe direction: nothing is un-revoked or re-opened.
CREATE FUNCTION calu_server_ended(p_server uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE server_tokens SET revoked = true WHERE server_id = p_server AND NOT revoked;
    UPDATE servers SET ended_at = now() WHERE id = p_server AND ended_at IS NULL;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION calu_server_ended(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_ended(uuid) TO calu_session;

-- The relay asks whether a hash is a valid token of its server. Returns the token's label
-- (possibly '') for a valid one and stamps last_used_at; NULL otherwise.
CREATE FUNCTION calu_server_token_ok(p_server uuid, p_hash text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_id bigint;
    v_label text;
BEGIN
    SELECT t.id, t.label INTO v_id, v_label
      FROM server_tokens t JOIN servers s ON s.id = t.server_id
     WHERE t.server_id = p_server AND t.token_sha256 = p_hash
       AND NOT t.revoked AND s.ended_at IS NULL;
    IF v_id IS NULL THEN
        RETURN NULL;
    END IF;
    UPDATE server_tokens SET last_used_at = now() WHERE id = v_id;
    RETURN v_label;
END
$$;

REVOKE ALL ON FUNCTION calu_server_token_ok(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_server_token_ok(uuid, text) TO calu_session;

-- The relay files a session request for the holder of a valid server token (D242 (4)). The
-- session role still cannot write `session_requests` itself: a request needs either an
-- operator login (D208) or a valid token of the server that will claim it. The request names
-- that server's host and user, and `requested_by` says that it came through the relay.
CREATE FUNCTION calu_relay_session_request(
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
                                  project)
    VALUES ('relay', coalesce(calu_relay_session_request.label, ''), v_host, v_user,
            calu_relay_session_request.directory, calu_relay_session_request.kind,
            calu_relay_session_request.claude_session_id,
            calu_relay_session_request.fork_before_turn, calu_relay_session_request.prompt,
            calu_relay_session_request.model, calu_relay_session_request.effort,
            calu_relay_session_request.project)
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;

REVOKE ALL ON FUNCTION calu_relay_session_request(
    uuid, text, text, text, text, integer, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_relay_session_request(
    uuid, text, text, text, text, integer, text, text, text, text, text) TO calu_session;
