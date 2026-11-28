"""`calu uninstall`: remove calu, its logins, its state and its local database from this machine.

What it removes (the owner's request of 2026-11-24, agent/records/PLAN.md D267), per run:

    calu uninstall (as you)              sudo calu uninstall (root)
    ~/.config/calu   logins, keys        /etc/calu        root's logins, keys
    ~/.local/state/calu  logs, state,    /var/lib/calu    root's state, logs, the place file,
                     the default                          the default database directory
                     database directory  and, for the user who ran sudo, that user's
    the local database (Docker:          ~/.config/calu, ~/.local/state/calu and own
      container, volume, data directory) installation
    your own installation (uv tool)      the local database, the root installation
                                         (/usr/local/lib/calu, its commands in /usr/local/bin)

What it keeps: prerequisites (uv, Python installed by uv for other tools, Docker and its
postgres image, Claude Code, bubblewrap, socat, git, curl), a database on another server
(it is not this machine's), the IDE extensions, and everything calu wrote into projects
(session traces, recorded edits, a git hook of `calu traces hook`).

`plan` decides without changing anything; `remove` carries a plan out and reports each item.
The caller (`calu.cli.cmd_uninstall`) prints the plan, refuses while calu processes run
(`calu.machine.calu_processes`), and asks for `CONFIRM_PHRASE` typed by hand (`confirm`).
Load everything before the removal starts. The last step may delete the installation this
module runs from, so the module must not import anything afterwards.
"""

import pwd
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CONFIRM_PHRASE = "remove calu"
"""What a person types to confirm the removal."""

MIN_SECONDS = 2.0
"""The least time typing `CONFIRM_PHRASE` may take; a faster answer was not typed by a person
looking at the prompt (piped input, a pasted line, an automatic "yes")."""

ROOT_HOME = Path("/usr/local/lib/calu")
"""The root installation of `install.sh` (agent/records/PLAN.md D211, D264)."""

COMPOSE_PROJECT = "calu-db"
"""The compose project of the local database (`calu.db.local.PROJECT`)."""

VOLUME = f"{COMPOSE_PROJECT}_data"
"""The named volume an early `calu db up` created (`calu.db.local.VOLUME`)."""

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
"""Runs a command and returns it finished (any exit code); raises FileNotFoundError if the
program is missing."""


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output (the default `Runner`).

    Args:
        args: The command and its arguments.

    Returns:
        The finished process, whatever its exit code.

    Raises:
        FileNotFoundError: If the program is not installed.
    """
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Item:
    """One thing `remove` deletes."""

    kind: str
    """"path" (a file or directory), "data" (a database directory whose files belong to the
    container's user), "container" (the compose project's containers), "volume" (the named
    volume)."""
    target: str
    """The path, or the name of the compose project or volume."""
    what: str
    """One line for the plan, e.g. "your logins and checker keys"."""


@dataclass
class Plan:
    """What `calu uninstall` would remove, and what it says about the rest."""

    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Lines about what stays, and why."""

    def lines(self) -> list[str]:
        """Render the plan for the terminal.

        Returns:
            One line per item ("  - <target>: <what>"), then the notes.
        """
        shown = [f"  - {item.target}: {item.what}" for item in self.items]
        return [*shown, *(f"Kept: {note}" for note in self.notes)]


def _tool_env(module_file: Path) -> Path | None:
    """Find the uv tool environment calu runs from, by its receipt.

    Args:
        module_file: `calu.__file__`.

    Returns:
        The directory that holds `uv-receipt.toml` above the module, or None (calu runs from
        a checkout or another kind of installation).
    """
    for place in module_file.resolve().parents:
        if (place / "uv-receipt.toml").is_file():
            return place
    return None


