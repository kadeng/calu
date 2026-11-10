"""ACP session modes and config options, mapped onto calu's auto-escalation categories.

An editor shows a picker for how much the agent may do on its own. calu's equivalent is
`AutoEscalate` (PLAN.md D21, D38): three independent switches that make every command, every
edit or every subagent result wait for a human, whatever the checker decided.

ACP has two APIs for this. **Session modes** (`modes`, `session/set_mode`,
`current_mode_update`) are one flat list of named modes, and the newer **session config
options** (`configOptions`, `session/set_config_option`, `config_option_update`) are a list of
typed options that supersede them; the specification asks an agent to offer both during the
transition and keep them in sync, because a client may understand only one.

The two APIs fit calu's three switches differently:

* A boolean config option is an exact fit -- one option per switch -- but a client must
  advertise `session.configOptions.boolean` before an agent may send one.
* A mode, and a select option, is one value out of a list, so the eight combinations of three
  switches have to be flattened. `LADDER` is that flattening: four modes of increasing
  strictness, each a superset of the one before. The combinations that are not on the ladder
  (commands without edits, for instance) are reachable with `calu config set` and the web
  interface, so the state of a running session need not be on it; `mode_of` then answers
  `CUSTOM`, which is offered as a mode only while it is the current one and cannot be selected.

So a client that supports boolean options gets the three switches themselves, and one that does
not gets the ladder.
"""

from dataclasses import dataclass
from typing import Any

from calu.config import AutoEscalate

MODE_CATEGORY = "mode"
"""The `category` of the select option that mirrors the mode list (a UX hint in ACP)."""

MODE_OPTION_ID = "mode"
"""`id` of that select option, and the `configId` a client sets it by."""

CUSTOM = "custom"
"""Mode id for a combination of switches that is not on `LADDER`."""


@dataclass(frozen=True)
class Mode:
    """One rung of the ladder: a mode id with the switches it stands for."""

    id: str
    name: str
    description: str
    settings: AutoEscalate


LADDER: tuple[Mode, ...] = (
    Mode(
        "checkers",
        "Checkers decide",
        "Only a checker's escalation waits for a human; what the checkers approve runs.",
        AutoEscalate(),
    ),
    Mode(
        "edits",
        "Confirm edits",
        "Every file edit waits for a human, even one the edit checker approved.",
        AutoEscalate(edits=True),
    ),
    Mode(
        "writes",
        "Confirm edits and commands",
        "Every file edit and every command waits for a human, whatever the checkers decided.",
        AutoEscalate(commands=True, edits=True),
    ),
    Mode(
        "everything",
        "Confirm everything",
        "Every edit, every command and every subagent result waits for a human.",
        AutoEscalate(commands=True, edits=True, subagents=True),
    ),
)
"""The modes offered to a client, from the least to the most strict."""

SWITCHES: dict[str, str] = {
    "auto_escalate_commands": "commands",
    "auto_escalate_edits": "edits",
    "auto_escalate_subagents": "subagents",
}
"""Boolean config option id -> field of `AutoEscalate`."""

SWITCH_NAMES: dict[str, tuple[str, str]] = {
    "auto_escalate_commands": (
        "Confirm every command",
        "Every command waits for a human, even one the command checker approved.",
    ),
    "auto_escalate_edits": (
        "Confirm every edit",
        "Every file edit waits for a human, even one the edit checker approved.",
    ),
    "auto_escalate_subagents": (
        "Confirm every subagent result",
        "The final result of every subagent waits for a human.",
    ),
}
"""Boolean config option id -> (name, description) the client shows."""

CUSTOM_MODE = Mode(
    CUSTOM,
    "Custom",
    "A combination set outside the editor, with `calu config set` or the web interface.",
    AutoEscalate(),
)
"""Shown only while it is the current mode; `settings_for` refuses it as a target."""


def mode_of(auto: AutoEscalate) -> str:
    """Return the mode id that stands for these switches.

    Args:
        auto: The auto-escalation categories of the session.

    Returns:
        The id of the matching rung of `LADDER`, or `CUSTOM` if no rung matches exactly.
    """
    for mode in LADDER:
        if mode.settings == auto:
            return mode.id
    return CUSTOM


def settings_for(mode_id: str) -> AutoEscalate:
    """Return the switches a mode stands for.

    Args:
        mode_id: A mode id from `LADDER`.

    Returns:
        The switches of that mode.

    Raises:
        ValueError: If the id is unknown, or is `CUSTOM` -- which describes a state rather than
            naming one, so it cannot be selected.
    """
    if mode_id == CUSTOM:
        raise ValueError(
            "'custom' says that the categories were set outside the editor; it cannot be chosen. "
            "Pick one of the other modes, or set the categories with calu."
        )
    for mode in LADDER:
        if mode.id == mode_id:
            return mode.settings
    known = ", ".join(mode.id for mode in LADDER)
    raise ValueError(f"unknown mode {mode_id!r}; calu offers {known}")


