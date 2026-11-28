"""The model presets an editor offers for "use one model for everything".

    calu.web.settings.MODEL_OPTIONS ─┐
    calu.config.BEDROCK_EU_HAIKU  ───┼──► PRESETS ──► `ide.models.presets` ──► the editor
    calu.model_choice.FIREWORKS_MODEL┘

The editor shows what `presets` returns and holds no model name of its own, so a model that
changes here changes in every editor. Every preset's model is one of the models the web
settings form offers (`MODEL_OPTIONS`); `_check` fails at import time if that stops being true.
The list holds small and medium-priced models only; of Mistral's models only the small one
(project rule: never Mistral Medium or Large).
"""

from dataclasses import asdict, dataclass

from calu.config import BEDROCK_EU_HAIKU
from calu.model_choice import FIREWORKS_MODEL
from calu.web.settings import MODEL_OPTIONS

DEFAULT_ID = "default"
"""The id of the preset that stores no model, and the value `ide.models.set_all` takes for
it, as `calu config models default` does."""


@dataclass(frozen=True)
class Preset:
    """One model an editor offers."""

    id: str
    """A stable name for the editor, e.g. "anthropic-haiku"."""
    label: str
    """What the editor shows: provider and model."""
    group: str
    """The provider, for grouping the presets."""
    model: str
    """The Pydantic AI model string that is stored; "" for the preset that stores none."""
    note: str
    """What the preset needs to work: which key or which sign-in."""


def _mistral_small() -> str:
    """Find the small Mistral model among the offered models.

    Returns:
        Its Pydantic AI model string.

    Raises:
        LookupError: If `MODEL_OPTIONS` offers no Mistral model whose label says "Small".
    """
    for _provider, options in MODEL_OPTIONS:
        for model, label in options:
            if model.startswith("mistral:") and "Small" in label:
                return model
    raise LookupError("MODEL_OPTIONS offers no small Mistral model")


_ANTHROPIC_NOTE = "Needs ANTHROPIC_API_KEY in calu's env file (~/.config/calu/.env)."
_BEDROCK_NOTE = (
    "Needs AWS credentials that may call Bedrock (the usual AWS environment variables, "
    "profile or instance role) and access to the model in the account's region."
)

PRESETS: tuple[Preset, ...] = (
    Preset(
        DEFAULT_ID,
        "calu's default (Haiku 5.5; Bedrock in Bedrock mode)",
        "Default",
        "",
        "Stores no model: every role uses Claude Haiku 5.5 through the Anthropic API, or "
        "Haiku through AWS Bedrock when `claude.use_bedrock` is on. Needs ANTHROPIC_API_KEY, "
        "or AWS credentials in Bedrock mode.",
    ),
    Preset(
        "anthropic-haiku",
        "Anthropic · Claude Haiku 5.5",
        "Anthropic",
        "anthropic:claude-haiku-5-5",
        _ANTHROPIC_NOTE,
    ),
    Preset(
        "anthropic-sonnet",
        "Anthropic · Claude Sonnet 5.5",
        "Anthropic",
        "anthropic:claude-sonnet-5-5",
        _ANTHROPIC_NOTE + " Slower and more expensive than Haiku.",
    ),
    Preset(
        "bedrock-haiku",
        "AWS Bedrock · Claude Haiku 5.5",
        "AWS Bedrock",
        "bedrock:anthropic.claude-haiku-5-5",
        _BEDROCK_NOTE,
    ),
    Preset(
        "bedrock-sonnet",
        "AWS Bedrock · Claude Sonnet 5.5",
        "AWS Bedrock",
        "bedrock:anthropic.claude-sonnet-5-5",
        _BEDROCK_NOTE + " Slower and more expensive than Haiku.",
    ),
    Preset(
        "bedrock-eu-haiku",
        "AWS Bedrock · Claude Haiku 4.5 (EU profile)",
        "AWS Bedrock",
        BEDROCK_EU_HAIKU,
        _BEDROCK_NOTE + " Uses the EU inference profile, so requests stay in EU regions.",
    ),
    Preset(
        "fireworks-nemotron",
        "Fireworks · Nemotron Lightning 3.5",
        "Fireworks",
        FIREWORKS_MODEL,
        "Needs FIREWORKS_API_KEY in calu's env file (~/.config/calu/.env).",
    ),
    Preset(
        "mistral-small",
        "Mistral · Mistral Small",
        "Mistral",
        _mistral_small(),
        "Needs MISTRAL_API_KEY in calu's env file (~/.config/calu/.env).",
    ),
)
"""The presets in the order an editor shows them."""


def _check() -> None:
    """Check that every preset names a model the settings form offers too.

    Raises:
        LookupError: If a preset's model is not in `MODEL_OPTIONS`.
    """
    offered = {model for _provider, options in MODEL_OPTIONS for model, _label in options}
    for preset in PRESETS:
        if preset.model and preset.model not in offered:
            raise LookupError(f"the preset {preset.id} names {preset.model}, which is not offered")


_check()


def presets() -> list[dict[str, str]]:
    """Return the presets as JSON objects.

    Returns:
        A list with one `{id, label, group, model, note}` object per preset, in display
        order.
    """
    return [asdict(preset) for preset in PRESETS]


def model_of(value: str) -> str:
    """Turn what `ide.models.set_all` was given into the model string to store.

    Args:
        value: A preset id, "default", or a Pydantic AI model string.

    Returns:
        The preset's model for a preset id ("" for "default"), else `value` unchanged.
    """
    for preset in PRESETS:
        if preset.id == value:
            return preset.model
    return value