def _entry_points(env: Path) -> list[Path]:
    """Read where uv put the commands of a tool environment.

    Args:
        env: The tool environment (`_tool_env`).

    Returns:
        The install paths of its entry points (e.g. `/usr/local/bin/calu`); [] if the receipt
        cannot be read.
    """
    try:
        receipt = tomllib.loads((env / "uv-receipt.toml").read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return []
    tool = receipt.get("tool")
    found = tool.get("entrypoints", []) if isinstance(tool, dict) else []
    paths = [entry.get("install-path") for entry in found if isinstance(entry, dict)]
    return [Path(path) for path in paths if isinstance(path, str) and path]


def _user_dirs(home: Path, environ: Mapping[str, str]) -> tuple[Path, Path]:
    """Return a user's calu configuration and state directories.

    Args:
        home: The user's home directory.
        environ: That user's environment (`XDG_CONFIG_HOME`, `XDG_STATE_HOME`); for another
            user than this process's, pass {} (the defaults).

    Returns:
        (`$XDG_CONFIG_HOME/calu`, `$XDG_STATE_HOME/calu`) with the XDG defaults.
    """
    config = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "calu"
    state = Path(environ.get("XDG_STATE_HOME") or home / ".local" / "state") / "calu"
    return config, state


def _database_dirs(runner: Runner, defaults: Sequence[Path]) -> list[Path]:
    """Find the host directories that hold the local database's files.

    Args:
        runner: Runs docker.
        defaults: Directories `calu setup` uses by default (`<state>/calu/db`).

    Returns:
        The data directory the container mounts (the parent of its `pg` mount), and each of
        `defaults` that exists; without duplicates.
    """
    found: list[Path] = []
    try:
        ids = runner(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            ]
        ).stdout.split()
        for container in ids[:1]:
            shown = runner(["docker", "inspect", container, "--format", "{{json .Mounts}}"])
            for source in re.findall(r'"Type":"bind","Source":"([^"]+)"', shown.stdout):
                mount = Path(source)
                found.append(mount.parent if mount.name == "pg" else mount)
    except FileNotFoundError:
        pass
    found += [path for path in defaults if path.exists()]
    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def _database_items(
    runner: Runner, defaults: Sequence[Path], euid: int
) -> tuple[list[Item], list[str]]:
    """Plan the removal of the local database in Docker.

    The Docker daemon is the machine's, so the local database may be root's (`sudo calu
    setup`) although a user asks. A run that is not root's leaves a database whose data
    directory it does not own, and says so.

    Args:
        runner: Runs docker.
        defaults: The default data directories to look at.
        euid: The effective uid of this process.

    Returns:
        (items: the containers of the compose project, the named volume, the data
        directories; notes: why something cannot be looked at or is left).
    """
    items: list[Item] = []
    notes: list[str] = []
    dirs = _database_dirs(runner, defaults)
    foreign = [path for path in dirs if euid != 0 and _owner(path) not in (None, euid)]
    unknown = [path for path in dirs if euid != 0 and _owner(path) is None]
    if foreign:
        notes.append(
            f"the local database in {foreign[0]} is root's (`sudo calu setup`): `sudo calu "
            "uninstall` removes it"
        )
        return items, notes
    if unknown:
        notes.append(
            f"the owner of the local database in {unknown[0]} cannot be looked at, so it is "
            "not removed (`sudo calu uninstall` can remove it)"
        )
        return items, notes
    try:
        ids = runner(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            ]
        )
        if ids.returncode != 0:
            notes.append(
                "the local database could not be looked for: docker answered "
                f"{(ids.stderr or ids.stdout).strip()!r} (as yourself you may need to be in "
                "the docker group)"
            )
            return items, notes
        if ids.stdout.split():
            items.append(
                Item("container", COMPOSE_PROJECT, "the local database's container (Docker)")
            )
        if runner(["docker", "volume", "inspect", VOLUME]).returncode == 0:
            items.append(Item("volume", VOLUME, "the local database of an early calu (volume)"))
    except FileNotFoundError:
        notes.append("Docker is not installed, so there is no local database container")
    items += [Item("data", str(path), "the local database's files") for path in dirs]
    return items, notes


def _owner(path: Path) -> int | None:
    """Return the uid that owns a path.

    Args:
        path: The path.

    Returns:
        The owner's uid, or None if the path cannot be looked at.
    """
    try:
        return path.stat().st_uid
    except OSError:
        return None


