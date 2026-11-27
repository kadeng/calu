"""Messages of the calu client protocol between a session and its clients.

See agent/records/PLAN.md §6, D2.

Transport: one JSON object per WebSocket message, following JSON-RPC 2.0:

    client -> calu   {"jsonrpc": "2.0", "id": 1, "method": "chat.send", "params": {...}}
    calu -> client   {"jsonrpc": "2.0", "id": 1, "result": {...}}            (or "error")
    calu -> client   {"jsonrpc": "2.0", "method": "tool.use", "params": {...}} (event, no id)

Requests are the `*Params` models in `REQUESTS`; events are the models in `EVENTS`, each with a
`method` class variable. Several clients watch the same session, so every event carries a
sequence number (`Event.seq`, counted from 1 per calu process): a client that reconnects asks
for the events after the last number it saw (`events.subscribe`) and gets the current state with
`state.get`. `agent_id` is `None` for the main agent; for a subagent it is the id of the Agent
tool call that started it.

A client proves with `initialize` that it may talk to the session (the token that `calu run`
prints). The configuration, the rules and the checker instructions are not part of this
protocol: they live in the database and are changed with calu's own commands, which need other
credentials than a session (agent/records/PLAN.md D6, §5).
"""

import base64
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from calu.checkers.model import Decision
from calu.checkers.policy import Route
from calu.comments.model import Comment
from calu.config import AutoEscalate, PermissionMode
from calu.context.turns import TurnRecord

__all__ = ["AutoEscalate"]  # defined in `calu.config`, which Config embeds it in; re-exported
"""Names this module re-exports, so that a client imports the whole protocol from here."""

PROTOCOL_VERSION = 1

CLOSE_TOKEN_REVOKED = 4001
"""WebSocket close code: the session closed the connection because the client's token was
revoked (`calu token revoke`), or because it is a minted token of a session that has ended while
the process runs the next one. Reconnecting with the same token is refused; the close reason
says which of the two it was."""

ENDED_WITH_SESSION = "the client token belonged to a session that has ended: attach again"
"""Close reason (with `CLOSE_TOKEN_REVOKED`) for a minted token whose connection outlived its
session row: the process runs the next row of the same session (a `session.start` after a
stop), for which a client needs a token of its own."""

CLOSE_FELL_BEHIND = 4002
"""WebSocket close code: the session closed the connection because the client fell more than
`calu.bus.CLIENT_QUEUE` events behind. Nothing is wrong with the token: the client should
connect again and subscribe with the last sequence number it received (`events.subscribe`);
if the answer says `complete: false`, it takes a fresh `state.get` as well."""

# --------------------------------------------------------------------------- requests (FE -> core)


class InitializeParams(BaseModel):
    """First request of a client; every other request is refused before it.

    The result is an `InitializeResult`. `token` is the client token that `calu run` printed
    (and stored hashed in the database). `client` and `client_version` name the client in the
    log.
    """

    client: str = Field(min_length=1, max_length=100)
    client_version: str = ""
    token: str = ""


class InitializeResult(BaseModel):
    """Result of `initialize`: versions, what the session works on, and where to subscribe."""

    protocol_version: int = PROTOCOL_VERSION
    core_version: str
    workspace: str
    project: str | None = None
    calu_session_id: str | None = None
    """Id of the `sessions` row of the running Claude session (None before the first start)."""
    last_seq: int = 0
    """Sequence number of the newest event; `events.subscribe` continues after it."""


