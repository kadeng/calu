-- calu schema, version 44: a session that runs as the user login records the trust it
-- verified (agent/records/PLAN.md D265).
--
-- Since migration 0043 every session logs in as `calu_admin`, a member of `calu_operator`.
-- `calu_certify_commit` and `calu_trust_file` (migration 0029) asked `calu_caller_is_operator()`
-- first and allowed an operator the method 'user' only, so a session refused its own 'check'
-- and 'review' certifications (a clean commit, a checked edit, an honesty review). The two
-- functions now ask first whether the connection has proven a session:
--
--     calu_current_session()   caller is operator   allowed methods        session_id recorded
--     a session id             (either)             user, review, check    that session
--     NULL                     yes                  user                   NULL
--     NULL                     no                   none (refused)         -
--
-- Everything else stays as migration 0029 defined it. CREATE OR REPLACE keeps the grants.

CREATE OR REPLACE FUNCTION calu_certify_commit(
    p_commit text, p_tree text, p_method text, p_by text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_commits;
    after      trusted_commits;
BEGIN
    IF of_session IS NULL THEN
        IF NOT calu_caller_is_operator() THEN
            RAISE EXCEPTION 'only a session that has proven its identity certifies a commit'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF calu_trust_rank(p_method) IS NULL THEN
        RAISE EXCEPTION 'the method is one of user, review, check'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO before FROM trusted_commits WHERE commit_id = p_commit FOR UPDATE;
    INSERT INTO trusted_commits AS held
           (commit_id, tree_id, certified_by, method, note, session_id)
    VALUES (p_commit, p_tree, p_by, p_method, nullif(p_note, ''), of_session)
    ON CONFLICT (commit_id) DO UPDATE
       SET tree_id = EXCLUDED.tree_id, certified_at = now(),
           certified_by = EXCLUDED.certified_by, method = EXCLUDED.method,
           note = EXCLUDED.note, session_id = EXCLUDED.session_id,
           recorded_as = session_user
     WHERE calu_trust_rank(EXCLUDED.method) > calu_trust_rank(held.method)
    RETURNING * INTO after;
    IF after.commit_id IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (p_by, of_session, 'trusted_commits',
            CASE WHEN before.commit_id IS NULL THEN 'INSERT' ELSE 'UPDATE' END,
            CASE WHEN before.commit_id IS NULL THEN NULL ELSE to_jsonb(before) END,
            to_jsonb(after));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    RETURN true;
END
$$;

CREATE OR REPLACE FUNCTION calu_trust_file(
    p_sha256 text, p_path text, p_method text, p_by text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_files;
    after      trusted_files;
BEGIN
    IF of_session IS NULL THEN
        IF NOT calu_caller_is_operator() THEN
            RAISE EXCEPTION 'only a session that has proven its identity records a trusted file'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF calu_trust_rank(p_method) IS NULL THEN
        RAISE EXCEPTION 'the method is one of user, review, check'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO before FROM trusted_files WHERE content_sha256 = p_sha256 FOR UPDATE;
    INSERT INTO trusted_files AS held
           (content_sha256, path, certified_by, method, note, session_id)
    VALUES (p_sha256, p_path, p_by, p_method, nullif(p_note, ''), of_session)
    ON CONFLICT (content_sha256) DO UPDATE
       SET path = EXCLUDED.path, certified_at = now(),
           certified_by = EXCLUDED.certified_by, method = EXCLUDED.method,
           note = EXCLUDED.note, session_id = EXCLUDED.session_id,
           recorded_as = session_user
     WHERE calu_trust_rank(EXCLUDED.method) > calu_trust_rank(held.method)
    RETURNING * INTO after;
    IF after.content_sha256 IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, old_row, new_row)
    VALUES (p_by, of_session, 'trusted_files',
            CASE WHEN before.content_sha256 IS NULL THEN 'INSERT' ELSE 'UPDATE' END,
            CASE WHEN before.content_sha256 IS NULL THEN NULL ELSE to_jsonb(before) END,
            to_jsonb(after));
    IF p_method = 'user' THEN
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN true;
END
$$;
