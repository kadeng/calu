-- calu schema, version 31: a session records in which process table it runs.
--
-- `calu sessions` closes the open escalations of a silent session whose process is gone
-- (`calu.herd.close_gone`). It looked the pid up for every session with this machine's host
-- name. Two machines or containers with the same host name have different process tables, so
-- a listing on one could cancel the open escalation of a silent session of the other.
--
-- `sessions.machine` holds what `calu.machine` reads at the start: machine id, boot id and pid
-- namespace, joined with "|". NULL for a session of a calu that recorded none; the listing
-- then judges by the host name, as before.

ALTER TABLE sessions ADD COLUMN machine text;

COMMENT ON COLUMN sessions.machine IS
    'machine id|boot id|pid namespace of the calu process (calu.machine); NULL if not recorded';

-- The start records it with the row. The last argument is optional, so that a calu of the
-- version before still starts its sessions.
DROP FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text, text);

CREATE FUNCTION calu_start_session(
    project_id bigint,
    host_id bigint,
    os_user text,
    directory text,
    git_commit text,
    pid integer,
    model text,
    key_sha256 text,
    machine text DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    new_id uuid;
BEGIN
    IF calu_start_session.key_sha256 IS NULL
       OR calu_start_session.key_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'key_sha256 must be the lowercase hex SHA-256 of the session key';
    END IF;
    INSERT INTO sessions (project_id, host_id, os_user, directory, git_commit, pid, model,
                          started_by, machine)
    VALUES (calu_start_session.project_id, calu_start_session.host_id,
            calu_start_session.os_user, calu_start_session.directory,
            calu_start_session.git_commit, calu_start_session.pid, calu_start_session.model,
            session_user, NULLIF(calu_start_session.machine, ''))
    RETURNING id INTO new_id;
    INSERT INTO session_keys (session_id, key_sha256)
    VALUES (new_id, calu_start_session.key_sha256);
    RETURN new_id;
END
$$;

REVOKE ALL
    ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text, text, text)
    FROM PUBLIC;
GRANT EXECUTE
    ON FUNCTION calu_start_session(bigint, bigint, text, text, text, integer, text, text, text)
    TO calu_session, calu_operator;
