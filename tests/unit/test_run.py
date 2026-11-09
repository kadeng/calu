"""`calu run`: event rendering for the terminal, the idle signal and reading stdin."""

import io

import anyio
import pytest

from calu.checkers import Decision, Route
from calu.protocol.messages import (
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    CheckResult,
    EscalationRequest,
    Event,
    Log,
    SessionState,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.run import Console, IdleSignal, format_event, read_lines

pytestmark = pytest.mark.anyio


def test_events_become_one_line_each():
    assert format_event(SessionState(state="checking")) == "[checking]"
    assert format_event(UserInput(text="do\nit")) == "> do it"
    assert format_event(AssistantText(text="Hello")) == "Hello"
    assert format_event(AssistantText(agent_id="a1", text="Hi")) == "a1: Hi"
    assert "· Bash" in str(format_event(ToolUse(tool_use_id="t", name="Bash", input={"a": 1})))
    assert format_event(ToolResult(tool_use_id="t", content="ok")) == "  = ok"
    assert format_event(ToolResult(tool_use_id="t", content="no", is_error=True)) == "  ! no"
    assert format_event(TurnResult(result="done")) == "[turn ended: success]"
    assert format_event(Log(level="warning", message="hm")) == "[warning] hm"
    assert format_event(AssistantDelta(message_id="m", text="x")) is None


def test_a_check_result_shows_the_decision_and_the_comment():
    line = format_event(
        CheckResult(
            tool_use_id="t1",
            tool_name="Bash",
            route=Route.COMMAND,
            decision=Decision.ALLOW,
            reason="harmless",
            step_summary="Lists files.",
            comment="Use -1.",
        )
    )
    assert line == "  [command checker: ALLOW] Lists files. (harmless) comment: Use -1."


def test_an_escalation_names_the_resolve_command():
    line = format_event(
        EscalationRequest(
            escalation_id="17",
            tool_use_id="t1",
            tool_name="Bash",
            tool_input={},
            reason="needs a decision",
            rendered="`ls`",
        )
    )
    assert line is not None
    assert "escalation 17: Bash" in line and "calu resolve 17 allow|deny" in line


async def test_the_console_hides_thinking_unless_asked(capsys):
    await Console()(AssistantThinking(text="hmm"))
    assert capsys.readouterr().out == ""
    await Console(show_thinking=True)(AssistantThinking(text="hmm"))
    assert "(thinking) hmm" in capsys.readouterr().out


async def test_the_idle_signal_waits_until_the_session_worked():
    idle = IdleSignal()
    for state in ("starting", "idle"):
        await idle(SessionState(state=state))
    with anyio.move_on_after(0.05):
        await idle.wait()
        raise AssertionError("the signal fired before the session worked")
    await idle(SessionState(state="running"))
    await idle(SessionState(state="idle"))
    with anyio.fail_after(1):
        await idle.wait()


async def test_a_failed_session_also_ends_the_wait():
    idle = IdleSignal()
    await idle(SessionState(state="failed"))
    with anyio.fail_after(1):
        await idle.wait()


async def test_read_lines_sends_messages_until_quit(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("first\n\n  second  \n/quit\nnever\n"))
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await read_lines(send)
    assert sent == ["first", "second"]


async def test_read_lines_ends_at_end_of_input(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("only\n"))
    sent: list[Event] = []
    await read_lines(lambda text: _collect(sent, text))
    assert sent == ["only"]


async def _collect(sink: list, text: str) -> None:
    sink.append(text)
