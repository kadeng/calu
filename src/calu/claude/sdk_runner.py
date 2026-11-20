"""`ClaudeRunner` implementation on top of the Claude Agent SDK (`claude-agent-sdk`).

The SDK runs Claude Code headless (stream-json over stdin/stdout). This runner

- installs a PreToolUse hook for all tools that asks `ToolGate.pre_tool_use` (allow/deny/ask),
- installs `can_use_tool`, which Claude Code calls for "ask" verdicts; it waits for `ToolGate.ask`
  without a time limit,
- translates SDK messages into protocol events and keeps the list of subagents,
- sends user messages with an explicit `priority` ("next" or "later"),
- decides which user-role messages of the stream are the user's words (`_user_source`): only a
  text that the session sent for a human through `send`; calu's own notes, task notifications
  and everything else Claude Code puts into the conversation are reported as not from the user,
- reports the effective permission mode, because outside the auto mode the checkers decide
  alone (agent/records/PLAN.md D5, D21),
- does not start, and makes a Bash step wait for a human, while a settings file of Claude Code
  holds a sandbox entry beyond calu's configuration and `claude.sandbox.fail_if_unavailable`
  is set (`calu.preflight.settings_file_sandbox_blockers`),
- calls the event sink for every event and never stops the stream because the sink raised;
  every turn Claude Code ends is reported with a `turn.result` (`_forward`, `_emit`).

Verified behaviour of these mechanisms: `.claude/skills/claude-code-integration/references/
spike-results.md`.
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
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
    RateLimitEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    ToolPermissionContext,
)

from calu.checkers.model import Step
from calu.checkers.policy import SHELL_TOOLS, Route, unknown_tools
from calu.checkers.result_checker import SubagentResult
from calu.claude.runner import (
    NOT_FROM_USER,
    EventSink,
    Fork,
    ModelChangeFailed,
    PermissionModeChangeFailed,
    Priority,
    SideQuestionFailed,
    StartRefused,
    ToolGate,
    UsageReport,
    Verdict,
    hook_decision,
)
from calu.config import ClaudeConfig, PermissionMode
from calu.context.foreign import (
    SkillContentFilter,
    skill_directory,
    user_message_source,
)
from calu.files import skill_file
from calu.launch import LAUNCH_ENV, RunAs, claude_path, launcher_path
from calu.markdown import fence
from calu.paths import INSIDE_SESSION_ENV
from calu.preflight import (
    launch_permission_mode,
    settings_file_sandbox_blockers,
    settings_permission_mode,
)
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    ChatImage,
    ContextUsage,
    Event,
    Log,
    PermissionModeChanged,
    RateLimit,
    ToolFiles,
    ToolResult,
    ToolUse,
    TurnResult,
    UsageUpdate,
    UserInput,
)

log = logging.getLogger(__name__)

SIDE_QUESTION_TIMEOUT_S = 180.0
"""How long `SdkRunner.side_question` waits for Claude Code's answer."""

USAGE_TIMEOUT_S = 20.0
"""How long `SdkRunner.usage_report` waits for each of Claude Code's two answers."""

ACCOUNT_LIMIT_TYPES = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
"""The windows of `get_usage`'s `rate_limits` that `account_limits` reads, in this order."""

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
SYNTHETIC_MODEL = "<synthetic>"
"""`AssistantMessage.model` of a text Claude Code wrote itself: the output of a slash command
it ran without the model, or an error text."""
SLASH_COMMAND = re.compile(r"/[\w:.-]+(\s|$)")
"""The form of a slash command at the start of a message: "/compact", "/compact keep the plan"."""
LOCAL_COMMAND_STDOUT = re.compile(r"<local-command-stdout>(.*)</local-command-stdout>", re.DOTALL)
"""A user-role text that holds what a slash command of Claude Code printed."""
LATER_TURNS_REFUSED = "user entry not attributable to the declared turn"
"""Part of Claude Code's refusal of a fork point whose guard names one turn while later turns
would be discarded with it ("Resume rejected by --resume-drops-turn: ... range contains a user
entry not attributable to the declared turn")."""
INTERRUPTED_TEXT = re.compile(r"\[Request interrupted by user( for tool use)?\]")
"""The user-role text Claude Code adds to the conversation when a turn was interrupted."""
MARKDOWN_LINE = re.compile(r"^\s*(#{1,6} |\||[-*] |```)", re.MULTILINE)
"""A line that only Markdown output has: a heading, a table row, a list item or a fence."""
COMPACTING_NOTICE = (
    "Claude Code is compacting the conversation: Claude continues with a summary of the "
    "turns so far."
)
"""Shown when a compaction starts (`/compact`, or by itself when the context is full)."""
CONVERSATION_CLEARED_NOTICE = (
    "Claude Code cleared the conversation (/clear): Claude no longer knows the turns above. "
    "calu's record of them and the checkers' context are kept."
)
"""Shown after `/clear`: the clients' transcripts still show what Claude has forgotten."""
MAX_PENDING_HUMAN_TEXTS = 256
"""How many texts sent for a human the runner remembers until Claude Code replays them. A text
that is never replayed (the session was interrupted first) would otherwise stay for ever; the
oldest is dropped beyond this number, and its late replay is then not counted as user input."""
MAX_PENDING_HUMAN_IMAGE_TEXTS = 8
"""How many pending human texts (`MAX_PENDING_HUMAN_TEXTS`) may keep their attached images until
the replay. An image can be several megabytes, so beyond this number the images of the oldest
pending text are dropped (the text itself stays pending); its `UserInput` then has no images.
Claude Code has received the images already in any case (`send`)."""
REVIEW_HOOK_TIMEOUT_S = 24 * 3600
"""Timeout of the two hooks that review a subagent's result: they may wait for a human, because
an escalated result is decided inside the hook."""


def fork_turn(fork: Fork | None) -> str:
    """Name the turn a fork discards first, for a log line.

    Args:
        fork: The fork, or None.

    Returns:
        The turn number, or "?" if it is not known.
    """
    return "?" if fork is None or fork.before_turn is None else str(fork.before_turn)


def command_output(text: str) -> str:
    r"""Return what a slash command of Claude Code printed, as Markdown that keeps its lines.

    `/cost` prints aligned lines of plain text, which Markdown would join into one paragraph;
    `/context` prints Markdown (headings and a table).

    Args:
        text: The command's output.

    Returns:
        `text` unchanged if it has one line or a line that starts like Markdown (a heading, a
        table row, a list item, a fence); else `text` in a code fence it cannot close.

    Example:
        `command_output("Cost:  $0.01\nTime:  2s")` is "```\nCost:  $0.01\nTime:  2s\n```".
    """
    if "\n" not in text.strip() or MARKDOWN_LINE.search(text):
        return text
    return fence(text.strip("\n"))


def account_limits(answer: object) -> tuple[list[RateLimit], str]:
    """Read the plan's usage limits out of Claude Code's answer to `get_usage`.

    The answer's shape is Claude Code's experimental one: `rate_limits` maps a window name to
    `{utilization: 0-100 or null, resets_at: ISO 8601 or null}` or null, and
    `subscription_type` names the plan. Anything that does not have that shape is skipped.

    Args:
        answer: The control response's payload.

    Returns:
        (one `RateLimit` per window of `ACCOUNT_LIMIT_TYPES` that has a utilisation, with
        `utilization` 0-1, `status` "allowed" and `reported_at` now; the plan's name or "").

    Example:
        `{"rate_limits": {"five_hour": {"utilization": 40, "resets_at": None}}}` gives one
        limit of type "five_hour" with utilization 0.4.
    """
    if not isinstance(answer, dict):
        return [], ""
    subscription = answer.get("subscription_type")
    windows = answer.get("rate_limits")
    found: list[RateLimit] = []
    now = datetime.now(UTC)
    for kind in ACCOUNT_LIMIT_TYPES:
        window = windows.get(kind) if isinstance(windows, dict) else None
        used = window.get("utilization") if isinstance(window, dict) else None
        if not isinstance(used, int | float) or isinstance(used, bool):
            continue
        resets: datetime | None = None
        raw = window.get("resets_at") if isinstance(window, dict) else None
        if isinstance(raw, str):
            try:
                resets = datetime.fromisoformat(raw)
            except ValueError:
                resets = None
        found.append(
            RateLimit(type=kind, utilization=float(used) / 100, resets_at=resets, reported_at=now)
        )
    return found, subscription if isinstance(subscription, str) else ""


