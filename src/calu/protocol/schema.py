"""JSON Schema export of the calu client protocol, used to generate client types (e.g. in TS).

The document has three maps: `requests` (method -> params schema), `events` (method -> params
schema) and `results` (method -> result schema, only for requests whose result is a model).
All schemas are `$ref`s into `$defs`.
"""

from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from calu.protocol.messages import (
    EVENTS,
    PROTOCOL_VERSION,
    REQUESTS,
    CheckerContextResult,
    CommentAddResult,
    EventsSubscribeResult,
    InitializeResult,
    SessionListResult,
    SessionSnapshot,
)


def protocol_schema() -> dict[str, Any]:
    """Build the JSON Schema document of the protocol (see module docstring).

    Returns:
        A JSON-serializable dict with "$schema", "title", "version", "requests", "events",
        "results" and "$defs".
    """
    models: list[type[BaseModel]] = [
        *REQUESTS.values(),
        *EVENTS,
        InitializeResult,
        CommentAddResult,
        SessionListResult,
        SessionSnapshot,
        EventsSubscribeResult,
        CheckerContextResult,
    ]
    refs, defs = models_json_schema(
        [(m, "validation") for m in models], ref_template="#/$defs/{model}"
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "calu protocol",
        "version": PROTOCOL_VERSION,
        "requests": {name: refs[model, "validation"] for name, model in REQUESTS.items()},
        "events": {e.method: refs[e, "validation"] for e in EVENTS},
        "results": {
            "initialize": refs[InitializeResult, "validation"],
            "state.get": refs[SessionSnapshot, "validation"],
            "events.subscribe": refs[EventsSubscribeResult, "validation"],
            "session.list": refs[SessionListResult, "validation"],
            "context.get": refs[CheckerContextResult, "validation"],
            "comment.add": refs[CommentAddResult, "validation"],
        },
        "$defs": defs.get("$defs", {}),
    }
