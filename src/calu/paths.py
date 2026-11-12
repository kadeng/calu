"""Where calu keeps its own secrets and state on this machine.

This module imports nothing from calu, so that both the configuration (`calu.config`, which
puts these directories on the sandbox's read deny list) and the rules (`calu.checkers.rules`,
whose built-in rule makes a tool call on them wait for a human) can use it.

    $XDG_CONFIG_HOME/calu   credential store, env file with the checker API keys
    $XDG_STATE_HOME/calu    snapshot repositories (`calu.changes.default_root`)

The literal defaults `~/.config/calu` and `~/.local/state/calu` always count as well, whatever
the two variables say.
"""

import os
from collections.abc import Mapping
from pathlib import Path


def calu_private_dirs(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> list[Path]:
    """Return calu's own directories as absolute paths, each also with its links resolved.

    The value of an XDG variable is taken the way calu itself uses it
    (`calu.credentials.default_path`, `calu.changes.default_root`): a relative value is relative
    to calu's working directory, and `..` segments are removed. Where a directory is reached
    through a symbolic link, the link's target is listed too, so that the directory is
    recognised under both names.

    Args:
        environ: The environment to read `XDG_CONFIG_HOME` and `XDG_STATE_HOME` from; None
            uses `os.environ`.
        home: The home directory; None uses `Path.home()`.

    Returns:
        Absolute, normalised paths without duplicates: `<home>/.config/calu` and
        `<home>/.local/state/calu` first, then the directories the variables name, then the
        resolved form of each where it differs. None of them has to exist.

    Example:
        With `XDG_CONFIG_HOME=cfg` and the working directory `/work`, the list holds
        `/work/cfg/calu`.
    """
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    named = [home / ".config" / "calu", home / ".local" / "state" / "calu"]
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        value = env.get(variable)
        if value:
            named.append(Path(value) / "calu")
    directories: list[Path] = []
    for directory in named:
        absolute = Path(os.path.abspath(directory))
        if absolute not in directories:
            directories.append(absolute)
    for directory in list(directories):
        try:
            resolved = directory.resolve()
        except OSError, RuntimeError:
            continue  # a link loop: the unresolved name is listed, nothing more can be said
        if resolved not in directories:
            directories.append(resolved)
    return directories


def calu_private_paths(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return calu's own directories in the form Claude Code's sandbox settings expect.

    `calu.config.SandboxConfig.settings` adds these paths to the sandbox's read deny list
    whatever the configured `deny_read` holds: the protection of calu's own files is not a
    setting.

    Args:
        environ: The environment to read `XDG_CONFIG_HOME` and `XDG_STATE_HOME` from; None
            uses `os.environ`.

    Returns:
        `calu_private_dirs(environ)` as text, in the same order. A path below the home
        directory is written with "~", every other one as an absolute path; no entry is
        relative, because Claude Code would resolve it against its own directory.

    Example:
        With `XDG_CONFIG_HOME=/srv/cfg` and no `XDG_STATE_HOME`:
        `["~/.config/calu", "~/.local/state/calu", "/srv/cfg/calu"]`.
    """
    home = Path.home()
    paths: list[str] = []
    for directory in calu_private_dirs(environ, home):
        try:
            text = "~/" + directory.relative_to(home).as_posix()
        except ValueError:
            text = directory.as_posix()
        if text not in paths:
            paths.append(text)
    return paths


def calu_private_dir_of(path: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """Return the directory of calu's own that `path` is, or lies in.

    Args:
        path: An absolute path; `..` segments are removed before the comparison, and the path
            is also compared with its links resolved.
        environ: The environment to read the XDG variables from; None uses `os.environ`.

    Returns:
        The first of `calu_private_dirs(environ)` that equals `path` or is one of its parents,
        else None.

    Example:
        With `XDG_CONFIG_HOME=/srv/cfg`, `calu_private_dir_of(Path("/srv/cfg/calu/.env"))` is
        `Path("/srv/cfg/calu")`, and `calu_private_dir_of(Path("/srv/cfg/calumet"))` is None.
    """
    forms = [Path(os.path.abspath(path))]
    try:
        resolved = path.resolve()
    except OSError, RuntimeError:
        resolved = forms[0]
    if resolved not in forms:
        forms.append(resolved)
    for directory in calu_private_dirs(environ):
        for form in forms:
            if form == directory or directory in form.parents:
                return directory
    return None
