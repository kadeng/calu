"""Does an Agent call without `run_in_background` run in the background, and does calu review it?

When this spike was written (2026-11-25), calu decided from the main agent's Agent call
whether a subagent runs in the background: `AgentInfo.background` was the call's
`run_in_background`, False when the field is absent (`SdkRunner._translate`). It reviews a
subagent's final result in one of two hooks: PostToolUse on the Agent tool, only when
`tool_response.status` is "completed" (a foreground subagent), and SubagentStop, only when
`AgentInfo.background` is True. The claude-code-integration reference says that subagents of
`-p` and Agent SDK sessions run in the background by default. Then a call without the field ran
in the background while calu recorded it as foreground, and neither hook reviewed the result.
This spike measures what Claude Code does and what calu's runner then does. calu was fixed the
same day: an absent field counts as background, and the call's tool result corrects the flag
(spikes/RESULTS.md); a run now shows the fixed behaviour.

    uv run python spikes/spike_agent_background_default.py --parent DIR \
        [--variants abc] [--handback allow|deny] [--permission-mode default|acceptEdits|auto]

Variants of the main agent's one Agent call: a = without `run_in_background`, b = with `false`,
c = with `true`. Each variant runs calu's own `SdkRunner` with `claude.model` = Haiku,
`claude.permission_mode` as given by `--permission-mode` (not set by default) and otherwise
calu's default `ClaudeConfig`, so Claude Code gets calu's options: the CLI the SDK
finds (its bundled one unless `claude.cli_path` is set), `setting_sources` user/project/local,
calu's `--settings` document, the permission mode calu chooses, its hooks and extra arguments.
Claude Code runs in a new empty directory under DIR. One prompt tells the main agent the exact
input of its Agent call; the subagent is asked to reply with one word. A stand-in `ToolGate`
(`RecordingGate`) allows the Agent and Task calls, denies every other tool call, and records
every call of `review_subagent_result`. `SubagentHandback` (a subagent's hand-back of its
report, which calu's session reviews as a result instead of checking it as a step,
`Session._handback`) is allowed with `--handback allow` (the default: what calu's session does
when the review passes) and denied with `--handback deny` (what it does when a person denies
the report; then the subagent ends without handing back). `RecordingRunner` records every SDK
message (except stream deltas, rate limit events and the system messages `thinking_tokens` and
`commands_changed`) and every call of calu's hooks without changing what they do. A run ends
once the subagent has ended, a later turn has ended with no background task running and 5 s
passed without a message (if the main agent made no Agent call: once a turn has ended and 5 s
passed), or after the timeout.

Output: a timeline and a summary per variant on stdout, and `records.jsonl` (the same records;
texts and tool inputs shortened) in each run's directory. Claude Code loads the user's
settings, plugins and plugin hooks and writes its session transcript under `~/.claude/projects`,
as in any calu session.

Environment: before Claude Code starts, the variables of an enclosing Claude Code session
(`HOST_SESSION_VARIABLES`, `CLAUDE_SSH_*`) are removed from this process's environment, because
calu started by a user has none of them and some change Claude Code's behaviour; their names,
never their values, are printed. Authentication variables are kept as they are.
`CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` is added for Claude Code, so that no model but Haiku
answers when the user's settings name an advisor model.

Needs Claude Code authenticated as on this machine; a few Haiku calls per variant. Results are
recorded in `spikes/RESULTS.md`.
"""

import argparse
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._cli_version import __cli_version__ as BUNDLED_CLI_VERSION
from claude_agent_sdk.types import (
    RateLimitEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
)

from calu.checkers.model import Step, SubagentResult
from calu.claude.runner import Fork, GateResult, HumanAnswer, ToolGate, Verdict
from calu.claude.sdk_runner import AGENT_TOOLS, SdkRunner
from calu.config import PROVIDER_KEYS, ClaudeConfig, PermissionMode
from calu.protocol.messages import AgentsUpdate, Event, Log, TurnResult

