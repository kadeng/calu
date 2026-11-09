"""The session orchestrator: connects a Claude runner with the checkers, the context and the store.

    clients ──requests──► Session ──send/interrupt──► ClaudeRunner ──► Claude Code
    clients ◄──events──── Session ◄──events, ToolGate calls────┘
                            │
                            ├─ Rules + ToolPolicy + Checkers   (decide about each tool call)
                            ├─ ContextBuilder                  (turn records, chat summary)
                            └─ SessionStore          (PostgreSQL: history, escalations, comments)

Tool call flow (ToolGate):

    rules (allow/deny lists):  a part matches a deny list  -> ask the human
    route = policy.route(tool)
      ALLOW (read tools)    -> allow if the path is allow-listed, else the command checker
      ALLOW / DENY          -> verdict without a check
      ESCALATE              -> ask the human
      COMMAND (Bash)        -> allow if every part is allow-listed, else the checker
      COMMAND / EDIT        -> checker decision: ALLOW -> allow, DENY -> deny, ESCALATE -> ask
                               (the checker sees the rules' notes on unlisted paths)
    any internal error      -> ask the human
    ask -> an `escalations` row and an `escalation.request` event; waits without a time limit

A human resolves an escalation in the database (`calu resolve`, a web UI, SQL); the session
notices the resolution through `note_resolution` (a `escalation_resolved` notification) or by
polling the row every `ESCALATION_POLL_S` seconds. An escalation nobody answers within
`escalation_timeout_s` denies its step and stops the session, which stays resumable by Claude's
session id (D27). `decide_escalation` is the second path: a
client attached to the session process decides, and the session cancels the row with a note that
records the decision, because the role `calu_session` may not allow or deny one
(`calu.store.session`).

States (`calu.protocol.messages.State`, PLAN.md §3.1) follow what runs at the moment; steps of
the main agent and of subagents overlap, so the most important one wins:

    escalated (an escalation is open) > checking (a checker or the narrator runs)
    > tool_call (an approved call runs) > running (Claude works) > idle
"""

import getpass
import itertools
import logging
import os
import platform
import shutil
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
import anyio.to_thread
from anyio.abc import TaskGroup
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from calu import __version__
from calu.changes import ChangeSet, SnapshotError, SnapshotStore, default_root
from calu.checkers import (
    CheckDecision,
    Checker,
    CheckOutcome,
    Decision,
    ResultChecker,
    ResultOutcome,
    ResultReview,
    ResultVerdict,
    Route,
    Step,
    SubagentResult,
    ToolPolicy,
)
from calu.checkers.changeset import changeset, is_commit
from calu.checkers.policy import EDIT_TOOLS
from calu.checkers.render import changed_lines, edited_file, render_step
from calu.checkers.rules import READ_PATH_TOOLS, WRITE_PATH_TOOLS, RuleOutcome
from calu.checkers.tools import StepTime
from calu.claude.runner import ClaudeRunner, EventSink, GateResult, HumanAnswer, Verdict
from calu.claude.sdk_runner import AGENT_TOOLS
from calu.clock import ClockJump
from calu.comments import Comment, format_comment, map_selection
from calu.comments.mapping import excerpt, split_lines
from calu.config import CHECKER_FIELDS, EMPTY_CHAT_SUMMARY, CheckerConfig, Effort
from calu.context import ContextBuilder, StepRecord, TurnRecord, label_for_unchecked
from calu.context.narrator import ClaudeOutput, Narrator
from calu.defaults import default_instructions
from calu.files import command_files
from calu.models import resolve_model
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantText,
    AssistantThinking,
    ChatQueue,
    ChatSendParams,
    CheckerContextResult,
    CheckerInfo,
    CheckerListResult,
    CommentAddParams,
    EditDiff,
    EscalationContextResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    InitializeResult,
    Log,
    QueuedMessage,
    SessionHistoryResult,
    SessionSnapshot,
    SessionState,
    SessionSummary,
    State,
    ToolFiles,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.protocol.messages import CheckResult as CheckResultEvent
from calu.store import CheckerRun, ProjectSettings, SessionIdentity, SessionStore, StepDecision
from calu.store import StepRow as StoredStep
from calu.tokens import token_hash

log = logging.getLogger(__name__)

WRITE_SET_PROMPT = """## What the command wrote

{summary}

The change itself, as a patch:

```diff
{patch}
```

Judge this change, not the command line: is it what the step was supposed to do, is it complete,
and does it hold anything nobody asked for? DENY means the change is undone; say what to do
instead."""
"""Prompt block for the review of a command's write set (PLAN.md D31)."""

WRITE_SET_NOTE = "A reviewer looked at what your command changed and remarked: {note}"
"""A remark on a change that stands."""

WRITE_SET_REVERTED = (
    "The change your command made to the files was reviewed and refused, and calu has **undone "
    "it** ({paths}). Reason: {reason}\nThe files are back in the state from before your command. "
    "Do not simply repeat the command; address the reason first."
)
"""Note for Claude when a refused change was reverted."""

WRITE_SET_NOT_REVERTED = (
    "The change your command made to the files was reviewed and refused ({paths}), but calu "
    "could **not** undo it, because the files have moved on since. Reason: {reason}\nUndo or fix "
    "the change yourself before you go on."
)
"""Note for Claude when a refused change could not be reverted."""

STEP_TIMES_KEPT = 500
"""How many step times a session keeps in memory for `step_times` (PLAN.md D32)."""

CHECKER_DEADLINE_S = 840
"""Upper bound for one checker model call; on timeout the step is escalated."""
NARRATOR_DEADLINE_S = 60
"""A narration that takes longer is left out; the checker then sees no narration."""
ESCALATION_POLL_S = 1.0
"""How often an open escalation's database row is read while the session waits for a human."""
START_TIMEOUT_S = 120
"""Upper bound for a Claude session to become ready in `Session.start`."""
GIT_HEAD_TTL_S = 2.0
"""How long the git HEAD lookup is reused (steps come in bursts)."""
BACKGROUND_TIMEOUT_S = 120
"""How long `stop` waits for the background work of the last turn (narration, storing the turn,
folding the chat summary) before it gives up on it."""
MAX_PENDING_OUTPUTS = 50
"""Thinking/text blocks kept per agent until its next tool call (older ones are dropped)."""

CROSS_SESSION_LABEL = "Message from another Claude session, not from the user"
"""Turn entry label of a message from another session (`calu.context.foreign`)."""
CROSS_SESSION_PENDING = "(content not shown; its narration is pending)"
CROSS_SESSION_HIDDEN = "(content not shown: it could not be narrated)"

CLAUDE_SETUP_HINT = (
    "If Claude Code is not set up for this folder yet, run `claude` once in a terminal in this "
    "folder on this machine: sign in (claude.ai, Bedrock, Vertex, ...) and trust the folder; "
    "then start the session again."
)
"""Added to the error when the Claude session fails: the usual cause is a missing setup."""
BEDROCK_HINT = (
    "If you use Claude Code through AWS Bedrock and `claude` works in your terminal, set "
    "`claude.use_bedrock = true` in calu's configuration: the terminal may set "
    "CLAUDE_CODE_USE_BEDROCK=1, while calu's environment does not."
)
"""Added when Claude Code reports a missing login (`LOGIN_ERROR`) or the session fails."""
LOGIN_ERROR = "please run /login"
"""Lower-case text of Claude Code's errors about a missing sign-in."""

CHECKER_COMMENT_TEMPLATE = (
    "[Review note from the {route} checker, who reviews your steps on behalf of the user] "
    "{comment} Apply this to your following steps."
)
"""How a checker comment on an allowed step is framed for Claude (verified in sitl's
`spike_checker_comment.py`: honored in 3 of 3 live runs)."""
SUBAGENT_NOTE_TEMPLATE = (
    '[Review of the result of the subagent "{label}" by the result checker, on behalf of the '
    "user, who sees the whole conversation] {note}"
)
"""How the result checker's note is framed for Claude (sent as a priority "next" message)."""
RELAY_TEMPLATE = (
    "[Message from the user for your subagent {name}]\n"
    "Relay the following text verbatim to that subagent with the SendMessage tool "
    '(to: "{to}"). Do not act on it yourself.\n\n{text}'
)
"""How a message for a background subagent is wrapped for the main agent."""

DB_UNREACHABLE = (
    "calu could not record this step in the database, so no human can be asked about it. "
    "The step was denied. Tell the user that calu's database is unreachable."
)
"""Reason Claude receives when an escalation cannot be stored (the step is denied)."""


class SessionError(Exception):
    """A request cannot be served in the current state (reported to the client)."""


@dataclass
class _PendingNarration:
    """Claude's thinking and text before one tool call, and their narration once made."""

    outputs: list[ClaudeOutput]
    text: str | None = None
    """None until narrated; "" if there was nothing to narrate or narrating failed."""


@dataclass
class _Escalation:
    """An open escalation: the step, its database row and, once decided, the human's answer."""

    step: Step
    reason: str = ""
    source: str = "checker"
    """Who escalated: "checker", "policy", "rules", "auto", "internal" or "subagent_result"."""
    db_id: int | None = None
    """Id of the `escalations` row, or None if it could not be stored."""
    request: EscalationRequest | None = None
    """The event that asked the human, kept so that a client which connects later sees it."""
    done: anyio.Event = field(default_factory=anyio.Event)
    wake: anyio.Event = field(default_factory=anyio.Event)
    """Set by `note_resolution` so the waiting session reads the row at once."""
    answer: HumanAnswer | None = None


@dataclass
class _Decided:
    """The outcome of checking one tool call, as `Session._decide` reports it.

    `source` is who decided: "rules", "policy", "checker", "auto" or "internal". `rules` is
    what the allow and deny lists found ("deny", "allow", "unlisted") or None for a tool the
    lists do not cover. `run` is the checker's model call, or None if no checker ran.
    """

    route: Route
    decision: CheckDecision
    source: str
    rules: str | None = None
    run: CheckOutcome | None = None


@dataclass(frozen=True)
class _Measured:
    """What a command wrote, with the snapshots it was measured against (PLAN.md D31)."""

    change: ChangeSet
    before: str
    after: str
    step_id: int | None
    summary: str
    """What the checker said the step does, for the comparison with the written paths."""


