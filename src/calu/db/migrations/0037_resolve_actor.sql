-- calu schema, version 37: who resolved an escalation from the database.
--
-- `calu resolve` recorded only the database role (`resolved_by = session_user`, for example
-- "calu_operator"), which is the same for everyone who holds the operator login. The command
-- now also says which OS user on which host ran it, and the record reads
--
--   operator:kai@buildbox        role calu_operator, OS user kai on host buildbox
--   admin:kai@buildbox           role calu_admin
--   <role>:kai@buildbox          any other role, under its own name
--
-- The role is what the database authenticated. The part after the colon is what the caller
-- says about itself: a hint for people, not proof. It is cut to 200 characters, and control
-- characters are removed. Without it (NULL or empty) the record is the role alone, as before.
--
-- The function of three arguments stays for callers that name nobody (the web interface).
-- Every statement can run again without harm.

CREATE OR REPLACE FUNCTION calu_resolve_escalation(
    escalation_id bigint, decision text, resolution_note text, actor text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

REVOKE ALL ON FUNCTION calu_resolve_escalation(bigint, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_resolve_escalation(bigint, text, text, text)
    TO calu_operator, calu_admin;
