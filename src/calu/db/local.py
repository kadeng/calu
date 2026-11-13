"""A local PostgreSQL in Docker (the variant `docker` of `calu setup`, PLAN.md D14, D205).

The container runs from `local-compose.yml` (compose project `calu-db`) and listens on
127.0.0.1 only. Its files are in a host directory chosen at setup (default
`default_data_dir()`), or in the Docker volume `calu-db_data` for a database that an earlier
`calu db up` created. The database owner is `calu_owner`; its password is generated on the
first start and exists in plain text only in calu's memory and, during that first start, in the
container's environment.

`ensure_running` starts a stopped container when a calu command needs the database, so nobody
has to run `calu db up` before `calu run`.
"""

import os
import secrets
import socket
import subprocess
from importlib import resources
from pathlib import Path

from calu.db.target import VOLUME_DATA, DockerBlock, PasswordAuth, Target

PROJECT = "calu-db"
VOLUME = f"{PROJECT}_data"
OWNER = "calu_owner"
DATABASE = "calu"
DEFAULT_PORT = 5433


class DockerError(Exception):
    """A docker command failed; the message holds its output."""


def compose_file() -> Path:
    """Return the path of the compose file shipped with calu.

    Returns:
        The path of `calu/db/local-compose.yml` inside the installed package.
    """
    return Path(str(resources.files("calu.db").joinpath("local-compose.yml")))


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a docker command and capture its output.

    Args:
        args: The command, starting with "docker".
        env: Extra environment variables for the command.

    Returns:
        The finished process (any exit code).

    Raises:
        DockerError: If docker is not installed.
    """
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError("docker is not installed or not on PATH") from exc


def volume_exists() -> bool:
    """Tell whether the named data volume exists, i.e. a database was initialized in it.

    Returns:
        True if the volume `calu-db_data` exists.

    Raises:
        DockerError: If docker is not installed.
    """
    return _run(["docker", "volume", "inspect", VOLUME]).returncode == 0


def default_data_dir() -> Path:
    """Return where `calu setup` puts the files of a new local database unless told otherwise.

    Returns:
        `$XDG_STATE_HOME/calu/db` or `~/.local/state/calu/db`: inside the state directory
        that calu already keeps out of reach of the commands Claude runs (`calu.paths`).
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "calu" / "db"


def mount_of(data: str) -> Path | None:
    """Return the host directory the container mounts for a stored `DockerBlock.data`.

    The container's PostgreSQL runs under a user id of its own and has to traverse the mount,
    so the mounted directory cannot be private to the host user. calu therefore keeps the
    chosen directory itself private (mode 0700) and mounts its subdirectory `pg`.

    Args:
        data: An absolute directory, or `VOLUME_DATA`.

    Returns:
        `<data>/pg`, or None for the named volume.
    """
    return None if data == VOLUME_DATA else Path(data) / "pg"


def _unused(directory: Path) -> bool:
    """Tell whether a directory holds nothing but what `prepare_data_dir` itself creates.

    Args:
        directory: An existing directory.

    Returns:
        True if it is empty, or holds only an empty directory `pg` (a setup that stopped
        before the database was initialized, for example at a mistyped password).
    """
    entries = list(directory.iterdir())
    if not entries:
        return True
    mount = directory / "pg"
    return (
        entries == [mount]
        and mount.is_dir()
        and not mount.is_symlink()
        and not any(mount.iterdir())
    )


