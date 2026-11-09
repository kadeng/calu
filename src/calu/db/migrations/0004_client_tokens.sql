-- calu schema, version 4: the tokens with which clients authenticate to a session process.
--
-- `calu run` issues one token per session, prints it once and stores only its SHA-256 here
-- (PLAN.md §5), so that an operator can see which sessions accept clients and revoke a token.
-- The session process compares the hash of the token a client sends; the token itself is never
-- stored anywhere.

CREATE TABLE client_tokens (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    token_sha256 text NOT NULL,
    label        text NOT NULL DEFAULT '',
    address      text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked      boolean NOT NULL DEFAULT false,
    UNIQUE (session_id, token_sha256)
);

GRANT SELECT, INSERT, UPDATE ON client_tokens TO calu_session;
GRANT SELECT, UPDATE ON client_tokens TO calu_operator;
