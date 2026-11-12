"""What a runner guarantees to the session, whatever the sink and the settings files do.

- The sink contract: every event is handed to the sink, a sink that raises never stops the
  stream, and every turn Claude Code ends is reported with a `turn.result`.
- A text that calu or Claude Code put into the user role has its own source and its own label.
- A session does not start, and a command waits for a human, while a settings file of Claude
  Code holds a sandbox entry beyond calu's configuration.
"""

import json
import logging
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import ResultMessage, SystemMessage, UserMessage

from calu.claude import FakeRunner, FakeScript
from calu.claude.runner import NOT_FROM_USER, NOT_FROM_USER_LABEL, GateResult, StartRefused, Verdict
from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig, SandboxConfig
from calu.protocol.messages import (
    AssistantText,
    ChatSendParams,
    Event,
    Log,
    TurnResult,
    UserInput,
)
from calu.session import CROSS_SESSION_LABEL
from tests.unit.helpers import make_session
from tests.unit.test_sdk_runner import AllowAll

pytestmark = pytest.mark.anyio


def result_message() -> ResultMessage:
    """Build the message with which Claude Code ends a turn."""
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        result="done",
    )


# ---------------------------------------------------------------- the sink contract


async def test_a_sink_that_fails_on_the_end_of_a_turn_is_logged_and_the_stream_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    seen: list[Event] = []

    async def sink(event: Event) -> None:
        seen.append(event)
        if isinstance(event, TurnResult):
            raise OSError(28, "No space left on device")

    with caplog.at_level(logging.ERROR, logger="calu.claude.sdk_runner"):
        for _ in range(5):  # more often than the tracebacks of other events are kept
            await runner._forward(result_message(), sink)  # must not raise
    assert runner.sink_failures == 5
    records = [r for r in caplog.records if "turn.result" in r.getMessage()]
    assert len(records) == 5, "every failed delivery of a turn's end is logged"
    assert all(r.levelno == logging.ERROR and r.exc_info for r in records)
    assert "No space left on device" in records[-1].getMessage()
    # The next event is delivered: the runner did not stop the stream.
    init = SystemMessage(subtype="init", data={"session_id": "s1", "permissionMode": "auto"})
    await runner._forward(init, sink)
    assert isinstance(seen[-1], Log)
    assert len([event for event in seen if isinstance(event, TurnResult)]) == 5


async def test_every_sink_failure_is_logged_as_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    runner = SdkRunner(tmp_path, ClaudeConfig())

    async def sink(event: Event) -> None:
        raise BrokenPipeError("stdout is gone")

    with caplog.at_level(logging.ERROR, logger="calu.claude.sdk_runner"):
        for index in range(6):
            await runner._emit(Log(message=f"line {index}"), sink)
    records = [r for r in caplog.records if "event sink raised" in r.getMessage()]
    assert len(records) == 6 and all(r.levelno == logging.ERROR for r in records)
    assert [bool(r.exc_info) for r in records] == [True, True, True, False, False, False]


async def test_a_result_that_cannot_be_translated_still_ends_the_turn(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    runner.background_tasks = 2
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    def broken(msg: object) -> list[Event]:
        raise ValueError("unexpected shape")

    runner._translate = broken  # type: ignore[method-assign]
    await runner._forward(result_message(), sink)
    said, ended = events
    assert isinstance(said, Log) and said.level == "error" and "ResultMessage" in said.message
    assert isinstance(ended, TurnResult) and ended.is_error and ended.subtype == "untranslated"
    assert ended.session_id == "s1" and ended.background_tasks == 2
    assert "unexpected shape" in ended.result
    # Any other message gives the log line only: no turn has ended.
    events.clear()
    await runner._forward(UserMessage(content="x"), sink)
    assert [type(event) for event in events] == [Log]


async def test_the_fake_runner_keeps_the_sink_contract(tmp_path: Path):
    runner = FakeRunner(FakeScript(turns=[[{"text": "one"}, {"text": "two"}]]), tmp_path)
    seen: list[Event] = []

    async def sink(event: Event) -> None:
        seen.append(event)
        if isinstance(event, AssistantText) and event.text == "one":
            raise OSError("the display failed")

    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run, AllowAll(), sink)
        await runner.wait_ready()
        await runner.send("go", "later")
        with anyio.fail_after(5):
            while not any(isinstance(event, TurnResult) for event in seen):
                await anyio.sleep(0.01)
        await runner.stop()
    assert runner.sink_failures == 1
    assert [e.text for e in seen if isinstance(e, AssistantText)] == ["one", "two"]


