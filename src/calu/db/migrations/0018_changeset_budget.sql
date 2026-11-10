-- The changeset checker gets more requests than a step checker (PLAN.md D75).
--
-- It reviews a whole commit, and its instructions ask it to use its read-only tools: "A few
-- calls are expected here, because the whole change is being judged". Measured on the new
-- changeset cases with a reasoning model: 3 of 12 runs ended in "UsageLimitExceeded: the next
-- request would exceed the request_limit of 5" -- the diff, two or three file reads and the
-- answer do not fit in five requests. Every other run decided correctly, so the budget was the
-- only thing in the way.
--
-- Eight covers the request, up to five tool calls and one retry. As in 0017, only a row that
-- still carries the previous default is lifted.

UPDATE checker_types
   SET request_limit = 8
 WHERE name = 'changeset' AND request_limit = 5;
