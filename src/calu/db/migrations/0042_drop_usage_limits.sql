-- calu schema, version 42: the stored usage limits of migration 0041 are removed again
-- (agent/records/PLAN.md D250, the owner's decision of 2026-11-20: keep it simple).
--
-- The usage panel asks Claude Code on demand (`get_usage`) and otherwise shows the limits
-- Claude Code reported to the running session process; nothing about the Claude account is
-- read or stored. Migration 0041 stays in place, because databases have applied it; this one
-- drops what it created. `IF EXISTS` keeps it harmless on any schema.

DROP FUNCTION IF EXISTS calu_usage_limits(text);
DROP FUNCTION IF EXISTS calu_note_usage_limit(text, text, text, double precision, timestamptz,
                                              timestamptz);
DROP FUNCTION IF EXISTS calu_usage_account(uuid, text);
DROP TABLE IF EXISTS usage_limits;
