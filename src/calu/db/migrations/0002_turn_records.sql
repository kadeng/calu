-- calu schema, version 2: complete turn records and a lookup for resumed sessions.
--
-- Version 1 kept only the user inputs and the narrated answer of a turn. The context builder
-- (`calu.context`) needs the whole `TurnRecord` to restore the context of a resumed session, so
-- the remaining fields are added here. `turns.narrated_answer` holds `answer_narration`.

ALTER TABLE turns
    ADD COLUMN steps      jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN answer     text NOT NULL DEFAULT '',
    ADD COLUMN narrated   boolean NOT NULL DEFAULT false,
    ADD COLUMN started_at timestamptz,
    ADD COLUMN ended_at   timestamptz;

-- `calu run --resume <claude session id>` looks up the turns and the chat summary of the calu
-- session that ran that Claude session last.
CREATE INDEX sessions_claude_session ON sessions (claude_session_id, started_at DESC)
    WHERE claude_session_id IS NOT NULL;

-- A session cancels its own escalations and records why (e.g. "the session was stopped", or a
-- decision that reached it from an attached client). The role may still not allow or deny one:
-- that stays with calu_resolve_escalation, which only calu_operator may call.
DROP FUNCTION calu_cancel_escalation(bigint);

CREATE FUNCTION calu_cancel_escalation(escalation_id bigint, resolution_note text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE escalations
       SET state = 'cancelled', resolved_by = session_user, resolved_at = now(),
           note = coalesce(resolution_note, note)
     WHERE id = escalation_id AND state = 'open';
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION calu_cancel_escalation(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_cancel_escalation(bigint, text) TO calu_session, calu_operator;
