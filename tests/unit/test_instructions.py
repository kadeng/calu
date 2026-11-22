"""How a checker's prompt is put together from its parts (agent/records/PLAN.md D181)."""

import pytest

from calu.defaults.instructions import (
    EVASION_PART,
    HONESTY_ROUTES,
    SYSTEM_PARTS,
    compose_instructions,
    default_instructions,
    has_test_triggers,
    honesty_rules,
    legacy_instructions,
    superseded_instructions,
    system_part,
    without_test_triggers,
)
from calu.defaults.instructions import _read as read_part
from calu.web.settings import Sources, panes, parse


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


def test_only_the_honesty_checker_reads_the_honesty_rules() -> None:
    """D224: honesty is judged by the honesty checker alone; no other prompt has the rules."""
    shipped = honesty_rules().strip()
    for route in SYSTEM_PARTS:
        assert shipped not in compose_instructions(route, "x", honesty_state=True)
    assert shipped in compose_instructions("edit", "x", name="honesty")


def test_configured_honesty_rules_replace_the_shipped_ones() -> None:
    """A project's `honesty.rules` is used instead of calu's text, not in addition to it."""
    own = "# Our standard\n\nNumPy docstrings."
    prompt = compose_instructions("edit", "x", rules=own, name="honesty")
    assert "NumPy docstrings." in prompt
    assert honesty_rules().strip() not in prompt
    assert "NumPy docstrings." not in compose_instructions("edit", "x", rules=own)


def test_the_part_about_unverified_code_is_added_only_while_honesty_is_on() -> None:
    """D224: the change checkers get the note about unverified code and no way to mark."""
    for route in HONESTY_ROUTES:
        assert "# Unverified code" not in compose_instructions(route, "x")
        prompt = compose_instructions(route, "x", honesty_state=True)
        assert "# Unverified code" in prompt
        assert "Do not rely on the names or the documentation" in prompt
        assert "marks_dirty" not in prompt and "marks_clean" not in prompt
    assert "# Unverified code" not in compose_instructions("subagent", "x", honesty_state=True)
    # The command checker always says whether a call executes project code (the gate reads
    # it in enforce mode); the others are not asked.
    assert "`executes_project_code`" in compose_instructions("command", "x")
    assert "executes_project_code" not in compose_instructions("edit", "x")


@pytest.mark.parametrize("name", ["command", "edit", "changeset", "subagent-result"])
def test_the_shipped_instructions_are_short(name: str) -> None:
    """Requirement 38: system part and default together are a few plain rules.

    The command checker's limit is higher by the section on commands outside the sandbox
    (D255), and every limit by the fixed part on getting around safeguards (`EVASION_PART`),
    both of which the owner asked for after requirement 38.

    Args:
        name: The checker type.
    """
    route = "subagent" if name == "subagent-result" else name
    words = len(compose_instructions(route, default_instructions(name)).split())
    evasion = len(read_part(EVASION_PART).split())
    assert words < (410 if name == "command" else 360) + evasion, f"{name}: {words} words"
    assert "auto mode" in system_part(route) or route == "subagent"


@pytest.mark.parametrize("name", ["command", "edit", "changeset", "subagent-result"])
def test_the_test_triggers_are_one_removable_section(name: str) -> None:
    """The four trigger words are in every shipped default, in a section that can be cut.

    Args:
        name: The checker type.
    """
    text = default_instructions(name)
    assert has_test_triggers(text)
    for word in ("calu:test-deny", "calu:test-escalate", "calu:test-allow", "calu:test-pass"):
        assert word in text
    # The permissive triggers count only in the user's own message.
    assert 'in a message of the user ("**User:**")' in text
    without = without_test_triggers(text)
    assert not has_test_triggers(without) and "calu:test-" not in without
    assert without.startswith("# Project rules") and without.endswith("\n")
    assert without_test_triggers(without) == without


def test_cutting_the_test_triggers_keeps_what_follows_them() -> None:
    """The section ends at the next first-level heading."""
    text = "# Rules\n\nA.\n\n# Test triggers\n\n- x\n\n## sub\n\ny\n\n# More\n\nB.\n"
    assert without_test_triggers(text) == "# Rules\n\nA.\n\n# More\n\nB.\n"
    assert without_test_triggers("# Test triggers\n\n- x\n") == ""