MODEL = "claude-haiku-5-5"
HANDBACK_TOOL = "SubagentHandback"
DENIAL = "This test allows only the Agent tool."
WORD = "DONE"
TASK_INPUT: dict[str, Any] = {
    "description": "Say DONE",
    "subagent_type": "general-purpose",
    "prompt": f"Reply with the single word {WORD}.",
}
VARIANTS: dict[str, tuple[str, dict[str, Any]]] = {
    "a": ("absent", {}),
    "b": ("false", {"run_in_background": False}),
    "c": ("true", {"run_in_background": True}),
}
"""Variant key -> (how `run_in_background` is sent, the fields added to `TASK_INPUT`)."""
QUIET_S = 5.0
TIMEOUT_S = 300.0
HOST_SESSION_VARIABLES = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_MCP_NO_PREFIX",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_CHILD_NOTES_DIR",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_DESKTOP_APP_VERSION",
        "CLAUDE_CODE_DISABLE_CRON",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
        "CLAUDE_CODE_EAGER_FLUSH",
        "CLAUDE_CODE_EMIT_STARTUP_TIMING",
        "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
        "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_HOST_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_REPORT_FINDINGS",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
        "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
        "CLAUDE_EFFORT",
        "CLAUDE_PID",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
        "DISABLE_MICROCOMPACT",
        "MCP_CONNECTION_NONBLOCKING",
        "MCP_SERVER_CONNECTION_BATCH_SIZE",
    }
)
"""Variables an enclosing Claude Code session (the desktop app, an SSH child) sets for the
commands it runs. Not removed: the authentication variables (`ANTHROPIC_*`,
`CLAUDE_CODE_ACCOUNT_UUID`, `CLAUDE_CODE_ORGANIZATION_UUID`, `CLAUDE_CODE_USER_EMAIL`,
`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`) and `DISABLE_AUTOUPDATER`."""
ADDED_ENV = {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}


