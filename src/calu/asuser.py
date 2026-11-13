"""calu as root, everything it does in the project as the unprivileged user (PLAN.md D206).

When calu runs as root and Claude Code as another user, the project directory belongs to that
user. Whatever calu itself does there must not carry root's rights: git runs programs a
repository configures, and a symbolic link in the project can point at a file only root may
read. Three things take care of that, all set up by `enter`:

    file access of the calu process  ── file-system uid, gid and groups of the user
                                        (per thread on Linux; threads started later inherit
                                        them; measured in `spikes/spike_fs_identity.py`)
    programs calu starts (git, ...)  ── uid, gid, groups and an environment built for the
                                        user (`child`)
    Claude Code                      ── calu's launcher (`calu.launch`)

The process itself stays root: the user can neither signal nor trace it, nor read its memory.
What calu keeps for the user outside the project (snapshots, and Claude Code's state for a
uid without a home directory) lives below `STATE_ROOT`, in directories owned by that user.
"""

import ctypes
import os
import pwd
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from calu.launch import DEFAULT_KEEP, RunAs, environment

STATE_ROOT = Path("/var/lib/calu")
"""Below it: `claude/<uid>`, the home calu gives Claude Code for a uid without one, and
`state/<uid>`, calu's own state for that user (the snapshots of the project)."""

CHILD_KEEP = (*DEFAULT_KEEP, "GIT_*")
"""Environment variables a program calu starts as the user keeps, as shell patterns."""

SYS_SETGROUPS = {"x86_64": 116, "aarch64": 159}
"""Number of the `setgroups` system call per machine type. The C library's `setgroups` changes
every thread of the process; the system call changes the calling thread only."""

_active: RunAs | None = None


class UserError(Exception):
    """The user Claude Code is to run as cannot be used; the message says why."""


def resolve(text: str, state_root: Path = STATE_ROOT) -> RunAs:
    """Read `--user`: a number is a bare uid, anything else a login name (requirement 26).

        "1000" or "1000:1000"  ──► that uid (gid: the second number, else the same number),
                                   no name, no supplementary groups, the home
                                   `<state_root>/claude/<uid>` that `prepare` creates. Also
                                   when the uid has a name on this machine: the rule is
                                   the same everywhere.
        "ada"                  ──► uid, gid, groups and home directory from the system's
                                   user database

    Args:
        text: The option's value.
        state_root: Where the home of a bare uid is created.

    Returns:
        The user.

    Raises:
        UserError: If a number is negative or malformed, or the name is unknown.
    """
    head, _, tail = text.partition(":")
    if head.isdigit():
        if tail and not tail.isdigit():
            raise UserError(f"--user {text!r}: a uid takes a numeric gid, as in 1000:1000")
        uid = int(head)
        gid = int(tail) if tail else uid
        return RunAs(uid=uid, gid=gid, home=str(state_root / "claude" / str(uid)))
    try:
        entry = pwd.getpwnam(text)
    except KeyError:
        raise UserError(
            f"--user {text!r}: no such user on this machine (a number names a uid without one)"
        ) from None
    groups = tuple(sorted(set(os.getgrouplist(entry.pw_name, entry.pw_gid)) - {entry.pw_gid}))
    return RunAs(
        uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir, groups=groups, user=entry.pw_name
    )


def state_home(run_as: RunAs, state_root: Path = STATE_ROOT) -> Path:
    """Return the directory calu keeps its own state for a user in (the snapshots).

    Args:
        run_as: The user.
        state_root: The root of calu's state.

    Returns:
        `<state_root>/state/<uid>`; `enter` makes it the process's `XDG_STATE_HOME`.
    """
    return state_root / "state" / str(run_as.uid)


def prepare(run_as: RunAs, state_root: Path = STATE_ROOT) -> None:
    """Create the directories calu provides for a user. Needs root; call before `enter`.

    Side effects: creates `state_home(run_as)` and, for a uid without a name, its home; both
    owned by the user, mode 0700. An existing directory keeps its content and gets owner and
    mode set again.

    Args:
        run_as: The user.
        state_root: The root of calu's state.

    Raises:
        UserError: If a named user's home directory does not exist, or a directory cannot be
            created.
    """
    wanted = [state_home(run_as, state_root)]
    if run_as.user is None:
        wanted.append(Path(run_as.home))
    elif not Path(run_as.home).is_dir():
        raise UserError(
            f"the home directory {run_as.home} of {run_as.user} does not exist; Claude Code "
            "keeps its login and its conversations there"
        )
    for directory in wanted:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, run_as.uid, run_as.gid)
            directory.chmod(0o700)
        except OSError as exc:
            raise UserError(f"cannot prepare {directory}: {exc}") from exc


