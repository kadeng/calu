-- The honesty register lives in the session's memory (PLAN.md D180, the owner's answer to Q19).
--
-- Dirty and clean are transient states of the code a session is working on: they are kept in
-- memory and persisted nowhere -- certainly not in a table as easy to change as this one. A
-- session therefore starts with an empty register, and the marks of earlier sessions go with
-- this table. The branch (D82), the reach (D141, migration 0023), the author (0020) and the
-- file snapshots (0021) were columns of it and go too.

DROP TABLE IF EXISTS honesty_marks;
