-- Raise the checkers' request and token budgets (PLAN.md D72).
--
-- Measured against the built-in evaluation cases with a thinking checker model (Nemotron
-- Lightning on Fireworks, 25 cases, three runs each): with the old budgets, 5 of 18 edit-checker
-- runs ended in "UsageLimitExceeded: the next request would exceed the request_limit of 3" and
-- the obfuscated-command case reproducibly spent its whole `max_tokens` of 4000 on thinking
-- before writing any decision. Both failures escalate the step, so the gate stays closed, but
-- the user is interrupted by a checker that never reached a verdict -- and the instructions are
-- meant to be edited and will grow.
--
-- Why these numbers: one check is a request, up to two tool calls (`read_file`, `grep_files`)
-- and, after an answer that was too long, one retry -- five requests. `output_tokens_limit` has
-- to hold more than one maximal answer, or the retry cannot happen: three times `max_tokens`.
--
-- Raising a budget cannot weaken the gate: a check that runs out escalates either way. The cost
-- only grows for a check that really uses the room.

ALTER TABLE checker_types ALTER COLUMN request_limit SET DEFAULT 5;
ALTER TABLE checker_types ALTER COLUMN output_tokens_limit SET DEFAULT 24000;
ALTER TABLE checker_types ALTER COLUMN max_tokens SET DEFAULT 8000;

-- Only rows that still carry the old defaults: a value somebody chose stays as it is. The
-- trigger on this table records the change and bumps the version, as for any other edit.
UPDATE checker_types
   SET request_limit = 5, output_tokens_limit = 24000, max_tokens = 8000
 WHERE (request_limit, output_tokens_limit, max_tokens) = (3, 8000, 4000);
