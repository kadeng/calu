"""Optional token streaming: `assistant.delta` events (off by default)."""

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from claude_agent_sdk.types import StreamEvent

from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig
from calu.protocol.messages import (
    AssistantDelta,
    AssistantText,
)

pytestmark = pytest.mark.anyio


def stream(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=None)


def test_streaming_is_on_by_default(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    assert runner._options(None, None).include_partial_messages is True  # type: ignore[arg-type]
    off = SdkRunner(tmp_path, ClaudeConfig(stream_partial=False))
    assert off._options(None, None).include_partial_messages is False  # type: ignore[arg-type]


def test_stream_events_become_deltas_with_the_message_id(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig(stream_partial=True))
    assert runner._translate(stream({"type": "message_start", "message": {"id": "msg_1"}})) == []
    (delta,) = runner._translate(
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}})
    )
    assert isinstance(delta, AssistantDelta)
    assert (delta.message_id, delta.text) == ("msg_1", "Hel")
    assert (
        runner._translate(
            stream({"type": "content_block_delta", "delta": {"type": "input_json_delta"}})
        )
        == []
    )
    final = AssistantMessage(content=[TextBlock(text="Hello")], model="m", message_id="msg_1")
    (text,) = runner._translate(final)
    assert isinstance(text, AssistantText) and text.message_id == "msg_1"


def test_thinking_blocks_and_deltas_become_events(tmp_path: Path):
    from claude_agent_sdk import ThinkingBlock

    from calu.protocol.messages import AssistantThinking

    runner = SdkRunner(tmp_path, ClaudeConfig())
    assert runner._options(None, None).extra_args["thinking-display"] == "summarized"  # type: ignore[arg-type]
    runner._translate(stream({"type": "message_start", "message": {"id": "m2"}}))
    (delta,) = runner._translate(
        stream(
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Let"}}
        )
    )
    assert isinstance(delta, AssistantDelta) and delta.thinking
    block = AssistantMessage(
        content=[ThinkingBlock(thinking="Let me think.", signature="s")], model="m", message_id="m2"
    )
    (thinking,) = runner._translate(block)
    assert isinstance(thinking, AssistantThinking) and thinking.text == "Let me think."
    empty = AssistantMessage(content=[ThinkingBlock(thinking="", signature="s")], model="m")
    assert runner._translate(empty) == []


def test_stream_deltas_keep_their_agent(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())

    def ev(event, parent=None):
        return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=parent)

    runner._translate(ev({"type": "message_start", "message": {"id": "main1"}}))
    runner._translate(ev({"type": "message_start", "message": {"id": "sub1"}}, "agent-1"))
    text = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}
    (main,) = runner._translate(ev(text))
    (sub,) = runner._translate(ev(text, "agent-1"))
    assert isinstance(main, AssistantDelta) and isinstance(sub, AssistantDelta)
    assert (main.agent_id, main.message_id) == (None, "main1")
    assert (sub.agent_id, sub.message_id) == ("agent-1", "sub1")
