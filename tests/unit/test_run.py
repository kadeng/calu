"""`calu run`: event rendering for the terminal, the idle signal and reading stdin."""

import io
import os

import anyio
import pytest

from calu import run
from calu.checkers import Decision, Route
from calu.config import AutoEscalate
from calu.protocol.messages import (
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    CheckResult,
    EscalationRequest,
    Event,
    HonestyFileStatus,
    HonestyUnitStatus,
    Log,
    SessionState,
    State,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.run import Activity, Console, IdleSignal, format_event, read_lines, read_lines_until

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


def test_the_honesty_checkers_result_is_shown_per_file_and_never_as_a_decision():
    """The honesty checker's status per file follows the decision line; no ALLOW or PASS."""
    event = CheckResult(
        tool_use_id="t1",
        tool_name="Edit",
        route=Route.EDIT,
        decision=Decision.ESCALATE,
        step_summary="Edits two files.",
        honesty=[
            HonestyFileStatus(path="a.py", verdict="HONEST"),
            HonestyFileStatus(
                path="b.py",
                verdict="DISHONEST",
                reason="save() also uploads",
                units=[
                    HonestyUnitStatus(symbol="save", honest=False, reason="uploads the file"),
                    HonestyUnitStatus(symbol="load", honest=True),
                ],
            ),
        ],
        honesty_escalation="looks deliberate",
    )
    assert format_event(event) == "\n".join(
        [
            "  [edit checker: ESCALATE] Edits two files.",
            "    honesty: a.py honest",
            "    honesty: b.py DISHONEST: save() also uploads",
            "      save: uploads the file",
            "    honesty checker asks a person to look: looks deliberate",
        ]
    )


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


async def test_the_console_prints_the_status_and_never_the_conversation(capsys):
    """D225: `calu run` and `calu serve` print the session's status; the clients show the
    conversation."""
    console = Console()
    for event in (
        UserInput(text="secret plan"),
        AssistantThinking(text="hmm"),
        AssistantText(text="Hello"),
        ToolUse(tool_use_id="t", name="Bash", input={"command": "ls"}),
        ToolResult(tool_use_id="t", content="a.py"),
    ):
        await console(event)
    assert capsys.readouterr().out == ""
    # Nor the course of the work: states other than the end, the end of a turn.
    await console(SessionState(state="checking"))
    await console(SessionState(state="idle"))
    await console(TurnResult(result="done"))
    assert capsys.readouterr().out == ""
    await console(Log(level="warning", message="two\nlines"))
    await console(SessionState(state="stopped"))
    assert capsys.readouterr().out.splitlines() == ["[warning] two lines", "[stopped]"]


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
    # The end is not silent: it is reported as an error, and the run does not exit with 0.
    assert "1 message(s) still count as queued" in capsys.readouterr().err
    assert watched.idle.unfinished


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


async def test_a_line_typed_while_no_reader_waits_reaches_the_next_reader(monkeypatch):
    """The line client stops reading when it switches sessions (`/switch`, `/new`, a start
    under a new row) and the next client reads on. The line typed next must reach that
    client: it was lost to the abandoned read of the first one."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as stdin, os.fdopen(write_fd, "w") as typing:
        monkeypatch.setattr("sys.stdin", stdin)
        first: list[str] = []
        with anyio.move_on_after(0.3):
            await read_lines(lambda text: _collect(first, text))  # nothing typed: cancelled
        typing.write("are you there\n/quit\n")
        typing.flush()
        second: list[str] = []
        with anyio.fail_after(5):
            assert await read_lines(lambda text: _collect(second, text)) == "quit"
    assert first == [] and second == ["are you there"]


async def test_a_clients_shutdown_ends_the_reading_of_the_terminal(monkeypatch):
    """A client's `/shutdown` must end an interactive `calu run` although nothing is typed in
    its own terminal (the host rehearsal found the process waiting there for minutes)."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as stdin, os.fdopen(write_fd, "w") as typing:
        monkeypatch.setattr("sys.stdin", stdin)
        shutdown = anyio.Event()
        sent: list[str] = []
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_set_later, shutdown)
                assert await read_lines_until(lambda t: _collect(sent, t), shutdown) == "quit"
        typing.write("/quit\n")  # ends the pending read of this pipe for the next test
        typing.flush()
    assert sent == []


PROCESS_EXIT_SCRIPT = """
import anyio
from calu.run import read_lines_until

async def main():
    shutdown = anyio.Event()

    async def later():
        await anyio.sleep(0.2)
        shutdown.set()

    async def send(text):
        pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(later)
        print(await read_lines_until(send, shutdown), flush=True)

anyio.run(main)
"""


