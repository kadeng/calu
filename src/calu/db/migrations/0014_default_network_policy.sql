-- calu schema, version 14: a new project reaches package registries and git, not the whole
-- internet (PLAN.md D45, proposed -- Q5 asks the owner to confirm it).
--
-- `projects.network_policy` defaulted to 'full', which means every domain through the sandbox's
-- proxy: the same reach a command has without calu. 'proxy' plus the starter allow list that
-- `calu project add` writes (`calu.config.DEFAULT_ALLOWED_DOMAINS`) lets a session install
-- packages and use git over HTTPS and reach nothing else.
--
-- Only rows inserted **after** this migration are affected, and only those that do not name a
-- policy. Existing projects keep what they have; `calu project network --name <p> --network
-- <policy>` changes one either way.

ALTER TABLE projects ALTER COLUMN network_policy SET DEFAULT 'proxy';