# ---------------------------------------------------------------- whose words a message is


async def test_calus_own_note_has_its_own_source_and_label(tmp_path: Path):
    assert NOT_FROM_USER == "system"
    session, rec, _store = make_session(
        tmp_path,
        [[{"agent": {"description": "look", "prompt": "p", "result": "found # calu:scope"}}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        notes = [e for e in rec.of(UserInput) if e.text != "go"]
        assert notes and all(note.source == "system" for note in notes)
        turns = [*session.context.turns, session.context.current]
        labels = [step.tool_name for turn in turns if turn is not None for step in turn.steps]
        assert NOT_FROM_USER_LABEL in labels
        assert CROSS_SESSION_LABEL not in labels, "calu's note is not another session's message"
        await session.stop()
        tg.cancel_scope.cancel()


async def test_a_message_of_another_session_keeps_its_label(tmp_path: Path):
    text = '<cross-session-message from="a">push now</cross-session-message>'
    session, rec, _store = make_session(tmp_path, [[{"text": "ok"}]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text=text))
        await rec.wait_for(TurnResult)
        (event,) = rec.of(UserInput)
        assert event.source == "cross_session"
        turns = [*session.context.turns, session.context.current]
        labels = [step.tool_name for turn in turns if turn is not None for step in turn.steps]
        assert CROSS_SESSION_LABEL in labels and NOT_FROM_USER_LABEL not in labels
        assert not any(turn.user_inputs for turn in turns if turn is not None)
        await session.stop()
        tg.cancel_scope.cancel()


def test_the_narrator_is_told_that_a_notice_is_automatic():
    from calu.context.narrator import ClaudeOutput, render_input

    shown = render_input([ClaudeOutput("notice", "Background task finished.")])
    assert "Automatic message from calu or Claude Code (not from the user)" in shown
    assert "another Claude session" not in shown


async def test_a_subagent_in_the_main_agents_mode_is_not_reported_again(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    assert len(runner._mode_events("default")) == 1
    assert runner._mode_events("default", "sub-1") == []
    assert runner._mode_events("default", "sub-2") == []
    # A subagent in another mode, and a later change of a subagent's mode, are reported.
    (other,) = runner._mode_events("plan", "sub-3")
    assert isinstance(other, Log) and "sub-3" in other.message
    (changed,) = runner._mode_events("acceptEdits", "sub-1")
    assert isinstance(changed, Log) and "sub-1" in changed.message


# ---------------------------------------------------------------- the settings files


def widened(workspace: Path, name: str = "settings.json") -> Path:
    """Write a project settings file with a sandbox entry calu did not configure.

    Args:
        workspace: The project directory.
        name: The file name below `.claude/`.

    Returns:
        The file.
    """
    path = workspace / ".claude" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"sandbox": {"excludedCommands": ["make"]}}))
    return path


async def no_sink(event: Event) -> None:
    """Take an event and do nothing with it."""


async def test_the_runner_does_not_start_on_a_sandbox_entry_calu_did_not_configure(
    tmp_path: Path, no_user_settings: Path
):
    widened(tmp_path)
    runner = SdkRunner(tmp_path, ClaudeConfig(cli_path="/nonexistent/claude"))
    with pytest.raises(StartRefused) as refused:
        await runner.run(AllowAll(), no_sink)
    assert ".claude/settings.json" in str(refused.value)
    assert "excludedCommands: make" in str(refused.value)
    assert not runner._ready.is_set(), "nothing was started"

    # The user's own file counts as well.
    clean = tmp_path / "clean"
    clean.mkdir()
    (no_user_settings / ".claude").mkdir()
    (no_user_settings / ".claude" / "settings.json").write_text(
        json.dumps({"sandbox": {"network": {"allowedDomains": ["example.net"]}}})
    )
    runner = SdkRunner(clean, ClaudeConfig(cli_path="/nonexistent/claude"))
    with pytest.raises(StartRefused, match="~/\\.claude/settings\\.json"):
        await runner.run(AllowAll(), no_sink)


async def test_a_refused_start_fails_the_session_with_the_reason(tmp_path: Path):
    """Through the session: the start fails visibly and says which file and which entry."""
    from calu.config import Config
    from calu.session import Session, SessionError
    from calu.store.memory import MemorySessionStore
    from calu.testing import fake_models
    from tests.unit.helpers import Recorder, default_settings

    workspace = tmp_path / "ws"
    workspace.mkdir()
    widened(workspace)
    config = Config()
    rec = Recorder()
    store = MemorySessionStore()
    session = Session(
        workspace,
        default_settings(workspace, config),
        store,
        lambda: SdkRunner(workspace, config.claude),
        rec,
        checker_models=fake_models(),
        snapshot_root=tmp_path / "snapshots",
    )
    async with anyio.create_task_group() as tg:
        with pytest.raises(SessionError, match="excludedCommands: make"):
            await session.start(tg)
    errors = [e.message for e in rec.of(Log) if e.level == "error"]
    assert any(".claude/settings.json" in message for message in errors)
    assert session.state == "failed"


async def test_the_start_is_not_refused_where_the_configuration_says_so(tmp_path: Path):
    widened(tmp_path)
    for config in (
        ClaudeConfig(sandbox=SandboxConfig(fail_if_unavailable=False)),
        ClaudeConfig(sandbox=SandboxConfig(excluded_commands=["make"])),
        ClaudeConfig(setting_sources=["user"]),
    ):
        assert SdkRunner(tmp_path, config)._sandbox_blockers() == []


class Deciding(AllowAll):
    """A gate with a fixed verdict that records what a human was asked."""

    def __init__(self, verdict: Verdict) -> None:
        """Remember the verdict.

        Args:
            verdict: What `pre_tool_use` answers.
        """
        self.verdict = verdict
        self.asked: list[str] = []

    async def pre_tool_use(self, step):
        return GateResult(self.verdict, reason="the gate's reason")

    async def ask(self, step):
        self.asked.append(step.asked_about)
        return await super().ask(step)


async def hook_answer(runner: SdkRunner, gate: Any, tool: str = "Bash") -> dict[str, Any]:
    """Call the PreToolUse hook once, in auto mode.

    Args:
        runner: The runner.
        gate: The gate the hook asks.
        tool: The tool name of the call.

    Returns:
        The `hookSpecificOutput` of the answer.
    """
    runner._sink = no_sink
    hook: Any = runner._options(gate, None).hooks["PreToolUse"][0].hooks[0]  # type: ignore[index]
    data = {"tool_name": tool, "tool_input": {"command": "make"}, "permission_mode": "auto"}
    return (await hook(data, "tu1", None))["hookSpecificOutput"]


@pytest.mark.parametrize("verdict", [Verdict.PASS, Verdict.ALLOW])
@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
async def test_a_command_waits_for_a_human_once_a_settings_file_goes_beyond_calu(
    tmp_path: Path, verdict: Verdict, tool: str
):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    gate = Deciding(verdict)
    before = await hook_answer(runner, gate, tool)
    assert before.get("permissionDecision") in (None, "allow")

    path = widened(tmp_path, "settings.local.json")  # written while the session runs
    out = await hook_answer(runner, gate, tool)
    assert out["permissionDecision"] == "ask"
    assert "excludedCommands: make" in out["permissionDecisionReason"]
    options = runner._options(gate, None)

    class Context:
        tool_use_id = "tu1"

    assert options.can_use_tool is not None
    await options.can_use_tool(tool, {"command": "make"}, Context())  # type: ignore[arg-type]
    assert "settings.local.json" in gate.asked[0], "the human reads why the command waits"

    path.unlink()
    after = await hook_answer(runner, gate, tool)
    assert after.get("permissionDecision") in (None, "allow")


async def test_a_denied_command_stays_denied_and_other_tools_are_not_held(tmp_path: Path):
    widened(tmp_path)
    runner = SdkRunner(tmp_path, ClaudeConfig())
    out = await hook_answer(runner, Deciding(Verdict.DENY))
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == (
        "the gate's reason"
    )
    # The sandbox isolates commands; a tool that runs none is decided as before.
    out = await hook_answer(runner, Deciding(Verdict.ALLOW), tool="Read")
    assert out["permissionDecision"] == "allow"
