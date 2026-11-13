"""Spike: the session's root-owned socket, made and used after the switch to the user (D215).

Run as root in a container (see `spikes/spike_other_uid.sh` for the mounts). It switches the
process's file identity to uid 1000 as `calu run --user 1000` does, then binds the socket
through `calu.asuser.as_root`, and prints: who owns the socket and its directory, whether the
process's own (user) file access can reach it, whether a connection made with root's file
access is accepted, and which uid the kernel reports for that peer. Results are recorded in
`spikes/RESULTS.md`.
"""

import os

from calu import asuser, localsocket

run_as = asuser.resolve("1000")
asuser.prepare(run_as)
asuser.enter(run_as)
path = localsocket.path_of("11111111-2222-4333-8444-555555555555")
listener = asuser.as_root(localsocket.bind, path)


def owner(place: object) -> str:
    """Describe a path's owner and mode, read with root's file access.

    Args:
        place: The path.

    Returns:
        `uid:gid mode`.
    """
    status = os.lstat(str(place))
    return f"{status.st_uid}:{status.st_gid} {oct(status.st_mode & 0o777)}"


print("socket:", asuser.as_root(owner, path), "| directory:", asuser.as_root(owner, path.parent))
try:
    localsocket.connected(path)
    print("the process's own file access: connected (wrong)")
except OSError as exc:
    print("the process's own file access:", type(exc).__name__)
client = asuser.as_root(localsocket.connected, path)
accepted, _ = listener.accept()
print(
    "with root's file access: connected; the kernel reports the peer as uid",
    localsocket.peer_uid(accepted),
)
print("problems found by the check:", asuser.as_root(localsocket.problems, path))