def short(value: Any, limit: int = 200) -> str:
    """Return `value` as one line of at most `limit` characters.

    Args:
        value: A string, or anything else, which is rendered as JSON (`str` for what JSON
            cannot hold).
        limit: Most characters kept; a cut text ends with "... [N chars]", N the full length.

    Returns:
        The text with runs of white space joined to one space.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text)} chars]"


class Recorder:
    """Collect the records of one run, print each as one line and append it to a JSONL file.

    A record is a dict with `t` (seconds since the recorder was made, two decimals), `kind`
    and the fields of that kind. Side effects of `add`: one line on stdout, one line in the
    file.
    """

    def __init__(self, path: Path) -> None:
        """Open `path` for writing (an existing file is replaced) and start the clock.

        Args:
            path: The JSONL file.
        """
        self.started = time.monotonic()
        self.last = self.started
        """Monotonic time of the newest record."""
        self.records: list[dict[str, Any]] = []
        self._file = path.open("w", encoding="utf-8")

    def add(self, kind: str, **fields: Any) -> None:
        """Record, print and write one record.

        Args:
            kind: What happened (e.g. "assistant", "hook", "gate:review_subagent_result").
            **fields: The details; values must be JSON-serialisable or have a useful `str`.
        """
        self.last = time.monotonic()
        record = {"t": round(self.last - self.started, 2), "kind": kind, **fields}
        self.records.append(record)
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()
        print(f"  [{record['t']:6.1f}s] {kind}: {short(fields, 500)}", flush=True)

    def close(self) -> None:
        """Close the JSONL file."""
        self._file.close()


def describe(msg: object) -> dict[str, Any] | None:
    """Return what the spike records of one SDK message.

    Args:
        msg: A message of `ClaudeSDKClient.receive_messages`.

    Returns:
        A dict with "kind" and the message's relevant fields (texts shortened), or None for a
        message that is not recorded: a stream delta, a rate limit event, and the system
        messages `thinking_tokens` and `commands_changed`.
    """
    match msg:
        case StreamEvent() | RateLimitEvent():
            return None
        case SystemMessage() if msg.subtype in ("thinking_tokens", "commands_changed"):
            return None
        case AssistantMessage():
            blocks: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    blocks.append({"text": short(block.text)})
                elif isinstance(block, ThinkingBlock):
                    blocks.append({"thinking": short(block.thinking, 100)})
                elif isinstance(block, ToolUseBlock):
                    blocks.append({"tool_use": block.name, "id": block.id, "input": block.input})
                else:
                    blocks.append({"block": type(block).__name__})
            return {
                "kind": "assistant",
                "agent": msg.parent_tool_use_id,
                "model": msg.model,
                "blocks": blocks,
            }
        case UserMessage():
            content: Any
            if isinstance(msg.content, str):
                content = short(msg.content, 700)
            else:
                content = []
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content.append(
                            {
                                "tool_result": block.tool_use_id,
                                "is_error": block.is_error,
                                "content": short(block.content, 500),
                            }
                        )
                    elif isinstance(block, TextBlock):
                        content.append({"text": short(block.text, 700)})
                    else:
                        content.append({"block": type(block).__name__})
            result = msg.tool_use_result
            details = (
                {
                    "keys": sorted(result),
                    "status": result.get("status"),
                    "agentId": result.get("agentId"),
                    "isAsync": result.get("isAsync"),
                }
                if isinstance(result, dict)
                else None
            )
            origin = getattr(msg, "origin", None)
            return {
                "kind": "user",
                "agent": msg.parent_tool_use_id,
                "origin_kind": origin.get("kind") if isinstance(origin, dict) else None,
                "origin": short(origin, 150) if origin is not None else None,
                "content": content,
                "tool_use_result": details,
            }
        case TaskStartedMessage():
            return {
                "kind": "task_started",
                "task_id": msg.task_id,
                "tool_use_id": msg.tool_use_id,
                "task_type": msg.task_type,
                "description": short(msg.description, 80),
            }
        case TaskNotificationMessage():
            return {
                "kind": "task_notification",
                "task_id": msg.task_id,
                "tool_use_id": msg.tool_use_id,
                "status": str(msg.status),
                "summary": short(msg.summary, 300),
            }
        case TaskProgressMessage():
            return {
                "kind": "task_progress",
                "task_id": msg.task_id,
                "last_tool_name": msg.last_tool_name,
            }
        case SystemMessage() if msg.subtype == "init":
            data = msg.data
            tools = data.get("tools") or []
            return {
                "kind": "system:init",
                "claude_code_version": data.get("claude_code_version"),
                "model": data.get("model"),
                "permissionMode": data.get("permissionMode"),
                "tool_count": len(tools),
                "agent_tool": [name for name in tools if name in AGENT_TOOLS],
                "handback_tool_listed": HANDBACK_TOOL in tools,
                "agents": data.get("agents"),
            }
        case SystemMessage() if msg.subtype == "background_tasks_changed":
            tasks = msg.data.get("tasks")
            return {
                "kind": "system:background_tasks_changed",
                "count": len(tasks) if isinstance(tasks, list) else None,
                "tasks": short(tasks, 500),
            }
        case SystemMessage():
            return {"kind": f"system:{msg.subtype}", "data": short(msg.data, 300)}
        case ResultMessage():
            return {
                "kind": "result",
                "subtype": msg.subtype,
                "num_turns": msg.num_turns,
                "result": short(msg.result or "", 200),
                "total_cost_usd": msg.total_cost_usd,
                "models": sorted((msg.model_usage or {}).keys()),
            }
        case _:
            return {"kind": f"other:{type(msg).__name__}"}


def describe_hook_input(event: str, data: Any, tool_use_id: str | None) -> dict[str, Any]:
    """Return what the spike records of one hook input.

    Args:
        event: The hook event ("PreToolUse", "PostToolUse", "SubagentStop", ...).
        data: The hook input as the SDK passes it.
        tool_use_id: The tool call id the SDK passes (None for events without one).

    Returns:
        The event, ids and agent fields; for tool events the tool name, the input's keys, how
        `run_in_background` appears in it ("absent" or its repr) and the input shortened; for
        PostToolUse the response's keys, `status`, `agentId`, `isAsync` and content shortened;
        for SubagentStop the last assistant message shortened and the input's keys.
    """
    data = dict(data or {})
    fields: dict[str, Any] = {
        "event": event,
        "tool_use_id": tool_use_id,
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
    }
    if event in ("PreToolUse", "PostToolUse"):
        fields["tool_name"] = data.get("tool_name")
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            fields["tool_input_keys"] = sorted(tool_input)
            fields["run_in_background"] = (
                repr(tool_input["run_in_background"])
                if "run_in_background" in tool_input
                else "absent"
            )
        fields["tool_input"] = short(tool_input, 300)
    if event == "PostToolUse":
        response = data.get("tool_response")
        fields["tool_response"] = (
            {
                "keys": sorted(response),
                "status": response.get("status"),
                "agentId": response.get("agentId"),
                "isAsync": response.get("isAsync"),
                "content": short(response.get("content"), 300),
            }
            if isinstance(response, dict)
            else short(response, 300)
        )
    if event == "SubagentStop":
        fields["last_assistant_message"] = short(data.get("last_assistant_message") or "", 300)
        fields["keys"] = sorted(data)
    return fields


class RecordingGate:
    """A stand-in for calu's session as the runner's `ToolGate`; records every call.

    - `pre_tool_use`: ALLOW for the Agent and Task tools, and for `SubagentHandback` when
      `allow_handback` is set; DENY with `DENIAL` as the reason for every other call. A
      `SubagentHandback` call of a subagent is where calu's session would review the
      handed-back report (`Session._handback`); its message is recorded.
    - `ask`: denies.
    - `review_write_set`: returns "" (no note).
    - `tool_succeeded`: does nothing.
    - `review_subagent_result`: returns "" (no note, so calu sends Claude nothing).
    """

    def __init__(self, recorder: Recorder, allow_handback: bool) -> None:
        """Remember where to record and whether hand-backs pass.

        Args:
            recorder: Receives one record per call.
            allow_handback: True lets `SubagentHandback` calls run; False denies them.
        """
        self.recorder = recorder
        self.allowed = (*AGENT_TOOLS, HANDBACK_TOOL) if allow_handback else AGENT_TOOLS

    async def pre_tool_use(self, step: Step) -> GateResult:
        """Allow the Agent tools (and hand-backs if so configured), deny the rest.

        Args:
            step: The tool call.

        Returns:
            ALLOW or DENY.
        """
        allowed = step.tool_name in self.allowed
        extra = (
            {"message": short(step.tool_input.get("message", ""), 300)}
            if step.tool_name == HANDBACK_TOOL
            else {}
        )
        self.recorder.add(
            "gate:pre_tool_use",
            tool_name=step.tool_name,
            agent_id=step.agent_id,
            tool_use_id=step.tool_use_id,
            verdict="allow" if allowed else "deny",
            **extra,
        )
        if allowed:
            return GateResult(Verdict.ALLOW)
        return GateResult(Verdict.DENY, reason=DENIAL)

    async def ask(self, step: Step) -> HumanAnswer:
        """Deny a step that waits for a human (none is expected: the gate never asks).

        Args:
            step: The tool call.

        Returns:
            A denial.
        """
        self.recorder.add("gate:ask", tool_name=step.tool_name, agent_id=step.agent_id)
        return HumanAnswer(allow=False, message=DENIAL)

    async def review_write_set(self, step: Step) -> str:
        """Return no note (the gate denies every command).

        Args:
            step: The command that ran.

        Returns:
            "".
        """
        return ""

    async def tool_succeeded(self, step: Step) -> None:
        """Do nothing (the gate denies every edit).

        Args:
            step: The edit that ran.
        """

    async def review_subagent_result(self, result: SubagentResult) -> str:
        """Record that calu's runner asked for the review of a subagent's result.

        Args:
            result: What the runner passes: agent id, task, result, background, handback.

        Returns:
            "" (no note for Claude).
        """
        self.recorder.add(
            "gate:review_subagent_result",
            agent_id=result.agent_id,
            background=result.background,
            handback=result.handback,
            result=short(result.result, 300),
        )
        return ""


HookCallback = Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]


class RecordingRunner(SdkRunner):
    """calu's `SdkRunner` with Haiku, which also records messages and hook calls.

    What Claude Code gets and what calu does with it are the inherited behaviour: the options
    of `SdkRunner._options`, its hooks and `SdkRunner._translate`. The additions: each hook is
    wrapped so that its input and its answer are recorded, every SDK message is recorded before
    it is translated, the options are recorded once (names of environment variables only), and
    `ADDED_ENV` is put on top of calu's environment for Claude Code.
    """

    def __init__(
        self, workspace: Path, recorder: Recorder, permission_mode: PermissionMode | None
    ) -> None:
        """Prepare a runner for `workspace` with calu's default configuration and Haiku.

        Args:
            workspace: Claude Code's working directory.
            recorder: Receives the records.
            permission_mode: `claude.permission_mode`; None is calu's default (the mode of
                Claude Code's settings files, else "auto").
        """
        config = ClaudeConfig(model=MODEL, permission_mode=permission_mode)
        super().__init__(workspace, config, hidden_env=set(PROVIDER_KEYS))
        self.recorder = recorder

    def _options(
        self,
        gate: ToolGate,
        resume_id: str | None,
        fork: Fork | None = None,
        session_id: str | None = None,
    ) -> ClaudeAgentOptions:
        """Build calu's options, wrap every hook in a recorder and record the options.

        Args:
            gate: Passed on to `SdkRunner._options`.
            resume_id: Passed on.
            fork: Passed on.
            session_id: Passed on.

        Returns:
            calu's options; each hook callback is replaced by `_recorded` of it.
        """
        options = super()._options(gate, resume_id, fork, session_id)
        hooks: dict[Any, Any] = dict(options.hooks or {})
        for event, matchers in hooks.items():
            for matcher in matchers:
                matcher.hooks = [self._recorded(event, hook) for hook in matcher.hooks]
        self.recorder.add(
            "options",
            cli_path=options.cli_path,
            permission_mode=options.permission_mode,
            setting_sources=options.setting_sources,
            model=options.model,
            extra_args=options.extra_args,
            hooks={event: [m.matcher for m in matchers] for event, matchers in hooks.items()},
            env_names=sorted(options.env),
        )
        return options

    def _claude_env(self) -> dict[str, str]:
        """Return calu's environment overrides plus `ADDED_ENV`.

        Returns:
            Variable name -> value.
        """
        return {**super()._claude_env(), **ADDED_ENV}

    def _recorded(self, event: str, hook: HookCallback) -> HookCallback:
        """Wrap one of calu's hooks so that its input and answer are recorded.

        Args:
            event: The hook event the callback is registered for.
            hook: calu's callback.

        Returns:
            A callback that records the input (`describe_hook_input`), awaits `hook` and
            records its answer.
        """

        async def recorded(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """Record the input, run calu's hook, record and return its answer.

            Args:
                input_data: The hook input.
                tool_use_id: The tool call id, if any.
                context: The SDK hook context.

            Returns:
                What calu's hook returned.
            """
            self.recorder.add("hook", **describe_hook_input(event, input_data, tool_use_id))
            answer = await hook(input_data, tool_use_id, context)
            self.recorder.add(
                "hook-answer", event=event, tool_use_id=tool_use_id, answer=short(answer, 300)
            )
            return answer

        return recorded

    def _translate(self, msg: object) -> list[Event]:
        """Record the message (`describe`), then translate it as calu does.

        Args:
            msg: An SDK message.

        Returns:
            calu's events for it.
        """
        fields = describe(msg)
        if fields is not None:
            kind = fields.pop("kind")
            self.recorder.add(kind, **fields)
        return super()._translate(msg)


def sink_for(recorder: Recorder) -> Callable[[Event], Awaitable[None]]:
    """Return an event sink that records calu's subagent list, turn ends and log lines.

    Args:
        recorder: Receives the records.

    Returns:
        The sink; other events are ignored.
    """

    async def sink(event: Event) -> None:
        """Record `agents.update`, `turn.result` and `log` events.

        Args:
            event: A protocol event of the runner.
        """
        if isinstance(event, AgentsUpdate):
            recorder.add(
                "calu:agents.update",
                agents=[
                    {
                        "agent_id": a.agent_id,
                        "sdk_agent_id": a.sdk_agent_id,
                        "background": a.background,
                        "status": a.status,
                    }
                    for a in event.agents
                ],
            )
        elif isinstance(event, TurnResult):
            recorder.add(
                "calu:turn.result",
                subtype=event.subtype,
                background_tasks=event.background_tasks,
                result=short(event.result, 120),
            )
        elif isinstance(event, Log):
            recorder.add("calu:log", level=event.level, message=short(event.message, 300))

    return sink


def prompt_for(extra: dict[str, Any]) -> str:
    """Return the main agent's prompt for one variant.

    Args:
        extra: The fields added to `TASK_INPUT` (none for variant a).

    Returns:
        The prompt: call the Agent tool once with exactly the given JSON input and no other
        tool, then answer with the subagent's word once it has arrived.
    """
    tool_input = {**TASK_INPUT, **extra}
    return (
        "This is a test of the Agent tool. Follow these steps exactly.\n"
        "1. Call the Agent tool once. Its input must be exactly this JSON object, with these "
        f"{len(tool_input)} fields and no other field:\n{json.dumps(tool_input)}\n"
        "2. Do not call any other tool, and do not check on the agent.\n"
        "3. If the Agent tool result contains the agent's reply, answer with the single word of "
        "that reply. Otherwise answer with the single word WAITING and end your turn; when the "
        "agent's reply reaches you later, answer with its single word."
    )


def finished(records: list[dict[str, Any]]) -> bool:
    """Tell whether a run has reached its end condition (before the quiet period).

    Args:
        records: The run's records so far.

    Returns:
        True if a turn ended with no background task running and either no Agent call was made
        (the prompt failed) or the turn ended after the subagent ended (its first SubagentStop
        hook call or task notification).
    """
    turn_ends = [
        r["t"] for r in records if r["kind"] == "calu:turn.result" and r["background_tasks"] == 0
    ]
    if not turn_ends:
        return False
    if not agent_calls(records):
        return True
    ended = [
        r["t"]
        for r in records
        if (r["kind"] == "hook" and r["event"] == "SubagentStop")
        or r["kind"] == "task_notification"
    ]
    return bool(ended) and max(turn_ends) >= min(ended)


def agent_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the main agent's Agent and Task tool_use blocks, in order.

    Args:
        records: A run's records.

    Returns:
        The blocks (`tool_use`, `id`, `input`).
    """
    return [
        block
        for r in records
        if r["kind"] == "assistant" and r["agent"] is None
        for block in r["blocks"]
        if block.get("tool_use") in AGENT_TOOLS
    ]


