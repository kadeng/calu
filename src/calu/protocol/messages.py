"""Messages of the calu client protocol between a session and its clients.

Transport: one JSON object per line (NDJSON) on the core's stdin (front end -> core) and stdout
(core -> front end), following JSON-RPC 2.0:

    FE -> core   {"jsonrpc": "2.0", "id": 1, "method": "chat.send", "params": {...}}
    core -> FE   {"jsonrpc": "2.0", "id": 1, "result": {...}}            (or "error")
    core -> FE   {"jsonrpc": "2.0", "method": "tool.use", "params": {...}} (event, no id)

Requests are the `*Params` models in `REQUESTS`; events are the models in `EVENTS`, each with a
`method` class variable. `agent_id` is `None` for the main agent; for a subagent it is the id of
the Agent tool call that started it.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from calu.checkers.model import Decision
from calu.checkers.policy import Route
from calu.comments.model import Comment

PROTOCOL_VERSION = 1

# --------------------------------------------------------------------------- requests (FE -> core)


class InitializeParams(BaseModel):
    """First request of a front end. The result is an `InitializeResult`."""

    client: str
    client_version: str = ""
    training: bool = False
    """Start with the human-in-the-loop training mode on (see `training.set`)."""
    training_timeout_s: float = Field(default=120, gt=0)
    """Review timeout of the training mode (see `training.set`)."""
    auto_escalate: AutoEscalate | None = None
    """Auto-escalation settings of the front end; None keeps those of config.toml."""


class InitializeResult(BaseModel):
    """Result of `initialize`: protocol and core versions, workspace and project file paths."""

    protocol_version: int = PROTOCOL_VERSION
    core_version: str
    workspace: str
    files: ProjectFiles


class SessionStartParams(BaseModel):
    """Start a Claude session, or resume the session `resume_id`. Result: `{}`.

    `model` and `effort` replace the configured `claude.model` / `claude.effort` for this and
    later sessions of the core process; None keeps the configured value. `model` is a Claude
    Code alias ("fable", "opus", "sonnet", "haiku": the latest version) or a model id. `effort`
    "default" switches back to Claude Code's default effort.
    """

    resume_id: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] | None = None


class EmptyParams(BaseModel):
    """Parameters of requests without arguments (`session.stop`, `interrupt`, `shutdown`)."""


class ChatSendParams(BaseModel):
    """A user chat message.

    `delivery`: "queued" waits until Claude's current turn ends; "inject" is read as soon as the
    running tool calls finish. `to`: "main" or the `agent_id` of a running background subagent;
    a message to a subagent is relayed by the main agent with its SendMessage tool.
    """

    text: str = Field(min_length=1)
    delivery: Literal["queued", "inject"] = "queued"
    to: str = "main"


class CommentAddParams(BaseModel):
    """A comment on a selection; the core computes the `Comment` (result: `CommentAddResult`).

    For `kind == "file"`: `side` must be "after" and `before_text` is omitted. `after_text` is
    the full current document. For `kind == "diff"`: both full texts are given and `side` names
    the diff side the selection was made on. Lines are 1-based and inclusive.
    """

    kind: Literal["diff", "file"]
    path: str
    side: Literal["before", "after"] = "after"
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    after_text: str
    before_text: str | None = None
    ref: str | None = None
    text: str = Field(min_length=1)


class CommentAddResult(BaseModel):
    """Result of `comment.add`: the comment as computed by the core and sent to Claude."""

    comment: Comment


class EscalationDecideParams(BaseModel):
    """The human's answer to an `escalation.request`. Result: `{}`."""

    escalation_id: str
    decision: Literal["allow", "deny"]
    note: str | None = None


class AutoEscalate(BaseModel):
    """Categories whose steps always wait for the human, whatever the checker decided.

    `commands`: Bash and other tool calls reviewed by the command checker. `edits`: file edits.
    `subagents`: final results of subagents. The checker still runs; where it would have
    approved (ALLOW / ACCEPT / ANNOTATE), the step is escalated instead, with the checker's
    decision and reason in the escalation. A checker DENY stays a DENY. Read-only tools that the
    policy allows without a check are not affected. With training mode on as well, the training
    review (labelling the checker's decision) comes first, then the escalation (running the step).
    """

    commands: bool = False
    edits: bool = False
    subagents: bool = False