def prepare_data_dir(path: Path) -> Path:
    """Check and create the directory for the files of a new local database.

    Side effects: creates `path` with mode 0700 and `path/pg` with mode 0755 (`mount_of`).

    Args:
        path: The directory the user chose; `~` is expanded.

    Returns:
        The absolute directory.

    Raises:
        DockerError: If the path is not absolute after expansion, contains a colon (Docker
            reads one as a separator), is the root or the home directory or a directory above
            the home directory, or already holds something other than the empty `pg` that an
            unfinished setup left behind.
    """
    chosen = path.expanduser()
    if not chosen.is_absolute():
        raise DockerError(f"the data directory must be an absolute path, not {path}")
    chosen = Path(os.path.normpath(chosen))
    home = Path.home()
    if ":" in str(chosen):
        raise DockerError("the data directory must not contain a colon")
    if chosen == Path("/") or chosen == home or chosen in home.parents:
        raise DockerError(f"{chosen} cannot be the data directory of a database")
    if chosen.exists() and (not chosen.is_dir() or not _unused(chosen)):
        raise DockerError(
            f"{chosen} is not an empty directory; choose a new one for a new database"
        )
    chosen.mkdir(mode=0o700, parents=True, exist_ok=True)
    chosen.chmod(0o700)
    (chosen / "pg").mkdir(mode=0o755, exist_ok=True)
    (chosen / "pg").chmod(0o755)
    return chosen


def initialized(data: str = VOLUME_DATA) -> bool:
    """Tell whether a database was initialized at a stored location.

    Args:
        data: An absolute directory, or `VOLUME_DATA`.

    Returns:
        For the named volume: whether it exists. For a directory: whether its `pg`
        subdirectory holds anything.

    Raises:
        DockerError: If docker is not installed (named volume only).
    """
    mount = mount_of(data)
    if mount is None:
        return volume_exists()
    try:
        return any(mount.iterdir())
    except OSError:
        return False


def new_owner_password() -> str:
    """Generate a password for the database owner.

    Returns:
        43 URL-safe random characters (256 bits).
    """
    return secrets.token_urlsafe(32)


def owner_url(password: str, port: int = DEFAULT_PORT) -> str:
    """Build the connection URL of the database owner.

    Args:
        password: The owner password (URL-safe characters, see `new_owner_password`).
        port: The host port of the container.

    Returns:
        E.g. "postgresql://calu_owner:<password>@127.0.0.1:5433/calu".
    """
    return f"postgresql://{OWNER}:{password}@127.0.0.1:{port}/{DATABASE}"


def owner_target(password: str, port: int = DEFAULT_PORT, data: str = VOLUME_DATA) -> Target:
    """Build the stored record of the owner login of the local database.

    The record requires SCRAM authentication of the server: a session then refuses another
    program that listens on the port, or a cluster in a replaced data directory, because
    neither can prove that it knows the password (measured with postgres:18 and libpq 18).

    Args:
        password: The owner password.
        port: The host port of the container.
        data: An absolute directory, or `VOLUME_DATA`.

    Returns:
        The target of the variant `docker` for the role `calu_owner`.
    """
    return Target(
        variant="docker",
        conninfo={
            "host": "127.0.0.1",
            "port": str(port),
            "dbname": DATABASE,
            "user": OWNER,
            "require_auth": "scram-sha-256",
        },
        auth=PasswordAuth(password=password),
        docker=DockerBlock(port=port, data=data),
    )


def up(
    port: int = DEFAULT_PORT, owner_password: str | None = None, data: str = VOLUME_DATA
) -> None:
    """Start the container and wait until PostgreSQL accepts connections.

    Side effects: after a start with an owner password the container is created a second time
    without it, so the password is in the container's configuration only while the cluster
    is being initialized.

    Args:
        port: Host port on 127.0.0.1.
        owner_password: Required on the first start (nothing initialized at `data` yet):
            becomes the owner password. Ignored by PostgreSQL once the cluster exists.
        data: Where the cluster's files are: an absolute directory prepared with
            `prepare_data_dir`, or `VOLUME_DATA` for the named volume.

    Raises:
        ValueError: If nothing is initialized at `data` and no owner password is given: a
            start without one never creates a new, empty database.
        DockerError: If docker is missing or `docker compose up` fails.
    """
    if owner_password is None and not initialized(data):
        raise ValueError("the first start needs the owner password")
    env = {"CALU_DB_PORT": str(port), "CALU_DB_OWNER_PASSWORD": owner_password or ""}
    mount = mount_of(data)
    if mount is not None:
        env["CALU_DB_DATA"] = str(mount)
    command = ["docker", "compose", "-f", str(compose_file()), "-p", PROJECT, "up", "-d", "--wait"]
    result = _run(command, env)
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or result.stdout.strip())
    if owner_password:
        # The cluster exists now and ignores the variable from here on. Start the container
        # once more without it, so that the owner password does not stay in the container's
        # configuration, where `docker inspect` shows it to every program that may use Docker.
        result = _run(command, {**env, "CALU_DB_OWNER_PASSWORD": ""})
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or result.stdout.strip())
    harden()


