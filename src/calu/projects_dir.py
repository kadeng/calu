"""The projects directory of a `calu serve`, and the relay port derived from it (PLAN D243).

    calu serve [--projects DIR]          (default: the current directory)
        │
        ├─ check_projects_dir: realpath; refused if "/", hidden (a component starting with
        │  "."), inside a system directory (SYSTEM_DIRECTORIES), or root's home while Claude
        │  Code does not run as root
        │
        ├─ relay_ports: 8811 + sha256(realpath) mod 189, then the next ones in 8811-8999,
        │  skipping RESERVED_PORTS; the first one free is the relay's port
        │
        └─ served_path, for every session request the server claims: the requested
           directory, resolved, must be the projects directory or below it, with no hidden
           component below it

Nothing here touches the database or the network; the module imports nothing from calu, so
`calu.cli` can check the directory before it asks for a password.
"""

import hashlib
import os
import sys
from pathlib import Path, PurePosixPath

SYSTEM_DIRECTORIES: tuple[str, ...] = (
    # Linux and macOS
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/run",
    "/sbin",
    "/snap",
    "/sys",
    "/usr",
    "/var/backups",
    "/var/cache",
    "/var/db",
    "/var/lib",
    "/var/lock",
    "/var/log",
    "/var/mail",
    "/var/run",
    "/var/spool",
    # macOS (/etc and /var are links into /private there)
    "/Applications",
    "/Library",
    "/System",
    "/cores",
    "/private/etc",
    "/private/var/at",
    "/private/var/audit",
    "/private/var/backups",
    "/private/var/db",
    "/private/var/log",
    "/private/var/mail",
    "/private/var/run",
    "/private/var/spool",
    "/private/var/vm",
)
"""Directories in which `calu serve` serves no projects directory: the operating system's
programs, libraries, configuration, kernel interfaces, devices and state. All of `/usr` is in
it (also `/usr/local`, which holds installed software, not projects). `/var/www`, `/srv`,
`/opt`, `/tmp`, `/mnt` and `/home` are not. On macOS the comparison ignores case, as its file
system does."""

ROOT_HOMES: tuple[str, ...] = ("/root", "/var/root", "/private/var/root")
"""Root's home directory on Linux and on macOS: refused unless Claude Code runs as root."""

RELAY_PORT_FIRST = 8811
"""The lowest port a relay takes by default."""

RELAY_PORT_LAST = 8999
"""The highest port a relay takes by default."""

RESERVED_PORTS: frozenset[int] = frozenset({8834, 8843, 8880, 8883, 8888, 8889, 8983})
"""Ports of the range that other common programs use (Nessus, alternative HTTP and HTTPS,
MQTT over TLS, Jupyter, Solr); a relay never takes them by default."""


class ProjectsDirRefused(ValueError):
    """`calu serve` does not serve this projects directory; the message says why and what to do."""


class NotServed(ValueError):
    """A session directory outside what the server serves; the message says why and what to do."""


def _parts(path: PurePosixPath) -> tuple[str, ...]:
    """Return the path's components for comparing, without case on macOS.

    Args:
        path: An absolute path.

    Returns:
        Its components ("/" first), case-folded on macOS.
    """
    parts = path.parts
    return tuple(part.casefold() for part in parts) if sys.platform == "darwin" else parts


def _within(path: PurePosixPath, base: str) -> bool:
    """Tell whether a path is a directory or lies below it.

    Args:
        path: An absolute, resolved path.
        base: An absolute directory.

    Returns:
        True if `path` equals `base` or lies below it.
    """
    mine, theirs = _parts(path), _parts(PurePosixPath(base))
    return mine[: len(theirs)] == theirs


def refusal(path: Path, claude_as_root: bool) -> str | None:
    """Say why a resolved directory cannot be a projects directory, if it cannot.

    Args:
        path: An absolute path whose symbolic links are resolved (`os.path.realpath`).
        claude_as_root: Whether Claude Code runs as root (calu runs as root without `--user`,
            or with `--user root`); only then may root's home directory be served.

    Returns:
        The reason, or None if the directory may be served.
    """
    pure = PurePosixPath(path)
    if pure == PurePosixPath("/"):
        return "it is the root directory, which holds the whole system"
    hidden = [part for part in pure.parts[1:] if part.startswith(".")]
    if hidden:
        return f"it is hidden or inside a hidden directory ({hidden[0]})"
    for system in SYSTEM_DIRECTORIES:
        if _within(pure, system):
            return f"it is inside {system}, a directory of the operating system"
    if not claude_as_root:
        for home in ROOT_HOMES:
            if _within(pure, home):
                return (
                    f"it is inside {home}, root's home directory, and Claude Code does not run "
                    "as root"
                )
    return None