class AutoEscalateSetParams(AutoEscalate):
    """`auto_escalate.set`: replace the auto-escalation settings of the session. Result: `{}`."""


TrainingChecker = Literal["command", "edit", "subagent"]


class TrainingResponse(BaseModel):
    """A checker answer in training mode: the model's, or the human's corrected version.

    `decision` is one of the request's `options` (ALLOW/DENY/ESCALATE for the command and edit
    checkers; ACCEPT/ANNOTATE/ESCALATE for the subagent result checker). For the subagent result
    checker, `reason` holds the triage note and `comment` is unused.
    """

    decision: str
    reason: str = ""
    summary: str = ""
    comment: str = ""


class TrainingSetParams(BaseModel):
    """`training.set`: switch the human-in-the-loop training mode on or off. Result: `{}`.

    While it is on, every checker decision is sent as a `training.request` and waits for
    `training.decide`, at most `timeout_s` seconds; after that the checker's own decision is
    used and nothing is recorded.
    """

    enabled: bool
    timeout_s: float = Field(default=120, gt=0)


class TrainingDecideParams(BaseModel):
    """The human's answer to a `training.request`. Result: `{}`.

    `response` replaces the checker's decision (send the model response unchanged to accept
    it). `insufficient_information` marks that the checker input did not allow a sound decision.
    """

    training_id: str
    response: TrainingResponse
    insufficient_information: bool = False


class RecordedEvent(BaseModel):
    """An event as stored: the JSON-RPC method, its params and the time it was emitted."""

    method: str
    params: dict[str, Any]
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionSummary(BaseModel):
    """A stored session, for a "resume session" list in a front end."""

    session_id: str
    started_at: datetime
    last_event_at: datetime
    first_message: str
    """The first user input of the session, shortened to 100 characters ("" if none)."""
    events: int


class SessionListResult(BaseModel):
    """Result of `session.list`: stored sessions, most recently active first."""

    sessions: list[SessionSummary]


class SessionHistoryParams(BaseModel):
    """Parameters of `session.history`: which stored session, and how many events at most.

    The result is a `SessionHistoryResult`. A front end uses it to rebuild its view, e.g.
    before or after resuming the session with `session.start {resume_id}`.
    """

    session_id: str
    limit: int = Field(default=2_000, ge=1, le=20_000)


class SessionHistoryResult(BaseModel):
    """Result of `session.history`: events oldest first, as `(method, params)` with a time."""

    events: list[RecordedEvent]


class CheckerContextResult(BaseModel):
    """Result of `context.get`: the conversation context as the checkers see it.

    Leaves out what does not change during a conversation: the checker instructions and the
    project files (AGENTS.md, CLAUDE.md, ...). A checker additionally sees the step it judges.
    """

    chat_summary: str
    """CHAT_SUMMARY.md: the turns folded so far ("" if the file does not exist)."""
    recent: str
    """The unfolded turns and the running turn, rendered as for a check: the user's messages,
    one line per step with its summary, narrations of Claude's thinking and text."""


REQUESTS: dict[str, type[BaseModel]] = {
    "initialize": InitializeParams,
    "session.start": SessionStartParams,
    "session.stop": EmptyParams,
    "session.list": EmptyParams,
    "session.history": SessionHistoryParams,
    "chat.send": ChatSendParams,
    "comment.add": CommentAddParams,
    "escalation.decide": EscalationDecideParams,
    "training.set": TrainingSetParams,
    "auto_escalate.set": AutoEscalateSetParams,
    "training.decide": TrainingDecideParams,
    "interrupt": EmptyParams,
    "context.get": EmptyParams,
    "shutdown": EmptyParams,
}

# ---------------------------------------------------------------------------- events (core -> FE)


class Event(BaseModel):
    """Base class of all events. `method` is the JSON-RPC method name of the notification."""

    method: ClassVar[str]


