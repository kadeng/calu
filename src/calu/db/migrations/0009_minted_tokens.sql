-- A session accepts every non-revoked client token of its own session, not only the one it
-- printed (PLAN.md D30, part of Q4). That is what makes a stored token practical: an editor's
-- ACP adapter, a local command or a remote interface keeps working when the session restarts,
-- and an operator can revoke one token without touching the others.
--
-- The session role may not read `client_tokens.token_sha256` (migration 0007), so it presents
-- the hash of the token a client sent and the database answers whether that hash belongs to one
-- of this session's non-revoked tokens. The role therefore cannot enumerate the tokens: it can
-- only confirm a hash it was given, and guessing one means guessing 32 random bytes.

CREATE FUNCTION calu_client_token_ok(p_session uuid, p_hash text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_id bigint;
BEGIN
    SELECT id INTO v_id
      FROM client_tokens
     WHERE session_id = p_session AND token_sha256 = p_hash AND NOT revoked;
    IF v_id IS NULL THEN
        RETURN false;
    END IF;
    UPDATE client_tokens SET last_used_at = now() WHERE id = v_id;
    RETURN true;
END;
$$;

COMMENT ON FUNCTION calu_client_token_ok(uuid, text) IS
    'True if the hash belongs to a non-revoked client token of that session; stamps last_used_at.';

REVOKE ALL ON FUNCTION calu_client_token_ok(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION calu_client_token_ok(uuid, text) TO calu_session;

-- The session only inserts the token it printed; the stamp of `last_used_at` now happens inside
-- the function above, which runs as the owner. So the session no longer needs UPDATE, and
-- without it a session cannot un-revoke a token that an operator revoked.
REVOKE UPDATE ON client_tokens FROM calu_session;

-- An operator mints tokens for clients and revokes them again.
GRANT INSERT ON client_tokens TO calu_operator;
