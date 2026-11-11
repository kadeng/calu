-- Whether a mark names a file other than the ones its step was about (PLAN.md D141, Q20 (c)).
--
-- A checker may mark any existing file of the project (D112), because an edit to one file can
-- be what makes another one lie. The owner's question Q20 is how far that reach should go. Until
-- it is answered, the reach is made visible instead of restricted: the register says when a mark
-- lies outside the files of the step that opened it, so a person reading `calu honesty list`
-- sees which marks are a checker's judgement about code it was not shown being changed.
--
-- NULL when it cannot be said: the step named no file (a Bash command, for example), or the row
-- is older than this column.

ALTER TABLE honesty_marks ADD COLUMN beyond_step boolean;

COMMENT ON COLUMN honesty_marks.beyond_step IS
    'True when the mark names a file the step that opened it did not touch, false when it names '
    'one of that step''s files, NULL when the step named no file (PLAN.md D141).';
