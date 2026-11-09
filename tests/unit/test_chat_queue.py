"""Chat messages that Claude has not taken in yet (`chat.queue`)."""

from pathlib import Path

import anyio
import pytest

from calu.protocol.messages import ChatQueue, ChatSendParams, EscalationRequest, TurnResult
from tests.unit.helpers import make_session

pytestmark = pytest.mark.anyio

APPROVAL_STEP = {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}


def queue_texts(event: ChatQueue) -> list[str]:
    """Return the texts of the queued messages of a `chat.queue` event."""
    return [m.text for m in event.messages]


async def test_a_queued_message_waits_until_claude_takes_it_in(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, [[APPROVAL_STEP], [{"text": "Second answer."}]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        esc = (await rec.wait_for(EscalationRequest))[0]
        await session.send_chat(ChatSendParams(text="second", delivery="queued"))
        queues = rec.of(ChatQueue)
        assert queue_texts(queues[-1]) == ["second"]  # "first" was taken in already
        assert queues[-1].messages[0].delivery == "queued"
        await session.decide_escalation(esc.escalation_id, "allow", None)
        await rec.wait_for(TurnResult, 2)
        assert queue_texts(rec.of(ChatQueue)[-1]) == []
        await session.stop()


async def test_messages_left_in_the_queue_are_dropped_when_the_session_ends(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, [[APPROVAL_STEP]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(EscalationRequest)
        await session.send_chat(ChatSendParams(text="never read"))
        assert queue_texts(rec.of(ChatQueue)[-1]) == ["never read"]
        await session.stop()
    assert queue_texts(rec.of(ChatQueue)[-1]) == []