def usage_shape_problem(answer: object) -> str:
    """Say whether Claude Code's answer to `get_usage` has the shape `account_limits` reads.

    Known shape: a mapping with the key `rate_limits`, whose value is null (an account without
    such limits) or maps window names to null or to a mapping whose `utilization` is a number
    or null. `get_usage` is experimental on Claude Code's side, so a later version may answer
    otherwise; the usage panel then says so (D250).

    Args:
        answer: The control response's payload.

    Returns:
        "" for the known shape, else one sentence that says what is unknown about it.
    """
    unknown = "Claude Code answered get_usage in a shape calu does not know"
    if not isinstance(answer, dict) or "rate_limits" not in answer:
        return f"{unknown} (no rate_limits)"
    windows = answer["rate_limits"]
    if windows is None:
        return ""
    if not isinstance(windows, dict):
        return f"{unknown} (rate_limits is no mapping)"
    for name, window in windows.items():
        if window is None:
            continue
        used = window.get("utilization") if isinstance(window, dict) else "?"
        if used is not None and (not isinstance(used, int | float) or isinstance(used, bool)):
            return f"{unknown} (the window {name})"
    return ""


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


def permission_prompt(ctx: Any) -> str:
    """Return what Claude Code says it is asking permission for (agent/records/PLAN.md D60).

    `ToolPermissionContext` carries its own prompt sentence (`title`), a subtitle
    (`description`), why the request was raised (`decision_reason`) and the path that triggered
    it (`blocked_path`). The SDK's own documentation says to show the title rather than
    rebuilding a sentence from the tool name and the input, which is doubly true for a
    capability request, where the "tool" is something like `SandboxNetworkAccess` and the input
    is empty.

    Args:
        ctx: The SDK's permission context (any object; missing fields are ignored).

    Returns:
        The parts that are present, joined with " — "; "" when the context says nothing, which
        is the case for an older CLI.

    Example:
        A command that wants an unlisted domain gives something like
        "Claude wants to fetch https://example.net/ — the sandbox allows only the configured
        domains".
    """
    parts = [
        str(getattr(ctx, name, "") or "").strip()
        for name in ("title", "description", "decision_reason")
    ]
    blocked = str(getattr(ctx, "blocked_path", "") or "").strip()
    if blocked:
        parts.append(f"the path it asked for: {blocked}")
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " — ".join(seen)


