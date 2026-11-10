-- Ending a session revokes its own client tokens (PLAN.md D108). The session role must not get
-- UPDATE on `client_tokens` back: migration 0009 took it away so that a session cannot
-- **un**-revoke a token an operator revoked, and migration 0007 took SELECT away so that the
-- token hash is a capability the process holds rather than a value its role can read. Both stay
-- as they are, and the revocation goes through a function that can only move a token in the one
-- safe direction.
--
-- What the session role can do with it: mark every token of a session id revoked. What it still
-- cannot do: un-revoke anything, read a hash, or change a label. Passing another session's id
-- revokes that session's tokens, which is the same direction and no worse than what a session
-- can already do to its own -- the clients then re-attach and mint a new token (D105).
CREATE FUNCTION calu_revoke_session_tokens(p_session uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    revoked_rows integer;
BEGIN
    UPDATE client_tokens SET revoked = true
     WHERE session_id = p_session AND NOT revoked;
    GET DIAGNOSTICS revoked_rows = ROW_COUNT;
    RETURN revoked_rows;
END
$$;

REVOKE ALL ON FUNCTION calu_revoke_session_tokens(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_revoke_session_tokens(uuid) TO calu_session, calu_operator;