def _entry(mode: Mode) -> dict[str, Any]:
    """Turn a mode into an `availableModes` entry.

    Args:
        mode: The mode.

    Returns:
        Its ACP representation.
    """
    return {"id": mode.id, "name": mode.name, "description": mode.description}


def mode_state(auto: AutoEscalate) -> dict[str, Any]:
    """Build the `modes` object of `session/new` and `session/load`.

    Args:
        auto: The auto-escalation categories of the session.

    Returns:
        `{"currentModeId": ..., "availableModes": [...]}`; the `custom` entry is in the list
        only when the session's switches are not on the ladder, because ACP requires the
        current mode to be one of the advertised ones.
    """
    current = mode_of(auto)
    available = [_entry(mode) for mode in LADDER]
    if current == CUSTOM:
        available.append(_entry(CUSTOM_MODE))
    return {"currentModeId": current, "availableModes": available}


def mode_update(auto: AutoEscalate) -> dict[str, Any]:
    """Build a `current_mode_update` notification body.

    Args:
        auto: The auto-escalation categories the session now uses.

    Returns:
        The `update` object of a `session/update` notification.
    """
    return {"sessionUpdate": "current_mode_update", "currentModeId": mode_of(auto)}


def config_options(auto: AutoEscalate, booleans: bool) -> list[dict[str, Any]]:
    """Build the `configOptions` list for a client.

    Args:
        auto: The auto-escalation categories of the session.
        booleans: Whether the client advertised `session.configOptions.boolean`. An agent must
            not send boolean options to a client that did not, so with False the list holds only
            the select option, which every client understands.

    Returns:
        The select option that mirrors the mode list first (clients should respect the order),
        then one boolean option per switch if they are allowed.
    """
    state = mode_state(auto)
    options: list[dict[str, Any]] = [
        {
            "id": MODE_OPTION_ID,
            "name": "What waits for a human",
            "description": "How much of what the checkers approve still needs a decision.",
            "category": MODE_CATEGORY,
            "type": "select",
            "currentValue": state["currentModeId"],
            "options": [
                {"value": entry["id"], "name": entry["name"], "description": entry["description"]}
                for entry in state["availableModes"]
            ],
        }
    ]
    if booleans:
        for option_id, field in SWITCHES.items():
            name, description = SWITCH_NAMES[option_id]
            options.append(
                {
                    "id": option_id,
                    "name": name,
                    "description": description,
                    "category": MODE_CATEGORY,
                    "type": "boolean",
                    "currentValue": getattr(auto, field),
                }
            )
    return options


def config_update(auto: AutoEscalate, booleans: bool) -> dict[str, Any]:
    """Build a `config_option_update` notification body.

    ACP wants the complete list of options in an update, not only what changed, so this is
    `config_options` wrapped in the notification.

    Args:
        auto: The auto-escalation categories the session now uses.
        booleans: Whether the client may receive boolean options (see `config_options`).

    Returns:
        The `update` object of a `session/update` notification.
    """
    return {
        "sessionUpdate": "config_option_update",
        "configOptions": config_options(auto, booleans),
    }


def with_option(auto: AutoEscalate, config_id: str, value: Any) -> AutoEscalate:
    """Return the switches that setting one config option leads to.

    Args:
        auto: The switches the session uses now.
        config_id: The option's id: `MODE_OPTION_ID` or one of `SWITCHES`.
        value: The new value -- a mode id for the select option, a boolean for a switch.

    Returns:
        The new switches; `auto` is not changed.

    Raises:
        ValueError: If the id is unknown, or the value does not fit the option (an unknown mode
            id, `custom`, or a non-boolean for a switch).
    """
    if config_id == MODE_OPTION_ID:
        if not isinstance(value, str):
            raise ValueError(f"the mode option takes a mode id, not {type(value).__name__}")
        return settings_for(value)
    field = SWITCHES.get(config_id)
    if field is None:
        known = ", ".join([MODE_OPTION_ID, *SWITCHES])
        raise ValueError(f"unknown config option {config_id!r}; calu offers {known}")
    if not isinstance(value, bool):
        raise ValueError(f"{config_id} takes true or false, not {type(value).__name__}")
    return auto.model_copy(update={field: value})
