"""Shared helpers of the unit tests."""

from pathlib import Path

from calu.checkers.rules import Rules
from calu.setup import default_rules as default_rule_rows


def default_rules(workspace: Path, replace: dict[str, str] | None = None) -> Rules:
    """Return the built-in rules for `workspace`, as `calu setup` loads them.

    Args:
        workspace: The workspace root.
        replace: List texts (line format of `parse_rules`) that replace whole default lists.
    """
    rules = Rules.from_rows(
        workspace, ((r.list, r.pattern, r.position) for r in default_rule_rows())
    )
    if replace:
        lists = {name: rules.lists(name) for name in rules._lists}
        lists.update(Rules.from_texts(workspace, replace)._lists)
        rules.replace(lists)
    return rules