def fs_identity(uid: int, gid: int, groups: tuple[int, ...]) -> None:
    """Set the calling thread's file-system uid, gid and supplementary groups (Linux).

    Only file access is affected; the process's real and effective ids stay as they are. A
    thread started by this one afterwards inherits the three.

    Args:
        uid: The file-system uid.
        gid: The file-system gid.
        groups: The supplementary groups.

    Raises:
        UserError: If the platform is not supported, or the kernel refuses (the process is
            not root) or did not apply the change.
    """
    number = SYS_SETGROUPS.get(os.uname().machine)
    if sys.platform != "linux" or number is None:
        raise UserError(
            f"running Claude Code as another user is not supported on {sys.platform} "
            f"{os.uname().machine}"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    members = (ctypes.c_uint32 * len(groups))(*groups)
    if libc.syscall(number, len(groups), members) != 0:
        raise UserError(f"setgroups failed: {os.strerror(ctypes.get_errno())}")
    libc.setfsgid(gid)
    libc.setfsuid(uid)
    # Both calls return the previous value and report no error: ask again to see the result.
    if (libc.setfsuid(uid), libc.setfsgid(gid)) != (uid, gid):
        raise UserError(f"the file-system identity could not be set to {uid}:{gid}")


THREADS_END_S = 5.0
"""How long `enter` waits for threads that are just ending, such as the worker threads of an
event loop that was closed a moment ago: they are told to stop and are not waited for."""


def _other_threads(wait_s: float) -> list[str]:
    """Wait for the threads beside the calling one to end, and name those that did not.

    Args:
        wait_s: How long to wait in all.

    Returns:
        The names of the threads that still run; empty if the calling thread is alone.
    """
    deadline = time.monotonic() + wait_s
    current = threading.current_thread()
    for thread in threading.enumerate():
        if thread is not current:
            thread.join(max(0.0, deadline - time.monotonic()))
    return [thread.name for thread in threading.enumerate() if thread is not current]


def enter(run_as: RunAs, state_root: Path = STATE_ROOT) -> None:
    """Make this process work as the user wherever it touches files or starts programs.

    Side effects, for the rest of the process: sets the calling thread's file-system identity
    (`fs_identity`), so call it in the main thread before any other thread exists; sets
    `XDG_STATE_HOME` to `state_home(run_as)`, so calu's snapshots are kept where the user's
    git can write them, and `CLAUDE_CONFIG_DIR` to `<home>/.claude`, so calu reads the
    conversations Claude Code keeps for that user; makes `child` return that user. Everything
    that needs root's files (the credential store, the checker keys) must have been read.

    Args:
        run_as: The user; `prepare` has been called for it.
        state_root: The root of calu's state.

    Raises:
        UserError: If other threads still run after `THREADS_END_S` seconds, or the identity
            cannot be set.
    """
    global _active
    others = _other_threads(THREADS_END_S)
    if others:
        raise UserError(
            "the user cannot be switched while other threads of this process run, because "
            f"they would keep root's file access: {', '.join(others)}"
        )
    fs_identity(run_as.uid, run_as.gid, (run_as.gid, *run_as.groups))
    os.environ["XDG_STATE_HOME"] = str(state_home(run_as, state_root))
    os.environ["CLAUDE_CONFIG_DIR"] = str(Path(run_as.home) / ".claude")
    _active = run_as


def as_root[T](work: Callable[..., T], *arguments: Any) -> T:
    """Run a function with root's file access, also after `enter`.

    Before `enter` the function is simply called. After it, the function runs in a thread of
    its own whose file-system identity is set back to root; the calling thread and every
    other thread keep the user's (measured: `spikes/spike_fs_identity.py`). Blocks until the
    function returns. Use it only for calu's own files, never for a path the user controls.

    Args:
        work: The function.
        *arguments: Its arguments.

    Returns:
        What the function returned.

    Raises:
        UserError: If the thread cannot get root's identity.
    """
    if _active is None:
        return work(*arguments)
    outcome: list[T] = []
    failure: list[BaseException] = []

    def body() -> None:
        """Take root's file identity, call the function and keep its outcome."""
        try:
            fs_identity(0, 0, (0,))
            outcome.append(work(*arguments))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=body, name="calu-as-root")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return outcome[0]


