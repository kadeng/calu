"""The root-owned Unix socket of a session: local root clients connect without a token (D215).

A session that runs as root listens, next to its TCP port, on a Unix socket that only root
can reach:

    /run/calu/sessions/              root, 0700
    /run/calu/sessions/<id>.sock     root, 0600      <id> is the session's row id

A client that runs as root (`calu attach --sudo`) finds the socket by the session's id, checks
that the socket and every directory above it belong to root and that nobody else may use
them, and connects. The session accepts such a connection without a client token when the
kernel reports the peer as root: the file system and the kernel have done what the token does
for a TCP connection. A program of the unprivileged user can neither open the socket nor
replace it.

Binding, checking and connecting need root's file access; a process that has switched its
file identity to its user runs them through `calu.asuser.as_root`.

The same functions, with `owner` set to a user's uid, bind and check the `--insecure`
sockets in `user_root()` (D227). Those are that user's alone, not root's: every program of
that user, Claude Code's commands included, can connect to them, and the session asks no
token there. Where a docstring below says "root", read "the owner" for those sockets.
"""

import os
import socket
import stat
import struct
import tempfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path("/run/calu/sessions")
"""Where the sockets are; `/run` is cleared at boot."""

PRIVATE = stat.S_IRWXG | stat.S_IRWXO
"""The permission bits a socket or its directory must not have."""


class NotTrusted(OSError):
    """A socket that is not root's alone; the message says what is wrong with it."""


def user_root(environ: Mapping[str, str] | None = None, uid: int | None = None) -> Path:
    """Return where the `--insecure` sockets of one user's sessions are (D227).

    A session that runs as that user, and a root session whose Claude Code runs as that user,
    put their socket there; that user's `calu attach` looks there.

        uid not given, $XDG_RUNTIME_DIR set  ──► $XDG_RUNTIME_DIR/calu/sessions
        /run/user/<uid> is a directory        ──► /run/user/<uid>/calu/sessions
        otherwise                             ──► <temporary directory>/calu-<uid>/sessions

    Args:
        environ: The environment to read `XDG_RUNTIME_DIR` from; None uses `os.environ`.
            It is read only when `uid` is not given.
        uid: The user; None is this process's effective uid.

    Returns:
        The directory; its two lowest directories get mode 0700 when `bind` creates them.
    """
    if uid is None:
        base = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR")
        if base:
            return Path(base) / "calu" / "sessions"
        uid = os.geteuid()
    runtime = Path("/run/user") / str(uid)
    if runtime.is_dir():
        return runtime / "calu" / "sessions"
    return Path(tempfile.gettempdir()) / f"calu-{uid}" / "sessions"


def path_of(session_id: str, root: Path = ROOT) -> Path:
    """Return where the socket of a session is.

    Args:
        session_id: The id of the session's row.
        root: The directory of the sockets.

    Returns:
        `<root>/<session_id>.sock`.
    """
    return root / f"{session_id}.sock"


def bind(path: Path, owner: int = 0) -> socket.socket:
    """Create the socket of a session and listen on it. Needs root's file access.

    Side effects: creates the socket's directory and the one above it with mode 0700 if they
    are missing, removes a socket file left by an earlier process, creates the socket with
    mode 0600. Both directories are checked whether calu created them or found them: an
    existing `/run/calu` that somebody else owns or may write to is refused before anything
    is removed or bound in it.

    Args:
        path: `path_of(session_id)`.
        owner: The uid that must own the result (root; a test passes its own).

    Returns:
        The listening socket.

    Raises:
        OSError: If a directory or the socket cannot be created.
        NotTrusted: If either directory or the result is not root's alone (`problems`).
    """
    for directory in (path.parent.parent, path.parent):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
    # Whoever may write to the outer directory can replace the inner one between this check
    # and the bind, so both are checked before the old socket is removed.
    wrong = _directory_problems(path.parent.parent, owner) + _directory_problems(path.parent, owner)
    if wrong:
        raise NotTrusted("; ".join(wrong))
    if path.is_socket():
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The mask makes the socket private from the moment it exists.
        before = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(before)
        path.chmod(0o600)
        listener.listen()
        wrong = problems(path, owner)
        if wrong:
            raise NotTrusted("; ".join(wrong))
    except BaseException:
        listener.close()
        raise
    return listener


def _directory_problems(directory: Path, owner: int) -> list[str]:
    """Say why a directory of the sockets cannot be trusted. Needs root's file access.

    Checked without following a symbolic link at the directory's own name: it is a
    directory, belongs to `owner`, and its group and others cannot write to it.

    Args:
        directory: The directory.
        owner: The uid that must own it (root; a test passes its own).

    Returns:
        One sentence per finding; empty if the directory can be trusted.
    """
    try:
        status = directory.lstat()
    except OSError as exc:
        return [f"{directory}: {exc}"]
    found: list[str] = []
    if not stat.S_ISDIR(status.st_mode):
        found.append(f"{directory} is no directory")
    if status.st_uid != owner:
        found.append(f"{directory} belongs to uid {status.st_uid}, not to uid {owner}")
    if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        found.append(f"{directory} is writable by its group or by others")
    return found


