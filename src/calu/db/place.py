"""Where root's calu database is, written down for every user of the machine.

See agent/records/PLAN.md D228.

    /var/lib/calu/database.json     root, 0644; the directory is root's and only root writes it

`calu setup` as root writes the file. A client that found a root session through a local
socket, and therefore asked no database which session to attach to, reads it to learn which
database that session uses, so that it can log in there as `calu_admin` with the calu user
password a person types. The file holds the host, the port, the database name and how the
server is verified.

With an empty user password (`calu setup --no-password`) it also holds the generated
password of the role `calu_admin`, the user login (the key `admin_password`): the owner's
ruling of 2026-11-18 is that a setup without passwords has weak security and that this is
allowed. Every user and every program of the
machine can then read the file and change calu's settings; a client asks for nothing. After
a setup with a user password the file holds no secret.

Why a file and not the session's word: a socket in a user's runtime directory can be created
by any program of that user. A client that sent the user password to whatever database such
a "session" named would hand the password to that program. Only root can write this file, and
the client checks that before it reads it.
"""

import json
import os
import stat
from pathlib import Path

from calu.db.target import INHERITED, PasswordAuth, Target

PATH = Path("/var/lib/calu/database.json")
"""The place file of root's calu on this machine."""

PATH_ENV = "CALU_PLACE_FILE"
"""Environment variable that names another place file. calu's own tests set it for every
process they start, so that none of them reads the file of the machine they run on. The
checks of `problems` apply to that file as well."""


def default_path() -> Path:
    """Return the place file this process uses.

    Returns:
        The path in `PATH_ENV` if that variable is set and not empty, else `PATH`.
    """
    chosen = os.environ.get(PATH_ENV)
    return Path(chosen) if chosen else PATH


FORMAT = 1
"""The number in `calu_place` of the file's format."""


class PlaceError(ValueError):
    """The place file cannot be trusted or read; the message says why."""


def write(owner: Target, path: Path | None = None, admin_password: str | None = None) -> None:
    """Write where the database of `owner` is. Needs the right to write the directory.

    Side effects: creates the directory if it is missing (mode 0755), writes the file under
    a temporary name and renames it over `path`, mode 0644.

    Args:
        owner: Any login of the database; only `INHERITED` of its connection parameters is
            written (host, port, database name, how the server is verified).
        path: The file; None is `default_path()`.
        admin_password: The password of the role `calu_admin`, to be readable by every user
            of the machine (`calu setup --no-password`); None writes no secret.

    Raises:
        OSError: If the file cannot be written.
        PlaceError: If the login names a libpq service instead of a host and a port.
    """
    if "service" in owner.conninfo:
        raise PlaceError("the login names a libpq service; its place cannot be written down")
    conninfo = {key: owner.conninfo[key] for key in INHERITED if key in owner.conninfo}
    path = default_path() if path is None else path
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    data: dict[str, object] = {"calu_place": FORMAT, "conninfo": conninfo}
    if admin_password:
        data["admin_password"] = admin_password
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def problems(path: Path | None = None, owner: int = 0) -> list[str]:
    """Say why the place file cannot be trusted to be root's word.

    Checked without following a symbolic link at the file's own name: it is a regular file,
    it and its directory belong to `owner`, and neither can be written by a group or by
    others.

    Args:
        path: The file; None is `default_path()`.
        owner: The uid that must own it (root; a test passes its own).

    Returns:
        One sentence per finding; empty if the file can be trusted.
    """
    path = default_path() if path is None else path
    try:
        own = path.lstat()
        directory = path.parent.stat()
    except OSError as exc:
        return [f"{path}: {exc}"]
    found: list[str] = []
    if not stat.S_ISREG(own.st_mode):
        found.append(f"{path} is no regular file")
    for place, status in ((path, own), (path.parent, directory)):
        if status.st_uid != owner:
            found.append(f"{place} belongs to uid {status.st_uid}, not to uid {owner}")
        if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            found.append(f"{place} is writable by its group or by others")
    return found


def _conninfo(path: Path) -> tuple[dict[str, str], str]:
    """Read the connection parameters and the password of `calu_admin` out of the place file.

    Args:
        path: The file.

    Returns:
        The parameters named in `INHERITED` that the file holds, and the password of
        `calu_admin` if the file holds one, else "".

    Raises:
        PlaceError: If the file cannot be read or does not hold a place this version
            understands.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = isinstance(data, dict) and data.get("calu_place") == FORMAT
        found = data.get("conninfo") if known else None
    except (OSError, ValueError) as exc:
        raise PlaceError(f"{path}: {exc}") from exc
    if not isinstance(found, dict):
        raise PlaceError(f"{path}: unknown format")
    conninfo = {str(key): str(value) for key, value in found.items() if key in INHERITED}
    return conninfo, str(data.get("admin_password") or "")


def read(path: Path | None = None, owner: int = 0) -> Target:
    """Read the place file and return the login of `calu_admin` there.

    Args:
        path: The file; None is `default_path()`.
        owner: The uid that must own it (root; a test passes its own).

    Returns:
        The login of the role `calu_admin`: with the password the file holds (`calu setup
        --no-password`), else with an empty one, which a client replaces by the password a
        person types (`calu.tui.adminaccess.StoreAdmin`).

    Raises:
        PlaceError: If the file is missing, is not `owner`'s alone to write (`problems`), or
            does not hold a place this version understands.
    """
    path = default_path() if path is None else path
    wrong = problems(path, owner)
    if wrong:
        raise PlaceError("; ".join(wrong))
    conninfo, password = _conninfo(path)
    # Imported here: `calu.setup` imports from `calu.db`.
    from calu.setup import USER_ROLE

    return Target(
        variant="server",
        conninfo={**conninfo, "user": USER_ROLE},
        auth=PasswordAuth(password=password),
    )
