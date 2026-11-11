"""Translating the Agent SDK's messages into calu's events (`calu.claude.sdk_runner`).

The fake runner covers the session's behaviour; this covers the layer below it, which turns what
the SDK reports into what the checkers, the clients and the database see. The subagent
life-cycle messages are the interesting part: nothing else tracks whether a background subagent
is still running, and the web interface and `calu attach` show exactly this.
"""

from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
)

from calu.claude.sdk_runner import SdkRunner, tool_result_text
from calu.config import ClaudeConfig
from calu.protocol.messages import AgentInfo, AgentsUpdate, TurnResult


def runner(tmp_path: Path) -> SdkRunner:
    """Build a runner without starting anything.

    Args:
        tmp_path: A workspace directory.

    Returns:
        The runner.
    """
    return SdkRunner(tmp_path, ClaudeConfig())


def test_a_tool_result_is_rendered_for_a_human_and_a_checker() -> None:
    """The result text reaches a checker and a client, so every shape has to render."""
    assert tool_result_text(None) == ""
    assert tool_result_text("plain") == "plain"
    assert tool_result_text([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]) == (
        "one\ntwo"
    )
    # A block that is not text is named rather than dropped silently.
    assert tool_result_text([{"type": "image", "source": {}}]) == "[image]"
    assert tool_result_text([{"nothing": True}]) == "[block]"
    assert tool_result_text(["not a dict"]) == "[block]"
    assert tool_result_text(42) == "42"
    # Too long is cut, and says that it was.
    cut = tool_result_text("x" * 50, limit=10)
    assert cut.startswith("x" * 10)
    assert "truncated" in cut
    assert tool_result_text("x" * 10, limit=10) == "x" * 10  # exactly the limit is not cut


def test_a_started_subagent_is_tied_to_its_tool_call(tmp_path: Path) -> None:
    """The SDK's task id and the Agent tool call id are two names for one subagent."""
    sdk = runner(tmp_path)
    sdk.agents["call-1"] = AgentInfo(agent_id="call-1", description="review the diff")
    events = sdk._translate(
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="task-9",
            description="review the diff",
            uuid="u1",
            session_id="claude-1",
            tool_use_id="call-1",
        )
    )
    assert sdk._task_to_tool_use == {"task-9": "call-1"}
    assert sdk.agents["call-1"].sdk_agent_id == "task-9"
    update = events[0]
    assert isinstance(update, AgentsUpdate)
    assert [info.agent_id for info in update.agents] == ["call-1"]

    # A task calu never saw as a tool call is remembered but reported about nothing.
    assert (
        sdk._translate(
            TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="task-x",
                description="",
                uuid="u2",
                session_id="claude-1",
                tool_use_id="unknown-call",
            )
        )
        == []
    )


def test_the_status_of_a_subagent_follows_its_notifications(tmp_path: Path) -> None:
    """ "completed" and "failed" are kept; anything else means it stopped."""
    sdk = runner(tmp_path)
    sdk.agents["call-1"] = AgentInfo(agent_id="call-1")

    def notify(status: str) -> str:
        """Send one notification and return the agent's status afterwards.

        Args:
            status: The status the SDK reports.

        Returns:
            The status calu recorded.
        """
        events = sdk._translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="t",
                status=cast(Any, status),
                output_file="",
                summary="",
                uuid="u3",
                session_id="claude-1",
                tool_use_id="call-1",
            )
        )
        assert [type(event) for event in events] == [AgentsUpdate]
        return sdk.agents["call-1"].status

    assert notify("completed") == "completed"
    assert notify("failed") == "failed"
    assert notify("cancelled") == "stopped"
    assert notify("anything else") == "stopped"

    # A notification about a subagent calu does not know changes nothing.
    assert (
        sdk._translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="t",
                status="completed",
                output_file="",
                summary="",
                uuid="u4",
                session_id="claude-1",
                tool_use_id="other",
            )
        )
        == []
    )


def test_a_result_message_ends_the_turn_and_records_the_session(tmp_path: Path) -> None:
    """`TurnResult` is what a client shows as the end of a turn, and what `--batch` waits for."""
    sdk = runner(tmp_path)
    events = sdk._translate(
        ResultMessage(
            subtype="success",
            duration_ms=1200,
            duration_api_ms=900,
            is_error=False,
            num_turns=3,
            session_id="claude-5",
            total_cost_usd=0.012,
            result="done",
        )
    )
    assert sdk.session_id == "claude-5"
    assert [type(event) for event in events] == [TurnResult]
    result = events[0]
    assert isinstance(result, TurnResult)
    assert (result.session_id, result.result, result.num_turns) == ("claude-5", "done", 3)
    assert result.is_error is False
    assert result.cost_usd == pytest.approx(0.012)

    # An error turn keeps its subtype, which is what the session logs and a client shows.
    errored = sdk._translate(
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="claude-5",
            total_cost_usd=None,
            result=None,
        )
    )
    assert isinstance(errored[0], TurnResult)
    assert errored[0].is_error and errored[0].subtype == "error_during_execution"
    assert errored[0].result == ""  # None becomes "", so a client can print it


