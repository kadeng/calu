-- Temporary rules no longer lift calu's built-in rules (agent/records/PLAN.md D291).
--
-- A temporary rule is text for the checkers only. calu's built-in rules are switched off by
-- the settings `built_in_rules.*` instead. The column `lifts_builtin` and the one-day limit
-- that went with it are dropped; every rule lasts between one minute and seven days.

ALTER TABLE public.temporary_rules DROP CONSTRAINT temporary_rules_duration_check;

ALTER TABLE public.temporary_rules DROP COLUMN lifts_builtin;

ALTER TABLE public.temporary_rules ADD CONSTRAINT temporary_rules_duration_check CHECK (
    expires_at - created_at >= interval '1 minute'
    AND expires_at - created_at <= interval '7 days');