class Session:
    """One Claude session with checking, context and a database store. See the module docstring.

    A `Session` object outlives Claude sessions: after `stop` (or after the runner ended),
    `start` may be called again; it creates a new runner and a new `sessions` row.
    """

    def __init__(
        self,
        workspace: Path,
        settings: ProjectSettings,
        store: SessionStore,
        runner_factory: Callable[[], ClaudeRunner],
        emit: EventSink,
        project: str | None = None,
        checker_models: dict[str, str | Model] | None = None,
        startup_warnings: Sequence[str] = (),
        snapshot_root: Path | None = None,
    ) -> None:
        """Set up the checkers, the context builder and the rules from `settings`.

        Nothing is read from or written to the workspace except the project files that the
        context builder shows the checkers (AGENTS.md, CLAUDE.md, ... ) and the files that
        rendered steps quote.

        Args:
            workspace: Project root and Claude's working directory.
            settings: Configuration, checker definitions and rules from the database
                (`calu.store.ConfigStore.load`).
            store: Where the session records what it does.
            runner_factory: Creates a fresh runner for each started Claude session.
            emit: Receives every event for the clients, in order.
            project: Name of the project row the session belongs to, or None.
            checker_models: Replacements for the configured models, by key "command", "edit",
                "subagent", "narrator" and "summarizer" (tests pass fake models here).
            startup_warnings: Warnings about missing prerequisites, emitted as `log` warnings
                at every `start`, before the runner starts.
            snapshot_root: Where the optional write-set snapshots live (PLAN.md D31); None
                uses `calu.changes.default_root()`.
        """
        self.workspace = workspace
        self.settings = settings
        self.config = settings.config
        self.store = store
        self.project = project
        self.runner_factory = runner_factory
        self.runner: ClaudeRunner | None = None
        self._sink = emit
        self._models = dict(checker_models or {})
        self.startup_warnings = tuple(startup_warnings)
        self.snapshot_root = snapshot_root or default_root()
        """Where the write-set snapshots of this process live (PLAN.md D31)."""
        self.policy = ToolPolicy(
            overrides=self.config.tool_routes, default=self.config.default_route
        )
        self.rules = settings.rules
        self.checkers = {
            Route.COMMAND: self._checker("command", self.config.command_checker),
            Route.EDIT: self._checker("edit", self.config.edit_checker),
            Route.CHANGESET: self._checker("changeset", self.config.changeset_checker),
        }
        self.result_checker = ResultChecker(
            instructions=self._instructions("subagent-result"),
            model=resolve_model(self._models.get("subagent") or self.config.subagent_checker.model),
            usage_limits=UsageLimits(
                request_limit=self.config.subagent_checker.request_limit,
                output_tokens_limit=self.config.subagent_checker.output_tokens_limit,
            ),
            max_tokens=self.config.subagent_checker.max_tokens,
        )
        self.context = ContextBuilder(
            workspace,
            self.config.context,
            summarizer_model=resolve_model(
                self._models.get("summarizer") or self.config.context.summarizer_model
            ),
            summary=EMPTY_CHAT_SUMMARY,
            on_summary=self._store_summary,
        )
        self.narrator: Narrator | None = self._narrator()
        # agent id (None = main agent) -> thinking and text since that agent's last tool call
        self._claude_outputs: dict[str | None, list[ClaudeOutput]] = {}
        self.session_id: str | None = None
        """Claude Code's own session id, once known."""
        self.state: State = "stopped"
        self._escalations: dict[str, _Escalation] = {}
        self._local_ids = itertools.count(1)
        self._step_seq = itertools.count(1)
        self._step_ids: dict[str, int] = {}  # tool_use_id -> `steps` row id
        self._times: deque[StepTime] = deque(maxlen=STEP_TIMES_KEPT)
        self._time_index: dict[str, int] = {}  # tool_use_id -> position in `_times`
        self._clock_alarm: str | None = None  # a backward clock jump nobody has seen yet (D32)
        self._snapshots: SnapshotStore | None = None  # the optional write-set snapshot (D31)
        self._before: dict[str, tuple[str, int | None, str]] = {}
        self._overlapped: set[str] = set()
        self._changesets: dict[str, str] = {}  # tool_use_id -> the diff a commit would record
        self._running_tools: set[str] = set()  # tool_use_ids of approved, unfinished calls
        self._checking = 0  # checker and narrator calls in flight
        self._busy = False  # Claude works on a turn
        self._git_head_value: str | None = None
        self._git_head_time = float("-inf")
        self._pending_reasons: dict[str, tuple[str, str]] = {}
        self._tg: TaskGroup | None = None
        self._pending = 0  # background tasks (narration, turn storage, folding)
        self._quiet = anyio.Event()
        self._quiet.set()
        self._run_done = anyio.Event()
        self._run_done.set()
        self._run_error: str | None = None
        self._queue: list[tuple[QueuedMessage, str]] = []
        self._queue_ids = itertools.count(1)
        self._login_hint_sent = False

    # ------------------------------------------------------------------ settings

    def _instructions(self, name: str) -> str:
        """Return the instructions of a checker type, with the built-in default as fallback.

        Args:
            name: Checker type name ("command", "edit", "subagent-result", "narrator").

        Returns:
            The instructions from the database, or the ones shipped with calu if the database
            has no enabled checker type of that name.
        """
        stored = self.settings.instructions(name)
        if stored:
            return stored
        try:
            return default_instructions(name)
        except OSError:
            log.exception("reading the built-in %s instructions failed", name)
            return ""

    def _checker(self, name: str, cfg: CheckerConfig) -> Checker:
        """Create one step checker from its database definition and configuration.

        Args:
            name: Checker type name ("command" or "edit").
            cfg: Model and limits from the configuration.

        Returns:
            The checker.
        """
        return Checker(
            name=name,
            instructions=self._instructions(name),
            model=resolve_model(self._models.get(name) or cfg.model),
            workspace=self.workspace,
            rules=self.rules,
            times=self.step_times,
            usage_limits=UsageLimits(
                request_limit=cfg.request_limit, output_tokens_limit=cfg.output_tokens_limit
            ),
            max_tokens=cfg.max_tokens,
        )

    def _narrator(self) -> Narrator | None:
        """Create the narrator if narration is on.

        Returns:
            The narrator with the stored narrator instructions, or None if
            `context.narrate` is off.
        """
        if not self.config.context.narrate:
            return None
        return Narrator(
            instructions=self._instructions("narrator"),
            model=resolve_model(self._models.get("narrator") or self.config.context.narrator_model),
        )

    def apply_settings(self, settings: ProjectSettings) -> None:
        """Replace configuration, rules and checker instructions (after `config_changed`).

        Takes effect from the next tool call on: the checkers rebuild their agents when their
        instructions change, and the rules object is replaced as a whole. The settings of the
        running Claude session itself (model, effort, permission mode) are not changed, because
        Claude Code reads them when the session starts; they apply to the next `start`.

        Args:
            settings: The newly loaded settings.
        """
        claude = self.config.claude
        self.settings = settings
        self.config = settings.config.model_copy(update={"claude": claude})
        self.rules = settings.rules
        self.policy = ToolPolicy(
            overrides=self.config.tool_routes, default=self.config.default_route
        )
        self.context.config = self.config.context
        for route, name in (
            (Route.COMMAND, "command"),
            (Route.EDIT, "edit"),
            (Route.CHANGESET, "changeset"),
        ):
            checker = self.checkers[route]
            cfg = getattr(self.config, CHECKER_FIELDS[name])
            checker.instructions = self._instructions(name)
            checker.rules = self.rules  # the code tools follow the new read deny lists
            if name not in self._models:
                checker.model = resolve_model(cfg.model)
            checker.usage_limits = UsageLimits(
                request_limit=cfg.request_limit, output_tokens_limit=cfg.output_tokens_limit
            )
            checker.max_tokens = cfg.max_tokens
        self.result_checker.instructions = self._instructions("subagent-result")
        if "subagent" not in self._models:
            self.result_checker.model = resolve_model(self.config.subagent_checker.model)
        if self.config.context.narrate:
            if self.narrator is None:
                self.narrator = self._narrator()
            else:
                self.narrator.instructions = self._instructions("narrator")
                if "narrator" not in self._models:
                    self.narrator.model = resolve_model(self.config.context.narrator_model)
        else:
            self.narrator = None

    # ------------------------------------------------------------------ client requests

    def step_times(self) -> Sequence[StepTime]:
        """Return when the recent steps of this session ran, oldest first (PLAN.md D32).

        The checkers get this as a tool (`calu.checkers.tools.step_times`) and use it to
        attribute a file's change time to a step. Only the last `STEP_TIMES_KEPT` steps are
        kept, because the list exists for attributing recent changes, not as a history: the
        database holds the history.

        Returns:
            The steps, oldest first; empty before the first tool call.
        """
        return tuple(self._times)

    def _finish_time(self, tool_use_id: str) -> None:
        """Note that a tool call ended, so its time window is closed.

        Args:
            tool_use_id: Claude's id of the call; an unknown id is ignored (its step may have
                fallen out of the kept window).
        """
        seq = self._time_index.pop(tool_use_id, None)
        if seq is None:
            return
        for index, step in enumerate(self._times):
            if step.seq == seq:
                self._times[index] = replace(step, finished_at=datetime.now(UTC))
                return

    def _summarize_time(self, seq: int, summary: str) -> None:
        """Attach the checker's summary to a recorded step time, for `step_times`.

        Args:
            seq: Sequence number of the step.
            summary: What the checker said the step does.
        """
        if not summary:
            return
        for index, step in enumerate(self._times):
            if step.seq == seq:
                self._times[index] = replace(step, summary=summary)
                return

    async def _prepare_snapshots(self) -> None:
        """Create this session's snapshot repository if the write-set snapshot is on (D31).

        Side effects: sets `_snapshots`, creates a git directory under `snapshot_root` named
        after the process, and takes no snapshot yet. A failure is reported as a warning and
        leaves the session without snapshots, because a missing write set must not stop a run.
        """
        self._snapshots = None
        if not self.config.changes.snapshot:
            return
        changes = self.config.changes
        store = SnapshotStore(
            workspace=self.workspace,
            root=self.snapshot_root / f"{os.getpid()}-{int(time.time())}",
            max_file_bytes=changes.max_file_bytes,
            max_patch_bytes=changes.max_patch_bytes,
            always_capture=tuple(changes.always_capture),
        )
        try:
            await store.prepare()
        except SnapshotError as exc:
            await self._emit_safely(
                Log(level="warning", message=f"no write sets in this session: {exc}")
            )
            return
        self._snapshots = store

    async def _discard_snapshots(self) -> None:
        """Delete this session's snapshot repository when the session ends (D31).

        The trees are the source of a revert of a *recent* step, so they are of no use once the
        session is over, and keeping them would let `snapshot_root` grow without bound. The
        write sets themselves stay in the database.

        Side effects: removes the directory and clears `_snapshots`. A failure is logged only.
        """
        store, self._snapshots = self._snapshots, None
        if store is None:
            return
        try:
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(store.root, ignore_errors=True))
        except OSError as exc:
            log.warning("could not remove the snapshots in %s: %s", store.root, exc)

    async def _snapshot_before(self, step: Step, step_id: int | None, summary: str) -> None:
        """Snapshot the project directory before an approved command runs (D31).

        Side effect: remembers the tree for `_snapshot_after`, and marks every open window as
        overlapping if more than one command runs at a time, because the write set can then not
        be attributed to one of them.

        Args:
            step: The tool call that is about to run.
            step_id: Its `steps` row, or None if it could not be stored.
            summary: What the checker said the step does, for the honesty comparison.
        """
        if self._snapshots is None or not step.tool_use_id:
            return
        try:
            tree = await self._snapshots.take()
        except SnapshotError as exc:
            log.warning("no snapshot before %s: %s", step.tool_name, exc)
            return
        if self._before:
            self._overlapped.update(self._before)
            self._overlapped.add(step.tool_use_id)
        self._before[step.tool_use_id] = (tree, step_id, summary)

    async def _snapshot_after(self, tool_use_id: str) -> None:
        """Measure a finished command's write set if the review has not done it already (D31).

        The PostToolUse hook for Bash measures and reviews while Claude waits, which is the
        better moment; this is the fallback for a call that never reaches that hook (a denied
        call, a runner without hooks) and does nothing when the measurement already ran.

        Args:
            tool_use_id: Claude's id of the finished call.
        """
        await self._measure_write_set(tool_use_id)

    async def _measure_write_set(self, tool_use_id: str) -> _Measured | None:
        """Snapshot the project directory after a command and record what changed (D31).

        Takes the "after" snapshot, compares it with the "before" one, records the write set on
        the `steps` row and reports it as a `log` event — a warning if the command wrote paths
        that the checker's summary did not mention, which is the dishonesty signal of D31.

        Idempotent: the "before" entry is consumed, so a second call for the same id returns
        None and records nothing.

        Args:
            tool_use_id: Claude's id of the finished call.

        Returns:
            What was measured, or None if this call had no snapshot (snapshots off, the call was
            not one the command checker judges, or it was measured already).
        """
        opened = self._before.pop(tool_use_id, None)
        overlapping = tool_use_id in self._overlapped
        self._overlapped.discard(tool_use_id)
        if opened is None or self._snapshots is None:
            return None
        before, step_id, summary = opened
        try:
            after = await self._snapshots.take()
            change = await self._snapshots.changes(before, after)
        except SnapshotError as exc:
            log.warning("no write set for %s: %s", tool_use_id, exc)
            return None
        change = replace(change, overlapping=overlapping)
        measured = _Measured(
            change=change, before=before, after=after, step_id=step_id, summary=summary
        )
        if not change.changes:
            return measured
        undeclared = () if overlapping else change.undeclared(summary)
        if step_id is not None:
            await self._store_call(
                "recording a write set",
                self.store.record_changes(
                    step_id, before, after, change.paths, undeclared, overlapping
                ),
            )
        if undeclared:
            await self._emit_safely(
                Log(
                    level="warning",
                    message=(
                        f"the command wrote {len(undeclared)} path(s) its summary did not "
                        f"mention: {', '.join(undeclared[:10])}"
                    ),
                )
            )
        else:
            await self._emit_safely(Log(level="info", message=f"wrote {change.summary()}"))
        return measured

    async def review_write_set(self, step: Step) -> str:
        """Review what a finished command wrote, and undo it if the review refuses (D31).

        This is the second half of the write-set snapshot: the step checker judged the command
        before it ran, with no way of knowing what it would write; this judges the change
        itself, with the patch in front of it. Only reached when `changes.review` is on, which
        needs `changes.snapshot`.

        A DENY reverts the change (`git apply --reverse` from calu's own snapshot) and tells
        Claude what was undone and why; a revert that does not apply is reported as such, so
        Claude is never told that something was undone when it was not. An ESCALATE asks the
        human, and a denial from the human reverts in the same way. A comment on an accepted
        change is passed on.

        Side effects: measures and records the write set (see `_measure_write_set`), may change
        files in the workspace, and may open an escalation.

        Args:
            step: The tool call that has just run.

        Returns:
            A note for Claude, or "" if there is nothing to say.
        """
        measured = await self._measure_write_set(step.tool_use_id or "")
        if measured is None or not self.config.changes.review:
            return ""
        change = measured.change
        if not change.changes or change.overlapping or self._snapshots is None:
            # Overlapping windows hold the writes of more than one call, so a review of them
            # could blame or revert the wrong command.
            return ""
        outcome = await self._review_change(step, change, measured.step_id)
        decision = outcome.decision
        if decision is Decision.ESCALATE:
            self._pending_reasons[step.tool_use_id or ""] = (outcome.reason, "write_set")
            answer = await self.ask(step)
            if answer.cancelled:
                return ""
            if answer.allow:
                return WRITE_SET_NOTE.format(note=answer.note) if answer.note else ""
            return await self._undo(measured, answer.message or outcome.reason)
        if decision is Decision.DENY:
            return await self._undo(measured, outcome.reason)
        return WRITE_SET_NOTE.format(note=outcome.comment) if outcome.comment.strip() else ""

    async def _review_change(
        self, step: Step, change: ChangeSet, step_id: int | None
    ) -> CheckDecision:
        """Let the changeset checker judge what a command wrote.

        Side effect: records the model call in `checker_runs`, so that the review has the same
        audit trail as every other check (who judged, with which model and version, how long it
        took).

        Args:
            step: The command that wrote it.
            change: Its write set, with the patch.
            step_id: The `steps` row the checker run belongs to, or None if it was not stored.

        Returns:
            The checker's decision; a failure of the checker yields ESCALATE, so a human
            decides instead of the change standing unreviewed.
        """
        checker = self.checkers[Route.CHANGESET]
        extra = WRITE_SET_PROMPT.format(summary=change.summary(), patch=change.patch)
        self._checking += 1
        await self._refresh_state()
        try:
            with anyio.fail_after(CHECKER_DEADLINE_S):
                run = await checker.run(step, self.context.check_context(), extra=extra)
            if step_id is not None:
                await self._store_call(
                    "recording the review of a write set",
                    self.store.add_checker_run(
                        step_id, self._checker_run(checker, Route.CHANGESET.value, run)
                    ),
                )
            return run.decision
        except TimeoutError:
            return CheckDecision(
                decision=Decision.ESCALATE,
                reason=f"The review of the write set did not answer within {CHECKER_DEADLINE_S} s.",
                step_summary=change.summary(),
            )
        except Exception as exc:
            log.exception("reviewing a write set failed")
            return CheckDecision(
                decision=Decision.ESCALATE,
                reason=f"The review of the write set failed ({type(exc).__name__}: {exc}).",
                step_summary=change.summary(),
            )
        finally:
            self._checking -= 1
            await self._refresh_state()

    async def _undo(self, measured: _Measured, reason: str) -> str:
        """Undo a command's writes and describe the outcome for Claude.

        Side effect: changes files in the workspace. Nothing is lost: both snapshots stay in
        calu's snapshot repository until the session ends.

        Args:
            measured: The write set and the two snapshots.
            reason: Why the change was refused; Claude reads it.

        Returns:
            A note saying what was undone, or that the revert did not apply and Claude has to
            undo the change itself.
        """
        change = measured.change
        reverted = False
        if self._snapshots is not None:
            try:
                reverted = await self._snapshots.revert(
                    measured.before, measured.after, change.paths
                )
            except SnapshotError as exc:
                log.warning("the revert failed: %s", exc)
        await self._emit_safely(
            Log(
                level="warning",
                message=(
                    f"the write set of a command was refused and "
                    f"{'reverted' if reverted else 'could NOT be reverted'}: {change.summary()}"
                ),
            )
        )
        template = WRITE_SET_REVERTED if reverted else WRITE_SET_NOT_REVERTED
        return template.format(reason=reason, paths=", ".join(change.paths[:20]))

    async def clock_jumped(self, jump: ClockJump) -> None:
        """React to a move of the wall clock that `calu.clock.ClockWatch` detected (D32).

        A forward jump is only reported, because a first time-server sync legitimately jumps
        forward. A **backward** jump breaks the attribution of file changes to steps, so the
        session escalates its next tool call with the jump as the reason: nothing runs
        unsupervised until a human has seen it. The human's answer to that escalation clears the
        alarm either way — allowing means "the clock change was legitimate, carry on".

        Limitation: an idle session has no next tool call, so the jump reaches the clients as a
        log event at once and escalates only when Claude acts again.

        Side effects: emits a `log` event; for a backward jump, arms the alarm that `_decide`
        reads.

        Args:
            jump: The detected jump.
        """
        if not jump.backward:
            await self._emit_safely(
                Log(level="warning", message=f"clock: {jump.describe()} (not an escalation)")
            )
            return
        self._clock_alarm = (
            f"The clock of the calu host was moved: {jump.describe()}. Until a human has seen "
            "this, calu cannot tell which step changed which file, because that attribution "
            "compares a file's change time with the times of the steps. Check what moved the "
            "clock before you let this session continue."
        )
        await self._emit_safely(Log(level="error", message=f"clock: {jump.describe()}"))
        await self._refresh_state()

    async def accepts_token(self, token: str) -> bool:
        """Tell whether a client may attach to this session with `token` (PLAN.md D30).

        The protocol server checks the token it printed itself and asks this only for a token it
        does not know, so that attaching with the printed token keeps working even if the
        database is unreachable. Every token an operator minted for this session counts
        (`calu token mint`), until it is revoked.

        Args:
            token: The token the client sent in `initialize`.

        Returns:
            True if the session has a non-revoked token with this hash.

        Raises:
            psycopg.Error: If the database cannot be asked; the caller must not report that as a
                wrong token.
        """
        return await self.store.client_token_ok(token_hash(token))

    def initialize_result(self) -> InitializeResult:
        """Return the answer to the `initialize` request of a client.

        Returns:
            Protocol and calu version, the workspace, the project and the id of the `sessions`
            row; the caller fills `last_seq` from the bus.
        """
        return InitializeResult(
            core_version=__version__,
            workspace=str(self.workspace),
            project=self.project,
            calu_session_id=self.store.session_id,
        )

    def snapshot(self) -> SessionSnapshot:
        """Return everything a client needs to show the session as it is now (`state.get`).

        The open escalations are included with the request events that asked for them, so a
        client that connects while the session waits can answer them. The caller fills
        `last_seq` from the bus.

        Returns:
            The snapshot.
        """
        return SessionSnapshot(
            state=self.state,
            claude_session_id=self.session_id,
            calu_session_id=self.store.session_id,
            workspace=str(self.workspace),
            project=self.project,
            model=self.config.claude.model,
            effort=self.config.claude.effort,
            agents=list(self.runner.agents.values()) if self.runner else [],
            escalations=[
                esc.request for esc in self._escalations.values() if esc.request is not None
            ],
            queue=[message for message, _ in self._queue],
            auto_escalate=self.config.auto_escalate,
        )

    async def recent_sessions(self, limit: int = 20) -> list[SessionSummary]:
        """List the stored sessions of this workspace, newest first (`session.list`).

        Args:
            limit: Maximum number of sessions returned.

        Returns:
            The sessions from the database; an empty list if the database cannot be read (the
            error is logged).
        """
        try:
            return await self.store.recent_sessions(str(self.workspace), limit)
        except Exception:
            log.exception("listing the stored sessions failed")
            return []

    async def history(
        self, claude_session_id: str | None = None, limit: int = 500
    ) -> SessionHistoryResult:
        """Return the recorded conversation of a session (`session.history`).

        A client that joins a session which started before it rebuilds its view from this: the
        event buffer of the bus only covers the running process. The rows come from the
        database, so the history of an earlier session survives a restart of calu.

        Args:
            claude_session_id: Which session; None is the one that runs now.
            limit: Maximum number of steps returned (the newest ones).

        Returns:
            The turns, the steps and the chat summary; empty fields if the database cannot be
            read (the error is logged).
        """
        try:
            return await self.store.load_history(claude_session_id, limit)
        except Exception:
            log.exception("reading the history of %s failed", claude_session_id or "this session")
            return SessionHistoryResult(claude_session_id=claude_session_id)

    async def escalation_context(self, escalation_id: str) -> EscalationContextResult | None:
        """Return what the checker saw and said when it escalated a step.

        A client shows this next to the question: the condensed conversation and the chat
        summary of that moment, the file an edit would change, and the checker runs with their
        model and version. It comes from the `escalations` row, so it still answers "what did
        the checker know?" after the session has moved on.

        Args:
            escalation_id: Id from the `escalation.request` event.

        Returns:
            The stored context, or None if no escalation has that id (or it is not a number, or
            the database cannot be read; the error is logged).
        """
        try:
            return await self.store.escalation_context(int(escalation_id))
        except ValueError:
            log.warning("not an escalation id: %r", escalation_id)
            return None
        except Exception:
            log.exception("reading the context of escalation %s failed", escalation_id)
            return None

    def checker_definitions(self) -> CheckerListResult:
        """Return the checker definitions this session uses (`checkers.get`).

        A client shows them so that a human can see which instructions, which model and which
        version judged a step. Changing them needs other credentials than a session has
        (PLAN.md §6.2), so this is read-only.

        Returns:
            The definitions by name, with their instructions.
        """
        return CheckerListResult(
            checkers=[
                CheckerInfo(
                    name=definition.name,
                    route=definition.route,
                    model=definition.model,
                    version=definition.version,
                    instructions=definition.instructions,
                )
                for definition in sorted(self.settings.checkers.values(), key=lambda d: d.name)
            ]
        )

    def checker_context(self) -> CheckerContextResult:
        """Return the conversation context as the checkers currently see it.

        Returns:
            The folded chat summary and the rendered recent turns.
        """
        return CheckerContextResult(chat_summary=self.context.summary, recent=self.context.recent())

    async def start(
        self,
        tg: TaskGroup,
        resume_id: str | None = None,
        model: str | None = None,
        effort: Effort | Literal["default"] | None = None,
    ) -> None:
        """Start a Claude session in `tg` and return once it accepts messages.

        Side effects: creates a `sessions` row (state "starting"), restores the turns and the
        chat summary of the resumed session from the database, emits `startup_warnings` as
        `log` warnings, creates a runner and starts it as a task in `tg`.

        Args:
            tg: Task group that runs the session; it must outlive the session.
            resume_id: Claude session id to resume, or None for a new session.
            model: Claude model alias or id for this and later sessions; None keeps the
                configured model.
            effort: Reasoning effort for this and later sessions; "default" uses Claude Code's
                default effort; None keeps the configured effort.

        Raises:
            SessionError: If a session is already running, if the runner ended before it was
                ready (the message names the runner's error), or if it was not ready within
                `START_TIMEOUT_S` seconds (the runner is stopped then).
        """
        self._claude_outputs.clear()  # text of an earlier session must not reach this one
        if self.state not in ("stopped", "failed") or not self._run_done.is_set():
            raise SessionError(f"a session is already {self.state}")
        overrides: dict[str, str | None] = {}
        if model is not None:
            overrides["model"] = model
        if effort is not None:
            overrides["effort"] = None if effort == "default" else effort
        if overrides:
            self.config.claude = self.config.claude.model_copy(update=overrides)
        self._tg = tg
        self._run_done = anyio.Event()
        self._run_error = None
        self.session_id = None
        self._busy = False
        self._running_tools.clear()
        self._step_ids.clear()
        self._times.clear()
        self._time_index.clear()
        self._before.clear()
        self._overlapped.clear()
        self._changesets.clear()
        await self._prepare_snapshots()
        self._pending_reasons.clear()
        self._step_seq = itertools.count(1)
        self._checking = 0
        self.state = "stopped"
        await self._store_call("creating the session row", self.store.start(self._identity()))
        await self._set_state("starting")
        await self._restore_context(resume_id)
        for warning in self.startup_warnings:
            await self.emit(Log(level="warning", message=warning))
        runner = self.runner = self.runner_factory()
        tg.start_soon(self._run, runner, resume_id)

        ready = anyio.Event()

        async def wait_ready() -> None:
            """Wait until the runner is ready, then set `ready`."""
            await runner.wait_ready()
            ready.set()

        async with anyio.create_task_group() as waiters:
            waiters.start_soon(wait_ready)
            with anyio.move_on_after(START_TIMEOUT_S):
                await _first_of(ready, self._run_done)
            waiters.cancel_scope.cancel()

        if self._run_done.is_set():
            raise SessionError(f"Claude session ended during start: {self._run_error or 'exit'}")
        if not ready.is_set():
            await runner.stop()
            raise SessionError(f"Claude session was not ready within {START_TIMEOUT_S} s")
        # As early as possible: a session that stops before its first turn (an escalation that
        # timed out, for example) must still be resumable by its Claude session id (D27).
        await self._record_claude_session(runner.session_id)
        await self._set_state("idle")

    async def _run(self, runner: ClaudeRunner, resume_id: str | None) -> None:
        """Run `runner` until it ends; then mark the session as stopped or failed.

        Never raises: runner errors are logged and reported as a `log` event.

        Args:
            runner: The runner of this session.
            resume_id: Claude session id to resume, or None.
        """
        try:
            await runner.run(self, self._on_runner_event, resume_id)
        except Exception as exc:
            log.exception("Claude session failed")
            self._run_error = f"{type(exc).__name__}: {exc}"
            hints = CLAUDE_SETUP_HINT
            if not self.config.claude.use_bedrock:
                hints += f" {BEDROCK_HINT}"
            await self._emit_safely(
                Log(level="error", message=f"Claude session failed: {exc} {hints}")
            )
        finally:
            self._run_done.set()  # first, so that `stop` never waits for a broken client
            with anyio.CancelScope(shield=True):
                await self._cancel_escalations("The Claude session ended.")
                if self._queue:  # messages Claude did not read are lost with the session
                    self._queue.clear()
                    await self._emit_safely(ChatQueue(messages=[]))
                await self._wait_quiet()  # the last turn is recorded before the session ends
                final: State = "failed" if self._run_error else "stopped"
                await self._set_state_safely(final)
                await self._store_call("recording the end of the session", self.store.end(final))
                await self._discard_snapshots()

    async def stop(self) -> None:
        """Stop the Claude session and wait until its runner has ended.

        Open escalations are cancelled (their tool calls are denied). Claude's text that was
        not narrated yet is dropped. Waits for the background work of the last turn (narration,
        storing the turn, folding), at most `BACKGROUND_TIMEOUT_S` seconds. Does nothing if no
        session runs and nothing is pending.
        """
        self._claude_outputs.clear()
        if not self._run_done.is_set():
            await self._cancel_escalations("The session was stopped.")
            assert self.runner is not None
            await self.runner.stop()
            await self._run_done.wait()
        await self._wait_quiet()
        if self.state not in ("stopped", "failed"):
            await self._set_state_safely("stopped")

    async def interrupt(self) -> None:
        """Stop Claude's current turn; the session stays usable.

        Raises:
            SessionError: If no session runs.
        """
        runner = self._require_running()
        self._claude_outputs.pop(None, None)  # the main agent's text of the interrupted turn
        # Pending reviews of foreground subagent results belong to the interrupted turn; the
        # hooks that wait for them may not be cancelled by Claude Code, so they are ended here
        # (as cancelled: no note is sent to Claude). Background subagents keep running.
        for esc_id, esc in list(self._escalations.items()):
            if esc.step.tool_name == "SubagentResult" and not esc.step.tool_input.get("background"):
                self._escalations.pop(esc_id, None)
                esc.answer = HumanAnswer(
                    allow=False, message="The turn was interrupted.", cancelled=True
                )
                esc.done.set()
                if esc.db_id is not None:
                    await self._store_call(
                        "cancelling an escalation",
                        self.store.close_escalation(esc.db_id, "the turn was interrupted"),
                    )
        await runner.interrupt()

    async def send_chat(self, params: ChatSendParams) -> None:
        """Send a chat message to the main agent, or relayed to a background subagent.

        "queued" messages are read after the current turn, "inject" messages as soon as the
        running tool calls finish. A message to a subagent is wrapped in a request to the main
        agent to relay it with SendMessage and is always injected.

        Args:
            params: Text, delivery and target of the message.

        Raises:
            SessionError: If no session runs, the target subagent is unknown, or it is not a
                running background subagent.
        """
        runner = self._require_running()
        priority: Literal["next", "later"] = "next" if params.delivery == "inject" else "later"
        text = params.text
        if params.to != "main":
            agent = self._find_agent(params.to)
            text = RELAY_TEMPLATE.format(
                name=agent.description or agent.agent_id,
                to=agent.sdk_agent_id or agent.agent_id,
                text=params.text,
            )
            priority = "next"
        await runner.send(text, priority)
        queued = QueuedMessage(
            id=f"q{next(self._queue_ids)}", text=params.text, delivery=params.delivery, to=params.to
        )
        self._queue.append((queued, text))
        await self._emit_queue()
        await self._mark_busy()

    async def _emit_queue(self) -> None:
        """Send `chat.queue` with the messages that Claude has not taken in yet."""
        await self.emit(ChatQueue(messages=[message for message, _ in self._queue]))

    async def _take_from_queue(self, text: str) -> None:
        """Remove the oldest queued message whose sent text is `text`, then send `chat.queue`.

        Does nothing if no queued message matches.

        Args:
            text: The text of a user message that Claude took in.
        """
        for i, (_, sent) in enumerate(self._queue):
            if sent.strip() == text.strip():
                del self._queue[i]
                await self._emit_queue()
                return

    async def add_comment(self, params: CommentAddParams) -> Comment:
        """Build a `Comment` from a selection, inject it into Claude's session and store it.

        A comment reaches Claude as a user message with priority "next", so it is read as soon
        as the running tool calls finish. The comment is also recorded in the database, where it
        can serve as training or evaluation data for the checkers.

        Args:
            params: Selection, document text(s) and comment text from the client.

        Returns:
            The comment as sent to Claude.

        Raises:
            SessionError: If no session runs (the comment is then neither sent nor stored).
            ValueError: If the selection lies outside the document, or a diff comment lacks
                `before_text`.
        """
        runner = self._require_running()
        if params.kind == "diff":
            if params.before_text is None:
                raise ValueError("a diff comment needs before_text")
            before, after = map_selection(
                params.before_text,
                params.after_text,
                params.side,
                params.start_line,
                params.end_line,
            )
            comment = Comment(
                kind="diff",
                path=params.path,
                before=before,
                after=after,
                text=params.text,
                ref=params.ref,
            )
        else:
            after = excerpt(split_lines(params.after_text), params.start_line, params.end_line)
            comment = Comment(kind="file", path=params.path, after=after, text=params.text)
        await runner.send(format_comment(comment), "next")
        await self._mark_busy()
        await self._store_call(
            "storing a comment", self.store.add_comment(comment, self.context.turn_number)
        )
        return comment

    async def decide_escalation(
        self, escalation_id: str, decision: str, note: str | None, client: str = ""
    ) -> None:
        """Resolve an open escalation from a client. Only the first decision counts.

        The decision is recorded in the `escalations` row as the resolution it is, with
        `resolved_by` saying that a client of this session decided and which one (PLAN.md D28).
        A client needs no database login for that: it authenticates to the session with its
        token, and the session speaks to the database for it. An operator who resolves the row
        in the database instead reaches the session through `note_resolution` or its poll.

        Args:
            escalation_id: Id from the `escalation.request` event.
            decision: "allow" or "deny".
            note: Optional text from the user; Claude receives it with a denial.
            client: Name of the client that decided, for the record.

        Raises:
            SessionError: If no escalation with this id is open (unknown, or already decided).
        """
        esc = self._escalations.pop(escalation_id, None)
        if esc is None:
            raise SessionError(f"no open escalation {escalation_id!r}")
        allow = decision == "allow"
        message = "The user denied this step."
        if note:
            message += f" Note from the user: {note}"
        esc.answer = HumanAnswer(allow=allow, message=message, note=note or "")
        esc.done.set()
        if esc.db_id is not None:
            await self._record_decision(esc.db_id, allow, note or "", client)

    async def _record_decision(
        self, escalation_id: int, allow: bool, note: str, client: str
    ) -> None:
        """Write a client's decision to the `escalations` row; never raises.

        If the database refuses the decision — the row was resolved meanwhile, or this session
        may not decide it — the row is cancelled with a note instead, so that it does not stay
        open after the step has already run or been denied.

        Args:
            escalation_id: Id of the `escalations` row.
            allow: Whether the step may run.
            note: The human's note.
            client: Name of the client that decided.
        """
        try:
            recorded = await self.store.decide_escalation(escalation_id, allow, note, client)
        except Exception as exc:
            log.exception("recording a client decision failed")
            await self._emit_safely(
                Log(level="error", message=f"recording a client decision failed: {exc}")
            )
            return
        if recorded:
            return
        decided = "allowed" if allow else "denied"
        await self._store_call(
            "cancelling an escalation",
            self.store.close_escalation(
                escalation_id,
                f"{decided} by the client {client or 'unknown'}, but the row could not be "
                "resolved (already resolved, or this session may not decide it)",
            ),
        )

    def note_resolution(self, escalation_id: int) -> None:
        """Wake the session so it reads a resolved escalation at once.

        Called with the id from an `escalation_resolved` notification (`calu.db.notify`). An
        unknown id is ignored; without this call the session notices the resolution with its
        next poll (`ESCALATION_POLL_S`).

        Args:
            escalation_id: Id of the resolved `escalations` row.
        """
        for esc in self._escalations.values():
            if esc.db_id == escalation_id:
                esc.wake.set()

    # ------------------------------------------------------------------ ToolGate

    async def pre_tool_use(self, step: Step) -> GateResult:
        """Decide about a tool call before it runs (ToolGate).

        Side effects: records the step in the database and in the context, emits `edit.diff`
        (edits) and `check.result`, and remembers the reason of an escalation for `ask`.

        Args:
            step: The tool call.

        Returns:
            ALLOW, DENY (with the reason for Claude) or ASK. Any internal error yields ASK, so
            a human decides instead of the call running unchecked.
        """
        pending = _PendingNarration(self._claude_outputs.pop(step.agent_id, []))
        step_id = await self._add_step(step)
        try:
            decided = await self._decide(step, pending)
        except Exception as exc:
            log.exception("pre_tool_use failed")
            decided = _Decided(
                route=self.policy.route(step.tool_name),
                decision=CheckDecision(
                    decision=Decision.ESCALATE,
                    reason=(
                        f"Checking failed with an internal error ({type(exc).__name__}: {exc})."
                    ),
                    step_summary=label_for_unchecked(step.tool_name, step.tool_input),
                ),
                source="internal",
            )
        route, result, source = decided.route, decided.decision, decided.source
        comment = result.comment.strip() if result.decision.approves else ""
        if comment and self._comment_already_sent(comment, step.agent_id):
            comment = ""  # the same remark was already sent in this turn
        record = StepRecord(
            tool_name=step.tool_name,
            agent_id=step.agent_id,
            decision=result.decision.value,
            summary=result.step_summary,
            comment=comment,
            narration=pending.text or "",
        )
        self.context.step(record)
        if step.tool_use_id:
            self._summarize_time(self._time_index.get(step.tool_use_id, 0), result.step_summary)
        if pending.outputs and pending.text is None and self.narrator and self._tg is not None:
            # No checker ran (rules, policy): narrate for the context without delaying the call.
            self._spawn(self._narrate_record, record, pending.outputs)
        if step_id is not None:
            if decided.run is not None:
                await self._store_call(
                    "recording a checker run",
                    self.store.add_checker_run(
                        step_id, self._checker_run(self.checkers[route], route.value, decided.run)
                    ),
                )
            await self._store_call(
                "recording a decision",
                self.store.decide_step(
                    step_id,
                    StepDecision(
                        decision=result.decision.value.lower(),
                        reason=result.reason,
                        summary=result.step_summary,
                        narration=pending.text or "",
                        rules_outcome=decided.rules,
                    ),
                ),
            )
        await self._emit_safely(
            CheckResultEvent(
                tool_use_id=step.tool_use_id,
                agent_id=step.agent_id,
                tool_name=step.tool_name,
                route=route,
                decision=result.decision,
                reason=result.reason,
                step_summary=result.step_summary,
                comment=comment,
                git_head=await self._git_head(),
            )
        )
        if result.decision is Decision.ESCALATE and step.tool_use_id:
            self._pending_reasons[step.tool_use_id] = (result.reason, source)
        if result.decision.approves and step.tool_use_id:
            self._running_tools.add(step.tool_use_id)
        await self._refresh_state()
        verdict = {
            Decision.PASS: Verdict.PASS,
            Decision.ALLOW: Verdict.ALLOW,
            Decision.DENY: Verdict.DENY,
            Decision.ESCALATE: Verdict.ASK,
        }[result.decision]
        if result.decision.approves and route in (Route.COMMAND, Route.CHANGESET):
            # The command runs as soon as this returns, so this is the "before" of its write set
            # (D31). Edits need no snapshot: their content came through the gate.
            await self._snapshot_before(step, step_id, result.step_summary)
        context = ""
        if comment:
            context = CHECKER_COMMENT_TEMPLATE.format(route=route.value, comment=comment)
        return GateResult(verdict=verdict, reason=result.reason, context=context)

    async def ask(self, step: Step) -> HumanAnswer:
        """Ask the human about `step` and wait for the answer, without a time limit (ToolGate).

        Side effects: opens an `escalations` row, emits `escalation.request`, sets the state to
        `escalated`, and emits `escalation.resolved` when the escalation ends — also when it is
        cancelled (stop, interrupt, end of the session), with decision "cancelled".

        Args:
            step: The tool call that needs a decision.

        Returns:
            The human's answer. A cancelled escalation counts as a denial. If the escalation
            cannot be stored, the step is denied with `DB_UNREACHABLE`, because no human could
            see it.
        """
        reason, source = self._pending_reasons.pop(
            step.tool_use_id or "", ("Approval required.", "internal")
        )
        rendered = _render_safely(step, self.workspace)
        db_id = await self._open_escalation(step, reason, rendered)
        if db_id is None:
            return HumanAnswer(allow=False, message=DB_UNREACHABLE)
        escalation_id = str(db_id)
        esc = _Escalation(step=step, reason=reason, source=source, db_id=db_id)
        esc.request = EscalationRequest(
            escalation_id=escalation_id,
            tool_use_id=step.tool_use_id,
            agent_id=step.agent_id,
            tool_name=step.tool_name,
            tool_input=step.tool_input,
            reason=reason,
            rendered=rendered,
        )
        self._escalations[escalation_id] = esc
        try:
            await self._emit_safely(esc.request)
            await self._refresh_state()
            await self._wait_for_decision(esc)
        finally:
            self._escalations.pop(escalation_id, None)
            with anyio.CancelScope(shield=True):
                answer = esc.answer
                decision = "cancelled" if answer is None else "allow" if answer.allow else "deny"
                await self._emit_safely(
                    EscalationResolved(
                        escalation_id=escalation_id,
                        decision=decision,
                        note=None
                        if answer is None
                        else (answer.note or None)
                        if answer.allow
                        else answer.message,
                    )
                )
                await self._refresh_state()
        assert esc.answer is not None
        if esc.answer.allow and step.tool_use_id:
            self._running_tools.add(step.tool_use_id)
            await self._refresh_state()
        return esc.answer

    async def _wait_for_decision(self, esc: _Escalation) -> None:
        """Wait until `esc` is decided locally or in the database.

        Reads the escalation row every `ESCALATION_POLL_S` seconds, and at once when
        `note_resolution` was called for it, until it is no longer open; a local
        `decide_escalation` ends the wait as well. Store errors are logged and the poll
        continues, so a short database outage does not turn into a denial.

        Args:
            esc: The open escalation; its `answer` and `done` are set by this call or by
                `decide_escalation`.
        """
        timeout = self.config.escalation_timeout_s or float("inf")
        if esc.db_id is None:
            with anyio.move_on_after(timeout):
                await esc.done.wait()
            if esc.answer is None:
                await self._escalation_timed_out(esc)
            return

        async def poll() -> None:
            """Read the escalation row until it is resolved."""
            while True:
                wake = esc.wake
                with anyio.move_on_after(ESCALATION_POLL_S):
                    await wake.wait()
                esc.wake = anyio.Event()
                assert esc.db_id is not None
                try:
                    row = await self.store.escalation(esc.db_id)
                except Exception:
                    log.exception("reading an escalation failed")
                    continue
                if row.open:
                    continue
                message = (
                    "The user denied this step."
                    if not row.allowed
                    else "The user allowed this step."
                )
                if row.note:
                    message += f" Note from the user: {row.note}"
                esc.answer = HumanAnswer(allow=row.allowed, message=message, note=row.note)
                esc.done.set()
                return

        async with anyio.create_task_group() as tg:
            tg.start_soon(poll)
            with anyio.move_on_after(timeout):
                await esc.done.wait()
            tg.cancel_scope.cancel()
        if esc.answer is None:
            await self._escalation_timed_out(esc)

    async def _escalation_timed_out(self, esc: _Escalation) -> None:
        """Deny the step of an escalation nobody answered, and stop the session (D27).

        The session gives the machine and the Claude session back instead of waiting for ever.
        Claude's session id is in the database, so `calu run --resume <id>` takes the
        conversation up again; the escalation row is cancelled with the reason. The stop runs in
        its own task, because this call happens inside the tool call that is being held: waiting
        for the runner to end here would deadlock.

        Args:
            esc: The escalation that was not answered.
        """
        seconds = self.config.escalation_timeout_s
        resume = f"calu run --resume {self.session_id}" if self.session_id else "calu sessions"
        message = (
            f"No decision within {seconds / 3600:.1f} h. The step was denied and the session "
            f"stops; take the conversation up again with `{resume}`."
        )
        esc.answer = HumanAnswer(allow=False, message=message)
        esc.done.set()
        if esc.db_id is not None:
            await self._store_call(
                "cancelling an escalation", self.store.close_escalation(esc.db_id, message)
            )
        await self._emit_safely(Log(level="error", message=message))
        if self._tg is not None:
            # Not a tracked background task: `stop` waits for those, and this one calls it.
            self._tg.start_soon(self._stop_after_timeout)

    async def _stop_after_timeout(self) -> None:
        """Stop the session after an escalation timed out; never raises."""
        try:
            await self.stop()
        except Exception:
            log.exception("stopping the session after an escalation timeout failed")

    async def review_subagent_result(self, result: SubagentResult) -> str:
        """Review a subagent's final result against the conversation (ToolGate).

        Side effects: records a step and a checker run, emits `check.result` (route "subagent";
        the triage note in `comment`), records the review in the context, and for ESCALATE asks
        the human like a tool call (`escalation.request` with tool name "SubagentResult").

        Args:
            result: The subagent's task and final result.

        Returns:
            The note for the main agent ("" if the result was accepted without remarks or the
            review is disabled by `check_subagent_results = false`).
        """
        # The subagent's text after its last tool call is its result, reviewed below.
        self._claude_outputs.pop(result.agent_id, None)
        if not self.config.check_subagent_results:
            return ""
        label = result.description or "subagent"
        step = Step(
            tool_name="SubagentResult",
            tool_input={
                "description": label,
                "background": result.background,
                "result": result.result[:4_000],
            },
            tool_use_id=f"result-{result.agent_id}",
            agent_id=result.agent_id,
            cwd=str(self.workspace),
        )
        step_id = await self._add_step(step)
        outcome = await self._review(result)
        review = outcome.review
        if self.config.auto_escalate.subagents and review.verdict is not ResultVerdict.ESCALATE:
            note = review.note.strip()
            review = ResultReview(
                verdict=ResultVerdict.ESCALATE,
                note=(
                    "Auto-escalation for subagent results is on. The result checker decided "
                    f"{review.verdict.value}" + (f": {note}" if note else ".")
                ),
                summary=review.summary,
            )
        escalated = review.verdict is ResultVerdict.ESCALATE
        self.context.step(
            StepRecord(
                tool_name="Agent result",
                agent_id=result.agent_id,
                decision=review.verdict.value,
                summary=review.summary,
            )
        )
        if step_id is not None:
            await self._store_call(
                "recording a checker run",
                self.store.add_checker_run(
                    step_id,
                    CheckerRun(
                        checker_name="subagent-result",
                        checker_version=self._version("subagent-result"),
                        model=_model_name(self.result_checker.model),
                        decision="escalate" if escalated else "pass",
                        reason=review.note,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                        cache_read_tokens=outcome.cache_read_tokens,
                        duration_ms=outcome.duration_ms,
                    ),
                ),
            )
            await self._store_call(
                "recording a decision",
                self.store.decide_step(
                    step_id,
                    StepDecision(
                        decision="escalate" if escalated else "pass",
                        reason=review.note,
                        summary=review.summary,
                    ),
                ),
            )
        await self._emit_safely(
            CheckResultEvent(
                tool_use_id=result.agent_id,
                agent_id=result.agent_id,
                tool_name="Agent",
                route=Route.SUBAGENT,
                decision=Decision.ESCALATE if escalated else Decision.PASS,
                reason=review.note if escalated else "",
                step_summary=review.summary,
                comment="" if escalated else review.note,
            )
        )
        note = review.note.strip()
        if step_id is not None and not escalated:
            await self._store_call(
                "recording a step result",
                self.store.finish_step(step_id, note or "accepted"),
            )
        if escalated:
            self._pending_reasons[step.tool_use_id or ""] = (note, "subagent_result")
            answer = await self.ask(step)
            if answer.cancelled:
                return ""
            if answer.allow:
                decision = "The user accepts the result; act on it as appropriate."
            else:
                decision = "The user does not want you to act on this result now."
            if answer.note:
                decision += f" Note from the user: {answer.note}"
            note = f"{note}\n\nDecision of the user: {decision}".strip()
            if step_id is not None:
                await self._store_call(
                    "recording a step result", self.store.finish_step(step_id, note)
                )
        if not note:
            return ""
        return SUBAGENT_NOTE_TEMPLATE.format(label=label, note=note)

    async def _review(self, result: SubagentResult) -> ResultOutcome:
        """Run the result checker; never raises.

        Args:
            result: The subagent's task and final result.

        Returns:
            The review with the cost of the call; an internal error yields an ESCALATE review.
        """
        self._checking += 1
        await self._refresh_state()
        try:
            return await self.result_checker.run(result, self.context.check_context())
        except Exception as exc:
            log.exception("reviewing a subagent result failed")
            return ResultOutcome(
                review=ResultReview(
                    verdict=ResultVerdict.ESCALATE,
                    note=f"Reviewing the result failed ({type(exc).__name__}: {exc}).",
                    summary="Subagent result not reviewed because of an internal error.",
                )
            )
        finally:
            self._checking -= 1
            await self._refresh_state()

    # ------------------------------------------------------------------ deciding

    async def _decide(self, step: Step, pending: _PendingNarration | None) -> _Decided:
        """Decide about `step`: rules, tool policy, then the checker of its route.

        Side effect: if a checker runs, narrates `pending` first (sets `pending.text`) and
        passes the narration to the checker.

        Args:
            step: The tool call.
            pending: Claude's thinking and text before the call, or None.

        Returns:
            The route, the decision, who decided, what the rules found and the checker's model
            call. Checker failures already yield ESCALATE decisions.
        """
        route = self.policy.route(step.tool_name)
        outcome = self.rules.evaluate(step)
        rules = _rules_outcome(step, outcome)
        label = label_for_unchecked(step.tool_name, step.tool_input)
        if self._clock_alarm is not None:
            # A backward clock jump makes the attribution of file changes unreliable (D32), so a
            # human decides before anything else runs. The alarm is cleared either way: the
            # answer to this escalation is the answer to the jump.
            alarm, self._clock_alarm = self._clock_alarm, None
            return _Decided(
                route=route,
                decision=CheckDecision(
                    decision=Decision.ESCALATE, reason=alarm, step_summary=label
                ),
                source="clock",
                rules=rules,
            )
        if outcome.ask:
            return _Decided(
                route=route,
                decision=CheckDecision(
                    decision=Decision.ESCALATE, reason=outcome.reason, step_summary=label
                ),
                source="rules",
                rules=rules,
            )
        if route is Route.ALLOW and step.tool_name in READ_PATH_TOOLS and not outcome.allowed:
            route = Route.COMMAND  # a read outside the read allowlist: the command checker decides
        if outcome.notes:
            step = step.model_copy(update={"notes": [*step.notes, *outcome.notes]})
        if route is Route.COMMAND and step.tool_name == "Bash" and outcome.allowed:
            # The allow lists say "no check needed", not "run this whatever the harness
            # thinks": the step passes, and Claude Code's auto mode still sees it (D21).
            allowed = CheckDecision(
                decision=Decision.PASS,
                reason="Every command and path is on the user's allowlists.",
                step_summary=label,
            )
            return self._auto_escalate(
                _Decided(route=route, decision=allowed, source="rules", rules=rules)
            )
        if route in (Route.ALLOW, Route.DENY, Route.ESCALATE):
            # Route.ALLOW means "no check": the step passes to Claude Code's own approval.
            decision = Decision.PASS if route is Route.ALLOW else Decision(route.value.upper())
            reason = (
                ""
                if route is Route.ALLOW
                else f"The tool policy routes {step.tool_name} to {route}."
            )
            return _Decided(
                route=route,
                decision=CheckDecision(decision=decision, reason=reason, step_summary=label),
                source="policy",
                rules=rules,
            )
        diff = ""
        if route is Route.COMMAND and step.tool_name == "Bash" and self._checks_changesets():
            command = str(step.tool_input.get("command", ""))
            if is_commit(command, Path(step.cwd or self.workspace)):
                route = Route.CHANGESET
                diff = await self._changeset(command)
                if step.tool_use_id:
                    self._changesets[step.tool_use_id] = diff
        if route is Route.EDIT:
            await self._emit_edit_diff(step)
        if pending is not None:
            if pending.text is None:
                pending.text = await self._narrate_safely(pending.outputs)
            if pending.text:
                step = step.model_copy(update={"narration": pending.text})
        checker = self.checkers[route]
        context = self.context.check_context()
        self._checking += 1
        await self._refresh_state()
        try:
            with anyio.fail_after(CHECKER_DEADLINE_S):
                run = await checker.run(step, context, extra=diff)
        except TimeoutError:
            run = CheckOutcome(
                decision=CheckDecision(
                    decision=Decision.ESCALATE,
                    reason=(
                        f"The {route.value} checker did not answer within {CHECKER_DEADLINE_S} s."
                    ),
                    step_summary=label,
                ),
                duration_ms=CHECKER_DEADLINE_S * 1000,
            )
        finally:
            self._checking -= 1
            await self._refresh_state()
        return self._auto_escalate(
            _Decided(route=route, decision=run.decision, source="checker", rules=rules, run=run)
        )

    def _auto_escalate(self, decided: _Decided) -> _Decided:
        """Turn an approval into an escalation if auto-escalation is on for its route.

        Args:
            decided: The decision of the checker or of the rules.

        Returns:
            `decided` unchanged, or a copy with an ESCALATE decision and source "auto".
        """
        auto = self.config.auto_escalate
        route, source = decided.route, decided.source
        # A checker DENY stays a DENY: auto-escalation only turns approvals into questions, so
        # a hasty "allow" can never run a step that the checker blocked.
        if decided.decision.decision.approves and (
            (route is Route.EDIT and auto.edits) or (route is Route.COMMAND and auto.commands)
        ):
            kind = "edits" if route is Route.EDIT else "commands"
            decided.decision = CheckDecision(
                decision=Decision.ESCALATE,
                reason=(
                    f"Auto-escalation for {kind} is on. "
                    + (f"The {route.value} checker" if source == "checker" else "The rules")
                    + f" decided {decided.decision.decision.value}"
                    + (f": {decided.decision.reason}" if decided.decision.reason else ".")
                ),
                step_summary=decided.decision.step_summary,
            )
            decided.source = "auto"
        return decided

    def _checks_changesets(self) -> bool:
        """Tell whether the changeset checker is in use.

        Returns:
            True if the database holds an enabled checker type "changeset"; a database that has
            none (or an administrator who disabled it) leaves commits to the command checker.
        """
        return "changeset" in self.settings.checkers

    async def _changeset(self, command: str) -> str:
        """Return the change a commit command would record, as a prompt block; never raises.

        Args:
            command: The commit command line.

        Returns:
            A "# Changeset of this commit" block, or a block saying that there is nothing to
            commit or that git could not be asked.
        """
        try:
            diff = await changeset(self.workspace, command)
        except Exception as exc:
            log.exception("collecting the changeset failed")
            diff = f"(the changeset could not be read: {type(exc).__name__}: {exc})"
        if not diff.strip():
            diff = "(nothing would be committed: the diff is empty)"
        return f"# Changeset of this commit\n\n{diff}"

    async def _emit_edit_diff(self, step: Step) -> None:
        """Emit `edit.diff` with the file content before and after an edit; never raises.

        Args:
            step: The edit tool call.
        """
        try:
            edited = edited_file(step, self.workspace)
        except Exception:
            log.exception("rendering an edit failed")
            return
        if edited is None:
            return
        path, before, after = edited
        await self._emit_safely(
            EditDiff(
                tool_use_id=step.tool_use_id,
                agent_id=step.agent_id,
                path=path,
                before=before,
                after=after,
                **dict(zip(("start_line", "end_line"), changed_lines(before, after), strict=False)),
            )
        )

    def _comment_already_sent(self, comment: str, agent_id: str | None) -> bool:
        """Tell whether the same checker comment was already sent in the running turn.

        Comparison ignores case and whitespace, so a checker that repeats itself for similar
        steps reaches Claude only once. Only steps of the same agent count: a subagent has its
        own context and has not seen comments that went to the main agent.

        Args:
            comment: The new comment.
            agent_id: The agent of the step (None for the main agent).

        Returns:
            True if an equal comment is recorded for the same agent in the running turn.
        """

        def norm(text: str) -> str:
            """Normalize a comment for comparison.

            Args:
                text: The comment.

            Returns:
                The text in lower case with single spaces.
            """
            return " ".join(text.lower().split())

        turn = self.context.current
        return turn is not None and any(
            s.agent_id == agent_id and norm(s.comment) == norm(comment) for s in turn.steps
        )

    # ------------------------------------------------------------------ runner events

    async def _on_runner_event(self, event: Event) -> None:
        """Update context, state and store from a runner event, then forward it to the clients.

        Args:
            event: The event from the runner.
        """
        match event:
            case UserInput() if event.source == "cross_session":
                self._record_cross_session(event.text)
            case UserInput():
                self.context.user_input(event.text)
                await self._take_from_queue(event.text)
            case AssistantText() | AssistantThinking() if self.narrator is not None:
                kind = "thinking" if isinstance(event, AssistantThinking) else "text"
                outputs = self._claude_outputs.setdefault(event.agent_id, [])
                outputs.append(ClaudeOutput(kind, event.text))
                del outputs[:-MAX_PENDING_OUTPUTS]
            case ToolUse() if event.name in AGENT_TOOLS:
                # The subagent's own id is this call's id; the caller is its parent (D29).
                await self._store_call(
                    "recording a subagent",
                    self.store.add_agent(event.tool_use_id, event.agent_id),
                )
            case AgentsUpdate():
                await self._store_call(
                    "recording the subagents", self.store.update_agents(event.agents)
                )
            case ToolResult():
                await self._snapshot_after(event.tool_use_id)
                self._finish_time(event.tool_use_id)
                self._pending_reasons.pop(event.tool_use_id, None)
                self._running_tools.discard(event.tool_use_id)
                self._changesets.pop(event.tool_use_id, None)
                step_id = self._step_ids.pop(event.tool_use_id, None)
                if step_id is not None:
                    await self._store_call(
                        "recording a step result",
                        self.store.finish_step(step_id, _result_summary(event)),
                    )
            case TurnResult():
                await self._on_turn_result(event)
            case _:
                pass
        await self.emit(event)
        if isinstance(event, AssistantText) and event.agent_id is None:
            await self._hint_on_login_error(event.text)
        if isinstance(event, TurnResult) and event.is_error:
            await self._hint_on_login_error(event.result)
        if isinstance(event, ToolUse) and event.name == "Bash":
            await self._emit_command_files(event)
        if isinstance(event, TurnResult):
            self._busy = False
            await self._refresh_state()
        else:
            await self._mark_busy()

    async def _on_turn_result(self, event: TurnResult) -> None:
        """Record Claude's session id and close the running turn.

        Args:
            event: The turn result.
        """
        await self._record_claude_session(event.session_id)
        outputs = self._claude_outputs.pop(None, [])
        if self.context.current is not None:
            # Recorded (and persisted) with the turn: never show the verbatim answer.
            self.context.current.narrated = self.narrator is not None
        turn = self.context.end_turn(event.result)
        if turn is not None and self._tg is not None:
            self._spawn(self._finish_turn, turn, outputs)

    async def _record_claude_session(self, claude_session_id: str | None) -> None:
        """Write Claude's own session id to the `sessions` row, once and on every change.

        The id is what `calu run --resume` and `session.history` need, so it is recorded as soon
        as Claude Code reports it (at the start, not only after the first turn): a session that
        ends before its first turn must still be resumable (D27).

        Args:
            claude_session_id: The id Claude Code reports, or None if it is not known yet.
        """
        if not claude_session_id or claude_session_id == self.session_id:
            return
        self.session_id = claude_session_id
        await self._store_call(
            "recording Claude's session id",
            self.store.set_claude_session(claude_session_id, self.config.claude.model),
        )

    async def _hint_on_login_error(self, text: str) -> None:
        """After Claude Code's "Please run /login" error, send a setup hint once per process.

        Args:
            text: An answer of the main agent or the result of a failed turn.
        """
        if self._login_hint_sent or LOGIN_ERROR not in text.lower():
            return
        self._login_hint_sent = True
        bedrock = (
            "Claude Code runs through AWS Bedrock here (use_bedrock is on): check the AWS "
            "credentials and the region on this machine."
            if self.config.claude.use_bedrock
            else BEDROCK_HINT
        )
        await self.emit(
            Log(
                level="warning",
                message=f"Claude Code is not signed in. {CLAUDE_SETUP_HINT} {bedrock}",
            )
        )

    async def _emit_command_files(self, event: ToolUse) -> None:
        """Send `tool.files` with the existing text files that a Bash command names.

        Relative paths resolve against the workspace (Claude's working directory); errors are
        logged, never raised, because links are optional.

        Args:
            event: The Bash `tool.use` event.
        """
        try:
            files = command_files(str(event.input.get("command", "")), self.workspace, self.rules)
        except Exception:
            log.exception("finding the files of a command failed")
            return
        if files:
            await self.emit(
                ToolFiles(agent_id=event.agent_id, tool_use_id=event.tool_use_id, files=files)
            )

    # ------------------------------------------------------------------ narration and turns

    async def _narrate_safely(self, outputs: list[ClaudeOutput]) -> str:
        """Narrate `outputs`; never raises (errors are logged).

        Args:
            outputs: Claude's thinking and text blocks of one agent.

        Returns:
            The narration, or "" if narration is off, there is nothing to narrate, or the
            narrator failed or took longer than `NARRATOR_DEADLINE_S`.
        """
        if self.narrator is None or not outputs:
            return ""
        self._checking += 1
        await self._refresh_state()
        try:
            with anyio.fail_after(NARRATOR_DEADLINE_S):
                return await self.narrator.narrate(outputs)
        except Exception:
            log.exception("narrating Claude's output failed")
            return ""
        finally:
            self._checking -= 1
            await self._refresh_state()

    def _record_cross_session(self, text: str) -> None:
        """Record a message from another Claude session as a turn entry, not as user input.

        The entry's summary is a placeholder until the narrator has retold the message (in the
        background); with narration off it stays a fixed text. The message is never put into
        the checkers' context verbatim.

        Args:
            text: The message as Claude Code delivered it.
        """
        record = StepRecord(tool_name=CROSS_SESSION_LABEL, summary=CROSS_SESSION_PENDING)
        if self.narrator is None:
            record.summary = CROSS_SESSION_HIDDEN
        self.context.step(record)
        if self.narrator is not None and self._tg is not None:
            self._spawn(self._narrate_cross_session, record, text)

    async def _narrate_cross_session(self, record: StepRecord, text: str) -> None:
        """Narrate a message from another session into its turn entry; never raises.

        Args:
            record: The turn entry created by `_record_cross_session`.
            text: The message.
        """
        narration = await self._narrate_safely([ClaudeOutput("message", text)])
        record.summary = narration or CROSS_SESSION_HIDDEN

    async def _narrate_record(self, record: StepRecord, outputs: list[ClaudeOutput]) -> None:
        """Narrate the thinking and text before an unchecked step into its record; never raises.

        Args:
            record: The step record (already in the context); its `narration` is set.
            outputs: Claude's thinking and text before the step.
        """
        record.narration = await self._narrate_safely(outputs)

    async def _finish_turn(self, turn: TurnRecord, outputs: list[ClaudeOutput]) -> None:
        """Narrate the end of a turn, store it, then fold old turns if due; never raises.

        Folding waits for the narration, so the chat summary is made from the narrated answer.

        Args:
            turn: The finished turn; its `answer_narration` is set if narration is on.
            outputs: The main agent's thinking and text after its last tool call.
        """
        if self.narrator is not None and outputs:
            turn.answer_narration = await self._narrate_safely(outputs)
        await self._store_call("storing a turn", self.store.save_turn(turn))
        if self.context.needs_fold():
            await self._fold()

    async def _fold(self) -> None:
        """Fold old turns into the chat summary; never raises (errors are logged)."""
        try:
            await self.context.fold()
        except Exception:
            log.exception("folding the context failed")

    async def _store_summary(self, summary: str, covered_turns: int) -> None:
        """Store the chat summary after a fold and mark the folded turns (never raises).

        Args:
            summary: The new chat summary.
            covered_turns: Number of the newest folded turn.
        """
        await self._store_call(
            "storing the chat summary", self.store.save_summary(summary, covered_turns)
        )
        await self._store_call("marking folded turns", self.store.mark_folded(covered_turns))

    async def _restore_context(self, resume_id: str | None) -> None:
        """Load the turns and the chat summary of the session that is resumed.

        A new session (or a database error) starts with no turns and the empty summary.

        Args:
            resume_id: Claude session id to resume, or None for a new session.
        """
        turns: list[TurnRecord] = []
        summary = ""
        if resume_id is not None:
            try:
                turns, summary = await self.store.load_previous(resume_id)
            except Exception:
                log.exception("restoring the context of %s failed", resume_id)
        self.context.restore(turns)
        self.context.summary = summary or EMPTY_CHAT_SUMMARY

    # ------------------------------------------------------------------ store helpers

    def _identity(self) -> SessionIdentity:
        """Describe where this session runs, for the `sessions` row.

        Returns:
            Host name, operating system, OS user, directory, process id, git commit, project
            and Claude model.
        """
        return SessionIdentity(
            hostname=socket.gethostname(),
            os_user=getpass.getuser(),
            directory=str(self.workspace),
            pid=os.getpid(),
            os=f"{platform.system()} {platform.release()}".strip(),
            git_commit=self._git_head_value,
            project=self.project,
            model=self.config.claude.model,
        )

    def _version(self, checker: str) -> int:
        """Return the stored version of a checker definition.

        Args:
            checker: Checker type name.

        Returns:
            The version from the database, or 0 if the definition is built in.
        """
        definition = self.settings.checkers.get(checker)
        return definition.version if definition else 0

    def _checker_run(self, checker: Checker, route: str, outcome: CheckOutcome) -> CheckerRun:
        """Build the `checker_runs` row of one step check.

        Args:
            checker: The checker that ran.
            route: Its route value ("command" or "edit"), used as the checker type name.
            outcome: What the call returned and cost.

        Returns:
            The row.
        """
        return CheckerRun(
            checker_name=route,
            checker_version=self._version(route),
            model=_model_name(checker.model),
            decision=outcome.decision.decision.value.lower(),
            reason=outcome.decision.reason,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            cache_read_tokens=outcome.cache_read_tokens,
            duration_ms=outcome.duration_ms,
        )

    async def _add_step(self, step: Step) -> int | None:
        """Record a tool call in the database before it is checked.

        Args:
            step: The tool call.

        Returns:
            The id of the `steps` row, or None if it could not be stored (then the step is
            still checked; only its history is lost).
        """
        row = StoredStep(
            seq=next(self._step_seq),
            tool_name=step.tool_name,
            tool_input=step.tool_input,
            turn=self.context.turn_number,
            agent_id=step.agent_id,
            tool_use_id=step.tool_use_id,
            git_commit=await self._git_head(),
        )
        try:
            step_id = await self.store.add_step(row)
        except Exception as exc:
            log.exception("recording a step failed")
            await self._emit_safely(
                Log(level="error", message=f"recording a step in the database failed: {exc}")
            )
            return None
        self._times.append(
            StepTime(
                seq=row.seq,
                tool_name=step.tool_name,
                started_at=datetime.now(UTC),
                agent_id=step.agent_id,
            )
        )
        if step.tool_use_id:
            self._time_index[step.tool_use_id] = row.seq
            self._step_ids[step.tool_use_id] = step_id
        return step_id

    async def _open_escalation(self, step: Step, reason: str, rendered: str) -> int | None:
        """Open an `escalations` row for a step that needs a human decision.

        The context holds what an operator needs to decide and what an evaluation case needs
        to reproduce the step later (`calu.evals.cases`): the rendered step, the tool input with
        workspace-relative paths, and the content of the file an edit would change.

        Args:
            step: The tool call.
            reason: Why it needs a decision.
            rendered: The step rendered as Markdown, for the operator.

        Returns:
            The id of the row, or None if it could not be stored.
        """
        context: dict[str, Any] = {
            "rendered": rendered,
            "tool_name": step.tool_name,
            "tool_input": workspace_relative_input(step.tool_input, self.workspace),
            "agent_id": step.agent_id,
            "files": files_before(step, self.workspace),
            "changeset": self._changesets.get(step.tool_use_id or "", ""),
            "recent": self.context.recent(),
            "chat_summary": self.context.summary,
            "calu_version": __version__,
        }
        try:
            return await self.store.open_escalation(
                reason, context, self._step_ids.get(step.tool_use_id or "")
            )
        except Exception as exc:
            log.exception("opening an escalation failed")
            await self._emit_safely(
                Log(level="error", message=f"storing an escalation failed: {exc}")
            )
            return None

    async def _store_call(self, what: str, call: Any) -> None:
        """Await a store call and report its failure as a `log` error; never raises.

        Args:
            what: What was attempted, for the message (e.g. "storing a turn").
            call: The awaitable returned by the store method.
        """
        try:
            await call
        except Exception as exc:
            log.exception("%s failed", what)
            await self._emit_safely(Log(level="error", message=f"{what} failed: {exc}"))

    async def _cancel_escalations(self, message: str) -> None:
        """Deny all open escalations with `message` (their `ask` calls then return).

        Args:
            message: The text Claude receives with the denial.
        """
        for esc in list(self._escalations.values()):
            esc.answer = HumanAnswer(allow=False, message=message)
            esc.done.set()
            if esc.db_id is not None:
                await self._store_call(
                    "cancelling an escalation", self.store.close_escalation(esc.db_id, message)
                )
        self._escalations.clear()

    # ------------------------------------------------------------------ state and events

    def _spawn(self, func: Callable[..., Awaitable[None]], *args: Any) -> None:
        """Start a background task that `stop` waits for.

        Narration, storing a turn and folding the chat summary run next to the next tool call,
        so they must not delay Claude; `stop` and the end of the session wait for them, so that
        a finished session has recorded its last turn.

        Args:
            func: The coroutine function to run.
            *args: Its arguments.
        """
        if self._tg is None:
            return
        if self._pending == 0:
            self._quiet = anyio.Event()
        self._pending += 1
        self._tg.start_soon(self._tracked, func, *args)

    async def _tracked(self, func: Callable[..., Awaitable[None]], *args: Any) -> None:
        """Run a background task and report when the last one has finished.

        Args:
            func: The coroutine function to run.
            *args: Its arguments.
        """
        try:
            await func(*args)
        finally:
            self._pending -= 1
            if self._pending <= 0:
                self._quiet.set()

    async def _wait_quiet(self) -> None:
        """Wait up to `BACKGROUND_TIMEOUT_S` seconds for the background tasks to finish."""
        if self._quiet.is_set():
            return
        with anyio.move_on_after(BACKGROUND_TIMEOUT_S):
            await self._quiet.wait()
        if not self._quiet.is_set():
            log.warning("%d background tasks did not finish in time", self._pending)

    async def emit(self, event: Event) -> None:
        """Send `event` to the clients.

        Args:
            event: The event.

        Raises:
            Exception: Whatever the client sink raises (e.g. a closed connection).
        """
        await self._sink(event)

    def _derived_state(self) -> State:
        """Return the state that the running work implies (see the module docstring).

        Returns:
            "escalated", "checking", "tool_call", "running" or "idle".
        """
        if self._escalations:
            return "escalated"
        if self._checking > 0:
            return "checking"
        if self._running_tools:
            return "tool_call"
        return "running" if self._busy else "idle"

    async def _refresh_state(self) -> None:
        """Set the state to what the running work implies, unless the session is not active."""
        if self.state in ("stopped", "failed", "starting"):
            return
        await self._set_state_safely(self._derived_state())

    async def _set_state(self, state: State) -> None:
        """Change the state, record it in the database and emit `session.state`.

        Does nothing if the state is unchanged.

        Args:
            state: The new state.

        Raises:
            Exception: Whatever the client sink raises.
        """
        if state == self.state:
            return
        self.state = state
        # "starting" is the state the session row is created in, "stopped" and "failed" are
        # written by `store.end`, so only the states in between need an update.
        if self.store.session_id is not None and state not in (
            "starting",
            "stopped",
            "failed",
        ):
            await self._store_call("recording the session state", self.store.set_state(state))
        sid = self.session_id or (self.runner.session_id if self.runner else None)
        await self.emit(SessionState(state=state, session_id=sid))

    async def _set_state_safely(self, state: State) -> None:
        """Like `_set_state`, but errors of the client sink are logged, not raised.

        Args:
            state: The new state.
        """
        try:
            await self._set_state(state)
        except Exception:
            log.exception("emitting the session state failed")

    async def _mark_busy(self) -> None:
        """Note that Claude is working and update the state."""
        self._busy = True
        await self._refresh_state()

    async def _emit_safely(self, event: Event) -> None:
        """Emit `event`; errors of the client sink are logged, not raised.

        Args:
            event: The event to send.
        """
        try:
            await self.emit(event)
        except Exception:
            log.exception("emitting %s failed", event.method)

    async def _git_head(self) -> str | None:
        """Return the commit id of the workspace's git HEAD, cached for `GIT_HEAD_TTL_S`.

        Returns:
            The full commit id, or None if the workspace is not a git repository (or git fails).
        """
        now = anyio.current_time()
        if now - self._git_head_time < GIT_HEAD_TTL_S:
            return self._git_head_value
        value: str | None = None
        try:
            with anyio.fail_after(5):
                done = await anyio.run_process(
                    ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                    cwd=self.workspace,
                    check=False,
                )
            if done.returncode == 0:
                value = done.stdout.decode().strip() or None
        except OSError, TimeoutError:
            value = None
        self._git_head_value, self._git_head_time = value, now
        return value

    def _require_running(self) -> ClaudeRunner:
        """Return the active runner.

        Returns:
            The runner of the running session.

        Raises:
            SessionError: If no session accepts messages (stopped or still starting).
        """
        if self.state in ("stopped", "failed", "starting") or self.runner is None:
            raise SessionError("no Claude session is running (send session.start first)")
        return self.runner

    def _find_agent(self, agent_id: str) -> AgentInfo:
        """Return the running background subagent `agent_id`.

        Args:
            agent_id: Protocol agent id (id of the Agent tool call).

        Returns:
            The subagent's record.

        Raises:
            SessionError: If no session runs, the subagent is unknown, or it is not a running
                background subagent.
        """
        agent = self._require_running().agents.get(agent_id)
        if agent is None:
            raise SessionError(f"unknown subagent {agent_id!r}")
        if not agent.background or agent.status != "running":
            raise SessionError(
                "messages can only be sent to running background subagents; "
                "a foreground subagent blocks the main agent until it finishes"
            )
        return agent