def active() -> RunAs | None:
    """Return the user `enter` switched to.

    Returns:
        The user, or None while calu works as the user it runs as.
    """
    return _active


def child(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the keyword arguments that start a program as the user calu works for.

    For `subprocess.run`, `subprocess.Popen` and `anyio.run_process`:
    `subprocess.run(command, cwd=..., **child(environment))`.

    Args:
        env: The environment the caller wants the program to have; None means calu's own.

    Returns:
        Without `enter`: `{"env": env}` if `env` is given, else nothing, so the program
        starts as before. After `enter`: `user`, `group`, `extra_groups`, and an `env` built
        from an empty one: the variables of `env` (or of calu's environment) that match
        `CHILD_KEEP`, with `HOME`, `USER` and `LOGNAME` of the user.
    """
    if _active is None:
        return {} if env is None else {"env": dict(env)}
    spec = _active.spec("").model_copy(update={"keep": CHILD_KEEP})
    return {
        "user": _active.uid,
        "group": _active.gid,
        "extra_groups": list(_active.groups),
        "env": environment(spec, os.environ if env is None else env),
    }


SYSTEM_CONFIG = "/etc"
"""`XDG_CONFIG_HOME` of a calu that runs as root: its credentials are in `/etc/calu`."""

SYSTEM_STATE = "/var/lib"
"""`XDG_STATE_HOME` of a calu that runs as root: its state is in `/var/lib/calu`."""

INSTALL_HINT = (
    "sudo env UV_TOOL_DIR=/usr/local/lib/calu UV_TOOL_BIN_DIR=/usr/local/bin "
    "UV_PYTHON_INSTALL_DIR=/usr/local/lib/calu/python "
    "uv tool install git+https://github.com/kadeng/calu"
)
"""How to install calu where only root can change it."""


def use_system_directories(environ: MutableMapping[str, str], euid: int) -> None:
    """Point a calu that runs as root at the system directories (requirement 26).

        root, started through sudo        ──► always /etc/calu and /var/lib/calu: the
                                              variables sudo let through may be the user's
        root otherwise, variable not set  ──► the same
        root otherwise, variable set      ──► kept: root chose it (a container, a test)
        not root                          ──► nothing changes

    Side effect: sets `XDG_CONFIG_HOME` and `XDG_STATE_HOME` in `environ`.

    Args:
        environ: The process environment.
        euid: The effective uid of the process.
    """
    if euid != 0:
        return
    forced = "SUDO_UID" in environ
    for name, value in (("XDG_CONFIG_HOME", SYSTEM_CONFIG), ("XDG_STATE_HOME", SYSTEM_STATE)):
        if forced or not environ.get(name):
            environ[name] = value


def writable_by_others(paths: Sequence[Path]) -> list[str]:
    """Find what somebody other than root could change among files and all their parents.

    Args:
        paths: Files or directories of calu's installation.

    Returns:
        One sentence per path, or parent directory of one, that is not owned by root or is
        writable by its group or by everybody; empty if only root can change any of them.
        A path that does not exist is reported too.
    """
    problems: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            continue
        for place in (resolved, *resolved.parents):
            if place in seen:
                continue
            seen.add(place)
            status = place.stat()
            if status.st_uid != 0:
                problems.append(f"{place} belongs to uid {status.st_uid}, not to root")
            elif status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                problems.append(f"{place} is writable by its group or by everybody")
    return problems


def sudo_command(arguments: Sequence[str], user: str, executable: Path) -> list[str]:
    """Build the command that runs calu again as root for the invoking user.

    Args:
        arguments: calu's own arguments as they were given, without the program name.
        user: The login name of the invoking user.
        executable: The `calu` console script of this installation.

    Returns:
        `sudo -- <executable> <arguments without --sudo> --user <user> --from-sudo`.
    """
    kept = [argument for argument in arguments if argument != "--sudo"]
    return ["sudo", "--", str(executable), *kept, "--user", user, "--from-sudo"]
