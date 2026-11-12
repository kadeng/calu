"""Which terminal git program `f4` starts, and how to install one that is missing.

The full-screen client has no git viewer of its own. `f4` hands the terminal to an existing
program in the session's project directory and takes it back when that program ends. This
module decides which program that is and what to tell a user who has none; it starts nothing
and imports nothing from Textual.

The program, by priority:

1. the environment variable `CALU_GIT_TOOL` (a command line, e.g. `lazygit` or `tig status`);
2. `git_tool` in `tui.toml`;
3. the first of `KNOWN` that is installed: lazygit, gitui, tig;
4. `git diff` through git's pager, if git is installed.

lazygit is the recommended program: staging, the diff of the working copy, log and branches
on one screen. When none of `KNOWN` is installed, `install_plan` names the command that
installs lazygit with the package manager of this machine. A command that needs the rights of
the administrator is only shown; the client never runs `sudo`.
"""

import shlex
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ENV = "CALU_GIT_TOOL"
"""The environment variable that names the program as a command line."""

KNOWN = ("lazygit", "gitui", "tig")
"""The programs tried when none is configured, in this order."""

RECOMMENDED = "lazygit"
"""The program the help text and the install hint name."""

RELEASES = "https://github.com/jesseduffield/lazygit/releases"
"""Where the release binaries of the recommended program are."""

Which = Callable[[str], str | None]
"""Finds a program on the search path: its full path, or None (`shutil.which`)."""


@dataclass(frozen=True)
class GitTool:
    """The program `f4` starts."""

    argv: tuple[str, ...]
    source: str
    """Why this one: "CALU_GIT_TOOL", "tui.toml", "installed" or "fallback" (`git diff`)."""

    @property
    def name(self) -> str:
        """The program's name.

        Returns:
            The file name of the first word of the command line.
        """
        return Path(self.argv[0]).name


@dataclass(frozen=True)
class Missing:
    """Why no program can be started."""

    reason: str


@dataclass(frozen=True)
class InstallPlan:
    """How to install the recommended program on this machine."""

    command: str
    """The command line, as the user would type it."""
    manager: str
    """The package manager it uses, or "release" when there is only the download page."""
    runnable: bool
    """True if the client may run the command after an explicit yes: it needs no rights of
    the administrator. False: the command is shown for the user to run."""
    note: str = ""
    """One more sentence for the user ("" if none)."""


_PLANS: tuple[tuple[str, str, bool, str], ...] = (
    ("brew", "brew install lazygit", True, ""),
    ("pacman", "sudo pacman -S lazygit", False, ""),
    ("dnf", "sudo dnf install lazygit", False, "Fedora 41 and later carry the package."),
    ("zypper", "sudo zypper install lazygit", False, ""),
    (
        "apt",
        "sudo apt install lazygit",
        False,
        "Debian 13 and Ubuntu 25.10 and later carry the package; on older releases "
        "sudo apt install tig works.",
    ),
    ("go", "go install github.com/jesseduffield/lazygit@latest", True, ""),
)
"""Package manager, command, whether the client may run it, a note; the first installed one
is used. `go install` writes into the user's own directory and comes last."""


def configured(environ: Mapping[str, str], setting: str = "") -> tuple[str, str]:
    """Return the configured command line and where it comes from.

    Args:
        environ: The environment (`CALU_GIT_TOOL`).
        setting: `git_tool` of `tui.toml`, or "".

    Returns:
        `(command line, source)`; `("", "")` if neither names one.
    """
    if environ.get(ENV, "").strip():
        return environ[ENV].strip(), ENV
    if setting.strip():
        return setting.strip(), "tui.toml"
    return "", ""


def choose(
    environ: Mapping[str, str], setting: str = "", which: Which = shutil.which
) -> GitTool | Missing:
    """Decide which program `f4` starts.

    Args:
        environ: The environment (`CALU_GIT_TOOL`).
        setting: `git_tool` of `tui.toml`, or "".
        which: Finds a program on the search path.

    Returns:
        The program by the priority of the module docstring. `Missing` if a configured
        command line cannot be read or its program is not installed (the reason names the
        setting), or if not even git is installed.
    """
    line, source = configured(environ, setting)
    if line:
        try:
            argv = tuple(shlex.split(line))
        except ValueError as error:
            return Missing(f"{source} is no command line: {error}")
        if not argv:
            return Missing(f"{source} is empty")
        if which(argv[0]) is None:
            return Missing(f"{source} names {argv[0]}, which is not installed")
        return GitTool(argv, source)
    for name in KNOWN:
        if which(name) is not None:
            return GitTool((name,), "installed")
    if which("git") is not None:
        return GitTool(("git", "-c", "core.pager=less -R", "diff"), "fallback")
    return Missing("git is not installed on this machine")


def install_plan(which: Which = shutil.which) -> InstallPlan:
    """Say how to install the recommended program on this machine.

    Args:
        which: Finds a program on the search path; used to detect the package manager.

    Returns:
        The command of the first package manager found (brew, pacman, dnf, zypper, apt, then
        `go install`), or the download page of the release binaries when none is found. Only
        a command without `sudo` is marked `runnable`.
    """
    for manager, command, runnable, note in _PLANS:
        if which(manager) is not None:
            return InstallPlan(command, manager, runnable, note)
    return InstallPlan(
        f"download the binary for this machine from {RELEASES} and put it on the PATH",
        "release",
        False,
    )


def usable_directory(workspace: str) -> str | None:
    """Say why a program cannot be started in the session's project directory.

    Args:
        workspace: The project directory the session names.

    Returns:
        None if the directory exists on this machine, else the sentence to show.
    """
    if workspace and Path(workspace).is_dir():
        return None
    where = workspace or "the session's project directory"
    return (
        f"{where} is not on this machine: the git program needs a terminal on the session's machine"
    )
