"""`ClaudeRunner` implementation on top of the Claude Agent SDK (`claude-agent-sdk`).

The SDK runs Claude Code headless (stream-json over stdin/stdout). This runner

- installs a PreToolUse hook for all tools that asks `ToolGate.pre_tool_use` (allow/deny/ask),
- installs `can_use_tool`, which Claude Code calls for "ask" verdicts; it waits for `ToolGate.ask`
  without a time limit,
- translates SDK messages into protocol events and keeps the list of subagents,
- sends user messages with an explicit `priority` ("next" or "later").

Verified behaviour of these mechanisms: `.claude/skills/claude-code-integration/references/
spike-results.md`.
"""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    TaskNotificationMessage,
    TaskStartedMessage,
    ToolPermissionContext,
)

from calu.checkers.model import Step
from calu.checkers.result_checker import SubagentResult
from calu.claude.runner import EventSink, Priority, ToolGate, Verdict
from calu.config import ClaudeConfig
from calu.context.foreign import (
    SkillContentFilter,
    skill_directory,
    user_message_source,
)
from calu.files import skill_file
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    Event,
    ToolFiles,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)

log = logging.getLogger(__name__)

AGENT_TOOLS = ("Agent", "Task")
HOOK_TIMEOUT_S = 900
"""Timeout of the PreToolUse hook. The session bounds the checker call inside it
(`CHECKER_DEADLINE_S`, 840 s) and escalates when that is exceeded, so the hook answers well
before this; the wait for a human is not in this hook but in `can_use_tool`. A hook that hangs
anyway stops the step: the in-process hook of the SDK fails closed, and Claude retries the call
once per timeout (verified in `spikes/RESULTS.md`, "Hook timeouts"), which is why this is 15
minutes and not hours."""
MAX_RESULT_CHARS = 4_000
SUBAGENT_RESULT_CHARS = 60_000
"""Upper bound for a subagent result passed to the result checker."""
REVIEW_HOOK_TIMEOUT_S = 24 * 3600
"""Timeout of the two hooks that review a subagent's result: they may wait for a human, because
an escalated result is decided inside the hook."""


def tool_result_text(content: Any, limit: int = MAX_RESULT_CHARS) -> str:
    """Return a plain text rendering of a tool result's `content`, cut to `limit` characters.

    Text blocks contribute their text; other blocks are shown as "[<type>]".

    Args:
        content: A string, a list of content blocks (dicts with "type"/"text"), None, or any
            other value (converted with `str`).
        limit: Maximum number of characters kept; longer text gets a truncation marker.

    Returns:
        The text; "" for None.
    """
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'block')}]")
            else:
                parts.append("[block]")
        text = "\n".join(parts)
    else:
        text = str(content)
    return text if len(text) <= limit else text[:limit] + "\n[... truncated]"


