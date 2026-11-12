"""The session orchestrator: connects a Claude runner with the checkers, the context and the store.

    clients ──requests──► Session ──send/interrupt──► ClaudeRunner ──► Claude Code
    clients ◄──events──── Session ◄──events, ToolGate calls────┘
                            │
                            ├─ StepGate: Rules + ToolPolicy + Checkers (decides about each
                            │                                           tool call, `calu.gate`)
                            ├─ EscalationDesk      (asks a human and waits, `calu.escalations`)
                            ├─ WriteSets           (what a command wrote, its review and the
                            │                       undo, `calu.write_sets`)
                            ├─ ContextBuilder                  (turn records, chat summary)
                            └─ Recorder ──► SessionStore  (PostgreSQL: history, escalations,
                                            comments; a failed write is reported and counted,
                                            `calu.recorder`)

What this module itself holds: the lifecycle (`start`, `_run`, `stop`, `interrupt`), the
handling of the runner's events, the bookkeeping of turns and narration, the requests of the
clients, and the wiring of the components above. The state of one run is in `calu.run_state`.

Tool call flow (ToolGate). `Session.pre_tool_use` adds the step, lets `calu.gate.StepGate`
decide, records the decision and answers Claude Code; an ASK is followed by `Session.ask`. The
decision itself, and the invariant that every error path ends in ESCALATE, are in `calu.gate`:

    rules (allow/deny lists):  a part matches a deny list  -> ask the human
    route = policy.route(tool)
      ALLOW (read tools)    -> allow if the path is allow-listed, else the command checker
      ALLOW / DENY          -> verdict without a check
      ESCALATE              -> ask the human
      COMMAND (Bash)        -> allow if every part is allow-listed, else the checker
      COMMAND / EDIT        -> checker decision: ALLOW -> allow, DENY -> deny, ESCALATE -> ask
                               (the checker sees the rules' notes on unlisted paths)
      COMMAND with a commit -> a line that is one plain `git commit`: the command checkers and,
                               in addition, the changeset checkers with the commit's diff;
                               any other line with a commit: deny ("commit as its own command");
                               a diff that calu could not read: ask the human, without a
                               model call (or deny, where staging first makes it readable)
    any internal error      -> ask the human, also after the decision (it never weakens a
                               DENY or an ASK)
    no answer within `GATE_BUDGET_S` -> ask the human
    ask -> an `escalations` row and an `escalation.request` event; waits without a time limit

While an escalation of the session is open or being opened, the gate approves no tool call that
needs a check (`EscalationDesk.hold`), so that no other agent of the session gets a step approved
while a human decides. An escalation counts from the moment the gate's verdict for its step is
ASK (`EscalationDesk.expect_ask`), not only once its row exists. The gate looks three times:

    another call reaches the gate ── no check needed (read tools, tool policy) ──► answered
            │ needs a check
            ▼
    1. wait until no escalation is open or being opened
            │ released                              │ still open after `HOLD_BUDGET_S`
            ▼                                       ▼
    checked as usual                    checked; DENY stays DENY, ESCALATE stays ESCALATE,
            │ approved                  an approval becomes an escalation of its own
            ▼                           (the hook that carries the gate cannot wait longer)
    2. an escalation appeared during the check: wait again, for what is left of `HOLD_BUDGET_S`;
       still open after that: the approval becomes an escalation of its own
            │ released
            ▼
    the decision is recorded
            │
            ▼
    3. an escalation appeared while it was recorded: the approval becomes an escalation

"Open" ends when the escalation is decided, timed out, or cancelled by a stop or an interrupt.
"Being opened" ends when the row exists, when it could not be stored, or `ASK_PENDING_S` after
the verdict if Claude Code never asks. Tool calls that already run are not touched, and the
reviews after a step (`review_write_set`, `review_subagent_result`) never wait: they open
escalations themselves. The escalations and this hold are kept by
`calu.escalations.EscalationDesk` (`Session.desk`), whose docstring states the invariants.

A human resolves an escalation in the database (`calu resolve`, a web UI, SQL); the session
notices the resolution through `note_resolution` (a `escalation_resolved` notification) or by
polling the row every `ESCALATION_POLL_S` seconds. An escalation nobody answers within
`escalation_timeout_s` denies its step and stops the session, which stays resumable by Claude's
session id (D27). `decide_escalation` is the second path: a client attached to the session
process decides. The decision is written to the row first and releases the step only once the
database accepted it, so the row never says anything else than what happened to the step.

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
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import anyio
from anyio.abc import TaskGroup
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from calu import __version__
from calu.changes import default_root
from calu.checkers import (
    Checker,
    CodeMark,
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
from calu.checkers.render import changed_lines, edited_file
from calu.checkers.tools import StepTime
from calu.claude.runner import (
    NOT_FROM_USER_LABEL,
    ClaudeRunner,
    EventSink,
    Fork,
    GateResult,
    HumanAnswer,
    ModelChangeFailed,
    SideQuestionFailed,
    Verdict,
)
from calu.claude.sdk_runner import AGENT_TOOLS
from calu.clock import ClockJump
from calu.comments import Comment, format_comment, map_selection
from calu.comments.mapping import excerpt, split_lines
from calu.config import EMPTY_CHAT_SUMMARY, AutoEscalate, CheckerConfig, Effort
from calu.context import ContextBuilder, StepRecord, TurnRecord
from calu.context.narrator import ClaudeOutput, Narrator
from calu.defaults import default_instructions
from calu.defaults.instructions import compose_instructions
from calu.escalations import ANSWER_TEMPLATE as ANSWER_TEMPLATE
from calu.escalations import ASK_PENDING_S as ASK_PENDING_S
from calu.escalations import DB_UNREACHABLE as DB_UNREACHABLE
from calu.escalations import ESCALATION_POLL_S as ESCALATION_POLL_S
from calu.escalations import NOBODY_ASKED as NOBODY_ASKED
from calu.escalations import OPEN_ESCALATION_RETRY_S as OPEN_ESCALATION_RETRY_S
from calu.escalations import STORE_DECISION_TIMEOUT_S as STORE_DECISION_TIMEOUT_S
from calu.escalations import Asked, EscalationDesk, EscalationError
from calu.escalations import Escalation as Escalation
from calu.escalations import Outcome as Outcome
from calu.files import command_files
from calu.gate import CHANGESET_NOTE as CHANGESET_NOTE
from calu.gate import CHANGESET_STAGE_FIRST as CHANGESET_STAGE_FIRST
from calu.gate import CHANGESET_UNREAD as CHANGESET_UNREAD
from calu.gate import CHECKER_DEADLINE_S as CHECKER_DEADLINE_S
from calu.gate import GATE_BUDGET_S as GATE_BUDGET_S
from calu.gate import HELD_LATE as HELD_LATE
from calu.gate import HOLD_BUDGET_S as HOLD_BUDGET_S
from calu.gate import Decided, PendingNarration, StepGate, route_checkers, unrecorded
from calu.gate import _changeset_block as _changeset_block
from calu.gate import combine_decisions as combine_decisions
from calu.honesty import (
    HonestyMark,
    Register,
    checked_marks,
)
from calu.models import resolve_model
from calu.protocol.messages import (
    AgentInfo,
    AgentsUpdate,
    AssistantText,
    AssistantThinking,
    AutoEscalateChanged,
    ChatQueue,
    ChatSendParams,
    CheckerContextResult,
    CheckerInfo,
    CheckerListResult,
    CommentAddParams,
    EditDiff,
    EscalationContextResult,
    Event,
    HonestyMarkInfo,
    HonestyUpdate,
    InitializeResult,
    Log,
    QueuedMessage,
    RateLimit,
    SessionHistoryResult,
    SessionSnapshot,
    SessionState,
    SessionSummary,
    State,
    ToolFiles,
    ToolResult,
    ToolUse,
    TurnResult,
    UsageResult,
    UsageUpdate,
    UserInput,
)
from calu.protocol.messages import CheckResult as CheckResultEvent
from calu.recorder import Recorder, model_name
from calu.run_state import STEP_TIMES_KEPT as STEP_TIMES_KEPT
from calu.run_state import RunState
from calu.steps import files_before as files_before
from calu.steps import paths_of_step, result_summary
from calu.steps import workspace_relative_input as workspace_relative_input
from calu.store import CheckerRun, ProjectSettings, SessionIdentity, SessionStore, StepDecision
from calu.tokens import token_hash
from calu.write_sets import WRITE_SET_NOT_REVERTED as WRITE_SET_NOT_REVERTED
from calu.write_sets import WRITE_SET_NOTE as WRITE_SET_NOTE
from calu.write_sets import WRITE_SET_PROMPT as WRITE_SET_PROMPT
from calu.write_sets import WRITE_SET_REVERTED as WRITE_SET_REVERTED
from calu.write_sets import WRITE_SET_UNMEASURED as WRITE_SET_UNMEASURED
from calu.write_sets import WRITE_SET_UNMEASURED_REFUSED as WRITE_SET_UNMEASURED_REFUSED
from calu.write_sets import WRITE_SET_UNREVIEWED as WRITE_SET_UNREVIEWED
from calu.write_sets import WriteSets

log = logging.getLogger(__name__)


NARRATOR_DEADLINE_S = 60
"""A narration that takes longer is left out; the checker then sees no narration."""
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

HARNESS_ASKED = (
    "Claude Code's own approval asked for a confirmation; calu's checkers had no objection to "
    "this step (PASS, see the check above). Whoever answers decides whether it runs."
)
"""Reason of an escalation that nothing in calu raised (PLAN.md D38).