def summarize(
    key: str, handback: str, records: list[dict[str, Any]], timed_out: bool
) -> dict[str, Any]:
    """Condense one run's records into the facts the spike asks for.

    Args:
        key: The variant ("a", "b" or "c").
        handback: "allow" or "deny", the gate's answer to `SubagentHandback`.
        records: The run's records.
        timed_out: Whether the run hit the timeout.

    Returns:
        A dict of short strings: what was sent, the tool result, the hooks, where the
        subagent's word went and arrived (each with the seconds since the start), what calu
        recorded and reviewed, the CLI version and models.
    """
    calls = agent_calls(records)
    call = calls[0] if calls else None
    call_id = call["id"] if call else None
    sent = "no Agent call"
    if call is not None:
        tool_input = call["input"]
        sent = (
            repr(tool_input["run_in_background"]) if "run_in_background" in tool_input else "absent"
        )
        others = sorted(set(tool_input) - set(TASK_INPUT) - {"run_in_background"})
        if others:
            sent += f" (+{','.join(others)})"
    pre = [
        r
        for r in records
        if r["kind"] == "hook" and r["event"] == "PreToolUse" and r["tool_use_id"] == call_id
    ]
    post = [
        r
        for r in records
        if r["kind"] == "hook" and r["event"] == "PostToolUse" and r["tool_use_id"] == call_id
    ]
    result = next(
        (
            r
            for r in records
            if r["kind"] == "user"
            and isinstance(r["content"], list)
            and any(b.get("tool_result") == call_id for b in r["content"])
        ),
        None,
    )
    places: list[str] = []
    for r in records:
        t = f"{r['t']:.1f}s"
        text = short(r.get("content", ""), 10_000)
        with_word = "with" if WORD in text else "without"
        if r is result:
            places.append(f"Agent tool_result {with_word} {WORD} {t}")
        elif r["kind"] == "user" and r["agent"] is None and "<task-notification" in text:
            places.append(f"task-notification message {with_word} {WORD} {t}")
        elif r["kind"] == "user" and r["agent"] is None and "[Subagent hand-back]" in text:
            places.append(
                f"hand-back message, origin {r.get('origin_kind')}, {with_word} {WORD} {t}"
            )
        elif r["kind"] == "task_notification":
            word = "with" if WORD in r["summary"] else "without"
            places.append(f"task_notification summary {word} {WORD} {t}")
        elif (
            r["kind"] == "assistant"
            and r["agent"] == call_id
            and any(WORD in block.get("text", "") for block in r["blocks"])
        ):
            places.append(f"subagent's own text {t}")
        elif (
            r["kind"] == "gate:pre_tool_use"
            and r["tool_name"] == HANDBACK_TOOL
            and WORD in r.get("message", "")
        ):
            places.append(f"SubagentHandback call ({r['verdict']}) {t}")
        elif (
            r["kind"] == "hook"
            and r["event"] == "SubagentStop"
            and WORD in r["last_assistant_message"]
        ):
            places.append(f"SubagentStop last_assistant_message {t}")
    stops = [
        f"{r['agent_id']} ({r['agent_type'] or 'no type'}) {r['t']:.1f}s"
        for r in records
        if r["kind"] == "hook" and r["event"] == "SubagentStop"
    ]
    changes = [
        f"{r['count']}@{r['t']:.1f}s"
        for r in records
        if r["kind"] == "system:background_tasks_changed"
    ]
    infos = [
        agent
        for r in records
        if r["kind"] == "calu:agents.update"
        for agent in r["agents"]
        if agent["agent_id"] == call_id
    ]
    reviews = [
        f"background={r['background']} handback={r['handback']} {r['t']:.1f}s"
        for r in records
        if r["kind"] == "gate:review_subagent_result"
    ]
    handbacks = [
        f"{r['verdict']} {r['t']:.1f}s"
        for r in records
        if r["kind"] == "gate:pre_tool_use" and r["tool_name"] == HANDBACK_TOOL
    ]
    main_texts = [
        f"{block['text']}@{r['t']:.1f}s"
        for r in records
        if r["kind"] == "assistant" and r["agent"] is None
        for block in r["blocks"]
        if "text" in block
    ]
    results = [r for r in records if r["kind"] == "result"]
    return {
        "variant": key,
        "SubagentHandback": handback,
        "sent": sent,
        "agent calls": str(len(calls)),
        "PreToolUse tool_input": pre[0]["run_in_background"] if pre else "-",
        "tool_result status/agentId": (
            f"{result['tool_use_result']['status']} / {result['tool_use_result']['agentId']}"
            if result and result["tool_use_result"]
            else "-"
        ),
        "tool_result content": short(result["content"], 160) if result else "-",
        "PostToolUse status": (
            ", ".join(
                str(r["tool_response"]["status"])
                for r in post
                if isinstance(r["tool_response"], dict)
            )
            if post
            else "not called"
        ),
        "SubagentStop": "; ".join(stops) or "not called",
        "background_tasks_changed": " ".join(changes) or "none",
        "where the subagent's word went": "; ".join(places) or "nowhere",
        "main agent's texts": " | ".join(main_texts) or "-",
        "calu AgentInfo (first, last)": (
            f"background={infos[0]['background']} {infos[0]['status']} -> "
            f"background={infos[-1]['background']} {infos[-1]['status']}"
            if infos
            else "-"
        ),
        "calu review_subagent_result": "; ".join(reviews) or "never",
        "SubagentHandback calls": "; ".join(handbacks) or "none",
        "CLI version (init)": ", ".join(
            sorted({str(r["claude_code_version"]) for r in records if r["kind"] == "system:init"})
        ),
        "permission mode (init)": ", ".join(
            sorted({str(r["permissionMode"]) for r in records if r["kind"] == "system:init"})
        ),
        "models billed": ", ".join(sorted({m for r in results for m in r["models"]})) or "-",
        "session cost (last result)": f"{results[-1]['total_cost_usd']}" if results else "-",
        "timed out": str(timed_out),
    }


