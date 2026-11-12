"""Write screenshots of the full-screen client: `uv run python scripts/tui_screenshots.py DIR`.

The client runs headless on a scripted session (`calu.tui.fake`, `calu.tui.scenarios`) with a
clock the script controls, so the pictures are the same on every run. Each state is written as
SVG (Textual's own export) at 120x36 and 80x24; if a headless Chromium is available through
Playwright (`scripts/get-browser.sh` installs it without root) each SVG is also rendered to a
PNG next to it.

The states: a finished turn, an open escalation, a session with subagents and a question, a
subagent's own tab, a step expanded to its diff, the full-screen detail of an escalation, a
set of three questions, a side question with its answer, the transcript scrolled up while
output arrives, the side panel over the transcript on a narrow terminal, the help page, and the
light theme.
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.pilot import Pilot

from calu.tui import scenarios
from calu.tui.app import CaluApp, TuiOptions
from calu.tui.fake import FakeSession, fake_connector
from calu.tui.theme import LIGHT

SIZES = ((120, 36), (80, 24))
"""The terminal sizes every state is drawn at: a wide one and the classic default."""


class Clock:
    """A clock that only moves when the script says so."""

    def __init__(self) -> None:
        """Start at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The current time in seconds.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: By how much.
        """
        self.now += seconds


CONTEXT = {
    "recent": (
        "**User:** Rename the helper in gate.py and run the unit tests.\n\n"
        "- Read: read gate.py\n"
        "- Edit: renamed check_step to judge_step in gate.py\n"
        "- Bash: ran the unit tests: 412 passed\n"
        "- Bash: read a credentials file (denied: the task does not need this file)\n\n"
        "Claude said that the rename is complete and that it would now publish the branch."
    ),
    "chat_summary": "The user works on the step gate of the project. Nothing was folded yet.",
    "checker_runs": [
        {
            "checker_name": "command-checker",
            "checker_version": 7,
            "model": "fireworks:nemotron-lightning",
            "decision": "escalate",
            "reason": "A force push rewrites the history of main; the user asked for a rename"
            " and a test run, not for publishing.",
            "input_tokens": 4120,
            "output_tokens": 96,
            "duration_ms": 840,
        }
    ],
}
"""What the scripted session answers to `escalation.context` in the pictures."""

Drive = Callable[[Pilot[None], CaluApp, FakeSession, Clock], Awaitable[None]]
"""What a state does after its scenario was played: keys, further events."""


async def nothing(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
    """Leave the screen as the scenario left it.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """


def keys(*pressed: str) -> Drive:
    """Build a state that presses keys after its scenario.

    Args:
        *pressed: The keys, in order.

    Returns:
        The drive function.
    """

    async def drive(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
        """Press the keys.

        Args:
            pilot: The pilot of the running app.
            app: The app.
            session: The scripted session.
            clock: The clock.
        """
        for key in pressed:
            await pilot.press(key)
            await pilot.pause()

    return drive


async def side_question(
    pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock
) -> None:
    """Ask a side question while the escalation is open.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    question = "Which callers of check_step did you update?"
    session.side_answers[question] = (
        "Two, both in `src/gate.py`:\n\n"
        "- `gate()`, which returns whether the step may run\n"
        "- the module's doctest\n\n"
        "No caller outside `src/` exists; the `explore` subagent is still checking `docs/`."
    )
    await pilot.press("f3")
    await pilot.pause()
    for character in question:
        await pilot.press(character if character != " " else "space")
    await pilot.press("enter")
    await pilot.pause(0.2)
    for character in "And the tests?":
        await pilot.press(character if character != " " else "space")
    await pilot.pause()


