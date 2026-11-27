"""The built-in checker instructions, and how a checker's full prompt is put together.

A checker's prompt has these parts (agent/records/PLAN.md D181, D224):

    system/<route>.md         calu's own: the task, how to answer            fixed
    system/role.md,           soft guidance vs. hard guardrail; getting      fixed
    system/evasion.md,        around safeguards; no contents of other files
    system/disclosure.md      and no secrets in the answer
    <checker>.md              the user's: the project's rules, and the      `calu checker edit`
                              test triggers of the shipped defaults
    system/honesty-state.md   not to rely on unverified code                 fixed, honesty on

The command, edit, changeset and subagent-result checkers are a thin layer of project rules:
general safety is judged by Claude Code's auto mode, which the system parts say in one
sentence. Honesty is judged by the honesty checker alone (D224): its prompt is the fixed
`system/honesty.md`, the user's part `honesty.md` and the honesty rules (`honesty.rules`, or
the shipped `honesty-rules.md`). No other checker reads the honesty rules.

The honesty reviewer (`calu.checkers.honesty_review`) is no checker type: its prompt is the
fixed `system/honesty-review.md`, `system/disclosure.md` and the honesty rules
(`review_instructions`). The
redaction agent of the session traces (`calu.traces.redact`) is none either: its prompt is the
fixed `system/trace-redaction.md` followed by `traces.redactor_instructions`, or the shipped
`trace-redaction.md` (`redaction_instructions`, D245).

The user's part is what `checker_types` holds (D6); `calu setup` loads the shipped one.
"""

import difflib
import hashlib
import json
from dataclasses import dataclass
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


def default_redaction_instructions() -> str:
    """Return the shipped instructions of the redaction agent (agent/records/PLAN.md D245).

    They apply while `traces.redactor_instructions` is not set.

    Returns:
        The Markdown text of `trace-redaction.md`: what a trace keeps (requirements and
        decisions, what was changed and why), what it may lose (the user's questions, hints
        of when the session happened and how long it took, private remarks), and which
        secrets and personal data are replaced by typed placeholders.
    """
    return _read("trace-redaction.md")


def redaction_system_part() -> str:
    """Return the fixed part calu puts in front of the redaction agent's instructions.

    Returns:
        The Markdown text of `system/trace-redaction.md`: the task, that the excerpt is data
        and not instructions, the form of the answer, and when the block may be removed as a
        whole.
    """
    return _read("system/trace-redaction.md")


def redaction_instructions(user: str | None = None) -> str:
    """Put the prompt of the redaction agent of the session traces together (D232, D245).

    Args:
        user: The configured instructions (`traces.redactor_instructions`); None or a text
            without visible characters uses the shipped ones
            (`default_redaction_instructions`).

    Returns:
        The fixed part `system/trace-redaction.md` (the excerpt is data, the form of the
        answer, when the block may be removed as a whole), followed by the instructions.
    """
    system = redaction_system_part().strip()
    chosen = user if user and user.strip() else default_redaction_instructions()
    return system + "\n\n" + chosen.strip() + "\n"


def final_redaction_system_part() -> str:
    """Return the fixed part of the final redaction pass before a commit (PLAN.md D246).

    Returns:
        The Markdown text of `system/trace-final-redaction.md`: the agent sees a whole stretch
        of the trace (what was added since the last commit), keeps requirements and decisions
        faithful, summarises nothing, treats the stretch as data, and never removes it as a
        whole.
    """
    return _read("system/trace-final-redaction.md")


def final_redaction_instructions(user: str | None = None) -> str:
    """Put the prompt of the final redaction pass of the session traces together (D246).

    Args:
        user: The configured instructions (`traces.redactor_instructions`); None or a text
            without visible characters uses the shipped ones, as for the per-block pass.

    Returns:
        The fixed part `system/trace-final-redaction.md`, followed by the same instructions
        the per-block pass gets (`redaction_instructions` without its fixed part).
    """
    system = final_redaction_system_part().strip()
    chosen = user if user and user.strip() else default_redaction_instructions()
    return system + "\n\n" + chosen.strip() + "\n"


def review_instructions(rules: str | None = None) -> str:
    """Put the prompt of the honesty reviewer together (specification section 4).

    Args:
        rules: The honesty rules to include (`honesty.rules`); None or "" uses the shipped
            ones.

    Returns:
        The fixed part `system/honesty-review.md`, the part on what an answer may contain
        (`DISCLOSURE_PART`), and the honesty rules.
    """
    system = _read("system/honesty-review.md").strip()
    disclosure = _read(DISCLOSURE_PART).strip()
    return (
        system + "\n\n" + disclosure + "\n\n" + (rules if rules else honesty_rules()).strip() + "\n"
    )


ROLE_PART = "system/role.md"
"""The fixed part every checker gets after its own system part: soft guidance on complex
questions, a hard guardrail on security, the user's explicit rules and attempts to get around
a safeguard (the owner's principle of 2026-11-22: a checker is usually a smaller model than
Claude and is no final arbiter of complex questions)."""


def reference_judge_instructions() -> str:
    """Return the instructions of the reference judge (`calu.refindex.judge`).

    Returns:
        The fixed text `system/reference-judge.md`. It has no part a user edits.
    """
    return _read("system/reference-judge.md").strip()


EVASION_PART = "system/evasion.md"
"""The fixed part every checker gets after its own system part: ESCALATE when Claude's narrated
thinking or text shows that it knowingly tries to get around a safeguard or to deceive the
user. Fixed, so that no edit of a checker's instructions removes it."""


