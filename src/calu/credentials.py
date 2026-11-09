"""Encrypted store for database credentials (PLAN.md D12).

The store is one JSON file, by default `~/.config/calu/credentials.json` (directory mode 0700,
file mode 0600). It maps a credential name (`admin`, `session`) to an encrypted secret, usually
a PostgreSQL connection URL:

    password ──Argon2id(salt, t, m, p)──► 32-byte key
    secret ──AES-256-GCM(key, nonce, associated data = name)──► ciphertext

File format (version 1):

    {"version": 1,
     "entries": {"admin": {"kdf": "argon2id", "time_cost": 3, "memory_cost": 65536,
                           "parallelism": 4, "salt": "<base64>", "nonce": "<base64>",
                           "ciphertext": "<base64>"}}}

The name is the associated data of the encryption, so an entry copied under another name does
not decrypt. Decrypted secrets are returned as `bytearray`, so callers can overwrite them with
`wipe` once they are no longer needed. Python and the libraries used here may keep further
copies in memory; PLAN.md §5 explains why that is accepted.
"""

import base64
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FILE_VERSION = 1


def default_path() -> Path:
    """Return the default location of the credential store.

    Returns:
        `$XDG_CONFIG_HOME/calu/credentials.json`, or `~/.config/calu/credentials.json` if
        `XDG_CONFIG_HOME` is not set.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "calu" / "credentials.json"


class CredentialError(Exception):
    """Base class of the errors of this module."""


class WrongPassword(CredentialError):
    """The password does not decrypt the entry (or the entry was modified)."""


class UnknownCredential(CredentialError):
    """The store has no entry of that name."""


class CorruptStore(CredentialError):
    """The store file is not valid JSON or does not have the expected format."""


@dataclass(frozen=True)
class KdfParams:
    """Argon2id cost parameters.

    The defaults (3 passes, 64 MiB, 4 lanes) take roughly 0.1-0.5 s on current machines.
    Tests use smaller values to stay fast.
    """

    time_cost: int = 3
    memory_cost: int = 64 * 1024
    """Memory in KiB."""
    parallelism: int = 4


def wipe(buffer: bytearray) -> None:
    """Overwrite a secret in place with zero bytes.

    Args:
        buffer: The secret; its length is unchanged, every byte becomes 0.
    """
    buffer[:] = bytes(len(buffer))


def _as_bytes(value: str | bytes | bytearray) -> bytes:
    """Convert a password or secret to bytes (UTF-8 for strings).

    Args:
        value: The value.

    Returns:
        The value as bytes (a copy for `bytearray`).
    """
    return value.encode() if isinstance(value, str) else bytes(value)


def _derive_key(password: str | bytes | bytearray, salt: bytes, params: KdfParams) -> bytes:
    """Derive a 32-byte AES key from a password with Argon2id.

    Args:
        password: The password.
        salt: A random salt of at least 16 bytes.
        params: The Argon2id cost parameters.

    Returns:
        The key.
    """
    return hash_secret_raw(
        secret=_as_bytes(password),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=32,
        type=Type.ID,
    )


class CredentialStore:
    """Reads and writes the encrypted credential file.

    Every method reads the file anew, so several processes see each other's changes. Writes
    replace the file atomically (write a temporary file, then rename).
    """

    def __init__(self, path: Path | None = None, kdf: KdfParams | None = None) -> None:
        """Create a store object; the file is not touched until a method needs it.

        Args:
            path: The store file; None uses `default_path()`.
            kdf: Argon2id parameters for new entries; None uses `KdfParams()`. Existing entries
                keep the parameters they were written with.
        """
        self.path = path or default_path()
        self.kdf = kdf or KdfParams()

    def _read(self) -> dict[str, dict[str, object]]:
        """Read all entries of the file.

        Returns:
            The entries by name; empty if the file does not exist.

        Raises:
            CorruptStore: If the file is not valid JSON or has an unknown format.
            OSError: If the file exists but cannot be read.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CorruptStore(f"{self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            raise CorruptStore(f"{self.path}: unknown format")
        entries = data.get("entries")
        if not isinstance(entries, dict):
            raise CorruptStore(f"{self.path}: no entries")
        return entries

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        """Replace the file with the given entries.

        Creates the directory with mode 0700 if needed; the file gets mode 0600.

        Args:
            entries: All entries by name.

        Raises:
            OSError: If the directory or file cannot be written.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = json.dumps({"version": FILE_VERSION, "entries": entries}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def names(self) -> list[str]:
        """Return the names of all stored credentials.

        Returns:
            The names, sorted.

        Raises:
            CorruptStore: If the file has an unknown format.
            OSError: If the file exists but cannot be read.
        """
        return sorted(self._read())

    def __contains__(self, name: object) -> bool:
        """Tell whether an entry of this name exists.

        Args:
            name: The credential name.

        Returns:
            True if the store has the entry.

        Raises:
            CorruptStore: If the file has an unknown format.
            OSError: If the file exists but cannot be read.
        """
        return name in self._read()

    def save(
        self, name: str, secret: str | bytes | bytearray, password: str | bytes | bytearray
    ) -> None:
        """Encrypt `secret` under `password` and store it as `name`.

        An existing entry of the same name is replaced; other entries are kept.

        Args:
            name: The credential name, e.g. "admin" or "session". Must not be empty.
            secret: The secret, e.g. a connection URL.
            password: The password that will be needed to decrypt it. Must not be empty.

        Raises:
            ValueError: If `name` or `password` is empty.
            CorruptStore: If the existing file has an unknown format.
            OSError: If the file cannot be read or written.
        """
        if not name or not password:
            raise ValueError("name and password must not be empty")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = _derive_key(password, salt, self.kdf)
        ciphertext = AESGCM(key).encrypt(nonce, _as_bytes(secret), name.encode())
        entries = self._read()
        entries[name] = {
            "kdf": "argon2id",
            "time_cost": self.kdf.time_cost,
            "memory_cost": self.kdf.memory_cost,
            "parallelism": self.kdf.parallelism,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        self._write(entries)

    def load(self, name: str, password: str | bytes | bytearray) -> bytearray:
        """Decrypt the entry `name` with `password`.

        Args:
            name: The credential name.
            password: The password the entry was saved with.

        Returns:
            The secret. The caller should `wipe` it after use.

        Raises:
            UnknownCredential: If there is no entry `name`.
            WrongPassword: If the password is wrong or the entry was modified.
            CorruptStore: If the file or the entry has an unknown format.
            OSError: If the file cannot be read.
        """
        entries = self._read()
        if name not in entries:
            raise UnknownCredential(name)
        entry = entries[name]
        try:
            if entry["kdf"] != "argon2id":
                raise CorruptStore(f"{name}: unknown kdf {entry['kdf']!r}")
            params = KdfParams(
                time_cost=int(entry["time_cost"]),  # type: ignore[arg-type]
                memory_cost=int(entry["memory_cost"]),  # type: ignore[arg-type]
                parallelism=int(entry["parallelism"]),  # type: ignore[arg-type]
            )
            salt = base64.b64decode(str(entry["salt"]))
            nonce = base64.b64decode(str(entry["nonce"]))
            ciphertext = base64.b64decode(str(entry["ciphertext"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise CorruptStore(f"{name}: {exc}") from exc
        key = _derive_key(password, salt, params)
        try:
            return bytearray(AESGCM(key).decrypt(nonce, ciphertext, name.encode()))
        except InvalidTag as exc:
            raise WrongPassword(name) from exc

    def remove(self, name: str) -> bool:
        """Delete the entry `name`.

        Args:
            name: The credential name.

        Returns:
            True if the entry existed.

        Raises:
            CorruptStore: If the file has an unknown format.
            OSError: If the file cannot be read or written.
        """
        entries = self._read()
        if name not in entries:
            return False
        del entries[name]
        self._write(entries)
        return True
