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
    `honesty.mode` enforce  -> a command that executes project code, and a commit, need a
                               clean honesty state: deny with the open marks, or ask the
                               human (no baseline, or the honesty review did not verify the
                               unreviewed files); see `calu.gate`
    any internal error      -> ask the human, also after the decision (it never weakens a
                               DENY or an ASK)
    no answer within `GATE_BUDGET_S` -> ask the human
    ask -> an `escalations` row and an `escalation.request` event; waits without a time limit

Code honesty (`calu.honesty`, while `honesty.mode` is not "off"). The session holds the
honesty state (`Session.honesty`) and keeps it up to date:

    start, a moved HEAD, `trust_changed`,     the baseline and the stored file trust are read
    `honesty.trust`                           again (`_refresh_trust`)
    a step reaches the gate                   the state is computed (`honesty_snapshot`) and
                                              given to the gate and, as a note, to the checkers
    the checkers answered                     their marks and the edit's expected content are
                                              kept with the call (`_keep_honesty`), not applied
    the checkers of a command or a commit     their dirty marks are recorded at once
    answered                                  (`_mark_at_once`): they name existing code
    the tool result arrived without error     they are applied (`_apply_honesty`): marks,
                                              the verification of a checked edit, the
                                              certification of a commit made in a clean state
    the call was denied or failed             they are dropped
    a step needs a clean state (the gate),    the honesty review judges the unreviewed and the
    `honesty.review`                          marked files (`_run_review`): one at a time, and
                                              not again by itself while nothing changed

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

