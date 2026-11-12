"""Which registered project a directory belongs to (migration 0025).

`calu project add DIR` records the directory and the host with the `projects` row;
`calu run` without `--project` looks the row up again from its working directory:

    calu project add /src/shop --name shop      projects: (shop, /src/shop, host-a)
    cd /src/shop/backend && calu run            project_for_directory -> "shop"

The nearest registered directory wins: with `/src/shop` and `/src/shop/backend` both
registered, a session in `/src/shop/backend/api` gets the project of `/src/shop/backend`.
Only rows of the same host count, because the same path on another machine is another
directory.
"""

from pathlib import Path

from calu.db import Db


class AmbiguousProject(ValueError):
    """Several projects are registered for the same directory on this host.

    Raised instead of picking one: the projects may carry different rules, and a session must
    not run under rules chosen by row order.
    """

    def __init__(self, directory: str, names: list[str]) -> None:
        """Create the error.

        Args:
            directory: The directory the projects share.
            names: Their names.
        """
        self.directory = directory
        self.names = names
        super().__init__(
            f"{', '.join(names)} are all registered for {directory}; "
            "name the one to use with --project"
        )


async def register_directory(db: Db, name: str, directory: Path, hostname: str) -> bool:
    """Record the directory a project was registered for.

    Side effects: updates `projects.directory` and `projects.hostname` of the named row.

    Args:
        db: A connection of the database owner.
        name: Name of an existing project.
        directory: The project directory; stored as given, so pass a resolved absolute path.
        hostname: The host the directory is on (`socket.gethostname()`).

    Returns:
        True if the row exists and was updated, False if no project has that name.

    Raises:
        psycopg.Error: On database errors.
    """
    row = await db.one(
        "UPDATE projects SET directory = %s, hostname = %s WHERE name = %s RETURNING id",
        (str(directory), hostname, name),
    )
    return row is not None


async def project_for_directory(db: Db, directory: Path, hostname: str) -> str | None:
    """Find the project registered for a directory or for the nearest directory above it.

    Args:
        db: A connection that may read `projects` (the session role may).
        directory: The session's working directory, resolved and absolute.
        hostname: This host's name; rows of other hosts are ignored.

    Returns:
        The project's name, or None if no project is registered for the directory or any of
        its parents on this host.

    Raises:
        AmbiguousProject: If the nearest registered directory belongs to several projects.
        psycopg.Error: On database errors.
    """
    rows = await db.all(
        "SELECT name, directory FROM projects"
        " WHERE hostname = %s AND directory IS NOT NULL ORDER BY name",
        (hostname,),
    )
    by_directory: dict[Path, list[str]] = {}
    for name, registered in rows:
        by_directory.setdefault(Path(str(registered)), []).append(str(name))
    for candidate in (directory, *directory.parents):
        names = by_directory.get(candidate)
        if not names:
            continue
        if len(names) > 1:
            raise AmbiguousProject(str(candidate), names)
        return names[0]
    return None