class ProjectFiles(Event):
    """Absolute paths of the editable project files, for links in the front end."""

    method: ClassVar[str] = "files"
    command_instructions: str
    edit_instructions: str
    chat_summary: str
    comments: str
    subagent_instructions: str = ""
    narrator_instructions: str = ""
    command_allow: str = ""
    """The rules files (`calu.checkers.rules`); "" from cores without rules."""
    command_deny: str = ""
    read_allow: str = ""
    read_deny: str = ""
    write_allow: str = ""
    write_deny: str = ""


State = Literal[
    "starting", "idle", "running", "checking", "tool_call", "escalated", "stopped", "failed"
]
"""States of a session (PLAN.md §3.1):

    starting ─► idle ◄──► running ─► checking ─► tool_call ─► running
                            │           │
                            │           └─► escalated ─(resolved)─► tool_call | running
                            └─► stopped | failed

`running`: Claude works on a turn. `checking`: a checker or the narrator runs. `tool_call`: an
approved tool call runs. `escalated`: at least one escalation waits for a human. `failed`: the
Claude session ended with an error. Steps of the main agent and of subagents overlap, so the
state is the most important of them, in the order escalated > checking > tool_call > running."""


class SessionState(Event):
    """The Claude session changed state (see `State`)."""

    method: ClassVar[str] = "session.state"
    state: State
    session_id: str | None = None


class AssistantText(Event):
    """A completed block of text written by Claude (main agent or subagent).

    If `assistant.delta` events with the same `message_id` came before, this text replaces
    their concatenation.
    """

    method: ClassVar[str] = "assistant.text"
    agent_id: str | None = None
    text: str
    message_id: str | None = None


class AssistantDelta(Event):
    """A fragment of assistant text while it is generated (token streaming).

    Only sent when `claude.stream_partial` is enabled. Fragments with the same `message_id` are
    concatenated; the final `assistant.text` with that id replaces them. Front ends may ignore
    this event.
    """

    method: ClassVar[str] = "assistant.delta"
    agent_id: str | None = None
    message_id: str
    text: str
    thinking: bool = False
    """True for a fragment of thinking text (completed by `assistant.thinking`)."""


class AssistantThinking(Event):
    """A completed block of Claude's thinking (a summary of its reasoning).

    Only sent when Claude Code returns readable thinking (`claude.thinking_display` is
    "summarized" and the model thought). Front ends may show it de-emphasized, or hide it.
    """

    method: ClassVar[str] = "assistant.thinking"
    agent_id: str | None = None
    text: str
    message_id: str | None = None


class ToolUse(Event):
    """Claude requested a tool call (before any check)."""

    method: ClassVar[str] = "tool.use"
    agent_id: str | None = None
    tool_use_id: str
    name: str
    input: dict[str, Any]


class ToolResult(Event):
    """Result of a tool call. `content` is a text rendering, possibly truncated."""

    method: ClassVar[str] = "tool.result"
    agent_id: str | None = None
    tool_use_id: str
    is_error: bool = False
    content: str


class QueuedMessage(BaseModel):
    """A chat message that was sent to Claude but that Claude has not taken in yet.

    `text` is the user's text (for a subagent, without the relay request around it).
    """

    id: str
    text: str
    delivery: Literal["queued", "inject"]
    to: str = "main"


class ChatQueue(Event):
    """The chat messages waiting to be taken in by Claude, oldest first (the whole list).

    Sent whenever the list changes: after `chat.send`, when Claude takes a message in (its
    `user.input` arrives), and when the session ends (empty list). Not recorded in the history.
    """

    method: ClassVar[str] = "chat.queue"
    messages: list[QueuedMessage]


class FileLink(BaseModel):
    """A file that a tool call refers to.

    `raw` is the text as it appears in the tool input or label (e.g. a command argument), `path`
    the absolute path of the existing file.
    """

    raw: str
    path: str


class ToolFiles(Event):
    """Files that a tool call refers to, for links (`calu.files`).

    Sent after `tool.use` for a Bash command that names existing text files, and after a skill
    loaded (its SKILL.md). Front ends link each `raw` occurrence in the tool's label to `path`.
    A later `tool.files` for the same call replaces the earlier list.
    """

    method: ClassVar[str] = "tool.files"
    agent_id: str | None = None
    tool_use_id: str
    files: list[FileLink]


