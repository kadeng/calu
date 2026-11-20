-- calu schema, version 41: the last usage limits Claude Code reported, per Claude account
-- (agent/records/PLAN.md D250).
--
-- A session writes each limit when Claude Code's `rate_limit_event` arrives; the usage panel
-- and `usage.get` of a later session of the same account show it with its age. The figures
-- are information: Anthropic's servers enforce the limits.
--
-- The account: Claude Code names its login in the answer to the SDK's `initialize` request
-- (`account.email` and `account.organization`, present for a claude.ai login). The session
-- passes "<email>" or "<email> (<organization>)"; the key becomes "claude:" and that text.
-- Without such a name (an API key, Bedrock, Vertex, or a Claude Code that does not say), the
-- key is "os:<OS user>@<host>" of the calling session, which the database takes from the
-- session's own row, so that a session cannot write the figures of another OS user's key.
--
-- The session login has no right on the table: it writes and reads through the two SECURITY
-- DEFINER functions below, and only as a session that proved its key (`calu_current_session`).
-- Operators read the table (who used how much of which account), admins may change it.

CREATE TABLE usage_limits (
    account      text NOT NULL CHECK (account ~ '^(claude|os):.'),
    limit_type   text NOT NULL,
    status       text NOT NULL,
    utilization  double precision,
    resets_at    timestamptz,
    reported_at  timestamptz NOT NULL,
    session_id   uuid REFERENCES sessions (id) ON DELETE SET NULL,
    PRIMARY KEY (account, limit_type)
);

COMMENT ON TABLE usage_limits IS
    'Newest state of each usage limit Claude Code reported, per Claude account (D250)';
COMMENT ON COLUMN usage_limits.account IS
    '"claude:<email>[ (<organization>)]" as Claude Code names its login, else "os:<user>@<host>"';
COMMENT ON COLUMN usage_limits.utilization IS 'Used part of the limit, 0.0 to 1.0; NULL if not reported';

REVOKE ALL ON usage_limits FROM PUBLIC;
GRANT SELECT ON usage_limits TO calu_operator;

-- The key of the account a session's figures belong to (see the head of this file). NULL for
-- a session that does not exist.
CREATE FUNCTION calu_usage_account(p_session uuid, p_account text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE
               WHEN coalesce(btrim(p_account), '') <> '' THEN 'claude:' || btrim(p_account)
               ELSE 'os:' || s.os_user || '@' || h.hostname
           END
      FROM sessions s JOIN hosts h ON h.id = s.host_id
     WHERE s.id = p_session;
$$;

REVOKE ALL ON FUNCTION calu_usage_account(uuid, text) FROM PUBLIC;

-- A session stores one limit of its account. A report older than the stored one changes
-- nothing (two sessions of one account may report out of order). Returns the account key, or
-- NULL for a connection that is no proven session (nothing is stored then).
CREATE FUNCTION calu_note_usage_limit(
    p_account text,
    p_type text,
    p_status text,
    p_utilization double precision,
    p_resets_at timestamptz,
    p_reported_at timestamptz
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me  uuid := calu_current_session();
    key text;
BEGIN
    IF me IS NULL THEN
        RETURN NULL;
    END IF;
    key := calu_usage_account(me, p_account);
    INSERT INTO usage_limits AS u
           (account, limit_type, status, utilization, resets_at, reported_at, session_id)
    VALUES (key, coalesce(p_type, ''), coalesce(p_status, ''), p_utilization, p_resets_at,
            p_reported_at, me)
    ON CONFLICT (account, limit_type) DO UPDATE
       SET status = excluded.status, utilization = excluded.utilization,
           resets_at = excluded.resets_at, reported_at = excluded.reported_at,
           session_id = excluded.session_id
     WHERE u.reported_at <= excluded.reported_at;
    RETURN key;
END
$$;

REVOKE ALL ON FUNCTION calu_note_usage_limit(text, text, text, double precision, timestamptz,
                                             timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_note_usage_limit(text, text, text, double precision, timestamptz,
                                                timestamptz) TO calu_session;

-- The stored limits of the calling session's account, newest report first. Nothing for a
-- connection that is no proven session.
CREATE FUNCTION calu_usage_limits(p_account text)
RETURNS TABLE (account text, limit_type text, status text, utilization double precision,
               resets_at timestamptz, reported_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT u.account, u.limit_type, u.status, u.utilization, u.resets_at, u.reported_at
      FROM usage_limits u
     WHERE calu_current_session() IS NOT NULL
       AND u.account = calu_usage_account(calu_current_session(), p_account)
     ORDER BY u.reported_at DESC, u.limit_type;
$$;

REVOKE ALL ON FUNCTION calu_usage_limits(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_usage_limits(text) TO calu_session;
