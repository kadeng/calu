-- calu schema, version 7: a session records the decision of one of its clients (PLAN.md D28).
--
-- A client of a session process -- `calu attach`, the ACP adapter, an IDE panel -- has no
-- database login of its own: it authenticates to the session with a client token and the session
-- speaks to the database for it. Until now the session could only *cancel* an escalation, so a
-- client's decision reached the database as a cancellation with a note, which is a poor record
-- of "a human allowed this".
--
-- `calu_client_decision` lets the session record the real decision. It is not the same as
-- `calu_resolve_escalation` (operator only):
--
--   * it resolves only an escalation of the session it is called for, and only while that
--     escalation is open;
--   * it requires the SHA-256 of a client token of that session, which the session process holds
--     in memory and which the role `calu_session` cannot read back (the SELECT privilege on
--     `client_tokens` is narrowed to the id below), so the database credential alone is not
--     enough to forge a decision;
--   * it records in `resolved_by` that the decision came through a client, and which one.

CREATE FUNCTION calu_client_decision(
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
    IF decision NOT IN ('allowed', 'denied') THEN
        RAISE EXCEPTION 'decision must be allowed or denied, not %', decision;
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

REVOKE ALL ON FUNCTION calu_client_decision(bigint, uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_client_decision(bigint, uuid, text, text, text, text)
    TO calu_session, calu_operator;

-- The token hash becomes a capability the session holds, not a value its role can read back.
-- The id stays readable, because `INSERT ... RETURNING id` needs it.
REVOKE SELECT ON client_tokens FROM calu_session;
GRANT SELECT (id) ON client_tokens TO calu_session;
