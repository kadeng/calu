-- calu schema, version 5: the fourth decision, PASS (PLAN.md D21).
--
-- A checker now answers PASS when it has no objection and Claude Code's own auto mode may
-- decide; ALLOW and DENY are deliberate exceptions. Earlier rows used "allow" for both cases.

ALTER TABLE steps DROP CONSTRAINT steps_decision_check;
ALTER TABLE steps ADD CONSTRAINT steps_decision_check
    CHECK (decision IN ('pass', 'allow', 'deny', 'escalate'));

ALTER TABLE checker_runs DROP CONSTRAINT checker_runs_decision_check;
ALTER TABLE checker_runs ADD CONSTRAINT checker_runs_decision_check
    CHECK (decision IN ('pass', 'allow', 'deny', 'escalate'));
