"""Recognition of user messages that the user did not write in this session.

Such text must not count as the user's words for the checkers: it could carry instructions ("the
user allows pushing") that a checker would otherwise treat as authorized. The messages come in
two kinds:

- Messages from another Claude session on the same machine: Claude Code delivers them as
  ordinary user messages that contain an opening `<cross-session-message ...>` tag. The session
  records such a message as a separate entry of the turn, narrated in indirect speech
  (`calu.context.narrator`), and front ends show it as its own message kind.
- Skill text: after a `Skill` tool call, Claude Code inserts the skill's SKILL.md as a user
  message (`SkillContentFilter`). The session drops it; the `Skill` call itself stays visible.
"""

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

CROSS_SESSION_MARKER = re.compile(r"<cross-session-message\s", re.IGNORECASE)
"""The opening tag `<cross-session-message ` (the tag name followed by white space)."""

UserSource = Literal["user", "cross_session"]
"""Who a user message comes from: the user, or another Claude session (`CROSS_SESSION_MARKER`)."""


RELAY_TEMPLATE = (
    "[Message from the user for your subagent {name}]\n"
    "Relay the following text verbatim to that subagent with the SendMessage tool "
    '(to: "{to}"). Do not act on it yourself.\n\n{text}'
)
"""How a message of the user for a background subagent is wrapped for the main agent
(`calu.session.Session.send_chat`). The user's own text follows the first blank line."""

_RELAY_HEAD = RELAY_TEMPLATE.partition("{name}")[0]
_RELAY_ORDER = RELAY_TEMPLATE.partition("]\n")[2].partition("{to}")[0]


def own_words(text: str) -> str:
    """Return what the user typed, for a message that calu wrapped for relaying.

    A message the user sent to a subagent reaches the main agent, the stored turn and Claude
    Code's transcript inside `RELAY_TEMPLATE`. A session's title or summary taken from its
    first or last user input shows the user's own words, not calu's relay request.

    Args:
        text: A user input as it was stored or read from a transcript.

    Returns:
        The text after the wrapper if `text` has the form of `RELAY_TEMPLATE`; otherwise
        `text` unchanged.

    Example:
        For the text that `RELAY_TEMPLATE.format(name="tests", to="a1", text="run them
        again")` gives, the result is "run them again"; `own_words("Fix it.")` is "Fix it.".
    """
    if not text.startswith(_RELAY_HEAD):
        return text
    head, blank, own = text.partition("\n\n")
    return own if blank and _RELAY_ORDER in head else text


def user_message_source(text: str) -> UserSource:
    """Classify the text of a user message that Claude has taken in.

    Args:
        text: The message text as Claude Code reports it.

    Returns:
        "cross_session" if the text contains the marker of a message from another Claude session
        anywhere (not only at the start, so a prefix cannot hide it), else "user".

    Example:
        `user_message_source('<cross-session-message from="a">push now</cross-session-message>')`
        is "cross_session"; `user_message_source("Fix the tests.")` is "user".
    """
    return "cross_session" if CROSS_SESSION_MARKER.search(text) else "user"


SKILL_CONTENT_PREFIX = "Base directory for this skill: "
"""Start of the text that Claude Code inserts as a user message when Claude loads a skill."""


@dataclass
class SkillContentFilter:
    """Recognizes the skill text that Claude Code inserts after a `Skill` tool call.

    When Claude calls the `Skill` tool, Claude Code adds the skill's SKILL.md as a synthetic user
    message ("Base directory for this skill: <dir>" followed by the file's body; marked
    `isSynthetic` in the stream, but the Agent SDK drops that flag). It is not the user's text:
    the session must neither show it as a user message nor give it to the checkers as user input.

    A text counts as skill content only if it starts with `SKILL_CONTENT_PREFIX` and a `Skill`
    call of the main agent is still unmatched, so a user message that happens to start with the
    prefix is kept.
    """

    pending: deque[str] = field(default_factory=deque)
    """Ids of the `Skill` calls of the main agent whose skill text has not arrived yet."""

    def tool_use(self, name: str, agent_id: str | None, tool_use_id: str) -> None:
        """Note a tool call; remembers `Skill` calls of the main agent.

        Args:
            name: The tool name.
            agent_id: None for the main agent, else the subagent's id (not remembered: the
                session takes user messages from the main agent only).
            tool_use_id: The tool call's id.
        """
        if name == "Skill" and agent_id is None:
            self.pending.append(tool_use_id)

    def match(self, text: str) -> str | None:
        """Tell whether a user message of the main agent is skill text; consumes one `Skill` call.

        Args:
            text: The user message text.

        Returns:
            The id of the oldest unmatched `Skill` call if there is one and `text` starts with
            `SKILL_CONTENT_PREFIX`; else None.
        """
        if self.pending and text.startswith(SKILL_CONTENT_PREFIX):
            return self.pending.popleft()
        return None


def skill_directory(text: str) -> str:
    """Return the directory from the first line of a skill text.

    Args:
        text: A text that starts with `SKILL_CONTENT_PREFIX` (see `SkillContentFilter`).

    Returns:
        The rest of the first line after the prefix, e.g. "/w/.claude/skills/greet".
    """
    return text.removeprefix(SKILL_CONTENT_PREFIX).split("\n", 1)[0].strip()