class SdkRunner:
    """Runs one Claude Code session through `ClaudeSDKClient`. See the module docstring.

    `workspace` is Claude's working directory. One instance runs one session; create a new
    instance for another session.
    """

    home: Path | None = None
    """The home directory whose `.claude/settings.json` is inspected for sandbox entries; None
    uses the real one. The unit tests point it at an empty directory, so that they do not
    depend on the settings of whoever runs them."""

    def __init__(
        self,
        workspace: Path,
        config: ClaudeConfig,
        hidden_env: set[str] | None = None,
        tool_routes: dict[str, Route] | None = None,
        run_as: RunAs | None = None,
    ) -> None:
        """Prepare a runner; nothing is started until `run`.

        Args:
            workspace: Claude's working directory.
            config: Claude Code settings (model, CLI path, setting sources).
            hidden_env: Names of environment variables (checker API keys) that Claude Code
                must not see.
            tool_routes: The configured routes per tool, so that a tool named there is not
                reported as unknown (D154).
            run_as: The user Claude Code runs as, started through calu's launcher with an
                environment built for that user (D207); None starts it as the user calu
                runs as, with calu's environment.
        """
        self.run_as = run_as
        self.tool_routes = dict(tool_routes or {})
        self._tools_reported = False
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
        self._side_pending = 0  # side questions sent and not answered yet
        self._side_stops: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        """Unknown agents that stopped while a side question was open: (last message, review).

        Claude Code answers a side question with a fork of its own and calls the SubagentStop
        hook for it; `side_question` drops the entry whose message is its answer and runs the
        review of every other one.
        """
        self._skills = SkillContentFilter()  # skill text after Skill calls is no user input
        self.agents: dict[str, AgentInfo] = {}  # by Agent tool call id
        self._task_to_tool_use: dict[str, str] = {}  # SDK task/agent id -> tool call id
        self._human_texts: list[tuple[str, list[ChatImage]]] = []
        """Texts sent for a human and not yet replayed, oldest first: (stripped text, images).

        Each entry keeps the images sent with that text, so that two sends of the same text pair
        with their replays in order. At most `MAX_PENDING_HUMAN_TEXTS` entries, of which at most
        `MAX_PENDING_HUMAN_IMAGE_TEXTS` keep images (`send`).
        """
        self._interrupting = False
        """True from `interrupt` until the next turn ends.

        Claude Code ends an interrupted turn as "error_during_execution" with the error flag
        set (measured with CLI 2.1.327) and adds the user-role text `INTERRUPTED_TEXT`. The
        user's own interrupt is no error: the turn's result says "interrupted" instead, as
        the fake runner's does, and the text is left out.
        """
        self._compactions = 0
        """Compactions that started (PreCompact hook) and whose own agent has not stopped yet.

        Claude Code writes the summary of a compaction with an agent of its own and calls the
        SubagentStop hook for it. That agent is no subagent of the conversation: its stop is
        counted here and not reviewed (`subagent_stop`).
        """
        self._modes: dict[str | None, str] = {}  # SDK agent id -> permission mode last seen
        self._launch_mode: PermissionMode | None = None
        """The `--permission-mode` of the last start (`_options`); None: none was passed."""
        self._expected_mode: str | None = None
        """The mode the last start should give: `_launch_mode`, else the `defaultMode` of a
        loaded settings file; what `permission_mode` answers before Claude Code reports one."""
        self._chosen_mode: str | None = None
        """The mode calu last set or the user chose: `_expected_mode` at a start, then the mode
        of each `set_permission_mode`. Claude Code reporting another mode than this one for the
        main agent at its first sight is the only case of the "started in auto" warning
        (`_mode_logs`, D250)."""
        self._switch_pending = False
        """True from a `set_permission_mode` until Claude Code's next report of the main
        agent's mode: that report answers the switch (`_mode_events`)."""
        self._switched_to: str | None = None
        """The mode of a `set_permission_mode` that Claude Code accepted and has not reported
        yet; the hook answers by it in that short time (`_mode_now`)."""
        self._ask_reasons: dict[str, str] = {}  # tool call id -> why the runner made it wait
        self._sink: EventSink | None = None  # set by `run`; hooks report through it
        self.sink_failures = 0
        """How often the sink raised on an event; each time is logged (`_emit`)."""
        self.background_tasks = 0
        """Claude Code's background tasks now running, from `background_tasks_changed` (D158)."""
        self.session_id: str | None = None
        self.rate_limits: dict[str, RateLimit] = {}
        """The newest state of each usage limit Claude Code reported, by limit type."""

    # ------------------------------------------------------------------ ClaudeRunner interface

    async def run(
        self,
        gate: ToolGate,
        sink: EventSink,
        resume_id: str | None = None,
        fork: Fork | None = None,
        session_id: str | None = None,
    ) -> None:
        """Connect, then translate messages to events until `stop` is called.

        Before anything is started, Claude Code's settings files are inspected: with
        `claude.sandbox.fail_if_unavailable`, a sandbox entry in them that goes beyond calu's
        configuration refuses the start (`calu.preflight.settings_file_sandbox_blockers`).

        The sink contract (`calu.claude.runner.EventSink`): `sink` is called once for every
        event, in order. A message that cannot be translated and an event on which `sink`
        raises are logged at level "error" and do not stop the stream (`_forward`, `_emit`);
        a result message that cannot be translated still gives a `turn.result`, so the end of
        a turn is always reported. Only an error of the SDK's own transport ends the loop.

        Args:
            gate: Decides about every tool call (PreToolUse hook and `can_use_tool`).
            sink: Receives the translated events.
            resume_id: Claude session id to resume, or None for a new session.
            fork: Where to branch off the resumed conversation (agent/records/PLAN.md D36).
            session_id: The id a new or forked conversation gets (`ClaudeRunner.run`).

        Raises:
            StartRefused: If a loaded settings file holds sandbox entries beyond calu's
                configuration; nothing was started.
            claude_agent_sdk.ClaudeSDKError: If the CLI cannot be started or fails, including a
                fork point the CLI refuses ("Resume rejected by --resume-drops-turn:").

        A fork that discards more than one turn is started a second time without
        `Fork.drops_turn` (`_without_guard`).
        """
        self._sink = sink
        blockers = self._sandbox_blockers()
        if blockers:
            raise StartRefused(" ".join(blockers))
        with anyio.CancelScope() as scope:
            self._cancel = scope
            if self._stop_requested:
                scope.cancel()
            try:
                await self._converse(self._options(gate, resume_id, fork, session_id), sink)
            except ClaudeSDKError as exc:
                unguarded = None if self._ready.is_set() else self._without_guard(fork, exc)
                if unguarded is None:
                    raise
                log.info("fork before turn %s: started again without the guard", fork_turn(fork))
                await self._converse(self._options(gate, resume_id, unguarded, session_id), sink)
        self._client = None

    async def _converse(self, options: ClaudeAgentOptions, sink: EventSink) -> None:
        """Start Claude Code with `options` and forward its messages until the stream ends.

        Side effects: sets `_client` and, once connected, `_ready`.

        Args:
            options: The SDK options of this start.
            sink: Receives the translated events.

        Raises:
            claude_agent_sdk.ClaudeSDKError: If the CLI cannot be started or fails.
        """
        async with ClaudeSDKClient(options=options) as client:
            self._client = client
            self._ready.set()
            async for msg in client.receive_messages():
                await self._forward(msg, sink)

    @staticmethod
    def _without_guard(fork: Fork | None, error: Exception) -> Fork | None:
        """Return the fork to try again with after Claude Code refused a guarded fork point.

        `Fork.drops_turn` makes Claude Code check that everything after `Fork.resume_at`
        belongs to that one turn. The check can only hold for a fork before the newest turn:
        a fork further back discards later turns too, and Claude Code refuses it (measured
        with CLI 2.1.327; the same fork without the check works and knows exactly the turns
        up to `resume_at`). The runner does not know how many turns follow, so it asks with
        the check first.

        Args:
            fork: The fork that was refused, or None.
            error: What the start raised.

        Returns:
            `fork` without `drops_turn` if the refusal is the one about later turns
            (`LATER_TURNS_REFUSED`); None for every other error, a fork without a point and
            no fork, which the caller raises again.
        """
        if fork is None or fork.resume_at is None or fork.drops_turn is None:
            return None
        if LATER_TURNS_REFUSED not in str(error):
            return None
        return replace(fork, drops_turn=None)

    def _sandbox_blockers(self) -> list[str]:
        """Return why calu cannot be sure of the sandbox right now; reads the settings files.

        Returns:
            `calu.preflight.settings_file_sandbox_blockers` for this runner's configuration,
            workspace and home directory: [] when nothing stands against running a command.
        """
        return settings_file_sandbox_blockers(self.config, self.home, self.workspace)

    async def _forward(self, msg: object, sink: EventSink) -> None:
        """Translate one SDK message and hand its events to `sink`; never raises `Exception`.

        The stream carries text the model wrote, and the sink ends in a terminal or a pipe.
        Neither may end an unsupervised session, and neither may leave it waiting for the end
        of a turn that has already ended:

        - A failing translation becomes one `log` event of level "error" that names the
          message type. If the message was the result of a turn, a `turn.result` with
          `is_error` true and the subtype "untranslated" follows, so that the turn ends for
          the session as it has ended for Claude Code.
        - Every event is handed to the sink, also after the sink raised on an earlier one
          (`_emit`).

        Tool calls are not affected either way, because they are decided in the hooks, not
        here. Cancellation passes through.

        Args:
            msg: A message from `ClaudeSDKClient.receive_messages`.
            sink: Receives the events.
        """
        events: list[Event]
        try:
            events = self._translate(msg)
        except Exception as exc:
            log.exception("translating a %s failed", type(msg).__name__)
            events = [
                Log(
                    level="error",
                    message=(
                        f"calu could not translate a {type(msg).__name__} of Claude Code "
                        f"({type(exc).__name__}: {exc}); the message is not shown."
                    ),
                )
            ]
            if isinstance(msg, ResultMessage):
                session_id = getattr(msg, "session_id", None)
                events.append(
                    TurnResult(
                        session_id=session_id if isinstance(session_id, str) else None,
                        is_error=True,
                        subtype="untranslated",
                        result=(
                            "Claude Code ended the turn, and calu could not read the "
                            f"result ({type(exc).__name__}: {exc})."
                        ),
                        background_tasks=self.background_tasks,
                    )
                )
        for event in events:
            await self._emit(event, sink)

    async def _emit(self, event: Event, sink: EventSink | None = None) -> None:
        """Hand one event to the sink; a sink that raises is logged, never propagated.

        This is the runner's half of the sink contract (`calu.claude.runner.EventSink`): the
        stream goes on, the next event is delivered, and the failed event is not delivered
        again. `sink_failures` counts the failures. Each one is logged at level "error". The
        traceback is included for the first three and for every `turn.result`, because a sink
        that fails on the end of a turn can leave its own state behind (a session that still
        reads "running") and whoever looks into that needs the cause; further failures on
        other events are one line each, so that a closed terminal does not fill the log with
        tracebacks during a long run.

        Args:
            event: The event.
            sink: The sink; None uses the one `run` was called with (nothing happens before
                `run`).
        """
        sink = sink or self._sink
        if sink is None:
            return
        try:
            await sink(event)
        except Exception as exc:
            self.sink_failures += 1
            end_of_turn = isinstance(event, TurnResult)
            log.error(
                "the event sink raised on a %s event (%s: %s; failure %d of this session)%s",
                event.method,
                type(exc).__name__,
                exc,
                self.sink_failures,
                ": the end of this turn may not have reached every client" if end_of_turn else "",
                exc_info=end_of_turn or self.sink_failures <= 3,
            )

    async def wait_ready(self) -> None:
        """Return once the SDK client is connected and `send` may be called."""
        await self._ready.wait()

    async def send(self, text: str, priority: Priority, images: Sequence[ChatImage] = ()) -> None:
        """Write a message **of the human user** to Claude's input.

        Precondition: `wait_ready` has returned and `run` is still active.

        Side effect: the text and its images are remembered, so that its replay on the stream
        is reported as `UserInput` with source "user" and these images (`_user_source`).
        Callers therefore pass only text a human client sent (a prompt, a queued or injected
        chat message, a comment); calu's own notes go through `_send_note`. The oldest pending
        texts beyond `MAX_PENDING_HUMAN_TEXTS`, and the images of the oldest pending texts
        beyond `MAX_PENDING_HUMAN_IMAGE_TEXTS`, are forgotten.

        Args:
            text: The message text.
            priority: "next" (read after the running tool calls) or "later" (after the turn).
            images: Images the user attached. They are sent to Claude as image blocks after
                the text and kept until the text's replay, whose `UserInput` carries them.
        """
        self._human_texts.append((text.strip(), list(images)))
        del self._human_texts[:-MAX_PENDING_HUMAN_TEXTS]
        # Bound the memory held for replays that may never come: keep the images of the newest
        # texts only, and drop them from older entries (newest first, so the newest are kept).
        with_images = [i for i, (_, imgs) in enumerate(self._human_texts) if imgs]
        for i in with_images[:-MAX_PENDING_HUMAN_IMAGE_TEXTS]:
            self._human_texts[i] = (self._human_texts[i][0], [])
        await self._write(text, priority, images)

    async def _write(self, text: str, priority: Priority, images: Sequence[ChatImage] = ()) -> None:
        """Write a user-role message to Claude's input without marking it as the user's.

        Args:
            text: The message text.
            priority: "next" (read after the running tool calls) or "later" (after the turn).
            images: Images to send with the text, as image blocks.
        """
        assert self._client is not None, "runner is not running"
        content: str | list[dict[str, Any]] = text
        if images:
            content = [
                {"type": "text", "text": text},
                *(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.media_type,
                            "data": image.data,
                        },
                    }
                    for image in images
                ),
            ]
        message = {
            "type": "user",
            "message": {"role": "user", "content": content},
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

        Pending `can_use_tool` calls are cancelled by the SDK. The turn that ends next is
        reported as "interrupted" (`_translate`).
        """
        if self._client is not None:
            self._interrupting = True
            await self._client.interrupt()

    async def stop(self) -> None:
        """End the session: `run` returns soon after.

        A stop before `run` makes `run` return immediately.
        """
        self._stop_requested = True
        if self._cancel is not None:
            self._cancel.cancel()

    async def side_question(self, question: str, history: Sequence[tuple[str, str]] = ()) -> str:
        """Ask Claude Code a side question (its `/btw`) and return the answer.

        Sent as the control request `side_question` of the CLI's stream-json protocol, which
        Claude Code answers from the conversation so far, without tools, also while a turn
        runs; neither question nor answer enters the conversation (measured with CLI 2.1.319,
        `spikes/side_question.py`). The Agent SDK has no public method for this request, so
        it goes through the SDK's own control channel.

        Claude Code answers with a fork of its own and calls the SubagentStop hook for it.
        That fork is no subagent of the session: its result is not reviewed and no client
        sees it as an agent. It is recognised by its last message, which is the answer. Any
        other agent that was not started with the Agent tool and stopped while this question
        was open is reviewed when the question ends, as it would have been at once otherwise.

        Args:
            question: The question.
            history: Earlier side questions with their answers, oldest first.

        Returns:
            The answer text.

        Raises:
            SideQuestionFailed: If no session runs, or Claude Code answered with an error, with
                nothing, or not within `SIDE_QUESTION_TIMEOUT_S`.
        """
        query: Any = getattr(self._client, "_query", None)
        if query is None:
            raise SideQuestionFailed("no Claude session is running")
        request: dict[str, Any] = {"subtype": "side_question", "question": question}
        if history:
            request["history"] = [{"question": q, "response": a} for q, a in history]
        text: object = None
        self._side_pending += 1
        try:
            answer = await query._send_control_request(request, timeout=SIDE_QUESTION_TIMEOUT_S)
            text = answer.get("response") if isinstance(answer, dict) else None
        except Exception as exc:
            # The SDK raises bare `Exception` for an error response and for a timeout.
            raise SideQuestionFailed(
                f"Claude Code did not answer the side question: {exc}"
            ) from exc
        finally:
            self._side_pending -= 1
            with anyio.CancelScope(shield=True):  # a cancelled question still owes the reviews
                await self._settle_side_stops(text if isinstance(text, str) else None)
        if not isinstance(text, str) or not text.strip():
            raise SideQuestionFailed("Claude Code returned no answer to the side question")
        return text

    async def _settle_side_stops(self, answer: str | None) -> None:
        """Decide about the unknown agents that stopped while a side question was open.

        The one whose last message is `answer` was the fork that answered the question and is
        dropped. When no side question is open any more, the result of every other one is
        reviewed, and the checker's note is sent to the main agent. A failed review is logged:
        it must not replace the side question's own outcome.

        Args:
            answer: The answer of the side question that just ended; None if it had none.
        """
        if answer is not None:
            for index, (last, _) in enumerate(self._side_stops):
                if last.strip() == answer.strip():
                    del self._side_stops[index]
                    break
        if self._side_pending:
            return
        stops, self._side_stops = self._side_stops, []
        for _, review in stops:
            try:
                await review()
            except Exception:
                log.exception("the review of a subagent that stopped beside a side question failed")

    async def context_usage(self) -> ContextUsage | None:
        """Ask Claude Code for the fill of the main agent's context window.

        Returns:
            The numbers, or None if not connected or Claude Code gave no usable answer.
        """
        if self._client is None:
            return None
        try:
            usage: Any = await self._client.get_context_usage()
            return ContextUsage(
                tokens=int(usage["totalTokens"]),
                max_tokens=int(usage["maxTokens"]),
                percent=float(usage["percentage"]),
                model=str(usage.get("model") or ""),
            )
        except Exception:
            # The SDK raises bare `Exception`; a missing figure is not worth more than a log line.
            log.warning("Claude Code did not report the context usage", exc_info=True)
            return None

    async def usage_report(self) -> UsageReport:
        """Read Claude Code's `/usage` text and the plan's limits through two control requests.

        `get_session_cost` answers `{text}`, "the same text /usage prints in non-interactive
        mode"; `get_usage` (marked experimental by Claude Code) answers the session's totals
        and, for a claude.ai plan, `rate_limits` with `utilization` 0-100 and `resets_at` per
        window. Neither sends anything to the model or starts a turn (measured with CLI
        2.1.319, `spikes/spike_usage_control.py`). The SDK has no public method for them, so
        they go through its own control channel, as `side_question` does.

        Returns:
            The report. A request that fails or times out leaves its part empty; a failed
            `get_session_cost` names the reason in `problem`, a failed `get_usage` or an
            answer of unknown shape (`usage_shape_problem`) in `account_problem`.
        """
        query: Any = getattr(self._client, "_query", None)
        if query is None:
            return UsageReport(
                problem="no Claude session is running",
                account_problem="no Claude session is running",
            )
        text, problem = "", ""
        try:
            answer = await query._send_control_request(
                {"subtype": "get_session_cost"}, timeout=USAGE_TIMEOUT_S
            )
            text = str(answer.get("text") or "") if isinstance(answer, dict) else ""
            if not text.strip():
                problem = "Claude Code returned no /usage text"
        except Exception as exc:  # the SDK raises bare `Exception` for an error and a timeout
            problem = f"Claude Code did not answer /usage: {exc}"
        limits: list[RateLimit] = []
        subscription, account_problem = "", ""
        try:
            answer = await query._send_control_request(
                {"subtype": "get_usage", "skip_behaviors": True}, timeout=USAGE_TIMEOUT_S
            )
            account_problem = usage_shape_problem(answer)
            limits, subscription = account_limits(answer)
        except Exception as exc:
            # Experimental on Claude Code's side: a missing answer only leaves the part empty,
            # and the panel says why (D250).
            log.info("Claude Code did not answer get_usage", exc_info=True)
            account_problem = f"Claude Code did not answer get_usage: {exc}"
        return UsageReport(
            text=text,
            problem=problem,
            account_limits=tuple(limits),
            subscription=subscription,
            account_problem=account_problem,
        )

    async def set_model(self, model: str | None) -> None:
        """Change the model of the running session through the SDK.

        Args:
            model: A Claude Code alias or model id; None for the default model.

        Raises:
            ModelChangeFailed: If not connected or Claude Code refused the change.
        """
        if self._client is None:
            raise ModelChangeFailed("no Claude session is running")
        try:
            await self._client.set_model(model)
        except Exception as exc:  # the SDK raises bare `Exception` for an error response
            raise ModelChangeFailed(f"Claude Code did not change the model: {exc}") from exc

    @property
    def permission_mode(self) -> str | None:
        """The main agent's permission mode (see `ClaudeRunner.permission_mode`).

        Returns:
            The mode Claude Code last reported for the main agent (init and status messages,
            hook inputs); before its first report the mode the start should give
            (`_expected_mode`); None while neither is known.
        """
        return self._modes.get(None) or self._expected_mode

    async def set_permission_mode(self, mode: str) -> None:
        """Switch Claude Code's permission mode through the SDK (`set_permission_mode`).

        Measured with CLI 2.1.319 (`spikes/spike_permission_mode_switch.py`): Claude Code
        answers the control request at once, also while a tool call of the running turn waits
        in the hook, then sends a `status` system message with the new `permissionMode`
        (reported as `PermissionModeChanged` by `_events_of`); the next tool call of the same
        turn already runs in the new mode.

        Side effects: the mode becomes the chosen one (`_chosen_mode`), so Claude Code's
        report of it gives no warning; a tool call whose hook answers before that report is
        answered by this mode (`_mode_now`).

        Args:
            mode: The mode to switch to.

        Raises:
            PermissionModeChangeFailed: If not connected or Claude Code refused the mode (for
                instance "bypassPermissions" in a session not started with
                `--dangerously-skip-permissions`).
        """
        if self._client is None:
            raise PermissionModeChangeFailed("no Claude session is running")
        # Set before the request: Claude Code's status report may arrive before its answer,
        # and it must be read as the answer to this switch, not as a mode nobody chose.
        chosen, pending = self._chosen_mode, self._switch_pending
        self._chosen_mode, self._switch_pending = mode, True
        try:
            await self._client.set_permission_mode(mode)  # type: ignore[arg-type]
        except Exception as exc:  # the SDK raises bare `Exception` for an error response
            self._chosen_mode, self._switch_pending = chosen, pending
            raise PermissionModeChangeFailed(
                f"Claude Code did not change the permission mode: {exc}"
            ) from exc
        if self._modes.get(None) != mode:
            self._switched_to = mode  # until Claude Code's own report arrives

    # ------------------------------------------------------------------ options and callbacks

    def _options(
        self,
        gate: ToolGate,
        resume_id: str | None,
        fork: Fork | None = None,
        session_id: str | None = None,
    ) -> ClaudeAgentOptions:
        """Build the SDK options: working directory, model, hooks, callbacks and environment.

        Claude Code authenticates as configured on this machine; only checker keys are hidden
        from it (`_claude_env`). The sandbox settings of `config.sandbox` are passed as a
        `--settings` JSON document, which has precedence over the settings files of the project
        and of the user (agent/records/PLAN.md D15; what was measured of that: `SandboxConfig`).

        Args:
            gate: Used by the PreToolUse hook and by `can_use_tool`.
            resume_id: Claude session id to resume, or None.
            fork: Where to branch off the resumed conversation (agent/records/PLAN.md D36); None
                resumes it whole.
            session_id: The id a new or forked conversation gets (`--session-id`; measured
                with CLI 2.1.321, `spikes/spike_session_id.py`: Claude Code reports exactly
                this id, also for a fork). Not passed for a plain resume, which keeps its id.

        Returns:
            The options for `ClaudeSDKClient`. Side effect: sets `_launch_mode` from
            `calu.preflight.launch_permission_mode` (reads Claude Code's settings files).
        """
        self._launch_mode = launch_permission_mode(self.config, self.home, self.workspace)
        chosen = settings_permission_mode(self.config, self.home, self.workspace)
        self._expected_mode = self._launch_mode or (chosen[0] if chosen else None)
        self._chosen_mode, self._switch_pending, self._switched_to = (
            self._expected_mode,
            False,
            None,
        )

        async def pre_tool_use(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PreToolUse hook: ask `gate.pre_tool_use` and return its verdict to Claude Code.

            For an ASK verdict the step is stored, so that `can_use_tool` reuses it. A PASS verdict
            is answered without a decision where Claude Code's auto mode is in effect when the
            answer goes out (`_mode_now`: a switch that landed during the check counts, D250;
            else the permission mode of the hook input), so that the auto mode classifier
            decides as well (agent/records/PLAN.md D5, D21); where there is no auto mode to defer
            to, PASS becomes "allow" -- or, with `claude.require_auto_mode`, "ask", so that a human
            decides in the classifier's place. ALLOW answers "allow". One case overrides both PASS
            and ALLOW: a shell command (`SHELL_TOOLS`) is answered "ask" while a settings file holds
            a sandbox entry beyond calu's configuration (`_sandbox_blockers`, read again for every
            command), so a human decides whether it runs.

            Side effect: the first mode seen for the calling agent, and every change of it, is
            reported as a `log` event (`_mode_events`).

            Args:
                input_data: The hook input (dict with tool_name, tool_input, agent_id, cwd...).
                tool_use_id: Id of the tool call, if the SDK passes it.
                context: SDK hook context (unused).

            Returns:
                The hook output with `permissionDecision` "allow", "deny" or "ask", or without
                a decision for PASS under auto mode.
            """
            step = self._step_from_hook(input_data, tool_use_id)
            agent = input_data.get("agent_id")
            for event in self._mode_events(input_data.get("permission_mode"), agent):
                await self._emit(event)
            result = await gate.pre_tool_use(step)
            # The check can take minutes; a switch that landed meanwhile decides (D250).
            mode = self._mode_now(agent, input_data.get("permission_mode"))
            if step.tool_use_id and result.verdict is Verdict.ASK:
                self._steps[step.tool_use_id] = step  # taken again by can_use_tool
            output: dict[str, Any] = {"hookEventName": "PreToolUse"}
            if result.context:
                output["additionalContext"] = result.context  # verified: Claude reads it
            if result.verdict in (Verdict.PASS, Verdict.ALLOW) and step.tool_name in SHELL_TOOLS:
                # The settings files can change while a session runs, and Claude Code reads
                # them again. A command runs only while calu is sure of the sandbox.
                blockers = self._sandbox_blockers()
                if blockers:
                    reason = " ".join(blockers)
                    if step.tool_use_id:
                        self._steps[step.tool_use_id] = step
                        self._ask_reasons[step.tool_use_id] = reason
                    output["permissionDecision"] = "ask"
                    output["permissionDecisionReason"] = reason
                    return {"hookSpecificOutput": output}
            decision = hook_decision(result.verdict, mode, self.config.require_auto_mode)
            if decision is None:
                # No decision: Claude Code's auto mode classifier decides as well (checkers AND
                # auto mode). "allow" would skip the classifier (verified, spike_auto_mode.py).
                return {"hookSpecificOutput": output}
            if result.verdict is Verdict.PASS and decision == "ask":
                # The checkers have no objection, but the second approval is missing, and the
                # configuration says a step needs both: a human decides in its place.
                reason = (
                    "calu's checkers passed this step, but auto mode's classifier is not "
                    f"active (Claude Code reports the permission mode {_quoted(mode)}) and "
                    "claude.require_auto_mode is set, so a human decides."
                )
                if step.tool_use_id:
                    self._steps[step.tool_use_id] = step
                    self._ask_reasons[step.tool_use_id] = reason
                output["permissionDecision"] = "ask"
                output["permissionDecisionReason"] = reason
                return {"hookSpecificOutput": output}
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

        async def post_command(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PostToolUse hook for Bash: review what the command wrote (agent/records/PLAN.md D31).

            Claude waits while this runs, so the review may measure the write set, ask a checker
            and even wait for a human; the matcher therefore gets `REVIEW_HOOK_TIMEOUT_S`. A
            note is sent as a priority "next" message, as for the subagent review. The call is a
            no-op when the snapshot is off, so no configuration is needed here.

            Args:
                input_data: Hook input (`tool_name`, `tool_input`, `cwd`).
                tool_use_id: Id of the Bash call.
                context: SDK hook context (unused).

            Returns:
                An empty hook output.
            """
            step = Step(
                tool_name=str(input_data.get("tool_name", "Bash")),
                tool_input=input_data.get("tool_input") or {},
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                cwd=str(input_data.get("cwd") or self.workspace),
            )
            note = await gate.review_write_set(step)
            await self._send_note(note)
            return {}

        async def post_edit(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PostToolUse hook for the edit tools: tell the gate that the edit has run.

            Claude Code calls this hook after a tool call that succeeded, and Claude waits
            while it runs, so the gate has applied the edit's result before the next call is
            judged (`ToolGate.tool_succeeded`).

            Args:
                input_data: Hook input (`tool_name`, `tool_input`, `cwd`).
                tool_use_id: Id of the edit call.
                context: SDK hook context (unused).

            Returns:
                An empty hook output.
            """
            step = Step(
                tool_name=str(input_data.get("tool_name", "Edit")),
                tool_input=input_data.get("tool_input") or {},
                tool_use_id=tool_use_id,
                agent_id=input_data.get("agent_id"),
                cwd=str(input_data.get("cwd") or self.workspace),
            )
            await gate.tool_succeeded(step)
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
            last = str(input_data.get("last_assistant_message") or "")
            if info is None:
                # Not started by the Agent tool -- a Workflow script's `agent()` starts its
                # subagents directly. Their steps pass the gate like any other (verified), and
                # their results are reviewed too; the task comes from their transcript (D157).
                if self._compactions and not input_data.get("agent_type"):
                    # The agent that writes the summary of a compaction (measured with CLI
                    # 2.1.327: PreCompact, then this hook with an agent id of its own and an
                    # empty agent type). It is part of Claude Code's own bookkeeping; a
                    # review would show a subagent nobody started.
                    self._compactions -= 1
                    return {}
                kind = str(input_data.get("agent_type") or "subagent")
                result = SubagentResult(
                    agent_id=agent_id,
                    description=f"{kind}, started without the Agent tool (e.g. by a Workflow)",
                    prompt=first_prompt(input_data.get("agent_transcript_path")),
                    result=last[:SUBAGENT_RESULT_CHARS],
                    background=True,
                )

                async def review() -> None:
                    """Review the result and send the checker's note to the main agent."""
                    await self._send_note(await gate.review_subagent_result(result))

                if self._side_pending and not input_data.get("agent_type"):
                    # Probably the fork that answers a side question (measured with CLI
                    # 2.1.321, `spikes/side_question.py`: this hook, an agent id of its own, an
                    # empty agent type, the answer as last message, before the answer
                    # arrives). `side_question` decides when it has the answer; waiting for
                    # it here would hold up that answer.
                    self._side_stops.append((last, review))
                    return {}
                await review()
                return {}
            if not info.background:
                return {}
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

        async def pre_compact(
            input_data: Any, tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            """PreCompact hook: note that a compaction starts (`/compact` or an automatic one).

            Args:
                input_data: Hook input (`trigger`: "manual" or "auto").
                tool_use_id: Unused (None for this event).
                context: SDK hook context (unused).

            Returns:
                An empty hook output (the compaction goes on).
            """
            self._compactions += 1
            return {}

        async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            """Permission callback for "ask" verdicts: wait for the human via `gate.ask`.

            Uses the step stored by `pre_tool_use` if there is one, else builds it from the
            arguments. Waits without a time limit. A reason the runner itself had for making
            the step wait (`claude.require_auto_mode`, a settings file's sandbox entries) is
            put in front of what Claude Code
            says it is asking about. The user's note on an allowed step is sent to Claude as
            a message of the user.

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
            # What Claude Code says it is asking about. Without this a capability request --
            # `SandboxNetworkAccess` for a command that wants an unlisted domain -- reaches the
            # human as a tool name and an empty input, and says neither the domain nor the
            # command (D60).
            asked = [self._ask_reasons.pop(tool_use_id or "", ""), permission_prompt(ctx)]
            step = step.model_copy(update={"asked_about": " — ".join(a for a in asked if a)})
            answer = await gate.ask(step)
            if answer.allow:
                if answer.note:
                    await self._send_note(
                        f"Note from the user on the step they allowed: {answer.note}",
                        human=True,
                    )
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(message=answer.message or "The user denied this step.")

        return ClaudeAgentOptions(
            cwd=str(self.workspace),
            model=self.config.model,
            effort=self.config.effort,
            cli_path=self._cli_path(),
            # "auto" unless the user chose a mode (calu's setting or a settings file, D241);
            # the effective mode is reported (`_mode_events`).
            permission_mode=self._launch_mode,
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
                    ),
                    HookMatcher(
                        matcher="Bash",
                        hooks=[post_command],  # type: ignore[list-item]
                        timeout=REVIEW_HOOK_TIMEOUT_S,
                    ),
                    HookMatcher(
                        matcher="Edit|Write|MultiEdit|NotebookEdit",
                        hooks=[post_edit],  # type: ignore[list-item]
                        timeout=HOOK_TIMEOUT_S,
                    ),
                ],
                "SubagentStop": [
                    HookMatcher(
                        matcher=None,
                        hooks=[subagent_stop],  # type: ignore[list-item]
                        timeout=REVIEW_HOOK_TIMEOUT_S,
                    )
                ],
                "PreCompact": [
                    HookMatcher(
                        matcher=None,
                        hooks=[pre_compact],  # type: ignore[list-item]
                        timeout=HOOK_TIMEOUT_S,
                    )
                ],
            },
            setting_sources=self.config.setting_sources,  # type: ignore[arg-type]
            # `--settings` as a JSON document: it overrides the project's and the user's
            # settings files (agent/records/PLAN.md D15). The keys it holds: `PINNED_SANDBOX_KEYS`.
            settings=json.dumps(self.config.sandbox.settings()),
            extra_args={
                "replay-user-messages": None,
                "forward-subagent-text": None,
                # Readable thinking summaries (verified: works without setting a thinking mode).
                "thinking-display": self.config.thinking_display,
            },
            include_partial_messages=self.config.stream_partial,
            resume=resume_id,
            session_id=session_id if resume_id is None or fork is not None else None,
            # A fork runs under a new session id, so the original stays intact
            # (agent/records/PLAN.md D36). With a fork point it keeps the conversation up to
            # `resume_at` and discards the turn that starts at `drops_turn`; the CLI validates the
            # pair and refuses a mismatch. Without one it keeps the whole conversation.
            fork_session=fork is not None,
            resume_session_at=fork.resume_at if fork else None,
            resume_drops_turn=fork.drops_turn if fork else None,
            env=self._claude_env(),
            stderr=lambda line: log.debug("claude stderr: %s", line),
        )

    async def _send_note(self, note: str, human: bool = False) -> None:
        """Send `note` as a priority "next" message; empty notes and send errors are ignored.

        Used from hooks, which may still run after the session was stopped. A note is calu's
        own text (a checker's remark on a subagent result or on a write set, which may
        paraphrase what an untrusted tool result said), so its replay on the stream is **not**
        reported as the user's words unless `human` is set.

        Args:
            note: The text, or "".
            human: True only for text a human wrote (the note of whoever resolved an
                escalation); its replay then counts as user input.
        """
        if not note or self._client is None:
            return
        try:
            if human:
                await self.send(note, "next")
            else:
                await self._write(note, "next")
        except Exception:
            log.exception("sending a note to Claude failed")

    def _user_source(
        self, text: str, origin: Any = None
    ) -> tuple[Literal["user", "cross_session", "system"], list[ChatImage]]:
        """Decide whether a user-role text on the stream is the human user's.

        The checkers accept only the user's messages as authorization, so the default is "not
        the user". A text is the user's only if both hold:

        - Claude Code does not attribute it to another origin (`UserMessage.origin` is None or
          of kind "human"; task notifications, channel and peer messages and every unknown
          kind are not human), and
        - the session sent exactly this text (ignoring surrounding white space) through `send`
          and its replay has not been seen yet. Each sent text is matched once; of several
          pending sends of the same text, the oldest is matched first.

        Side effect: a matched text is removed from the pending texts.

        Args:
            text: The text as Claude Code reports it.
            origin: The message's `origin`, or None.

        Returns:
            (source, images). source is "user" for the user's text (or "cross_session" where it
            carries the marker of a message from another Claude session, `user_message_source`),
            else `NOT_FROM_USER`. images are those `send` kept for the matched text (empty when
            none were sent or they were dropped); always empty when the text is not matched.

        Example:
            After `send("Fix it.", "later")`, `_user_source("Fix it.")` is ("user", []) and a
            second `_user_source("Fix it.")` is ("system", []).

        Measured with CLI 2.1.319 (`tests/unit/test_runner_live.py`): a prompt, a message
        queued for the next turn and a message injected into a running turn are each replayed
        once, with the text unchanged and no other origin, so all three are recognised.
        """
        if origin is not None and not (isinstance(origin, dict) and origin.get("kind") == "human"):
            return NOT_FROM_USER, []
        stripped = text.strip()
        for i, (pending, images) in enumerate(self._human_texts):
            if pending == stripped:
                del self._human_texts[i]
                return user_message_source(text), images
        return NOT_FROM_USER, []

    def _mode_events(self, mode: Any, sdk_agent_id: Any = None) -> list[Event]:
        """Report the permission mode of an agent when it is first seen and when it changes.

        Outside the auto mode a step the checkers pass runs without the classifier's second approval
        (agent/records/PLAN.md D21). A mode the user chose is reported as information
        (agent/records/PLAN.md D241). One case is a warning: the mode calu last set or the user
        chose (`_chosen_mode`: the start's mode, then each switch) is auto, and the main agent
        reports another mode at its first sight or in answer to that switch, which Claude Code
        does without a word for a model that has no auto mode. A switch the user made to
        another mode gives no warning (D250). A tool call (`EnterPlanMode`) can also change the
        mode during a session; that is information.

        Args:
            mode: `permission_mode` of a hook input or `permissionMode` of the init or a
                status message; anything that is not a non-empty string is ignored.
            sdk_agent_id: The SDK's id of the agent the mode belongs to; None for the main
                agent.

        Returns:
            One `Log` (level "warning" in the fallback case above, else "info") if the mode is
            new for that agent, else []; for the main agent a `PermissionModeChanged` (with
            `client` None) before it, which the session passes on to its clients. A subagent
            whose first mode is the one the main agent runs in gives no event: the line about
            the main agent already says it, and a run with many subagents would repeat it for
            each.
        """
        if not isinstance(mode, str) or not mode:
            return []
        agent = sdk_agent_id if isinstance(sdk_agent_id, str) and sdk_agent_id else None
        answers_switch = False
        if agent is None:
            # Claude Code's report of the main agent's mode answers a switch under way.
            answers_switch, self._switch_pending, self._switched_to = (
                self._switch_pending,
                False,
                None,
            )
        if self._modes.get(agent) == mode:
            return []
        first = agent not in self._modes
        self._modes[agent] = mode
        if agent is not None and first and self._modes.get(None) == mode:
            return []
        if agent is None:
            return [
                PermissionModeChanged(mode=mode),
                *self._mode_logs(mode, agent, first or answers_switch),
            ]
        return self._mode_logs(mode, agent, first)

    def _mode_now(self, sdk_agent_id: object, hook_mode: object) -> str | None:
        """Return the permission mode an agent is in when the hook answers (D250).

        A check can take minutes, and the user may switch the mode meanwhile. For the main
        agent the answer follows the switch: the mode of a `set_permission_mode` that Claude
        Code accepted and has not reported yet, else the mode Claude Code last reported. A
        subagent's mode is known only from its own hook inputs, so for a subagent it is the
        mode of the hook input. A hook input that names no mode cannot show that the
        classifier is active: without a switch under way the answer is then None.

        Args:
            sdk_agent_id: The SDK's id of the calling agent; None or "" for the main agent.
            hook_mode: `permission_mode` of the hook input.

        Returns:
            The mode, or None if nothing is known.
        """
        hook = hook_mode if isinstance(hook_mode, str) and hook_mode else None
        if isinstance(sdk_agent_id, str) and sdk_agent_id:
            return hook
        if self._switched_to is not None:
            return self._switched_to
        return (self._modes.get(None) or hook) if hook is not None else None

    def _mode_logs(self, mode: str, agent: str | None, first: bool) -> list[Event]:
        """Say in a `log` event what an agent's newly seen permission mode means for a step.

        Args:
            mode: The mode, a non-empty string.
            agent: The SDK's id of the agent; None for the main agent.
            first: Whether this is the first mode seen for that agent, or the main agent's
                report in answer to a switch.

        Returns:
            One `Log`: a warning when the mode calu last set or the user chose is "auto" and
            the main agent reports another mode at its first sight or in answer to the
            switch, else information.
        """
        who = f"The subagent {agent}" if agent else "Claude Code"
        if mode == "auto":
            return [
                Log(
                    message=(
                        f'{who} runs in the permission mode "auto": a step needs calu\'s '
                        "checkers and auto mode's classifier."
                    )
                )
            ]
        consequence = (
            "a step the checkers pass waits for a human (claude.require_auto_mode)"
            if self.config.require_auto_mode
            else "a step the checkers pass runs on their approval alone"
        )
        if agent is None and first and self._chosen_mode == "auto":
            return [
                Log(
                    level="warning",
                    message=(
                        f'{who} was asked for the permission mode "auto" but runs in "{mode}" '
                        "(the model may have no auto mode): auto mode's classifier is off, so "
                        f"{consequence}."
                    ),
                )
            ]
        return [
            Log(
                message=(
                    f'{who} runs in the permission mode "{mode}": auto mode\'s classifier is '
                    f"off, so {consequence}."
                ),
            )
        ]

    def _claude_env(self) -> dict[str, str]:
        """Return the environment overrides for the Claude Code process.

        Checker keys (`hidden_env`) are blanked so that Claude cannot read them.
        `calu.paths.INSIDE_SESSION_ENV` is set to "1", which tells calu's own command line that
        it was started from inside a session. With
        `claude.use_bedrock`, `CLAUDE_CODE_USE_BEDROCK` is set to "1". Otherwise Claude Code's own
        authentication settings (claude.ai login, Bedrock, Vertex, Foundry, API key) are left as
        they are configured on this machine.

        With `run_as`, the launch record for calu's launcher is added (`CALU_LAUNCH`); the
        launcher drops every variable that Claude Code does not need, and the record itself.

        Returns:
            Variable name -> value ("" means unset for Claude Code).

        Raises:
            LaunchError: With `run_as`, if no Claude Code executable is found.
        """
        env = dict.fromkeys(sorted(self.hidden_env), "")
        env[INSIDE_SESSION_ENV] = "1"
        if self.config.use_bedrock:
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if self.run_as is not None:
            env[LAUNCH_ENV] = self.run_as.spec(claude_path(self.config.cli_path)).dump()
        return env

    def _cli_path(self) -> str | None:
        """Return the executable the Agent SDK starts.

        Returns:
            calu's launcher when Claude Code runs as another user (`run_as`); otherwise
            `claude.cli_path`, where None lets the SDK find Claude Code.

        Raises:
            LaunchError: With `run_as`, if this installation has no launcher.
        """
        if self.run_as is not None:
            return str(launcher_path())
        return self.config.cli_path

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

        Side effects: updates `agents`, the SDK-id mapping and `session_id`; consumes a
        pending human text when its replay arrives (`_user_source`).

        A user-role text of the main agent becomes `UserInput` with source "user" only if the
        session sent it for a human; every other one (calu's own notes, task notifications,
        hook feedback) gets `NOT_FROM_USER`, which the session records narrated and never as
        the user's words.

        Args:
            msg: A message from `ClaudeSDKClient.receive_messages`; unknown types are ignored.

        Returns:
            The events in the order they should be emitted.

        """
        events: list[Event] = []
        match msg:
            case AssistantMessage():
                agent = msg.parent_tool_use_id
                command = (
                    self._local_command() if agent is None and msg.model == SYNTHETIC_MODEL else []
                )
                events += command  # the input of `/cost`, `/context`, ... before its output
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        message_id = getattr(msg, "message_id", None)
                        events.append(
                            AssistantText(
                                agent_id=agent,
                                text=command_output(block.text) if command else block.text,
                                message_id=message_id,
                                uuid=str(getattr(msg, "uuid", "") or ""),
                            )
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
                            # The model wrote these fields and nothing has validated them
                            # yet, so each is coerced to the type the record needs.
                            kind = block.input.get("subagent_type")
                            self._agent_prompts[block.id] = str(block.input.get("prompt", ""))
                            self.agents[block.id] = AgentInfo(
                                agent_id=block.id,
                                description=str(block.input.get("description", "")),
                                subagent_type=kind if isinstance(kind, str) else None,
                                background=bool(block.input.get("run_in_background", False)),
                            )
                            events.append(self._agents_update())
            case UserMessage():
                agent = msg.parent_tool_use_id
                origin = getattr(msg, "origin", None)
                if isinstance(msg.content, str):
                    stdout = LOCAL_COMMAND_STDOUT.fullmatch(msg.content.strip())
                    if self._interrupt_text(msg.content):
                        pass
                    elif agent is None and stdout is not None:
                        # What a command of Claude Code printed ("Compacted"): a line of the
                        # program, neither the user's words nor a message to narrate.
                        events += self._local_command()
                        if stdout.group(1).strip():
                            events.append(
                                Log(level="info", message=f"Claude Code: {stdout.group(1).strip()}")
                            )
                    elif agent is None and not self._skill_text(msg.content, events):
                        source, images = self._user_source(msg.content, origin)
                        events.append(
                            UserInput(
                                text=msg.content,
                                source=source,
                                images=images if source == "user" else [],
                                # Claude Code replays the prompt with its transcript uuid
                                # (`replay-user-messages`), which is the fork point of the turn
                                # that starts here (D36).
                                uuid=str(getattr(msg, "uuid", "") or ""),
                            )
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
                        elif isinstance(block, TextBlock) and self._interrupt_text(block.text):
                            pass
                        elif (
                            isinstance(block, TextBlock)
                            and agent is None
                            and not self._skill_text(block.text, events)
                        ):
                            # The images are what the user attached to this text (`send`);
                            # only the user's own message can have any.
                            source, images = self._user_source(block.text, origin)
                            events.append(
                                UserInput(
                                    text=block.text,
                                    source=source,
                                    images=images if source == "user" else [],
                                )
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
                self._compactions = 0
                if msg.num_turns == 0:
                    events += self._local_command()  # a command without any output
                interrupted = self._interrupting and msg.subtype == "error_during_execution"
                self._interrupting = False
                events.append(
                    TurnResult(
                        session_id=msg.session_id,
                        is_error=msg.is_error and not interrupted,
                        subtype="interrupted" if interrupted else msg.subtype,
                        result=msg.result or "",
                        cost_usd=msg.total_cost_usd,
                        num_turns=msg.num_turns,
                        background_tasks=self.background_tasks,
                    )
                )
            case RateLimitEvent():
                info = msg.rate_limit_info
                kind = info.rate_limit_type or ""
                self.rate_limits[kind] = RateLimit(
                    type=kind,
                    status=info.status,
                    utilization=info.utilization,
                    resets_at=(
                        datetime.fromtimestamp(info.resets_at, UTC) if info.resets_at else None
                    ),
                    reported_at=datetime.now(UTC),
                )
                events.append(UsageUpdate(rate_limits=list(self.rate_limits.values())))
            case SystemMessage() if msg.subtype == "background_tasks_changed":
                tasks = msg.data.get("tasks")
                self.background_tasks = len(tasks) if isinstance(tasks, list) else 0
            case SystemMessage() if msg.subtype == "status":
                if msg.data.get("status") == "compacting":
                    events += self._local_command()
                    events.append(Log(level="info", message=COMPACTING_NOTICE))
                # Claude Code sends a status message with the new mode after every change of
                # it (`set_permission_mode`, measured with CLI 2.1.319).
                events += self._mode_events(msg.data.get("permissionMode"))
            case SystemMessage() if msg.subtype == "init":
                self.session_id = msg.data.get("session_id", self.session_id)
                events += self._unknown_tools_warning(msg.data.get("tools"))
                events += self._mode_events(msg.data.get("permissionMode"))
            case _ if type(msg).__name__ == "ConversationResetMessage":
                # `/clear` (the class exists only in newer SDKs, hence the name).
                events += self._local_command()
                events.append(Log(level="warning", message=CONVERSATION_CLEARED_NOTICE))
            case _:
                pass
        return events

    def _interrupt_text(self, text: str) -> bool:
        """Tell whether a user-role text is Claude Code's own line about the user's interrupt.

        Args:
            text: The text as Claude Code reports it.

        Returns:
            True if an interrupt was requested (`interrupt`) and the text is `INTERRUPTED_TEXT`;
            such a text is no message of anybody and gives no event.
        """
        return self._interrupting and INTERRUPTED_TEXT.fullmatch(text.strip()) is not None

    def _local_command(self) -> list[Event]:
        """Return the user's input for a slash command that Claude Code ran by itself.

        Claude Code replays a prompt it gives to the model, and `_user_source` recognises the
        replay. A command it runs itself (`/cost`, `/context`, `/compact`, `/clear`) is not
        replayed (measured with CLI 2.1.327): only the command's effect arrives. Without an
        input event the clients never learn that the message was taken in, and it stays in
        the session's queue. The callers are the places where such an effect arrives.

        Side effect: removes the returned text from the pending human texts.

        Returns:
            One `UserInput` (source "user") for the oldest text that was sent for a human, is
            not yet replayed and has the form of a slash command (`SLASH_COMMAND`); [] if
            there is none.
        """
        for i, (text, _images) in enumerate(self._human_texts):
            if SLASH_COMMAND.match(text):
                del self._human_texts[i]
                return [UserInput(text=text, source="user")]
        return []

    def _unknown_tools_warning(self, tools: Any) -> list[Event]:
        """Name, once per runner, the tools of this Claude Code that calu has no route for (D154).

        Args:
            tools: The `tools` list of the CLI's init message (anything else is ignored).

        Returns:
            One warning `Log` the first time unknown tools are seen, else [].
        """
        if self._tools_reported or not isinstance(tools, list):
            return []
        self._tools_reported = True
        unknown = unknown_tools([str(t) for t in tools], self.tool_routes)
        if not unknown:
            return []
        return [
            Log(
                level="warning",
                message=(
                    f"This Claude Code offers tools calu has no route for: {', '.join(unknown)}. "
                    "Each call of them goes to the default route (the command checker unless "
                    "`default_route` says otherwise). To make one always ask, run "
                    '`calu config set tool_routes \'{"NAME": "escalate"}\'`.'
                ),
            )
        ]

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


def _quoted(mode: Any) -> str:
    """Return a permission mode for a message.

    Args:
        mode: The mode a hook input reports, or anything else when it reports none.

    Returns:
        The mode in double quotes, or "none" (unquoted) when it is not a non-empty string.
    """
    return f'"{mode}"' if isinstance(mode, str) and mode else "none"


def first_prompt(transcript_path: Any, limit: int = 20_000) -> str:
    """Return the first user message of a subagent's transcript: the task it was given (D157).

    A subagent the Agent tool started has its task in that tool call; one a Workflow script
    started does not, but Claude Code's SubagentStop hook names its transcript, whose first
    `user` line holds the prompt.

    Args:
        transcript_path: `agent_transcript_path` of the hook input; anything that is not an
            existing file gives "".
        limit: Most characters returned.

    Returns:
        The prompt text, or "" when it cannot be read.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "user":
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str):
                    return content[:limit]
                if isinstance(content, list):
                    texts = [
                        str(block.get("text", ""))
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    if texts:
                        return "\n".join(texts)[:limit]
    except OSError:
        return ""
    return ""