class UserInput(Event):
    """A user message that Claude has taken in (chat message, comment, relayed message).

    `source` "cross_session": the message came from another Claude session on the same machine
    (`calu.context.foreign`), not from the user; front ends show it as such, and the
    checkers see it only narrated, never as the user's words.
    """

    method: ClassVar[str] = "user.input"
    text: str
    source: Literal["user", "cross_session"] = "user"


class TurnResult(Event):
    """Claude finished a turn."""

    method: ClassVar[str] = "turn.result"
    session_id: str | None = None
    is_error: bool = False
    subtype: str = "success"
    result: str = ""
    cost_usd: float | None = None
    num_turns: int = 0


class AgentInfo(BaseModel):
    """A subagent. `agent_id` is the id of the Agent tool call that started it."""

    agent_id: str
    sdk_agent_id: str | None = None
    description: str = ""
    subagent_type: str | None = None
    background: bool = False
    status: Literal["running", "completed", "failed", "stopped"] = "running"


class AgentsUpdate(Event):
    """The complete list of subagents of this session, sent whenever it changes."""

    method: ClassVar[str] = "agents.update"
    agents: list[AgentInfo]


class CheckResult(Event):
    """The routing and (if checked) the checker decision for one tool call."""

    method: ClassVar[str] = "check.result"
    tool_use_id: str | None
    agent_id: str | None = None
    tool_name: str
    route: Route
    decision: Decision
    reason: str = ""
    step_summary: str = ""
    comment: str = ""
    """A remark of the checker to Claude on an allowed step; front ends show it in the chat."""
    git_head: str | None = None
    """Commit id of the workspace's git HEAD when the step was checked (None outside git)."""


class EscalationRequest(Event):
    """A tool call waits for the human's decision (`escalation.decide`)."""

    method: ClassVar[str] = "escalation.request"
    escalation_id: str
    tool_use_id: str | None
    agent_id: str | None = None
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    rendered: str
    """Markdown rendering of the step, as the checker saw it."""


class EscalationResolved(Event):
    """An escalation ended, by a human decision or by cancellation.

    "cancelled": withdrawn without a human decision (e.g. interrupt); the tool call did not run.
    """

    method: ClassVar[str] = "escalation.resolved"
    escalation_id: str
    decision: Literal["allow", "deny", "cancelled"]
    note: str | None = None


class EditDiff(Event):
    """Before/after content of a file edit, for a diff view.

    Sent when the edit is checked, which may be before the matching `tool.use` event arrives.
    """

    method: ClassVar[str] = "edit.diff"
    tool_use_id: str | None
    agent_id: str | None = None
    path: str
    before: str
    after: str
    start_line: int | None = None
    """First line (1-based) of the changed region in `after`; None if nothing changed."""
    end_line: int | None = None
    """Last changed line (inclusive) in `after`; `start_line - 1` for a pure deletion."""


class Log(Event):
    """A diagnostic message for the front end's log (not for the chat)."""

    method: ClassVar[str] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str


class TrainingRequest(Event):
    """Training mode: a checker decided; the human reviews the decision (`training.decide`).

    `input` holds the complete checker input without the checker's instructions: the project
    context, the recent steps and the step or result to check, as separate texts.
    """

    method: ClassVar[str] = "training.request"
    training_id: str
    checker: TrainingChecker
    agent_id: str | None = None
    tool_name: str
    tool_input: dict[str, Any]
    input: list[str]
    options: list[str]
    response: TrainingResponse


class TrainingResolved(Event):
    """A training request ended.

    "recorded": the human answered. "cancelled" (stop, interrupt, mode switched off) and
    "timeout" (no answer within the timeout): the checker's own decision was used and nothing
    was recorded.
    """

    method: ClassVar[str] = "training.resolved"
    training_id: str
    outcome: Literal["recorded", "cancelled", "timeout"]


EVENTS: list[type[Event]] = [
    ProjectFiles,
    SessionState,
    AssistantText,
    AssistantDelta,
    AssistantThinking,
    ToolUse,
    ToolResult,
    ToolFiles,
    ChatQueue,
    UserInput,
    TurnResult,
    AgentsUpdate,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    EditDiff,
    TrainingRequest,
    TrainingResolved,
    Log,
]

InitializeResult.model_rebuild()