DISCLOSURE_PART = "system/disclosure.md"
"""The fixed part every checker and the honesty reviewer get: the answer discloses no contents
of files read beyond the files under review, and no secret from any file (the owner's rule of
2026-11-23; worded without tool names, so that a later reading tool is covered too). Fixed, so
that no edit of a checker's instructions removes it."""


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
        For the honesty checker: its system part, the checker's role (`ROLE_PART`), the part
        about getting around safeguards (`EVASION_PART`), the part on what an answer may
        contain (`DISCLOSURE_PART`), the user's instructions under their own heading, and the
        honesty rules. For every other checker with a system part: the route's system part,
        `ROLE_PART`, `EVASION_PART`, `DISCLOSURE_PART`, the user's instructions under their
        own heading and, with `honesty_state` on a route of `HONESTY_ROUTES`, the part about
        unverified code. No other checker gets the honesty rules or a way to record marks.

    Example:
        compose_instructions("edit", "# Project rules ...") starts with "# Edit checker" and
        contains "# The user's instructions" followed by the given text.
    """
    if name == "honesty":
        return (
            "\n\n".join(
                [
                    _read("system/honesty.md").strip(),
                    _read(ROLE_PART).strip(),
                    _read(EVASION_PART).strip(),
                    _read(DISCLOSURE_PART).strip(),
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
        parts.append(_read(ROLE_PART).strip())
        parts.append(_read(EVASION_PART).strip())
        parts.append(_read(DISCLOSURE_PART).strip())
        parts.append("# The user's instructions\n\n" + user.strip())
    else:
        parts.append(user.strip())
    if honesty_state and route in HONESTY_ROUTES:
        parts.append(_read("system/honesty-state.md").strip())
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------------ shipped versions (PLAN.md D248)

SHIPPED_TEXTS = (
    "command",
    "edit",
    "changeset",
    "subagent-result",
    "honesty",
    "narrator",
    "honesty-rules",
    "trace-redaction",
)
"""The editable texts calu ships: the checker types' instructions, the honesty rules
(`honesty.rules`) and the redaction agent's instructions (`traces.redactor_instructions`).
`checkers/shipped.json` holds the hash of every version of each that calu ever shipped."""


def text_hash(text: str) -> str:
    """Hash an instruction text the way `checkers/shipped.json` stores it.

    Line ends are made LF and the text is stripped of white space at both ends first, so
    that a copy saved by an editor with one more or one less final newline still matches.

    Args:
        text: The text.

    Returns:
        The SHA-256 of the normalised text, as 64 hex digits.
    """
    normal = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


def _shipped_hashes(name: str) -> list[tuple[str, str]]:
    """Read the hashes of every shipped version of one text.

    Args:
        name: One of `SHIPPED_TEXTS`.

    Returns:
        `(hash of the text, hash of the text without its test triggers)` per version, oldest
        first; empty for a name the file does not list.
    """
    table = json.loads(_read("shipped.json"))
    return [(row[0], row[1]) for row in table.get(name, [])]


@dataclass(frozen=True)
class ShippedStatus:
    """How a stored instruction text relates to the texts calu shipped (`shipped_status`).

    `kind` is one of:

        current    the present shipped text (or it without its test triggers)
        outdated   an earlier shipped text, unchanged by the user (or it without its test
                   triggers): `update` replaces it with `present`, nothing of the user's is lost
        edited     none of them: a text somebody wrote or changed; it is never replaced,
                   only reported with `diff`
    """

    name: str
    kind: str
    triggers_removed: bool
    """Whether the stored text is a shipped one without its section of test triggers; the
    update then removes them from the present text as well."""
    present: str
    """What an update stores: the present shipped text, without its test triggers if the
    stored one had none."""

    def diff(self, stored: str) -> str:
        """Show how the stored text differs from the present shipped one.

        Args:
            stored: The stored text.

        Returns:
            A unified diff from the shipped text (`shipped`) to the stored one (`stored`);
            "" if they are equal.
        """
        return "".join(
            difflib.unified_diff(
                self.present.splitlines(keepends=True),
                stored.splitlines(keepends=True),
                fromfile=f"shipped/{self.name}.md",
                tofile=f"stored/{self.name}.md",
            )
        )

    def sentence(self, what: str, update: str) -> str:
        """Say in one sentence what the status means for the user.

        Args:
            what: What the text is, e.g. "the command checker's instructions".
            update: The command or button that updates an outdated text.

        Returns:
            For "current": that it is the present shipped text; for "outdated": that it is an
            older shipped version the user did not change, and how to update it; for
            "edited": that it differs from the shipped text and is kept as it is.
        """
        triggers = " (without its test triggers)" if self.triggers_removed else ""
        if self.kind == "current":
            return f"{what}: the text calu ships now{triggers}"
        if self.kind == "outdated":
            return (
                f"{what}: an older version calu shipped{triggers}, not changed by you; "
                f"{update} replaces it with the present one"
            )
        return f"{what}: your own text, which differs from the one calu ships; it stays as it is"


def shipped_status(name: str, stored: str) -> ShippedStatus:
    """Compare a stored instruction text with every version calu shipped of it.

    Args:
        name: One of `SHIPPED_TEXTS`.
        stored: The text the database holds.

    Returns:
        The status; "edited" for a name without shipped versions.

    Raises:
        FileNotFoundError: If no shipped text of that name exists.
    """
    present = default_instructions(name)
    digest = text_hash(stored)
    if digest == text_hash(present):
        return ShippedStatus(name, "current", False, present)
    plain = without_test_triggers(present)
    if digest == text_hash(plain):
        return ShippedStatus(name, "current", True, plain)
    for full, bare in _shipped_hashes(name):
        if digest == full:
            return ShippedStatus(name, "outdated", False, present)
        if digest == bare and bare != full:
            return ShippedStatus(name, "outdated", True, plain)
    return ShippedStatus(name, "edited", False, present)