class SessionStartParams(BaseModel):
    """Start a Claude session, or resume the session `resume_id`.

    Result: `{"claude_session_id": ..., "calu_session_id": ...}`, the ids of the Claude session
    and of the session row that runs now. A start after a stop runs a new row, for which a
    client that attached with a minted token needs a token of its own (`ENDED_WITH_SESSION`).

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


MAX_IMAGE_BYTES = 5 * 1024 * 1024
"""Largest image of a chat message, decoded (the limit of the model's API for one image)."""

MAX_IMAGES = 4
"""How many images one chat message may carry."""

MAX_FRAME_BYTES = 32 * 1024 * 1024
"""Largest protocol message a session or a client accepts: room for `MAX_IMAGES` images of
`MAX_IMAGE_BYTES` in base64 with the rest of the message."""

NO_BROWSER_ORIGINS: tuple[None] = (None,)
"""The `origins` of every TCP WebSocket server of a session and of the relay: only a handshake
without an `Origin` header is accepted. Browsers send one, on behalf of whatever page asks;
Calu's own clients (`calu attach`, `calu ide`, `calu web`'s relay) send none, so a web page
cannot reach a session even where `--insecure` trusts the loopback interface (D279)."""


class ChatImage(BaseModel):
    """An image a user attached to a chat message (agent/records/PLAN.md D220).

    `data` is the image file, base64. Only the listed raster formats: an SVG is a document
    that can carry scripts.
    """

    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data: str = Field(min_length=1)

    @field_validator("data")
    @classmethod
    def _decodable_and_small(cls, value: str) -> str:
        """Refuse what is no base64 or decodes to more than `MAX_IMAGE_BYTES`.

        Args:
            value: The base64 text.

        Returns:
            The text unchanged.

        Raises:
            ValueError: If the text is no valid base64, or the image is too large.
        """
        if len(value) * 3 // 4 > MAX_IMAGE_BYTES + 3:
            raise ValueError(f"an image may have at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        try:
            base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("the image data is no valid base64") from exc
        return value


class ChatSendParams(BaseModel):
    """A user chat message.

    `delivery`: "queued" waits until Claude's current turn ends; "inject" is read as soon as the
    running tool calls finish. `to`: "main" or the `agent_id` of a running background subagent;
    a message to a subagent is relayed by the main agent with its SendMessage tool.
    """

    text: str = Field(min_length=1)
    delivery: Literal["queued", "inject"] = "queued"
    to: str = "main"
    images: list[ChatImage] = Field(default_factory=list, max_length=MAX_IMAGES)
    """Images attached to the message, for the main agent only. Claude sees them; the
    checkers are told how many there were, not what they show."""


class ChatQueuedParams(BaseModel):
    """`chat.retract` and `chat.send_now`: one queued message that calu still holds (D283).

    `chat.retract` takes the message out of the queue; Claude never sees it. Result:
    `ChatRetractResult`, with the text, so that a client can put it back into its input.
    `chat.send_now` interrupts the running turn, as the user's stop does, and the message is
    sent as soon as the turn has ended. Result: `{}`. Both refuse a message that calu no
    longer holds (`QueuedMessage.held` false: it was handed to Claude Code or taken in).
    """

    id: str = Field(min_length=1)
    """The `QueuedMessage.id` from `chat.queue`."""


class ChatRetractResult(BaseModel):
    """Result of `chat.retract`: the withdrawn message."""

    text: str
    """The user's text of the message, as it was sent."""


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
    """The human's answer to an `escalation.request`. Result: `{}`.

    `answer` belongs to a question of the agent (`escalation.request` with `questions`,
    agent/records/PLAN.md D35): it is the text Claude receives as the result of its
    `AskUserQuestion` call, and the call itself never runs. With an answer the `decision` is
    ignored, and the escalation is recorded as "answered" rather than allowed or denied.
    """

    escalation_id: str
    decision: Literal["allow", "deny"] = "deny"
    note: str | None = None
    answer: str | None = None
    """The answer to a question of the agent; None for an ordinary allow or deny."""


class AutoEscalateSetParams(AutoEscalate):
    """`auto_escalate.set`: replace the auto-escalation settings of the session. Result: `{}`."""


class PauseSetParams(BaseModel):
    """`pause.set`: pause the session, or end its pause (the owner, 2026-11-26).

    A pause interrupts nothing. The next tool call that needs a check (an edit, a command, a
    commit, the start of a subagent, of any agent) is checked as usual and, if approved,
    waits until the pause ends: by `pause.set {paused: false}`, after `minutes`, or when the
    PreToolUse hook's time is used up. A denied or escalated call is answered at once.
    Pausing a paused session starts its time again. Result: `PauseInfo`.
    """

    paused: bool
    minutes: float | None = Field(default=None, gt=0)
    """How long the pause lasts at most. This model only checks that it is positive: the
    session (`Session.set_pause`, which the `pause.set` handler of `calu.protocol.server`
    calls) takes `pause_timeout_s` of the configuration for None and shortens a longer value
    to it."""


class PauseInfo(BaseModel):
    """Whether the session is paused, and until when (`state.get`, `pause.set`)."""

    paused: bool = False
    until: datetime | None = None
    """When the pause ends by itself (UTC); None when not paused."""


# --- code honesty: requests (kept together; see also `HonestyInfo` and `HonestyUpdate`) ---


class HonestyClearParams(BaseModel):
    """`honesty.clear`: close one open mark of the session's honesty state by hand.

    The marks are in the session's memory, so this is how a person overrides a checker's mark
    on a running session. Whether a review is required does not change. Result: `{}`; an id
    that names no open mark is an error.
    """

    mark_id: int


class HonestyTrustParams(BaseModel):
    """`honesty.trust`: certify a commit, or one file's present content, as honest.

    Without `path`: `rev` is resolved to the commit it names now in the main checkout, or with
    `checkout` in that worktree (a branch is resolved to its present head; the branch itself is
    not remembered), and that commit is recorded as trusted with the source "user" and the
    client's name. With `path`:
    the SHA-256 of that file's present content is recorded the same way, and `rev` is ignored.
    The session then looks up its baseline again and sends `honesty.update`. Result:
    `HonestyTrustResult`; a `rev` that names no commit and a file that cannot be read are
    errors.
    """

    rev: str = "HEAD"
    """A commit id or its prefix, a branch, a tag, or "HEAD"."""
    path: str | None = None
    """A file, absolute or relative to the workspace; None certifies a commit."""
    checkout: str | None = None
    """The checkout to resolve `rev` in (agent/records/PLAN.md D252): a worktree's name as
    `HonestyInfo.worktrees` lists it (`agent-a1b2` for `.claude/worktrees/agent-a1b2`), its
    label, or "." for the main checkout; None is the main checkout. A name that no worktree
    has is an error. Ignored with `path`."""
    note: str = ""


class FilesUserEditParams(BaseModel):
    """`files.user_edit`: the user is about to write these files through an editor.

    `calu ide` sends it immediately before it replaces, deletes or renames a file for the editor's
    file synchronisation (`agent/records/notes/jetbrains-sync-spec.md`). Such a write is the user's
    own edit, not a step of Claude: no checker judges it. The session leaves the paths out of the
    write set of a command that runs at that moment and watches the files for the code honesty
    state, where they count as unreviewed like any change made outside calu. With an empty list
    nothing is recorded and only the result is of use.
    """

    paths: list[str] = Field(default_factory=list, max_length=1000)
    """The files, relative to the workspace, with "/" separators; no `..` segment."""


class FilesUserEditResult(BaseModel):
    """Result of `files.user_edit`."""

    excluded: list[str] = Field(default_factory=list)
    """Directories, relative to the workspace, that hold calu's own output of this session
    (the session traces while `traces.enabled` is on): an editor leaves them alone."""


HUMAN_DIFF_MAX_CHARS = 1_000_000
"""The longest text `human_diffs.add` carries in `before` or `after`."""


class HumanDiffAddParams(BaseModel):
    """`human_diffs.add`: the user saved a file in an editor (agent/records/PLAN.md D253).

    An editor extension sends it after each save through its editor (not for files that
    another program, Claude or the editor's file synchronisation wrote). The session records
    the diff of `before` and `after` while `human_diffs.enabled` is on and git tracks the file;
    otherwise it records nothing. Both texts have their line ends normalised to newlines.
    """

    path: str = Field(min_length=1, max_length=4096)
    """The file, relative to the workspace, with "/" separators; no `..` segment."""
    before: str = Field(max_length=HUMAN_DIFF_MAX_CHARS)
    """The file's text as the editor last loaded or saved it, before this save."""
    after: str = Field(max_length=HUMAN_DIFF_MAX_CHARS)
    """The saved text."""


class HumanDiffAddResult(BaseModel):
    """Result of `human_diffs.add`."""

    enabled: bool
    """`human_diffs.enabled`. An editor that hears False may send nothing for a while."""
    recorded: bool = False
    """True if a diff was written."""
    file: str | None = None
    """The file the diff was appended to, relative to the workspace; None if none."""
    reason: str = ""
    """Why nothing was recorded: "off", "not tracked by git", "unchanged", "binary",
    "calu's own output", or the error of a failed write."""


class HonestyTrustResult(BaseModel):
    """Result of `honesty.trust`: what was recorded."""

    commit_id: str | None = None
    """The full id of the certified commit; None when a file was certified."""
    content_sha256: str | None = None
    """The SHA-256 of the certified file content; None when a commit was certified."""
    path: str | None = None
    """The file as the session names it; None when a commit was certified."""
    recorded: bool = True
    """False if a record of the same strength existed already, so nothing was written."""
    checkout: str = "."
    """The label of the checkout `rev` was resolved in ("." the main checkout,
    `.claude/worktrees/<name>`, or an absolute path); "." when a file was certified."""


class HonestyReviewResult(BaseModel):
    """Result of `honesty.review`: whether an honesty review ran.

    The review judges the unreviewed and the marked files against the baseline. It can take
    minutes; the answer comes when it ended. Meanwhile `honesty.update` says that a review is
    running and `log` events name its progress. Its effect on the state arrives as
    `honesty.update`.
    """

    ran: bool
    """True if the reviewer judged the files and its result was applied to the state."""
    problem: str = ""
    """Why the review did not run or failed; for a review that ran, which files it could not
    judge and which changed while it ran; "" if it judged every file."""


class SideExchange(BaseModel):
    """One earlier side question of a client with the answer it got (`side.ask`)."""

    question: str = Field(min_length=1, max_length=20_000)
    answer: str = Field(max_length=100_000)


class SideAskParams(BaseModel):
    """`side.ask`: a question to Claude beside the running work. Result: `SideAskResult`.

    Claude Code answers from the conversation as it stands, without tools, while the turn of
    the main agent goes on (its `/btw`). Neither the question nor the answer becomes part of
    the conversation: the main agent never sees them, no turn is started or interrupted, no
    step is taken, and nothing is recorded in the history. The answer goes to the asking client
    only. `history` holds that client's earlier side questions, so that a follow-up question
    can refer to them; the session keeps none.
    """

    question: str = Field(min_length=1, max_length=20_000)
    history: list[SideExchange] = Field(default_factory=list, max_length=20)


class SideAskResult(BaseModel):
    """Result of `side.ask`: Claude's answer to the side question, as Markdown."""

    answer: str


class ModelSetParams(BaseModel):
    """`model.set`: change the model of the running Claude session. Result: `{}`.

    The change applies from the next request Claude Code sends to the model, also inside a
    running turn, and to later sessions of this calu process (as `session.start {model}` does).
    `model` is a Claude Code alias ("fable", "opus", "sonnet", "haiku") or a model id; None
    switches back to Claude Code's default model. The effort is fixed when a session starts
    (`session.start {effort}`).
    """

    model: str | None = Field(default=None, min_length=1, max_length=100)


class PermissionModeSetParams(BaseModel):
    """`permission_mode.set`: switch Claude Code's permission mode while the session runs.

    Claude Code's own Shift+Tab, for every client (agent/records/PLAN.md D247). calu offers
    `SWITCHABLE_PERMISSION_MODES`; the other modes Claude Code knows are refused with the
    reason in `REFUSED_PERMISSION_MODES`, and with `claude.require_auto_mode` every mode but
    "auto" is refused. The change applies between turns and inside a running turn, from the
    next tool call on: a call whose check has started keeps the mode its hook reported. It is
    recorded in the session's audit log (`calu sessions <id>`), reaches every client as
    `permission_mode.changed`, and is kept for later starts of this calu process (as
    `model.set` is), not written to the project's settings. A mode the session already runs
    in changes nothing and is not recorded. Result: `{}`.
    """

    mode: PermissionMode


SWITCHABLE_PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "auto")
"""The modes `permission_mode.set` accepts, in the order of Claude Code's Shift+Tab cycle.

Under calu the checkers decide every tool call in all three: "auto" adds auto mode's
classifier as a second approval of what the checkers pass; in "default" and "acceptEdits" the
checkers decide alone (a hook "ask" still reaches the human in "acceptEdits", measured in
`spikes/spike_permission_mode_switch.py`)."""

REFUSED_PERMISSION_MODES: dict[str, str] = {
    "plan": (
        "calu does not offer plan mode: in plan mode Claude Code writes its plan to a file "
        "under ~/.claude/plans, outside the project, and leaving plan mode is a request for "
        "approval that calu does not present as a decision yet"
    ),
    "dontAsk": (
        "in dontAsk mode Claude Code denies every call that would need a permission prompt, "
        "so an escalation would become a denial without anybody being asked"
    ),
    "bypassPermissions": (
        "Claude Code accepts bypassPermissions only in a session started with "
        "--dangerously-skip-permissions, which calu never does; it would also switch off "
        "Claude Code's own approval of what the checkers pass"
    ),
}
"""Claude Code's other modes, with the reason `permission_mode.set` gives for refusing each."""


class RateLimit(BaseModel):
    """The state of one usage limit of the Claude Code account, as Claude Code reports it."""

    type: str = ""
    """Which limit: "five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet" or
    "overage"; "" if Claude Code did not say."""
    status: str = "allowed"
    """"allowed", "allowed_warning" (close to the limit) or "rejected" (the limit is reached)."""
    utilization: float | None = None
    """Used part of the limit, 0.0 to 1.0; None if Claude Code did not report it."""
    resets_at: datetime | None = None
    """When the limit's window starts again."""
    reported_at: datetime | None = None
    """When calu received this state from Claude Code; None if not known (a session before
    D248)."""


class ContextUsage(BaseModel):
    """How full the context window of the main agent is, as Claude Code counts it."""

    tokens: int
    max_tokens: int
    percent: float
    """`tokens` as a percentage of `max_tokens`, 0 to 100."""
    model: str = ""
    """The model the numbers were counted for: the resolved model id of the session."""


class UsageResult(BaseModel):
    """Result of `usage.get`: what the session knows about cost, context and limits.

    `context` is asked from Claude Code when the request arrives and is None when no session
    runs or Claude Code does not answer. `rate_limits` is the newest state of each limit Claude
    Code reported in this calu process, each with `reported_at`; an account without limits (an
    API key, Bedrock) has none. `cost_usd` and `num_turns` are those of the last `turn.result`.
    """

    context: ContextUsage | None = None
    rate_limits: list[RateLimit] = Field(default_factory=list)
    cost_usd: float | None = None
    num_turns: int = 0


class UsagePanelResult(BaseModel):
    """Result of `usage.panel`: everything the usage panel of a client shows (PLAN.md D248).

    `limits` are the usage limits Claude Code reported in this calu process (`rate_limit_event`
    of the stream, each with `reported_at`); empty if Claude Code has not reported one yet.
    `claude_usage` is the text of Claude Code's own `/usage` in a session
    without a terminal, read with the control request `get_session_cost`: no model call, no
    turn, nothing recorded and nothing shown to the checkers. `account_limits` are the plan's
    limits that Claude Code reads from its account at this request (control request
    `get_usage`, which Claude Code marks experimental), with `reported_at` set to now; empty
    for an account without such limits (an API key, Bedrock, Vertex) or when Claude Code does
    not answer it. `subscription` is the account's plan as Claude Code names it ("pro",
    "max", ...), "" if none or not known. When `get_usage` fails or answers in a shape calu
    does not know, `account_limits` stays empty and `account_limits_problem` says so: the
    client then points to `limits`, the figures reported in this process (D250).
    """

    limits: list[RateLimit] = Field(default_factory=list)
    account_limits: list[RateLimit] = Field(default_factory=list)
    account_limits_problem: str = ""
    """Why the plan's limits could not be read now ("" if they could, or the account has none):
    no session runs, Claude Code did not answer `get_usage`, or its answer has an unknown
    shape. The panel says so and points to `limits`."""
    subscription: str = ""
    claude_usage: str = ""
    """Claude Code's `/usage` text: cost, durations, code changes, tokens; "" if not read."""
    claude_usage_problem: str = ""
    """Why `claude_usage` is empty ("" if it is not): no session runs, or Claude Code did not
    answer."""
    context: ContextUsage | None = None
    cost_usd: float | None = None
    num_turns: int = 0


class EventsSubscribeParams(BaseModel):
    """`events.subscribe`: receive events from now on, with the ones since `after_seq`.

    A client that connects for the first time sends `after_seq = 0` and takes the state from
    `state.get`; a client that reconnects sends the last sequence number it saw. Result:
    `EventsSubscribeResult`.
    """

    after_seq: int = Field(default=0, ge=0)


class EventsSubscribeResult(BaseModel):
    """Result of `events.subscribe`: the events the client missed, oldest first.

    `complete` is False if the requested events are no longer in the buffer (the client should
    then take a fresh `state.get` instead of rebuilding from the events). Events that happen
    after this result are sent as notifications.
    """

    events: list[RecordedEvent] = Field(default_factory=list)
    complete: bool = True
    last_seq: int = 0


class StoredStep(BaseModel):
    """One recorded tool call of a stored session (a row of `steps`).

    `decision` is "pass", "allow", "deny" or "escalate"; `summary` is what the checker said the
    step does, `result_summary` the beginning of what the tool returned.
    """

    seq: int
    turn: int | None = None
    agent_id: str | None = None
    tool_use_id: str | None = None
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    decision: str | None = None
    reason: str = ""
    summary: str = ""
    result_summary: str = ""
    created_at: datetime | None = None


class SessionHistoryParams(BaseModel):
    """`session.history`: the recorded conversation of a session, from the database.

    A client that connects to a session which started before it (or before calu was restarted)
    rebuilds its view from this; the event buffer of the bus only holds the running process
    (`events.subscribe`). The result is a `SessionHistoryResult`.
    """

    claude_session_id: str | None = None
    """Which session; None is the one that runs now."""
    limit: int = Field(default=500, ge=1, le=5_000)
    """Maximum number of steps returned (the newest ones)."""


class SessionHistoryResult(BaseModel):
    """Result of `session.history`: the turns, the steps and the chat summary of a session.

    `turns` is the condensed conversation as the checkers see it (user inputs verbatim, one
    record per step, the narrated answer); `steps` adds the tool input and the result of each
    call. Both are oldest first. `turns` includes a turn that has not ended (`ended_at` is
    None): the running turn of a live session, a turn that waited on a decision when a fork
    copied the conversation, a turn whose session was cut off. Such a turn holds its inputs and
    `prompt_uuid` only, no steps and no answer; a client that also replays the session's events
    tells the running turn from them by `prompt_uuid`.
    """

    calu_session_id: str | None = None
    claude_session_id: str | None = None
    chat_summary: str = ""
    turns: list[TurnRecord] = Field(default_factory=list)
    steps: list[StoredStep] = Field(default_factory=list)


class CheckerRunInfo(BaseModel):
    """One checker model call that judged a step (a row of `checker_runs`)."""

    checker_name: str
    checker_version: int
    model: str
    decision: str
    reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int | None = None


class EscalationContextParams(BaseModel):
    """`escalation.context`: everything the checker saw and said when it escalated a step.

    An `escalation.request` event carries what a client needs to show the question (the step,
    its rendering, the reason). This request adds what a client needs to **judge** it: the
    condensed conversation and the chat summary as they were at that moment, the content of the
    file an edit would change, and the checker runs with their models and versions. The result
    is an `EscalationContextResult`.
    """

    escalation_id: str


class EscalationContextResult(BaseModel):
    """Result of `escalation.context` (see `EscalationContextParams`).

    `recent` and `chat_summary` are the context of the moment the step was checked, not of now:
    they are stored with the escalation, so they still answer "what did the checker know?" after
    the session has moved on. `state` is "open", "allowed", "denied" or "cancelled".
    """

    escalation_id: str
    state: str
    reason: str = ""
    rendered: str = ""
    """The step as the checker saw it, rendered as Markdown."""
    tool_name: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    files: dict[str, str] = Field(default_factory=dict)
    """Content of the file an edit would change, before the change."""
    recent: str = ""
    chat_summary: str = ""
    checker_runs: list[CheckerRunInfo] = Field(default_factory=list)
    note: str = ""
    resolved_by: str | None = None


class CheckerInfo(BaseModel):
    """One checker definition as a client may read it (a row of `checker_types`).

    Clients read these; changing them needs other credentials than a session has
    (agent/records/PLAN.md §6.2), so there is no request to write them here.
    """

    name: str
    route: str
    model: str
    version: int
    enabled: bool = True
    instructions: str = ""


class CheckerListResult(BaseModel):
    """Result of `checkers.get`: the checker definitions this session uses, by name."""

    checkers: list[CheckerInfo] = Field(default_factory=list)


class SessionSummary(BaseModel):
    """A stored session of this project, for a "resume session" list in a client."""

    calu_session_id: str
    claude_session_id: str | None = None
    state: str
    directory: str
    started_at: datetime
    ended_at: datetime | None = None
    first_input: str = ""
    """The first user input of the session, shortened to 200 characters ("" if none)."""


class SessionListParams(BaseModel):
    """`session.list`: the stored sessions of this project directory, newest first.

    Result: `SessionListResult`. The ids come from the database, so a client can offer
    `session.start {resume_id: <claude_session_id>}` after a restart of calu.
    """

    limit: int = Field(default=20, ge=1, le=200)


class SessionListResult(BaseModel):
    """Result of `session.list`: stored sessions, newest first."""

    sessions: list[SessionSummary]


class RecordedEvent(BaseModel):
    """An event as the bus keeps it for replay: its method, its params and its time."""

    method: str
    params: dict[str, Any]
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HonestyMarkInfo(BaseModel):
    """One open mark of the session's honesty state, for a client.

    A mark names code a checker or an honesty review found dishonest. `symbol` is empty when
    the whole file is meant.
    """

    id: int
    path: str
    symbol: str = ""
    reason: str
    opened_by: str | None = None
    """Name of the checker that opened it, or "the honesty review"."""

    def label(self) -> str:
        """Render the marked spot.

        Returns:
            `path:symbol`, or the path alone when no symbol is named.
        """
        return f"{self.path}:{self.symbol}" if self.symbol else self.path


class HonestyInfo(BaseModel):
    """The code honesty state of a session as a client shows it (`calu.honesty`).

    With the mode "off" every other field but `worktrees` has its default. The state is clean
    when there is a baseline, no open mark, no unreviewed file and no review is required; with
    the mode "enforce" a command that executes project code and a commit need a clean state.
    """

    mode: str = "off"
    """`off`, `track` or `enforce` (`honesty.mode`)."""
    marks: list[HonestyMarkInfo] = Field(default_factory=list)
    """The open marks, oldest first."""
    baseline: str | None = None
    """The full id of the trusted commit the files are compared with; None if no ancestor of
    HEAD is trusted by a sufficient source."""
    baseline_source: str | None = None
    """Who established the baseline's trust: "user", "review" or "check"."""
    unreviewed_all: bool = False
    """True when every file counts as unreviewed: there is no baseline, or git could not be
    asked."""
    unreviewed_count: int = 0
    """How many files have a content nobody verified; 0 when `unreviewed_all` is set."""
    unreviewed: list[str] = Field(default_factory=list)
    """The first 20 of those files, sorted."""
    review_required: bool = False
    """True while more marks are open than `honesty.max_open_marks` allowed; an honesty review
    clears it."""
    review_running: bool = False
    """True while an honesty review runs."""
    clean: bool = False
    worktrees: list[str] = Field(default_factory=list[str])
    """The project's worktrees by name, sorted (`calu.checkouts.worktree_name`: `agent-a1b2` for
    `.claude/worktrees/agent-a1b2`, the absolute path for one outside the project); empty
    without worktrees. Sent in every mode, also "off", so that `/trust <worktree> <ref>` can
    complete the names (agent/records/PLAN.md D252)."""


class SessionSnapshot(BaseModel):
    """Result of `state.get`: everything a client needs to show the session right now.

    A client that connects takes this snapshot and then applies the events after `last_seq`
    (`events.subscribe`). The open escalations are in it, so a client that joins while the
    session waits can answer them.
    """

    state: State
    claude_session_id: str | None = None
    calu_session_id: str | None = None
    workspace: str
    project: str | None = None
    model: str | None = None
    effort: str | None = None
    last_seq: int = 0
    agents: list[AgentInfo] = Field(default_factory=list)
    escalations: list[EscalationRequest] = Field(default_factory=list)
    queue: list[QueuedMessage] = Field(default_factory=list)
    auto_escalate: AutoEscalate = Field(default_factory=lambda: AutoEscalate())
    permission_mode: str | None = None
    """Claude Code's permission mode as it last reported it (`permission_mode.changed`); before
    its first report the mode calu started it in; None while that is not known (no session
    runs, or a settings file chose the mode and Claude Code has not reported it yet)."""
    honesty_mode: str = "off"
    """`honesty.mode`: `off`, `track` or `enforce`. The same value as `honesty.mode`."""
    honesty_marks: list[HonestyMarkInfo] = Field(default_factory=list)
    """The open marks. The same list as `honesty.marks`."""
    honesty: HonestyInfo = Field(default_factory=HonestyInfo)
    """The code honesty state as the session last computed it (it is computed at the start,
    for every checked step and after every change, and sent as `honesty.update`)."""
    rate_limits: list[RateLimit] = Field(default_factory=list)
    """The newest state of each usage limit Claude Code reported (`usage.update`)."""
    pause: PauseInfo = Field(default_factory=PauseInfo)
    """The user's pause (`pause.set`, `pause.changed`)."""


class CheckerContextResult(BaseModel):
    """Result of `context.get`: the conversation context as the checkers see it.

    Leaves out what does not change during a conversation: the checker instructions. A
    checker additionally sees the step it judges. No file of the workspace is part of it.
    """

    chat_summary: str
    """CHAT_SUMMARY.md: the turns folded so far ("" if the file does not exist)."""
    recent: str
    """The unfolded turns and the running turn, rendered as for a check: the user's messages,
    one line per step with its summary, narrations of Claude's thinking and text."""


REQUESTS: dict[str, type[BaseModel]] = {
    "initialize": InitializeParams,
    "state.get": EmptyParams,
    "events.subscribe": EventsSubscribeParams,
    "session.start": SessionStartParams,
    "session.stop": EmptyParams,
    "session.list": SessionListParams,
    "session.history": SessionHistoryParams,
    "chat.send": ChatSendParams,
    "chat.retract": ChatQueuedParams,
    "chat.send_now": ChatQueuedParams,
    "comment.add": CommentAddParams,
    "escalation.decide": EscalationDecideParams,
    "escalation.context": EscalationContextParams,
    "checkers.get": EmptyParams,
    "auto_escalate.set": AutoEscalateSetParams,
    "pause.set": PauseSetParams,
    "honesty.clear": HonestyClearParams,
    "honesty.trust": HonestyTrustParams,
    "honesty.review": EmptyParams,
    "files.user_edit": FilesUserEditParams,
    "human_diffs.add": HumanDiffAddParams,
    "interrupt": EmptyParams,
    "context.get": EmptyParams,
    "side.ask": SideAskParams,
    "usage.get": EmptyParams,
    "usage.panel": EmptyParams,
    "model.set": ModelSetParams,
    "permission_mode.set": PermissionModeSetParams,
    "shutdown": EmptyParams,
}
"""Request method -> model of its params. `shutdown` ends the calu process, so only a client
that may steer the session may send it (every client with a token may)."""

# ------------------------------------------------------------- the relay of `calu serve` (D242)

CLOSE_SERVER_TOKEN_REVOKED = 4003
"""WebSocket close code of the relay: the client's server token was revoked, or its server
ended. Connecting again with it is refused."""

RELAY_SESSION_PATH = "/session/"
"""Path prefix on a relay address that names one session: `ws://127.0.0.1:<port>/session/<id>`
speaks the session protocol above, with the server token in `initialize`. The bare relay
address (path `/`) speaks `RELAY_REQUESTS`."""


class RelayServer(BaseModel):
    """Which `calu serve` a relay belongs to."""

    server_id: str | None = None
    """Id of the server's `servers` row; None if the server could not record itself."""
    hostname: str
    os_user: str
    projects_directory: str = ""
    """The resolved directory the server starts sessions in or below (D243); a `relay.start`
    for another directory is refused."""


class RelayInitializeResult(BaseModel):
    """Result of `initialize` on a relay's own address (path `/`).

    A client tells a relay from a session by the `relay` key (a session answers with an
    `InitializeResult`, which has `workspace` instead).
    """

    protocol_version: int = PROTOCOL_VERSION
    core_version: str
    relay: RelayServer


class RelaySession(BaseModel):
    """One session of a relay's server, as `relay.sessions` lists it."""

    calu_session_id: str
    """Id of the session's `sessions` row; the relay address of the session is the relay's
    address followed by `RELAY_SESSION_PATH` and this id."""
    directory: str
    state: str
    """The session's state (`State`), or "unknown" if it did not answer in time."""
    claude_session_id: str | None = None
    model: str | None = None
    open_escalations: int = 0
    started_at: datetime
    name: str = ""
    """The name a person gave the session (`sessions.name`); "" for none, and for a client
    without a server token (`--insecure`)."""
    state_since: datetime | None = None
    """When the session entered its current state, from the database; None if not known (a
    client without a server token, a schema before migration 0040)."""


class RelaySessionsResult(BaseModel):
    """Result of `relay.sessions`: the server's running sessions, newest first."""

    sessions: list[RelaySession]


class RelayStartParams(BaseModel):
    """Ask the relay's server for a session (`relay.start`), as `calu start` asks (D208).

    `kind` "new" needs no `claude_session_id`; "resume" continues it (with `fork_before_turn`:
    a branch before that turn); "fork" continues a copy of it. `directory` is a directory on
    the server's machine, inside the server's projects directory (`RelayServer`). The result
    is a `RelayStartResult` once the session can be attached to; a refusal or failure of the
    server is an error (-32000) with its reason.
    """

    kind: Literal["new", "resume", "fork"] = "new"
    directory: str = Field(min_length=1, max_length=4096)
    claude_session_id: str | None = Field(default=None, max_length=100)
    fork_before_turn: int | None = Field(default=None, ge=2)
    prompt: str | None = Field(default=None, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh", "max", "default"] | None = None
    project: str | None = Field(default=None, max_length=200)


class RelayStartResult(BaseModel):
    """Result of `relay.start`: the started session."""

    calu_session_id: str


RELAY_REQUESTS: dict[str, type[BaseModel]] = {
    "initialize": InitializeParams,
    "relay.sessions": EmptyParams,
    "relay.start": RelayStartParams,
}
"""Request method -> params of the relay's own address. `initialize` carries the server token
(`calu token mint --server`) in `token`."""

# ---------------------------------------------------------------------------- events (core -> FE)


class Event(BaseModel):
    """Base class of all events. `method` is the JSON-RPC method name of the notification.

    `seq` numbers the events of one calu process from 1; the bus sets it when it publishes the
    event (`calu.bus.Bus.publish`), and a client uses it to ask for what it missed
    (`events.subscribe`). An event that was never published keeps 0.
    """

    method: ClassVar[str]
    seq: int = 0


State = Literal[
    "starting", "idle", "running", "checking", "tool_call", "escalated", "stopped", "failed"
]
"""States of a session (agent/records/PLAN.md §3.1):

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
    uuid: str = ""
    """The transcript entry's uuid, if the SDK reported one. The last one of a turn is where a
    fork of that turn resumes from (D36)."""


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
    held: bool = False
    """True while calu still holds the message (a "queued" message for the main agent, sent
    while a turn ran; it is written to Claude Code when the turn ends, D283): `chat.retract`
    and `chat.send_now` apply to it. False once Claude Code has it."""


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
    checkers see it only narrated, never as the user's words. `source` "system": calu or
    Claude Code put the text into the conversation (a checker's note, the notification of a
    finished background task, hook feedback); it is treated like a "cross_session" message
    and labelled as what it is.
    """

    method: ClassVar[str] = "user.input"
    text: str
    source: Literal["user", "cross_session", "system"] = "user"
    images: list[ChatImage] = Field(default_factory=list)
    """The images the user attached to this message, for clients that can show them."""
    uuid: str = ""
    """The transcript entry's uuid, when Claude Code replays it (`replay-user-messages`). calu
    records it with the turn so that a later session can be forked before this prompt (D36)."""
    to: str = "main"
    """Whom the user wrote to: "main", or the `agent_id` of a background subagent
    (`chat.send {to}`). For a subagent, `text` is the user's own text, without the request to
    relay it that the main agent received around it."""


class TurnResult(Event):
    """Claude finished a turn."""

    method: ClassVar[str] = "turn.result"
    session_id: str | None = None
    is_error: bool = False
    subtype: str = "success"
    result: str = ""
    cost_usd: float | None = None
    num_turns: int = 0
    background_tasks: int = 0
    """Background tasks still running when the turn ended (background commands, Monitors,
    workflows, background subagents). Claude Code starts another turn by itself when they
    finish (measured, CLI 2.1.319), so a client that waits for "done" waits for 0 (D158)."""


class AgentInfo(BaseModel):
    """A subagent. `agent_id` is the id of the Agent tool call that started it."""

    agent_id: str
    sdk_agent_id: str | None = None
    description: str = ""
    subagent_type: str | None = None
    background: bool = False
    status: Literal["running", "completed", "failed", "stopped"] = "running"


class HonestyUpdate(Event, HonestyInfo):
    """The whole code honesty state of the session, sent whenever it changes (`HonestyInfo`)."""

    method: ClassVar[str] = "honesty.update"


class AgentsUpdate(Event):
    """The complete list of subagents of this session, sent whenever it changes."""

    method: ClassVar[str] = "agents.update"
    agents: list[AgentInfo]


class HonestyUnitStatus(BaseModel):
    """The honesty checker's status of one function, method or class of a file."""

    symbol: str
    """E.g. `fields` or `Parser.fields`."""
    honest: bool
    """True if its name and documentation comment agree with what it does after the step."""
    reason: str = ""
    """For a dishonest unit: what disagrees with what."""


class HonestyFileStatus(BaseModel):
    """The honesty checker's status of one file a step changes.

    See agent/records/PLAN.md D224 part 4.
    """

    path: str
    """The file, relative to the project root as the checker named it."""
    verdict: Literal["HONEST", "DISHONEST"]
    """HONEST: after the step the names and documentation comments in the file agree with
    what the code does. DISHONEST: some unit in it does not."""
    reason: str = ""
    """For DISHONEST: what disagrees with what, and both ways to settle it (change the code, or
    change the name and documentation), neither favoured."""
    units: list[HonestyUnitStatus] = Field(default_factory=list[HonestyUnitStatus])
    """The units whose honesty the step changed or that are dishonest now; empty when the
    verdict is about the file as a whole."""


class CheckResult(Event):
    """The routing and (if checked) the checker decision for one tool call.

    `decision`, `reason` and `comment` are the combined answer of the checkers that approve or
    refuse a step. The honesty checker is not one of them: its result is `honesty`, a status
    per file, and `honesty_escalation` when it asks for a person. A client shows those and
    does not show the honesty checker's run as a decision.
    """

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
    honesty: list[HonestyFileStatus] = Field(default_factory=list[HonestyFileStatus])
    """The honesty checker's status of every file the step changes, if that checker judged
    the step; empty otherwise (it is disabled, the step is no edit, or its call failed). The
    statuses say what the checker found. They take effect in the honesty state
    (`honesty.update`) only after the tool call succeeded."""
    honesty_escalation: str = ""
    """Non-empty if the honesty checker asked for a person, with its reason: something in the
    step looks like a deliberate backdoor or malicious behaviour. `decision` is then
    ESCALATE (unless another checker denied the step)."""


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
    questions: list[dict[str, Any]] = Field(default_factory=list)
    """For a question of the agent (`AskUserQuestion`, agent/records/PLAN.md D35): the questions as
    Claude asked them, each with `question`, `header`, `options` (`label` and `description`) and
    `multiSelect`. A client shows the options and answers with `escalation.decide {answer}`. Empty
    for every other escalation."""
    opened_at: datetime | None = None
    """When the step began to wait. The snapshot of `state.get` carries the same value, so a
    client that attaches later can say how long the step has waited. None from a session that
    does not report it."""
    suggested_rule: str = ""
    """A temporary rule a checker suggested with the escalation, which would have let the step
    pass (agent/records/notes/temporary-rules-design.md, section 5); "" for none. A client
    shows it in an editable field next to Allow and Deny and adds it with the user login if
    the field is not empty when the person decides. The session never adds it."""


class EscalationResolved(Event):
    """An escalation ended, by a human decision or by cancellation.

    "cancelled": withdrawn without a human decision (e.g. interrupt); the tool call did not run.
    """

    method: ClassVar[str] = "escalation.resolved"
    escalation_id: str
    decision: Literal["allow", "deny", "cancelled"]
    note: str | None = None


class StepHeld(Event):
    """A tool call starts or stops waiting because a decision about another step is open.

    While a human decides about one step, calu approves no other step of the session (the hold,
    agent/records/PLAN.md D235). A step is held either before its check or after its approval was
    recorded, when an escalation came up meanwhile; in the second case its `check.result` has
    already been sent, so a client cannot tell from the other events that the step waits. Every hold
    sends one event with `held` true when it begins and one with `held` false when it ends (the step
    then runs, or is escalated itself). A `log` line says the same in words.
    """

    method: ClassVar[str] = "step.held"
    tool_use_id: str | None
    agent_id: str | None = None
    tool_name: str
    held: bool
    """True when the wait begins, False when it ends."""
    reason: str = ""
    """What holds the step, e.g. "escalation 4" ("" in the event that ends the wait)."""


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


class PauseChanged(Event):
    """The session was paused, its pause was started again, or it ended (`pause.set`).

    Sent to every client, so that each shows the pause; also when the time ran out (then
    `client` is ""). While the session is paused, every call it holds sends `step.held` with
    the reason "the user's pause". Not recorded in the history.
    """

    method: ClassVar[str] = "pause.changed"
    paused: bool
    until: datetime | None = None
    """When the pause ends by itself (UTC); None when it ended."""
    client: str = ""
    """The client that paused or resumed; "" when the time ran out."""


class AutoEscalateChanged(Event):
    """The auto-escalation categories of the session changed (`AutoEscalate`).

    Sent when a client calls `auto_escalate.set` and when a reloaded configuration carries other
    categories than the running session had (`config_changed`, after `calu config set` or the web
    interface). Several clients may be attached at once, so a client that offers the setting --
    the ACP adapter maps it to ACP's session modes -- learns from this event that somebody else
    changed it. Not recorded in the history.
    """

    method: ClassVar[str] = "auto_escalate.changed"
    settings: AutoEscalate


class PermissionModeChanged(Event):
    """Claude Code's permission mode changed, as Claude Code reports it.

    Sent when a client switched the mode (`permission_mode.set`) and when Claude Code reports
    another mode by itself: at the start of a run, when a tool call changed it, or when it fell
    back from "auto" for a model without auto mode. Every attached client gets it, so a client
    that shows the mode learns of a change made elsewhere. The event is not stored in the
    history; a change a client asked for is in the audit log.
    """

    method: ClassVar[str] = "permission_mode.changed"
    mode: str
    """The mode: "default", "acceptEdits", "auto", or another mode Claude Code reports
    ("plan" after a tool call entered it)."""
    client: str | None = None
    """The client that asked for the change; None when Claude Code changed it by itself or
    reported it at a start."""


class UsageUpdate(Event):
    """Claude Code reported the state of a usage limit of its account (the whole known list).

    Sent when a limit changes state (for example from "allowed" to "allowed_warning"), so a
    client can show how much of the quota is used without asking. Not recorded in the history,
    and it does not change the session's state.
    """

    method: ClassVar[str] = "usage.update"
    rate_limits: list[RateLimit]


EVENTS: list[type[Event]] = [
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
    StepHeld,
    EditDiff,
    AutoEscalateChanged,
    PauseChanged,
    PermissionModeChanged,
    HonestyUpdate,
    UsageUpdate,
    Log,
]
"""Every event class, for the JSON Schema export (`calu.protocol.schema`)."""