async def scrolled_up(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
    """Scroll up, then let more output arrive below.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    await pilot.press("pageup")
    await pilot.pause()
    more = [
        *scenarios.user("Now update the changelog."),
        *scenarios.text("I add an entry for the rename to `CHANGELOG.md`."),
        *scenarios.step(
            "u1", "Read", {"file_path": "CHANGELOG.md"}, "read CHANGELOG.md", route="allow"
        ),
    ]
    await scenarios.play(session, more, clock.advance)
    await pilot.pause()


async def answered_questions(
    pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock
) -> None:
    """Answer the first question and choose two options of the second.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    for key in ("1", "1", "3"):
        await pilot.press(key)
        await pilot.pause()


STATES: list[tuple[str, str, Drive, str]] = [
    ("01-turn", "simple_turn", nothing, ""),
    ("02-escalation", "escalation", nothing, ""),
    ("03-subagents-question", "showcase", nothing, ""),
    ("04-subagent-tab", "showcase", keys("alt+right"), ""),
    ("05-step-expanded", "simple_turn", keys("ctrl+up", "k", "k", "k", "enter"), ""),
    ("06-escalation-detail", "escalation", keys("w"), ""),
    ("07-questions", "question_multi", answered_questions, ""),
    ("08-side-question", "escalation", side_question, ""),
    ("09-scrolled-up", "long_session", scrolled_up, ""),
    ("10-panel-narrow", "showcase", keys("f2"), ""),
    ("11-help", "escalation", keys("f1"), ""),
    ("12-light", "showcase", nothing, LIGHT),
    ("13-armed-allow", "escalation", keys("a"), ""),
]
"""`(file name, scenario, what to do after it, theme)` per state."""


async def shoot(name: str, scenario: str, drive: Drive, theme: str, size: tuple[int, int]) -> str:
    """Run the client through one state and export the screen.

    Args:
        name: The state's name, for the title in the picture.
        scenario: A key of `calu.tui.scenarios.SCENARIOS`.
        drive: What to do after the scenario was played.
        theme: A theme name; "" for the default.
        size: Terminal width and height.

    Returns:
        The screen as an SVG document.
    """
    clock = Clock()
    session = FakeSession()
    items = scenarios.SCENARIOS[scenario]()
    if scenario == "long_session":
        items = scenarios.long_session(60)
    session.results["escalation.context"] = CONTEXT
    options = TuiOptions(theme=theme) if theme else TuiOptions()
    app = CaluApp(fake_connector(session), options, clock=clock, live=False)
    async with app.run_test(size=size, notifications=False) as pilot:
        await pilot.pause()
        await scenarios.play(session, items, clock.advance)
        if session.escalations and scenario == "escalation":
            clock.advance(42.0)  # the step has been waiting for a while
        await pilot.pause()
        app.render_all()
        await drive(pilot, app, session, clock)
        await pilot.pause()
        return app.export_screenshot(title=f"calu attach · {name} · {size[0]}x{size[1]}")


async def to_png(svgs: list[Path]) -> bool:
    """Render SVG files to PNG files beside them with a headless Chromium.

    Args:
        svgs: The SVG files.

    Returns:
        True if the PNGs were written; False if Playwright or the browser is missing.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False
    libs = Path(os.environ.get("CALU_BROWSER_LIBS", Path.home() / ".cache" / "calu-browser-libs"))
    libs = libs / "root" / "usr" / "lib" / "x86_64-linux-gnu"
    environment: dict[str, Any] = dict(os.environ)
    if libs.is_dir():
        environment["LD_LIBRARY_PATH"] = f"{libs}:{os.environ.get('LD_LIBRARY_PATH', '')}"
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(env=environment)
        except Exception as exc:
            print(f"no headless browser ({type(exc).__name__}): SVG only", file=sys.stderr)
            return False
        page = await browser.new_page(device_scale_factor=1.5)
        for svg in svgs:
            await page.set_content(
                f"<body style='margin:0;background:#000'>{svg.read_text(encoding='utf-8')}</body>"
            )
            picture = page.locator("svg").first
            await picture.screenshot(path=str(svg.with_suffix(".png")))
        await browser.close()
    return True


async def main(directory: Path, only: str = "") -> int:
    """Write every state at every size.

    Args:
        directory: Where the files go; it must exist.
        only: If not empty, only the states whose name contains this text.

    Returns:
        0.
    """
    written: list[Path] = []
    for name, scenario, drive, theme in STATES:
        if only and only not in name:
            continue
        for size in SIZES:
            target = directory / f"{name}-{size[0]}x{size[1]}.svg"
            picture = await shoot(name, scenario, drive, theme, size)
            await asyncio.to_thread(target.write_text, picture, encoding="utf-8")
            written.append(target)
            print(target)
    if await to_png(written):
        print(f"{len(written)} PNG files written beside the SVG files")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    sys.exit(asyncio.run(main(out, sys.argv[2] if len(sys.argv) > 2 else "")))
