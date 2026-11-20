"""The library of checker instruction templates (agent/records/PLAN.md D250)."""

import re
from pathlib import Path

import pytest

from calu.defaults import library
from calu.defaults.instructions import SHIPPED_TEXTS

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "src" / "calu" / "defaults" / "templates"


def test_every_template_names_a_shipped_checker_and_has_a_text() -> None:
    found = library.templates()
    assert [t.name for t in found] == sorted(
        p.stem for p in FOLDER.glob("*.md") if p.stem != "README"
    )
    for template in found:
        assert template.checker in SHIPPED_TEXTS, template.name
        assert template.text.endswith("\n") and not template.text.startswith("---")
    assert library.template("review-findings.md").checker == "subagent-result"


def test_the_index_lists_every_template() -> None:
    index = (FOLDER / "README.md").read_text(encoding="utf-8")
    for template in library.templates():
        assert f"| `{template.name}.md` | `{template.checker}` |" in index


def test_the_documentation_shows_the_same_texts() -> None:
    """The examples in DOCUMENTATION.md and the templates are one text in two places."""
    documentation = (ROOT / "DOCUMENTATION.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```markdown\n(.*?)```", documentation, re.DOTALL)
    for template in library.templates():
        assert template.text in blocks, f"{template.name} differs from its example"


def test_the_licence_covers_the_templates() -> None:
    assert "src/calu/defaults/templates/" in (ROOT / "LICENSE.md").read_text(encoding="utf-8")
    assert "src/calu/defaults/templates/" in (ROOT / "LICENSE").read_text(encoding="utf-8")


def test_an_unknown_or_malformed_template_is_refused_with_the_reason() -> None:
    with pytest.raises(library.TemplateError, match="review-findings"):
        library.template("nope")
    with pytest.raises(library.TemplateError, match="header"):
        library.parse("x", "# no header\n")
    with pytest.raises(library.TemplateError, match="not closed"):
        library.parse("x", "---\nchecker: edit\n")
    with pytest.raises(library.TemplateError, match="checker and a summary"):
        library.parse("x", "---\nchecker: edit\n---\ntext\n")
    with pytest.raises(library.TemplateError, match="no text"):
        library.parse("x", "---\nchecker: edit\nsummary: s\n---\n\n")
    parsed = library.parse("x", "---\nchecker: edit\nsummary: s\n---\n\n# Rules\n\n")
    assert parsed.text == "# Rules\n"


def test_an_unchanged_template_is_recognised() -> None:
    template = library.template("review-findings")
    assert library.matching("\n" + template.text + "\n\n") == template
    assert library.matching(template.text + "- one more rule\n") is None