async def run_variant(
    key: str, handback: str, mode: PermissionMode | None, parent: Path, limit_s: float
) -> dict[str, Any]:
    """Run one variant in a new directory under `parent` and summarize it.

    Side effects: creates `parent/agent-bg-<key>-<handback>-<mode, or "calu">-<time>/workspace`
    (Claude Code's working directory) and `records.jsonl` beside it; starts and stops Claude
    Code through calu's runner; prints the timeline.

    Args:
        key: The variant ("a", "b" or "c").
        handback: "allow" or "deny": the gate's answer to `SubagentHandback`.
        mode: `claude.permission_mode` for the run; None is calu's default.
        parent: Directory for the run's new directory.
        limit_s: Seconds after which the run is stopped whatever happened.

    Returns:
        `summarize` of the run.

    Raises:
        ExceptionGroup: From the task group, holding `calu.claude.runner.StartRefused` if calu's
            runner refuses to start Claude Code, `claude_agent_sdk.ClaudeSDKError` if Claude
            Code cannot be started or fails, or `TimeoutError` if it is not connected within
            120 s.
    """
    label, extra = VARIANTS[key]
    run_dir = (
        parent / f"agent-bg-{key}-{handback}-{mode or 'calu'}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    recorder = Recorder(run_dir / "records.jsonl")
    runner = RecordingRunner(workspace, recorder, mode)
    gate = RecordingGate(recorder, allow_handback=handback == "allow")
    print(
        f"\n=== variant {key}: run_in_background {label}, SubagentHandback {handback}, "
        f"claude.permission_mode {mode or 'not set'}; "
        f"workspace {workspace}",
        flush=True,
    )
    timed_out = False
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.run, gate, sink_for(recorder))
            with anyio.fail_after(120):
                await runner.wait_ready()
            await runner.send(prompt_for(extra), "later")
            deadline = time.monotonic() + limit_s
            while not finished(recorder.records) or time.monotonic() - recorder.last < QUIET_S:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                await anyio.sleep(0.5)
            await runner.stop()
    finally:
        recorder.close()
    return summarize(key, handback, recorder.records, timed_out)


