"""The "new session" dialog of `calu web`: choose a folder, make one, or clone a repository.

See agent/records/PLAN.md D270 (the owner's request of 2026-11-24).

    /start?at=<folder>          the dialog, in a window of its own: the folders below the
                                projects directory of the `calu serve` of this host and user
    POST /start                 makes the folder (a new name, or a `git clone`), files a
                                session request there if asked (`calu.session_requests`), and
                                turns the window into the chat pane of that folder if asked

What may happen on disk is bounded twice. Every folder is checked against the server's projects
directory with the server's own rule (`calu.projects_dir.served_path`: below it, no hidden
part), before and after it is made. And the web interface does its file access as the user it
works for (`calu web --sudo`, `calu serve --web`: `calu.asuser.enter`), so a folder or a clone
belongs to that user and goes only where that user may write; `git` runs as that user too
(`calu.asuser.child`).

A clone takes `https://`, `ssh://` and `user@host:path` addresses only: git's other transports
include `ext::`, which runs a command. Git asks nothing (no terminal prompt, ssh in batch
mode), and a clone that takes longer than `CLONE_TIMEOUT_S` is stopped.
"""

import os
import pwd
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from calu import asuser
from calu.projects_dir import NotServed, served_path

CLONE_TIMEOUT_S = 300.0
"""How long a `git clone` may take before it is stopped."""

MAX_LISTED = 300
"""How many folders the dialog lists at one level."""

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
"""A new folder's name: no path separator, not hidden, no leading dash, at most 100 chars."""

_SCP = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/+-]+$")
"""An scp-like git address, e.g. `git@github.com:kadeng/calu.git`."""


class StartRefused(Exception):
    """The folder cannot be used or made; the message is for the person in the dialog."""


def web_user() -> str:
    """Name the user whose files the web interface works with.

    Returns:
        The user `calu.asuser.enter` switched to (a root `calu web` working for a user), else
        the user this process runs as.
    """
    active = asuser.active()
    if active is not None:
        return active.user or str(active.uid)
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def folder(projects: Path, at: str) -> Path:
    """Resolve a folder of the dialog, given relative to the projects directory.

    Args:
        projects: The server's projects directory (resolved).
        at: The folder below it, e.g. "shop/api"; "" for the projects directory itself.

    Returns:
        The resolved folder; it may not exist.

    Raises:
        StartRefused: If it lies outside the projects directory or in a hidden folder
            (`served_path`).
    """
    try:
        return served_path(projects, str(projects / at.strip("/")), "this machine")
    except NotServed as exc:
        raise StartRefused(str(exc)) from exc


def listing(projects: Path, at: str) -> list[str]:
    """List the folders the dialog offers at one level.

    Args:
        projects: The server's projects directory (resolved).
        at: The folder below it whose folders are listed.

    Returns:
        The names of its folders, without hidden ones, sorted, at most `MAX_LISTED`.

    Raises:
        StartRefused: If `at` is outside the projects directory, hidden, or no folder.
    """
    base = folder(projects, at)
    if not base.is_dir():
        raise StartRefused(f"{base} is no folder")
    try:
        names = [
            entry.name
            for entry in os.scandir(base)
            if entry.is_dir() and not entry.name.startswith(".")
        ]
    except OSError as exc:
        raise StartRefused(f"{base} cannot be read: {exc.strerror}") from exc
    return sorted(names, key=str.lower)[:MAX_LISTED]


def clone_refusal(url: str) -> str | None:
    """Tell whether a git address may be cloned.

    Args:
        url: The address as typed.

    Returns:
        None for `https://…`, `ssh://…` and `user@host:path`; otherwise why not.
    """
    if url.startswith("-") or "::" in url or any(c.isspace() for c in url):
        return "that is no git address calu clones"
    if url.startswith(("https://", "ssh://")) or _SCP.match(url):
        return None
    return "calu clones https://, ssh:// and user@host:path addresses only"


def repository_name(url: str) -> str:
    """Derive a folder name from a git address, as `git clone` does.

    Args:
        url: The address, e.g. `https://github.com/kadeng/calu.git`.

    Returns:
        The last part without `.git`, e.g. "calu"; "" if none can be derived.
    """
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    return last.removesuffix(".git")


Runner = Callable[..., subprocess.CompletedProcess[str]]
"""`subprocess.run`, or a fake of it in tests."""


def prepare(projects: Path, at: str, name: str, url: str, run: Runner = subprocess.run) -> Path:
    """Make the folder a session is to start in, as the dialog asked.

        url given      ──► `git clone url <at>/<name or the repository's name>`
        name given     ──► a new, empty folder <at>/<name>
        neither        ──► the folder <at> itself, which must exist

    Side effects: creates a folder, or runs `git clone` as the user the web interface works
    for (`calu.asuser.child`), with no terminal prompt, ssh in batch mode and a limit of
    `CLONE_TIMEOUT_S`. Nothing is written when a check fails.

    Args:
        projects: The server's projects directory (resolved).
        at: The folder below it the new folder goes in, or the chosen folder.
        name: The new folder's name; "" for the repository's name or for no new folder.
        url: The git address; "" for no clone.
        run: Runs git (`subprocess.run`).

    Returns:
        The folder, resolved; it exists.

    Raises:
        StartRefused: If a folder is outside the projects directory or hidden, a name or an
            address is refused, the new folder exists already, or git or the file system
            fails (the message says which and why).
    """
    base = folder(projects, at)
    name, url = name.strip(), url.strip()
    if url:
        why = clone_refusal(url)
        if why:
            raise StartRefused(why)
        name = name or repository_name(url)
    if name and not _NAME.match(name):
        raise StartRefused(
            f"{name!r} is no folder name calu makes: letters, digits, '.', '_' and '-', not "
            "starting with '.' or '-'"
        )
    if not name:
        if not base.is_dir():
            raise StartRefused(f"{base} is no folder")
        return base
    if not base.is_dir():
        raise StartRefused(f"{base} is no folder to make {name} in")
    target = folder(projects, str(base.relative_to(projects) / name))
    if target.exists():
        raise StartRefused(f"{target} exists already; choose it in the list instead")
    if not url:
        try:
            target.mkdir(mode=0o755)
        except OSError as exc:
            raise StartRefused(f"{target} cannot be made: {exc.strerror}") from exc
        return target
    _clone(url, target, run)
    return folder(projects, str(target.relative_to(projects)))


def _clone(url: str, target: Path, run: Runner) -> None:
    """Run `git clone` as the user the web interface works for.

    Args:
        url: A checked address (`clone_refusal`).
        target: The folder to clone into; it does not exist.
        run: Runs git.

    Raises:
        StartRefused: If git is missing, fails or takes longer than `CLONE_TIMEOUT_S`.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    }
    command: Sequence[str] = [
        "git",
        "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
        "-c", "protocol.ssh.allow=always",
        "clone", "--", url, str(target),
    ]  # fmt: skip
    keep = (*asuser.CHILD_KEEP, "GIT_TERMINAL_PROMPT", "GIT_SSH_COMMAND")
    try:
        done = run(
            list(command),
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_S,
            check=False,
            **asuser.child(env, keep),
        )
    except FileNotFoundError as exc:
        raise StartRefused("git is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise StartRefused(f"git clone took longer than {CLONE_TIMEOUT_S:.0f} s") from exc
    if done.returncode != 0:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise StartRefused("git clone failed: " + (said[-1] if said else f"exit {done.returncode}"))
