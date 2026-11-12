"""Trusted commits and trusted file contents in the database (migrations 0027, 0028).

    trusted_commits   commit id, tree id, when, by whom, source (user | review | check), note
    trusted_files     SHA-256 of a file content, a path as a hint, when, by whom, source, note

Every row names the source of its trust (`TrustSource`, column `method`). The sources are
ordered by strength, user > review > check; a stronger certification replaces a weaker row
for the same commit or content, never the other way round. Which sources count is decided
where the rows are read (`honesty.sufficient_trust`, `sufficient`); no row is deleted for it.

A session reads both tables and adds through `calu_certify_commit` and `calu_trust_file`
(`DatabaseSessionStore` calls the functions of this module). An operator or the owner also
lists, removes and prunes, which is what `calu honesty trust | trusted | untrust | prune`
does. Every function here takes the connection of whichever role calls it; the database
decides what that role may do.

Size: `trusted_files` gets one row per distinct file content that was verified, so a session
adds at most one row per checked edit and one per file of an honesty review (a session with
1000 checked edits adds up to 1000 rows of about 300 bytes). `trusted_commits` gets one row
per certified commit. `prune` removes `check` and `review` rows older than a given age.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from calu.db import Db

type TrustSource = Literal["user", "review", "check"]
"""Who established a trust: "user" a person, "review" calu's honesty review, "check" calu's
step checkers (an edit the edit route's checkers judged without leaving a mark)."""

TRUST_STRENGTH: dict[str, int] = {"check": 1, "review": 2, "user": 3}
"""The order of the sources; a larger number is a stronger source."""


def sufficient(source: str | None, minimum: str) -> bool:
    """Tell whether a recorded source of trust counts under `honesty.sufficient_trust`.

    Args:
        source: The source a row or the session's memory records; None for "not trusted".
        minimum: The weakest source that counts.

    Returns:
        True if `source` is at least as strong as `minimum`. False for None, for a source that
        is none of the three, and when `minimum` is none of the three.

    Example:
        sufficient("review", "check") is True; sufficient("check", "review") is False.
    """
    needed = TRUST_STRENGTH.get(minimum)
    held = TRUST_STRENGTH.get(source or "")
    return needed is not None and held is not None and held >= needed


def stronger(first: TrustSource | None, second: TrustSource | None) -> TrustSource | None:
    """Return the stronger of two sources.

    Args:
        first: A source, or None for "not trusted".
        second: Another one.

    Returns:
        The stronger one; the other if one is None; None if both are.
    """
    if first is None or second is None:
        return first or second
    return first if TRUST_STRENGTH[first] >= TRUST_STRENGTH[second] else second


def weakest(sources: Iterable[TrustSource]) -> TrustSource | None:
    """Return the weakest of some sources.

    Args:
        sources: The sources of the parts of one thing, e.g. of the changes of a commit.

    Returns:
        The weakest one; None if `sources` is empty.
    """
    found = sorted(sources, key=TRUST_STRENGTH.__getitem__)
    return found[0] if found else None


@dataclass(frozen=True)
class TrustedCommit:
    """One row of `trusted_commits`."""

    commit_id: str
    tree_id: str
    certified_at: datetime
    certified_by: str
    """The client or OS user for the source "user"; what the session wrote for the others."""
    method: TrustSource
    """The source of the trust: "user", "review" or "check"."""
    note: str = ""

    def line(self) -> str:
        """Render the row as one terminal line.

        Returns:
            The commit id (12 characters), the date, the source, who certified it, and the
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
    method: TrustSource = "user"
    """The source of the trust: "user", "review" or "check"."""
    note: str = ""

    def line(self) -> str:
        """Render the row as one terminal line.

        Returns:
            The hash (12 characters), the date, the source, who certified it, the path, and
            the note if there is one.
        """
        note = f"  {self.note}" if self.note else ""
        return (
            f"{self.content_sha256[:12]}  {self.certified_at:%Y-%m-%d %H:%M}  "
            f"{self.method:<7}  {self.certified_by}  {self.path}{note}"
        )


async def certify_commit(
    db: Db, commit_id: str, tree_id: str, method: TrustSource, by: str, note: str = ""
) -> bool:
    """Record a commit as trusted, or strengthen its certification (`calu_certify_commit`).

    Args:
        db: A connection of a session that has started, of an operator or of the owner.
        commit_id: The full commit id.
        tree_id: The id of the tree the commit records.
        method: The source of the trust. An operator and the owner may only pass "user".
        by: Who certified it; must not be empty.
        note: An optional note.

    Returns:
        True if the commit was recorded, or its row was replaced because `method` is stronger
        than the recorded one. False if a row of the same or a stronger source exists; that
        row is left as it is.

    Raises:
        psycopg.Error: On database errors, on ids that are no full object ids, on an unknown
            `method`, on an empty `by`, and (`InsufficientPrivilege`) for a session connection
            that has not proven its session or an operator passing another method than "user".
    """
    return bool(
        await db.scalar(
            "SELECT calu_certify_commit(%s, %s, %s, %s, %s)",
            (commit_id, tree_id, method, by, note),
        )
    )


async def trusted_among(
    db: Db, commit_ids: Sequence[str], tree_ids: Sequence[str]
) -> tuple[dict[str, TrustSource], dict[str, TrustSource]]:
    """Tell which of some commit ids and tree ids are trusted, and by which source.

    Args:
        db: A connection of any calu role.
        commit_ids: Commit ids to look up.
        tree_ids: Tree ids to look up.

    Returns:
        (commit id -> source, for the commit ids among `commit_ids` that have a row;
        tree id -> the strongest source among the rows that record that tree, for the tree ids
        among `tree_ids`). The caller applies `sufficient`.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT commit_id, tree_id, method FROM trusted_commits"
        " WHERE commit_id = ANY(%s) OR tree_id = ANY(%s)",
        (list(commit_ids), list(tree_ids)),
    )
    wanted_commits, wanted_trees = set(commit_ids), set(tree_ids)
    commits: dict[str, TrustSource] = {}
    trees: dict[str, TrustSource] = {}
    for commit_id, tree_id, method in rows:
        if commit_id in wanted_commits:
            commits[commit_id] = method
        if tree_id in wanted_trees:
            trees[tree_id] = stronger(trees.get(tree_id), method) or method
    return commits, trees


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


