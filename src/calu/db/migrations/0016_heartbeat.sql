-- calu schema, version 16: a session says it is still there (PLAN.md D56).
--
-- A herd is watched through the database (REQUIREMENTS 13), and until now the database could
-- not tell a working session from a dead one. A process that is killed -- OOM, `kill -9`, a
-- lost machine -- leaves its row at `state = 'running'` with `ended_at IS NULL` for ever, so
-- `calu sessions` and the fleet page list it as active, and an escalation of that session waits
-- for an answer that will never reach anybody.
--
-- `last_seen` is written by the session itself every `clock.check_interval_s` seconds (the
-- interval that already exists for the clock watch). A reader compares it with `now()`: a row
-- that has not been seen for several intervals belongs to a process that is gone, which
-- `herd.SessionRow.stale` and `calu sessions` report. Nothing is deleted or changed
-- automatically, because a stale row is evidence: it holds the session's steps, its
-- escalations and its conversation.
--
-- Existing rows start out as "seen now", which is the only honest default: calu cannot know
-- when a session from before this migration was last alive.

ALTER TABLE sessions ADD COLUMN last_seen timestamptz NOT NULL DEFAULT now();

COMMENT ON COLUMN sessions.last_seen IS
    'When the session process last said it was running (PLAN.md D56).';
