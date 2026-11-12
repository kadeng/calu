"""Trusted commits and trusted file contents in the database (migration 0027).

    trusted_commits   commit id, tree id, when, by whom, method (user | review | session), note
    trusted_files     SHA-256 of a file content, a path as a hint, when, by whom, note

A session reads both and adds through `calu_certify_commit` and `calu_trust_file`
(`DatabaseSessionStore` calls the functions of this module). An operator or the owner also
lists and removes, which is what `calu honesty trust | trusted | untrust` does. Every function
here takes the connection of whichever role calls it; the database decides what that role may
do.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from calu.db import Db

type TrustMethod = Literal["user", "review", "session"]
"""How a commit was certified: by a person, by an honesty review, or by the session after a
commit made in a clean state."""


@dataclass(frozen=True)
class TrustedCommit:
    """One row of `trusted_commits`."""

    commit_id: str
    tree_id: str
    certified_at: datetime
    certified_by: str
    """The client or OS user for the method "user"; what the session wrote for the others."""
    method: str
    note: str = ""

    def line(self) -> str:
        """Render the row as one terminal line.

        Returns:
            The commit id (12 characters), the date, the method, who certified it, and the
            note if there is one.
        """
        note = f"  {self.note}" if self.note else ""
        return (
            f"{self.commit_id[:12]}  {self.certified_at:%Y-%m-%d %H:%M}  "
            f"{self.method:<7}  {self.certified_by}{note}"
        )


@dataclass(frozen=True)
class TrustedFile:
    """One row of `trusted_files`."""

    content_sha256: str
    path: str
    """Where the file was when it was certified: a hint for people, not part of the trust."""
    certified_at: datetime
    certified_by: str
    note: str = ""

    def line(self) -> str:
        """Render the row as one terminal line.

        Returns:
            The hash (12 characters), the date, who certified it, the path, and the note if
            there is one.
        """
        note = f"  {self.note}" if self.note else ""
        return (
            f"{self.content_sha256[:12]}  {self.certified_at:%Y-%m-%d %H:%M}  "
            f"{self.certified_by}  {self.path}{note}"
        )


async def certify_commit(
    db: Db, commit_id: str, tree_id: str, method: TrustMethod, by: str, note: str = ""
) -> bool:
    """Record a commit as trusted (`calu_certify_commit`).

    Args:
        db: A connection of a session that has started, of an operator or of the owner.
        commit_id: The full commit id.
        tree_id: The id of the tree the commit records.
        method: How it was certified. An operator and the owner may only pass "user".
        by: Who certified it; must not be empty.
        note: An optional note.

    Returns:
        True if the commit was recorded; False if it was trusted already (the first
        certification is kept).

    Raises:
        psycopg.Error: On database errors, on ids that are no full object ids, on an empty
            `by`, and (`InsufficientPrivilege`) for a session connection that has not proven
            its session or an operator passing another method than "user".
    """
    return bool(
        await db.scalar(
            "SELECT calu_certify_commit(%s, %s, %s, %s, %s)",
            (commit_id, tree_id, method, by, note),
        )
    )


async def trusted_among(
    db: Db, commit_ids: Sequence[str], tree_ids: Sequence[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Tell which of some commit ids and tree ids are trusted.

    Args:
        db: A connection of any calu role.
        commit_ids: Commit ids to look up.
        tree_ids: Tree ids to look up.

    Returns:
        (the commit ids among `commit_ids` that are trusted, the tree ids among `tree_ids`
        that a trusted commit records).

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT commit_id, tree_id FROM trusted_commits"
        " WHERE commit_id = ANY(%s) OR tree_id = ANY(%s)",
        (list(commit_ids), list(tree_ids)),
    )
    wanted_commits, wanted_trees = set(commit_ids), set(tree_ids)
    return (
        frozenset(row[0] for row in rows if row[0] in wanted_commits),
        frozenset(row[1] for row in rows if row[1] in wanted_trees),
    )


async def trusted_commits(db: Db, limit: int | None = None) -> list[TrustedCommit]:
    """List the trusted commits, newest first.

    Args:
        db: A connection of any calu role.
        limit: The largest number of rows; None for all.

    Returns:
        The rows.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT commit_id, tree_id, certified_at, certified_by, method, coalesce(note, '')"
        " FROM trusted_commits ORDER BY certified_at DESC, commit_id LIMIT %s",
        (limit,),
    )
    return [TrustedCommit(*row) for row in rows]