def workspace_relative_input(tool_input: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Return `tool_input` with `file_path`/`notebook_path` relative to `workspace` if inside it.

    Paths in an escalation's context are stored relative, so that an evaluation case built from
    it can be reproduced in another directory.

    Args:
        tool_input: The tool call's input.
        workspace: The workspace root.

    Returns:
        A copy; paths outside the workspace stay absolute.
    """
    result = dict(tool_input)
    for key in ("file_path", "notebook_path"):
        value = result.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            continue
        try:
            result[key] = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            pass
    return result


def files_before(step: Step, workspace: Path, limit: int = 50_000) -> dict[str, str]:
    """Return the current content of the file an edit step targets, for evaluation cases.

    Args:
        step: The escalated step.
        workspace: The workspace root.
        limit: Files longer than this many characters are not recorded.

    Returns:
        {workspace-relative path: content}, or {} for calls that are not edits and for
        missing, long, binary or outside files.
    """
    raw = step.tool_input.get("file_path")
    if step.tool_name not in EDIT_TOOLS or not raw:
        return {}
    relative = workspace_relative_input({"file_path": raw}, workspace)["file_path"]
    if Path(relative).is_absolute():
        return {}
    try:
        content = (workspace / relative).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError, ValueError:
        return {}
    return {relative: content} if len(content) <= limit else {}


def _rules_outcome(step: Step, outcome: RuleOutcome) -> str | None:
    """Return what the allow and deny lists found, for the `steps` row.

    Args:
        step: The tool call.
        outcome: The result of `Rules.evaluate`.

    Returns:
        "deny" if a part matches a deny list, "allow" if every part is allow-listed,
        "unlisted" otherwise, or None for a tool whose calls the lists do not cover (the rules
        then say nothing about it).
    """
    tool = step.tool_name
    if tool != "Bash" and tool not in READ_PATH_TOOLS and tool not in WRITE_PATH_TOOLS:
        return None
    if outcome.ask:
        return "deny"
    return "allow" if outcome.allowed else "unlisted"


def _result_summary(event: ToolResult, limit: int = 2_000) -> str:
    """Return a short description of a tool result for the `steps` row.

    Args:
        event: The tool result event.
        limit: Maximum number of characters kept.

    Returns:
        The result content, cut at `limit`, prefixed with "error: " if the tool failed.
    """
    content = event.content[:limit]
    return f"error: {content}" if event.is_error else content


def _model_name(model: object) -> str:
    """Return a printable name of a checker model.

    Args:
        model: A Pydantic AI model string or `Model` instance.

    Returns:
        The string itself, or the instance's `model_name`.
    """
    return model if isinstance(model, str) else str(getattr(model, "model_name", model))


async def _first_of(*events: anyio.Event) -> None:
    """Return as soon as one of `events` is set.

    Args:
        *events: The events to wait for.
    """
    async with anyio.create_task_group() as tg:

        async def wait(event: anyio.Event) -> None:
            """Wait for `event`, then cancel the waits for the other events.

            Args:
                event: The event to wait for.
            """
            await event.wait()
            tg.cancel_scope.cancel()

        for event in events:
            tg.start_soon(wait, event)


def _render_safely(step: Step, workspace: Path) -> str:
    """Render `step` for an escalation; fall back to the raw input if rendering fails.

    Args:
        step: The tool call.
        workspace: Workspace root for relative paths.

    Returns:
        The Markdown rendering of the step.
    """
    try:
        return render_step(step, workspace)
    except Exception:
        log.exception("rendering a step failed")
        return f"`{step.tool_name}` {step.tool_input!r}"