import functools
import getpass
import itertools
import logging
import os
import platform
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
from anyio.abc import TaskGroup
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from calu import __version__, asuser, machine
from calu.changes import default_root
from calu.checkers import (
    CheckContext,
    CheckDecision,
    Checker,
    CheckOutcome,
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
from calu.checkers.honesty_review import HonestyReviewer, batches
from calu.checkers.policy import EDIT_TOOLS
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
from calu.claude.transcripts import unwitnessed_turn
from calu.clock import ClockJump
from calu.comments import Comment, format_comment, map_selection
from calu.comments.mapping import excerpt, split_lines
from calu.config import EMPTY_CHAT_SUMMARY, AutoEscalate, CheckerConfig, Effort, default_model
from calu.context import ContextBuilder, StepRecord, TurnRecord
from calu.context.narrator import ClaudeOutput, Narrator
from calu.defaults import default_instructions
from calu.defaults.instructions import compose_instructions, review_instructions
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
    NOTE_LIMIT,
    NOTE_PATH_CHARS,
    FileReview,
    FileSeen,
    GitError,
    HonestyMark,
    HonestySnapshot,
    HonestyState,
    MarkChanges,
    Reviewed,
    ReviewPlan,
    find_baseline,
    resolve_commit,
    run_git,
    shown,
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
    HonestyInfo,
    HonestyMarkInfo,
    HonestyReviewResult,
    HonestyTrustResult,
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
from calu.run_state import PendingHonesty, RunState
from calu.steps import files_before as files_before
from calu.steps import result_summary
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


REVIEW_CERTIFIER = "calu honesty review"
"""Who an honesty review records as the certifier of what it verified."""


REVIEW_RETRY_S = 60.0
"""How long after a failed honesty review (model error, timeout, malformed answer) calu starts
no review by itself: the steps that need one in that time get the failure as their answer and
are escalated. A person's request runs a review at any time."""

REVIEW_PASSES = 3
"""How many reviews one request runs at most, one after the other: a further one follows a
review that dropped results because files changed while it ran, and a review that another
caller started before this request."""


@dataclass
class ReviewRun:
    """The honesty review in flight (`Session._run_review`)."""

    fresh: bool = False
    """True if a person asked for this review. Only such a review runs although the last one
    looked at the same files, and only such a review certifies HEAD without a baseline."""
    dropped: bool = False
    """Set when the review's result was applied except for files that changed while it ran."""
    stopped: bool = False
    """Set when the review ended because the run of the session ended."""
    done: anyio.Event = field(default_factory=anyio.Event)
    """Set when the review ended, whatever its outcome."""
    result: HonestyReviewResult | None = None
    """The review's answer, once `done` is set."""
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    """Around the review's work; cancelled when the run of the session ends."""


CERTIFIER = "calu session"
"""Who certified, for a commit or a file content the session records itself."""


class SessionError(Exception):
    """A request cannot be served in the current state (reported to the client)."""


def _os_user() -> str:
    """Name the OS user a session works for, for its `sessions` row.

    Returns:
        The user calu switched to (`calu.asuser.enter`): its login name, or its uid as text
        when it has none. Otherwise the user this process runs as.
    """
    run_as = asuser.active()
    if run_as is not None:
        return run_as.user or str(run_as.uid)
    return getpass.getuser()


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
        transcript_inputs: Callable[[str], list[str]] | None = None,
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
                "subagent", "narrator", "summarizer" and "honesty-review" (tests pass fake
                models here).
            startup_warnings: Warnings about missing prerequisites, emitted as `log` warnings
                at every `start`, before the runner starts.
            snapshot_root: Where the optional write-set snapshots live (PLAN.md D31); None
                uses `calu.changes.default_root()`.
            transcript_inputs: Reads what the user typed in a conversation from Claude Code's
                own transcript, by Claude session id (it may block: it is run in a worker
                thread). Used when a conversation is resumed or forked that the store knows
                nothing about, because it was started without calu (PLAN.md D209); None
                starts such a session without that context.
        """
        self.transcript_inputs = transcript_inputs
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
        self.honesty = HonestyState(
            workspace, self.config.honesty.max_open_marks, self.config.honesty.sufficient_trust
        )
        """The code honesty state of this session, in memory only (`calu.honesty`): marks,
        verified contents, the baseline. Unused while `honesty.mode` is "off"."""
        self._honesty_lock = anyio.Lock()
        """Held while the honesty state is refreshed, read as a snapshot or changed, so that a
        snapshot never sees half of a tool result's changes."""
        self._trust_stale = True
        """True when the baseline and the stored file trust must be read from the database
        again: at the start, after a trust command, after `trust_changed` and after a failure."""
        self._written: set[str] = set()
        """Files that measured write sets named and the honesty state does not watch yet
        (`_watch_written`); the next worker-thread call of the state takes them."""
        self._honesty_head: str | None = None
        """The HEAD the baseline was last looked up for; a different HEAD looks it up again."""
        self._review_running = False
        """True while an honesty review runs; at most one runs at a time."""
        self._review_run: ReviewRun | None = None
        """The honesty review in flight, if any; a second caller waits for it."""
        self._last_review: tuple[Reviewed, HonestyReviewResult] | None = None
        """What the last honesty review whose result was applied looked at, and its answer.
        calu starts no review by itself while a new review would read only that
        (`Reviewed.covers`). A failed review is not kept here."""
        self._review_failed: tuple[float, str] | None = None
        """When the last honesty review failed (`anyio.current_time`) and its problem; None
        once a review's result was applied. calu starts no review by itself within
        `REVIEW_RETRY_S` of that time."""
        self._honesty_sent = HonestyInfo()
        """What the last `honesty.update` said, so an unchanged state is not sent again."""
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
            clock_alarm=self._clock_alarm,
            keep_changeset=self._keep_changeset,
            honesty=self._gate_honesty,
            honesty_review=lambda _step, wait_s: self.honesty_review(wait_s=wait_s),
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
            run_checkers=self._review_checkers,
            check_context=lambda: self.context.check_context(),
            wrote=self._watch_written,
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

    # ------------------------------------------------------------------ code honesty

    async def clear_mark(self, mark_id: int, client: str = "unknown") -> HonestyMark:
        """Close one open mark by hand: a person's override of a checker (`honesty.clear`).

        Side effects: closes the mark in the honesty state, logs who did it and sends
        `honesty.update`. Whether a review is required does not change, and nothing is
        verified or stored: the file counts as it did without the mark.

        Args:
            mark_id: The mark's id, as `honesty.update` reported it.
            client: Who asked, for the log.

        Returns:
            The closed mark.

        Raises:
            SessionError: If no open mark has that id.
        """
        async with self._honesty_lock:
            closed = await anyio.to_thread.run_sync(
                self.honesty.clear, mark_id, f"{client} (by hand)"
            )
        if closed is None:
            raise SessionError(f"no open honesty mark {mark_id}")
        await self._emit_safely(
            Log(level="info", message=f"honesty: {client} cleared {closed.label()} by hand")
        )
        await self.announce_honesty()
        return closed

    async def trust(
        self, rev: str = "HEAD", path: str | None = None, note: str = "", client: str = "unknown"
    ) -> HonestyTrustResult:
        """Certify a commit or one file's present content for a person (`honesty.trust`).

        Without `path`, `rev` is resolved with git in the workspace to the commit it names now,
        and that commit is recorded as trusted with the source "user". With `path`, the SHA-256
        of the file's present content is recorded with the source "user", and the file is
        watched from now on. Works in every `honesty.mode`.

        Side effects: writes to the database through `SessionStore.certify_commit` or
        `SessionStore.trust_file`, looks up the baseline again, logs who certified what and
        sends `honesty.update`.

        Args:
            rev: A commit id or its prefix, a branch, a tag or "HEAD".
            path: A file, absolute or relative to the workspace; None certifies a commit.
            note: An optional note, stored with the record.
            client: The client's name, stored as who certified.

        Returns:
            What was recorded.

        Raises:
            SessionError: If `rev` names no commit, the file cannot be read, or the database
                refused the record.
        """
        by = client or "unknown"
        try:
            if path:
                key = await anyio.to_thread.run_sync(self.honesty.watch, path)
                sha256 = await anyio.to_thread.run_sync(self.honesty.content_sha256, key)
                if sha256 is None:
                    raise SessionError(f"{path} is not a file calu can read")
                recorded = await self.store.trust_file(sha256, key, "user", by, note)
                result = HonestyTrustResult(content_sha256=sha256, path=key, recorded=recorded)
                what = f"the content of {key} ({sha256[:12]})"
            else:
                commit = await anyio.to_thread.run_sync(
                    resolve_commit, self.workspace, rev or "HEAD"
                )
                recorded = await self.store.certify_commit(
                    commit.commit_id, commit.tree_id, "user", by, note
                )
                result = HonestyTrustResult(commit_id=commit.commit_id, recorded=recorded)
                what = f"commit {commit.short}"
        except GitError as exc:
            raise SessionError(str(exc)) from exc
        except SessionError:
            raise
        except Exception as exc:
            log.exception("recording a certification failed")
            raise SessionError(
                f"the certification was not recorded ({type(exc).__name__}: {exc})"
            ) from exc
        self._trust_stale = True
        state = "certified" if recorded else "was already certified"
        await self._emit_safely(Log(level="info", message=f"honesty: {by}: {what} {state}"))
        await self.announce_honesty()
        return result

    async def honesty_review(self, *, asked: bool = False, wait_s: float | None = None) -> str:
        """Run the honesty review: judge the unreviewed and the marked files (section 4).

        The gate calls this for a step that needs a clean honesty state, and `request_review`
        calls it for a person (`_run_review` has the steps).

        Args:
            asked: True when a person asked for the review.
            wait_s: The longest time to wait for the review's end, in seconds; None waits
                until it ends.

        Returns:
            "" if a review ran and judged every file it had to (the caller then reads the
            state again); otherwise one sentence saying why none ran, why it failed, which
            files it could not judge, or that it is still running. Never raises.
        """
        return (await self._run_review(asked, wait_s)).problem

    async def _run_review(self, asked: bool, wait_s: float | None = None) -> HonestyReviewResult:
        """Run one honesty review, or say why none runs (specification section 4).

            the plan         `HonestyState.review_plan`: the unreviewed and the marked files,
                             each read once; more than `honesty.review_max_files` -> no review
            without a model  a file whose change is whitespace only is verified
            the reviewer     `calu.checkers.honesty_review.HonestyReviewer`: batches of
                             `honesty.review_batch_files`, `honesty.review_concurrency` at a
                             time, `honesty.review_timeout_s` in all; a failure verifies nothing
            the result       `HonestyState.apply_current_review`: an honest file is verified
                             at the content the reviewer was shown and stored with the source
                             "review"; findings replace the file's marks; "review required"
                             is cleared when no mark is open
            no baseline      only when a person asked: every file is judged whole; if HEAD
                             then records only verified contents, HEAD is certified with
                             the source of those verifications (`_certify_reviewed`).
                             A review nobody asked for does not run without a baseline and
                             certifies nothing

        At most one review runs at a time: a caller that finds one running waits for it.

        A review that the gate asks for does not run when the last review looked at
        everything it would read (`calu.honesty.Reviewed.covers`: every file with the same
        content and marks, the same baseline); the last answer is then given again. It does
        not run within `REVIEW_RETRY_S` of a failed review either; the failure is the answer.
        A review a person asks for runs anyway. With `honesty.sufficient_trust` "user" a
        review verifies no file, so only a person's request starts one (its findings and the
        clearing of "review required" still apply).

        One request runs up to `REVIEW_PASSES` reviews, within `wait_s`:

            the review dropped results because files
            changed while it ran                        -> a further review reads those files
            the caller waited for a review that another
            caller started (except: a person waited
            for a review a person asked for)            -> a further review for this caller,
                                                           which the rules above may answer
                                                           without a model call

        While the session runs, the review is a task of the session, not of its caller: a
        caller that stops waiting (`wait_s`, or the gate's own time limit) leaves the review
        running, and its result is applied when it ends. A running review ends with the run.

        Side effects: model calls; changes `self.honesty`; writes verified contents and
        possibly a trusted commit to the database; emits `honesty.update` when the review
        starts and ends (`review_running`) and `log` events with its progress and outcome.

        Args:
            asked: True when a person asked for the review.
            wait_s: The longest time to wait for the review's end, in seconds; None waits
                until it ends.

        Returns:
            `ran` says whether the reviewer's result was applied; `problem` is "" if every
            file was judged. Never raises: an error is the problem of a review that did not
            run, and so is a review that is still running after `wait_s`.
        """
        if self.config.honesty.mode == "off":
            return HonestyReviewResult(ran=False, problem="honesty.mode is off.")
        deadline = None if wait_s is None else anyio.current_time() + wait_s
        result = HonestyReviewResult(ran=False, problem="No honesty review ran.")
        for _ in range(REVIEW_PASSES):
            run = self._review_run
            joined = run is not None
            if run is None:
                run = self._review_run = ReviewRun(fresh=asked)
                running = not self._run_state.run_done.is_set()
                tasks = self._run_state.tg if running else None
                if tasks is None:
                    await self._review_job(run)
                else:
                    tasks.start_soon(self._review_job, run)
            left = None if deadline is None else max(0.0, deadline - anyio.current_time())
            with anyio.move_on_after(left):
                await run.done.wait()
            if run.result is None:
                return HonestyReviewResult(
                    ran=False,
                    problem=(
                        "The honesty review is still running. Its result is applied when it "
                        "ends; the step can be tried again then."
                    ),
                )
            result = run.result
            other = joined and not (asked and run.fresh)
            if run.stopped or not (run.dropped or other):
                break
        return result

    async def _review_job(self, run: ReviewRun) -> None:
        """Run the one honesty review in flight and hand its answer to everyone who waits.

        Side effects: as `_run_review`; sets `run.result`, `run.dropped`, `run.stopped` and
        `run.done`, and frees the session for the next review. Never raises, except for
        cancellation from outside.

        Args:
            run: The record of this review; `run.fresh` says whether a person asked for it.
        """
        try:
            with run.scope:
                run.result = await self._review_once(run)
            if run.result is None:
                run.stopped = True
                run.result = HonestyReviewResult(
                    ran=False, problem="The honesty review was stopped with the session."
                )
        except Exception as exc:
            log.exception("the honesty review failed")
            run.result = HonestyReviewResult(
                ran=False, problem=f"The honesty review failed ({type(exc).__name__}: {exc})."
            )
        finally:
            self._review_run = None
            run.done.set()

    async def _review_once(self, run: ReviewRun) -> HonestyReviewResult:
        """Do the work of the one honesty review in flight (`_review_job`).

        Side effects: as `_run_review`; sets `run.dropped` when results were dropped because
        files changed while the review ran.

        Args:
            run: The record of this review. `run.fresh` (a person asked) runs the review also
                when the last one looked at the same files, within `REVIEW_RETRY_S` of a
                failed one, and without a baseline. A review that was started without a
                person's request stays that: it certifies no commit, also when a person
                waits for it meanwhile.

        Returns:
            As `_run_review`.

        Raises:
            Exception: Whatever git, the worker thread or the store raised; `_review_job`
                turns it into the problem of a review that did not run.
        """
        cfg = self.config.honesty
        fresh = run.fresh
        if cfg.sufficient_trust == "user" and not fresh:
            return HonestyReviewResult(
                ran=False,
                problem=(
                    "No honesty review was started: honesty.sufficient_trust is 'user', so "
                    "only a person's certification verifies a file (`/trust`, `/trust-file`)."
                ),
            )
        failed = self._review_failed
        if not fresh and failed is not None:
            if anyio.current_time() - failed[0] < REVIEW_RETRY_S:
                return HonestyReviewResult(
                    ran=False,
                    problem=(
                        f"{failed[1]} calu starts the next review by itself "
                        f"{REVIEW_RETRY_S:.0f} s after that failure at the earliest; "
                        "`/honesty-review` runs one now."
                    ),
                )
        async with self._honesty_lock:
            await self._refresh_trust()
            plan = await anyio.to_thread.run_sync(
                self._review_plan, self._take_written(), cfg.review_max_files
            )
        base = plan.baseline
        if base is None and not fresh:
            return HonestyReviewResult(
                ran=False,
                problem=(
                    "The honesty review did not run: there is no baseline; a person "
                    "certifies a commit with `/trust` or asks for a review with "
                    "`/honesty-review`."
                ),
            )
        if plan.problem:
            return HonestyReviewResult(
                ran=False, problem=f"The honesty review did not run: {plan.problem}."
            )
        if not fresh and self._last_review is not None:
            reviewed, last = self._last_review
            if reviewed.covers(plan):
                if not last.problem:
                    return last
                return last.model_copy(
                    update={
                        "problem": (
                            f"{last.problem} Nothing changed since that review, so it was "
                            "not repeated; `/honesty-review` runs it again."
                        )
                    }
                )
        await self._emit_safely(
            Log(
                level="info",
                message=(
                    f"honesty: review started: {plan.count} file(s) to judge "
                    f"({len(plan.files)} by the reviewer in "
                    f"{len(batches(plan.files, cfg.review_batch_files))} batch(es), "
                    f"{len(plan.shortcuts)} with a whitespace-only change, "
                    f"{len(plan.skipped)} that the review cannot judge)"
                    + (
                        f"; {plan.verified_already} further file(s) are verified already"
                        if plan.verified_already
                        else ""
                    )
                ),
            )
        )
        self._review_running = True
        try:
            await self.announce_honesty()
            async with self._checking_scope():
                outcome = await self._reviewer().review(
                    plan.files, base.short if base else "", self._review_progress
                )
        finally:
            self._review_running = False
        if outcome.problem:
            self._review_failed = (anyio.current_time(), outcome.problem)
            await self._emit_safely(Log(level="warning", message=f"honesty: {outcome.problem}"))
            await self.announce_honesty()
            return HonestyReviewResult(ran=False, problem=outcome.problem)
        self._review_failed = None
        async with self._honesty_lock:
            changes, store, seen = await anyio.to_thread.run_sync(
                self._apply_review, {**plan.shortcuts, **outcome.results}
            )
            for sha256 in changes.withdrawn:
                await self.recorder.attempt(
                    "withdrawing the stored trust of a file content",
                    self.store.distrust_file(sha256, REVIEW_CERTIFIER),
                )
            for sha256, path in store:
                await self.recorder.attempt(
                    "storing a verified file content",
                    self.store.trust_file(sha256, path, "review", REVIEW_CERTIFIER),
                )
            if base is None and fresh:
                await self._certify_reviewed()
        await self._report_marks(changes)
        left = [*(f"{path} ({why})" for path, why in plan.skipped), *changes.ignored]
        parts: list[str] = []
        if left:
            parts.append(
                f"The honesty review could not judge {len(left)} file(s): {_some(left)}. "
                "A person certifies such a file with `/trust-file PATH`."
            )
        if changes.dropped:
            run.dropped = True
            names = _some([f"{path} ({why})" for path, why in changes.dropped])
            parts.append(
                f"{len(changes.dropped)} file(s) changed while the honesty review ran: "
                f"{names}. The next review reads them; a step that needs it starts one."
            )
        result = HonestyReviewResult(ran=True, problem=" ".join(parts))
        self._last_review = (
            Reviewed(
                baseline=base.commit_id if base else "",
                review_required=changes.review_required,
                seen={**{path: plan.seen[path] for path, _why in plan.skipped}, **seen},
            ),
            result,
        )
        await self._emit_safely(
            Log(
                level="info",
                message=(
                    f"honesty: review finished: {len(store)} file(s) verified, "
                    f"{len(changes.opened)} finding(s), {len(left)} file(s) not judged, "
                    f"{len(changes.dropped)} changed meanwhile "
                    f"({outcome.input_tokens} input and {outcome.output_tokens} output tokens)"
                ),
            )
        )
        await self.announce_honesty()
        return result

    def _reviewer(self) -> HonestyReviewer:
        """Build the reviewer agent of one honesty review from the configuration as it is now.

        Returns:
            The reviewer with the model `honesty.reviewer_model` (blank: the default model,
            `calu.config.default_model`), the fixed reviewer instructions with the honesty
            rules, and the budget settings `honesty.review*`.
        """
        cfg = self.config.honesty
        model = (
            self._models.get("honesty-review")
            or cfg.reviewer_model.strip()
            or default_model(self.config.claude.use_bedrock)
        )
        return HonestyReviewer(
            instructions=review_instructions(cfg.rules),
            model=resolve_model(model),
            workspace=self.workspace,
            rules=self.rules,
            usage_limits=UsageLimits(
                request_limit=cfg.reviewer_request_limit,
                output_tokens_limit=cfg.reviewer_output_tokens_limit,
            ),
            batch_files=cfg.review_batch_files,
            concurrency=cfg.review_concurrency,
            timeout_s=cfg.review_timeout_s,
        )

    async def _review_progress(self, done: int, total: int) -> None:
        """Tell the clients that one batch of the running honesty review answered.

        Args:
            done: How many batches have answered.
            total: How many batches the review has.
        """
        await self._emit_safely(
            Log(level="info", message=f"honesty: review: batch {done} of {total} answered")
        )

    def _review_plan(self, written: Collection[str], max_files: int) -> ReviewPlan:
        """Compute the plan of an honesty review (reads git and files; for a worker thread).

        Args:
            written: Files that commands wrote since the last look (`_take_written`); they
                are watched from now on.
            max_files: `honesty.review_max_files`.

        Returns:
            `HonestyState.review_plan`, after the marks whose code is gone were closed.
        """
        self.honesty.watch_all(written)
        self.honesty.close_stale()
        return self.honesty.review_plan(max_files)

    def _apply_review(
        self, results: Mapping[str, FileReview]
    ) -> tuple[MarkChanges, list[tuple[str, str]], dict[str, FileSeen]]:
        """Apply the result of an honesty review to the state (for a worker thread).

        Args:
            results: Path -> what the review said, for the files it judged.

        Returns:
            (what `HonestyState.apply_current_review` changed; the (SHA-256, path) of each
            verification to store in the database with the source "review"; for each file
            whose result was applied, the content the review judged and the file's open marks
            now (`HonestyState.seen_now`)).
        """
        state = self.honesty
        changes = state.apply_current_review(results)
        store: list[tuple[str, str]] = []
        for path in results:
            held = state.storable(path)
            if held is not None and held.source == "review":
                store.append((held.sha256, state.key(path)))
        state.close_stale()
        changes.review_required = state.review_required
        dropped = {path for path, _why in changes.dropped}
        return changes, store, state.seen_now(set(results) - dropped)

    async def _certify_reviewed(self) -> None:
        """Certify HEAD after an honesty review that a person asked for without a baseline.

        HEAD is recorded as trusted if every file it records has a verified content
        (`HonestyState.certification` with `rooted=False`): calu compares the commit's
        entries with the verified contents; the reviewer's answer alone certifies nothing.
        The source is the weakest among those verifications, and never "user". The caller
        holds `_honesty_lock`.

        Side effects: writes through `SessionStore.certify_commit`, marks the stored trust as
        stale so that the baseline is looked up again, and logs the outcome. A failure is
        logged; HEAD is then not certified.
        """
        try:
            head = await self._head()
            if head is None:
                return
            verdict = await anyio.to_thread.run_sync(
                functools.partial(self.honesty.certification, head, rooted=False)
            )
            if verdict.problem or verdict.source is None:
                await self._emit_safely(
                    Log(
                        level="info",
                        message=(
                            f"honesty: commit {head[:12]} is not certified after the review: "
                            f"{verdict.problem}"
                        ),
                    )
                )
                return
            source = "review" if verdict.source == "user" else verdict.source
            commit = await anyio.to_thread.run_sync(resolve_commit, self.workspace, head)
            await self.store.certify_commit(
                commit.commit_id, commit.tree_id, source, REVIEW_CERTIFIER
            )
            self._trust_stale = True
            await self._emit_safely(
                Log(
                    level="info",
                    message=(
                        f"honesty: commit {commit.short} certified as trusted after the "
                        f"review of every file it records (source {source})"
                    ),
                )
            )
        except Exception as exc:
            log.exception("certifying HEAD after an honesty review failed")
            await self._emit_safely(
                Log(
                    level="warning",
                    message=(
                        "honesty: HEAD was not certified after the review "
                        f"({type(exc).__name__}: {exc})"
                    ),
                )
            )

    async def request_review(self, client: str = "unknown") -> HonestyReviewResult:
        """Run the honesty review because a person asked for it (`honesty.review`).

        Side effects: as `_run_review`; logs who asked and sends `honesty.update`.

        Args:
            client: Who asked, for the log.

        Returns:
            Whether a review ran, and what it could not do.

        Raises:
            SessionError: If `honesty.mode` is "off".
        """
        if self.config.honesty.mode == "off":
            raise SessionError("honesty.mode is off; there is no honesty state to review")
        done = await self._run_review(asked=True)
        outcome = "ran" if done.ran else "did not run"
        await self._emit_safely(
            Log(
                level="info",
                message=(
                    f"honesty: the review {client} asked for {outcome}"
                    + (f": {done.problem}" if done.problem else "")
                ),
            )
        )
        await self.announce_honesty()
        return done

    def trust_changed(self) -> None:
        """Note that the stored trust changed, so the next snapshot reads it again.

        Called for a `trust_changed` notification of the database and whenever the listening
        connection is in force again. Reads nothing itself and never raises.
        """
        self._trust_stale = True

    async def announce_honesty(self) -> None:
        """Compute the honesty state and tell the clients, unless they already have it.

        Called at the start, after a settings reload, after a trust command and after a mark
        was cleared. With `honesty.mode` "off" only the mode is sent, and only if the clients
        were told another one. Never raises: a state that cannot be computed is logged.
        """
        if self.config.honesty.mode == "off":
            await self._emit_honesty(None)
            return
        try:
            await self.honesty_snapshot()
        except Exception:
            log.exception("computing the honesty state failed")

    async def honesty_snapshot(self) -> HonestySnapshot:
        """Compute the honesty state as it is now, and tell the clients if it changed.

        Before the state is read, the settings `honesty.max_open_marks` and
        `honesty.sufficient_trust` are applied, the baseline and the stored file trust are read
        again if HEAD moved or the stored trust changed (`_refresh_trust`), and the marks whose
        code is gone are closed. git and the files are read in a worker thread.

        Side effects: may query the database; may close marks; emits `honesty.update` if the
        state differs from the one last sent.

        Returns:
            The state.

        Raises:
            Exception: If git or the worker thread fails in a way `HonestyState` does not turn
                into "unreviewed". A database failure does not raise: the session then has no
                baseline.
        """
        async with self._honesty_lock:
            await self._refresh_trust()
            snapshot = await anyio.to_thread.run_sync(self._settled_snapshot, self._take_written())
        await self._emit_honesty(snapshot)
        return snapshot

    def _settled_snapshot(self, written: Collection[str]) -> HonestySnapshot:
        """Close the marks whose code is gone and compute the state (reads git and files).

        For a worker thread: the state's methods wait for one another and read many files.

        Args:
            written: Files that commands wrote since the last look (`_take_written`); they
                are watched from now on.

        Returns:
            The state after that.
        """
        self.honesty.watch_all(written)
        self.honesty.close_stale()
        return self.honesty.snapshot()

    def _take_written(self) -> set[str]:
        """Hand out the files that measured write sets named since the last call.

        Called on the event loop, where `_watch_written` adds to the set.

        Returns:
            The paths, relative to the workspace; the session's set is empty afterwards.
        """
        written, self._written = self._written, set()
        return written

    async def _gate_honesty(self) -> HonestySnapshot | None:
        """Give the gate the honesty state for a step (`StepGate`'s `honesty`).

        Returns:
            None while `honesty.mode` is "off". Otherwise the state; in the mode "track" also
            None if the state could not be computed, because that mode stops nothing.

        Raises:
            Exception: In the mode "enforce", whatever `honesty_snapshot` raised; the gate
                then escalates the step.
        """
        mode = self.config.honesty.mode
        if mode == "off":
            return None
        try:
            return await self.honesty_snapshot()
        except Exception:
            if mode == "enforce":
                raise
            log.exception("computing the honesty state failed")
            return None

    async def _head(self) -> str | None:
        """Read the commit id of HEAD now, without the cache of `_git_head`.

        Returns:
            The full commit id; None if the workspace is in no repository, has no commit yet,
            or git could not be asked.
        """
        try:
            answer = await anyio.to_thread.run_sync(
                run_git, self.workspace, ["rev-parse", "--verify", "--quiet", "HEAD"]
            )
        except GitError:
            return None
        return answer.decode(errors="replace").strip() or None

    async def _refresh_trust(self) -> None:
        """Bring the honesty state's settings, baseline and stored file trust up to date.

        The baseline and the stored trust are read from the database when HEAD is another
        commit than at the last look or `_trust_stale` is set. If the database cannot be
        asked, the state has no baseline and no stored trust until a later look succeeds, and
        a warning is logged. The caller holds `_honesty_lock`.
        """
        state, cfg = self.honesty, self.config.honesty
        state.max_open_marks = cfg.max_open_marks
        if state.sufficient_trust != cfg.sufficient_trust:
            state.sufficient_trust = cfg.sufficient_trust
            self._trust_stale = True
        head = await self._head()
        if not self._trust_stale and head == self._honesty_head:
            return
        self._trust_stale = False  # a change announced while this reads sets it again
        try:
            baseline = await find_baseline(
                self.workspace, self.store.trusted_among, minimum=cfg.sufficient_trust
            )
            hashes = await self.store.trusted_file_sources()
        except Exception as exc:
            log.exception("reading the stored trust failed")
            state.baseline, state.trusted_hashes = None, {}
            self._trust_stale = True
            await self._emit_safely(
                Log(
                    level="warning",
                    message=(
                        "honesty: the trusted commits could not be read "
                        f"({type(exc).__name__}: {exc}); the session has no baseline until "
                        "they can"
                    ),
                )
            )
            return
        state.baseline, state.trusted_hashes = baseline, hashes
        self._honesty_head = head

    async def _emit_honesty(self, snapshot: HonestySnapshot | None) -> None:
        """Tell the clients the honesty state, unless they already have exactly this.

        Side effects: emits `honesty.update` and remembers what it sent.

        Args:
            snapshot: The state as computed; None sends only the mode (for the mode "off").
        """
        info = HonestyInfo(mode=self.config.honesty.mode, review_running=self._review_running)
        if snapshot is not None:
            base = snapshot.baseline
            info = info.model_copy(
                update={
                    "marks": [_mark_info(mark) for mark in snapshot.marks],
                    "baseline": base.commit_id if base else None,
                    "baseline_source": base.source if base else None,
                    "unreviewed_all": snapshot.unreviewed.everything,
                    "unreviewed_count": len(snapshot.unreviewed.paths),
                    "unreviewed": list(snapshot.unreviewed.paths[:NOTE_LIMIT]),
                    "review_required": snapshot.review_required,
                    "clean": snapshot.clean,
                }
            )
        if info == self._honesty_sent:
            return
        self._honesty_sent = info
        await self._emit_safely(HonestyUpdate(**info.model_dump()))

    def _keep_honesty(self, step: Step, decided: Decided, expected: str | None) -> None:
        """Remember what a step changes in the honesty state if its tool call succeeds.

        Nothing is applied here: the checkers judged a call that has not run. The record is
        applied by `_apply_honesty` after the tool result, and discarded for a call that was
        denied or failed. A denied step, a step without an id and the mode "off" keep nothing.
        For a step on the command route only the clean marks are kept: its dirty marks are
        recorded when the checkers answer (`_mark_at_once`).

        Args:
            step: The tool call.
            decided: The gate's decision with the checkers' answers.
            expected: For an edit, the SHA-256 `HonestyState.expect_edit` computed before the
                checkers judged it; None otherwise.
        """
        if self.config.honesty.mode == "off" or not step.tool_use_id:
            return
        if decided.decision.decision is Decision.DENY:
            return
        at_once = decided.route is Route.COMMAND  # `_mark_at_once` recorded the dirty marks
        marks = [
            (
                checker.name,
                [] if at_once else list(run.decision.marks_dirty),
                list(run.decision.marks_clean),
            )
            for checker, run in decided.runs
            if (run.decision.marks_dirty and not at_once) or run.decision.marks_clean
        ]
        edit_path: str | None = None
        if step.tool_name in EDIT_TOOLS:
            raw = step.tool_input.get("file_path") or step.tool_input.get("notebook_path")
            edit_path = str(Path(step.cwd or self.workspace) / raw) if raw else None
        checked = (
            decided.route is Route.EDIT
            and bool(decided.runs)
            and all(run.decision.decision.approves for _checker, run in decided.runs)
        )
        if not marks and edit_path is None and not decided.clean_commit:
            return
        self._run_state.call(step.tool_use_id).honesty = PendingHonesty(
            marks=marks,
            edit_path=edit_path,
            expected_sha256=expected,
            checked=checked,
            clean_commit=decided.clean_commit,
        )

    async def tool_succeeded(self, step: Step) -> None:
        """Apply what a finished tool call changes in the honesty state (ToolGate).

        The runner calls this after a tool call succeeded, while the agent waits, so the next
        step is judged with the state after this one. Calling it again for the same call, or
        for a call that keeps nothing, does nothing.

        Args:
            step: The tool call that has just run.
        """
        await self._apply_honesty(step.tool_use_id or "", ok=True)

    async def _mark_at_once(self, step: Step, decided: Decided) -> None:
        """Record the dirty marks of a command's or a commit's checkers when they answer.

        The checkers of the command route, and the changeset checkers of a commit, mark code
        that exists in the project whatever the command does. So their `marks_dirty` are
        recorded for every decision, also for a step that is denied, escalated or fails. The
        marks of an edit are different: they name what the edit would write, and are applied
        only after it ran (`_keep_honesty`, `_apply_honesty`).

        Side effects: as `_apply_record`. Never raises.

        Args:
            step: The tool call.
            decided: The gate's decision with the checkers' answers.
        """
        if self.config.honesty.mode == "off" or decided.route is not Route.COMMAND:
            return
        marks = [
            (checker.name, list(run.decision.marks_dirty), list[CodeMark]())
            for checker, run in decided.runs
            if run.decision.marks_dirty
        ]
        if marks:
            await self._apply_record(PendingHonesty(marks=marks))

    async def _apply_honesty(self, tool_use_id: str, ok: bool) -> None:
        """Apply or discard what a tool call changes in the honesty state (section 2).

        For a call that succeeded, the record kept at the gate is applied (`_apply_record`).
        For a call that failed or was denied, the record is dropped and nothing changes.

        Side effects: as `_apply_record`. Never raises.

        Args:
            tool_use_id: Claude's id of the call.
            ok: Whether the tool result arrived without error.
        """
        call = self._run_state.peek(tool_use_id)
        pending, call.honesty = call.honesty, None
        self._run_state.release(tool_use_id)
        if pending is None or not ok or self.config.honesty.mode == "off":
            return
        await self._apply_record(pending)

    async def _apply_record(self, pending: PendingHonesty) -> None:
        """Change the honesty state by one record of marks, an edit or a commit.

        In this order: the edited file is watched; the checkers' marks are recorded; the
        stored trust of every content a mark was opened on is withdrawn
        (`SessionStore.distrust_file`); an edit that the edit checkers approved verifies the
        file at the content they judged, and that verification is stored in the database with
        the source "check" unless the file carries an open mark; the marks whose code is gone
        are closed; and a plain commit made in a clean state is certified (`_certify`).

        Side effects: changes `self.honesty`, may write to the database, emits a `log` event
        that names what changed and `honesty.update`. Never raises: if applying fails, a
        review is required from then on, so that the failure cannot leave the state cleaner
        than it is.

        Args:
            pending: What to apply.
        """
        try:
            async with self._honesty_lock:
                changes, store = await anyio.to_thread.run_sync(
                    self._record_result, pending, self._take_written()
                )
                for sha256 in changes.withdrawn:
                    await self.recorder.attempt(
                        "withdrawing the stored trust of a file content",
                        self.store.distrust_file(sha256, CERTIFIER),
                    )
                for sha256, path in store:
                    await self.recorder.attempt(
                        "storing a verified file content",
                        self.store.trust_file(sha256, path, "check", CERTIFIER),
                    )
                if pending.clean_commit:
                    try:
                        await self._certify(pending)
                    except Exception as exc:
                        # The commit stays uncertified; nothing else depends on it.
                        log.exception("certifying a commit failed")
                        await self._emit_safely(
                            Log(
                                level="warning",
                                message=(
                                    "honesty: the commit was not certified "
                                    f"({type(exc).__name__}: {exc})"
                                ),
                            )
                        )
            await self._report_marks(changes)
        except Exception as exc:
            log.exception("applying a step's result to the honesty state failed")
            self.honesty.review_required = True
            await self._emit_safely(
                Log(
                    level="warning",
                    message=(
                        "honesty: a step's result could not be applied "
                        f"({type(exc).__name__}: {exc}); an honesty review is required"
                    ),
                )
            )
        await self.announce_honesty()

    def _record_result(
        self, pending: PendingHonesty, written: Collection[str] = ()
    ) -> tuple[MarkChanges, list[tuple[str, str]]]:
        """Change the honesty state for one record (reads files; for a worker thread).

        Args:
            pending: What the call changes.
            written: Files that commands wrote since the last look (`_take_written`); they
                are watched from now on.

        Returns:
            (the marks opened, closed and ignored and the contents whose stored trust is to
            be withdrawn, over all checkers of the step; the (SHA-256, path) of each
            verification to store in the database).
        """
        state = self.honesty
        state.watch_all(written)
        total = MarkChanges()
        edited: set[str] = set()
        if pending.edit_path is not None:
            edited.add(state.watch(pending.edit_path))
        for name, dirty, clean in pending.marks:
            changes = state.record(dirty, clean, name, edited)
            total.opened += changes.opened
            total.closed += changes.closed
            total.ignored += [f"{name}: {message}" for message in changes.ignored]
            total.withdrawn += [one for one in changes.withdrawn if one not in total.withdrawn]
        store: list[tuple[str, str]] = []
        if pending.edit_path is not None:
            state.record_edit(
                pending.edit_path, pending.expected_sha256 if pending.checked else None
            )
            held = state.storable(pending.edit_path)
            if held is not None:
                store.append((held.sha256, state.key(pending.edit_path)))
        state.close_stale()
        total.review_required = state.review_required
        return total, store

    async def _certify(self, pending: PendingHonesty) -> None:
        """Record the new HEAD as trusted after a commit made in a clean state (section 5).

        The commit is certified only if HEAD moved since the gate decided and
        `HonestyState.certification` finds every entry of the new commit verified. The source
        is the weakest among the verifications and the baseline's own, and never "user": that
        source is recorded only when a person asked. The caller holds `_honesty_lock`.

        Side effects: writes through `SessionStore.certify_commit`, marks the stored trust as
        stale so that the baseline is looked up again, and logs the outcome.

        Args:
            pending: The record of the commit's call, with HEAD as it was before.

        Raises:
            Exception: Whatever git or the store raised; the commit is then not certified.
        """
        head = await self._head()
        if head is None or head == pending.head_before:
            return
        verdict = await anyio.to_thread.run_sync(self.honesty.certification, head)
        if verdict.problem or verdict.source is None:
            await self._emit_safely(
                Log(
                    level="info",
                    message=f"honesty: commit {head[:12]} is not certified: {verdict.problem}",
                )
            )
            return
        source = "review" if verdict.source == "user" else verdict.source
        commit = await anyio.to_thread.run_sync(resolve_commit, self.workspace, head)
        await self.store.certify_commit(commit.commit_id, commit.tree_id, source, CERTIFIER)
        self._trust_stale = True
        await self._emit_safely(
            Log(
                level="info",
                message=f"honesty: commit {commit.short} certified as trusted (source {source})",
            )
        )

    async def _report_marks(self, changes: MarkChanges) -> None:
        """Log what a step's result changed in the marks.

        Args:
            changes: The marks opened, closed and ignored.
        """
        if changes.ignored:
            await self._emit_safely(
                Log(level="warning", message="honesty: ignored " + "; ".join(changes.ignored))
            )
        parts: list[str] = []
        if changes.opened:
            parts.append(f"{len(changes.opened)} spot(s) recorded as dishonest")
        if changes.closed:
            parts.append(f"{len(changes.closed)} cleared")
        if parts:
            await self._emit_safely(Log(level="info", message="honesty: " + ", ".join(parts)))

    def _watch_written(self, paths: Sequence[str]) -> None:
        """Watch the files a measured write set names (`WriteSets`' `wrote`).

        A file a command wrote is tracked from then on even if git does not list it
        (specification section 12). The paths are only collected here, on the event loop; the
        next computation of the state hands them to it in its worker thread
        (`_take_written`). Does nothing while `honesty.mode` is "off".

        Args:
            paths: The written paths, relative to the workspace.
        """
        if self.config.honesty.mode == "off":
            return
        self._written.update(paths)

    async def _review_checkers(
        self,
        route: Route,
        step: Step,
        context: CheckContext,
        extra: str = "",
        *,
        extra_cut: bool = False,
    ) -> tuple[CheckDecision, list[tuple[Checker, CheckOutcome]]]:
        """Run the checkers that review a command's write set, with the honesty state.

        Like `StepGate.run_checkers`, with two additions while `honesty.mode` is not "off":
        the checkers get the note of the honesty state, and the marks of their answers are
        recorded at once, because the command has already run (`_apply_record`). A change the
        checkers deny is undone, so the marks of a DENY, which name that change, are not
        recorded.

        Args:
            route: The route whose checkers run (the changeset route).
            step: The command that wrote the change.
            context: The cached context for the checkers.
            extra: The prompt block with the write set.
            extra_cut: True if `extra` does not show all of the change.

        Returns:
            What `StepGate.run_checkers` returned.

        Raises:
            Exception: In the mode "enforce", whatever `honesty_snapshot` raised; the review
                of the write set then escalates.
        """
        state = await self._gate_honesty()
        if state is not None:
            step = step.model_copy(update={"notes": [*step.notes, state.note()]})
        decision, runs = await self.gate.run_checkers(
            route, step, context, extra, extra_cut=extra_cut
        )
        if state is not None and decision.decision is not Decision.DENY:
            pending = PendingHonesty(
                marks=[
                    (checker.name, list(run.decision.marks_dirty), list(run.decision.marks_clean))
                    for checker, run in runs
                    if run.decision.marks_dirty or run.decision.marks_clean
                ]
            )
            if pending.marks:
                await self._apply_record(pending)
        return decision, runs

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
        and may open an escalation. Afterwards the marks the checkers left for this command
        are recorded and a commit made in a clean honesty state is certified
        (`_apply_honesty`).

        Args:
            step: The tool call that has just run.

        Returns:
            A note for Claude, or "" if there is nothing to say.
        """
        note = await self.write_sets.review(step)
        # The command has run and its change stands or was undone: the honesty state follows
        # before the agent goes on.
        await self._apply_honesty(step.tool_use_id or "", ok=True)
        return note

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
            honesty_marks=list(self._honesty_sent.marks),
            honesty=self._honesty_sent.model_copy(update={"mode": self.config.honesty.mode}),
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
        session_id: str | None = None,
    ) -> None:
        """Start a Claude session in `tg` and return once it accepts messages.

        Side effects: creates a `sessions` row (state "starting"), restores the turns and the
        chat summary of the resumed session from the database, emits `startup_warnings` as
        `log` warnings, creates a runner and starts it as a task in `tg`. Once the runner is
        ready, the row gets the Claude session id (with `session_id`: before the first turn),
        and a fork stores the turns it keeps under its own row (`_restore_context`). A start
        that fails writes neither.

        Args:
            tg: Task group that runs the session; it must outlive the session.
            resume_id: Claude session id to resume, or None for a new session.
            fork: Where to branch off the resumed conversation (PLAN.md D36); None resumes it
                whole. Needs `resume_id`; the forked conversation gets its own session id, so
                the one that was forked from is left untouched.
            session_id: The Claude session id a new or forked conversation gets, a UUID
                (D203); None lets Claude Code choose, and the id is then known with the first
                turn. Ignored for a plain resume, which keeps `resume_id`.
            model: Claude model alias or id for this and later sessions; None keeps the
                configured model.
            effort: Reasoning effort for this and later sessions; "default" uses Claude Code's
                default effort; None keeps the configured effort.

        Raises:
            SessionError: If a session is already running, if the runner ended before it was
                ready (the message names the runner's error), or if it was not ready within
                `START_TIMEOUT_S` seconds (the runner is stopped then).
        """
        if self.state not in ("stopped", "failed") or not self._run_state.run_done.is_set():
            # Nothing of the running session is touched: a rejected start once cleared its
            # thinking and text that were not narrated yet.
            raise SessionError(f"a session is already {self.state}")
        overrides: dict[str, str | None] = {}
        if model is not None:
            overrides["model"] = model
        if effort is not None:
            overrides["effort"] = None if effort == "default" else effort
        if overrides:
            self.config.claude = self.config.claude.model_copy(update=overrides)
        # A run begins: everything that describes one run starts from its default, except
        # what `RunState.following` carries over. That includes Claude's output that was not
        # narrated: text of an earlier session must not reach this one.
        self._run_state = self._run_state.following(tg)
        await self.write_sets.prepare()
        self.state = "stopped"
        await self.recorder.attempt("creating the session row", self.store.start(self._identity()))
        await self._set_state("starting")
        inherited = await self._restore_context(resume_id, fork)
        chosen = session_id if resume_id is None or fork is not None else None
        # The baseline of this run: the nearest trusted ancestor of HEAD as it is now.
        self._trust_stale = True
        await self.announce_honesty()
        for warning in self.startup_warnings:
            await self.emit(Log(level="warning", message=warning))
        runner = self.runner = self.runner_factory()
        tg.start_soon(self._run, runner, resume_id, fork, chosen)

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
        # timed out, for example) must still be resumable by its Claude session id (D27). An
        # id calu chose is known now; Claude Code reports it with the first turn only.
        await self._record_claude_session(runner.session_id or chosen)
        if fork is not None and resume_id is not None:
            # So that the fork's page can say where its first turns come from, and the
            # conversation it left can be found from it.
            await self.recorder.attempt(
                "recording what the session is a fork of",
                self.store.set_fork_origin(resume_id, fork.before_turn),
            )
        # Only now, with Claude Code running: a fork whose start failed must not leave a
        # conversation behind that is listed and cannot be resumed.
        for turn in inherited:
            await self.recorder.attempt(
                "storing an inherited turn with the fork", self.store.save_turn(turn)
            )
        await self._set_state("idle")

    async def _run(
        self,
        runner: ClaudeRunner,
        resume_id: str | None = None,
        fork: Fork | None = None,
        session_id: str | None = None,
    ) -> None:
        """Run `runner` until it ends; then mark the session as stopped or failed.

        Never raises: runner errors are logged and reported as a `log` event.

        Args:
            runner: The runner of this session.
            resume_id: Claude session id to resume, or None.
            fork: Where to branch off the resumed conversation, or None (PLAN.md D36).
            session_id: The id a new or forked conversation gets, or None (D203).
        """
        try:
            await runner.run(self, self._on_runner_event, resume_id, fork, session_id)
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
            if self._review_run is not None:
                # An honesty review in flight ends with the run; it has verified nothing.
                self._review_run.scope.cancel()
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
        if params.images and params.to != "main":
            raise SessionError(
                "an image can be sent to the main agent only: a message to a subagent is "
                "relayed as text"
            )
        if params.to != "main":
            agent = self._find_agent(params.to)
            text = RELAY_TEMPLATE.format(
                name=agent.description or agent.agent_id,
                to=agent.sdk_agent_id or agent.agent_id,
                text=params.text,
            )
            priority = "next"
        await runner.send(text, priority, params.images)
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
        expected: str | None = None
        try:
            step_id = await self.recorder.add_step(step)
            expected = await self._expect_edit(step)
            decided = await self.gate.decide(step, pending)
        except Exception as exc:
            log.exception("pre_tool_use failed")
            decided = self.gate.failed(step, exc)
        await self._mark_at_once(step, decided)
        try:
            self._keep_honesty(step, decided, expected)
            if decided.clean_commit and step.tool_use_id:
                kept = self._run_state.peek(step.tool_use_id).honesty
                if kept is not None:
                    kept.head_before = await self._head()
        except Exception:
            # Without the record nothing is verified and no commit is certified.
            log.exception("keeping a step's honesty changes failed")
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

        Side effects: adds the step to the checkers' context, records the checker runs and the
        decision in the database, emits `check.result`, counts an
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

        Side effects: as `ask`. A human's decision about the escalation of a backward clock
        jump clears the clock alarm; every other ending of that escalation leaves it.

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
        if (
            source == "clock"
            and asked.outcome in ("allowed", "denied")
            and self._run_state.clock_alarm == reason
        ):
            # A human has seen the jump and decided: that answers the alarm. A jump that was
            # detected since then has another reason and stays.
            self._run_state.clock_alarm = None
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
        try:
            return await self._review_subagent_result(result)
        finally:
            # The review is recorded as a step, but it is no tool call: no result event ends
            # it. Without this, a result that a human allowed left the session in the state
            # "tool_call" until the next start.
            self._run_state.forget(f"result-{result.agent_id}")
            with anyio.CancelScope(shield=True):
                await self._refresh_state()

    async def _review_subagent_result(self, result: SubagentResult) -> str:
        """Implement `review_subagent_result`; the caller removes the step's per-call record.

        Side effects: as `review_subagent_result`.

        Args:
            result: The subagent's task and final result.

        Returns:
            The note for the main agent, as `review_subagent_result` returns it.
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

    def _clock_alarm(self) -> str | None:
        """Tell the gate the pending clock alarm (`clock_jumped`, D32).

        Returns:
            The reason of a backward clock jump that no human has answered yet, or None if
            there is none. `_ask` forgets it when a human has decided its escalation.
        """
        return self._run_state.clock_alarm

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
                    # The checkers judge by the user's words. An image is part of what the
                    # user said and they do not see it, so they are told that there was one.
                    seen = event.text
                    if event.images:
                        seen += (
                            f"\n[{len(event.images)} image(s) attached to this message; "
                            "calu does not show images to the checkers]"
                        )
                    self.context.user_input(seen, event.uuid)
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
                    # A runner that has no hook after the call (and every failed or denied
                    # call) reaches the honesty state here; `_apply_honesty` never raises.
                    await self._apply_honesty(event.tool_use_id, ok=not event.is_error)
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
        """Record Claude's session id, send the honesty state if it changed, close the turn.

        Args:
            event: The turn result.
        """
        await self._record_claude_session(event.session_id)
        # The clients see the honesty state as it is when the turn ends (a command may have
        # written files since the last step was judged).
        await self.announce_honesty()
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

    async def _restore_context(
        self, resume_id: str | None, fork: Fork | None = None
    ) -> list[TurnRecord]:
        """Load the turns and the chat summary of the session that is resumed.

        A new session (or a database error) starts with no turns and the empty summary.

        A fork keeps only the turns before its fork point, and its first turn takes the number
        of the first discarded one. The fork runs under a session id of its own, and a
        conversation is read back by that id (`SessionStore.load_previous`): the caller
        therefore stores the returned turns under the fork's own `sessions` row once the
        runner is ready; without that copy, resuming the fork later would find only the turns
        the fork ran itself. The chat summary is copied by `load_previous`.

        A summary that already covers a turn the fork discards cannot be unfolded. That is
        reported as a `log` warning, and the fork's copy of the summary is recorded as
        covering the turns before the fork point only, so that the turns the fork runs are
        read back when it is resumed.

        A conversation the store knows nothing about was started without calu: it gets one
        turn that reports what Claude Code's transcript says the user wrote (`_unwitnessed`).

        Args:
            resume_id: Claude session id to resume, or None for a new session.
            fork: Where the resumed conversation is branched off, or None.

        Returns:
            The turns the caller has to store under this session's row: what a fork
            inherits, and the turn that describes a conversation started without calu.
            Empty for a new session and for a plain resume of a conversation the store knows.
        """
        turns: list[TurnRecord] = []
        summary = ""
        unknown_to_store = False
        if resume_id is not None:
            try:
                turns, summary = await self.store.load_previous(resume_id)
            except Exception:
                log.exception("restoring the context of %s failed", resume_id)
            else:
                if not turns and not summary:
                    unwitnessed = await self._unwitnessed(resume_id)
                    if unwitnessed is not None:
                        # Stored with this session also on a plain resume: the store holds
                        # nothing of this conversation yet.
                        turns, unknown_to_store = [unwitnessed], True
        before = fork.before_turn if fork is not None else None
        if before is not None:
            # `turns` are the turns the summary does not cover. If the lowest of them is
            # above the fork point (or there is none), the summary reaches past it.
            if summary and min((turn.index for turn in turns), default=before + 1) > before:
                await self._emit_safely(
                    Log(
                        level="warning",
                        message=(
                            "the chat summary the checkers read also covers turns this "
                            f"fork discards (turn {before} and later). Correct it with "
                            "`calu summary <session id> --edit` (`calu sessions` "
                            "shows the id)."
                        ),
                    )
                )
                await self.recorder.attempt(
                    "recording what the fork's summary covers",
                    self.store.save_summary(summary, before - 1),
                )
            turns = [turn for turn in turns if turn.index < before]
        self.context.restore(turns, next_index=before)
        self.context.summary = summary or EMPTY_CHAT_SUMMARY
        return turns if fork is not None or unknown_to_store else []

    async def _unwitnessed(self, resume_id: str) -> TurnRecord | None:
        """Describe a conversation the store does not know from Claude Code's transcript.

        Side effect: reports as a `log` event that the conversation ran without calu.

        Args:
            resume_id: The Claude session id that is resumed or forked.

        Returns:
            One turn that tells the checkers what the transcript says the user asked for
            (`calu.claude.transcripts.unwitnessed_turn`); None without a transcript reader,
            without a readable transcript or without a typed message in it.
        """
        if self.transcript_inputs is None:
            return None
        try:
            inputs = await anyio.to_thread.run_sync(self.transcript_inputs, resume_id)
        except Exception:
            log.exception("reading Claude Code's transcript of %s failed", resume_id)
            return None
        turn = unwitnessed_turn(inputs)
        if turn is not None:
            await self._emit_safely(
                Log(
                    level="warning",
                    message=(
                        f"the conversation {resume_id} was started in Claude Code without "
                        "calu: none of its steps was checked. The checkers are told what its "
                        f"transcript says the user wrote ({len(inputs)} message(s))."
                    ),
                )
            )
        return turn

    # ------------------------------------------------------------------ store helpers

    def _identity(self) -> SessionIdentity:
        """Describe where this session runs, for the `sessions` row.

        Returns:
            Host name, operating system, OS user, directory, process id, git commit, project,
            Claude model and the identity of this machine's process table.
        """
        return SessionIdentity(
            hostname=socket.gethostname(),
            os_user=_os_user(),
            directory=str(self.workspace),
            pid=os.getpid(),
            os=f"{platform.system()} {platform.release()}".strip(),
            git_commit=self._git_head_value,
            project=self.project,
            model=self.config.claude.model,
            machine=machine.here().text(),
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

    async def _expect_edit(self, step: Step) -> str | None:
        """Compute, before an edit is judged, the hash of the content it will produce.

        Args:
            step: The tool call.

        Returns:
            What `HonestyState.expect_edit` returns for an Edit, Write or MultiEdit call while
            `honesty.mode` is not "off"; None for every other call, and if the computation
            failed (the file is then unreviewed after the edit).
        """
        raw = step.tool_input.get("file_path")
        if self.config.honesty.mode == "off" or step.tool_name not in EDIT_TOOLS or not raw:
            return None
        path = Path(step.cwd or self.workspace) / str(raw)
        try:
            return await anyio.to_thread.run_sync(
                self.honesty.expect_edit, path, step.tool_name, step.tool_input
            )
        except Exception:
            log.exception("predicting the content of an edit failed")
            return None

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
                    **asuser.child(),
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


def _some(items: Sequence[str]) -> str:
    """Join the first five of a list of names for a sentence a person reads.

    Args:
        items: The names, each cut to `NOTE_PATH_CHARS` characters.

    Returns:
        E.g. "a.py; b.py; and 3 more" for five names and three further ones.
    """
    rest = f"; and {len(items) - 5} more" if len(items) > 5 else ""
    return "; ".join(shown(one, NOTE_PATH_CHARS) for one in items[:5]) + rest


def _mark_info(mark: HonestyMark) -> HonestyMarkInfo:
    """Return an honesty mark as the protocol carries it.

    Args:
        mark: The mark as the honesty state holds it.

    Returns:
        The same spot and reason and who opened it.
    """
    return HonestyMarkInfo(
        id=mark.id,
        path=mark.path,
        symbol=mark.symbol,
        reason=mark.reason,
        opened_by=mark.opened_by,
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
