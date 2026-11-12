"""Write screenshots of the full-screen client: `uv run python scripts/tui_screenshots.py DIR`.

The client runs headless on a scripted session (`calu.tui.fake`, `calu.tui.scenarios`) with a
clock the script controls, so the pictures are the same on every run. Each state is written as
SVG (Textual's own export) at 120x36 and 80x24; if a headless Chromium is available through
Playwright (`scripts/get-browser.sh` installs it without root) each SVG is also rendered to a
PNG next to it.

The states: a finished turn, an open escalation, a session with subagents and a question, a
subagent's own tab, a step expanded to its diff, the full-screen detail of an escalation, a
set of three questions, a side question with its answer, the transcript scrolled up while
output arrives, the side panel over the transcript on a narrow terminal, the help page, the
light theme, the first press of an allow, a stopped session with its start card, the diff of an
edit with two selected lines, the side panel with the focus on a held draft, a search, the
confirmation before a session is stopped and resumed, the stored history above a turn, and the
screen without colours (`NO_COLOR`).
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
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
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


async def diff_with_selection(
    pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock
) -> None:
    """Open the diff of the edit and select two lines of it.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    edit = next(block for block in app.model.blocks if getattr(block, "diffs", None))
    app.transcript.focus()
    app.transcript.set_cursor(edit.id)
    for key in ("D", "v", "j"):
        await pilot.press(key)
        await pilot.pause()


async def panel_with_drafts(
    pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock
) -> None:
    """Hold two drafts and move into the side panel, onto the first draft.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    app.run_command("hold", "After the push: tag the release as v0.9.")
    app.run_command("hold", "Then update the changelog.")
    await pilot.press("f2")
    await pilot.pause()
    for _ in range(7):
        await pilot.press("j")
    await pilot.pause()


def typed(*parts: str) -> Drive:
    """Build a state that presses keys, where a longer part is typed character by character.

    Args:
        *parts: Key names (`ctrl+f`, `enter`) or texts that start with a space or a slash or
            hold a space.

    Returns:
        The drive function.
    """

    async def drive(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
        """Press and type.

        Args:
            pilot: The pilot of the running app.
            app: The app.
            session: The scripted session.
            clock: The clock.
        """
        for part in parts:
            if part.startswith("text:"):
                for character in part[5:]:
                    await pilot.press(character if character != " " else "space")
            else:
                await pilot.press(part)
            await pilot.pause()

    return drive


HONESTY_STATE: dict[str, Any] = {
    "mode": "enforce",
    "marks": [
        {
            "id": 4,
            "path": "src/calu/gate.py",
            "symbol": "judge_step",
            "reason": "the docstring still names check_step and says the step is only logged",
        }
    ],
    "baseline": {
        "commit_id": "4f2a9c1d07aa",
        "source": "user",
        "subject": "Rename the helper in gate.py",
    },
    "unreviewed": ["scripts/migrate.sh", "notes/plan.md"],
    "unreviewed_count": 2,
    "review_required": False,
    "review_running": False,
}
"""The honesty state of the honesty pictures: one mark, two unreviewed files."""


def honesty(*parts: str) -> Drive:
    """Build a state that reports an honesty state and then presses and types.

    Args:
        *parts: As for `typed`.

    Returns:
        The drive function.
    """

    async def drive(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
        """Send `honesty.update`, then press and type.

        Args:
            pilot: The pilot of the running app.
            app: The app.
            session: The scripted session.
            clock: The clock.
        """
        session.honesty_requests = True
        app.describe_commit = lambda workspace, rev: "HEAD = 9e8d7c6 Run the unit tests in CI"
        await session.emit("honesty.update", HONESTY_STATE)
        await pilot.pause()
        await typed(*parts)(pilot, app, session, clock)

    return drive


async def git_notice(pilot: Pilot[None], app: CaluApp, session: FakeSession, clock: Clock) -> None:
    """Press `f4` on a machine with apt and without a terminal git program.

    Args:
        pilot: The pilot of the running app.
        app: The app.
        session: The scripted session.
        clock: The clock.
    """
    app.model.workspace = str(Path.cwd())
    app.environ = {}
    app.which = lambda name: f"/usr/bin/{name}" if name in {"git", "apt"} else None
    app.run_program = lambda argv, cwd: 0
    await pilot.press("f4")
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
    ("14-stopped", "stopped", nothing, ""),
    ("15-diff", "simple_turn", diff_with_selection, ""),
    ("16-panel-drafts", "showcase", panel_with_drafts, ""),
    ("17-search", "simple_turn", typed("ctrl+f", "text:gate"), ""),
    ("18-confirm-restart", "simple_turn", typed("text:/effort max", "enter"), ""),
    ("19-stored-history", "history", nothing, ""),
    ("20-no-colour", "showcase", nothing, "mono"),
    ("21-checkers", "simple_turn", typed("text:/checkers", "enter", "j"), ""),
    ("22-checker-editor", "simple_turn", typed("text:/checkers", "enter", "e"), ""),
    ("23-honesty", "simple_turn", honesty("text:/honesty", "enter"), ""),
    ("24-trust-question", "simple_turn", honesty("text:/trust", "enter"), ""),
    ("25-git-install-notice", "simple_turn", git_notice, ""),
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
    if scenario == "history":
        session.results["session.history"] = scenarios.stored_history()
        scenario = "simple_turn"
    items = scenarios.SCENARIOS[scenario]()
    if scenario == "long_session":
        items = scenarios.long_session(60)
    session.results["escalation.context"] = CONTEXT
    options = TuiOptions(theme=theme) if theme and theme != "mono" else TuiOptions()
    if theme == "mono":
        os.environ["NO_COLOR"] = "1"
    try:
        app = CaluApp(
            fake_connector(session), options, clock=clock, live=False, admin=FakeAdmin(session)
        )
    finally:
        os.environ.pop("NO_COLOR", None)
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