def test_the_narrator_and_the_redaction_instructions_are_untouched() -> None:
    """Requirement 38: the narrator's instructions change only on purpose.

    Changed once since: the owner asked that plans to get around a safeguard are narrated in
    full (`EVASION_PART`).
    """
    import hashlib

    digests = {
        "narrator": "2962f12b92ae2ccc6eba290c5275439c524ad44944d5d394b60ce11837c31f26",
    }
    for name, digest in digests.items():
        text = default_instructions(name).encode("utf-8")
        assert hashlib.sha256(text).hexdigest() == digest


def test_an_unknown_route_gets_the_users_text_alone() -> None:
    """A checker without a known route sees exactly its own instructions."""
    assert compose_instructions("narrator", "Narrate.") == "Narrate.\n"


@pytest.mark.parametrize("name", ["command", "edit", "changeset", "subagent-result", "honesty"])
def test_the_new_defaults_differ_from_the_old_ones(name: str) -> None:
    """`calu setup` can tell an unedited earlier default from the present one.

    Args:
        name: The checker type.
    """
    old = legacy_instructions(name)
    earlier = superseded_instructions(name)
    assert old is not None and earlier[0] == old and len(earlier) == 2
    assert default_instructions(name) not in earlier
    if name != "honesty":
        assert all(len(default_instructions(name)) < len(text) for text in earlier)


def test_a_type_without_an_old_default_has_none() -> None:
    """A checker type that had no shipped text before D181 reports None."""
    assert legacy_instructions("no-such-checker") is None


def test_the_honesty_rules_are_an_editor_that_shows_the_shipped_text() -> None:
    """The settings page shows `honesty.rules` as a Markdown editor holding what applies."""
    src = Sources(None, {}, {}, [], {}, [])
    found = [f for pane in panes(src) for f in pane.fields if f.name == "honesty.rules"]
    assert [f.kind for f in found] == ["markdown"]
    assert found[0].value == honesty_rules() and not found[0].is_set
    assert parse("markdown", "# A\r\n\r\n- b\r\n") == "# A\n\n- b\n"
    assert parse("markdown", "  \n") is None


def test_the_redaction_instructions_are_an_editor_with_the_shipped_text_and_the_fixed_part() -> (
    None
):
    """D245: `traces.redactor_instructions` is edited like `honesty.rules`, on the traces pane."""
    from calu.defaults.instructions import default_redaction_instructions, redaction_system_part
    from calu.web.settings import fingerprint

    src = Sources(None, {}, {}, [], {}, [])
    found = [
        (pane.title, f)
        for pane in panes(src)
        for f in pane.fields
        if f.name == "traces.redactor_instructions"
    ]
    assert len(found) == 1
    title, field = found[0]
    assert title == "Session traces" and field.kind == "markdown"
    assert field.value == default_redaction_instructions() and not field.is_set
    assert field.extra == redaction_system_part() and field.version == fingerprint(None)
    stored = Sources(None, {"traces": {"redactor_instructions": "# Ours\n"}}, {}, [], {}, [])
    own = next(
        f for pane in panes(stored) for f in pane.fields if f.name == "traces.redactor_instructions"
    )
    assert own.is_set and own.value == "# Ours\n" and own.version == fingerprint("# Ours\n")
    assert fingerprint("# Ours\n") != fingerprint("# Ours!\n") != fingerprint(None)


def test_every_setting_of_the_model_is_on_the_page_exactly_once() -> None:
    """A new configuration field appears on the settings page without a change to the page."""
    from calu.configdoc import setting_rows
    from calu.web.settings import SKIP

    src = Sources(None, {}, {}, [], {}, [])
    names = [f.name for pane in panes(src) for f in pane.fields]
    assert len(names) == len(set(names))
    keys = [row[0] for row in setting_rows()]
    # A nested model has a row of its own; its fields are what the page shows.
    documented = {key for key in keys if not any(other.startswith(key + ".") for other in keys)}
    missing = {key for key in documented - set(names) if key.split(".")[-1] not in SKIP}
    assert not missing, f"not on the settings page: {sorted(missing)}"
