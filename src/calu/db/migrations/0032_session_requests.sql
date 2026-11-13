-- calu schema, version 32: requests for a session, answered by `calu serve` (PLAN.md D208).
--
-- `calu serve` is one process that holds the session login and starts sessions on request.
-- A request is a row of `session_requests`, written with the operator login (or by the
-- owner); the server claims it, starts the session and writes the outcome back:
--
--   pending ──claim──► claimed ──finish──► started | failed
--      └──withdraw──► withdrawn            (the requester gave up waiting for a server)
--
-- Who may ask for a session is therefore decided by who has a database login that may write
-- this table. A session login cannot write it; it can only claim and finish, through the two
-- functions below.
--
-- Notification: session_requested, payload {"id": ..., "hostname": ..., "os_user": ...}.

CREATE TABLE session_requests (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    requested_by text NOT NULL DEFAULT session_user,
    label text NOT NULL DEFAULT '',
    hostname text NOT NULL,
    os_user text NOT NULL,
    directory text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('new', 'resume', 'fork')),
    claude_session_id text,
    fork_before_turn integer CHECK (fork_before_turn >= 2),
    prompt text,
    model text,
    effort text,
    project text,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'claimed', 'started', 'failed', 'withdrawn')),
    claimed_at timestamptz,
    claimed_pid integer,
    finished_at timestamptz,
    session_id uuid REFERENCES sessions (id),
    message text NOT NULL DEFAULT '',
    CHECK ((kind = 'new') = (claude_session_id IS NULL)),
    CHECK (fork_before_turn IS NULL OR kind = 'resume')
);

COMMENT ON TABLE session_requests IS
    'Requests for a session that a calu serve process of that host and OS user starts (D208)';
COMMENT ON COLUMN session_requests.kind IS
    'new: a new conversation; resume: continue claude_session_id (with fork_before_turn: '
    'branch off before that turn); fork: continue a copy of claude_session_id';
COMMENT ON COLUMN session_requests.session_id IS
    'the sessions row of the started session; NULL until started';
COMMENT ON COLUMN session_requests.message IS
    'why the request failed, in words for the requester';

CREATE INDEX session_requests_pending
    ON session_requests (hostname, os_user, id) WHERE state = 'pending';

ALTER TABLE session_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY operator_all ON session_requests TO calu_operator USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON session_requests TO calu_operator;
GRANT USAGE, SELECT ON SEQUENCE session_requests_id_seq TO calu_operator;

CREATE FUNCTION calu_session_requested() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('session_requested', json_build_object(
        'id', NEW.id, 'hostname', NEW.hostname, 'os_user', NEW.os_user)::text);
    RETURN NULL;
END
$$;

CREATE TRIGGER session_requested AFTER INSERT ON session_requests
    FOR EACH ROW EXECUTE FUNCTION calu_session_requested();

-- A server takes the oldest pending request for its host and user. A request older than
-- `max_age_s` is not taken: its requester has stopped waiting, and a session that starts
-- long after it was asked for would surprise everybody.
CREATE FUNCTION calu_claim_session_request(
    hostname text,
    os_user text,
    pid integer,
    max_age_s double precision DEFAULT 60
) RETURNS SETOF session_requests
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

REVOKE ALL ON FUNCTION calu_claim_session_request(text, text, integer, double precision)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_claim_session_request(text, text, integer, double precision)
    TO calu_session;

-- The server writes the outcome of a request it claimed. Returns false if the request is not
-- in the state 'claimed' (finished before, or never claimed).
CREATE FUNCTION calu_finish_session_request(
    request_id bigint,
    outcome text,
    session_id uuid,
    message text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

REVOKE ALL ON FUNCTION calu_finish_session_request(bigint, text, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_finish_session_request(bigint, text, uuid, text) TO calu_session;

-- The requester gives up on a request no server has claimed. Returns false if a server
-- claimed it meanwhile: the requester then waits for the outcome after all.
CREATE FUNCTION calu_withdraw_session_request(request_id bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE session_requests r
       SET state = 'withdrawn', finished_at = now()
     WHERE r.id = calu_withdraw_session_request.request_id AND r.state = 'pending';
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION calu_withdraw_session_request(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_withdraw_session_request(bigint) TO calu_operator;