def test_the_session_id_arrives_with_the_init_message(tmp_path: Path) -> None:
    """Claude's own session id is what `--resume` and a fork need, so it is taken early."""
    sdk = runner(tmp_path)
    assert sdk._translate(SystemMessage(subtype="init", data={"session_id": "claude-7"})) == []
    assert sdk.session_id == "claude-7"
    # Another system message does not clear it.
    assert sdk._translate(SystemMessage(subtype="something", data={})) == []
    assert sdk.session_id == "claude-7"
    # An init without an id leaves what is known.
    assert sdk._translate(SystemMessage(subtype="init", data={})) == []
    assert sdk.session_id == "claude-7"


def test_what_claude_code_says_it_is_asking_about_is_collected() -> None:
    """The SDK says to show its own prompt sentence rather than rebuild one (PLAN.md D60)."""
    from calu.claude.sdk_runner import permission_prompt

    class Context:
        """A stand-in for the SDK's permission context."""

        title = "Allow network connection to example.net?"
        description = "the sandbox allows only the configured domains"
        decision_reason = None
        blocked_path = None

    said = permission_prompt(Context())
    assert said.startswith("Allow network connection to example.net?")
    assert "only the configured domains" in said

    # A path that triggered the request is named, and repeated text is not doubled.
    class WithPath:
        """A request about a file outside the allowed directories."""

        title = "Claude wants to read /etc/shadow"
        description = "Claude wants to read /etc/shadow"
        decision_reason = "the sandbox denies this path"
        blocked_path = "/etc/shadow"

    said = permission_prompt(WithPath())
    assert said.count("Claude wants to read /etc/shadow") == 1
    assert "the path it asked for: /etc/shadow" in said
    assert "the sandbox denies this path" in said

    # An older CLI says nothing, and that is not an error.
    assert permission_prompt(object()) == ""


def test_the_runner_puts_the_prompt_on_the_step(tmp_path: Path) -> None:
    """`can_use_tool` is where a capability request arrives, and the step must carry it."""
    from claude_agent_sdk import ToolPermissionContext

    from calu.checkers.model import Step

    sdk = runner(tmp_path)
    context = ToolPermissionContext(
        tool_use_id="call-1", title="Allow network connection to example.net?"
    )
    # The same composition the callback does.
    from calu.claude.sdk_runner import permission_prompt

    step = Step(tool_name="SandboxNetworkAccess", tool_input={}, cwd=str(tmp_path)).model_copy(
        update={"asked_about": permission_prompt(context)}
    )
    assert step.asked_about == "Allow network connection to example.net?"
    assert sdk.workspace == tmp_path


def test_tools_without_a_route_are_named_once(tmp_path: Path) -> None:
    """D154: CLI 2.1.319 offers tools calu never routed on purpose (RemoteTrigger, Artifact,
    ...). They go to the default route; the user hears once which ones they are."""
    from calu.checkers.policy import Route
    from calu.protocol.messages import Log

    sdk = SdkRunner(tmp_path, ClaudeConfig(), tool_routes={"Artifact": Route.ESCALATE})
    tools = [
        "Bash",
        "Read",
        "Edit",
        "WebFetch",
        "RemoteTrigger",
        "Artifact",
        "mcp__x__y",
        "CronCreate",
    ]
    first = sdk._translate(SystemMessage(subtype="init", data={"session_id": "c", "tools": tools}))
    (warning,) = first
    assert isinstance(warning, Log) and warning.level == "warning"
    assert "CronCreate, RemoteTrigger" in warning.message, "sorted, routed and MCP tools left out"
    assert "Artifact" not in warning.message and "mcp__" not in warning.message
    again = sdk._translate(SystemMessage(subtype="init", data={"session_id": "c", "tools": tools}))
    assert again == [], "once per session, not per turn"


def test_the_turn_result_carries_the_running_background_tasks(tmp_path: Path) -> None:
    """D158: `background_tasks_changed` lists Claude Code's running background tasks."""
    from claude_agent_sdk import ResultMessage

    sdk = runner(tmp_path)
    tasks = [{"task_id": "b1", "task_type": "local_bash"}, {"task_id": "w1"}]
    assert (
        sdk._translate(SystemMessage(subtype="background_tasks_changed", data={"tasks": tasks}))
        == []
    )

    def result() -> ResultMessage:
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
        )

    (turn,) = sdk._translate(result())
    assert isinstance(turn, TurnResult) and turn.background_tasks == 2
    sdk._translate(SystemMessage(subtype="background_tasks_changed", data={"tasks": []}))
    (turn,) = sdk._translate(result())
    assert isinstance(turn, TurnResult) and turn.background_tasks == 0
