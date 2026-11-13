-- calu schema, version 34: a fork records which conversation it was forked from.
--
-- A fork runs under a Claude session id of its own, so nothing said that the session
-- continues a copy of another conversation: its page showed turns it never ran, without a
-- word about where they come from, and the conversation it left gave no way to find it.
--
-- `forked_from` is the Claude session id of the conversation that was copied.
-- `forked_before_turn` is the first turn the fork dropped; NULL when it kept the whole
-- conversation. Both are NULL for a session that is no fork.

ALTER TABLE sessions ADD COLUMN forked_from text;
ALTER TABLE sessions ADD COLUMN forked_before_turn integer CHECK (forked_before_turn >= 2);

COMMENT ON COLUMN sessions.forked_from IS
    'Claude session id of the conversation this session is a fork of; NULL if it is none';
COMMENT ON COLUMN sessions.forked_before_turn IS
    'the first turn of that conversation the fork dropped; NULL if it kept all of it';