def plan(
    euid: int,
    environ: Mapping[str, str],
    module_file: Path,
    runner: Runner | None = None,
    root_home: Path = ROOT_HOME,
) -> Plan:
    """Decide what `calu uninstall` removes on this machine, changing nothing.

    See the module docstring for the two tables.

    Args:
        euid: The effective uid of this process (0: the root installation's run).
        environ: The process environment: `HOME`, the XDG variables of a user's run, and
            `SUDO_USER` of a run through sudo.
        module_file: `calu.__file__`, to find the installation calu runs from.
        runner: Runs docker (to find the local database); None is `run_command`.
        root_home: The root installation's directory.

    Returns:
        The plan. Paths that do not exist are left out, except the bind-mount sources of the
        database container, which are listed as they are.
    """
    runner = runner or run_command
    result = Plan()
    defaults: list[Path] = []
    dirs: list[tuple[Path, str]] = []
    if euid == 0:
        dirs += [
            (Path("/etc/calu"), "root's logins and checker keys"),
            (Path("/var/lib/calu"), "root's state, logs and the place file"),
        ]
        defaults.append(Path("/var/lib/calu/db"))
        user = environ.get("SUDO_USER")
        if user and user != "root":
            try:
                home = Path(pwd.getpwnam(user).pw_dir)
            except KeyError:
                home = None
            if home is not None:
                config, state = _user_dirs(home, {})
                dirs += [
                    (config, f"{user}'s calu logins and checker keys"),
                    (state, f"{user}'s calu state and logs"),
                ]
                defaults.append(state / "db")
                own = home / ".local" / "share" / "uv" / "tools" / "calu"
                if (own / "uv-receipt.toml").is_file():
                    dirs.append((own, f"{user}'s own calu installation"))
                    dirs += [(path, f"{user}'s calu command") for path in _entry_points(own)]
    else:
        home = Path(environ.get("HOME") or Path.home())
        config, state = _user_dirs(home, environ)
        dirs += [(config, "your calu logins and checker keys"), (state, "your calu state and logs")]
        defaults.append(state / "db")
    items, notes = _database_items(runner, defaults, euid)
    result.items += items
    result.notes += notes
    result.items += [Item("path", str(path), what) for path, what in dirs if path.exists()]
    env = _tool_env(module_file)
    if env is None:
        result.notes.append(
            "the calu that runs this is no uv tool installation (a checkout, or another "
            "kind of installation): remove it the way it was installed"
        )
    elif euid == 0 and env.is_relative_to(root_home):
        commands = [path for path in _entry_points(env) if path.exists() or path.is_symlink()]
        result.items += [Item("path", str(path), "a calu command") for path in commands]
        result.items.append(Item("path", str(root_home), "the root installation of calu"))
    elif euid != 0 and env.is_relative_to(root_home):
        result.notes.append(
            f"the installation in {root_home} is root's: `sudo calu uninstall` removes it and "
            "root's files"
        )
    else:
        commands = [path for path in _entry_points(env) if path.exists() or path.is_symlink()]
        result.items += [Item("path", str(path), "a calu command") for path in commands]
        result.items.append(Item("path", str(env), "your calu installation (uv tool)"))
    if euid != 0 and (root_home / "calu").exists() and not _in_root(env, root_home):
        result.notes.append(
            f"root's installation in {root_home} and its files: `sudo calu uninstall`"
        )
    result.notes += [
        "prerequisites (uv, Docker and its postgres image, Claude Code, bubblewrap, socat, git)",
        "a database on another server (calu set it up there; its administrator drops it)",
        "the IDE extensions (remove them in the editor)",
        "what calu wrote into your projects (session traces, recorded edits, a git hook of "
        "`calu traces hook`)",
    ]
    return result


def _in_root(env: Path | None, root_home: Path) -> bool:
    """Tell whether a tool environment belongs to the root installation.

    Args:
        env: The tool environment calu runs from, or None.
        root_home: The root installation's directory.

    Returns:
        True if `env` lies inside `root_home`.
    """
    return env is not None and env.is_relative_to(root_home)


class NotConfirmed(Exception):
    """The confirmation was missing, wrong or too fast; nothing was removed."""


def confirm(
    read: Callable[[str], str] = input, clock: Callable[[], float] = time.monotonic
) -> None:
    """Ask the person to type `CONFIRM_PHRASE`, and refuse an answer that came too fast.

    Args:
        read: Shows a prompt and returns the line typed; `input` by default.
        clock: A monotonic clock in seconds.

    Raises:
        NotConfirmed: If the line is not exactly `CONFIRM_PHRASE`, arrived in less than
            `MIN_SECONDS` after the prompt, or the input ended.
    """
    started = clock()
    try:
        answer = read(f'Type "{CONFIRM_PHRASE}" to remove all of this: ')
    except EOFError as exc:
        raise NotConfirmed("no confirmation was given; nothing was removed") from exc
    elapsed = clock() - started
    if answer.strip() != CONFIRM_PHRASE:
        raise NotConfirmed(f'that is not "{CONFIRM_PHRASE}"; nothing was removed')
    if elapsed < MIN_SECONDS:
        raise NotConfirmed(
            f"the confirmation arrived after {elapsed:.1f} s, faster than a person types it, "
            "so it looks automated; nothing was removed. Run the command again and type the "
            "phrase by hand"
        )


