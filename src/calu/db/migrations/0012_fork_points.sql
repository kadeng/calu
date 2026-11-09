-- Where a session can be forked (PLAN.md D36, measured in spikes/RESULTS.md).
--
-- The Agent SDK forks a conversation at an earlier point with `resume_session_at` (the last
-- transcript entry of the turn to keep) and `resume_drops_turn` (the prompt of the turn to
-- discard, which the CLI validates against). Claude Code replays both uuids to calu while the
-- session runs, so they are recorded per turn here rather than parsed out of the transcript
-- later: calu's own injected messages are prompts too, so counting prompts afterwards would
-- pick the wrong one — which is exactly what the CLI's guard refuses.

ALTER TABLE turns ADD COLUMN prompt_uuid     text;
ALTER TABLE turns ADD COLUMN last_entry_uuid text;

COMMENT ON COLUMN turns.prompt_uuid IS
    'Transcript uuid of the prompt that started this turn; `resume_drops_turn` of a fork that drops it.';
COMMENT ON COLUMN turns.last_entry_uuid IS
    'Transcript uuid of the last assistant entry of this turn; `resume_session_at` of a fork that keeps it.';
