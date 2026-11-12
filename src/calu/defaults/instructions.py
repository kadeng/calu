"""The built-in checker instructions, and how a checker's full prompt is put together.

A checker's prompt has up to four parts (PLAN.md D181, the owner's answer to Q16):

    system/<route>.md       calu's own: role, tools, how to answer        fixed
    <checker>.md            the user's: what to pass, deny, escalate       `calu checker edit`
    honesty-rules.md        the honesty standard (contracts, dishonesty)   `honesty.rules`
    system/marks.md         how to record dishonest code                   fixed, honesty on

The user's part is what `checker_types` holds (D6); `calu setup` loads the shipped one. The
honesty rules are a setting, so a project can adapt them to its documentation standards; they
reach the checkers that judge changes (command, edit, changeset). The marks part is added only
while `honesty.mode` is not "off", because only then is a mark used.
"""

from importlib import resources

SYSTEM_PARTS = {
    "command": "command",
    "edit": "edit",
    "changeset": "changeset",
    "subagent": "subagent-result",
}
"""Route -> the file of its system part in `checkers/system/`."""

HONESTY_ROUTES = frozenset({"command", "edit", "changeset"})
"""Routes whose checkers judge changes and therefore read the honesty rules."""


def _read(relative: str) -> str:
    """Read a file shipped in `calu/defaults/checkers/`.

    Args:
        relative: The path below that directory, e.g. "system/edit.md".

    Returns:
        The file's text.

    Raises:
        FileNotFoundError: If no such file is shipped.
    """
    return (
        resources.files("calu.defaults")
        .joinpath(f"checkers/{relative}")
        .read_text(encoding="utf-8")
    )


def default_instructions(name: str) -> str:
    """Return the shipped user instructions of a checker type (its editable part).

    Args:
        name: Checker type name: "command", "edit", "changeset", "subagent-result", "honesty"
            or "narrator".

    Returns:
        The Markdown instructions shipped with calu.

    Raises:
        FileNotFoundError: If no instructions file exists for that name.
    """
    return _read(f"{name}.md")


def legacy_instructions(name: str) -> str | None:
    """Return the instructions calu shipped for a checker type before the split of D181.

    `calu setup` compares a stored text with this to tell an unedited old default, which it may
    replace with the new compact one, from a text somebody wrote.

    Args:
        name: Checker type name.

    Returns:
        The old text, or None if the type had none.
    """
    try:
        return _read(f"legacy/{name}.md")
    except FileNotFoundError:
        return None


def honesty_rules() -> str:
    """Return the shipped honesty rules, used when `honesty.rules` is not set.

    Returns:
        The Markdown text of `honesty-rules.md`.
    """
    return _read("honesty-rules.md")


def compose_instructions(
    route: str, user: str, rules: str | None = None, marks: bool = False
) -> str:
    """Put a checker's full prompt together from its parts (D181).

    Args:
        route: The checker's route: "command", "edit", "changeset" or "subagent". Any other
            value gets no system part (the user's text is then the whole prompt).
        user: The checker's own instructions, as `checker_types` holds them.
        rules: The honesty rules to include (`honesty.rules`, or None for the shipped ones);
            used only for `HONESTY_ROUTES`.
        marks: Whether to add the part about recording dishonest code (`honesty.mode` on).

    Returns:
        The system part, the user's instructions under their own heading, and -- for a
        checker that judges changes -- the honesty rules and, with `marks`, how to record.

    Example:
        compose_instructions("edit", "# What to pass ...") starts with "# Edit checker" and
        contains "# The user's instructions" followed by the given text.
    """
    parts: list[str] = []
    system = SYSTEM_PARTS.get(route)
    if system is not None:
        parts.append(_read(f"system/{system}.md").strip())
        parts.append("# The user's instructions\n\n" + user.strip())
    else:
        parts.append(user.strip())
    if route in HONESTY_ROUTES:
        parts.append((rules if rules else honesty_rules()).strip())
        if marks:
            parts.append(_read("system/marks.md").strip())
    return "\n\n".join(parts) + "\n"