async def trust_file(
    db: Db, content_sha256: str, path: str, method: TrustSource, by: str, note: str = ""
) -> bool:
    """Record a verified file content, or strengthen its record (`calu_trust_file`).

    Args:
        db: A connection of a session that has started, of an operator or of the owner.
        content_sha256: SHA-256 of the content, 64 hexadecimal characters.
        path: Where the file is, as a hint for people.
        method: The source of the trust. An operator and the owner may only pass "user".
        by: Who certified it; must not be empty.
        note: An optional note.

    Returns:
        True if the hash was recorded, or its row was replaced because `method` is stronger
        than the recorded one. False if a row of the same or a stronger source exists; that
        row is left as it is.

    Raises:
        psycopg.Error: On database errors, on a malformed hash, on an unknown `method`, on an
            empty `by` or `path`, and (`InsufficientPrivilege`) for a session connection that
            has not proven its session or an operator passing another method than "user".
    """
    return bool(
        await db.scalar(
            "SELECT calu_trust_file(%s, %s, %s, %s, %s)", (content_sha256, path, method, by, note)
        )
    )


async def distrust_file(db: Db, content_sha256: str, by: str) -> bool:
    """Withdraw the stored trust of a file content a session found dishonest.

    Calls `calu_distrust_file` (migration 0028), which removes the row only if its method is
    "check" or "review". A row a person recorded stays.

    Args:
        db: A connection of a session that has started.
        content_sha256: SHA-256 of the content.
        by: Who withdrew it; must not be empty.

    Returns:
        True if a row was removed; False if there is none or its method is "user".

    Raises:
        psycopg.Error: On database errors, on an empty `by`, and (`InsufficientPrivilege`) for
            a connection that has not proven a session.
    """
    return bool(await db.scalar("SELECT calu_distrust_file(%s, %s)", (content_sha256, by)))


async def trusted_file_sources(
    db: Db, hashes: Sequence[str] | None = None
) -> dict[str, TrustSource]:
    """Read the source of trusted file contents.

    Args:
        db: A connection of any calu role.
        hashes: The SHA-256 values to look up; None reads the whole table (one row per
            verified content, see the module docstring for its size).

    Returns:
        SHA-256 -> source, for the contents that have a row. The caller applies `sufficient`.

    Raises:
        psycopg.Error: On database errors.
    """
    if hashes is None:
        rows = await db.all("SELECT content_sha256, method FROM trusted_files")
    else:
        rows = await db.all(
            "SELECT content_sha256, method FROM trusted_files WHERE content_sha256 = ANY(%s)",
            (list(hashes),),
        )
    return {row[0]: row[1] for row in rows}


async def trusted_files(db: Db, limit: int | None = None) -> list[TrustedFile]:
    """List the trusted file contents, newest first.

    Args:
        db: A connection of any calu role.
        limit: The largest number of rows; None for all.

    Returns:
        The rows.

    Raises:
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT content_sha256, path, certified_at, certified_by, method, coalesce(note, '')"
        " FROM trusted_files ORDER BY certified_at DESC, content_sha256 LIMIT %s",
        (limit,),
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


async def prune(db: Db, days: int) -> int:
    """Remove stored trust of the sources "check" and "review" older than an age.

    Rows of the source "user" are never removed (`calu_prune_trust`). A session whose baseline
    was removed looks it up again and may then have none.

    Args:
        db: A connection of an operator or the owner.
        days: The age in days; rows certified longer ago are removed. 0 removes every "check"
            and "review" row.

    Returns:
        How many rows were removed from both tables together.

    Raises:
        psycopg.Error: On database errors, on a negative `days`, and `InsufficientPrivilege`
            for a session connection.
    """
    return int(await db.scalar("SELECT calu_prune_trust(%s)", (days,)) or 0)
