-- calu schema, version 27: trusted commits and trusted file contents (code honesty).
--
-- calu tracks which code was verified to say what it does. Two tables hold what was certified:
--
--     trusted_commits   a commit that a person, an honesty review or a session certified.
--                       A commit counts as trusted if its commit id or its tree id is here.
--                       A session's baseline is the nearest trusted ancestor of HEAD.
--     trusted_files     the SHA-256 of a file content that a person certified, for files git
--                       does not hold. A file with that content counts as verified wherever
--                       it lies. `path` is a hint for people and decides nothing.
--
-- Both tables are global: a commit id identifies its repository, a hash its content.
--
--     who                         reads   adds                              removes
--     calu_session (a session)    yes     calu_certify_commit, calu_trust_file   no
--     calu_operator, the owner    yes     the same functions                calu_untrust_commit,
--                                                                           calu_untrust_file
--
-- No role but the owner has INSERT, UPDATE or DELETE on the tables. A caller of the role
-- `calu_session` must have proven which session it is (migration 0025); the row records that
-- session. An operator or the owner records certifications of the method 'user' only. Every
-- addition and removal is written to `audit_log` and announced on the channel `trust_changed`,
-- so a running session can look up its baseline again.

CREATE TABLE trusted_commits (
    commit_id     text PRIMARY KEY CHECK (commit_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    tree_id       text NOT NULL CHECK (tree_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    certified_at  timestamptz NOT NULL DEFAULT now(),
    certified_by  text NOT NULL CHECK (certified_by <> ''),
    method        text NOT NULL CHECK (method IN ('user', 'review', 'session')),
    note          text,
    session_id    uuid REFERENCES sessions (id) ON DELETE SET NULL,
    recorded_as   text NOT NULL DEFAULT session_user
);

CREATE INDEX trusted_commits_tree ON trusted_commits (tree_id);

COMMENT ON TABLE trusted_commits IS
    'Commits certified as honest. method: user (a person, certified_by names the client or OS '
    'user), review (an honesty review), session (a commit made in a clean state). session_id: '
    'the session that recorded it, if one did. recorded_as: the database login (0027).';

CREATE TABLE trusted_files (
    content_sha256  text PRIMARY KEY CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    path            text NOT NULL CHECK (path <> ''),
    certified_at    timestamptz NOT NULL DEFAULT now(),
    certified_by    text NOT NULL CHECK (certified_by <> ''),
    method          text NOT NULL DEFAULT 'user' CHECK (method = 'user'),
    note            text,
    session_id      uuid REFERENCES sessions (id) ON DELETE SET NULL,
    recorded_as     text NOT NULL DEFAULT session_user
);

COMMENT ON TABLE trusted_files IS
    'File contents a person certified as honest, by SHA-256; path is a hint for people (0027).';

REVOKE ALL ON trusted_commits, trusted_files FROM PUBLIC;
GRANT SELECT ON trusted_commits, trusted_files TO calu_session, calu_operator;

-- Records a certification. Returns true if the commit was not trusted before, false if a row
-- for it exists (the first certification is kept).
CREATE FUNCTION calu_certify_commit(
    p_commit text, p_tree text, p_method text, p_by text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    added      trusted_commits;
BEGIN
    IF calu_caller_is_operator() THEN
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        of_session := NULL;
    ELSIF of_session IS NULL THEN
        RAISE EXCEPTION 'only a session that has proven its identity certifies a commit'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO trusted_commits (commit_id, tree_id, certified_by, method, note, session_id)
    VALUES (p_commit, p_tree, p_by, p_method, nullif(p_note, ''), of_session)
    ON CONFLICT (commit_id) DO NOTHING
    RETURNING * INTO added;
    IF added.commit_id IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, new_row)
    VALUES (p_by, of_session, 'trusted_commits', 'INSERT', to_jsonb(added));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION calu_certify_commit(text, text, text, text, text) IS
    'Records a commit as trusted; the only way a session adds to trusted_commits (0027).';

-- Removes a certification. Operators and the owner only. Returns true if a row was removed.
CREATE FUNCTION calu_untrust_commit(p_commit text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    removed trusted_commits;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator removes a trusted commit'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    DELETE FROM trusted_commits WHERE commit_id = p_commit RETURNING * INTO removed;
    IF removed.commit_id IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, table_name, action, old_row)
    VALUES (session_user, 'trusted_commits', 'DELETE', to_jsonb(removed));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION calu_untrust_commit(text) IS
    'Removes a trusted commit; operators and the owner only (0027).';

-- Records a file content a person certified. Returns true if the hash was not trusted before.
CREATE FUNCTION calu_trust_file(p_sha256 text, p_path text, p_by text, p_note text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    added      trusted_files;
BEGIN
    IF calu_caller_is_operator() THEN
        of_session := NULL;
    ELSIF of_session IS NULL THEN
        RAISE EXCEPTION 'only a session that has proven its identity records a trusted file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO trusted_files (content_sha256, path, certified_by, note, session_id)
    VALUES (p_sha256, p_path, p_by, nullif(p_note, ''), of_session)
    ON CONFLICT (content_sha256) DO NOTHING
    RETURNING * INTO added;
    IF added.content_sha256 IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit_log (actor, session_id, table_name, action, new_row)
    VALUES (p_by, of_session, 'trusted_files', 'INSERT', to_jsonb(added));
    PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    RETURN true;
END
$$;

COMMENT ON FUNCTION calu_trust_file(text, text, text, text) IS
    'Records a file content as trusted; the only way a session adds to trusted_files (0027).';

-- Removes the trusted contents with that hash or that path. Operators and the owner only.
-- Returns the number of rows removed.
CREATE FUNCTION calu_untrust_file(p_hash_or_path text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator removes a trusted file'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    WITH gone AS (
        DELETE FROM trusted_files
         WHERE content_sha256 = p_hash_or_path OR path = p_hash_or_path
        RETURNING *
    ), logged AS (
        INSERT INTO audit_log (actor, table_name, action, old_row)
        SELECT session_user, 'trusted_files', 'DELETE', to_jsonb(gone) FROM gone
        RETURNING 1
    )
    SELECT count(*) INTO removed FROM logged;
    IF removed > 0 THEN
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN removed;
END
$$;

COMMENT ON FUNCTION calu_untrust_file(text) IS
    'Removes trusted file contents by hash or path; operators and the owner only (0027).';

REVOKE ALL ON FUNCTION calu_certify_commit(text, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_untrust_commit(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_trust_file(text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_untrust_file(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_certify_commit(text, text, text, text, text)
    TO calu_session, calu_operator;
GRANT EXECUTE ON FUNCTION calu_trust_file(text, text, text, text)
    TO calu_session, calu_operator;
GRANT EXECUTE ON FUNCTION calu_untrust_commit(text) TO calu_operator;
GRANT EXECUTE ON FUNCTION calu_untrust_file(text) TO calu_operator;
