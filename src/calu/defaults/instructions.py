"""The built-in checker instructions, and how a checker's full prompt is put together.

A checker's prompt has up to three parts (agent/records/PLAN.md D181, D224):

    system/<route>.md         calu's own: the task, how to answer            fixed
    <checker>.md              the user's: the project's rules, and the      `calu checker edit`
                              test triggers of the shipped defaults
    system/honesty-state.md   not to rely on unverified code                 fixed, honesty on

The command, edit, changeset and subagent-result checkers are a thin layer of project rules:
general safety is judged by Claude Code's auto mode, which the system parts say in one
sentence. Honesty is judged by the honesty checker alone (D224): its prompt is the fixed
`system/honesty.md`, the user's part `honesty.md` and the honesty rules (`honesty.rules`, or
the shipped `honesty-rules.md`). No other checker reads the honesty rules.

The honesty reviewer (`calu.checkers.honesty_review`) is no checker type: its prompt is the
fixed `system/honesty-review.md` followed by the honesty rules (`review_instructions`). The
redaction agent of the session traces (`calu.traces.redact`) is none either: its prompt is the
fixed `system/trace-redaction.md` (`redaction_instructions`).

The user's part is what `checker_types` holds (D6); `calu setup` loads the shipped one.
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
"""Routes whose checkers get the code honesty state as a note of the step, and therefore the
fixed part `system/honesty-state.md` while `honesty.mode` is not "off"."""

TEST_TRIGGERS_HEADING = "# Test triggers"
"""Heading of the section of a shipped default that holds the test triggers. The section runs
to the next heading of the first level or to the end of the text (`without_test_triggers`)."""


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


def system_part(route: str) -> str:
    """Return the fixed part calu puts in front of the instructions of a route's checkers.

    Args:
        route: The checker's route, e.g. "edit".

    Returns:
        The Markdown text of `system/<route>.md`, or "" for a route without a system part.
    """
    name = SYSTEM_PARTS.get(route)
    return _read(f"system/{name}.md") if name is not None else ""


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

    Args:
        name: Checker type name.

    Returns:
        The old text, or None if the type had none.
    """
    try:
        return _read(f"legacy/{name}.md")
    except FileNotFoundError:
        return None


def superseded_instructions(name: str) -> list[str]:
    """Return every text calu shipped earlier as the user's part of a checker type.

    `calu setup` compares a stored text with these to tell an unedited old default, which it
    replaces with the present one, from a text somebody wrote.

    Args:
        name: Checker type name.

    Returns:
        The earlier shipped texts, oldest first: the one from before the split of D181
        (`legacy/`) and the compact one from before the short instructions of requirement 38
        (`legacy/v2/`). Empty for a type that had none.
    """
    found: list[str] = []
    for directory in ("legacy", "legacy/v2"):
        try:
            found.append(_read(f"{directory}/{name}.md"))
        except FileNotFoundError:
            continue
    return found


def without_test_triggers(text: str) -> str:
    r"""Return a checker's instructions without the section of test triggers.

    The shipped defaults hold a section `TEST_TRIGGERS_HEADING` with which a person can
    provoke each decision on purpose. A project that relies on its checkers removes it.

    Args:
        text: The user's part of a checker's instructions.

    Returns:
        The text without that section: from the heading line up to the next line that starts
        a first-level heading ("# "), or to the end. A text without the section is returned
        unchanged. The result ends with one newline unless it is empty.

    Example:
        without_test_triggers("# Project rules\n\nA.\n\n# Test triggers\n\nB.\n") is
        "# Project rules\n\nA.\n".
    """
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    found = False
    for line in lines:
        if line.strip() == TEST_TRIGGERS_HEADING:
            skipping = found = True
            continue
        if skipping and line.startswith("# "):
            skipping = False
        if not skipping:
            kept.append(line)
    if not found:
        return text
    stripped = "\n".join(kept).strip()
    return stripped + "\n" if stripped else ""


def has_test_triggers(text: str) -> bool:
    """Tell whether a checker's instructions hold the section of test triggers.

    Args:
        text: The user's part of a checker's instructions.

    Returns:
        True if a line of the text is exactly `TEST_TRIGGERS_HEADING`.
    """
    return any(line.strip() == TEST_TRIGGERS_HEADING for line in text.splitlines())


def honesty_rules() -> str:
    """Return the shipped honesty rules, used when `honesty.rules` is not set.

    Returns:
        The Markdown text of `honesty-rules.md`.
    """
    return _read("honesty-rules.md")


def redaction_instructions() -> str:
    """Return the fixed instructions of the redaction agent of the session traces (D232).

    Returns:
        The Markdown text of `system/trace-redaction.md`: which secrets and personal data the
        agent replaces by typed placeholders, and that it returns the text otherwise
        unchanged.
    """
    return _read("system/trace-redaction.md")


def review_instructions(rules: str | None = None) -> str:
    """Put the prompt of the honesty reviewer together (specification section 4).

    Args:
        rules: The honesty rules to include (`honesty.rules`); None or "" uses the shipped
            ones.

    Returns:
        The fixed part `system/honesty-review.md`, followed by the honesty rules.
    """
    system = _read("system/honesty-review.md").strip()
    return system + "\n\n" + (rules if rules else honesty_rules()).strip() + "\n"


def compose_instructions(
    route: str, user: str, rules: str | None = None, honesty_state: bool = False, name: str = ""
) -> str:
    """Put a checker's full prompt together from its parts (D181, D224).

    Args:
        route: The checker's route: "command", "edit", "changeset" or "subagent". Any other
            value gets no system part (the user's text is then the whole prompt).
        user: The checker's own instructions, as `checker_types` holds them.
        rules: The honesty rules (`honesty.rules`, or None for the shipped ones). Only the
            honesty checker reads them; for every other checker the value is not used.
        honesty_state: Whether `honesty.mode` is not "off". The checkers of `HONESTY_ROUTES`
            then get the fixed part `system/honesty-state.md`, which says not to rely on the
            names and documentation of code the honesty state lists.
        name: The checker type. The honesty checker ("honesty") answers per file and not
            with a decision, so it gets its own system part (`system/honesty.md`) in place of
            the route's, followed by the user's part and the honesty rules.

    Returns:
        For the honesty checker: its system part, the user's instructions under their own
        heading, and the honesty rules. For every other checker: the route's system part, the
        user's instructions under their own heading and, with `honesty_state` on a route of
        `HONESTY_ROUTES`, the part about unverified code. No other checker gets the honesty
        rules or a way to record marks.

    Example:
        compose_instructions("edit", "# Project rules ...") starts with "# Edit checker" and
        contains "# The user's instructions" followed by the given text.
    """
    if name == "honesty":
        return (
            "\n\n".join(
                [
                    _read("system/honesty.md").strip(),
                    "# The user's instructions\n\n" + user.strip(),
                    (rules if rules else honesty_rules()).strip(),
                ]
            )
            + "\n"
        )
    parts: list[str] = []
    system = system_part(route)
    if system:
        parts.append(system.strip())
        parts.append("# The user's instructions\n\n" + user.strip())
    else:
        parts.append(user.strip())
    if honesty_state and route in HONESTY_ROUTES:
        parts.append(_read("system/honesty-state.md").strip())
    return "\n\n".join(parts) + "\n"