async def untrust_commit(db: Db, commit_id: str) -> bool:
    """Remove a trusted commit (`calu_untrust_commit`).

    Args:
        db: A connection of an operator or the owner.
        commit_id: The full commit id.

    Returns:
        True if a row was removed.

    Raises:
        psycopg.Error: On database errors; `InsufficientPrivilege` for a session connection.
    """
    return bool(await db.scalar("SELECT calu_untrust_commit(%s)", (commit_id,)))


async def commits_with_prefix(db: Db, prefix: str) -> list[str]:
    """Find the trusted commits whose id starts with some characters.

    Args:
        db: A connection of any calu role.
        prefix: Hexadecimal characters.

    Returns:
        The full ids, sorted; [] if `prefix` is not hexadecimal.

    Raises:
        psycopg.Error: On database errors.
    """
    text = prefix.strip().lower()
    if not text or any(char not in "0123456789abcdef" for char in text):
        return []
    rows = await db.all(
        "SELECT commit_id FROM trusted_commits WHERE starts_with(commit_id, %s) ORDER BY commit_id",
        (text,),
    )
    return [row[0] for row in rows]


async def trust_file(db: Db, content_sha256: str, path: str, by: str, note: str = "") -> bool:
    """Record a file content that a person certified (`calu_trust_file`).

    Args:
        db: A connection of a session that has started, of an operator or of the owner.
        content_sha256: SHA-256 of the content, 64 hexadecimal characters.
        path: Where the file is, as a hint for people.
        by: Who certified it; must not be empty.
        note: An optional note.

    Returns:
        True if the hash was recorded; False if it was trusted already.

    Raises:
        psycopg.Error: On database errors, on a malformed hash, on an empty `by` or `path`,
            and (`InsufficientPrivilege`) for a session connection that has not proven its
            session.
    """
    return bool(
        await db.scalar("SELECT calu_trust_file(%s, %s, %s, %s)", (content_sha256, path, by, note))
    )


async def trusted_file_hashes(db: Db) -> frozenset[str]:
    """Read the hashes of every trusted file content.

    The table holds exceptions that people recorded by hand, so reading all of it is cheap.

    Args:
        db: A connection of any calu role.

    Returns:
        The SHA-256 values.

    Raises:
        psycopg.Error: On database errors.
    """
    return frozenset(row[0] for row in await db.all("SELECT content_sha256 FROM trusted_files"))


async def trusted_files(db: Db) -> list[TrustedFile]:
    """List the trusted file contents, newest first.

    Args:
        db: A connection of any calu role.

    Returns:
        The rows.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT content_sha256, path, certified_at, certified_by, coalesce(note, '')"
        " FROM trusted_files ORDER BY certified_at DESC, content_sha256"
    )
    return [TrustedFile(*row) for row in rows]


async def untrust_file(db: Db, hash_or_path: str) -> int:
    """Remove trusted file contents by hash or by recorded path (`calu_untrust_file`).

    Args:
        db: A connection of an operator or the owner.
        hash_or_path: A SHA-256, or a path exactly as a row records it.

    Returns:
        How many rows were removed.

    Raises:
        psycopg.Error: On database errors; `InsufficientPrivilege` for a session connection.
    """
    return int(await db.scalar("SELECT calu_untrust_file(%s)", (hash_or_path,)) or 0)
