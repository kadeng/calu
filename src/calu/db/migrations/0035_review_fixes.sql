-- calu schema, version 35: requests whose server died, narrower grants, a fixed search path.
--
-- 1. A request a server claimed and never finished (the server died after the claim) stayed
--    'claimed' for good. `calu_expire_session_request` lets the requester mark it 'failed'
--    once the claim is older than START_WAIT_S (180 s, `calu.session_requests`):
--
--      claimed ──finish──► started | failed
--         └──expire (claimed_at older than 180 s)──► failed
--
--    A server that answers later finds the request finished; `calu serve` then stops the
--    session it started, because nobody waits for it.
--
-- 2. Intended, unchanged (the owner, 2026-11-17): `calu_claim_session_request` takes the host
--    name and the OS user from its caller. Every holder of the shared session login
--    (`calu_session`) may claim the requests meant for another host or user, read their
--    prompt and directory, and finish them: the session login is trusted with that.
--
-- 3. `calu_session` had UPDATE on every column of its own `sessions` row (0001). It keeps
--    UPDATE only on the columns `calu.store.session.DatabaseSessionStore` writes: last_seen,
--    state, state_since, ended_at, claude_session_id, model, address, forked_from,
--    forked_before_turn. It can no longer change, for example, its directory, host, OS user,
--    project or start time. The row policy `session_updates` (0026) still limits it to its own
--    row. A later migration that adds a column the session writes must grant UPDATE on it too.
--
-- 4. `calu_admin` could UPDATE and DELETE `audit_log` rows (0033 granted it every table), so
--    an admin could remove the record of its own changes. It keeps SELECT and INSERT: the
--    audit trigger `calu_audit` runs as the caller and inserts the entry.
--
-- 5. `calu_revoke_session_tokens` (0025) is SECURITY DEFINER with `search_path = pg_catalog,
--    public`; without `pg_temp` last, PostgreSQL searches the caller's temporary schema first
--    for tables. It now ends with pg_temp, as the other functions do.
--
-- Every statement can run again without harm.
--
-- Tests for the owner to run: tests/db/test_review_fixes_db.py (this migration),
-- tests/db/test_serve.py, tests/db/test_admin_role.py, tests/db/test_schema.py and
-- tests/db/test_run.py (the session store still writes its row).

CREATE OR REPLACE FUNCTION calu_expire_session_request(request_id bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

REVOKE ALL ON FUNCTION calu_expire_session_request(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_expire_session_request(bigint) TO calu_operator;

COMMENT ON FUNCTION calu_claim_session_request(text, text, integer, double precision) IS
    'Claims the oldest pending request for a host and OS user, as the caller names them. Any '
    'holder of the session login may claim the requests of every host and user (intended)';

REVOKE UPDATE ON sessions FROM calu_session;
GRANT UPDATE (last_seen, state, state_since, ended_at, claude_session_id, model, address,
              forked_from, forked_before_turn)
    ON sessions TO calu_session;

REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM calu_admin;

ALTER FUNCTION calu_revoke_session_tokens(uuid) SET search_path = pg_catalog, public, pg_temp;