def remove_host_session_variables() -> list[str]:
    """Remove the variables of an enclosing Claude Code session from this process's environment.

    Side effect: changes `os.environ` of this process, and so the environment of the Claude
    Code it starts.

    Returns:
        The removed names (`HOST_SESSION_VARIABLES` and `CLAUDE_SSH_*`), sorted.
    """
    removed = sorted(
        name
        for name in os.environ
        if name in HOST_SESSION_VARIABLES or name.startswith("CLAUDE_SSH_")
    )
    for name in removed:
        del os.environ[name]
    return removed


async def main() -> None:
    """Parse the arguments, run the chosen variants one after another and print their summaries.

    Side effects: removes the enclosing session's variables from this process's environment
    first (`remove_host_session_variables`) and prints the removed names, the SDK version, the
    bundled CLI's version and the model; then `run_variant` for each variant.
    """
    parser = argparse.ArgumentParser(
        description="Does an Agent call without run_in_background run in the background?"
    )
    parser.add_argument(
        "--parent",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="directory for the runs' new directories (default: the system's temp directory)",
    )
    parser.add_argument("--variants", default="abc", help="variants to run, e.g. 'a' or 'abc'")
    parser.add_argument(
        "--handback",
        choices=("allow", "deny"),
        default="allow",
        help="the gate's answer to a subagent's SubagentHandback call",
    )
    parser.add_argument(
        "--permission-mode",
        choices=("default", "acceptEdits", "auto"),
        default=None,
        help="claude.permission_mode for the runs (default: not set, as in calu's default)",
    )
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help="seconds per run")
    args = parser.parse_args()
    removed = remove_host_session_variables()
    print(f"removed from the environment: {', '.join(removed) or 'nothing'}")
    bundled = Path(str(claude_agent_sdk.__file__)).parent / "_bundled" / "claude"
    print(
        f"claude-agent-sdk {claude_agent_sdk.__version__}; bundled CLI {BUNDLED_CLI_VERSION} "
        f"({'present' if bundled.is_file() else 'absent'}); model {MODEL}; "
        f"added for Claude Code: {ADDED_ENV}"
    )
    summaries = [
        await run_variant(key, args.handback, args.permission_mode, args.parent, args.timeout)
        for key in args.variants
    ]
    print("\n=== summary")
    for summary in summaries:
        print(f"\nvariant {summary['variant']}")
        for name, value in summary.items():
            print(f"  {name}: {value}")


if __name__ == "__main__":
    anyio.run(main)
