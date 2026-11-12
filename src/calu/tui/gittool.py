"""Which terminal git program `f4` starts, and how to install one that is missing.

The full-screen client has no git viewer of its own. `f4` hands the terminal to an existing
program in the session's project directory and takes it back when that program ends. This
module decides which program that is and what to tell a user who has none; it starts nothing
and imports nothing from Textual.

The program, by priority:

1. the environment variable `CALU_GIT_TOOL` (a command line, e.g. `lazygit` or `tig status`);
2. `git_tool` in `tui.toml`;
3. gitui, the default, if it is installed (on the search path or in `~/.local/bin`);
4. `git diff` through a pager (`FALLBACK`), if git is installed, after the client offered to
   install gitui.

gitui (https://github.com/gitui-org/gitui) is the default, the owner's choice: the diff of the
working copy, staging, log and branches in a fast terminal program. Another program (lazygit,
tig, ...) is used only when it is configured. When gitui is not installed, `install_plan`
names a command that installs it on this machine. The client runs such a command only after
an explicit yes and only if it needs no rights of the administrator; a command with `sudo` is
only shown.
"""

import platform
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ENV = "CALU_GIT_TOOL"
"""The environment variable that names the program as a command line."""

KNOWN = ("gitui",)
"""The programs tried when none is configured, in this order."""

RECOMMENDED = "gitui"
"""The program the help text and the install hint name."""

RELEASES = "https://github.com/gitui-org/gitui/releases"
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
    ("pacman", "sudo pacman -S gitui", False, ""),
    ("zypper", "sudo zypper install gitui", False, ""),
    ("cargo", "cargo install gitui --locked", True, "This compiles gitui, which takes minutes."),
)
"""Package manager, command, whether the client may run it, a note; the first installed one
is used when neither brew nor the release command applies. Debian, Ubuntu and Fedora carry no
gitui package, so they are not listed. `cargo install` writes into the user's own directory
and comes last."""

FALLBACK = ("git", "-c", "core.pager=less -R -+F -+X", "diff")
"""The command line without a git program: the unstaged changes, in `less`.

`-+F` and `-+X` switch off two options git sets for `less` by default (`LESS=FRX`): with them
`less` ends at once when the text fits on one screen, and the diff is gone when the client
draws itself again (seen in a real terminal). Without changes git prints nothing and starts
no pager; `unstaged_changes` says so beforehand. (`git diff --exit-code` would report it, but
git never starts a pager with that option.)
"""


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
        local = LOCAL_BIN / name
        if which(str(local)) is not None:
            # Installed by `install_plan`'s release command, with ~/.local/bin not on the PATH.
            return GitTool((str(local),), "installed")
    if which("git") is not None:
        return GitTool(FALLBACK, "fallback")
    return Missing("git is not installed on this machine")


def install_plan(which: Which = shutil.which) -> InstallPlan:
    """Say how to install the recommended program on this machine.

    Args:
        which: Finds a program on the search path; used to detect the package manager.

    Returns:
        `brew install gitui` where brew is installed; else, on Linux with curl and tar, a
        command that puts gitui's release binary into `~/.local/bin` (`release_command`);
        else the command of the first package manager found (pacman, zypper, then
        `cargo install`); else the download page of the release binaries. Only a command without
        `sudo` is marked `runnable`.
    """
    if which("brew") is not None:
        return InstallPlan("brew install gitui", "brew", True)
    release = release_command(which)
    if release is not None:
        return InstallPlan(
            release,
            "release",
            True,
            f"This downloads gitui's release binary from GitHub into {LOCAL_BIN}; your "
            "package manager may have it as well.",
        )
    for manager, command, runnable, note in _PLANS:
        if which(manager) is not None:
            return InstallPlan(command, manager, runnable, note)
    return InstallPlan(
        f"download the binary for this machine from {RELEASES} and put it on the PATH",
        "release",
        False,
    )


LOCAL_BIN = Path.home() / ".local" / "bin"
"""Where the release command puts gitui: a directory of the user, so no `sudo` is needed."""

RELEASE_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
"""`platform.machine()` -> the architecture name in gitui's release file names."""


def release_command(which: Which = shutil.which) -> str | None:
    """Build the command that installs gitui's newest release binary for this user.

    The command downloads the Linux archive of gitui's newest release for this machine's
    architecture from GitHub and unpacks the one file `gitui` into `LOCAL_BIN`. It changes
    nothing outside that directory and needs no rights of the administrator.

    Args:
        which: Finds a program on the search path.

    Returns:
        The command line (`sh -c '...'`), or None where it would not work: not Linux, an
        architecture gitui publishes no archive under that name for, or no curl or tar.
    """
    arch = RELEASE_ARCH.get(platform.machine().lower())
    if platform.system() != "Linux" or arch is None:
        return None
    if which("curl") is None or which("tar") is None or which("sh") is None:
        return None
    archive = f"{RELEASES}/latest/download/gitui-linux-{arch}.tar.gz"
    script = (
        f'set -e; mkdir -p "{LOCAL_BIN}"; curl -fsSL "{archive}" | tar -xz -C "{LOCAL_BIN}" ./gitui'
    )
    return "sh -c " + shlex.quote(script)


def unstaged_changes(directory: str) -> bool | None:
    """Ask git whether `FALLBACK` would show anything (`git diff --quiet`).

    Args:
        directory: The directory to ask in.

    Returns:
        True if the working copy has unstaged changes, False if it has none, None if git
        could not say (no repository, git missing, no answer within 10 seconds).
    """
    try:
        done = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return {0: False, 1: True}.get(done.returncode)


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
