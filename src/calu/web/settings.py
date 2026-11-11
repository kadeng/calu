"""The settings form, generated from the configuration model itself.

Everything calu can be configured with is a field of `calu.config.Config`: which model and
reasoning effort Claude runs with, whether `CLAUDE_CODE_USE_BEDROCK=1` is passed to it, which
model and effort each checker uses, the sandbox, the context budget, the clock watch, the write
sets. The web form is **derived** from that model rather than written out by hand, so a new
field appears in the browser as soon as it exists in the model, with the right control:

    bool            -> a checkbox
    Literal / enum  -> a dropdown (the model and effort selectors are these)
    int / float     -> a number field
    list[str]       -> one value per line
    dict            -> JSON
    BaseModel       -> a section of its own

Writing goes through `calu.admin.set_setting`, which validates the whole configuration against
the model before it stores anything and writes the audit log, so the form cannot store a value
that a session would then choke on. Only the fields a human actually changed are written.
"""

import json
import re
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from calu.config import Config

Kind = Literal["bool", "choice", "int", "number", "text", "lines", "json"]
"""Which control a field gets in the browser."""

MODEL_SUGGESTIONS = ("fable", "opus", "sonnet", "haiku")
"""Claude Code's model aliases, offered next to the free-text field (`claude.model`)."""

SKIP = frozenset({"hidden_env"})
"""Fields the form leaves out: secrets-adjacent lists that belong in the environment."""


@dataclass(frozen=True)
class Setting:
    """One editable configuration value.

    `path` is the dotted key `calu config set` would take, so the form and the command line
    agree on names. `value` is what is configured now (the default if nothing is set).
    """

    path: str
    label: str
    kind: Kind
    value: Any
    choices: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    help: str = ""
    default: Any = None
    """The model's default, so the page can say what a changed value replaced (D138)."""
    is_set: bool = False
    """Whether this scope stores the value, rather than inheriting the default."""

    @property
    def changed(self) -> bool:
        """Whether the value differs from the default.

        Returns:
            True when it does; a value stored but equal to the default is not "changed".
        """
        return self.value != self.default

    @property
    def default_text(self) -> str:
        """Render the default the way the control shows a value.

        Returns:
            The default as a one-line text: `(empty)` for nothing, lists joined with commas.
        """
        if self.default in (None, "", [], {}):
            return "(empty)"
        if isinstance(self.default, bool):
            return "on" if self.default else "off"
        if isinstance(self.default, (list, tuple)):
            return ", ".join(str(item) for item in self.default)
        if isinstance(self.default, dict):
            return json.dumps(self.default, sort_keys=True)
        return str(self.default)

    @property
    def help_parts(self) -> tuple[str, str]:
        """Split the help into its first sentence and the rest.

        The page shows the first sentence and folds the rest, because most of the descriptions
        explain a decision in several sentences and a page of 75 settings cannot show them all
        at once (D138).

        Returns:
            (first sentence, everything after it, possibly "").
        """
        text = " ".join(self.help.split())
        # Only at a full stop followed by a capital letter: a colon or a semicolon usually
        # introduces an example (`e.g. ...: ...`), and cutting there left half a code sample.
        for match in re.finditer(r"\.(\s+)(?=[A-Z])", text):
            head = text[: match.start() + 1]
            if head.endswith(("e.g.", "i.e.", "etc.", "vs.")):
                continue
            return head, text[match.end() :]
        return text, ""

    @property
    def text(self) -> str:
        """Render the value for a text, number or lines control.

        Returns:
            The value as the form shows it: one item per line for a list, JSON for a dict,
            "" for None, else the value as a string.
        """
        if self.value is None:
            return ""
        if self.kind == "lines":
            return "\n".join(str(item) for item in self.value or ())
        if self.kind == "json":
            return json.dumps(self.value, indent=2, sort_keys=True)
        return str(self.value)


@dataclass
class Section:
    """A group of settings, named after the part of the configuration it comes from."""

    name: str
    title: str
    settings: list[Setting] = field(default_factory=list)


def sections(values: Mapping[str, Any]) -> list[Section]:
    """Build the whole form from the configuration model and the values of one scope.

    Args:
        values: What is configured in this scope, as `calu.admin.settings` returns it (a nested
            dict; missing keys fall back to the model's defaults).

    Returns:
        One section for the top-level fields ("general") and one per nested model, in the order
        the model declares them.
    """
    general = Section(name="general", title="General")
    found = [general]
    for name, info in Config.model_fields.items():
        if name in SKIP:
            continue
        annotation = info.annotation
        nested = _model_of(annotation)
        current = values.get(name)
        if nested is not None:
            section = Section(name=name, title=_label(name))
            section.settings = _settings_of(
                nested, prefix=name, values=current if isinstance(current, dict) else {}
            )
            found.append(section)
            continue
        setting = _setting(name, name, info, current)
        if setting is not None:
            general.settings.append(setting)
    return [section for section in found if section.settings]


def changes(values: Mapping[str, Any], posted: Mapping[str, str]) -> dict[str, Any]:
    """Work out which settings the human actually changed.

    Args:
        values: The values of the scope before the form was submitted.
        posted: The submitted form, by dotted key. A checkbox that is off is absent, which is
            how HTML works, so every known boolean counts as False when it is missing.

    Returns:
        The changed settings by dotted key, with values of the right type. A value the form
        could not parse is left out.

    Raises:
        ValueError: If a JSON field does not hold valid JSON; the caller reports it.
    """
    changed: dict[str, Any] = {}
    for section in sections(values):
        for setting in section.settings:
            if setting.kind == "bool":
                new: Any = setting.path in posted
            elif setting.path not in posted:
                continue
            else:
                new = parse(setting.kind, posted[setting.path])
            if new != setting.value:
                changed[setting.path] = new
    return changed


