-- The honesty register: which code says something other than what it does (PLAN.md D82, §13).
--
-- A complex change makes a codebase temporarily dishonest on purpose -- a signature changes in
-- one step and its callers and documentation follow in the next five -- so a checker records the
-- spot instead of objecting to every step, and clears the record when the code and what it says
-- about itself agree again. calu stops dirty code from being **run** while a mark is open (D83);
-- editing it is never blocked, because fixing the dishonesty is an edit.
--
-- The marks belong to the project, not to the session: honesty debt outlives the session that
-- incurred it, and a resumed or second session has to see it. Rows are never deleted, so the
-- table is also the history of what was dishonest and for how long.

CREATE TABLE honesty_marks (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      bigint REFERENCES projects (id) ON DELETE CASCADE,
    path            text NOT NULL,
    symbol          text NOT NULL DEFAULT '',
    reason          text NOT NULL,
    -- The branch the mark was made on: a mark means something else after a branch switch, and
    -- Q19 asks the owner which rule to apply. Recording it keeps that open without a migration.
    git_branch      text,
    opened_session  uuid REFERENCES sessions (id) ON DELETE SET NULL,
    opened_step     bigint REFERENCES steps (id) ON DELETE SET NULL,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_session  uuid REFERENCES sessions (id) ON DELETE SET NULL,
    closed_step     bigint REFERENCES steps (id) ON DELETE SET NULL,
    closed_at       timestamptz,
    -- Why it was closed: by a checker that found the code honest again, or by a person.
    closed_by       text
);

-- One open mark per spot and project: a second "it is still dirty" updates the reason of the
-- open one instead of piling up rows.
-- NULLS NOT DISTINCT, so that the marks of a session without a project (project_id IS NULL)
-- are unique per spot as well; without it two NULL project ids count as different.
CREATE UNIQUE INDEX honesty_marks_open
    ON honesty_marks (project_id, path, symbol) NULLS NOT DISTINCT
 WHERE closed_at IS NULL;

CREATE INDEX honesty_marks_project ON honesty_marks (project_id) WHERE closed_at IS NULL;

COMMENT ON TABLE honesty_marks IS
    'Code that says something other than what it does, per project (PLAN.md D82). An open row '
    '(closed_at IS NULL) blocks steps that would run that code when honesty.mode is enforce.';

-- A session writes its own marks and reads the project's; an operator reads everything and may
-- close a mark by hand (`calu honesty clear`), which is the override when a checker is wrong.
GRANT SELECT, INSERT, UPDATE ON honesty_marks TO calu_session;
GRANT USAGE, SELECT ON SEQUENCE honesty_marks_id_seq TO calu_session;
GRANT SELECT, UPDATE ON honesty_marks TO calu_operator;

ALTER TABLE honesty_marks ENABLE ROW LEVEL SECURITY;

-- Readable for the project, writable by the session that is running: the marks are shared
-- state of a project, unlike steps and turns, which belong to one session (D49).
CREATE POLICY session_project ON honesty_marks TO calu_session
    USING (true)
    WITH CHECK (opened_session = current_setting('calu.session_id', true)::uuid
                OR closed_session = current_setting('calu.session_id', true)::uuid);

CREATE POLICY operator_all ON honesty_marks TO calu_operator USING (true) WITH CHECK (true);