def test_the_process_exits_after_a_shutdown_while_stdin_stays_open():
    """Test plan M20: with the loop ended, `calu run` still lived until a line was typed,
    because the abandoned wait for the stdin line held a (non-daemon) AnyIO worker thread."""
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", PROCESS_EXIT_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        code = child.wait(timeout=10)  # stdin is still open: nothing is typed, no end of input
        assert code == 0
        assert child.stdout is not None and child.stdout.read().strip() == "quit"
    finally:
        if child.poll() is None:
            child.kill()
        assert child.stdin is not None
        child.stdin.close()
        child.wait()


async def test_reading_without_a_shutdown_event_is_plain_read_lines(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("only\n"))
    sent: list[str] = []
    assert await read_lines_until(lambda t: _collect(sent, t), None) == "eof"
    assert sent == ["only"]


async def _set_later(event: anyio.Event) -> None:
    await anyio.sleep(0.2)
    event.set()


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


async def test_continue_resumes_the_newest_conversation_of_the_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """D280: `calu run --continue` after a stopped `calu run` or `calu serve`."""
    import dataclasses
    import socket
    from datetime import UTC, datetime

    from calu import herd, run
    from calu.herd import STALE_AFTER_S, SessionRow
    from calu.run import RunOptions

    when = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
    newest = SessionRow(
        id="dddddddd-0000-0000-0000-000000000000",
        hostname=socket.gethostname(),
        os_user="me",
        directory=str(tmp_path),
        state="stopped",
        state_since=when,
        started_at=when,
        ended_at=when,
        claude_session_id="cccccccc-1111-1111-1111-111111111111",
        open_escalations=0,
    )
    found: list[SessionRow | None] = [newest]
    asked: list[tuple] = []

    async def lookup(db, hostname, os_user, directories, hint=None):
        asked.append((hostname, directories, hint))
        return found[0]

    monkeypatch.setattr(herd, "dormant_session", lookup)
    said: list[Event] = []

    async def console(event: Event) -> None:
        said.append(event)

    options = RunOptions(workspace=tmp_path, continue_last=True)
    continued = await run._continued(None, options, console)  # type: ignore[arg-type]
    assert continued is not None and continued.resume_id == newest.claude_session_id
    assert not continued.continue_last
    assert asked == [(socket.gethostname(), [str(tmp_path)], None)], "this directory only"

    # Its process gone without ending the row (a killed server): still continued.
    found[0] = dataclasses.replace(newest, ended_at=None, silent_for_s=STALE_AFTER_S + 1)
    continued = await run._continued(None, options, console)  # type: ignore[arg-type]
    assert continued is not None and continued.resume_id == newest.claude_session_id

    # Still running: not a second time.
    found[0] = dataclasses.replace(newest, ended_at=None, silent_for_s=1.0)
    assert await run._continued(None, options, console) is None  # type: ignore[arg-type]
    assert isinstance(said[-1], Log) and said[-1].level == "error"

    # Nothing to continue: a new conversation.
    found[0] = None
    continued = await run._continued(None, options, console)  # type: ignore[arg-type]
    assert continued is not None and continued.resume_id is None
    assert "new conversation" in said[-1].message  # type: ignore[union-attr]

    # Together with a named conversation: refused.
    both = dataclasses.replace(options, resume_id=newest.claude_session_id)
    assert await run._continued(None, both, console) is None  # type: ignore[arg-type]


# ------------------------------------------------------------------ the hold of failed reloads


def test_failed_reloads_hold_once_and_a_reload_that_succeeds_goes_back() -> None:
    """The hold replaces the session's categories once; the next good reload restores them."""
    hold = run.ReloadHold()
    before = AutoEscalate(edits=True)
    assert hold.place(before) is True
    # The session now holds everything; later failures of the streak place nothing new.
    assert hold.place(run.HOLD_ALL) is False
    assert hold.release(run.HOLD_ALL) == before
    # The streak is over: a new one holds again.
    assert hold.place(before) is True


def test_a_level_a_person_chose_during_the_hold_stands() -> None:
    """The owner: "Confirm everything" must not come back every time (2026-11-27).

    A session whose settings cannot be reloaded holds every checked step. That is fail
    closed. A person who then chooses another level has read why the session holds, so the
    next failed retry does not put the hold back, and the next good reload keeps the choice.
    The price: the session runs on stale settings with the level the person chose.
    """
    hold = run.ReloadHold()
    assert hold.place(AutoEscalate()) is True
    chosen = AutoEscalate(edits=True)
    assert hold.place(chosen) is False, "a later failure of the same streak"
    assert hold.release(chosen) is None, "the choice stays after a reload that succeeds"


def test_no_hold_is_placed_on_a_session_that_holds_everything_already() -> None:
    """Nothing to replace, so nothing to restore."""
    hold = run.ReloadHold()
    assert hold.place(run.HOLD_ALL) is False
    assert hold.release(run.HOLD_ALL) is None
