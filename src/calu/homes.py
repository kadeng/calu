"""Directories written short for session lists: a home directory as `~` or `~user`.

    /home/dev/work/shop   session user dev            ~/work/shop
    /home/ann/shop        session user dev, ann's     ~ann/shop     (only on this machine)
    /srv/shop             anything else               /srv/shop

The session user's home is looked up in this machine's user database when the session runs
on this machine; for a session of another host it is taken to be `/home/<user>` (`/root` for
root), the usual place, because that host's user database cannot be read from here. Another
user's home is recognised on this machine only. A client that shows the short form keeps the
full path where the user can see it (a tooltip, a detail page).

This module imports nothing from calu.
"""

import pwd
import socket

UNUSABLE_HOMES = frozenset({"", "/", "/nonexistent", "/bin", "/sbin", "/usr/sbin", "/dev/null"})
"""Home directories of system accounts that say nothing about a path below them."""


def home_of(user: str, hostname: str | None = None) -> str | None:
    """Find the home directory of a user.

    Args:
        user: The user name.
        hostname: The host the user belongs to; None or this machine's name looks the user
            up in this machine's user database.

    Returns:
        The home directory without a trailing slash; `/home/<user>` (`/root` for root) for
        another host; None for a user this machine does not know, an empty name, or a home
        in `UNUSABLE_HOMES`.
    """
    if not user:
        return None
    if hostname and hostname != socket.gethostname():
        return "/root" if user == "root" else f"/home/{user}"
    try:
        home = pwd.getpwnam(user).pw_dir.rstrip("/")
    except KeyError:
        return None
    return None if home in UNUSABLE_HOMES else home


def _below(directory: str, home: str) -> str | None:
    """Return the part of a directory below a home directory.

    Args:
        directory: An absolute path.
        home: A home directory without a trailing slash.

    Returns:
        "" for the home itself, "/rest" for a path below it, None for any other path.
    """
    if directory == home:
        return ""
    if directory.startswith(home + "/"):
        return directory[len(home) :].rstrip("/")
    return None


def short_path(directory: str, user: str = "", hostname: str | None = None) -> str:
    """Write a session's directory with its user's home as `~`, another user's as `~name`.

    Args:
        directory: The session's directory, an absolute path.
        user: The OS user the session runs as; "" if not known.
        hostname: The session's host; None or this machine's name for this machine.

    Returns:
        `~` or `~/rest` below the session user's home; `~name` or `~name/rest` below the home
        of another user of this machine (the longest such home wins); else `directory`
        unchanged.

    Example:
        short_path("/home/dev/work", "dev", "box") is "~/work".
    """
    own = home_of(user, hostname)
    if own is not None:
        rest = _below(directory, own)
        if rest is not None:
            return "~" + rest
    if hostname and hostname != socket.gethostname():
        return directory
    best: tuple[int, str] | None = None
    try:
        entries = pwd.getpwall()
    except OSError:
        return directory
    for entry in entries:
        home = entry.pw_dir.rstrip("/")
        if home in UNUSABLE_HOMES or entry.pw_name == user:
            continue
        rest = _below(directory, home)
        if rest is not None and (best is None or len(home) > best[0]):
            best = (len(home), f"~{entry.pw_name}{rest}")
    return best[1] if best else directory
