"""Client tokens: how a client proves to a session process that it may talk to it (PLAN.md §5).

`calu run` issues one token per session, prints it once on the terminal and stores only its
SHA-256 in the database (`client_tokens`). A client sends the token in `initialize`; the session
process hashes it and compares the hash in constant time. The token itself is never stored, so a
leaked database does not give access to a running session.

This is the local case: the WebSocket listens on 127.0.0.1 by default, and remote access goes
through an SSH tunnel or a TLS proxy. How clients authenticate when the endpoint is reachable
from other machines is still open (PLAN.md Q4).
"""

import hashlib
import secrets

TOKEN_BYTES = 32
"""Length of a new token in random bytes (43 characters in base64url)."""


def new_token() -> str:
    """Create a client token.

    Returns:
        A URL-safe random string of `TOKEN_BYTES` bytes of entropy.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """Return the hash of a token, as it is stored in the database.

    A token has full entropy, so a plain hash is enough: it cannot be guessed, and a password
    hash (Argon2id, as for the database credentials) would only slow every request down.

    Args:
        token: The token.

    Returns:
        Its SHA-256 as lower-case hexadecimal.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def matches(token: str, expected_hash: str) -> bool:
    """Tell whether `token` is the token behind `expected_hash`.

    Args:
        token: The token a client sent.
        expected_hash: The stored hash (`token_hash`).

    Returns:
        True if the hashes are equal; the comparison takes the same time either way.
    """
    return secrets.compare_digest(token_hash(token), expected_hash)
