-- A question from the agent is an escalation whose answer is the answer, not an allow or a deny
-- (PLAN.md D35, measured in spikes/RESULTS.md). `AskUserQuestion` reaches calu's hook with the
-- whole question in it; the human picks an option and Claude receives that text as the message
-- of a refusal, which is how the Agent SDK lets a host answer a tool call. The step itself never
-- runs, so neither "allowed" nor "denied" describes what happened: the third state does.

ALTER TABLE escalations DROP CONSTRAINT escalations_state_check;
ALTER TABLE escalations ADD CONSTRAINT escalations_state_check
    CHECK (state IN ('open', 'allowed', 'denied', 'answered', 'cancelled'));

COMMENT ON COLUMN escalations.state IS
    'open, allowed, denied, answered (a question of the agent, note holds the answer) or cancelled.';

-- Both resolution functions accept the new state. They are otherwise unchanged (0001 and 0007).
CREATE OR REPLACE FUNCTION calu_resolve_escalation(
    escalation_id bigint, decision text, resolution_note text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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
