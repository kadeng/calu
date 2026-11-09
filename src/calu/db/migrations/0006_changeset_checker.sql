-- calu schema, version 6: the changeset checker (PLAN.md D26).
--
-- It reviews the whole change that a `git commit` would record, before the commit happens, and
-- is therefore a route of its own: the step checkers judge one edit with limited context, this
-- one judges the result.

ALTER TABLE checker_types DROP CONSTRAINT checker_types_route_check;
ALTER TABLE checker_types ADD CONSTRAINT checker_types_route_check
    CHECK (route IN ('command', 'edit', 'subagent', 'narrator', 'changeset'));
