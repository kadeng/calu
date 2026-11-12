"""How a checker's prompt is put together from its parts (PLAN.md D181)."""

import pytest

from calu.defaults.instructions import (
    HONESTY_ROUTES,
    SYSTEM_PARTS,
    compose_instructions,
    default_instructions,
    honesty_rules,
    legacy_instructions,
)
from calu.web.settings import parse, sections


@pytest.mark.parametrize("route", sorted(SYSTEM_PARTS))
def test_the_system_part_comes_first_and_the_users_text_follows(route: str) -> None:
    """Every route gets calu's fixed part and then the user's instructions under a heading.

    Args:
        route: The checker route.
    """
    prompt = compose_instructions(route, "Escalate anything touching billing.")
    head, _, rest = prompt.partition("# The user's instructions")
    assert head.strip(), "the system part is missing"
    assert rest.strip().startswith("Escalate anything touching billing.")


def test_only_the_change_checkers_read_the_honesty_rules() -> None:
    """The command, edit and changeset prompts carry the honesty rules; the subagent one not."""
    shipped = honesty_rules().strip()
    for route in HONESTY_ROUTES:
        assert shipped in compose_instructions(route, "x")
    assert shipped not in compose_instructions("subagent", "x")


def test_configured_honesty_rules_replace_the_shipped_ones() -> None:
    """A project's `honesty.rules` is used instead of calu's text, not in addition to it."""
    prompt = compose_instructions("edit", "x", rules="# Our standard\n\nNumPy docstrings.")
    assert "NumPy docstrings." in prompt
    assert honesty_rules().strip() not in prompt


def test_the_marks_part_is_added_only_when_asked() -> None:
    """How to record dishonest code is only in the prompt while honesty marks are in use."""
    assert "marks_dirty" not in compose_instructions("edit", "x")
    assert "marks_dirty" in compose_instructions("edit", "x", marks=True)
    assert "marks_dirty" not in compose_instructions("subagent", "x", marks=True)


def test_an_unknown_route_gets_the_users_text_alone() -> None:
    """A checker without a known route sees exactly its own instructions."""
    assert compose_instructions("narrator", "Narrate.") == "Narrate.\n"


@pytest.mark.parametrize("name", ["command", "edit", "changeset", "subagent-result", "honesty"])
def test_the_new_defaults_differ_from_the_old_ones(name: str) -> None:
    """`calu setup` can tell an unedited old default from the new compact one.

    Args:
        name: The checker type.
    """
    old = legacy_instructions(name)
    assert old is not None
    assert old != default_instructions(name)
    assert len(default_instructions(name)) < len(old)


def test_a_type_without_an_old_default_has_none() -> None:
    """A checker type that had no shipped text before D181 reports None."""
    assert legacy_instructions("no-such-checker") is None


def test_the_honesty_rules_are_a_large_field_in_the_settings_page() -> None:
    """The settings form shows `honesty.rules` as a Markdown text area and keeps its lines."""
    found = [s for section in sections({}) for s in section.settings if s.path == "honesty.rules"]
    assert [s.kind for s in found] == ["markdown"]
    assert parse("markdown", "# A\r\n\r\n- b\r\n") == "# A\n\n- b\n"
    assert parse("markdown", "  \n") is None