class SdkRunner:
    """Runs one Claude Code session through `ClaudeSDKClient`. See the module docstring.

    `workspace` is Claude's working directory. One instance runs one session; create a new
    instance for another session.
    """

    def __init__(
        self, workspace: Path, config: ClaudeConfig, hidden_env: set[str] | None = None
    ) -> None:
        """Prepare a runner; nothing is started until `run`.

        Args:
            workspace: Claude's working directory.
            config: Claude Code settings (model, CLI path, setting sources).
            hidden_env: Names of environment variables (checker API keys) that Claude Code
                must not see.
        """
        self.hidden_env = set(hidden_env or ())
        self.workspace = workspace
        self._stop_requested = False
        self._streaming_ids: dict[str | None, str] = {}  # agent -> streaming message id
        self.config = config
        self._client: ClaudeSDKClient | None = None
        self._ready = anyio.Event()
        self._cancel: anyio.CancelScope | None = None
        self._steps: dict[str, Step] = {}
        self._agent_prompts: dict[str, str] = {}  # Agent tool call id -> task prompt
        self._skills = SkillContentFilter()  # skill text after Skill calls is no user input
        self.agents: dict[str, AgentInfo] = {}  # by Agent tool call id
        self._task_to_tool_use: dict[str, str] = {}  # SDK task/agent id -> tool call id
        self.session_id: str | None = None

    # ------------------------------------------------------------------ ClaudeRunner interface

    async def run(self, gate: ToolGate, sink: EventSink, resume_id: str | None = None) -> None:
        """Connect, then translate messages to events until `stop` is called.

        Args:
            gate: Decides about every tool call (PreToolUse hook and `can_use_tool`).
            sink: Receives the translated events.
            resume_id: Claude session id to resume, or None for a new session.

        Raises:
            claude_agent_sdk.ClaudeSDKError: If the CLI cannot be started or fails.
        """
        options = self._options(gate, resume_id)
        with anyio.CancelScope() as scope:
            self._cancel = scope
            if self._stop_requested:
                scope.cancel()
            async with ClaudeSDKClient(options=options) as client:
                self._client = client
                self._ready.set()
                async for msg in client.receive_messages():
                    for event in self._translate(msg):
                        await sink(event)
        self._client = None

    async def wait_ready(self) -> None:
        """Return once the SDK client is connected and `send` may be called."""
        await self._ready.wait()

    async def send(self, text: str, priority: Priority) -> None:
        """Write a user message to Claude's input.

        Precondition: `wait_ready` has returned and `run` is still active.

        Args:
            text: The message text.
            priority: "next" (read after the running tool calls) or "later" (after the turn).
        """
        assert self._client is not None, "runner is not running"
        message = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "priority": priority,
        }

        async def one() -> AsyncIterator[dict[str, Any]]:
            """Yield the single stream-json message for `ClaudeSDKClient.query`.

            Yields:
                The user message dict.
            """
            yield message

        await self._client.query(one())

    async def interrupt(self) -> None:
        """Interrupt Claude's current turn; does nothing if not connected.

        Pending `can_use_tool` calls are cancelled by the SDK.
        """
        if self._client is not None:
            await self._client.interrupt()

    async def stop(self) -> None:
        """End the session: `run` returns soon after.

        A stop before `run` makes `run` return immediately.
        """
        self._stop_requested = True
        if self._cancel is not None:
            self._cancel.cancel()

    # ------------------------------------------------------------------ options and callbacks

    def _options(self, gate: ToolGate, resume_id: str | None) -> ClaudeAgentOptions:
        """Build the SDK options: working directory, model, hooks, callbacks and environment.

        Claude Code authenticates as configured on this machine; only checker keys are hidden
        from it (`_claude_env`). The sandbox settings of `config.sandbox` are passed as a
        `--settings` JSON document, which overrides the settings files of the project and of the
        user (PLAN.md D15).

        Args:
            gate: Used by the PreToolUse hook and by `can_use_tool`.
            resume_id: Claude session id to resume, or None.

        Returns:
            The options for `ClaudeSDKClient`.
        """

        async def pre_tool_use(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PreToolUse hook: ask `gate.pre_tool_use` and return its verdict to Claude Code.

            For an ASK verdict the step is stored, so that `can_use_tool` reuses it. A PASS
            verdict is answered without a decision where Claude Code's auto mode is in effect
            (`claude.auto_mode` and the permission mode of the hook input), so that the auto
            mode classifier decides as well (PLAN.md D5, D21); where there is no auto mode to
            defer to, PASS becomes "allow". ALLOW always answers "allow".

            Args:
                input_data: The hook input (dict with tool_name, tool_input, agent_id, cwd...).
                tool_use_id: Id of the tool call, if the SDK passes it.
                context: SDK hook context (unused).

            Returns:
                The hook output with `permissionDecision` "allow", "deny" or "ask", or without
                a decision for PASS under auto mode.
            """
            step = self._step_from_hook(input_data, tool_use_id)
            result = await gate.pre_tool_use(step)
            if step.tool_use_id and result.verdict is Verdict.ASK:
                self._steps[step.tool_use_id] = step  # taken again by can_use_tool
            output: dict[str, Any] = {"hookEventName": "PreToolUse"}
            if result.context:
                output["additionalContext"] = result.context  # verified: Claude reads it
            auto = self.config.auto_mode and input_data.get("permission_mode") == "auto"
            if result.verdict is Verdict.PASS and auto:
                # No decision: Claude Code's auto mode classifier decides as well (checkers AND
                # auto mode). "allow" would skip the classifier (verified, spike_auto_mode.py).
                return {"hookSpecificOutput": output}
            decision = "allow" if result.verdict is Verdict.PASS else result.verdict.value
            output["permissionDecision"] = decision
            output["permissionDecisionReason"] = result.reason
            return {"hookSpecificOutput": output}

        async def post_tool_use(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PostToolUse hook: review the result of a finished foreground subagent.

            The main agent waits while this runs (verified); a note from the review is sent as
            a priority "next" message, which Claude reads together with the result (verified;
            `additionalContext` was not acted on for the Agent tool).

            Args:
                input_data: Hook input (`tool_name`, `tool_input`, `tool_response`).
                tool_use_id: Id of the Agent tool call (the protocol agent id).
                context: SDK hook context (unused).

            Returns:
                An empty hook output.
            """
            if input_data.get("tool_name") not in AGENT_TOOLS:
                return {}
            response = input_data.get("tool_response")
            if not isinstance(response, dict) or response.get("status") != "completed":
                return {}
            tool_input = input_data.get("tool_input") or {}
            result = SubagentResult(
                agent_id=tool_use_id,
                description=str(tool_input.get("description", "")),
                prompt=str(tool_input.get("prompt", "")),
                result=tool_result_text(response.get("content"), limit=SUBAGENT_RESULT_CHARS),
            )
            note = await gate.review_subagent_result(result)
            await self._send_note(note)
            return {}

        async def subagent_stop(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """SubagentStop hook: review the final result of a background subagent.

            Foreground subagents are reviewed in `post_tool_use` instead (it has the full
            tool result). The note is injected as a priority "next" message; it reaches the
            main agent before the task notification with the result (verified).

            Args:
                input_data: Hook input (`agent_id`, `last_assistant_message`, ...).
                tool_use_id: Unused (None for this event).
                context: SDK hook context (unused).

            Returns:
                An empty hook output (the subagent is not asked to continue).
            """
            agent_id = self._agent_id(input_data.get("agent_id"))
            info = self.agents.get(agent_id or "")
            if info is None:
                log.warning("SubagentStop for unknown subagent %r; result not reviewed", agent_id)
                return {}
            if not info.background:
                return {}
            last = str(input_data.get("last_assistant_message") or "")
            result = SubagentResult(
                agent_id=agent_id,
                description=info.description,
                prompt=self._agent_prompts.get(info.agent_id, ""),
                result=last[:SUBAGENT_RESULT_CHARS],
                background=True,
            )
            note = await gate.review_subagent_result(result)
            await self._send_note(note)
            return {}

        async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            """Permission callback for "ask" verdicts: wait for the human via `gate.ask`.

            Uses the step stored by `pre_tool_use` if there is one, else builds it from the
            arguments. Waits without a time limit.

            Args:
                tool_name: Name of the tool.
                tool_input: Input of the tool call.
                ctx: SDK permission context (may carry `tool_use_id` and `agent_id`).

            Returns:
                Allow with the unchanged input, or deny with the human's message.
            """
            tool_use_id = getattr(ctx, "tool_use_id", None)
            step = self._steps.pop(tool_use_id or "", None) or Step(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                agent_id=self._agent_id(getattr(ctx, "agent_id", None)),
                cwd=str(self.workspace),
            )
            answer = await gate.ask(step)
            if answer.allow:
                if answer.note:
                    await self._send_note(
                        f"Note from the user on the step they allowed: {answer.note}"
                    )
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(message=answer.message or "The user denied this step.")

        return ClaudeAgentOptions(
            cwd=str(self.workspace),
            model=self.config.model,
            effort=self.config.effort,
            cli_path=self.config.cli_path,
            permission_mode="auto" if self.config.auto_mode else "default",
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[pre_tool_use],  # type: ignore[list-item]
                        timeout=HOOK_TIMEOUT_S,
                    )
                ],
                "PostToolUse": [
                    HookMatcher(
                        matcher="Agent|Task",
                        hooks=[post_tool_use],  # type: ignore[list-item]
                        timeout=REVIEW_HOOK_TIMEOUT_S,
                    )
                ],
                "SubagentStop": [
                    HookMatcher(
                        matcher=None,
                        hooks=[subagent_stop],  # type: ignore[list-item]
                        timeout=REVIEW_HOOK_TIMEOUT_S,
                    )
                ],
            },
            setting_sources=self.config.setting_sources,  # type: ignore[arg-type]
            # `--settings` as a JSON document: it overrides the project's and the user's
            # settings files, so the agent cannot switch the sandbox off (PLAN.md D15).
            settings=json.dumps(self.config.sandbox.settings()),
            extra_args={
                "replay-user-messages": None,
                "forward-subagent-text": None,
                # Readable thinking summaries (verified: works without setting a thinking mode).
                "thinking-display": self.config.thinking_display,
            },
            include_partial_messages=self.config.stream_partial,
            resume=resume_id,
            env=self._claude_env(),
            stderr=lambda line: log.debug("claude stderr: %s", line),
        )

    async def _send_note(self, note: str) -> None:
        """Send `note` as a priority "next" message; empty notes and send errors are ignored.

        Used from hooks, which may still run after the session was stopped.

        Args:
            note: The text, or "".
        """
        if not note or self._client is None:
            return
        try:
            await self.send(note, "next")
        except Exception:
            log.exception("sending a note to Claude failed")

    def _claude_env(self) -> dict[str, str]:
        """Return the environment overrides for the Claude Code process.

        Checker keys (`hidden_env`) are blanked so that Claude cannot read them. With
        `claude.use_bedrock`, `CLAUDE_CODE_USE_BEDROCK` is set to "1". Otherwise Claude Code's own
        authentication settings (claude.ai login, Bedrock, Vertex, Foundry, API key) are left as
        they are configured on this machine.

        Returns:
            Variable name -> value ("" means unset for Claude Code).
        """
        env = dict.fromkeys(sorted(self.hidden_env), "")
        if self.config.use_bedrock:
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        return env

    def _agent_id(self, sdk_agent_id: str | None) -> str | None:
        """Map an SDK agent id (hook/permission context) to the protocol agent id.

        Args:
            sdk_agent_id: The SDK's agent/task id, or None for the main agent.

        Returns:
            The id of the Agent tool call that started the subagent; the SDK id itself if it is
            unknown; None for the main agent.
        """
        if sdk_agent_id is None:
            return None
        return self._task_to_tool_use.get(sdk_agent_id, sdk_agent_id)

    def _step_from_hook(self, input_data: Any, tool_use_id: str | None) -> Step:
        """Build a `Step` from PreToolUse hook input.

        Args:
            input_data: The hook input dict.
            tool_use_id: Id of the tool call from the SDK; if None, taken from `input_data`.

        Returns:
            The step; `cwd` defaults to the workspace.
        """
        return Step(
            tool_name=str(input_data.get("tool_name", "")),
            tool_input=dict(input_data.get("tool_input") or {}),
            tool_use_id=tool_use_id or input_data.get("tool_use_id"),
            agent_id=self._agent_id(input_data.get("agent_id")),
            cwd=str(input_data.get("cwd") or self.workspace),
        )

    # ------------------------------------------------------------------ message translation

    def _agents_update(self) -> AgentsUpdate:
        """Return an `AgentsUpdate` event with copies of all subagent records.

        Returns:
            The event; later changes to `agents` do not affect it.
        """
        return AgentsUpdate(agents=[a.model_copy() for a in self.agents.values()])

    def _translate(self, msg: object) -> list[Event]:
        """Return the protocol events for one SDK message (possibly none).

        Side effects: updates `agents`, the SDK-id mapping and `session_id`.

        Args:
            msg: A message from `ClaudeSDKClient.receive_messages`; unknown types are ignored.

        Returns:
            The events in the order they should be emitted.

        """
        events: list[Event] = []
        match msg:
            case AssistantMessage():
                agent = msg.parent_tool_use_id
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        message_id = getattr(msg, "message_id", None)
                        events.append(
                            AssistantText(agent_id=agent, text=block.text, message_id=message_id)
                        )
                    elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                        events.append(
                            AssistantThinking(
                                agent_id=agent,
                                text=block.thinking,
                                message_id=getattr(msg, "message_id", None),
                            )
                        )
                    elif isinstance(block, ToolUseBlock):
                        self._skills.tool_use(block.name, agent, block.id)
                        events.append(
                            ToolUse(
                                agent_id=agent,
                                tool_use_id=block.id,
                                name=block.name,
                                input=dict(block.input),
                            )
                        )
                        if block.name in AGENT_TOOLS:
                            self._agent_prompts[block.id] = str(block.input.get("prompt", ""))
                            self.agents[block.id] = AgentInfo(
                                agent_id=block.id,
                                description=str(block.input.get("description", "")),
                                subagent_type=block.input.get("subagent_type"),
                                background=bool(block.input.get("run_in_background", False)),
                            )
                            events.append(self._agents_update())
            case UserMessage():
                agent = msg.parent_tool_use_id
                if isinstance(msg.content, str):
                    if agent is None and not self._skill_text(msg.content, events):
                        events.append(
                            UserInput(text=msg.content, source=user_message_source(msg.content))
                        )
                else:
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            events.append(
                                ToolResult(
                                    agent_id=agent,
                                    tool_use_id=block.tool_use_id,
                                    is_error=bool(block.is_error),
                                    content=tool_result_text(block.content),
                                )
                            )
                            events += self._agent_result(block.tool_use_id, msg)
                        elif (
                            isinstance(block, TextBlock)
                            and agent is None
                            and not self._skill_text(block.text, events)
                        ):
                            events.append(
                                UserInput(text=block.text, source=user_message_source(block.text))
                            )
            case TaskStartedMessage():
                if msg.tool_use_id:
                    self._task_to_tool_use[msg.task_id] = msg.tool_use_id
                    if msg.tool_use_id in self.agents:
                        self.agents[msg.tool_use_id].sdk_agent_id = msg.task_id
                        events.append(self._agents_update())
            case TaskNotificationMessage():
                info = self.agents.get(msg.tool_use_id or "")
                if info is not None:
                    status = str(msg.status)
                    info.status = (
                        "completed"
                        if status == "completed"
                        else "failed"
                        if status == "failed"
                        else "stopped"
                    )
                    events.append(self._agents_update())
            case StreamEvent():
                events += self._stream_delta(msg.event, msg.parent_tool_use_id)
            case ResultMessage():
                self.session_id = msg.session_id
                events.append(
                    TurnResult(
                        session_id=msg.session_id,
                        is_error=msg.is_error,
                        subtype=msg.subtype,
                        result=msg.result or "",
                        cost_usd=msg.total_cost_usd,
                        num_turns=msg.num_turns,
                    )
                )
            case SystemMessage() if msg.subtype == "init":
                self.session_id = msg.data.get("session_id", self.session_id)
            case _:
                pass
        return events

    def _stream_delta(self, event: dict[str, Any], agent: str | None = None) -> list[Event]:
        """Translate a raw API stream event into an `assistant.delta` event, if it has text.

        "message_start" remembers, per agent, the id of the message being generated; text and
        thinking deltas of "content_block_delta" events become `AssistantDelta` events with that
        id and agent. (So far the CLI sends stream events only for the main agent; the per-agent
        bookkeeping keeps parallel streams apart if that changes.)

        Args:
            event: The raw event dict of a `StreamEvent`.
            agent: The event's `parent_tool_use_id` (None for the main agent).

        Returns:
            A list with one `AssistantDelta`, or an empty list.
        """
        kind = event.get("type")
        if kind == "message_start":
            self._streaming_ids[agent] = str(event.get("message", {}).get("id") or "")
            return []
        message_id = self._streaming_ids.get(agent) or "unknown"
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [
                    AssistantDelta(agent_id=agent, message_id=message_id, text=str(delta["text"]))
                ]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [
                    AssistantDelta(
                        agent_id=agent,
                        message_id=message_id,
                        text=str(delta["thinking"]),
                        thinking=True,
                    )
                ]
        return []

    def _skill_text(self, text: str, events: list[Event]) -> bool:
        """Recognize the skill text after a `Skill` call (`SkillContentFilter`).

        For skill text, appends a `tool.files` event that links the `Skill` call to the skill's
        SKILL.md (if the file exists); the text itself is not reported as user input.

        Args:
            text: The text of a user message of the main agent.
            events: The events of the current message; may be extended.

        Returns:
            True if `text` is skill text (the caller drops it).
        """
        tool_use_id = self._skills.match(text)
        if tool_use_id is None:
            return False
        link = skill_file(skill_directory(text))
        if link is not None:
            events.append(ToolFiles(tool_use_id=tool_use_id, files=[link]))
        return True

    def _agent_result(self, tool_use_id: str, msg: UserMessage) -> list[Event]:
        """Update a subagent's record from the result of its Agent tool call.

        Records the SDK agent id and marks the subagent "completed" if the result says so.

        Args:
            tool_use_id: Id of the tool call whose result arrived.
            msg: The user message carrying the result (`tool_use_result` holds the details).

        Returns:
            An `AgentsUpdate` event if `tool_use_id` belongs to a known subagent and the result
            has details, else an empty list.
        """
        info = self.agents.get(tool_use_id)
        result = getattr(msg, "tool_use_result", None)
        if info is None or not isinstance(result, dict):
            return []
        info.sdk_agent_id = result.get("agentId") or info.sdk_agent_id
        if info.sdk_agent_id:
            self._task_to_tool_use[info.sdk_agent_id] = tool_use_id
        status = result.get("status")
        if status == "completed":
            info.status = "completed"
        return [self._agents_update()]
