"""JSON Schema export of the calu client protocol, used to generate client types (e.g. in TS).

The document has four maps: `requests` (method -> params schema), `events` (method -> params
schema) and `results` (method -> result schema, only for requests whose result is a model), and
`relay` with the `requests` and `results` of a `calu serve` relay's own address (D242).
All schemas are `$ref`s into `$defs`.
"""

from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from calu.protocol.messages import (
    EVENTS,
    PROTOCOL_VERSION,
    RELAY_REQUESTS,
    REQUESTS,
    ChatRetractResult,
    CheckerContextResult,
    CheckerListResult,
    CommentAddResult,
    ContextUsage,
    EscalationContextResult,
    EventsSubscribeResult,
    FilesUserEditResult,
    HonestyReviewResult,
    HonestyTrustResult,
    HumanDiffAddResult,
    InitializeResult,
    RelayInitializeResult,
    RelaySessionsResult,
    RelayStartResult,
    SessionHistoryResult,
    SessionListResult,
    SessionSnapshot,
    SideAskResult,
    UsagePanelResult,
    UsageResult,
)

__all__ = ["ContextUsage", "SideAskResult", "UsageResult", "protocol_schema"]


def protocol_schema() -> dict[str, Any]:
    """Build the JSON Schema document of the protocol (see module docstring).

    Returns:
        A JSON-serializable dict with "$schema", "title", "version", "requests", "events",
        "results", "relay" and "$defs".
    """
    models: list[type[BaseModel]] = [
        *REQUESTS.values(),
        *RELAY_REQUESTS.values(),
        *EVENTS,
        InitializeResult,
        CommentAddResult,
        SessionListResult,
        SessionHistoryResult,
        SessionSnapshot,
        EscalationContextResult,
        CheckerListResult,
        EventsSubscribeResult,
        CheckerContextResult,
        FilesUserEditResult,
        HumanDiffAddResult,
        HonestyTrustResult,
        HonestyReviewResult,
        SideAskResult,
        ChatRetractResult,
        UsageResult,
        UsagePanelResult,
        ContextUsage,
        RelayInitializeResult,
        RelaySessionsResult,
        RelayStartResult,
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
            "session.history": refs[SessionHistoryResult, "validation"],
            "context.get": refs[CheckerContextResult, "validation"],
            "escalation.context": refs[EscalationContextResult, "validation"],
            "checkers.get": refs[CheckerListResult, "validation"],
            "comment.add": refs[CommentAddResult, "validation"],
            "honesty.trust": refs[HonestyTrustResult, "validation"],
            "honesty.review": refs[HonestyReviewResult, "validation"],
            "files.user_edit": refs[FilesUserEditResult, "validation"],
            "human_diffs.add": refs[HumanDiffAddResult, "validation"],
            "side.ask": refs[SideAskResult, "validation"],
            "chat.retract": refs[ChatRetractResult, "validation"],
            "usage.get": refs[UsageResult, "validation"],
            "usage.panel": refs[UsagePanelResult, "validation"],
        },
        "relay": {
            "requests": {name: refs[model, "validation"] for name, model in RELAY_REQUESTS.items()},
            "results": {
                "initialize": refs[RelayInitializeResult, "validation"],
                "relay.sessions": refs[RelaySessionsResult, "validation"],
                "relay.start": refs[RelayStartResult, "validation"],
            },
        },
        "$defs": defs.get("$defs", {}),
    }