def remove(
    chosen: Plan,
    compose_file: Path,
    image: str,
    euid: int,
    runner: Runner = run_command,
    report: Callable[[str], None] = print,
) -> list[str]:
    """Carry out a plan: the database first, the installation last.

    Side effects: `docker compose down --volumes` for the container, `docker volume rm` for
    the volume; for a data directory, a run of `image` that deletes its files when this
    process is not root (as root they are deleted directly); deletes files and directories.
    Each item's outcome is reported as one line: `removed`, `already gone` for an item that
    was not there any more, or `not removed` with the reason. A failure is reported and the
    other items are still removed.

    Args:
        chosen: The plan (`plan`).
        compose_file: The local database's compose file (`calu.db.local.compose_file`).
        image: The postgres image of the compose file, used to delete the database files.
        euid: The effective uid of this process.
        runner: Runs docker.
        report: Receives one line per item.

    Returns:
        One line per item that could not be removed; [] if everything went.
    """
    failed: list[str] = []
    order = {"container": 0, "volume": 1, "data": 2, "path": 3}
    for item in sorted(chosen.items, key=lambda one: order[one.kind]):
        try:
            deleted = _remove_item(item, compose_file, image, euid, runner)
        except OSError as exc:  # FileNotFoundError too: docker vanished meanwhile
            failed.append(f"{item.target}: {exc}")
            report(f"not removed: {item.target} ({exc})")
            continue
        report(f"{'removed' if deleted else 'already gone'}: {item.target}")
    return failed


def _remove_item(item: Item, compose_file: Path, image: str, euid: int, runner: Runner) -> bool:
    """Remove one item of a plan (see `remove`).

    Args:
        item: The item.
        compose_file: The local database's compose file.
        image: The postgres image of the compose file.
        euid: The effective uid of this process.
        runner: Runs docker.

    Returns:
        True if something was removed; False if the item was not there any more (a volume
        docker does not know, a path that is gone).

    Raises:
        OSError: If it could not be removed; the message is docker's or the system's.
    """
    if item.kind == "container":
        command = ["docker", "compose", "-f", str(compose_file), "-p", item.target, "down"]
        done = runner([*command, "--volumes", "--remove-orphans"])
        if done.returncode != 0:
            raise OSError((done.stderr or done.stdout).strip())
    elif item.kind == "volume":
        done = runner(["docker", "volume", "rm", item.target])
        if done.returncode != 0:
            if "no such volume" not in done.stderr.lower():
                raise OSError((done.stderr or done.stdout).strip())
            return False
    elif item.kind == "data":
        return _remove_data(Path(item.target), image, euid, runner)
    else:
        return _remove_path(Path(item.target))
    return True


def _remove_data(path: Path, image: str, euid: int, runner: Runner) -> bool:
    """Delete a local database's data directory, whose cluster files the container owns.

    Args:
        path: The data directory (it holds `pg`, the directory the container mounts).
        image: The postgres image, run once to delete files this user may not.
        euid: The effective uid of this process; root deletes them directly.
        runner: Runs docker.

    Returns:
        True if the directory was deleted; False if it was not there.

    Raises:
        OSError: If the files cannot be deleted.
    """
    if not path.exists():
        return False
    mounted = path / "pg" if (path / "pg").is_dir() else path
    if euid != 0:
        done = runner(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{mounted}:/data",
                "--entrypoint",
                "find",
                image,
                "/data",
                "-mindepth",
                "1",
                "-delete",
            ]
        )
        if done.returncode != 0:
            raise OSError((done.stderr or done.stdout).strip())
    shutil.rmtree(path)
    return True


def _remove_path(path: Path) -> bool:
    """Delete a file, a symbolic link or a directory tree.

    Args:
        path: What to delete; nothing happens if it is gone already.

    Returns:
        True if something was deleted; False if the path was gone already.

    Raises:
        OSError: If it cannot be deleted.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        return False
    return True


def compose_image(compose_file: Path) -> str:
    """Read the postgres image the local database's compose file names.

    Args:
        compose_file: The compose file.

    Returns:
        Its `image:` value, or "postgres:18" if none is found.
    """
    try:
        found = re.search(r"^\s*image:\s*(\S+)", compose_file.read_text(), re.MULTILINE)
    except OSError:
        found = None
    return found.group(1) if found else "postgres:18"
