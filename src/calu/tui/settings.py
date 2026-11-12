"""What the full-screen client remembers between runs: its settings and the sent texts.

    $XDG_CONFIG_HOME/calu/tui.toml          settings, written by hand or by `/theme` and `/bell`
    $XDG_STATE_HOME/calu/tui-history.jsonl  the texts sent from the composer, one JSON string
                                            per line, the newest last

`tui.toml`:

    theme = "calu-dark"      # any theme `/theme` lists
    bell = true              # ring the terminal bell when a step begins to wait
    toasts = true            # show a short notice for what happened elsewhere
    thinking = true          # show thinking in full (false: one line per block)
    panel_width = 36         # columns of the side panel on a wide terminal (28 to 60)
    git_tool = "lazygit"     # the program f4 starts in the project directory (a command line);
                             # the environment variable CALU_GIT_TOOL overrides it

    [keys]                   # other keys for the bindings, by binding id (the help page
    detach = "ctrl+q"        # lists the ids); several keys are separated by commas
    agent-next = "alt+right,alt+l"

Both files are optional. A file that cannot be read or a value of the wrong type is ignored
and named in `Settings.problems`, which the client shows once after its start; it never keeps
the client from starting. This module imports nothing from Textual.
"""

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HISTORY_LIMIT = 500
"""How many sent texts the history file keeps (the oldest are dropped)."""

PANEL_WIDTHS = (28, 60)
"""The narrowest and the widest side panel `panel_width` may ask for."""


@dataclass
class Settings:
    """The settings of `tui.toml`, with the defaults for what the file does not name."""

    theme: str = ""
    """The theme name; "" means the default theme."""
    bell: bool = True
    toasts: bool = True
    thinking: bool = True
    panel_width: int = 36
    git_tool: str = ""
    """The command line `f4` starts; "" picks the first installed one (`calu.tui.gittool`)."""
    keys: dict[str, str] = field(default_factory=dict)
    """Binding id -> the keys that replace its default, separated by commas."""
    problems: list[str] = field(default_factory=list)
    """What was wrong with the file, one sentence each; empty for a good or a missing file."""


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return where `tui.toml` is.

    Args:
        environ: The environment to read `XDG_CONFIG_HOME` from; None uses `os.environ`.

    Returns:
        `$XDG_CONFIG_HOME/calu/tui.toml`, or `~/.config/calu/tui.toml` without the variable.
    """
    env = os.environ if environ is None else environ
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "calu" / "tui.toml"


def history_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return where the sent texts are kept.

    Args:
        environ: The environment to read `XDG_STATE_HOME` from; None uses `os.environ`.

    Returns:
        `$XDG_STATE_HOME/calu/tui-history.jsonl`, or the same under `~/.local/state`.
    """
    env = os.environ if environ is None else environ
    base = (
        Path(env["XDG_STATE_HOME"])
        if env.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    return base / "calu" / "tui-history.jsonl"


def load(path: Path) -> Settings:
    """Read the settings file.

    Args:
        path: The file; it does not have to exist.

    Returns:
        The settings. Every value the file does not name, and every value of the wrong type,
        keeps its default; the latter is named in `problems`, as is a file that is no valid
        TOML.
    """
    settings = Settings()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return settings
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        settings.problems.append(f"{path} was not read: {error}")
        return settings
    wanted: dict[str, type] = {
        "theme": str,
        "bell": bool,
        "toasts": bool,
        "thinking": bool,
        "panel_width": int,
        "git_tool": str,
    }
    for name, kind in wanted.items():
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            settings.problems.append(f"{path.name}: {name} must be a {kind.__name__}")
            continue
        setattr(settings, name, value)
    low, high = PANEL_WIDTHS
    settings.panel_width = min(high, max(low, settings.panel_width))
    keys = raw.get("keys", {})
    if isinstance(keys, dict):
        for binding, value in keys.items():
            if isinstance(value, str) and value.strip():
                settings.keys[str(binding)] = value.strip()
            else:
                settings.problems.append(f"{path.name}: keys.{binding} must be a text")
    else:
        settings.problems.append(f"{path.name}: keys must be a table")
    return settings


def _toml_value(value: Any) -> str:
    """Write one value as TOML.

    Args:
        value: A bool, an int or a str.

    Returns:
        The TOML text of the value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def remember(path: Path, name: str, value: bool | int | str) -> str:
    """Write one top-level setting into the settings file and keep everything else in it.

    The line `name = ...` is replaced where it stands; if the file has none, the line is put
    before the first table (or at the end). Comments and the `[keys]` table stay as they are.
    Side effect: creates the directory and the file if they are missing.

    Args:
        path: The settings file.
        name: The setting, e.g. "theme".
        value: Its new value.

    Returns:
        "" if the file was written, else the sentence that says why not.
    """
    line = f"{name} = {_toml_value(value)}"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = text.splitlines()
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
        table = next(
            (index for index, row in enumerate(lines) if row.lstrip().startswith("[")), len(lines)
        )
        place = next((index for index, row in enumerate(lines[:table]) if pattern.match(row)), None)
        if place is None:
            lines.insert(table, line)
        else:
            lines[place] = line
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"the setting was not stored in {path}: {error}"
    return ""


def load_history(path: Path) -> list[str]:
    """Read the sent texts of earlier runs.

    Args:
        path: The history file; it does not have to exist.

    Returns:
        The texts, oldest first, at most `HISTORY_LIMIT`; an empty list for a missing or
        unreadable file. A line that is no JSON string is skipped.
    """
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return []
    texts: list[str] = []
    for row in rows:
        try:
            value = json.loads(row)
        except ValueError:
            continue
        if isinstance(value, str) and value.strip():
            texts.append(value)
    return texts[-HISTORY_LIMIT:]


def append_history(path: Path, text: str) -> None:
    """Add one sent text to the history file; a failure to write is ignored.

    Side effects: creates the directory and the file (mode 0600: a sent text can hold what
    the user would not want others on the machine to read) and cuts the file back to
    `HISTORY_LIMIT` lines once it has twice as many.

    Args:
        path: The history file.
        text: The text; an empty one is not stored.
    """
    if not text.strip():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as file:
            file.write(json.dumps(text) + "\n")
        rows = path.read_text(encoding="utf-8").splitlines()
        if len(rows) > 2 * HISTORY_LIMIT:
            path.write_text("\n".join(rows[-HISTORY_LIMIT:]) + "\n", encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return
