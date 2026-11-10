"""The configuration reference, generated from `calu.config.Config`.

`docs/CONFIGURATION.md` is this module's output, and `tests/unit/test_configdoc.py` fails when
the committed file and the model disagree -- so a new setting cannot be added without its
documentation, and the documentation cannot describe a setting that no longer exists.

Each field's text is its attribute docstring (`ConfigModel` turns those into descriptions), so
the reference, the web interface's settings form and the JSON schema of the protocol all say the
same thing.
"""

import json
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from calu.config import Config

HEADER = """# calu configuration

Every value below is a row in the database, not a file in the project: a session may read the
configuration and may not change it (`PLAN.md` §6.2). Read and change it with the `admin`
credential:

```bash
calu config get                                  # everything the global scope stores
calu config get claude.sandbox                   # one subtree
calu config set claude.model opus                # one value
calu config add claude.sandbox.deny_read ~/x     # one entry of a list, keeping the others
calu config remove claude.sandbox.deny_read ~/x
calu config unset claude.model                   # back to the default below
calu config set claude.model opus --project demo # the project's scope instead of the global one
```

A project's scope overrides the global one, and both override the defaults listed here. The
four `*_checker` groups have one more source below them: the checker definition
(`calu checker set <name> --model ...`) provides the model and the limits, and a value set here
overrides it -- so `calu config set command_checker.model` wins over the definition's own
model, per checker field. The web
interface has the same settings as a form (`calu web`, "Settings"). A running session applies a
change at its next step; what Claude Code reads when it starts -- its model, effort and
permission mode -- applies to the next session.

This file is generated from `calu.config` by `calu.configdoc`; a test keeps the two in step.
"""
"""What the generated file starts with, before the tables."""

SKIP = {"hidden_env"}
"""Fields left out of the reference: `hidden_env` is a security setting documented in
CALU_GUIDE.md's section on the checker keys, and listing it as an ordinary value invites
someone to put a key in it."""


def type_name(annotation: Any) -> str:
    """Render a field's type the way a user has to type its value.

    Args:
        annotation: The annotation from `FieldInfo.annotation`.

    Returns:
        A short name: "bool", "int", "number", "text", "list of text", "one of a, b" for a
        literal or an enum, a mapping, or the Python name when nothing fits.

    Example:
        `type_name(bool)` is "bool" and `type_name(list[str])` is "list of text".
    """
    if annotation is None:
        return "nothing"
    origin = get_origin(annotation)
    if origin is Union or str(origin) == "<class 'types.UnionType'>":
        parts = [type_name(arg) for arg in get_args(annotation) if arg is not type(None)]
        rendered = " or ".join(dict.fromkeys(parts))
        return f"{rendered} (or empty)"
    if origin is Literal:
        return "one of " + ", ".join(f"`{value}`" for value in get_args(annotation))
    if origin is list:
        return f"list of {type_name(get_args(annotation)[0])}"
    if origin is dict:
        key, value = get_args(annotation)
        return f"map of {type_name(key)} to {type_name(value)}"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "number"
    if annotation is str:
        return "text"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "one of " + ", ".join(f"`{member.value}`" for member in annotation)
    name = getattr(annotation, "__name__", None)
    return str(name or annotation)


def default_of(info: FieldInfo) -> str:
    """Render a field's default as the user would write it.

    Args:
        info: The field.

    Returns:
        The default as compact JSON, "(empty)" for None, or "see below" for a nested group of
        settings (whose own fields carry the defaults).
    """
    value = info.get_default(call_default_factory=True)
    if isinstance(value, BaseModel):
        return "see below"
    if value is None:
        return "(empty)"
    if isinstance(value, list | dict) and not value:
        return "(empty)"
    rendered = json.dumps(value, default=str)
    if len(rendered) > 70:
        # A long list is unreadable in a table cell; name where it comes from instead.
        return f"{len(value)} entries (see `calu config get`)"
    return f"`{rendered}`"


def _rows(model: type[BaseModel], prefix: str) -> list[tuple[str, str, str, str]]:
    """Collect the rows of one model, nested models included.

    Args:
        model: The model to walk.
        prefix: The dotted prefix of its fields ("" at the top).

    Returns:
        One (key, type, default, description) tuple per field, in the order the model declares
        them, a nested model's own fields directly after it.
    """
    rows: list[tuple[str, str, str, str]] = []
    for name, info in model.model_fields.items():
        key = f"{prefix}{name}"
        if key in SKIP:
            continue
        text = " ".join((info.description or "").split())
        rows.append((key, type_name(info.annotation), default_of(info), text))
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            rows += _rows(annotation, f"{key}.")
    return rows


def configuration_markdown() -> str:
    """Build the whole reference.

    Returns:
        The content of `docs/CONFIGURATION.md`: the header, then one table of every setting with
        its type, its default and what it does.

    Example:
        `"claude.model" in configuration_markdown()` is True.
    """
    lines = [
        HEADER.rstrip(),
        "",
        "## Settings",
        "",
        "| Key | Type | Default | What it does |",
        "|---|---|---|---|",
    ]
    for key, kind, default, text in _rows(Config, ""):
        lines.append(f"| `{key}` | {kind} | {default} | {text} |")
    return "\n".join(lines) + "\n"