def problems(path: Path, owner: int = 0) -> list[str]:
    """Say why a socket cannot be trusted to be root's alone. Needs root's file access.

    Checked without following a symbolic link at the socket's own name: the path is a
    socket, it and its directory belong to `owner`, and neither grants anything to a group
    or to others; the directory above (`/run/calu`) belongs to `owner` and its group and
    others cannot write to it, so nobody else can replace the socket's directory.

    Args:
        path: The socket.
        owner: The uid that must own it (root; a test passes its own).

    Returns:
        One sentence per finding; empty if the socket can be trusted.
    """
    found: list[str] = []
    try:
        own = path.lstat()
        directory = path.parent.lstat()
    except OSError as exc:
        return [f"{path}: {exc}"]
    if not stat.S_ISSOCK(own.st_mode):
        found.append(f"{path} is no socket")
    if not stat.S_ISDIR(directory.st_mode):
        found.append(f"{path.parent} is no directory")
    for place, status in ((path, own), (path.parent, directory)):
        if status.st_uid != owner:
            found.append(f"{place} belongs to uid {status.st_uid}, not to uid {owner}")
        if status.st_mode & PRIVATE:
            found.append(f"{place} is open to its group or to others")
    found += _directory_problems(path.parent.parent, owner)
    return found


def listed(root: Path = ROOT, owner: int = 0) -> list[Path]:
    """Return the sessions' sockets that are root's alone. Needs root's file access.

    This is how a root client finds the running sessions without asking the database
    (agent/records/PLAN.md D226): every session that runs as root and was not started with
    `--secure` listens on `<root>/<session id>.sock`.

    Args:
        root: The directory of the sockets.
        owner: The uid that must own them (root; a test passes its own).

    Returns:
        The sockets in `root` for which `problems` finds nothing, sorted by name; empty if the
        directory is missing or cannot be read. A socket left by a process that died is
        listed too: connecting to it fails.
    """
    try:
        names = sorted(root.glob("*.sock"))
    except OSError:
        return []
    return [path for path in names if not problems(path, owner)]


def connected(path: Path, owner: int = 0) -> socket.socket:
    """Check a session's socket and connect to it. Needs root's file access.

    Args:
        path: The socket.
        owner: The uid that must own it.

    Returns:
        The connected socket, not blocking, as an event loop takes it.

    Raises:
        NotTrusted: If the socket is not `owner`'s alone; nothing is connected then.
        OSError: If the connection fails.
    """
    wrong = problems(path, owner)
    if wrong:
        raise NotTrusted("; ".join(wrong))
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        client.setblocking(False)
    except BaseException:
        client.close()
        raise
    return client


def peer_uid(connection: socket.socket) -> int | None:
    """Ask the kernel which user is at the other end of a Unix socket connection (Linux).

    Args:
        connection: A connected Unix socket.

    Returns:
        The peer's uid at the time it connected, or None if the kernel does not say.
    """
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError, AttributeError:
        return None
    return struct.unpack("3i", raw)[1]


def peer_pid(connection: socket.socket) -> int | None:
    """Ask the kernel which process is at the other end of a Unix socket connection (Linux).

    Args:
        connection: A connected Unix socket.

    Returns:
        The peer's pid as this process's pid namespace sees it, or None if the kernel does not
        say or the peer is not visible here (0).
    """
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError, AttributeError:
        return None
    return struct.unpack("3i", raw)[0] or None


MAX_SOCKET_PATH = 100
"""Bytes a Unix socket path may have. The kernel's limit is 108 including the terminating zero
(`sun_path`), and a few bytes of margin keep the message friendlier than `AF_UNIX path too
long`."""


def check_socket_path(socket_path: Path) -> None:
    """Refuse a Unix socket path the kernel cannot bind.

    Args:
        socket_path: The path to check.

    Raises:
        OSError: If the path is longer than `MAX_SOCKET_PATH` bytes. The message names the
            length and suggests `$XDG_RUNTIME_DIR`, because the usual cause is a socket under a
            deeply nested directory.
    """
    length = len(str(socket_path).encode())
    if length > MAX_SOCKET_PATH:
        raise OSError(
            f"the socket path is {length} bytes, and a Unix socket allows about "
            f"{MAX_SOCKET_PATH}: put it somewhere shorter, for example under $XDG_RUNTIME_DIR "
            f"({socket_path})"
        )
