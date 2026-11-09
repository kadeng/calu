-- calu schema, version 1 (PLAN.md §7).
--
-- Roles (cluster-wide, created here without login; `calu setup` gives calu_session a login
-- password):
--   owner of the database      configuration, checker definitions, rules, migrations
--   calu_session               one running session: writes its rows, reads configuration
--   calu_operator              herd management: reads everything, resolves escalations
--
-- Notifications (pg_notify channels):
--   escalation_resolved   payload {"id": ..., "session_id": ..., "state": ...}
--   config_changed        payload {"table": ...}

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_session') THEN
        CREATE ROLE calu_session NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_operator') THEN
        CREATE ROLE calu_operator NOLOGIN;
    END IF;
END
$$;

CREATE TABLE hosts (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hostname    text NOT NULL UNIQUE,
    os          text NOT NULL DEFAULT '',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    git_remote      text,
    network_policy  text NOT NULL DEFAULT 'full' CHECK (network_policy IN ('none', 'proxy', 'full')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id         bigint REFERENCES projects (id),
    host_id            bigint NOT NULL REFERENCES hosts (id),
    os_user            text NOT NULL,
    directory          text NOT NULL,
    git_commit         text,
    pid                integer NOT NULL,
    claude_session_id  text,
    model              text,
    state              text NOT NULL DEFAULT 'starting' CHECK (state IN (
                           'starting', 'idle', 'running', 'checking', 'tool_call',
                           'escalated', 'stopped', 'failed')),
    state_since        timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz NOT NULL DEFAULT now(),
    ended_at           timestamptz,
    started_by         text NOT NULL DEFAULT current_user
);
CREATE INDEX sessions_active ON sessions (state) WHERE ended_at IS NULL;

CREATE TABLE turns (
    session_id       uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    number           integer NOT NULL,
    user_inputs      jsonb NOT NULL DEFAULT '[]',
    narrated_answer  text,
    folded           boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, number)
);

CREATE TABLE steps (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    turn            integer,
    seq             integer NOT NULL,
    agent_id        text,
    tool_use_id     text,
    tool_name       text NOT NULL,
    tool_input      jsonb NOT NULL,
    git_commit      text,
    rules_outcome   text,
    decision        text CHECK (decision IN ('allow', 'deny', 'escalate')),
    reason          text,
    summary         text,
    narration       text,
    result_summary  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz,
    finished_at     timestamptz,
    UNIQUE (session_id, seq)
);

