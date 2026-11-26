-- Temporary rules for the command, edit and changeset checkers
-- (agent/records/notes/temporary-rules-design.md).
--
-- A rule is a sentence a person wrote; the checkers read it next to their instructions until it
-- expires. Its scope is every session, one project, or one conversation (Claude's session id,
-- so that the rule survives a resume, which creates a new `sessions` row). The duration lies
-- between one minute and seven days, and is one day at most for a rule that lifts calu's
-- built-in protections. Expired and removed rules stay for the record. Every change is audited
-- and announced as `config_changed`, like a setting.

CREATE TABLE public.temporary_rules (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text text NOT NULL,
    scope text NOT NULL,
    project_id bigint REFERENCES public.projects(id) ON DELETE CASCADE,
    conversation text,
    lifts_builtin boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    created_by text NOT NULL,
    origin text NOT NULL DEFAULT 'manual',
    removed_at timestamp with time zone,
    removed_by text,
    CONSTRAINT temporary_rules_text_check
        CHECK (btrim(text) <> '' AND length(text) <= 1000),
    CONSTRAINT temporary_rules_scope_check CHECK (
        (scope = 'global' AND project_id IS NULL AND conversation IS NULL)
        OR (scope = 'project' AND project_id IS NOT NULL AND conversation IS NULL)
        OR (scope = 'session' AND conversation IS NOT NULL AND conversation <> ''
            AND project_id IS NULL)),
    CONSTRAINT temporary_rules_duration_check CHECK (
        expires_at - created_at >= interval '1 minute'
        AND expires_at - created_at
            <= CASE WHEN lifts_builtin THEN interval '1 day' ELSE interval '7 days' END),
    CONSTRAINT temporary_rules_created_by_check CHECK (created_by <> ''),
    CONSTRAINT temporary_rules_removed_check CHECK ((removed_at IS NULL) = (removed_by IS NULL))
);

CREATE INDEX temporary_rules_active ON public.temporary_rules (expires_at)
    WHERE removed_at IS NULL;

CREATE TRIGGER temporary_rules_audit AFTER INSERT OR DELETE OR UPDATE
    ON public.temporary_rules FOR EACH ROW EXECUTE FUNCTION public.calu_audit();

GRANT SELECT, INSERT, UPDATE ON TABLE public.temporary_rules TO calu_admin;
GRANT SELECT ON TABLE public.temporary_rules TO calu_operator;
