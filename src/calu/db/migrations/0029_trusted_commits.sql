-- calu schema, version 27: trusted commits and trusted file contents (code honesty).
--
-- calu tracks which code was verified to say what it does. Two tables hold what was certified:
--
--     trusted_commits   a commit that a person or a session certified. A commit counts as
--                       trusted if its commit id or its tree id is here. A session's baseline
--                       is the nearest trusted ancestor of HEAD.
--     trusted_files     the SHA-256 of a file content that was verified, one row per content.
--                       A file with that content counts as verified wherever it lies. `path`
--                       is a hint for people and decides nothing.
--
-- Every row says who established the trust (`method`), ordered by strength:
--
--     user    >  review                       >  check
--     a person   calu's honesty review           calu's step checkers (an edit they judged
--                                                without leaving a mark)
--
-- A certification of something that has a row replaces the row only if its method is
-- stronger; an equal or weaker one changes nothing. The setting `honesty.sufficient_trust`
-- decides, when a session reads the rows, which methods count; nothing is deleted for it.
--
-- Both tables are global: a commit id identifies its repository, a hash its content.
--
--     who                         reads   adds                              removes
--     calu_session (a session)    yes     calu_certify_commit, calu_trust_file   no
--     calu_operator, the owner    yes     the same functions,               calu_untrust_commit,
--                                         method 'user' only                calu_untrust_file,
--                                                                           calu_prune_trust
--
-- No role but the owner has INSERT, UPDATE or DELETE on the tables. A caller of the role
-- `calu_session` must have proven which session it is (migration 0025); the row records that
-- session. Every addition, replacement and removal is written to `audit_log`. A change of
-- `trusted_commits`, a row of the method 'user' in `trusted_files` and every removal are
-- announced on the channel `trust_changed`, so a running session can read the tables again.
-- A session's own 'check' and 'review' rows of `trusted_files` are not announced: one is
-- written per verified edit, and the session that wrote it holds the content in memory.

CREATE FUNCTION calu_trust_rank(p_method text) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_method WHEN 'user' THEN 3 WHEN 'review' THEN 2 WHEN 'check' THEN 1 END
$$;

COMMENT ON FUNCTION calu_trust_rank(text) IS
    'Strength of a trust method: user 3, review 2, check 1, anything else NULL (0029).';

