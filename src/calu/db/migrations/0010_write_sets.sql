-- What a Bash step actually wrote, when the optional snapshot of PLAN.md D31 is on.
--
-- `Edit` and `Write` reach the tool gate with their content, so their effect is known before
-- they run. A command does not: `sed -i`, a formatter or a script can write anything, and the
-- command checker only describes what it expects. With `changes.snapshot` on, calu snapshots the
-- project directory before and after every Bash step and records the result here: the two git
-- tree ids (in calu's own git directory, outside the project) and the paths that changed, with
-- the ones the checker's summary did not mention.

ALTER TABLE steps ADD COLUMN snapshot_before text;
ALTER TABLE steps ADD COLUMN snapshot_after  text;
ALTER TABLE steps ADD COLUMN wrote           jsonb;

COMMENT ON COLUMN steps.snapshot_before IS
    'Git tree of the project directory before the step (calu''s own snapshot store, D31).';
COMMENT ON COLUMN steps.snapshot_after IS
    'Git tree of the project directory after the step.';
COMMENT ON COLUMN steps.wrote IS
    'What the step wrote: {"paths": [...], "undeclared": [...], "overlapping": bool}.';