def check_projects_dir(path: str | Path, claude_as_root: bool = False) -> Path:
    """Resolve and check the projects directory a `calu serve` is to serve.

    Symbolic links are resolved first, so a link to a refused directory is refused too.

    Args:
        path: The directory as given (`--projects`, or the current directory); a relative
            path is taken from the current directory.
        claude_as_root: Whether Claude Code runs as root (see `refusal`).

    Returns:
        The resolved directory.

    Raises:
        ProjectsDirRefused: If it does not exist, is no directory, or `refusal` names a reason.
    """
    resolved = Path(os.path.realpath(path))
    if not resolved.is_dir():
        raise ProjectsDirRefused(f"the projects directory {path} is not a directory")
    reason = refusal(resolved, claude_as_root)
    if reason is not None:
        shown = str(path) if str(path) == str(resolved) else f"{path} ({resolved})"
        raise ProjectsDirRefused(
            f"`calu serve` cannot serve the projects directory {shown}: {reason}. Start it in "
            "the directory that holds your projects, or name one with --projects DIR"
        )
    return resolved


def served_path(projects: Path, directory: str, hostname: str) -> Path:
    """Resolve a requested session directory and check that the server serves it.

    The requested directory is resolved (symbolic links followed) and must then be the
    projects directory or lie below it, with no hidden component below it.

    Args:
        projects: The server's resolved projects directory (`check_projects_dir`).
        directory: The directory a request names: an absolute path on the server's machine.
        hostname: The server's host, for the message.

    Returns:
        The resolved directory. It may not exist; the caller checks that it is a directory.

    Raises:
        NotServed: If it is no absolute path, lies outside `projects`, or is inside a hidden
            directory below it; the message is the reason for the requester.
    """
    if not os.path.isabs(directory):
        raise NotServed(f"{directory} is no absolute path on {hostname}")
    resolved = Path(os.path.realpath(directory))
    if not resolved.is_relative_to(projects):
        raise NotServed(
            f"{directory} is not in {projects}, the projects directory this `calu serve` on "
            f"{hostname} serves. On {hostname}, run `calu serve --projects <a directory that "
            "holds it>` for it, or `calu run` in that directory"
        )
    hidden = [part for part in resolved.relative_to(projects).parts if part.startswith(".")]
    if hidden:
        raise NotServed(
            f"{directory} is inside the hidden directory {hidden[0]}; calu serve starts no "
            "session there"
        )
    return resolved


def relay_ports(projects: Path) -> list[int]:
    """List the relay ports a server of this projects directory tries, in order.

    The first port is derived from the directory, so a server of the same directory takes
    the same port after every restart: 8811 + (SHA-256 of the path as a number) mod 189,
    moved on past `RESERVED_PORTS`. The rest of the range follows in order, wrapping around.

    Example: `relay_ports(Path("/home/ada/src"))[0]` is the same number on every call and on
    every machine; a second server whose first port is taken takes the next one listed.

    Args:
        projects: The resolved projects directory.

    Returns:
        Every port of `RELAY_PORT_FIRST`..`RELAY_PORT_LAST` that is not reserved, each once,
        starting with the derived one.
    """
    count = RELAY_PORT_LAST - RELAY_PORT_FIRST + 1
    digest = int.from_bytes(hashlib.sha256(os.fsencode(projects)).digest(), "big")
    start = digest % count
    ordered = [RELAY_PORT_FIRST + (start + step) % count for step in range(count)]
    return [port for port in ordered if port not in RESERVED_PORTS]


def relay_port(projects: Path) -> int:
    """Return the relay port a server of this projects directory takes when it is free.

    Args:
        projects: The resolved projects directory.

    Returns:
        The first port of `relay_ports`.
    """
    return relay_ports(projects)[0]
