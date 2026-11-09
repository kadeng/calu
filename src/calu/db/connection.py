"""Database connections and schema migrations (PLAN.md §7, D13).

Migrations are the files `calu/db/migrations/NNNN_name.sql`, applied in the order of their
number. Each runs in its own transaction and is recorded in `schema_migrations`. A
transaction-level advisory lock keeps two processes from migrating at the same time.
"""

import re
from dataclasses import dataclass
from importlib import resources

import psycopg

MIGRATION_LOCK = 0x6361_6C75  # "calu"
"""Key of the advisory lock held while migrating."""

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    sql: str


def migrations() -> list[Migration]:
    """Return the migrations shipped with calu, sorted by version.

    Returns:
        The migrations; file names that do not match `NNNN_name.sql` are ignored.

    Raises:
        ValueError: If two files have the same version number.
    """
    found: dict[int, Migration] = {}
    for entry in resources.files("calu.db").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {entry.name}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


async def connect(url: str | bytes | bytearray) -> psycopg.AsyncConnection:
    """Open an async connection in autocommit mode.

    Args:
        url: A libpq connection string or URL, e.g. "postgresql://user:pw@host:5432/calu".

    Returns:
        The open connection; the caller closes it.

    Raises:
        psycopg.OperationalError: If the server cannot be reached or rejects the login.
    """
    conninfo = url if isinstance(url, str) else bytes(url).decode()
    return await psycopg.AsyncConnection.connect(conninfo, autocommit=True)


async def applied_versions(conn: psycopg.AsyncConnection) -> set[int]:
    """Return the versions recorded in `schema_migrations`.

    Args:
        conn: An open connection.

    Returns:
        The applied versions; empty if the table does not exist yet.

    Raises:
        psycopg.Error: On database errors.
    """
    cur = await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
    row = await cur.fetchone()
    if not row or not row[0]:
        return set()
    cur = await conn.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in await cur.fetchall()}


async def migrate(conn: psycopg.AsyncConnection) -> list[Migration]:
    """Apply all migrations that are not applied yet.

    The connection must belong to the database owner (it creates tables, functions and the
    roles `calu_session` and `calu_operator`). Each migration runs in one transaction together
    with its `schema_migrations` row, so a failed migration leaves nothing behind.

    Args:
        conn: An open connection in autocommit mode (see `connect`).

    Returns:
        The migrations applied by this call, in order; empty if the schema was up to date.

    Raises:
        psycopg.Error: If a migration fails; earlier migrations of this call stay applied.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version integer PRIMARY KEY,"
        " name text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied: list[Migration] = []
    for migration in migrations():
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            if migration.version in await applied_versions(conn):
                continue
            await conn.execute(migration.sql.encode())
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
        applied.append(migration)
    return applied
