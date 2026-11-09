"""The built-in checker instructions, as a fallback for checker types the database does not hold.

A session reads its instructions from `checker_types` (PLAN.md D6). `calu setup` loads these
files into that table; PLAN.md §7 allows the narrator instructions to stay built in, so a
database without a "narrator" row is not an error.
"""

from importlib import resources


def default_instructions(name: str) -> str:
    """Return the built-in instructions of a checker type.

    Args:
        name: Checker type name: "command", "edit", "subagent-result" or "narrator".

    Returns:
        The Markdown instructions shipped with calu.

    Raises:
        FileNotFoundError: If no instructions file exists for that name.
    """
    path = resources.files("calu.defaults").joinpath(f"checkers/{name}.md")
    return path.read_text(encoding="utf-8")