def listening(port: int, timeout_s: float = 0.5) -> bool:
    """Tell whether something accepts TCP connections on a port of this machine.

    Args:
        port: The port on 127.0.0.1.
        timeout_s: How long to wait for the answer.

    Returns:
        True if a connection was accepted.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def ensure_running(block: DockerBlock) -> bool:
    """Start the local database if it does not answer (the on-demand start, PLAN.md D205).

    Nothing is done, and docker is not called, while the port answers. Otherwise the container
    is started as `up` does. A location without an initialized cluster is an error and not a
    reason to create one: that is `calu setup`'s job, with a password.

    Blocking (a container start takes seconds): call it outside the event loop or in a worker
    thread.

    Args:
        block: Port and data location from the stored record.

    Returns:
        True if the container was started; False if the port answered already, or if the
        record names the old Docker volume and no such volume exists.

    Raises:
        DockerError: If docker is missing, the start fails, or nothing is initialized in the
            stored data directory.
    """
    if listening(block.port):
        return False
    if not initialized(block.data):
        if mount_of(block.data) is None:
            # A record of the old form on the named volume, and no such volume: this may be
            # somebody's own local server or a tunnel that only looks like calu's container.
            # Start nothing and let the connection attempt say what is wrong.
            return False
        raise DockerError(
            f"the local database does not answer on port {block.port} and {block.data} holds "
            "no database yet; `calu db up` creates it with the stored owner login"
        )
    up(block.port, None, block.data)
    return True


HARDEN_SCRIPT = (
    'grep -qE "[[:space:]]trust$" "$PGDATA/pg_hba.conf" || exit 0; '
    'sed -i -E "s/[[:space:]]trust$/ scram-sha-256/" "$PGDATA/pg_hba.conf" && '
    'gosu postgres pg_ctl reload -D "$PGDATA" >/dev/null'
)
"""Replace every `trust` line of the container's pg_hba.conf with `scram-sha-256` and reload;
does nothing when none is left (D163)."""


def harden() -> None:
    """Make PostgreSQL in the container ask every connection for a password (PLAN.md D163).

    The official image trusts connections from inside the container, so `docker exec <container>
    psql -U calu_owner` gets in without a password -- for any program of the user that may use
    Docker, Claude's included, and with the owner's rights. New volumes are created without
    trust (`POSTGRES_INITDB_ARGS` in the compose file); this converts a volume created before,
    in place, and is a no-op afterwards. calu's own connections come through the published port,
    which already uses `scram-sha-256`, so nothing of calu's changes. Measured on postgres:18:
    afterwards `psql` inside the container fails without a password, works with it, and the
    healthcheck (`pg_isready`) is unaffected.

    Side effects: edits pg_hba.conf in the container's data directory and reloads the server.

    Raises:
        DockerError: If the edit or the reload fails.
    """
    result = _run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file()),
            "-p",
            PROJECT,
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            HARDEN_SCRIPT,
        ]
    )
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or result.stdout.strip())


def down() -> None:
    """Stop and remove the container; the data volume is kept.

    Raises:
        DockerError: If docker is missing or `docker compose down` fails.
    """
    result = _run(["docker", "compose", "-f", str(compose_file()), "-p", PROJECT, "down"])
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or result.stdout.strip())
