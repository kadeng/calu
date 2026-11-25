"""The password of the web interface where the calu user password is empty (D279).

With a calu user password, a browser signs in with it (the database checks it). Without one
(`calu setup --no-password`), the web interface has a password of its own, which a person
chooses with `calu web --set-password` and may leave empty (then the interface asks for none).
It replaces the login token that `calu web` used to print: printed text stays in scrollback
and logs, where Claude Code's commands could read it.

    stored as   {"kdf": "pbkdf2-sha256", "rounds": N, "salt": ..., "hash": ...}  or  {"empty": true}
    where       `<config>/calu/web-password.json`, mode 0600 (as root: /etc/calu, root's alone)

The password itself is never stored. Checking it costs `calu.setup.scram_iterations()` rounds,
0.03 to 0.5 s by CPU, so that a guesser pays for every guess; changing it needs the right to
write the file, which in root's installation only root has.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from calu.config import user_env_file
from calu.setup import scram_iterations

FILE_NAME = "web-password.json"
"""The file of the web password in calu's configuration directory."""

KDF = "pbkdf2-sha256"
"""How the stored hash is made."""


@dataclass(frozen=True)
class WebPassword:
    """The stored web password: its hash, or the choice of none.

    Attributes:
        empty: True when the person chose no password: the interface then asks for none.
        rounds: The PBKDF2 rounds of `digest`.
        salt: The salt of `digest`.
        digest: PBKDF2-SHA-256 of the password.
    """

    empty: bool
    rounds: int = 0
    salt: bytes = b""
    digest: bytes = b""

    def matches(self, typed: str) -> bool:
        """Tell whether `typed` is the password; takes `rounds` of work.

        Args:
            typed: What a person typed.

        Returns:
            True if it is the stored password (any text matches an empty choice).
        """
        if self.empty:
            return True
        made = hashlib.pbkdf2_hmac("sha256", typed.encode("utf-8"), self.salt, self.rounds)
        return hmac.compare_digest(made, self.digest)


def default_path() -> Path:
    """Return the file of the web password of this process's configuration.

    Returns:
        `FILE_NAME` next to calu's env file: `$XDG_CONFIG_HOME/calu` or `~/.config/calu`, and
        `/etc/calu` for root's installation (`calu.asuser` sets `XDG_CONFIG_HOME`).
    """
    return user_env_file().with_name(FILE_NAME)


def load(path: Path | None = None) -> WebPassword | None:
    """Read the stored web password.

    Args:
        path: The file; None is `default_path()`.

    Returns:
        The stored choice, or None when none was made yet (the file is missing).

    Raises:
        OSError: If the file exists but cannot be read.
        ValueError: If it holds no web password of this format.
    """
    path = default_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"{path} holds no web password")
    if data.get("empty") is True:
        return WebPassword(empty=True)
    if data.get("kdf") != KDF or not isinstance(data.get("rounds"), int):
        raise ValueError(f"{path} holds no web password of the format {KDF}")
    return WebPassword(
        empty=False,
        rounds=data["rounds"],
        salt=base64.b64decode(str(data.get("salt", ""))),
        digest=base64.b64decode(str(data.get("hash", ""))),
    )


def store(password: str, path: Path | None = None) -> None:
    """Store the hash of `password`, or the choice of none for "".

    Side effects: creates the directory (mode 0700) if missing; writes the file under a new
    temporary name (mode 0600, never following a symbolic link) and renames it over `path`.

    Args:
        password: The chosen password; "" means the interface asks for none.
        path: The file; None is `default_path()`.

    Raises:
        OSError: If the file cannot be written.
    """
    path = default_path() if path is None else path
    if password:
        salt = secrets.token_bytes(16)
        rounds = scram_iterations()
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        data: dict[str, object] = {
            "kdf": KDF,
            "rounds": rounds,
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(digest).decode(),
        }
    else:
        data = {"empty": True}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out)
        out.write("\n")
    temporary.replace(path)
