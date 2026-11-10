-- calu schema, version 13: a client's change of a running session's settings is on the record
-- (PLAN.md D43).
--
-- `auto_escalate.set` is the one setting a client may change through the session protocol (D42):
-- it holds back steps the checkers approved, and it lives in the running process only. Turning
-- it *off* weakens the session, and until now nothing recorded that it happened -- the audit log
-- is fed by triggers on `config`, `rules` and `checker_types`, and a session-local change touches
-- none of them. An operator reading the history afterwards could not tell that every edit was
-- waiting for a human until somebody switched it off.
--
-- `audit_log` gains the session the entry belongs to (NULL for the configuration changes the
-- triggers record, which are project-wide), and `calu_session_setting_change` lets a session
-- write such an entry for itself. The role `calu_session` may not write `audit_log` directly, so
-- it cannot forge an entry for another session or for the project's configuration.

ALTER TABLE audit_log ADD COLUMN session_id uuid REFERENCES sessions (id) ON DELETE CASCADE;

CREATE INDEX audit_log_session_idx ON audit_log (session_id, at DESC)
    WHERE session_id IS NOT NULL;

CREATE FUNCTION calu_session_setting_change(
    for_session uuid,
    setting text,
    old_value jsonb,
    new_value jsonb,
    client text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = for_session) THEN
        RAISE EXCEPTION 'no session %', for_session;
    END IF;
    -- The actor names who asked for it: a client of the session, or the session itself when a
    -- reloaded configuration changed the setting.
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (client, for_session, 'session_settings', setting, old_value, new_value);
END
$$;

REVOKE ALL ON FUNCTION calu_session_setting_change(uuid, text, jsonb, jsonb, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_session_setting_change(uuid, text, jsonb, jsonb, text)
    TO calu_session, calu_operator;
