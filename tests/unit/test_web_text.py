"""Untrusted text on the pages of the web interface: bidirectional control characters."""

from pathlib import Path

from markupsafe import Markup

import calu.web.app as web_app
from calu.web.app import BIDI_NAMES, TEMPLATES, visible


def test_every_bidirectional_control_character_is_shown_by_its_name() -> None:
    """A command must be read in the order it is stored and run in."""
    assert visible("rm -rf ‮gnp.tropxe") == "rm -rf ⟦RLO⟧gnp.tropxe"
    assert visible("a⁧b⁩c") == "a⟦RLI⟧b⟦PDI⟧c"
    for character, name in BIDI_NAMES.items():
        assert visible(f"x{character}y") == f"x⟦{name}⟧y"


def test_ordinary_text_and_other_values_pass_through_unchanged() -> None:
    """Right-to-left letters are text, not controls; numbers and None are not texts."""
    arabic = "مرحبا"
    assert visible(arabic) is arabic
    assert visible(7) == 7
    assert visible(None) is None
    marked = visible(Markup("<b>a‎b</b>"))
    assert isinstance(marked, Markup) and marked == "<b>a⟦LRM⟧b</b>"


def test_the_templates_apply_it_to_every_value_they_print() -> None:
    """One place covers every page: a template cannot forget the filter."""
    rendered = TEMPLATES.env.from_string("<pre>{{ text }}</pre>").render(
        text="report_‮gpj.exe <script>"
    )
    assert rendered == "<pre>report_⟦RLO⟧gpj.exe &lt;script&gt;</pre>"


def test_the_scripts_use_the_same_names() -> None:
    """`shared.js` does for the live view what `visible` does for the rendered pages."""
    script = (Path(web_app.__file__).parent / "static" / "shared.js").read_text(encoding="utf-8")
    for character, name in BIDI_NAMES.items():
        assert f'0x{ord(character):04x}: "{name}"' in script
