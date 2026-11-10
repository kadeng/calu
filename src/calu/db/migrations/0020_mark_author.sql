-- Which checker opened an honesty mark (PLAN.md D87).
--
-- A route carries several checkers (D81), and honesty, security and over-engineering are
-- plausibly separate ones, so "12 marks are open" is not an answer to "is this checker any
-- good". With the author recorded, `calu checker stats` can say per checker how many marks it
-- opened, how many it cleared again itself, and how many a person had to override -- which is
-- the honest measure of whether it can be trusted to block, in the same spirit as the
-- approved-then-denied count of D61.
--
-- Existing rows keep NULL: they were written before this column and the author is not
-- recoverable. They are reported as "unattributed" rather than guessed.

ALTER TABLE honesty_marks ADD COLUMN opened_by text;

COMMENT ON COLUMN honesty_marks.opened_by IS
    'Name of the checker whose verdict opened this mark (NULL for rows written before '
    'migration 0020). `closed_by` says who closed it: a checker or a person.';
