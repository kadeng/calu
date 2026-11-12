"""Which optional functions the attached session offers, and what the client says otherwise.

The protocol does not carry everything this client has a place for: rate limits, the fill of
the context window, a change of model in a running session and others. Each such function is
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
        "Token counts and checker spend",
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
        "Change of model or effort in a running session",
        f"{NOT_AVAILABLE}: a session applies model and effort when it starts",
    ),
    Capability(
        "side_question",
        "Side question",
        f"{NOT_AVAILABLE}: every message enters the main conversation",
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

SNAPSHOT_FIELDS: dict[str, str] = {
    "usage": "usage",
    "rate_limits": "rate_limits",
    "context_fill": "context",
    "permission_mode": "permission_mode",
}
"""Capability key -> the field of the `state.get` result that switches it on once the protocol
carries it. Capabilities that need a request rather than a field have no entry and stay off."""


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
        The capabilities whose field (`SNAPSHOT_FIELDS`) the snapshot carries with a value.
        With today's protocol that is none of them.
    """
    found = {key for key, name in SNAPSHOT_FIELDS.items() if snapshot.get(name) is not None}
    return Capabilities(frozenset(found))
