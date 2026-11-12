"""Colours of the full-screen client, named by what they mean.

Two things use the colours: the Textual CSS (`tui.tcss`), through the variables of a Textual
`Theme`, and the Rich texts of `calu.tui.render`, through a `Palette`. `CALU_DARK` and
`CALU_LIGHT` are the two themes calu registers; `palette_for` derives a `Palette` from the CSS
variables of whatever theme is active, so the built-in Textual themes work as well.

Every meaning a colour carries is also carried by a word or a sign (the badge word, the state
word, the `>` of the cursor), so the client stays readable without colours (`NO_COLOR`).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from textual.color import Color
from textual.theme import Theme

DARK = "calu-dark"
"""Name of calu's dark theme, the default."""

LIGHT = "calu-light"
"""Name of calu's light theme."""

CALU_DARK = Theme(
    name=DARK,
    primary="#6aa9ff",
    secondary="#5ccfe6",
    accent="#6aa9ff",
    warning="#f0a04b",
    error="#ef6b73",
    success="#6cc37a",
    foreground="#d7dce5",
    background="#0f1115",
    surface="#161a21",
    panel="#1d222c",
    dark=True,
)
"""calu's dark theme: one accent colour (blue), orange for what waits for the user."""

CALU_LIGHT = Theme(
    name=LIGHT,
    primary="#1f6feb",
    secondary="#0b7285",
    accent="#1f6feb",
    warning="#b35900",
    error="#cf222e",
    success="#1a7f37",
    foreground="#1f2430",
    background="#fbfbfc",
    surface="#f1f3f6",
    panel="#e4e8ee",
    dark=False,
)
"""calu's light theme, with the same assignments as the dark one."""


@dataclass(frozen=True)
class Palette:
    """The colours the Rich texts use, as `#rrggbb` strings.

    `fg` is ordinary text, `muted` secondary text, `faint` rules and hints. `accent` marks the
    user's own input and what runs, `green` an explicit allow and added lines, `orange` what
    waits for the user, `red` a denial, an error and removed lines, `cyan` a running check.
    `user_bg` is the background of the user's messages, `wait_bg` of a step that waits.
    """

    fg: str
    muted: str
    faint: str
    bg: str
    surface: str
    accent: str
    green: str
    orange: str
    red: str
    cyan: str
    user_bg: str
    wait_bg: str


def _blend(first: str, second: str, share: float) -> str:
    """Mix two colours.

    Args:
        first: A colour as `#rrggbb`.
        second: The colour to mix in.
        share: How much of `second`, 0.0 to 1.0.

    Returns:
        The mixed colour as `#rrggbb`.
    """
    return Color.parse(first).blend(Color.parse(second), share).hex


def palette_for(variables: Mapping[str, str]) -> Palette:
    """Derive the palette from the CSS variables of the active Textual theme.

    Args:
        variables: `App.get_css_variables()`; the base colours of a theme are in it as
            `#rrggbb` strings. A missing or unreadable one falls back to calu's dark theme.

    Returns:
        The palette. Muted and faint text are the foreground mixed with the background; the two
        tinted backgrounds are the background mixed with the accent and with the warning colour.
    """

    def colour(name: str, fallback: str) -> str:
        """Read one base colour.

        Args:
            name: The variable name.
            fallback: Used when the variable is missing or not a plain colour.

        Returns:
            The colour as `#rrggbb`.
        """
        try:
            return Color.parse(variables.get(name, fallback)).hex
        except Exception:  # an "auto" or "ansi_*" value has no fixed colour
            return fallback

    fg = colour("foreground", "#d7dce5")
    bg = colour("background", "#0f1115")
    accent = colour("primary", "#6aa9ff")
    orange = colour("warning", "#f0a04b")
    return Palette(
        fg=fg,
        muted=_blend(fg, bg, 0.38),
        faint=_blend(fg, bg, 0.62),
        bg=bg,
        surface=colour("surface", "#161a21"),
        accent=accent,
        green=colour("success", "#6cc37a"),
        orange=orange,
        red=colour("error", "#ef6b73"),
        cyan=colour("secondary", "#5ccfe6"),
        user_bg=_blend(bg, accent, 0.16),
        wait_bg=_blend(bg, orange, 0.13),
    )


DARK_PALETTE = palette_for(
    {
        "foreground": "#d7dce5",
        "background": "#0f1115",
        "surface": "#161a21",
        "primary": "#6aa9ff",
        "secondary": "#5ccfe6",
        "warning": "#f0a04b",
        "error": "#ef6b73",
        "success": "#6cc37a",
    }
)
"""The palette of `CALU_DARK`, for rendering without a running app (tests of `render`)."""
