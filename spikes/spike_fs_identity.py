"""Spike: can a root process do its file access as another user, per thread (D206)?

calu as root reads and writes files in a project directory that the unprivileged user
controls: a symbolic link there could point at a file only root may read. Linux keeps a
file-system uid, gid and supplementary groups per thread; `setfsuid`, `setfsgid` and the raw
`setgroups` system call change them without changing who the process is for signals and
tracing. This measures, as root in a container:

- after the switch in the main thread: can it read a root-only file, who owns a file it
  creates, and do threads started afterwards inherit the identity?
- can one thread go back to root for a moment without the others following?
- are the process's real, effective and saved uid still 0?

Run as root in a container: `docker run --rm -v <python>:<python>:ro -v $PWD:$PWD:ro
postgres:18 <python>/bin/python3 $PWD/spikes/spike_fs_identity.py`. Results are recorded in
`spikes/RESULTS.md`.
"""

import ctypes
import os
import tempfile
import threading
from pathlib import Path

UID = GID = 1000
SYS_SETGROUPS = 116  # x86_64
libc = ctypes.CDLL(None, use_errno=True)


def fs_identity(uid: int, gid: int, groups: list[int]) -> None:
    """Set the calling thread's file-system uid, gid and supplementary groups.

    Args:
        uid: The file-system uid.
        gid: The file-system gid.
        groups: The supplementary groups.
    """
    array = (ctypes.c_uint32 * len(groups))(*groups)
    if libc.syscall(SYS_SETGROUPS, len(groups), array) != 0:
        raise OSError(ctypes.get_errno(), "setgroups")
    libc.setfsgid(gid)
    libc.setfsuid(uid)


def can_read(path: Path) -> str:
    """Say whether the calling thread can read a file.

    Args:
        path: The file.

    Returns:
        "yes" or the error's name.
    """
    try:
        path.read_text()
        return "yes"
    except OSError as exc:
        return type(exc).__name__


def status() -> str:
    """Return the uid, gid and group lines of the calling thread from /proc.

    Returns:
        The lines, joined.
    """
    lines = Path("/proc/thread-self/status").read_text().splitlines()
    return "; ".join(line for line in lines if line.startswith(("Uid", "Gid", "Groups")))


def main() -> None:
    """Run the measurements and print them."""
    secret = Path("/etc/root-only")
    secret.write_text("secret\n")
    secret.chmod(0o600)
    work = Path(tempfile.mkdtemp())
    os.chown(work, UID, GID)
    (work / "link").symlink_to(secret)
    print("before:", status(), "| reads root-only:", can_read(secret))

    fs_identity(UID, GID, [GID])
    print("main after switch:", status())
    print("  reads root-only:", can_read(secret), "| through the link:", can_read(work / "link"))
    (work / "made").write_text("x")
    made = (work / "made").stat()
    print("  file it created is owned by", made.st_uid, made.st_gid)
    print("  process ids:", os.getresuid(), os.getresgid())

    seen: dict[str, str] = {}

    def later() -> None:
        """A thread started after the switch."""
        seen["later thread"] = f"{status()} | reads root-only: {can_read(secret)}"

    def back_to_root() -> None:
        """A thread that returns to root for itself."""
        fs_identity(0, 0, [0])
        seen["thread back at root"] = f"{status()} | reads root-only: {can_read(secret)}"

    for body in (later, back_to_root):
        thread = threading.Thread(target=body)
        thread.start()
        thread.join()
    for name, text in seen.items():
        print(f"{name}: {text}")
    print("main at the end:", status(), "| reads root-only:", can_read(secret))


if __name__ == "__main__":
    main()
