"""Which optional functions the attached session offers, and what the client says otherwise.

Not every session carries everything this client has a place for: an older calu has no side
question, no usage figures and no change of model in a running session, and no session can
withdraw a queued message or stop one subagent. Each such function is
one `Capability` here, with the sentence the client shows while the session does not offer it.
Every "not available from this session" text of the client comes from this table, so the list
of missing functions in the help screen and the texts in the views cannot disagree, and a
protocol addition switches a function on in one place (`capabilities`).
"""

from dataclasses import dataclass
from typing import Any

NOT_AVAILABLE = "not available from this session"
"""The phrase every such text starts with."""


@dataclass(frozen=True)
class Capability:
    """One optional function: its name in the client and the text shown while it is missing."""

    key: str
    title: str
    reason: str


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "usage",
        "Usage figures (cost, context, limits)",
        f"{NOT_AVAILABLE}: it reports the cost and the number of turns only",
    ),
    Capability(
        "rate_limits",
        "Used quota (5-hour and weekly limits)",
        f"{NOT_AVAILABLE}: it does not pass on the rate-limit state of Claude Code",
    ),
    Capability(
        "context_fill",
        "Fill of the context window",
        f"{NOT_AVAILABLE}: it does not report the size of the conversation",
    ),
    Capability(
        "live_model_change",
        "Change of model in a running session",
        f"{NOT_AVAILABLE}: it applies the model when it starts",
    ),
    Capability(
        "side_question",
        "Side question (/btw)",
        f"{NOT_AVAILABLE}: every message enters the main conversation",
    ),
    Capability(
        "live_effort_change",
        "Change of effort in a running session",
        f"{NOT_AVAILABLE}: the effort is fixed when a session starts (/effort, then /stop and"
        " /start)",
    ),
    Capability(
        "subagent_direct",
        "Messages to a foreground or finished subagent",
        f"{NOT_AVAILABLE}: only a running background subagent takes messages, relayed by the"
        " main agent",
    ),
    Capability(
        "queue_withdraw",
        "Withdraw or edit a queued message",
        f"{NOT_AVAILABLE}: a sent message cannot be withdrawn",
    ),
    Capability(
        "stop_subagent",
        "Stop one subagent",
        f"{NOT_AVAILABLE}: interrupt ends the whole turn",
    ),
    Capability(
        "permission_mode",
        "Permission mode of Claude Code",
        f"{NOT_AVAILABLE}: the session names the mode in its log only",
    ),
)
"""Every optional function the client has a place for, in the order the help screen lists
them."""

_BY_KEY = {capability.key: capability for capability in CAPABILITIES}

GROWN_WITH_RATE_LIMITS = frozenset(
    {"usage", "rate_limits", "context_fill", "live_model_change", "side_question"}
)
"""The functions a session offers if its snapshot carries `rate_limits`: that field came into
the protocol together with the requests `side.ask`, `usage.get` and `model.set` and the event
`usage.update`. The protocol version did not change with them, so the field is the sign."""


@dataclass(frozen=True)
class Capabilities:
    """The optional functions the attached session offers."""

    available: frozenset[str] = frozenset()

    def has(self, key: str) -> bool:
        """Tell whether the session offers a function.

        Args:
            key: A key of `CAPABILITIES`.

        Returns:
            True if the function can be used.
        """
        return key in self.available

    def reason(self, key: str) -> str | None:
        """Return the text to show for a function the session does not offer.

        Args:
            key: A key of `CAPABILITIES`.

        Returns:
            None if the session offers the function, else the sentence that says why not.

        Raises:
            KeyError: If the key names no capability.
        """
        return None if key in self.available else _BY_KEY[key].reason

    def missing(self) -> list[Capability]:
        """List the functions the session does not offer.

        Returns:
            The capabilities that are off, in the order of `CAPABILITIES`.
        """
        return [capability for capability in CAPABILITIES if capability.key not in self.available]


def capabilities(snapshot: dict[str, Any]) -> Capabilities:
    """Derive the capabilities from a `state.get` result.

    Args:
        snapshot: The snapshot as the session returned it.

    Returns:
        `GROWN_WITH_RATE_LIMITS` if the snapshot has the field `rate_limits`, else none. The
        other capabilities need protocol additions that do not exist yet and stay off.
    """
    return Capabilities(GROWN_WITH_RATE_LIMITS if "rate_limits" in snapshot else frozenset())
