-- calu schema, version 43: one login for the person who uses calu (agent/records/PLAN.md D265,
-- the owner's decision of 2026-11-24).
--
-- calu had three logins for one person: `calu_session` (a running session), `calu_operator`
-- (the fleet and escalations) and `calu_admin` (settings). From this version on every calu
-- process of that person logs in as `calu_admin`, "the user login", whose password is the
-- calu user password; the owner's login stays for migrations.
--
--   calu_owner     owns the schema: migrations, roles            (the `db-admin` entry)
--   calu_admin     everything a person and their sessions do      (the `user` entry)
--   calu_operator  no login; a group: `calu_caller_is_operator()` asks for its membership
--   calu_session   no login; its grants and policies stay, unused
--
-- The two roles stay, with their grants and row-level security policies, so that the earlier
-- migrations stay valid. They lose their logins and passwords: a stored `session` or
-- `operator` entry stops working, and `calu setup` replaces both by the `user` entry. A
-- session that is connected as `calu_session` while this runs keeps its connection until it
-- ends; it cannot reconnect.

ALTER ROLE calu_session NOLOGIN PASSWORD NULL;
ALTER ROLE calu_operator NOLOGIN PASSWORD NULL;
