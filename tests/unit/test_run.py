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
    State,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.run import Activity, Console, IdleSignal, format_event, read_lines

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


class Watched:
    """A stand-in for the session an `IdleSignal` reads: its state, queue and open turn."""

    def __init__(self) -> None:
        self.state: State = "starting"
        self.queued = 0
        self.turn_open = False
        self.idle = IdleSignal(lambda: Activity(self.state, self.queued, self.turn_open))

    async def to(self, state: State) -> None:
        """Change the state and report it, as the session does."""
        self.state = state
        await self.idle(SessionState(state=state))


async def still_waiting(idle: IdleSignal, why: str) -> None:
    """Assert that `idle.wait()` does not return within a short time."""
    with anyio.move_on_after(0.05) as scope:
        await idle.wait()
    assert scope.cancelled_caught, why


async def test_the_idle_signal_waits_until_the_session_worked():
    watched = Watched()
    await watched.to("idle")
    await still_waiting(watched.idle, "the signal fired before the session worked")
    await watched.to("running")
    await watched.to("idle")
    with anyio.fail_after(1):
        await watched.idle.wait()


async def test_an_unattended_run_waits_for_claude_codes_background_tasks():
    """D158: a turn may end while a background command, Monitor or Workflow still runs; Claude
    Code starts another turn when they finish (measured). Ending the run at the first idle
    stopped the session under them, with their steps still to be checked."""
    watched = Watched()
    await watched.to("running")
    await watched.idle(TurnResult(background_tasks=2))
    await watched.to("idle")
    await still_waiting(watched.idle, "the run ended while background tasks were running")
    # Claude Code's own follow-up turn, after the tasks finished.
    await watched.to("running")
    await watched.idle(TurnResult(background_tasks=0))
    await watched.to("idle")
    with anyio.fail_after(1):
        await watched.idle.wait()
    assert "2 background task(s) still running" in str(format_event(TurnResult(background_tasks=2)))


async def test_a_failed_session_also_ends_the_wait():
    watched = Watched()
    await watched.to("failed")
    with anyio.fail_after(1):
        await watched.idle.wait()


async def test_the_end_of_a_run_is_read_from_the_session_not_from_the_order_of_events():
    """An unattended run always ends. A message sent while a turn runs is queued; whatever
    was reported before or after it, the run waits for exactly the turns that are left."""
    watched = Watched()
    await watched.to("running")
    watched.queued = 1  # sent while the first turn runs
    await watched.to("idle")  # the first turn ended, the queued message has not started yet
    await still_waiting(watched.idle, "a queued message must get its turn")
    watched.queued, watched.turn_open = 0, True  # Claude took it; the state follows a moment later
    await still_waiting(watched.idle, "a turn that has started must be waited for")
    await watched.to("running")
    watched.turn_open = False
    await watched.to("idle")
    with anyio.fail_after(1):
        await watched.idle.wait()
    with anyio.fail_after(1):
        await watched.idle.wait()  # and it stays finished: no event is needed to see it again


async def test_the_wait_ends_without_any_further_event():
    """The condition is evaluated on a timer as well, so no single event has to be seen."""
    watched = Watched()
    await watched.to("running")
    watched.state = "idle"  # the session is idle, and nothing reports it
    with anyio.fail_after(3):
        await watched.idle.wait()


async def test_a_queued_message_that_never_starts_does_not_keep_the_run_open(capsys):
    """An unattended run always ends: a message that still counts as queued long after the
    session became idle is not waited for any longer."""
    watched = Watched()
    watched.idle.pickup_timeout_s = 0.2
    await watched.to("running")
    watched.queued = 1
    await watched.to("idle")
    await still_waiting(watched.idle, "the queued message is given time to start")
    with anyio.fail_after(3):
        await watched.idle.wait()
    assert "1 message(s) still count as queued" in capsys.readouterr().out


async def test_read_lines_sends_messages_until_quit(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("first\n\n  second  \n/quit\nnever\n"))
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    assert await read_lines(send) == "quit"
    assert sent == ["first", "second"]


async def test_read_lines_ends_at_end_of_input(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("only\n"))
    sent: list[Event] = []
    # cs5: the caller has to tell a closed stdin from a quit command, because only the
    # second one means "stop now".
    assert await read_lines(lambda text: _collect(sent, text)) == "eof"
    assert sent == ["only"]


async def _collect(sink: list, text: str) -> None:
    sink.append(text)


async def test_a_background_timeout_ends_the_wait_for_tasks_that_never_finish():
    """D170: a background dev server keeps an unattended run open forever; with
    `--background-timeout` the run ends once the session has been idle that long."""
    watched = Watched()
    watched.idle.background_timeout_s = 0.2
    await watched.to("running")
    await watched.idle(TurnResult(background_tasks=1))
    await watched.to("idle")
    with anyio.fail_after(3):
        await watched.idle.wait()
