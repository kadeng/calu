"""`calu config list`: every setting, its effective value, where it comes from, its default."""

from calu.cli import _configuration_table
from calu.configdoc import setting_rows


def test_every_setting_is_listed_with_its_origin() -> None:
    """D128: `calu config get` shows what a scope **stores**, which is `{}` on a fresh install."""
    table = _configuration_table({}, {}, None)
    leaves = [
        key
        for key, kind, default, _ in setting_rows()
        if kind != "object" and default != "see below"
    ]
    assert table.splitlines()[0] == f"{len(leaves)} setting(s), scope: global"
    for key in ("claude.model", "honesty.mode", "context.narrate", "command_checker.model"):
        assert any(line.startswith(key + " ") for line in table.splitlines()), key
    assert "global (set)" not in table, "nothing is stored, so every value is the default"


def test_a_project_value_beats_a_global_one_and_says_so() -> None:
    table = _configuration_table(
        {"claude": {"model": "opus"}, "honesty": {"mode": "track"}},
        {"honesty": {"mode": "enforce"}},
        "demo",
        changed_only=True,
    )
    lines = {line.split()[0]: line for line in table.splitlines()[1:] if line.strip()}
    assert "enforce" in lines["honesty.mode"] and "project demo (set)" in lines["honesty.mode"]
    assert "opus" in lines["claude.model"] and "global (set)" in lines["claude.model"]
    # --changed leaves the defaults out entirely.
    assert "context.narrate" not in lines
    # A value that differs from its default names the default beside it.
    assert 'default: "off"' in lines["honesty.mode"]


def test_values_are_shown_the_way_a_reader_types_them() -> None:
    """Strings bare, unset optionals and empty strings in words, everything else as JSON."""
    table = _configuration_table({}, {}, None)
    line = next(row for row in table.splitlines() if row.startswith("command_checker.effort "))
    assert "(none)" in line and "null" not in line
    model = next(row for row in table.splitlines() if row.startswith("command_checker.model "))
    assert '"' not in model.split()[1], "a string value is shown without JSON quotes"