Claude Code's auto mode does not only allow and deny: it can also ask. A PASS leaves the
decision to it (D21), and when it decides to ask, the SDK calls `can_use_tool`, which is calu's
wait for a human — so the step escalates even though no checker objected. Saying that plainly
matters, because the human would otherwise look for a checker's reason that does not exist.

What Claude Code itself says it is asking about goes **before** this (`Step.asked_about`,
D60), because that is the part a human acts on; this explains how the question got to them.
"""

FORK_REJECTED = "Resume rejected by --resume-drops-turn:"
"""What Claude Code says when a fork point does not match the turn it should discard (D36)."""

FORK_HINT = (
    "Claude Code refused the fork point: the turn to discard does not start where calu said it "
    "does. Resume the conversation whole (without --fork-before-turn), or pick another turn."
)
"""Hint for a refused fork, instead of the authentication hint."""

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


class SessionError(Exception):
    """A request cannot be served in the current state (reported to the client)."""


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

        Nothing is read from or written to the workspace except the files that rendered
        steps quote.

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
        self._rate_limits: list[RateLimit] = []
        """The newest state of each usage limit Claude Code reported (`usage.update`)."""
        self._last_cost: float | None = None
        self._last_turns = 0
        """Cost and turn count of the last `turn.result`, for `usage.get`."""
        self.register = Register()
        """The honesty register of this session, in memory only (PLAN.md D180)."""
        self._honesty_sent: tuple[tuple[HonestyMarkInfo, ...], str] | None = None
        """What the last `honesty.update` said, so an unchanged one is not sent again (D113)."""
        """The open marks of the project, reloaded when a checker changes them (PLAN.md D82).
        Empty while `honesty.mode` is "off", which is the default."""
        self.snapshot_root = snapshot_root or default_root()
        """Where the write-set snapshots of this process live (PLAN.md D31)."""
        self.policy = ToolPolicy(
            overrides=self.config.tool_routes, default=self.config.default_route
        )
        self.rules = settings.rules
        self.checkers: dict[Route, list[Checker]] = {
            Route.COMMAND: self._route_checkers(Route.COMMAND, self.config.command_checker),
            Route.EDIT: self._route_checkers(Route.EDIT, self.config.edit_checker),
            Route.CHANGESET: self._route_checkers(Route.CHANGESET, self.config.changeset_checker),
        }
        """The checkers of each step route, in the order they are reported. A route may carry
        several -- honesty, security, over-engineering beside the built-in one -- and a step runs
        all of them (D81)."""
        self.result_checker = self._result_checker()
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
        self._run_state = RunState.before_first_run()
        """Everything that describes one run; `start` replaces it (`calu.run_state`)."""
        self.recorder = Recorder(
            store,
            lambda: self._run_state,
            emit=self._emit_safely,
            failure_limit=lambda: self.config.store_failures_before_stop,
            checker_types=lambda: self.settings.checkers,
            turn_number=lambda: self.context.turn_number,
            git_head=lambda: self._git_head(),
            stop_session=self._stop_soon,
        )
        """Every write to the store goes through it; it reports and counts the failures and
        stops the session when they do not end (`calu.recorder`)."""
        self.desk = EscalationDesk(
            store,
            workspace,
            emit=self._emit_safely,
            refresh_state=self._refresh_state,
            store_call=self.recorder.attempt,
            store_failed=self.recorder.failed,
            store_succeeded=self.recorder.succeeded,
            timeout_s=lambda: self.config.escalation_timeout_s,
            claude_session_id=lambda: self.session_id,
            row_context=self._escalation_row_context,
            step_row=self.recorder.step_row,
            stop_session=self._stop_soon,
        )
        """The escalations of this session and the hold they put on other steps
        (`calu.escalations`); it outlives runs."""
        self.gate = StepGate(
            workspace,
            self.register,
            self.desk,
            policy=lambda: self.policy,
            rules=lambda: self.rules,
            config=lambda: self.config,
            checker_types=lambda: self.settings.checkers,
            checkers=lambda: self.checkers,
            check_context=lambda: self.context.check_context(),
            checking_scope=lambda: self._checking_scope(),
            narrate=lambda outputs: self._narrate_safely(outputs),
            emit_edit_diff=lambda step: self._emit_edit_diff(step),
            own_agent=lambda step: self._own_agent(step),
            take_clock_alarm=self._take_clock_alarm,
            keep_changeset=self._keep_changeset,
        )
        """Decides about each tool call (`calu.gate`); it reads the policy, the rules, the
        configuration and the checkers of this session as they are at the moment of a step."""
        self.write_sets = WriteSets(
            workspace,
            store,
            lambda: self._run_state,
            snapshot_root=lambda: self.snapshot_root,
            changes=lambda: self.config.changes,
            emit=self._emit_safely,
            store_call=self.recorder.attempt,
            add_checker_run=self.recorder.add_checker_run,
            ask=lambda step: self._ask(step),
            run_checkers=self.gate.run_checkers,
            check_context=lambda: self.context.check_context(),
        )
        """Measures what each command wrote, has it reviewed and undoes a refused change
        (`calu.write_sets`, PLAN.md D31)."""
        self.state: State = "stopped"
        self._git_head_value: str | None = None
        self._git_head_time = float("-inf")
        self._pending = 0  # background tasks (narration, turn storage, folding)
        self._quiet = anyio.Event()
        self._quiet.set()
        self._queue_ids = itertools.count(1)
        self._login_hint_sent = False

    @property
    def session_id(self) -> str | None:
        """Claude Code's own session id, once known.

        Returns:
            The id, or None before the runner of the current run has reported it.
        """
        return self._run_state.session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        """Set Claude Code's own session id in the state of the current run.

        Args:
            value: The id, or None if it is not known.
        """
        self._run_state.session_id = value

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

    def _prompt(self, route: str, name: str) -> str:
        """Return a checker's full prompt: calu's system part, the user's part, the rules (D181).

        Args:
            route: The checker's route ("command", "edit", "changeset", "subagent").
            name: The checker type, whose stored instructions are the user's part.

        Returns:
            The prompt `compose_instructions` builds, with the configured honesty rules and the
            part about recording marks while `honesty.mode` is on.
        """
        return compose_instructions(
            route,
            self._instructions(name),
            self.config.honesty.rules,
            marks=self.config.honesty.mode != "off",
        )

    def _result_checker(self) -> ResultChecker:
        """Build the subagent result checker from the configuration as it is now.

        One place for the start and for every reload: before, the reload updated its model and
        its instructions in place and left its request and token limits as they were at the
        start, and no path passed its effort at all (PLAN.md D135).

        Returns:
            The checker.
        """
        cfg = self.config.subagent_checker
        return ResultChecker(
            instructions=self._prompt("subagent", "subagent-result"),
            model=resolve_model(self._models.get("subagent") or cfg.model),
            usage_limits=UsageLimits(
                request_limit=cfg.request_limit, output_tokens_limit=cfg.output_tokens_limit
            ),
            max_tokens=cfg.max_tokens,
            effort=cfg.effort,
        )

    def _checker(self, name: str, cfg: CheckerConfig) -> Checker:
        """Create one step checker from its database definition and configuration.

        Args:
            name: Checker type name ("command" or "edit").
            cfg: Model and limits from the configuration.

        Returns:
            The checker.
        """
        definition = self.settings.checkers.get(name)
        route = definition.route if definition is not None else name
        return Checker(
            name=name,
            instructions=self._prompt(route, name),
            model=resolve_model(self._models.get(name) or cfg.model),
            workspace=self.workspace,
            rules=self.rules,
            times=self.step_times,
            effort=cfg.effort,
            usage_limits=UsageLimits(
                request_limit=cfg.request_limit, output_tokens_limit=cfg.output_tokens_limit
            ),
            max_tokens=cfg.max_tokens,
        )

    @asynccontextmanager
    async def _checking_scope(self) -> AsyncGenerator[None]:
        """Count one checker or narrator call as in flight while the block runs.

        The one place that raises and lowers the "checking" count, so that every raise has
        exactly one lowering, also when the block is cancelled or raises.

        Side effects: updates the session state on entry and on exit (`_refresh_state`).

        Yields:
            Nothing; the count is raised while the block runs.
        """
        self._run_state.checking += 1
        try:
            await self._refresh_state()
            yield
        finally:
            self._run_state.checking -= 1
            with anyio.CancelScope(shield=True):
                await self._refresh_state()

    def _route_checkers(self, route: Route, cfg: CheckerConfig) -> list[Checker]:
        """Create every enabled checker of one step route (`calu.gate.route_checkers`).

        Args:
            route: The step route.
            cfg: Model and limits for the route's own checker, from the configuration (D51).

        Returns:
            One checker per enabled definition, the route's own first; never empty.
        """
        return route_checkers(route, cfg, self.settings.checkers, self._checker)

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

    @property
    def honesty_marks(self) -> list[HonestyMark]:
        """The open marks of this session's register, oldest first (D180).

        Returns:
            The open marks; the register itself is `self.register`.
        """
        return self.register.open

    async def clear_mark(self, mark_id: int, client: str = "unknown") -> HonestyMark:
        """Close one open mark by hand, the override when a checker is wrong (D83, D180).

        Side effects: closes the mark in the register, logs who did it and tells every client.
        The register is in memory, so this is the only way to clear a mark of a running
        session; it needs nothing but the session's own protocol.

        Args:
            mark_id: The mark's id, as `honesty.update` reported it.
            client: Who asked, for the log.

        Returns:
            The closed mark.

        Raises:
            SessionError: If no open mark has that id.
        """
        closed = self.register.clear(mark_id, f"{client} (by hand)")
        if closed is None:
            raise SessionError(f"no open honesty mark {mark_id}")
        await self._emit_safely(
            Log(level="info", message=f"honesty: {client} cleared {closed.label()} by hand")
        )
        await self._emit_honesty()
        return closed

    async def announce_honesty(self) -> None:
        """Tell the clients the register and the mode after a settings reload (D113, D180).

        Switching `honesty.mode` to "off" is what a person does to unblock a session; a client
        that was told "enforce" would otherwise go on showing steps as blocked. Sends only what
        changed (`_emit_honesty`).
        """
        await self._emit_honesty()

    async def _emit_honesty(self) -> None:
        """Tell the clients the register and the mode, unless they already have both (D113).

        Comparing the **rendered** state rather than the mark ids is what makes this right in
        three cases the id comparison got wrong: a mark whose reason a checker refreshed (same
        id, new text), a mark that moved to another branch, and a change of `honesty.mode`
        alone, which is what a person does to unblock a session and which reached no client at
        all.

        Side effects: emits `honesty.update` and remembers what it sent.
        """
        payload = tuple(_mark_info(mark) for mark in self.honesty_marks)
        mode = self.config.honesty.mode
        if self._honesty_sent == (payload, mode):
            return
        self._honesty_sent = (payload, mode)
        await self._emit_safely(HonestyUpdate(marks=list(payload), mode=mode))

    async def reload_summary(self) -> None:
        """Read the chat summary again, in case an operator corrected it (PLAN.md D40).

        The summary is part of what steers the checkers — it is the conversation they read for
        everything older than the unfolded turns — so a wrong one is worth correcting by hand
        (`calu summary --edit`, or the web interface). Called with `apply_settings` after a
        `config_changed` notification; a changed summary reaches the next check.

        An empty answer from the store means "this session has no summary row", and the session
        then holds the placeholder it started with rather than an older text: a row deleted out
        of band must not leave the checkers reading a summary that is gone (D114). Emptying a
        summary **through calu** is refused on purpose (`calu.admin.set_chat_summary`), because
        the text covers turns that have left the recent window and an empty one would hide them
        rather than summarize them.

        Side effects: replaces `context.summary` and emits a `log` event when it changed.
        Never raises: a failed read is logged and the summary stays as it is. A summary the
        session itself replaced while the read was under way is kept.
        """
        before = self.context.summary
        try:
            stored = await self.store.load_summary()
        except Exception as exc:
            log.warning("could not read the chat summary again: %s", exc)
            return
        if self.context.summary != before:
            # The session folded turns into a new summary while the read was under way; what
            # was read is older than what the session holds and is about to store.
            return
        wanted = stored or EMPTY_CHAT_SUMMARY
        if wanted == self.context.summary:
            return
        self.context.summary = wanted
        message = (
            "the chat summary was changed; the checkers see the new one"
            if stored
            else "the chat summary was cleared; the checkers no longer read folded turns"
        )
        await self._emit_safely(Log(level="info", message=message))

    async def set_auto_escalate(self, auto: AutoEscalate, client: str = "unknown") -> None:
        """Replace the auto-escalation categories, record who did it, tell every client.

        Takes effect from the next check on: a step already waiting for a decision keeps the
        escalation it has. The change lives in the running session only -- it is not written to
        the project's settings -- so a restart goes back to what `calu config` holds. Switching a
        category off weakens the session, so the change goes into the audit log with the name of
        the client that asked for it (PLAN.md D43); a change that changes nothing is not
        recorded.

        Args:
            auto: The categories that must wait for a human whatever the checker decided.
            client: Name of the client that asked, for the audit entry.

        Raises:
            psycopg.Error: If the audit entry cannot be written; the setting is then unchanged,
                because a change nobody can see afterwards is worse than a refused request.

        Side effects:
            Writes an audit entry and emits `auto_escalate.changed` (errors of the client sink
            are logged, not raised).
        """
        before = self.config.auto_escalate
        if before == auto:
            return
        await self.store.note_setting_change(
            "auto_escalate", before.model_dump(), auto.model_dump(), client
        )
        self.config.auto_escalate = auto
        await self._emit_safely(AutoEscalateChanged(settings=auto))

    async def announce_auto_escalate(self, before: AutoEscalate) -> None:
        """Record and announce auto-escalation categories that a settings reload changed.

        `apply_settings` is synchronous and can neither write nor emit, so the caller that
        reloaded the settings calls this afterwards with what the categories were before.

        Args:
            before: The categories the session used before the reload.

        Raises:
            psycopg.Error: If the audit entry cannot be written. Unlike `set_auto_escalate` the
                setting is already in force, because the project's configuration is the source
                of truth for a reload.

        Side effects:
            Writes an audit entry and emits `auto_escalate.changed` (errors of the client sink
            are logged, not raised).
        """
        auto = self.config.auto_escalate
        if before == auto:
            return
        await self.store.note_setting_change(
            "auto_escalate", before.model_dump(), auto.model_dump(), "session: reloaded settings"
        )
        await self._emit_safely(AutoEscalateChanged(settings=auto))

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
        # Rebuilt rather than updated in place: a route carries a list now (D81), so a checker
        # row added, removed or disabled while the session runs takes effect at the next step,
        # as its instructions and its model already did. The code tools of the new checkers
        # follow the new read deny lists, because `_checker` passes the current rules.
        self.checkers = {
            Route.COMMAND: self._route_checkers(Route.COMMAND, self.config.command_checker),
            Route.EDIT: self._route_checkers(Route.EDIT, self.config.edit_checker),
            Route.CHANGESET: self._route_checkers(Route.CHANGESET, self.config.changeset_checker),
        }
        # Rebuilt like the route checkers above, so that its limits and its effort follow a
        # changed configuration as its model and its instructions always did (D135).
        self.result_checker = self._result_checker()
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
        return self.recorder.step_times()

    async def review_write_set(self, step: Step) -> str:
        """Review what a finished command wrote, and undo it if the review refuses (ToolGate, D31).

        Only a decision undoes the change: a DENY of the changeset checkers or a denial by a
        human who was asked. `calu.write_sets.WriteSets.review` has the full contract, with
        the table of escalation outcomes.

        Side effects: measures and records the write set, may change files in the workspace,
        and may open an escalation.

        Args:
            step: The tool call that has just run.

        Returns:
            A note for Claude, or "" if there is nothing to say.
        """
        return await self.write_sets.review(step)

    async def beat(self, interval_s: float) -> None:
        """Tell the database every `interval_s` seconds that this process is still running.

        A killed process cannot set its own state, so without this a row stays at its last
        state for ever and a herd cannot tell a working session from a dead one (PLAN.md D56).
        Runs until cancelled; a failed heartbeat is logged and the loop goes on, because losing
        the stamp is not a reason to stop a session that is otherwise fine.

        The first heartbeat is due one interval after the start, not at once: `calu run` starts
        this loop before the session row exists (the row is created by `start`, which needs the
        connection the loop would be using), and `sessions.last_seen` is stamped when the row
        is inserted, so there is nothing to refresh yet. Beating straight away instead made
        every single session log an error and tell the user its heartbeat had failed, which was
        both wrong and alarming.

        Args:
            interval_s: Seconds between heartbeats; a value of 0 or less returns at once, so
                no heartbeat is written. `calu run` passes the fixed `calu.herd.HEARTBEAT_S`;
                no setting changes it.

        Side effects:
            Writes `sessions.last_seen`; emits a `log` event when a heartbeat fails.
        """
        if interval_s <= 0:
            return
        while True:
            await anyio.sleep(interval_s)
            try:
                await self.store.heartbeat()
            except Exception:
                log.exception("the heartbeat failed")
                await self._emit_safely(
                    Log(
                        level="warning",
                        message="the heartbeat failed; the fleet view may call this session stale",
                    )
                )

    async def clock_jumped(self, jump: ClockJump) -> None:
        """React to a move of the wall clock that `calu.clock.ClockWatch` detected (D32).

        A forward jump is only reported, because a first time-server sync legitimately jumps
        forward. A **backward** jump breaks the attribution of file changes to steps, so the
        session escalates its next tool call with the jump as the reason: nothing runs
        unsupervised until a human has seen it. The human's answer to that escalation clears the
        alarm either way — allowing means "the clock change was legitimate, carry on".

        Limitation: an idle session has no next tool call, so the jump reaches the clients as a
        log event at once and escalates only when Claude acts again.

        Side effects: emits a `log` event; for a backward jump, arms the alarm that
        `StepGate.judge` reads.

        Args:
            jump: The detected jump.
        """
        if not jump.backward:
            await self._emit_safely(
                Log(level="warning", message=f"clock: {jump.describe()} (not an escalation)")
            )
            return
        self._run_state.clock_alarm = (
            f"The clock of the calu host was moved: {jump.describe()}. Until a human has seen "
            "this, calu cannot tell which step changed which file, because that attribution "
            "compares a file's change time with the times of the steps. Check what moved the "
            "clock before you let this session continue."
        )
        await self._emit_safely(Log(level="error", message=f"clock: {jump.describe()}"))
        await self._refresh_state()

    async def accepts_token(self, token: str) -> bool:
        """Tell whether a client may attach to this session with `token` (PLAN.md D30).

        Every token of this session counts until it is revoked: the one `calu run` printed and
        every one an operator minted (`calu token mint`). The protocol server asks the store
        the same question by hash (`SessionServer.observe`), for every token, and falls back to
        the printed token it holds in memory only when the database cannot be asked.

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
            escalations=self.desk.requests(),
            queue=[message for message, _ in self._run_state.queue],
            auto_escalate=self.config.auto_escalate,
            honesty_mode=self.config.honesty.mode,
            honesty_marks=[_mark_info(mark) for mark in self.honesty_marks],
            rate_limits=list(self._rate_limits),
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

        A turn is narrated and stored in a background task, so that neither delays Claude, and
        the `turn.result` event goes out before that task has finished. A client that reads the
        history as soon as a turn ends -- which is exactly what `calu attach` and the ACP
        adapter do -- would otherwise see one turn too few. This call therefore waits for the
        background work of the running session first, for at most `BACKGROUND_TIMEOUT_S`
        seconds; after that it answers with what the store holds, because a late history is
        worse than one that is a turn behind.

        Args:
            claude_session_id: Which session; None is the one that runs now.
            limit: Maximum number of steps returned (the newest ones).

        Returns:
            The turns, the steps and the chat summary; empty fields if the database cannot be
            read (the error is logged).
        """
        await self._wait_quiet()
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
        fork: Fork | None = None,
    ) -> None:
        """Start a Claude session in `tg` and return once it accepts messages.

        Side effects: creates a `sessions` row (state "starting"), restores the turns and the
        chat summary of the resumed session from the database, emits `startup_warnings` as
        `log` warnings, creates a runner and starts it as a task in `tg`.

        Args:
            tg: Task group that runs the session; it must outlive the session.
            resume_id: Claude session id to resume, or None for a new session.
            fork: Where to branch off the resumed conversation (PLAN.md D36); None resumes it
                whole. Needs `resume_id`; the forked conversation gets its own session id, so
                the one that was forked from is left untouched.
            model: Claude model alias or id for this and later sessions; None keeps the
                configured model.
            effort: Reasoning effort for this and later sessions; "default" uses Claude Code's
                default effort; None keeps the configured effort.

        Raises:
            SessionError: If a session is already running, if the runner ended before it was
                ready (the message names the runner's error), or if it was not ready within
                `START_TIMEOUT_S` seconds (the runner is stopped then).
        """
        self._run_state.claude_outputs.clear()  # text of an earlier session must not reach this one
        if self.state not in ("stopped", "failed") or not self._run_state.run_done.is_set():
            raise SessionError(f"a session is already {self.state}")
        overrides: dict[str, str | None] = {}
        if model is not None:
            overrides["model"] = model
        if effort is not None:
            overrides["effort"] = None if effort == "default" else effort
        if overrides:
            self.config.claude = self.config.claude.model_copy(update=overrides)
        # A run begins: everything that describes one run starts from its default, except
        # what `RunState.following` carries over.
        self._run_state = self._run_state.following(tg)
        await self.write_sets.prepare()
        self.state = "stopped"
        await self.recorder.attempt("creating the session row", self.store.start(self._identity()))
        await self._set_state("starting")
        await self._restore_context(resume_id)
        for warning in self.startup_warnings:
            await self.emit(Log(level="warning", message=warning))
        runner = self.runner = self.runner_factory()
        tg.start_soon(self._run, runner, resume_id, fork)

        ready = anyio.Event()

        async def wait_ready() -> None:
            """Wait until the runner is ready, then set `ready`."""
            await runner.wait_ready()
            ready.set()

        async with anyio.create_task_group() as waiters:
            waiters.start_soon(wait_ready)
            with anyio.move_on_after(START_TIMEOUT_S):
                await _first_of(ready, self._run_state.run_done)
            waiters.cancel_scope.cancel()

        if self._run_state.run_done.is_set():
            raise SessionError(
                f"Claude session ended during start: {self._run_state.run_error or 'exit'}"
            )
        if not ready.is_set():
            await runner.stop()
            raise SessionError(f"Claude session was not ready within {START_TIMEOUT_S} s")
        # As early as possible: a session that stops before its first turn (an escalation that
        # timed out, for example) must still be resumable by its Claude session id (D27).
        await self._record_claude_session(runner.session_id)
        await self._set_state("idle")

    async def _run(
        self, runner: ClaudeRunner, resume_id: str | None = None, fork: Fork | None = None
    ) -> None:
        """Run `runner` until it ends; then mark the session as stopped or failed.

        Never raises: runner errors are logged and reported as a `log` event.

        Args:
            runner: The runner of this session.
            resume_id: Claude session id to resume, or None.
            fork: Where to branch off the resumed conversation, or None (PLAN.md D36).
        """
        try:
            await runner.run(self, self._on_runner_event, resume_id, fork)
        except Exception as exc:
            log.exception("Claude session failed")
            self._run_state.run_error = f"{type(exc).__name__}: {exc}"
            hints = CLAUDE_SETUP_HINT
            if not self.config.claude.use_bedrock:
                hints += f" {BEDROCK_HINT}"
            if FORK_REJECTED in str(exc):
                # Deterministic: the fork point does not match the dropped turn (D36). Say so
                # instead of pointing at the authentication.
                hints = FORK_HINT
            await self._emit_safely(
                Log(level="error", message=f"Claude session failed: {exc} {hints}")
            )
        finally:
            self._run_state.run_done.set()  # first, so that `stop` never waits for a broken client
            with anyio.CancelScope(shield=True):
                await self.desk.cancel_all("The Claude session ended.")
                if self._run_state.queue:  # messages Claude did not read are lost with the session
                    self._run_state.queue.clear()
                    await self._emit_safely(ChatQueue(messages=[]))
                await self._wait_quiet()  # the last turn is recorded before the session ends
                final: State = "failed" if self._run_state.run_error else "stopped"
                await self._set_state_safely(final)
                await self.recorder.attempt(
                    "recording the end of the session", self.store.end(final)
                )
                await self.write_sets.discard()

    async def stop(self) -> None:
        """Stop the Claude session and wait until its runner has ended.

        Open escalations are cancelled (their tool calls are denied). Claude's text that was
        not narrated yet is dropped. Waits for the background work of the last turn (narration,
        storing the turn, folding), at most `BACKGROUND_TIMEOUT_S` seconds. Does nothing if no
        session runs and nothing is pending.
        """
        self._run_state.claude_outputs.clear()
        if not self._run_state.run_done.is_set():
            await self.desk.cancel_all("The session was stopped.")
            assert self.runner is not None
            await self.runner.stop()
            await self._run_state.run_done.wait()
        await self._wait_quiet()
        if self.state not in ("stopped", "failed"):
            await self._set_state_safely("stopped")

    async def interrupt(self) -> None:
        """Stop Claude's current turn; the session stays usable.

        Raises:
            SessionError: If no session runs.
        """
        runner = self._require_running()
        # the main agent's text of the interrupted turn
        self._run_state.claude_outputs.pop(None, None)
        await self.desk.turn_interrupted()
        await runner.interrupt()

    async def ask_side(self, question: str, history: Sequence[tuple[str, str]] = ()) -> str:
        """Ask Claude a question beside the running work (`side.ask`).

        The runner answers from the conversation as it stands, without tools. Nothing of the
        session changes: no message is sent to the main agent, no turn starts, the state stays
        what it is, the queue is untouched, and neither the question nor the answer is recorded
        or shown to the checkers. No step can be approved or taken this way.

        Args:
            question: The question.
            history: Earlier side questions of the asking client with their answers.

        Returns:
            Claude's answer.

        Raises:
            SessionError: If no session runs, or Claude Code gave no answer.
        """
        runner = self._require_running()
        try:
            return await runner.side_question(question, history)
        except SideQuestionFailed as exc:
            raise SessionError(str(exc)) from exc

    async def usage(self) -> UsageResult:
        """Return what the session knows about cost, context fill and usage limits (`usage.get`).

        Returns:
            The context fill as Claude Code counts it now (None when no session runs), the
            newest state of each usage limit, and cost and turn count of the last turn result.
        """
        context = None
        if self.runner is not None and self.state not in ("stopped", "failed", "starting"):
            context = await self.runner.context_usage()
        return UsageResult(
            context=context,
            rate_limits=list(self._rate_limits),
            cost_usd=self._last_cost,
            num_turns=self._last_turns,
        )

    async def set_model(self, model: str | None, client: str = "unknown") -> None:
        """Change the model of the running Claude session (`model.set`).

        Side effects: the runner applies the model from Claude's next model request; the
        configured `claude.model` of this process changes with it, so the snapshot and later
        starts use it; a `log` event names the change and who made it.

        Args:
            model: A Claude Code alias or model id; None for Claude Code's default model.
            client: Name of the client that asked, for the log.

        Raises:
            SessionError: If no session runs or Claude Code refused the change.
        """
        runner = self._require_running()
        try:
            await runner.set_model(model)
        except ModelChangeFailed as exc:
            raise SessionError(str(exc)) from exc
        self.config.claude = self.config.claude.model_copy(update={"model": model})
        await self._emit_safely(
            Log(message=f"model changed to {model or 'the default'} by {client}")
        )

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
        self._run_state.queue.append((queued, text))
        await self._emit_queue()
        await self._mark_busy()

    async def _emit_queue(self) -> None:
        """Send `chat.queue` with the messages that Claude has not taken in yet."""
        await self.emit(ChatQueue(messages=[message for message, _ in self._run_state.queue]))

    async def _take_from_queue(self, text: str) -> None:
        """Remove the oldest queued message whose sent text is `text`, then send `chat.queue`.

        Does nothing if no queued message matches.

        Args:
            text: The text of a user message that Claude took in.
        """
        for i, (_, sent) in enumerate(self._run_state.queue):
            if sent.strip() == text.strip():
                del self._run_state.queue[i]
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
        await self.recorder.attempt(
            "storing a comment", self.store.add_comment(comment, self.context.turn_number)
        )
        return comment

    async def decide_escalation(
        self,
        escalation_id: str,
        decision: str,
        note: str | None,
        client: str = "",
        answer: str | None = None,
        token_sha256: str | None = None,
    ) -> None:
        """Resolve an open escalation from a client. Only the first decision counts.

        The decision is recorded in the `escalations` row as the resolution it is, with
        `resolved_by` saying that a client of this session decided and which one (PLAN.md D28).
        A client needs no database login for that: it authenticates to the session with its
        token, and the session speaks to the database for it. An operator who resolves the row
        in the database instead reaches the session through `note_resolution` or its poll.

        The database decides first, and the step is released only by a decision it accepted:

            store.decide_escalation(...)    step                      this call
            True                            released with the answer  returns
            False (row no longer open, or   stays held; the poll      raises SessionError
              this session may not decide)  adopts what the row says
            raises, or no answer within     stays held; the poll      raises SessionError
              `STORE_DECISION_TIMEOUT_S`    adopts what the row says

        The write and the release happen in a scope shielded from cancellation, because the
        request of a client that disconnects right after sending it is cancelled: the row and
        the step must not end up saying different things. The write is bounded, so that a
        database which does not answer cannot keep every later decision out (`esc.deciding`).

        Args:
            escalation_id: Id from the `escalation.request` event.
            decision: "allow" or "deny"; ignored when `answer` is given.
            note: Optional text from the user; Claude receives it with a denial.
            client: Name of the client that decided, for the record.
            answer: The answer to a question of the agent (`escalation.request` with
                `questions`, PLAN.md D35). The call does not run; Claude receives this text as
                its result, and the row is recorded as "answered".
            token_sha256: SHA-256 of the token the deciding client authenticated with, which
                the database checks before it records the decision; None uses a token of this
                session that the store remembers.

        Raises:
            SessionError: If no escalation with this id is open (unknown, already decided, or
                another decision for it is being recorded), if the database refused the
                decision, or if the decision could not be written. In the last two cases the
                step has not been released. Also if the escalation was cancelled (stop,
                timeout, interrupt) while the decision was written: the step did not run.
        """
        try:
            await self.desk.decide(escalation_id, decision, note, client, answer, token_sha256)
        except EscalationError as exc:
            raise SessionError(str(exc)) from exc

    def note_resolution(self, escalation_id: int) -> None:
        """Wake the session so it reads a resolved escalation at once.

        Called with the id from an `escalation_resolved` notification (`calu.db.notify`). An
        unknown id is ignored; without this call the session notices the resolution with its
        next poll (`ESCALATION_POLL_S`).

        Args:
            escalation_id: Id of the resolved `escalations` row.
        """
        self.desk.note_resolution(escalation_id)

    # ------------------------------------------------------------------ ToolGate

    async def pre_tool_use(self, step: Step) -> GateResult:
        """Decide about a tool call before it runs (ToolGate).

        Side effects: records the step in the database and in the context, emits `edit.diff`
        (edits) and `check.result`, and remembers the reason of an escalation for `ask`. While
        an escalation of the session is open or being opened, a call that needs a check waits
        in the gate first (`StepGate.judge`), and a verdict ASK makes this step count as an
        escalation that is being opened (`EscalationDesk.expect_ask`).

        The verdict is fixed in guarded stages, so that no error and no hanging model can let a
        call run unchecked:

            stage                         failure                      verdict
            record the step, the gate's   exception                    ASK
            decision (`StepGate.decide`)
                                          `GATE_BUDGET_S` exceeded     ASK
            record the decision           exception after PASS/ALLOW   ASK
                                          exception after DENY/ASK     unchanged
            last look before the answer   an escalation opened since   ASK instead of
            (`StepGate.last_look`)        the gate looked              PASS/ALLOW

        Args:
            step: The tool call.

        Returns:
            ALLOW, DENY (with the reason for Claude) or ASK. Any internal error yields ASK, so
            a human decides instead of the call running unchecked.
        """
        pending = PendingNarration(self._run_state.claude_outputs.pop(step.agent_id, []))
        step_id: int | None = None
        try:
            step_id = await self.recorder.add_step(step)
            decided = await self.gate.decide(step, pending)
        except Exception as exc:
            log.exception("pre_tool_use failed")
            decided = self.gate.failed(step, exc)
        result, source, comment = decided.decision, decided.source, ""
        if result.decision is Decision.ESCALATE:
            # From here on no other step is approved, although the escalation's row does not
            # exist yet: recording comes first, then Claude Code calls `ask`.
            self.desk.expect_ask(step, source)
        try:
            comment = await self._record_check(step, step_id, pending, decided)
        except Exception as exc:
            log.exception("recording the decision about a step failed")
            comment = ""
            if result.decision.approves:
                result = unrecorded(result, exc)
                source = "internal"
                self._run_state.peek(step.tool_use_id or "").running = False
                self._run_state.release(step.tool_use_id or "")
        if (late := self.gate.last_look(result, source)) is not None:
            # The gate looked last before the decision was recorded, and recording takes time.
            # No await stands between this look and the answer to Claude Code.
            result = late
            source, comment = "held", ""
            self._run_state.peek(step.tool_use_id or "").running = False
            self._run_state.release(step.tool_use_id or "")
        if result.decision is Decision.ESCALATE:
            self.desk.expect_ask(step, source)
            if step.tool_use_id:
                self._run_state.call(step.tool_use_id).pending_reason = (result.reason, source)
        verdict = {
            Decision.PASS: Verdict.PASS,
            Decision.ALLOW: Verdict.ALLOW,
            Decision.DENY: Verdict.DENY,
            Decision.ESCALATE: Verdict.ASK,
        }[result.decision]
        context = ""
        if comment:
            context = CHECKER_COMMENT_TEMPLATE.format(route=decided.route.value, comment=comment)
        return GateResult(verdict=verdict, reason=result.reason, context=context)

    async def _record_check(
        self, step: Step, step_id: int | None, pending: PendingNarration, decided: Decided
    ) -> str:
        """Record the decision about a step everywhere it belongs, after it was made.

        Side effects: adds the step to the checkers' context, records the checker runs, the
        honesty marks and the decision in the database, emits `check.result`, counts an
        approved call as running, and takes the "before" snapshot of an approved command (D31).

        Args:
            step: The tool call.
            step_id: Its `steps` row, or None if it could not be stored.
            pending: Claude's thinking and text before the call, with the narration if made.
            decided: What `StepGate.decide` returned.

        Returns:
            The checker's remark for Claude on an approved step, or "" if there is none or the
            same remark was already sent in this turn.

        Raises:
            Exception: Whatever a part of the bookkeeping raises; `pre_tool_use` then turns an
                approval into ASK.
        """
        route, result = decided.route, decided.decision
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
            self.recorder.summarize_time(
                self._run_state.peek(step.tool_use_id).time_seq or 0, result.step_summary
            )
        if (
            pending.outputs
            and pending.text is None
            and self.narrator
            and self._run_state.tg is not None
        ):
            # No checker ran (rules, policy): narrate for the context without delaying the call.
            self._spawn(self._narrate_record, record, pending.outputs)
        if step_id is not None:
            for checker, run in decided.runs:
                await self.recorder.add_checker_run(
                    "recording a checker run",
                    step_id,
                    checker,
                    self.gate.checker_route(checker, route),
                    run,
                )
            await self._apply_honesty(decided, step_id, step)
            await self.recorder.attempt(
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
        if result.decision.approves and step.tool_use_id:
            self._run_state.call(step.tool_use_id).running = True
        await self._refresh_state()
        if result.decision.approves and route in (Route.COMMAND, Route.CHANGESET):
            # The command runs as soon as this returns, so this is the "before" of its write set
            # (D31). Edits need no snapshot: their content came through the gate. (The changeset
            # route is only reached by a tool that the tool policy sends there.)
            await self.write_sets.snapshot_before(step, step_id, result.step_summary)
        return comment

    async def ask(self, step: Step) -> HumanAnswer:
        """Ask the human about `step` and wait for the answer, without a time limit (ToolGate).

        Side effects: opens an `escalations` row, emits `escalation.request`, sets the state to
        `escalated`, and emits `escalation.resolved` when the escalation ends — also when it is
        cancelled (stop, interrupt, end of the session), with decision "cancelled". The row is
        closed in every case: if this call itself is cancelled while it waits (Claude Code
        cancels the permission callback on an interrupt), the row is cancelled with a note.

        Args:
            step: The tool call that needs a decision.

        Not every escalation comes from a checker: the SDK calls `can_use_tool` for any call
        that needs permission, so a step the checkers passed can still arrive here because
        Claude Code's own approval decided to ask (D38). Then there is no stored reason, and
        `HARNESS_ASKED` says so instead of inventing one.

        Returns:
            The human's answer. An escalation that ended without a human decision (stop,
            timeout, interrupt) counts as a denial and has `cancelled` set. If the escalation
            cannot be stored, the step is denied with `DB_UNREACHABLE`, because no human could
            see it.
        """
        return (await self._ask(step)).answer

    async def _ask(self, step: Step) -> Asked:
        """Implement `ask`, and also say how the escalation ended.

        Side effects: as `ask`.

        Args:
            step: The tool call that needs a decision.

        Returns:
            The answer and the outcome (see `Outcome`). The outcome is what the reviews after a
            step act on: only "denied" is a refusal by a human.
        """
        call = self._run_state.peek(step.tool_use_id or "")
        pending_reason, call.pending_reason = call.pending_reason, None
        self._run_state.release(step.tool_use_id or "")
        reason, source = pending_reason or (HARNESS_ASKED, "harness")
        asked = await self.desk.ask(step, reason, source)
        if asked.answer.allow and step.tool_use_id:
            self._run_state.call(step.tool_use_id).running = True
            await self._refresh_state()
        return asked

    def _stop_soon(self) -> None:
        """Start stopping the session in a task of its own, if a run has a task group.

        For callers that run inside a tool call that is being held: waiting for the runner to
        end there would deadlock.
        """
        if self._run_state.tg is not None:
            # Not a tracked background task: `stop` waits for those, and this one calls it.
            self._run_state.tg.start_soon(self._stop_after_timeout)

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
        the human like a tool call (`escalation.request` with tool name "SubagentResult"). The
        note reports a "Decision of the user" only if a human allowed or denied; an escalation
        that timed out or could not be stored is reported as undecided.

        Args:
            result: The subagent's task and final result.

        Returns:
            The note for the main agent ("" if the result was accepted without remarks or the
            review is disabled by `check_subagent_results = false`).
        """
        # The subagent's text after its last tool call is its result, reviewed below.
        self._run_state.claude_outputs.pop(result.agent_id, None)
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
        step_id = await self.recorder.add_step(step)
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
            await self.recorder.attempt(
                "recording a checker run",
                self.store.add_checker_run(
                    step_id,
                    CheckerRun(
                        checker_name="subagent-result",
                        checker_version=self.recorder.version("subagent-result"),
                        model=model_name(self.result_checker.model),
                        decision="escalate" if escalated else "pass",
                        reason=review.note,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                        cache_read_tokens=outcome.cache_read_tokens,
                        duration_ms=outcome.duration_ms,
                    ),
                ),
            )
            await self.recorder.attempt(
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
            await self.recorder.attempt(
                "recording a step result",
                self.store.finish_step(step_id, note or "accepted"),
            )
        if escalated:
            self._run_state.call(step.tool_use_id or "").pending_reason = (
                note,
                "subagent_result",
            )
            asked = await self._ask(step)
            answer = asked.answer
            if asked.outcome in ("interrupted", "cancelled"):
                return ""  # the turn or the session is over; there is nobody to tell
            if asked.outcome in ("allowed", "denied"):
                if answer.allow:
                    decision = "The user accepts the result; act on it as appropriate."
                else:
                    decision = "The user does not want you to act on this result now."
                if answer.note:
                    decision += f" Note from the user: {answer.note}"
                note = f"{note}\n\nDecision of the user: {decision}".strip()
            else:
                # Nobody decided: saying "the user does not want ..." would invent a decision.
                why = NOBODY_ASKED.get(asked.outcome, "No decision was made.")
                note = (
                    f"{note}\n\nNo human decided about this result: {why} Do not act on it "
                    "until the user has seen it."
                ).strip()
            if step_id is not None:
                await self.recorder.attempt(
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
        try:
            async with self._checking_scope():
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

    # ------------------------------------------------------------------ deciding

    def _take_clock_alarm(self) -> str | None:
        """Hand the pending clock alarm to the gate and forget it (`clock_jumped`, D32).

        Returns:
            The reason of a backward clock jump that no step has shown to a human yet, or None
            if there is none. After the call there is none.
        """
        alarm, self._run_state.clock_alarm = self._run_state.clock_alarm, None
        return alarm

    def _keep_changeset(self, tool_use_id: str, text: str) -> None:
        """Remember the rendered changeset of a commit, for the escalation's row.

        Args:
            tool_use_id: Claude's id of the commit's tool call.
            text: The changeset as the escalation's context shows it.
        """
        self._run_state.call(tool_use_id).changeset = text

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

        The session's state does not depend on the display: the event is forwarded, and the
        busy flag and the state are brought up to date, also when the handling of the event
        raised, and a client sink that raises is logged and changes nothing. After a
        `turn.result` the session is therefore never left "running" with a finished Claude.

        Args:
            event: The event from the runner.

        Raises:
            Exception: Whatever the handling of the event itself raised (recording it in the
                context or the store); the forwarding and the state update have happened then.
        """
        try:
            match event:
                case UserInput() if event.source != "user":
                    self._record_cross_session(event.text, event.source)
                case UserInput():
                    self.context.user_input(event.text, event.uuid)
                    await self._take_from_queue(event.text)
                case AssistantText() if event.agent_id is None and event.uuid:
                    # Where a fork that keeps this turn resumes from (D36); the narrator case below
                    # also wants this event, so this branch records and falls through to it.
                    self.context.assistant_entry(event.uuid)
                    if self.narrator is not None:
                        outputs = self._run_state.claude_outputs.setdefault(event.agent_id, [])
                        outputs.append(ClaudeOutput("text", event.text))
                        del outputs[:-MAX_PENDING_OUTPUTS]
                case AssistantText() | AssistantThinking() if self.narrator is not None:
                    kind = "thinking" if isinstance(event, AssistantThinking) else "text"
                    outputs = self._run_state.claude_outputs.setdefault(event.agent_id, [])
                    outputs.append(ClaudeOutput(kind, event.text))
                    del outputs[:-MAX_PENDING_OUTPUTS]
                case ToolUse() if event.name in AGENT_TOOLS:
                    # The subagent's own id is this call's id; the caller is its parent (D29).
                    await self.recorder.attempt(
                        "recording a subagent",
                        self.store.add_agent(event.tool_use_id, event.agent_id),
                    )
                case AgentsUpdate():
                    await self.recorder.attempt(
                        "recording the subagents", self.store.update_agents(event.agents)
                    )
                case ToolResult():
                    try:
                        await self.write_sets.snapshot_after(event.tool_use_id)
                    finally:
                        # The call is over whatever the measurement did: it must not stay
                        # "running" in the state.
                        self.recorder.finish_time(event.tool_use_id)
                        call = self._run_state.peek(event.tool_use_id)
                        call.pending_reason = None
                        call.running = False
                        call.changeset = None
                        self._run_state.release(event.tool_use_id)
                        self.desk.call_ended(event.tool_use_id)
                    call = self._run_state.peek(event.tool_use_id)
                    step_id, call.step_id = call.step_id, None
                    self._run_state.release(event.tool_use_id)
                    if step_id is not None:
                        await self.recorder.attempt(
                            "recording a step result",
                            self.store.finish_step(step_id, result_summary(event)),
                        )
                case TurnResult():
                    self._last_cost = event.cost_usd
                    self._last_turns = event.num_turns
                    await self._on_turn_result(event)
                case UsageUpdate():
                    self._rate_limits = list(event.rate_limits)
                case _:
                    pass
        finally:
            try:
                await self._emit_safely(event)
                await self._after_event(event)
            except Exception:
                log.exception("the follow-up of %s failed", event.method)
            finally:
                if isinstance(event, TurnResult):
                    self._run_state.busy = False
                    await self._refresh_state()
                elif not isinstance(event, UsageUpdate):
                    # A report about the account's limits is no work of Claude.
                    await self._mark_busy()

    async def _after_event(self, event: Event) -> None:
        """Emit what follows from a forwarded runner event: a login hint, a command's files.

        Args:
            event: The event that was just forwarded to the clients.
        """
        if isinstance(event, AssistantText) and event.agent_id is None:
            await self._hint_on_login_error(event.text)
        if isinstance(event, TurnResult) and event.is_error:
            await self._hint_on_login_error(event.result)
        if isinstance(event, ToolUse) and event.name == "Bash":
            await self._emit_command_files(event)

    async def _on_turn_result(self, event: TurnResult) -> None:
        """Record Claude's session id and close the running turn.

        Args:
            event: The turn result.
        """
        await self._record_claude_session(event.session_id)
        outputs = self._run_state.claude_outputs.pop(None, [])
        if self.context.current is not None:
            # Recorded (and persisted) with the turn: never show the verbatim answer.
            self.context.current.narrated = self.narrator is not None
        turn = self.context.end_turn(event.result)
        if turn is not None and self._run_state.tg is not None:
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
        await self.recorder.attempt(
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
        narrator = self.narrator
        try:
            async with self._checking_scope():
                with anyio.fail_after(NARRATOR_DEADLINE_S):
                    return await narrator.narrate(outputs)
        except Exception:
            log.exception("narrating Claude's output failed")
            return ""

    def _record_cross_session(self, text: str, source: str = "cross_session") -> None:
        """Record a message that is not the user's as a turn entry, not as user input.

        The entry's summary is a placeholder until the narrator has retold the message (in the
        background); with narration off it stays a fixed text. The message is never put into
        the checkers' context verbatim.

        Args:
            text: The message as Claude Code delivered it.
            source: `UserInput.source` of the message: "cross_session" for a message from
                another Claude session; every other value is recorded as an automatic message
                of calu or Claude Code (`NOT_FROM_USER_LABEL`).
        """
        cross = source == "cross_session"
        label = CROSS_SESSION_LABEL if cross else NOT_FROM_USER_LABEL
        record = StepRecord(tool_name=label, summary=CROSS_SESSION_PENDING)
        if self.narrator is None:
            record.summary = CROSS_SESSION_HIDDEN
        self.context.step(record)
        if self.narrator is not None and self._run_state.tg is not None:
            self._spawn(self._narrate_cross_session, record, text, "message" if cross else "notice")

    async def _narrate_cross_session(
        self, record: StepRecord, text: str, kind: Literal["message", "notice"] = "message"
    ) -> None:
        """Narrate a message that is not the user's into its turn entry; never raises.

        Args:
            record: The turn entry created by `_record_cross_session`.
            text: The message.
            kind: "message" for a message from another Claude session, "notice" for an
                automatic message of calu or Claude Code.
        """
        narration = await self._narrate_safely([ClaudeOutput(kind, text)])
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
        await self.recorder.attempt("storing a turn", self.store.save_turn(turn))
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
        await self.recorder.attempt(
            "storing the chat summary", self.store.save_summary(summary, covered_turns)
        )
        await self.recorder.attempt("marking folded turns", self.store.mark_folded(covered_turns))

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

    def _escalation_row_context(self, step: Step, rendered: str) -> dict[str, Any]:
        """Return the context that is stored with the `escalations` row of a step.

        The context holds what an operator needs to decide and what an evaluation case needs
        to reproduce the step later (`calu.evals.cases`): the rendered step, the tool input with
        workspace-relative paths, and the content of the file an edit would change.

        Args:
            step: The tool call.
            rendered: The step rendered as Markdown, for the operator.

        Returns:
            The context, as `SessionStore.open_escalation` stores it.
        """
        return {
            "rendered": rendered,
            "tool_name": step.tool_name,
            "tool_input": workspace_relative_input(step.tool_input, self.workspace),
            "agent_id": step.agent_id,
            "files": files_before(step, self.workspace),
            "changeset": self._run_state.peek(step.tool_use_id or "").changeset or "",
            "recent": self.context.recent(),
            "chat_summary": self.context.summary,
            "calu_version": __version__,
        }

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
        if self._run_state.tg is None:
            return
        if self._pending == 0:
            self._quiet = anyio.Event()
        self._pending += 1
        self._run_state.tg.start_soon(self._tracked, func, *args)

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
        if self.desk.escalations:
            return "escalated"
        if self._run_state.checking > 0:
            return "checking"
        if self._run_state.any_running():
            return "tool_call"
        return "running" if self._run_state.busy else "idle"

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
            await self.recorder.attempt("recording the session state", self.store.set_state(state))
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
        self._run_state.busy = True
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

    async def _apply_honesty(self, decided: Decided, step_id: int, step: Step) -> None:
        """Write the honesty marks of a step's verdicts and reload the register (D82).

        Every checker of the step may contribute marks, so the marks of all of them are applied
        together. A mark for a spot that is already open refreshes its reason; a clean mark that
        matches nothing open is counted and reported as such, because a checker claiming to
        have cleaned what was never dirty is worth seeing. Nothing happens while
        `honesty.mode` is "off".

        A mark whose path is absolute, escapes the project, or (for a dirty mark) names a file
        that does not exist is dropped and reported, because the path is text a model wrote and
        nothing else checks it (D112).

        Side effects: changes `self.register` (in memory, D180), emits a `log` event naming what
        changed and `honesty.update` for the clients.

        Args:
            decided: The verdicts of the step, with the marks their checkers asked for.
            step_id: The step the marks belong to.
            step: The tool call itself, whose file is snapshotted with a mark it opens so that
                an evaluation case built from the mark can be reproduced (D95). This runs in
                the gate, before the tool has run, so the snapshot is the state the checker
                judged.
        """
        if self.config.honesty.mode == "off":
            return
        with_marks = [
            (checker.name, run.decision)
            for checker, run in decided.runs
            if run.decision.marks_dirty or run.decision.marks_clean
        ]
        if not with_marks:
            return
        # The paths come out of a model's structured answer, so they are checked before they
        # reach the register: an invented path would otherwise sit there until a person cleared
        # it, or block the commands that run an unrelated file (D112).
        refused: list[str] = []
        checked: list[tuple[str, list[CodeMark], list[CodeMark]]] = []
        # The paths of the step itself count as existing: the gate runs before the tool, so a
        # mark about the file a `Write` is creating names a file that is not there yet.
        step_paths = set(files_before(step, self.workspace)) | paths_of_step(step, self.workspace)
        for name, decision in with_marks:
            dirty, bad_dirty = checked_marks(
                decision.marks_dirty, self.workspace, step_paths, must_exist=True
            )
            clean_marks, bad_clean = checked_marks(
                decision.marks_clean, self.workspace, must_exist=False
            )
            refused += [f"{name}: {message}" for message in bad_dirty + bad_clean]
            if dirty or clean_marks:
                checked.append((name, dirty, clean_marks))
        if refused:
            await self._emit_safely(
                Log(
                    level="warning",
                    message="honesty: ignored " + "; ".join(refused),
                )
            )
        if not checked:
            return
        clean = [mark for _name, _dirty, marks in checked for mark in marks]
        opened = closed = 0
        for name, dirty, clean_marks in checked:
            # One call per checker, so that each mark records which checker opened it: with
            # several checkers on a route (D81), "12 marks are open" says nothing about any one
            # of them (D87). The register is in memory (D180): nothing to fail.
            applied = self.register.apply(
                dirty,
                clean_marks,
                name,
                # The file as it is before the step runs (D95), and what the step was about,
                # so that a mark on any other file shows as reaching beyond it (D141).
                files_before(step, self.workspace),
                step_paths,
            )
            opened += applied[0]
            closed += applied[1]
        parts: list[str] = []
        if opened:
            parts.append(f"{opened} spot(s) recorded as dishonest")
        if closed:
            parts.append(f"{closed} cleared")
        if len(clean) > closed:
            parts.append(f"{len(clean) - closed} clean mark(s) matched nothing recorded")
        if parts:
            await self._emit_safely(Log(level="info", message="honesty: " + ", ".join(parts)))
        await self._emit_honesty()

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

    def _own_agent(self, step: Step) -> bool:
        """Tell whether a `SendMessage` call addresses a subagent of this session (D69).

        `SendMessage` is unchecked because it is how calu relays a client's message to one of
        the session's own subagents (`chat.send {to: <agent id>}`), whose every tool call is
        checked anyway. Claude Code's tool can also reach other sessions on this machine, and
        their steps are outside this gate, so only a message to a known subagent keeps the
        free route. calu's own relay always matches: the id it tells the main agent to use
        comes from this registry.

        A call without a target counts as foreign: a message that names nobody calu knows is
        not a relay.

        Args:
            step: The tool call; its `tool_input` is expected to carry `to`.

        Returns:
            True if `to` is the protocol id, the SDK id or the description of a subagent of
            this session; False for anything else, including when no session runs.
        """
        target = str(step.tool_input.get("to", "")).strip()
        if not target or self.runner is None:
            return False
        known: set[str] = set()
        for agent in self.runner.agents.values():
            known.update(
                value for value in (agent.agent_id, agent.sdk_agent_id, agent.description) if value
            )
        return target in known

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


def _mark_info(mark: HonestyMark) -> HonestyMarkInfo:
    """Return an honesty mark as the protocol carries it (PLAN.md D86).

    Args:
        mark: The mark as the register holds it.

    Returns:
        The same spot and reason, without the opened/closed bookkeeping a client does not need.
    """
    return HonestyMarkInfo(
        id=mark.id,
        path=mark.path,
        symbol=mark.symbol,
        reason=mark.reason,
        git_branch=mark.git_branch,
        beyond_step=mark.beyond_step,
    )


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
