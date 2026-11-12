-- calu schema, version 28: a session withdraws the stored trust of a file content.
--
-- A session stores the file contents its step checkers and its honesty review verified
-- (`trusted_files`, methods 'check' and 'review'; migration 0029). The marks that say which
-- code a checker found dishonest live in the session's memory only. When a checker or a review
-- finds a content dishonest that was stored as verified, the stored row has to go, or the
-- content would count as verified again after a restart.
--
--     who                        may withdraw
--     calu_session (a session)   a row of the method 'check' or 'review', through
--                                calu_distrust_file; never a row of the method 'user'
--     calu_operator, the owner   any row, through calu_untrust_file (0029)
--
-- The caller must have proven which session it is (migration 0025). The removal is written to
-- `audit_log` with that session and announced on the channel `trust_changed`.

-- Removes the row of that content if its method is 'check' or 'review'. Returns true if a row
-- was removed; false if there is none or a person recorded it.
CREATE FUNCTION calu_distrust_file(p_sha256 text, p_by text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    removed    trusted_files;
BEGIN
    IF of_session IS NULL THEN
        RAISE EXCEPTION 'only a session that has proven its identity withdraws a trusted file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_by IS NULL OR p_by = '' THEN
        RAISE EXCEPTION 'a withdrawal names who withdrew'
            USING ERRCODE = 'check_violation';
    END IF;
    DELETE FROM trusted_files
     WHERE content_sha256 = p_sha256 AND method IN ('check', 'review')
    RETURNING * INTO removed;
    IF removed.content_sha256 IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row)
    VALUES (p_by, of_session, 'trusted_files', 'DELETE', to_jsonb(removed));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION calu_distrust_file(text, text) IS
    'Withdraws a check or review row of trusted_files for a session whose checker or review '
    'found that content dishonest; never a user row (0030).';

REVOKE ALL ON FUNCTION calu_distrust_file(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_distrust_file(text, text) TO calu_session;
