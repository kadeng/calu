-- The file a mark was opened on, as it was then (PLAN.md D95).
--
-- An overruled mark becomes an evaluation case (D94), and a case about an **edit** can only be
-- reproduced with the file the edit applies to: without it the edit does not apply, the checker
-- sees raw JSON instead of a diff, and the case measures something other than what happened.
-- An escalation already captures the file in its context (`calu.session.files_before`); the
-- register did not, so this column gives it the same thing.
--
-- Only the content at the moment the mark was **opened** is kept: that is the state the checker
-- judged. A later "still dirty" for the same spot leaves it as it is.

ALTER TABLE honesty_marks ADD COLUMN files jsonb NOT NULL DEFAULT '{}';

COMMENT ON COLUMN honesty_marks.files IS
    'Workspace-relative path -> content of the file this mark was opened on, as it was then, '
    'so that an evaluation case built from the mark can be reproduced (PLAN.md D95). Empty '
    'for a mark on a step that is not an edit, or whose file was missing, long or binary.';
