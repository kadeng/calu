"""The whole screen of the full-screen client as text, compared with stored pictures.

Six scripted sessions at 120x36 and at 80x24: what every line of the terminal shows after the
scenario has played. The stored pictures are the files `tests/unit/tui_screens/*.txt`; they
are text, so a change of the layout shows up as a readable diff in review.

A deliberate change of the layout: run the tests once with `CALU_UPDATE_TUI_SCREENS=1`, which
rewrites the files, and look at the diff (and at the pictures of
`scripts/tui_screenshots.py`) before committing it.

The colours are not in these files. `test_tui_more` checks that `NO_COLOR` draws greys only;
here one test checks that the screen without colours holds the same text, which is what makes
it readable: every meaning is also a word or a sign.
"""

import os
from pathlib import Path

import pytest

pytest.importorskip("textual")

from calu.tui import scenarios
from calu.tui.app import CaluApp, TuiOptions
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
from tests.unit.test_tui_app import Clock

pytestmark = pytest.mark.anyio

SCREENS = Path(__file__).parent / "tui_screens"
"""Where the stored pictures are."""

SCENARIOS = ("simple_turn", "escalation", "showcase", "question_multi", "two_subagents", "stopped")
"""The scripted sessions a picture is stored of."""

SIZES = ((120, 36), (80, 24))
"""The terminal sizes: a wide one and the classic default."""


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


async def screen_text(scenario: str, size: tuple[int, int]) -> str:
    """Play a scenario and return what the terminal shows.

    Args:
        scenario: A key of `scenarios.SCENARIOS`.
        size: Terminal width and height.

    Returns:
        The lines of the screen without trailing spaces, joined with newlines.
    """
    clock = Clock()
    session = FakeSession()
    app = CaluApp(
        fake_connector(session),
        TuiOptions(bell=False),
        clock=clock,
        live=False,
        admin=FakeAdmin(session),
    )
    async with app.run_test(size=size, notifications=False) as pilot:
        await pilot.pause()
        await scenarios.play(session, scenarios.SCENARIOS[scenario](), clock.advance)
        await pilot.pause()
        app.render_all()
        await pilot.pause()
        strips = app.screen._compositor.render_strips()
        return "\n".join(strip.text.rstrip() for strip in strips) + "\n"


@pytest.mark.parametrize("size", SIZES, ids=lambda size: f"{size[0]}x{size[1]}")
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_screen_is_the_stored_picture(scenario: str, size: tuple[int, int]) -> None:
    stored = SCREENS / f"{scenario}-{size[0]}x{size[1]}.txt"
    shown = await screen_text(scenario, size)
    if os.environ.get("CALU_UPDATE_TUI_SCREENS"):
        stored.write_text(shown, encoding="utf-8")
    assert stored.exists(), f"no stored picture: run once with CALU_UPDATE_TUI_SCREENS=1 ({stored})"
    assert shown == stored.read_text(encoding="utf-8")


async def test_every_line_fits_the_terminal_and_the_fixed_regions_are_in_place() -> None:
    for size in SIZES:
        lines = (await screen_text("showcase", size)).splitlines()
        assert len(lines) <= size[1] and all(len(line) <= size[0] for line in lines)
        assert lines[0].lstrip().startswith("calu"), "the status bar is the first line"
        assert "main" in lines[1], "the agent tabs are the second line"


async def test_without_colours_the_screen_holds_the_same_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coloured = await screen_text("showcase", (120, 36))
    monkeypatch.setenv("NO_COLOR", "1")
    assert await screen_text("showcase", (120, 36)) == coloured
    for word in ("PASS", "DENY", "ESCALATE", "waits for you", "NEEDS YOU (2)", "2 need you"):
        assert word in coloured
