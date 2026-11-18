"""Control characters made visible, for clients that write to a terminal.

Text of the session reaches the terminal clients from Claude, from tool results and from
project files. A terminal obeys some characters instead of showing them: an escape sequence
moves the cursor, clears the screen or sets the window title; a carriage return lets the text
after it overwrite the text before it; a bidirectional control character changes the order in
which the following characters are drawn. A command on an approval card could so be displayed
as another command than the one that would run. `visible` replaces each such character by a
sign that is shown, and changes nothing else:

    line feed, tab                     unchanged
    carriage return before line feed   removed (a Windows line ending)
    other C0 controls, DEL             the Unicode control picture: ESC -> ␛, CR -> ␍, BS -> ␈
    C1 controls (U+0080..U+009F)       ⟦U+009B⟧
    bidirectional controls             ⟦RLO⟧, ⟦PDF⟧, ... (the names the web pages show too)

Letters of right-to-left scripts are not touched. This module imports nothing from calu.
"""

import re
from typing import Any

BIDI_NAMES = {
    "؜": "ALM",
    "‎": "LRM",
    "‏": "RLM",
    "‪": "LRE",
    "‫": "RLE",
    "‬": "PDF",
    "‭": "LRO",
    "‮": "RLO",
    "⁦": "LRI",
    "⁧": "RLI",
    "⁨": "FSI",
    "⁩": "PDI",
}
"""Unicode's bidirectional control characters with the names shown for them (the same names
as `calu.web.app.BIDI_NAMES`)."""

_SHOWN: dict[int, str] = {
    **{code: chr(0x2400 + code) for code in range(0x20) if code not in (0x09, 0x0A)},
    0x7F: "␡",
    **{code: f"⟦U+{code:04X}⟧" for code in range(0x80, 0xA0)},
    **{ord(char): f"⟦{name}⟧" for char, name in BIDI_NAMES.items()},
}
"""Translation table of `visible`: code point -> what is shown for it."""

_OBEYED = re.compile("[" + "".join(re.escape(chr(code)) for code in _SHOWN) + "]")
"""Matches a character `visible` replaces."""


def visible(text: str) -> str:
    """Replace the characters a terminal would obey by signs it shows (see the module).

    Args:
        text: Any text.

    Returns:
        The text with every control character except line feed and tab replaced; a text
        without such a character is returned as it is (the same object).

    Example:
        A text of "ok", the escape character, "[2Jrm" and a right-to-left override becomes
        "ok␛[2Jrm⟦RLO⟧".
    """
    if not _OBEYED.search(text):
        return text
    return text.replace("\r\n", "\n").translate(_SHOWN)


def visible_in(value: Any) -> Any:
    """Apply `visible` to every text inside a JSON-like value.

    Args:
        value: A text, a list, a dict (its values, not its keys) or anything else.

    Returns:
        A value of the same shape with every text made visible. Containers are copied only
        if something in them changed, so a value without control characters is returned as
        it is.
    """
    if isinstance(value, str):
        return visible(value)
    if isinstance(value, list):
        shown = [visible_in(item) for item in value]
        return (
            shown if any(new is not old for new, old in zip(shown, value, strict=True)) else value
        )
    if isinstance(value, dict):
        changed = {key: visible_in(item) for key, item in value.items()}
        return changed if any(changed[key] is not value[key] for key in value) else value
    return value