CREATE TABLE trusted_commits (
    commit_id     text PRIMARY KEY CHECK (commit_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    tree_id       text NOT NULL CHECK (tree_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    certified_at  timestamptz NOT NULL DEFAULT now(),
    certified_by  text NOT NULL CHECK (certified_by <> ''),
    method        text NOT NULL CHECK (method IN ('user', 'review', 'check')),
    note          text,
    session_id    uuid REFERENCES sessions (id) ON DELETE SET NULL,
    recorded_as   text NOT NULL DEFAULT session_user
);

CREATE INDEX trusted_commits_tree ON trusted_commits (tree_id);

COMMENT ON TABLE trusted_commits IS
    'Commits certified as honest. method: user (a person, certified_by names the client or OS '
    'user), review or check (a session certified a commit made in a clean state; the weakest '
    'source among the verifications of its changes). session_id: the session that recorded '
    'it, if one did. recorded_as: the database login (0029).';

CREATE TABLE trusted_files (
    content_sha256  text PRIMARY KEY CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    path            text NOT NULL CHECK (path <> ''),
    certified_at    timestamptz NOT NULL DEFAULT now(),
    certified_by    text NOT NULL CHECK (certified_by <> ''),
    method          text NOT NULL CHECK (method IN ('user', 'review', 'check')),
    note            text,
    session_id      uuid REFERENCES sessions (id) ON DELETE SET NULL,
    recorded_as     text NOT NULL DEFAULT session_user
);

CREATE INDEX trusted_files_age ON trusted_files (certified_at) WHERE method <> 'user';

COMMENT ON TABLE trusted_files IS
    'File contents verified as honest, by SHA-256, one row per content. method: user (a '
    'person), review (an honesty review), check (an edit the step checkers judged). path is a '
    'hint for people (0029).';

REVOKE ALL ON trusted_commits, trusted_files FROM PUBLIC;
GRANT SELECT ON trusted_commits, trusted_files TO calu_session, calu_operator;

-- Records a certification. Returns true if the commit was recorded or its row was replaced by
-- this stronger certification; false if a row of the same or a stronger method exists, which
-- is then left as it is.
CREATE FUNCTION calu_certify_commit(
    p_commit text, p_tree text, p_method text, p_by text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_commits;
    after      trusted_commits;
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

COMMENT ON FUNCTION calu_certify_commit(text, text, text, text, text) IS
    'Records a commit as trusted, or replaces a weaker certification of it; the only way a '
    'session adds to trusted_commits (0029).';

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
    'Removes a trusted commit; operators and the owner only (0029).';

-- Records a verified file content. Returns true if the hash was recorded or its row was
-- replaced by this stronger verification; false if a row of the same or a stronger method
-- exists, which is then left as it is.
CREATE FUNCTION calu_trust_file(
    p_sha256 text, p_path text, p_method text, p_by text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    of_session uuid := calu_current_session();
    before     trusted_files;
    after      trusted_files;
BEGIN
    IF calu_caller_is_operator() THEN
        IF p_method IS DISTINCT FROM 'user' THEN
            RAISE EXCEPTION 'an operator certifies with the method ''user'' only'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        of_session := NULL;
    ELSIF of_session IS NULL THEN
        RAISE EXCEPTION 'only a session that has proven its identity records a trusted file'
            USING ERRCODE = 'insufficient_privilege';
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

COMMENT ON FUNCTION calu_trust_file(text, text, text, text, text) IS
    'Records a file content as trusted, or replaces a weaker verification of it; the only way '
    'a session adds to trusted_files (0029).';

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
    'Removes trusted file contents by hash or path; operators and the owner only (0029).';

-- Removes the rows of the methods 'check' and 'review' that are older than p_days days, from
-- both tables; rows of the method 'user' are never removed. Operators and the owner only.
-- Returns the number of rows removed. One audit row per table says how many went.
CREATE FUNCTION calu_prune_trust(p_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    before_time timestamptz;
    commits     integer;
    files       integer;
BEGIN
    IF NOT calu_caller_is_operator() THEN
        RAISE EXCEPTION 'only an operator prunes stored trust'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_days IS NULL OR p_days < 0 THEN
        RAISE EXCEPTION 'the age in days must be 0 or more'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    before_time := now() - make_interval(days => p_days);
    WITH gone AS (
        DELETE FROM trusted_commits
         WHERE method IN ('check', 'review') AND certified_at < before_time
        RETURNING 1
    )
    SELECT count(*) INTO commits FROM gone;
    WITH gone AS (
        DELETE FROM trusted_files
         WHERE method IN ('check', 'review') AND certified_at < before_time
        RETURNING 1
    )
    SELECT count(*) INTO files FROM gone;
    IF commits > 0 THEN
        INSERT INTO audit_log (actor, table_name, action, old_row)
        VALUES (session_user, 'trusted_commits', 'DELETE',
                jsonb_build_object('pruned', commits, 'older_than_days', p_days));
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_commits')::text);
    END IF;
    IF files > 0 THEN
        INSERT INTO audit_log (actor, table_name, action, old_row)
        VALUES (session_user, 'trusted_files', 'DELETE',
                jsonb_build_object('pruned', files, 'older_than_days', p_days));
        PERFORM pg_notify('trust_changed', json_build_object('table', 'trusted_files')::text);
    END IF;
    RETURN commits + files;
END
$$;

COMMENT ON FUNCTION calu_prune_trust(integer) IS
    'Removes check and review rows older than a number of days from trusted_commits and '
    'trusted_files, never user rows; operators and the owner only (0029).';

REVOKE ALL ON FUNCTION calu_trust_rank(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_certify_commit(text, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_untrust_commit(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_trust_file(text, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_untrust_file(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_prune_trust(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_trust_rank(text) TO calu_session, calu_operator;
GRANT EXECUTE ON FUNCTION calu_certify_commit(text, text, text, text, text)
    TO calu_session, calu_operator;
GRANT EXECUTE ON FUNCTION calu_trust_file(text, text, text, text, text)
    TO calu_session, calu_operator;
GRANT EXECUTE ON FUNCTION calu_untrust_commit(text) TO calu_operator;
GRANT EXECUTE ON FUNCTION calu_untrust_file(text) TO calu_operator;
GRANT EXECUTE ON FUNCTION calu_prune_trust(integer) TO calu_operator;
