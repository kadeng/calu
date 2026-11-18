"""Command-line helper for the calu UI-testing desktop (container `calu-uitest`).

The container runs the Cua Linux desktop image. Its daemon `cua-spacesd`
listens on 127.0.0.1:56211 (host side) and executes screenshots, pointer and
keyboard input and shell commands inside the XFCE desktop session of the user
`cua`. This helper talks to that daemon through the Cua Python SDK (package
`cua` on PyPI).

Run it from anywhere on the host::

    uv run --no-project --with cua python calu/uitest/desktop.py <command> [args]

Commands::

    screenshot <out.png>      save a PNG of the whole desktop
    click <x> <y>             left click at pixel (x, y); (0, 0) is top left
    type <text>               type text into the focused window
    key <combo>               press a key or chord: enter, ctrl+shift+p, alt+F4
    shell <command>           run a shell line as user `cua` (DISPLAY=:1), print output
    open-vscode [folder]      start VSCode (default folder /workspace/calu)
    open-pycharm [folder]     start PyCharm (default: no folder, welcome screen)
    windows                   list the titles of the open windows
    viewer-url                print a browser link to watch and use the desktop

Configuration (environment variables)::

    CALU_UITEST_URL         daemon URL, default http://127.0.0.1:56211
    CALU_UITEST_TOKEN_FILE  token file, default ~/.cache/calu-uitest/token
                            (written by run.sh)

Exit status: 0 on success; `shell` exits with the remote command's exit code;
1 when the desktop is not reachable or a command fails.

The SDK keeps its own state in ~/.cache/calu-uitest/cua-home (not ~/.cua), and
its telemetry is switched off (DO_NOT_TRACK=1) before the SDK is imported.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

STATE_DIR = Path.home() / ".cache" / "calu-uitest"
DEFAULT_URL = "http://127.0.0.1:56211"
SANDBOX_NAME = "calu-uitest"
DEFAULT_VSCODE_FOLDER = "/workspace/calu"

# Both must be set before `import cua`: the SDK reads them when it is loaded.
os.environ["DO_NOT_TRACK"] = "1"
os.environ.setdefault("CUA_HOME", str(STATE_DIR / "cua-home"))

import cua  # ruff: ignore[module-import-not-at-top-of-file]  (import after the environment is prepared)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class DesktopError(RuntimeError):
    """The desktop is not reachable, or a desktop command failed."""


def read_token() -> str:
    """Read the access token of cua-spacesd from the token file.

    Returns:
        The token without surrounding whitespace.

    Raises:
        DesktopError: The token file does not exist or is empty (run.sh
            creates it).
    """
    path = Path(os.environ.get("CALU_UITEST_TOKEN_FILE", STATE_DIR / "token"))
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DesktopError(
            f"cannot read the token file {path}: run calu/uitest/run.sh first"
        ) from exc
    if not token:
        raise DesktopError(f"the token file {path} is empty: delete it and run calu/uitest/run.sh")
    return token


async def connect() -> tuple[cua.Sandbox, cua.SpacesdClient]:
    """Connect to cua-spacesd in the running container.

    Returns:
        The SDK's sandbox handle (needed for the viewer link) and the spacesd
        client (screenshots, input, processes).

    Raises:
        DesktopError: The token is missing, or the daemon does not answer
            (container stopped, wrong port or wrong token).
    """
    url = os.environ.get("CALU_UITEST_URL", DEFAULT_URL)
    token = read_token()
    try:
        sandbox = await cua.embedded().sandboxes().connect_url(url, token, SANDBOX_NAME)
        return sandbox, await sandbox.spacesd(10_000)
    except Exception as exc:  # the SDK raises its own generated error types
        raise DesktopError(
            f"cannot reach the desktop at {url}: {exc} (is calu/uitest/run.sh done?)"
        ) from exc


async def screenshot(out_path: str) -> str:
    """Save a PNG screenshot of the whole desktop to a host file.

    Args:
        out_path: Host path of the PNG file. Parent directories are created;
            an existing file is overwritten.

    Returns:
        A line "<absolute path> <width>x<height>".

    Raises:
        DesktopError: The desktop is not reachable.
        OSError: The file cannot be written.
    """
    _, env = await connect()
    shot = await env.screenshot(cua.ScreenshotOptions(format=cua.ImageFormat.PNG))
    path = Path(out_path).expanduser().resolve()  # ruff: ignore[blocking-path-method-in-async-function]  (one-shot CLI, nothing else runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(shot.image)
    return f"{path} {shot.width}x{shot.height}"


async def click(x: float, y: float) -> str:
    """Move the pointer to a pixel position and press the left button once.

    Args:
        x: Horizontal pixel position; 0 is the left edge of the screenshot.
        y: Vertical pixel position; 0 is the top edge of the screenshot.

    Returns:
        A line "clicked <x>,<y>".

    Raises:
        DesktopError: The desktop is not reachable.
    """
    _, env = await connect()
    await env.click(x, y)
    return f"clicked {x:g},{y:g}"


async def type_text(text: str) -> str:
    """Type text into the window that has the keyboard focus.

    Args:
        text: The characters to type. Newlines are not converted to Enter;
            use the `key enter` command for that.

    Returns:
        A line "typed <n> characters".

    Raises:
        DesktopError: The desktop is not reachable.
    """
    _, env = await connect()
    await env.type_text(text)
    return f"typed {len(text)} characters"


async def key(combo: str) -> str:
    """Press one key or a chord in the focused window.

    Args:
        combo: Key names joined with "+", modifiers first. Examples: "enter",
            "escape", "F1", "ctrl+s", "ctrl+shift+p". Key names follow
            cua-driver (xdotool-like names, case-insensitive modifiers).

    Returns:
        A line "pressed <combo>".

    Raises:
        DesktopError: The desktop is not reachable.
        ValueError: `combo` is empty or has an empty part ("ctrl++p").
    """
    keys = [part.strip() for part in combo.split("+")]
    if not all(keys):
        raise ValueError(f"not a key combination: {combo!r}")
    _, env = await connect()
    if len(keys) == 1:
        await env.press(keys[0])
    else:
        await env.hotkey(keys)
    return f"pressed {combo}"


async def shell(command: str, timeout_s: float = 120.0) -> tuple[int, str]:
    """Run a shell line inside the desktop session and collect its output.

    The line runs with `bash -lc` as the desktop user `cua`, with DISPLAY=:1,
    so GUI programs started here appear on the desktop. The call waits for
    the command to end; start long-running GUI programs with `setsid -f`.

    Args:
        command: The shell line, e.g. "uv --version".
        timeout_s: Seconds after which the daemon kills the command.

    Returns:
        (exit code, text). The text is stdout followed by stderr, decoded as
        UTF-8 with replacement characters. The exit code is 124 when the
        command timed out and 1 when it ended without a code (signal).

    Raises:
        DesktopError: The desktop is not reachable.
    """
    _, env = await connect()
    out = await env.sh(command, int(timeout_s * 1000))
    text = out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")
    if out.exit.timed_out:
        return 124, text + f"\n[timed out after {timeout_s:g} s]"
    return (out.exit.code if out.exit.code is not None else 1), text


async def start_detached(argv: list[str], log_name: str) -> str:
    """Start a GUI program in the desktop session without waiting for it.

    Args:
        argv: Program and arguments, e.g. ["uitest-code", "/workspace/calu"].
        log_name: File name under /tmp in the container that receives the
            program's stdout and stderr, e.g. "uitest-code.log".

    Returns:
        A line "started: <command> (log: /tmp/<log_name> in the container)".

    Raises:
        DesktopError: The desktop is not reachable, or the program could not
            be started (for example: launcher missing in the image).
    """
    line = f"setsid -f {shlex.join(argv)} >/tmp/{shlex.quote(log_name)} 2>&1 </dev/null"
    code, text = await shell(line, timeout_s=30)
    if code != 0:
        raise DesktopError(f"could not start {argv[0]} (exit {code}): {text.strip()}")
    return f"started: {shlex.join(argv)} (log: /tmp/{log_name} in the container)"


async def open_vscode(folder: str | None = None) -> str:
    """Start VSCode in the desktop on a folder.

    Uses the image's launcher `uitest-code` (code --no-sandbox, profile in
    /home/cua/.vscode-uitest). The window needs a few seconds to appear.

    Args:
        folder: Folder path inside the container. None opens
            /workspace/calu (the read-only repository mount).

    Returns:
        The "started: ..." line of `start_detached`.

    Raises:
        DesktopError: The desktop is not reachable or the launcher failed.
    """
    return await start_detached(["uitest-code", folder or DEFAULT_VSCODE_FOLDER], "uitest-code.log")


async def open_pycharm(folder: str | None = None) -> str:
    """Start PyCharm in the desktop, optionally on a project folder.

    Uses the image's launcher `uitest-pycharm`. The first start shows
    JetBrains' own dialogs (user agreement, data sharing, licence); this
    helper answers none of them. PyCharm writes `.idea` into the project, so
    the folder must be writable (not /workspace/calu).

    Args:
        folder: Project folder inside the container. None shows the welcome
            screen.

    Returns:
        The "started: ..." line of `start_detached`.

    Raises:
        DesktopError: The desktop is not reachable or the launcher failed.
    """
    return await start_detached(
        ["uitest-pycharm", *([folder] if folder else [])], "uitest-pycharm.log"
    )


async def windows() -> str:
    """List the titles of the windows on the desktop.

    Returns:
        One line per window: "<window id>  <desktop>  <host>  <title>"
        (the output of `wmctrl -l`), or the tool's error text.

    Raises:
        DesktopError: The desktop is not reachable.
    """
    _, text = await shell(
        "wmctrl -l 2>&1 || xdotool search --onlyvisible --name . getwindowname %@", 20
    )
    return text.rstrip()


async def viewer_url(ttl_s: int = 8 * 3600, *, view_only: bool = False) -> str:
    """Create a browser link to the desktop's HTML5 viewer.

    The link points to http://127.0.0.1:56211/viewer/ and carries a viewer
    ticket in its fragment. The ticket permits only the viewer's calls
    (video, input, clipboard, file upload), not process execution.

    Args:
        ttl_s: Lifetime of the link in seconds (the daemon allows at most 24 h).
        view_only: True makes a link that can watch but not click or type.

    Returns:
        The URL.

    Raises:
        DesktopError: The desktop is not reachable.
    """
    sandbox, _ = await connect()
    link = await sandbox.viewer_url(cua.ViewerOptions(ttl_seconds=ttl_s, view_only=view_only))
    return link.url


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser with one sub-command per helper command.

    Returns:
        The parser; `parse_args()` yields a namespace with `command` and the
        command's own arguments.
    """
    parser = argparse.ArgumentParser(
        description="Drive the calu UI-testing desktop (container calu-uitest)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("screenshot", help="save a PNG of the desktop").add_argument("out")
    p = sub.add_parser("click", help="left click at pixel x y")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    sub.add_parser("type", help="type text into the focused window").add_argument("text")
    sub.add_parser("key", help="press a key or chord, e.g. ctrl+shift+p").add_argument("combo")
    p = sub.add_parser("shell", help="run a shell line in the desktop session")
    p.add_argument("line")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds (default 120)")
    sub.add_parser("open-vscode", help="start VSCode").add_argument("folder", nargs="?")
    sub.add_parser("open-pycharm", help="start PyCharm").add_argument("folder", nargs="?")
    sub.add_parser("windows", help="list window titles")
    p = sub.add_parser("viewer-url", help="print a browser link to the desktop")
    p.add_argument("--ttl", type=int, default=8 * 3600, help="seconds (default 8 h)")
    p.add_argument("--view-only", action="store_true")
    return parser


async def run(args: argparse.Namespace) -> int:
    """Execute one parsed command and print its result to stdout.

    Args:
        args: The namespace from `build_parser().parse_args()`.

    Returns:
        The process exit status: the remote exit code for `shell`, else 0.

    Raises:
        DesktopError: The desktop is not reachable or the command failed.
        ValueError: A `key` combination is malformed.
        OSError: A screenshot file cannot be written.
    """
    if args.command == "shell":
        code, text = await shell(args.line, args.timeout)
        sys.stdout.write(text)
        return code
    simple: dict[str, Callable[[], Awaitable[str]]] = {
        "screenshot": lambda: screenshot(args.out),
        "click": lambda: click(args.x, args.y),
        "type": lambda: type_text(args.text),
        "key": lambda: key(args.combo),
        "open-vscode": lambda: open_vscode(args.folder),
        "open-pycharm": lambda: open_pycharm(args.folder),
        "windows": windows,
        "viewer-url": lambda: viewer_url(args.ttl, view_only=args.view_only),
    }
    print(await simple[args.command]())
    return 0


def main() -> int:
    """Parse the command line, run the command, and map errors to exit status 1.

    Returns:
        The process exit status (see the module docstring).
    """
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except (DesktopError, ValueError, OSError) as exc:
        print(f"desktop.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
