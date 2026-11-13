-- calu schema, version 33: the role calu_admin, for changing settings with a typed password.
--
-- What steers the checkers (configuration, checker instructions, rules, trust) could so far
-- be changed only with the owner's login, whose generated password is kept in a credential
-- store. Where calu runs privileged, that store is root's (`/etc/calu`), and a client of the
-- unprivileged user must still be able to change settings (the owner's ruling of 2026-11-13).
--
-- `calu_admin` is a login whose password a person knows: `calu setup` sets it to the admin
-- password. A client connects as `calu_admin` with the password its user types and stores
-- nothing. The role can read and write every table and call every function; it does not own
-- them, so it cannot change the schema, create roles or bypass the audit triggers.
--
--   calu_owner     owns the schema: migrations, roles           (stored login, root's)
--   calu_admin     changes settings, reads everything            (typed password)
--   calu_operator  reads everything, resolves escalations        (stored login, the user's)
--   calu_session   one running session                           (stored login, root's)

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_admin') THEN
        CREATE ROLE calu_admin NOLOGIN;
    END IF;
END
$$;

-- An admin may do what an operator may; `calu_caller_is_operator()` asks for this membership.
GRANT calu_operator TO calu_admin;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO calu_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO calu_admin;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO calu_admin;

-- For what later migrations create.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO calu_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO calu_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO calu_admin;

-- Row-level security: the owner passes it as the owner; calu_admin needs a policy on each
-- table that has it switched on. A later migration that switches it on for a new table adds
-- `admin_all` there (tests/db/test_schema.py checks that none is missing).
DO $$
DECLARE
    name text;
BEGIN
    FOR name IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
    LOOP
        EXECUTE format(
            'CREATE POLICY admin_all ON %I TO calu_admin USING (true) WITH CHECK (true)', name);
    END LOOP;
END
$$;