CREATE TABLE checker_types (
    name                 text PRIMARY KEY,
    route                text NOT NULL CHECK (route IN ('command', 'edit', 'subagent', 'narrator')),
    instructions         text NOT NULL,
    model                text NOT NULL,
    request_limit        integer NOT NULL DEFAULT 3,
    output_tokens_limit  integer NOT NULL DEFAULT 8000,
    max_tokens           integer NOT NULL DEFAULT 4000,
    enabled              boolean NOT NULL DEFAULT true,
    version              integer NOT NULL DEFAULT 1,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE checker_runs (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    step_id            bigint NOT NULL REFERENCES steps (id) ON DELETE CASCADE,
    checker_name       text NOT NULL,
    checker_version    integer NOT NULL,
    model              text NOT NULL,
    decision           text NOT NULL CHECK (decision IN ('allow', 'deny', 'escalate')),
    reason             text,
    input_tokens       integer,
    output_tokens      integer,
    cache_read_tokens  integer,
    duration_ms        integer,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE escalations (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    step_id      bigint REFERENCES steps (id) ON DELETE CASCADE,
    reason       text NOT NULL,
    context      jsonb NOT NULL DEFAULT '{}',
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'allowed', 'denied', 'cancelled')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_by  text,
    resolved_at  timestamptz,
    note         text
);
CREATE INDEX escalations_open ON escalations (session_id) WHERE state = 'open';

CREATE TABLE rules (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  bigint REFERENCES projects (id) ON DELETE CASCADE,
    list        text NOT NULL CHECK (list IN ('command-allow', 'command-deny', 'read-allow',
                                              'read-deny', 'write-allow', 'write-deny')),
    pattern     text NOT NULL,
    comment     text NOT NULL DEFAULT '',
    position    integer NOT NULL DEFAULT 0,
    enabled     boolean NOT NULL DEFAULT true
);
CREATE INDEX rules_by_list ON rules (list, project_id, position);

CREATE TABLE config (
    scope     text NOT NULL CHECK (scope IN ('global', 'project', 'session')),
    scope_id  text NOT NULL DEFAULT '',
    key       text NOT NULL,
    value     jsonb NOT NULL,
    PRIMARY KEY (scope, scope_id, key)
);

CREATE TABLE chat_summaries (
    session_id     uuid PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
    summary        text NOT NULL,
    covered_turns  integer NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL DEFAULT current_user,
    table_name  text NOT NULL,
    action      text NOT NULL,
    old_row     jsonb,
    new_row     jsonb
);

-- Every change of configuration, rules and checker definitions is logged and announced.
CREATE FUNCTION calu_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit_log (table_name, action, old_row, new_row)
    VALUES (TG_TABLE_NAME, TG_OP,
            CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
            CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END);
    PERFORM pg_notify('config_changed', json_build_object('table', TG_TABLE_NAME)::text);
    RETURN NULL;
END
$$;

CREATE TRIGGER config_audit AFTER INSERT OR UPDATE OR DELETE ON config
    FOR EACH ROW EXECUTE FUNCTION calu_audit();
CREATE TRIGGER rules_audit AFTER INSERT OR UPDATE OR DELETE ON rules
    FOR EACH ROW EXECUTE FUNCTION calu_audit();
CREATE TRIGGER checker_types_audit AFTER INSERT OR UPDATE OR DELETE ON checker_types
    FOR EACH ROW EXECUTE FUNCTION calu_audit();

-- A changed checker definition gets a new version number.
CREATE FUNCTION calu_checker_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.instructions, NEW.model, NEW.request_limit, NEW.output_tokens_limit, NEW.max_tokens,
        NEW.route, NEW.enabled)
       IS DISTINCT FROM
       (OLD.instructions, OLD.model, OLD.request_limit, OLD.output_tokens_limit, OLD.max_tokens,
        OLD.route, OLD.enabled) THEN
        NEW.version := OLD.version + 1;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER checker_types_version BEFORE UPDATE ON checker_types
    FOR EACH ROW EXECUTE FUNCTION calu_checker_version();

-- Escalations are resolved only through these functions, which also notify the waiting
-- session. calu_session may cancel its own open escalations but never allow or deny them.
CREATE FUNCTION calu_resolve_escalation(escalation_id bigint, decision text, resolution_note text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    row escalations%ROWTYPE;
BEGIN
    IF decision NOT IN ('allowed', 'denied') THEN
        RAISE EXCEPTION 'decision must be allowed or denied, not %', decision;
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

CREATE FUNCTION calu_cancel_escalation(escalation_id bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE escalations
       SET state = 'cancelled', resolved_by = session_user, resolved_at = now()
     WHERE id = escalation_id AND state = 'open';
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION calu_resolve_escalation(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION calu_cancel_escalation(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_resolve_escalation(bigint, text, text) TO calu_operator;
GRANT EXECUTE ON FUNCTION calu_cancel_escalation(bigint) TO calu_session, calu_operator;

-- calu_session: reads configuration, writes the history of its sessions.
GRANT SELECT ON projects, checker_types, rules, config TO calu_session;
GRANT SELECT, INSERT, UPDATE ON hosts, sessions, turns, steps, chat_summaries TO calu_session;
GRANT SELECT, INSERT ON checker_runs, escalations TO calu_session;

-- calu_operator: reads every table and resolves escalations through the function above.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO calu_operator;
