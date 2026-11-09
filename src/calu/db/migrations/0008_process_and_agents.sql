-- calu schema, version 8: how to reach a session process, and the agents inside a session.
--
-- Many calu processes wrap many Claude sessions, and each session runs subagents, so the
-- database has to answer two questions it could not answer before (PLAN.md D29):
--
--   "where does this session listen?"   sessions.address, next to the pid, host and OS user
--                                       that were already there
--   "which agent started which?"        the agents table, with parent_agent_id
--
-- The main agent of a session is not a row: a NULL `agent_id` means the main agent, in `steps`
-- as well. A subagent whose `parent_agent_id` is NULL was started by the main agent.

ALTER TABLE sessions ADD COLUMN address text;
COMMENT ON COLUMN sessions.address IS
    'Where the session process listens for clients: "ws://127.0.0.1:<port>" or a Unix socket '
    'path. NULL while the session serves no clients.';

CREATE TABLE agents (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id       uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    agent_id         text NOT NULL,
    parent_agent_id  text,
    sdk_agent_id     text,
    description      text NOT NULL DEFAULT '',
    subagent_type    text,
    background       boolean NOT NULL DEFAULT false,
    status           text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'completed', 'failed', 'stopped')),
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    UNIQUE (session_id, agent_id)
);
COMMENT ON COLUMN agents.agent_id IS
    'Id of the Agent tool call that started this subagent; `steps.agent_id` refers to it.';
COMMENT ON COLUMN agents.parent_agent_id IS
    'The agent that started this one; NULL means the session''s main agent.';
CREATE INDEX agents_by_parent ON agents (session_id, parent_agent_id);

GRANT SELECT, INSERT, UPDATE ON agents TO calu_session;
GRANT SELECT ON agents TO calu_operator;
