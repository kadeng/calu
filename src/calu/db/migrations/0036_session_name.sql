-- calu schema, version 36: a name for a session.
--
-- A person who watches several sessions tells them apart by a name of their own choosing
-- ("payment refactoring") more easily than by a directory or an id. `sessions.name` holds it:
-- free text, at most 80 characters, '' for none.
--
--   calu_operator, calu_admin  may set the name (`calu.herd.rename_session`)
--   calu_session               may not: it keeps UPDATE only on the columns migration 0035
--                              names, and the name is not one of them
--
-- The operator could so far only read `sessions` (grant of 0001, policy `operator_reads` of
-- 0015). It gets UPDATE on this one column and a row policy for updates; its column grant
-- keeps every other column of the row out of its reach.
--
-- Every statement can run again without harm.

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS name text NOT NULL DEFAULT '';

ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_name_length;
ALTER TABLE sessions ADD CONSTRAINT sessions_name_length CHECK (char_length(name) <= 80);

COMMENT ON COLUMN sessions.name IS
    'A name a person gave the session, at most 80 characters; '''' for none';

GRANT UPDATE (name) ON sessions TO calu_operator;

DROP POLICY IF EXISTS operator_renames ON sessions;
CREATE POLICY operator_renames ON sessions FOR UPDATE TO calu_operator
    USING (true) WITH CHECK (true);