def parse(kind: Kind, raw: str) -> Any:
    """Turn one submitted field into the value the configuration model expects.

    Args:
        kind: The control the field was rendered with.
        raw: What the browser sent.

    Returns:
        The parsed value; an empty text field becomes None, so that a field can be cleared back
        to the model's default.

    Raises:
        ValueError: If a number is not a number or a JSON field is not valid JSON.
    """
    text = raw.strip()
    match kind:
        case "bool":
            return text not in ("", "0", "false", "off")
        case "int":
            return int(text) if text else None
        case "number":
            return float(text) if text else None
        case "lines":
            return [line.strip() for line in raw.splitlines() if line.strip()]
        case "json":
            return json.loads(text) if text else {}
        case _:
            return text or None


# ------------------------------------------------------------------ building the fields


def _settings_of(model: type[BaseModel], prefix: str, values: Mapping[str, Any]) -> list[Setting]:
    """Build the settings of one model, flattening a model inside it into dotted keys.

    Args:
        model: The configuration model.
        prefix: The dotted prefix its fields get.
        values: What is configured for this part.

    Returns:
        Its settings, in declaration order; a model inside it contributes its own fields with a
        longer dotted key (for example "claude.sandbox.enabled").
    """
    found: list[Setting] = []
    for name, info in model.model_fields.items():
        if name in SKIP:
            continue
        path = f"{prefix}.{name}"
        current = values.get(name) if isinstance(values, Mapping) else None
        nested = _model_of(info.annotation)
        if nested is not None:
            found.extend(
                _settings_of(nested, path, current if isinstance(current, Mapping) else {})
            )
            continue
        setting = _setting(path, name, info, current)
        if setting is not None:
            found.append(setting)
    return found


def _setting(path: str, name: str, info: Any, current: Any) -> Setting | None:
    """Build one setting from a model field.

    Args:
        path: Its dotted key.
        name: Its field name, for the label.
        info: The Pydantic `FieldInfo`.
        current: What is configured, or None to use the field's default.

    Returns:
        The setting, or None for a field the form cannot show (an unsupported type).
    """
    kind, choices = _kind_of(info.annotation)
    if kind is None:
        return None
    default = _default(info)
    value = current if current is not None else default
    return Setting(
        path=path,
        label=_label(name),
        kind=kind,
        value=value,
        choices=choices,
        suggestions=MODEL_SUGGESTIONS if path == "claude.model" else (),
        help=str(info.description or ""),
        default=default,
        is_set=current is not None,
    )


def _kind_of(annotation: Any) -> tuple[Kind | None, tuple[str, ...]]:
    """Decide which control a type gets.

    Args:
        annotation: The field's type annotation.

    Returns:
        (kind, choices): the control and, for a dropdown, its options. (None, ()) for a type the
        form does not render.
    """
    inner = _without_none(annotation)
    origin = get_origin(inner)
    if get_origin(inner) is Literal:
        return "choice", tuple(str(value) for value in get_args(inner))
    if isinstance(inner, type) and issubclass(inner, Enum):
        return "choice", tuple(str(member.value) for member in inner)
    if inner is bool:
        return "bool", ()
    if inner is int:
        return "int", ()
    if inner is float:
        return "number", ()
    if inner is str:
        return "text", ()
    if origin in (list, tuple, Sequence):
        return "lines", ()
    if origin is dict:
        return "json", ()
    return None, ()


def _without_none(annotation: Any) -> Any:
    """Strip `| None` from a type.

    Args:
        annotation: The annotation.

    Returns:
        The annotation without `NoneType`, or the annotation itself if it is not a union.
    """
    if get_origin(annotation) in (types.UnionType, __import__("typing").Union):
        rest = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0]
        for arg in rest:  # a union of several Literals: take the first that is one
            if get_origin(arg) is Literal:
                return arg
        return rest[0] if rest else annotation
    return annotation


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """Return the nested configuration model of a field, if it is one.

    Args:
        annotation: The field's type annotation.

    Returns:
        The model class, or None.
    """
    inner = _without_none(annotation)
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner
    return None


def _default(info: Any) -> Any:
    """Return a field's default value.

    Args:
        info: The Pydantic `FieldInfo`.

    Returns:
        The default, the result of its factory, or None if it has neither.
    """
    if info.default_factory is not None:
        return info.default_factory()  # type: ignore[call-arg]
    return None if repr(info.default) == "PydanticUndefined" else info.default


def _label(name: str) -> str:
    """Turn a field name into a label.

    Args:
        name: The field name, e.g. "use_bedrock".

    Returns:
        The name with underscores as spaces and the first letter upper case, and a unit suffix
        shown as a unit: `escalation_timeout_s` is "Escalation timeout (s)", not "... s".
    """
    for suffix, unit in (("_ms", "ms"), ("_s", "s"), ("_kb", "kB"), ("_mb", "MB")):
        if name.endswith(suffix):
            return f"{name[: -len(suffix)].replace('_', ' ').capitalize()} ({unit})"
    return name.replace("_", " ").capitalize()
