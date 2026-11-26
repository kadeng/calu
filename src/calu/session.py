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
                            ├─ TraceWriter         (every emitted event as a redacted Markdown
                            │                       block in the project, `calu.traces`, D232)
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
    the checkers answered                     the honesty checker's marks and the edit's
                                              expected content are kept with the call
                                              (`_keep_honesty`), not applied. Marks that any
                                              other checker returned are ignored (D224).
    the honesty checker answered about a      its dirty marks are recorded at once
    command or a commit                       (`_mark_at_once`): they name existing code
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
    3. an escalation appeared while it was recorded: wait again, for what is still left of
       `HOLD_BUDGET_S`, and look again; still open after that: the approval becomes an
       escalation of its own

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

States (`calu.protocol.messages.State`, agent/records/PLAN.md §3.1) follow what runs at the moment;
steps of the main agent and of subagents overlap, so the most important one wins:

    escalated (an escalation is open) > checking (a checker or the narrator runs)
    > tool_call (an approved call runs) > running (Claude works) > idle
"""

import functools
import getpass
import itertools
import json
import logging
import os
import platform
import re
import socket
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
from anyio.abc import TaskGroup
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from calu import __version__, asuser, human_diffs, machine
from calu.call_locks import CallLocks
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
    SubagentTask,
    ToolPolicy,
)
from calu.checkers.changeset import CommitKind, classify_commit
from calu.checkers.honesty_review import HonestyReviewer, batches
from calu.checkers.policy import EDIT_TOOLS, HANDBACK_TOOL, SHELL_TOOLS
from calu.checkers.render import changed_lines, edited_file, inline, safe_name
from calu.checkers.shell import parse_command
from calu.checkers.tools import StepTime
from calu.checkouts import Checkout, Checkouts, checkout_name, worktree_name
from calu.claude.runner import (
    NOT_FROM_USER_LABEL,
    ClaudeRunner,
    EventSink,
    Fork,
    GateResult,
    HumanAnswer,
    ModelChangeFailed,
    PermissionModeChangeFailed,
    SideQuestionFailed,
    UsageReport,
    Verdict,
    runs_in_background,
)
from calu.claude.sdk_runner import AGENT_TOOLS, HOOK_TIMEOUT_S
from calu.claude.transcripts import unwitnessed_turn
from calu.clock import ClockJump
from calu.comments import Comment, format_comment, map_selection
from calu.comments.mapping import excerpt, split_lines
from calu.config import (
    EMPTY_CHAT_SUMMARY,
    PROVIDER_KEYS,
    PROVIDER_KEYS_BY_PREFIX,
    AutoEscalate,
    CheckerConfig,
    Effort,
    default_model,
)
from calu.context import ContextBuilder, StepRecord, TurnRecord
from calu.context.foreign import RELAY_TEMPLATE as RELAY_TEMPLATE
from calu.context.narrator import ClaudeOutput, Narrator
from calu.defaults import default_instructions
from calu.defaults.instructions import (
    compose_instructions,
    final_redaction_instructions,
    redaction_instructions,
    review_instructions,
)
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
    combine_snapshots,
    find_baseline,
    merge_plans,
    resolve_commit,
    run_git,
    shown,
)
from calu.logfile import one_line
from calu.models import resolve_model
from calu.protocol.messages import (
    REFUSED_PERMISSION_MODES,
    SWITCHABLE_PERMISSION_MODES,
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
    EscalationResolved,
    Event,
    FilesUserEditResult,
    HonestyFileStatus,
    HonestyInfo,
    HonestyMarkInfo,
    HonestyReviewResult,
    HonestyTrustResult,
    HonestyUnitStatus,
    HonestyUpdate,
    HumanDiffAddResult,
    InitializeResult,
    Log,
    PermissionModeChanged,
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
    UsagePanelResult,
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
from calu.store.trust import TrustSource
from calu.tokens import token_hash
from calu.traces import (
    Header,
    Redactor,
    Secrets,
    TraceRenderer,
    TraceWriter,
    session_directory,
)
from calu.traces.finalize import final_redactor, finalize, plans, repository_of
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
TRACE_END_TIMEOUT_S = 60
"""How long `stop` waits for the session trace to be written to its end; longer than the
writer's own limit for that (`calu.traces.writer.FLUSH_TIMEOUT_S`)."""
AUTO_RESUME_LIMIT = 3
"""How many automatic resumes (`Session.auto_resume`, D282) a session makes within
`AUTO_RESUME_WINDOW_S`; one more end in that time leaves it stopped, so that a Claude Code
that ends at every start is not started for ever."""
AUTO_RESUME_WINDOW_S = 600.0
"""The time window of `AUTO_RESUME_LIMIT`, in seconds."""
COMMIT_HOLD_S = 600.0
"""How long the trace writer stays held after the final pass for a commit of Claude's, at most,
if the commit's tool result never arrives (`_finalize_for_commit`)."""

FINAL_PASS_FAILED = (
    "calu redacts the new part of the session traces once more before a commit, and that "
    "final pass failed ({problem}). The commit was not run, so that no trace is committed "
    "without that pass. Tell the user; the commit can be made again once the redaction agent "
    "answers."
)
"""Reason of the denial of a commit whose final trace redaction failed (D246)."""

TRACES_STAGED_NOTE = (
    "[calu] Before this commit, calu redacted the session trace file(s) {paths} once more and "
    "staged them, so the commit records them together with your change. This is intended: "
    "the project keeps its session traces in git (traces.enabled; a project that does not "
    "want that lists the trace directory in .gitignore). Leave them in the commit unless the "
    "user asks otherwise."
)
"""What Claude is told when the final pass staged trace files for its commit: in a dogfooding
run Claude took them for files it had not meant to commit and undid the commit (D246)."""

TRACE_BLOCK_EVENTS = (
    UserInput,
    AssistantText,
    AssistantThinking,
    ToolUse,
    CheckResultEvent,
    EscalationResolved,
    ToolResult,
    TurnResult,
)
"""The events that add a block to the session trace (`calu.traces.render.TraceRenderer`; an
`escalation.request` follows a `check.result` or a `tool.use` of the same step). A change of
the `traces` settings in an idle session takes effect with the first of them
(`Session._follow_traces`)."""

MAX_PENDING_OUTPUTS = 500
"""Thinking/text blocks kept per agent until its next tool call (older ones are dropped)."""

CROSS_SESSION_LABEL = "Message from another Claude session, not from the user"
"""Turn entry label of a message from another session (`calu.context.foreign`)."""
CROSS_SESSION_PENDING = "(content not shown; its narration is pending)"
CROSS_SESSION_HIDDEN = "(content not shown: it could not be narrated)"

HARNESS_ASKED = (
    "Claude Code's own approval asked for a confirmation; calu's checkers had no objection to "
    "this step (PASS, see the check above). Whoever answers decides whether it runs."
)
"""Reason of an escalation that nothing in calu raised (agent/records/PLAN.md D38).

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
    "[Review note from the {route} checker, which reviews your steps on behalf of the user] "
    "{comment} Weigh this for your following steps; the decision is yours."
)
"""How a checker comment on an allowed step is framed for Claude. A reminder or a suggestion,
not an order (the owner's principle of 2026-11-22: the checker is usually a smaller model than
Claude). The earlier wording "Apply this to your following steps." was honored in 3 of 3 live
runs (sitl's `spike_checker_comment.py`); this one is not measured yet."""
SUBAGENT_NOTE_TEMPLATE = (
    '[Review of the result of the subagent "{label}" by the subagent checker, on behalf of the '
    "user: a reminder and suggestions; weigh them, the decision is yours] {note}"
)
"""How the result checker's note is framed for Claude (sent as a priority "next" message)."""
HANDED_BACK_EMPTY = (
    "The final result is empty because the subagent handed its report back with "
    "SubagentHandback, and the result checker reviewed that report then; this empty result "
    "was passed without a check."
)
"""Why an empty final result of a subagent that handed its report back is not reviewed (the
step's recorded reason, D252)."""
HANDBACK_POINTER = re.compile(r"This agent's report was delivered to you as a message from\b")
"""The start of the final result Claude Code (2.1.327) gives a foreground subagent that handed
its report back: a pointer to the report, which counts as an empty result."""
HANDBACK_REFUSED = (
    "The user does not want this report handed back. Do not hand it back again; end your work "
    "now. {note}"
)
"""The reason a subagent reads when a person denied its handed-back report (D252)."""
HANDBACK_REPORT_CHARS = 240_000
"""The longest handed-back report the result checker reads, as for a final result
(`calu.claude.sdk_runner.SUBAGENT_RESULT_CHARS`)."""
DECISION_NOTE_TEMPLATE = (
    "[The user {decision} a {tool} step of this turn that waited for approval, and wrote this "
    "note to Claude]\n{note}"
)
"""How the note a human wrote with a decision about an escalation is recorded as user input
in the checkers' context. `decision` is "allowed" or "denied"."""
QUESTION_ANSWER_TEMPLATE = "[The user's answer to a question Claude asked in this turn]\n{answer}"
"""How a human's answer to a question of the agent is recorded as user input in the checkers'
context."""


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
    checkouts: tuple[Checkout, ...] | None = None
    """The checkouts whose files the review judges (a step's, D251); None for every known
    checkout."""
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


def _message_text(tool_input: dict[str, Any]) -> str:
    """Return the text of a `SendMessage` call.

    Args:
        tool_input: The call's input.

    Returns:
        Its `message` (or `content`) as text; "" if it has neither.
    """
    value = tool_input.get("message", tool_input.get("content", ""))
    return value if isinstance(value, str) else str(value or "")


LOCK_HOOK_MARGIN_S = 30.0
"""Seconds kept back from the PreToolUse hook's timeout (`HOOK_TIMEOUT_S`) when an approved
call waits for its locks there, so that the hook answers before Claude Code gives up on it."""


class SessionError(Exception):
    """A request cannot be served in the current state (reported to the client)."""


def session_os_user() -> str:
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
        secret_env: Collection[str] = (),
        auto_resume: bool = False,
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
                "subagent", "narrator", "summarizer", "honesty-review" and "trace-redactor"
                (tests pass fake models here).
            startup_warnings: Warnings about missing prerequisites, emitted as `log` warnings
                at every `start`, before the runner starts.
            snapshot_root: Where the optional write-set snapshots live (agent/records/PLAN.md D31);
                None uses `calu.changes.default_root()`.
            transcript_inputs: Reads what the user typed in a conversation from Claude Code's
                own transcript, by Claude session id (it may block: it is run in a worker thread).
                Used when a conversation is resumed or forked that the store knows nothing about,
                because it was started without calu (agent/records/PLAN.md D209); None starts such a
                session without that context.
            secret_env: Names of environment variables whose values are secret (the ones calu
                hides from Claude Code, `calu.config.load_secrets`). A session trace never
                holds their values (`calu.traces.redact`).
            auto_resume: When a run ends that calu did not end itself -- an escalation
                timeout stopped it, or Claude Code ended or failed on its own -- resume its
                conversation at once in this object, so that it waits for the user's next
                message (D282). At most `AUTO_RESUME_LIMIT` times within
                `AUTO_RESUME_WINDOW_S`. For a session a person can come back to (`calu
                serve`, interactive `calu run`); False (an unattended run) leaves it stopped.
        """
        self.auto_resume = auto_resume
        self._auto_resumed: list[float] = []
        """`anyio.current_time()` of the automatic resumes within the last
        `AUTO_RESUME_WINDOW_S` (`_resume_after_end`)."""
        self._stops = 0
        """How many times `stop` was called; a run's end with no `stop` since its start is an
        end calu did not ask for."""
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
        self.checkouts = Checkouts(workspace)
        """The main checkout and the git worktrees of the project (`calu.checkouts`, D251);
        read again at the start and before every step (`_refresh_checkouts`)."""
        self.locks = CallLocks(
            self.config.locking, self.step_checkouts, self.checkouts.of, workspace
        )
        """The locks of the session's tool calls (`calu.call_locks`, D276): taken for an
        approved command or edit before it runs, given back with its tool result. With
        `locking.enabled` off the same calls take no lock."""
        self.honesty = HonestyState(
            workspace, self.config.honesty.max_open_marks, self.config.honesty.sufficient_trust
        )
        """The code honesty state of the main checkout, in memory only (`calu.honesty`):
        marks, verified contents, the baseline. Unused while `honesty.mode` is "off"."""
        self._states: dict[Path, HonestyState] = {self.checkouts.main.root: self.honesty}
        """Checkout root -> its honesty state (D251): the main checkout's is `honesty`, a
        worktree's is created when a step or a file first needs it (`_state_of`). All share
        the trust of contents and the mark ids (`HonestyShared`)."""
        self._trace_dir: str | None = None
        """The trace directory relative to a checkout's root while tracing is on, else None;
        no checkout's honesty state judges it (`_exclude_from_states`)."""
        self._human_diffs_dir: str | None = None
        """`human_diffs.directory` while `human_diffs.enabled` is on, else None (D253): calu's
        own output like the trace directory (`_guard_traces`)."""
        self._human_diffs_cleaned = 0.0
        """`anyio.current_time()` of the last cleanup of the human diffs; 0 before the first."""
        self._honesty_lock = anyio.Lock()
        """Held while the honesty state is refreshed, read as a snapshot or changed, so that a
        snapshot never sees half of a tool result's changes."""
        self._trust_stale = True
        """True when the baseline and the stored file trust must be read from the database
        again: at the start, after a trust command, after `trust_changed` and after a failure."""
        self._written: set[str] = set()
        """Files that measured write sets named and the honesty state does not watch yet
        (`_watch_written`); the next worker-thread call of the state takes them."""
        self._honesty_heads: dict[Path, str | None] = {}
        """Checkout root -> the HEAD its baseline was last looked up for; a different HEAD, or
        a checkout not looked up yet, looks the baselines up again."""
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
        """Where the write-set snapshots of this process live (agent/records/PLAN.md D31)."""
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
            stop_session=self._stop_after_timeout_soon,
            resumes_after_timeout=lambda: self.auto_resume,
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
            honesty_review=lambda step, wait_s: self.honesty_review(
                wait_s=wait_s, checkouts=self.step_checkouts(step)
            ),
            reviews_task=self._reviews_task,
            review_task=self._review_task,
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
            checkouts=self.step_checkouts,
            nested=self.checkouts.nested,
            refresh=self._refresh_checkouts,
        )
        """Measures what each command wrote, has it reviewed and undoes a refused change
        (`calu.write_sets`, agent/records/PLAN.md D31)."""
        self.state: State = "stopped"
        self._git_head_value: str | None = None
        self._git_head_time = float("-inf")
        self._pending = 0  # background tasks (narration, turn storage, folding)
        self._folding = False  # a fold of `_fold_if_due` runs (D272)
        self._quiet = anyio.Event()
        self._quiet.set()
        self._queue_ids = itertools.count(1)
        self._login_hint_sent = False
        self.traces: TraceWriter | None = None
        """The writer of this run's trace file while `traces.enabled` is on (`calu.traces`,
        agent/records/PLAN.md D232); None otherwise."""
        self._secret_env = set(secret_env)
        self._trace_as: tuple[object, ...] | None = None
        """`_trace_settings` as they were when the running trace was started (or when it
        was found that none is to be written); None before the first start."""
        self._trace_stale = False
        """True while the `traces` settings differ from `_trace_as` and the switch to a new
        file waits for the session's next block (`_follow_traces`)."""
        self._trace_closed = anyio.Event()
        """Set while no trace of a run waits to be written to its end; `stop` waits for it."""
        self._trace_closed.set()
        self._trace_secrets = Secrets()
        """The values a trace never holds: collected from the environment when a trace
        starts, plus what `add_trace_secret` named."""
        self._commit_hold: str | None = None
        """Claude's id of the commit call for which the trace writer is held
        (`_finalize_for_commit`); the call's tool result releases it."""
        self._permission_mode: str | None = None
        """Claude Code's permission mode as the runner last reported it (`permission_mode`)."""
        self._mode_request: tuple[str, str] | None = None
        """(mode, client) of a `permission_mode.set` under way, so that Claude Code's report
        of that mode names the client that asked for it."""
        self._mode_reports = 0
        """How many mode reports of the runner arrived; `set_permission_mode` compares it."""
        self._guard_traces()

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
            The prompt `compose_instructions` builds: for the honesty checker with the
            configured honesty rules; for the other checkers of the command, edit and
            changeset routes with the part about unverified code while `honesty.mode` is on.
        """
        return compose_instructions(
            route,
            self._instructions(name),
            self.config.honesty.rules,
            honesty_state=self.config.honesty.mode != "off",
            name=name,
        )

    def _result_checker(self) -> ResultChecker:
        """Build the subagent result checker from the configuration as it is now.

        One place for the start and for every reload: before, the reload updated its model and
        its instructions in place and left its request and token limits as they were at the
        start, and no path passed its effort at all (agent/records/PLAN.md D135).

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

    def _trust_needed(self, checkout: Checkout) -> TrustSource:
        """Name the weakest source of trust that counts in a checkout (D251).

        Args:
            checkout: A checkout of the project.

        Returns:
            "user" for a worktree outside the project directory: its files count as verified
            only when a person certified or trusted them (the owner's decision of
            2026-11-20). `honesty.sufficient_trust` for every other checkout.
        """
        return "user" if checkout.outside else self.config.honesty.sufficient_trust

    def _state_of(self, checkout: Checkout) -> HonestyState:
        """Return the honesty state of a checkout, creating it on first use (D251).

        A new state has no baseline yet; the next `_refresh_trust` looks it up from the
        checkout's own HEAD.

        Args:
            checkout: A checkout of the registry.

        Returns:
            The state, which asks git in the checkout's root and names files relative to the
            session directory.
        """
        state = self._states.get(checkout.root)
        if state is None:
            state = HonestyState(
                self.workspace,
                self.config.honesty.max_open_marks,
                self._trust_needed(checkout),
                root=checkout.root,
                shared=self.honesty.shared,
            )
            state.excluded = self._excluded_of(checkout)
            self._states[checkout.root] = state
        return state

    def _state_for_path(self, path: str | Path) -> HonestyState:
        """Return the honesty state that judges a file (D251).

        Args:
            path: Absolute, or relative to the session directory.

        Returns:
            The state of the checkout the file lies in; the main checkout's for a file
            outside every checkout (as before: named by its absolute path there).
        """
        checkout = self.checkouts.of(path)
        return self.honesty if checkout is None else self._state_of(checkout)

    def _scope_states(self, checkouts: Sequence[Checkout] | None) -> list[HonestyState]:
        """Return the honesty states of some checkouts, or of every known one.

        Args:
            checkouts: The checkouts (a step's); None for the main checkout, every known
                worktree and every state created earlier that the registry still lists.

        Returns:
            The states, each once, in the order of `checkouts` (the main checkout's first for
            None).
        """
        if checkouts is not None:
            chosen = [self._state_of(checkout) for checkout in checkouts]
        else:
            chosen = [self._state_of(checkout) for checkout in self.checkouts.all()]
        unique: list[HonestyState] = []
        for state in chosen:
            if state not in unique:
                unique.append(state)
        return unique

    def _excluded_of(self, checkout: Checkout) -> tuple[str, ...]:
        """Name what a checkout's honesty state does not judge.

        Args:
            checkout: A checkout of the registry.

        Returns:
            The checkout's copy of the trace directory while tracing is on, and for the main
            checkout also the worktrees nested in it, which are checkouts of their own (their
            directory entry would otherwise be an unreviewable file of the main checkout).
            Each relative to the session directory, or absolute outside it.
        """
        excluded: list[str] = []
        if self._trace_dir is not None:
            excluded.append(self.checkouts.label(checkout.root / self._trace_dir))
        if self._human_diffs_dir is not None and checkout.main:
            excluded.append(self._human_diffs_dir)
        if checkout.main:
            excluded += self.checkouts.nested()
        return tuple(excluded)

    def _exclude_from_states(self) -> None:
        """Bring every honesty state's `excluded` and the write sets' up to date.

        Called when the trace settings change and after the registry changed.
        Side effects: drops the state of a worktree that the registry no longer lists (git
        removed it, so its files and marks are gone with it).
        """
        known = {checkout.root: checkout for checkout in self.checkouts.all()}
        for root in [root for root in self._states if root not in known]:
            del self._states[root]
            self._honesty_heads.pop(root, None)
        for root, state in self._states.items():
            state.excluded = self._excluded_of(known[root])

    async def clear_mark(self, mark_id: int, client: str = "unknown") -> HonestyMark:
        """Close one open mark by hand: a person's override of a checker (`honesty.clear`).

        Clearing the last open mark of a file is a person's decision about that content, and
        it is stored as one: the SHA-256 of the file's present content is recorded as trusted
        with the source "user", the record `honesty.trust` with a path makes
        (`SessionStore.trust_file`). So the review of a later session does not mark the same
        content again. While other marks are open on the file, nothing is recorded.

        Side effects: closes the mark in the honesty state, records the file's content as
        described (a failure is logged and reported as a warning; the mark stays closed for
        this session), logs who did it and sends `honesty.update`. Whether a review is
        required does not change.

        Args:
            mark_id: The mark's id, as `honesty.update` reported it.
            client: Who asked, for the log and the record.

        Returns:
            The closed mark.

        Raises:
            SessionError: If no open mark has that id.
        """
        by = client or "unknown"
        closed: HonestyMark | None = None
        owner = self.honesty
        async with self._honesty_lock:
            for state in list(self._states.values()):
                closed = await anyio.to_thread.run_sync(state.clear, mark_id, f"{by} (by hand)")
                if closed is not None:
                    owner = state
                    break
        if closed is None:
            raise SessionError(f"no open honesty mark {mark_id}")
        stored = ""
        if not any(mark.path == closed.path for mark in owner.open_marks):
            try:
                sha256 = await anyio.to_thread.run_sync(owner.content_sha256, closed.path)
                if sha256 is not None:
                    note = f"mark cleared by hand: {closed.label()}"[:500]
                    await self.store.trust_file(sha256, closed.path, "user", by, note)
                    self._trust_stale = True
                    stored = f"; the content of {closed.path} ({sha256[:12]}) is certified"
            except Exception as exc:
                log.exception("recording a mark cleared by hand failed")
                await self._emit_safely(
                    Log(
                        level="warning",
                        message=(
                            f"honesty: the clearing of {closed.label()} could not be stored "
                            f"({type(exc).__name__}: {exc}); it holds for this session only"
                        ),
                    )
                )
        await self._emit_safely(
            Log(level="info", message=f"honesty: {by} cleared {closed.label()} by hand{stored}")
        )
        await self.announce_honesty()
        return closed

    async def trust(
        self,
        rev: str = "HEAD",
        path: str | None = None,
        note: str = "",
        client: str = "unknown",
        checkout: str | None = None,
    ) -> HonestyTrustResult:
        """Certify a commit or one file's present content for a person (`honesty.trust`).

        Without `path`, `rev` is resolved with git to the commit it names now, in the main
        checkout or, with `checkout`, in that worktree (the registry is read again first), and
        that commit is recorded as trusted with the source "user". With `path`, the SHA-256
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
            checkout: Where `rev` is resolved: a worktree's name or label, or "." (or None)
                for the main checkout (`Checkouts.named`). Ignored with `path`.

        Returns:
            What was recorded, with the label of the checkout `rev` was resolved in.

        Raises:
            SessionError: If `rev` names no commit, `checkout` names no known checkout, the
                file cannot be read, or the database refused the record.
        """
        by = client or "unknown"
        try:
            if path:
                state = await anyio.to_thread.run_sync(self._state_for_path, path)
                key = await anyio.to_thread.run_sync(state.watch, path)
                sha256 = await anyio.to_thread.run_sync(state.content_sha256, key)
                if sha256 is None:
                    raise SessionError(f"{path} is not a file calu can read")
                recorded = await self.store.trust_file(sha256, key, "user", by, note)
                result = HonestyTrustResult(content_sha256=sha256, path=key, recorded=recorded)
                what = f"the content of {key} ({sha256[:12]})"
            else:
                where = self.checkouts.main
                if checkout and checkout.strip() not in ("", "."):
                    await self._refresh_checkouts()
                    found = self.checkouts.named(checkout)
                    if found is None:
                        known = ", ".join(
                            worktree_name(other.label) for other in self.checkouts.others
                        )
                        raise SessionError(
                            f"no worktree is named {inline(checkout, 200)}; "
                            + (f"the worktrees are: {known}" if known else "there is none")
                        )
                    where = found
                commit = await anyio.to_thread.run_sync(resolve_commit, where.root, rev or "HEAD")
                recorded = await self.store.certify_commit(
                    commit.commit_id, commit.tree_id, "user", by, note
                )
                result = HonestyTrustResult(
                    commit_id=commit.commit_id, recorded=recorded, checkout=where.label
                )
                what = f"commit {commit.short} ({checkout_name(where.label)})"
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

    async def user_edit(self, paths: Sequence[str], client: str = "unknown") -> FilesUserEditResult:
        """Take note that the user writes files through an editor (`files.user_edit`).

        The write is the user's own, not a step of Claude: no checker judges it. Side
        effects: the paths are left out of the write set of a command that runs now
        (`WriteSets.user_edited`); the files are watched for the code honesty state, where
        their new content is unreviewed like any change made outside calu; one `log` event
        names them. The honesty state is computed again at the next checked step.

        Args:
            paths: The files, relative to the workspace, with "/" separators. May be empty.
            client: The client's name, for the log.

        Returns:
            The directories of the session's own output inside the workspace.

        Raises:
            SessionError: If a path is absolute or has a `..` segment.
        """
        for path in paths:
            if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
                raise SessionError(f"{path!r} is not a path relative to the workspace")
        if paths:
            self.write_sets.user_edited(paths)
            self._watch_written(paths)
            shown = ", ".join(inline(path, 200) for path in paths[:5])
            more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
            await self._emit_safely(
                Log(
                    level="info",
                    message=f"{client or 'unknown'}: the user wrote {shown}{more} in the editor",
                )
            )
        return FilesUserEditResult(excluded=list(self.write_sets.excluded))

    async def human_diff(
        self, path: str, before: str, after: str, client: str = "unknown"
    ) -> HumanDiffAddResult:
        """Record the diff of one save of the user in an editor (`human_diffs.add`, D253).

        Records nothing unless `human_diffs.enabled` is on and git tracks the file in its
        checkout; never for a file inside calu's own output (the human diffs, the traces),
        for equal texts or for text with a NUL character. The diff is written as it is,
        without redaction (`calu.human_diffs.record`). Side effects: appends a block to the
        file of that path, user and day; creates the directory with its `.gitignore`; runs
        the cleanup of old `*.tmp.diff` files when the last one is more than a day ago.

        Args:
            path: The file, relative to the workspace, with "/" separators.
            before: The file's text before the save.
            after: The saved text.
            client: The client's name, written into the block.

        Returns:
            Whether the feature is on, whether a diff was written, the file and the reason.

        Raises:
            SessionError: If `path` is absolute, empty or has a `..` segment.
        """
        cfg = self.config.human_diffs
        if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            raise SessionError(f"{path!r} is not a path relative to the workspace")
        if not cfg.enabled:
            return HumanDiffAddResult(enabled=False, reason="off")
        own = [cfg.directory, *([self._trace_dir] if self._trace_dir else [])]
        if any(path == mine or path.startswith(mine + "/") for mine in own):
            return HumanDiffAddResult(enabled=True, reason="calu's own output")
        if before == after:
            return HumanDiffAddResult(enabled=True, reason="unchanged")
        if "\x00" in before or "\x00" in after:
            return HumanDiffAddResult(enabled=True, reason="binary")
        file = self.workspace / path
        if not await anyio.to_thread.run_sync(human_diffs.is_tracked, file):
            return HumanDiffAddResult(enabled=True, reason="not tracked by git")
        saved = human_diffs.Saved(
            path=path,
            before=before,
            after=after,
            when=datetime.now(UTC),
            user=session_os_user(),
            session_id=self.store.session_id,
            client=client,
        )
        try:
            written = await anyio.to_thread.run_sync(
                human_diffs.record, self.workspace, cfg.directory, saved
            )
        except human_diffs.HumanDiffError as exc:
            log.warning("human diffs: %s", exc)
            return HumanDiffAddResult(enabled=True, reason=str(exc))
        if anyio.current_time() - self._human_diffs_cleaned > human_diffs.CLEANUP_INTERVAL_S:
            await self._clean_human_diffs()
        if written is None:
            return HumanDiffAddResult(enabled=True, reason="unchanged")
        return HumanDiffAddResult(
            enabled=True, recorded=True, file=written.relative_to(self.workspace).as_posix()
        )

    async def _clean_human_diffs(self) -> None:
        """Delete the `*.tmp.diff` files older than `human_diffs.keep_days`; never raises.

        Side effects: `calu.human_diffs.cleanup` in a worker thread; one `log` event with
        the count when files were deleted; sets `_human_diffs_cleaned`.
        """
        self._human_diffs_cleaned = anyio.current_time()
        cfg = self.config.human_diffs
        try:
            removed = await anyio.to_thread.run_sync(
                human_diffs.cleanup, self.workspace, cfg.directory, cfg.keep_days
            )
        except Exception:
            log.exception("the cleanup of the human diffs failed")
            return
        if removed:
            await self._emit_safely(
                Log(
                    level="info",
                    message=f"human diffs: deleted {len(removed)} file(s) older than "
                    f"{cfg.keep_days} days below {cfg.directory}",
                )
            )

    async def honesty_review(
        self,
        *,
        asked: bool = False,
        wait_s: float | None = None,
        checkouts: Sequence[Checkout] | None = None,
    ) -> str:
        """Run the honesty review: judge the unreviewed and the marked files (section 4).

        The gate calls this for a step that needs a clean honesty state, and `request_review`
        calls it for a person (`_run_review` has the steps).

        Args:
            asked: True when a person asked for the review.
            wait_s: The longest time to wait for the review's end, in seconds; None waits
                until it ends.
            checkouts: The checkouts whose files are judged (the step's, D251); None for
                every known checkout.

        Returns:
            "" if a review ran and judged every file it had to (the caller then reads the
            state again); otherwise one sentence saying why none ran, why it failed, which
            files it could not judge, or that it is still running. Never raises.
        """
        return (await self._run_review(asked, wait_s, checkouts)).problem

    async def _run_review(
        self,
        asked: bool,
        wait_s: float | None = None,
        checkouts: Sequence[Checkout] | None = None,
    ) -> HonestyReviewResult:
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

        With worktrees (D251) the review judges the files of the given checkouts, each against
        its own baseline, in one reviewer run. A worktree outside the project directory is
        left out: only a person's certification verifies its files, and the result says so
        while files there are not verified.

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
            checkouts: The checkouts whose files are judged; None for every known checkout.

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
                scope = None if checkouts is None else tuple(checkouts)
                run = self._review_run = ReviewRun(fresh=asked, checkouts=scope)
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
            plan, outside = await anyio.to_thread.run_sync(
                self._review_plan, self._take_written(), cfg.review_max_files, run.checkouts
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
                    f"{len(plan.shortcuts) - len(plan.no_code)} with a whitespace-only change, "
                    f"{len(plan.no_code)} without code (plain text, data, media), "
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
            return HonestyReviewResult(ran=False, problem=f"{outcome.problem} {outside}".strip())
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
        parts: list[str] = [outside] if outside else []
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

    def _review_plan(
        self,
        written: Collection[str],
        max_files: int,
        checkouts: Sequence[Checkout] | None = None,
    ) -> tuple[ReviewPlan, str]:
        """Compute the plan of an honesty review (reads git and files; for a worker thread).

        Args:
            written: Files that commands wrote since the last look (`_take_written`); they
                are watched from now on.
            max_files: `honesty.review_max_files`, for all checkouts together.
            checkouts: The checkouts to judge; None for every known one.

        Returns:
            (`HonestyState.review_plan` of each checkout inside the project directory,
            combined (`merge_plans`), after the marks whose code is gone were closed; "" or
            one sentence naming the worktrees outside the project directory in scope whose
            state is not clean, which only a person's certification settles).
        """
        self._watch_all(written)
        chosen = self.checkouts.all() if checkouts is None else checkouts
        outside: list[str] = []
        plans: list[ReviewPlan] = []
        for checkout in dict.fromkeys(chosen):
            state = self._state_of(checkout)
            state.close_stale()
            if checkout.outside:
                if not state.snapshot().clean:
                    outside.append(checkout.label)
                continue
            plans.append(state.review_plan(max_files))
        plan = merge_plans(plans) if plans else ReviewPlan(self.honesty.baseline)
        note = ""
        if outside:
            note = (
                "The files of a worktree outside the project directory count as verified only "
                f"when a person certifies them ({', '.join(outside)}): `/trust` or `/trust-file` "
                "in a client of the session, or `calu honesty trust` in a terminal."
            )
        return plan, note

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
        by_state: dict[int, tuple[HonestyState, dict[str, FileReview]]] = {}
        for path, review in results.items():
            state = self._state_for_path(path)
            by_state.setdefault(id(state), (state, {}))[1][path] = review
        changes = MarkChanges()
        store: list[tuple[str, str]] = []
        seen: dict[str, FileSeen] = {}
        for state, part in by_state.values():
            done = state.apply_current_review(part)
            _add_changes(changes, done)
            changes.dropped += done.dropped
            for path in part:
                held = state.storable(path)
                if held is not None and held.source == "review":
                    store.append((held.sha256, state.key(path)))
            state.close_stale()
            dropped = {path for path, _why in done.dropped}
            seen.update(state.seen_now(set(part) - dropped))
        changes.review_required = any(state.review_required for state in self._states.values())
        return changes, store, seen

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

    async def honesty_snapshot(
        self, checkouts: Sequence[Checkout] | None = None
    ) -> HonestySnapshot:
        """Compute the honesty state as it is now, and tell the clients if it changed.

        The state of several checkouts is combined (`combine_snapshots`, D251): it is clean
        only if each is. Only the state of every known checkout is sent to the clients.

        Before the state is read, the settings `honesty.max_open_marks` and
        `honesty.sufficient_trust` are applied, the baseline and the stored file trust are read
        again if HEAD moved or the stored trust changed (`_refresh_trust`), and the marks whose
        code is gone are closed. git and the files are read in a worker thread.

        Side effects: may query the database; may close marks; emits `honesty.update` if the
        state differs from the one last sent.

        Args:
            checkouts: The checkouts to look at (a step's); None for every known checkout.

        Returns:
            The state.

        Raises:
            Exception: If git or the worker thread fails in a way `HonestyState` does not turn
                into "unreviewed". A database failure does not raise: the session then has no
                baseline.
        """
        async with self._honesty_lock:
            await anyio.to_thread.run_sync(self._scope_states, checkouts)
            await self._refresh_trust()
            snapshot = await anyio.to_thread.run_sync(
                self._settled_snapshot, self._take_written(), checkouts
            )
        if checkouts is None:
            await self._emit_honesty(snapshot)
        return snapshot

    def _settled_snapshot(
        self, written: Collection[str], checkouts: Sequence[Checkout] | None = None
    ) -> HonestySnapshot:
        """Close the marks whose code is gone and compute the state (reads git and files).

        For a worker thread: the state's methods wait for one another and read many files.

        Args:
            written: Files that commands wrote since the last look (`_take_written`); they
                are watched from now on.
            checkouts: The checkouts to look at; None for every known checkout.

        Returns:
            The state after that, combined over the checkouts (`combine_snapshots`): for a
            step, or as the overview of the whole session when `checkouts` is None.
        """
        self._watch_all(written)
        parts: list[HonestySnapshot] = []
        for state in self._scope_states(checkouts):
            state.close_stale()
            parts.append(state.snapshot())
        if checkouts is None:
            labels = [self.checkouts.label(state.root) for state in self._scope_states(None)]
            return combine_snapshots(parts, labels)
        return combine_snapshots(parts)

    def _watch_all(self, written: Collection[str]) -> None:
        """Watch files that commands wrote, each in the state of its checkout (D251).

        For a worker thread.

        Args:
            written: Paths relative to the session directory, or absolute.
        """
        for path in written:
            self._state_for_path(path).watch(path)

    def _take_written(self) -> set[str]:
        """Hand out the files that measured write sets named since the last call.

        Called on the event loop, where `_watch_written` adds to the set.

        Returns:
            The paths, relative to the workspace; the session's set is empty afterwards.
        """
        written, self._written = self._written, set()
        return written

    async def _gate_honesty(self, step: Step) -> HonestySnapshot | None:
        """Give the gate the honesty state for a step (`StepGate`'s `honesty`).

        Args:
            step: The tool call; the state is the one of its checkouts (`step_checkouts`,
                D251), so a mark in another worktree does not stop it.

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
            scope = await anyio.to_thread.run_sync(self.step_checkouts, step)
            if scope == list(self.checkouts.all()):
                return await self.honesty_snapshot()  # the whole session: the clients get it
            return await self.honesty_snapshot(scope)
        except Exception:
            if mode == "enforce":
                raise
            log.exception("computing the honesty state failed")
            return None

    async def _head(self, root: Path | None = None) -> str | None:
        """Read the commit id of HEAD now, without the cache of `_git_head`.

        Args:
            root: The checkout whose HEAD is read; None for the session directory.

        Returns:
            The full commit id; None if the directory is in no repository, has no commit yet,
            or git could not be asked.
        """
        try:
            answer = await anyio.to_thread.run_sync(
                run_git, root or self.workspace, ["rev-parse", "--verify", "--quiet", "HEAD"]
            )
        except GitError:
            return None
        return answer.decode(errors="replace").strip() or None

    async def _refresh_trust(self) -> None:
        """Bring the honesty states' settings, baselines and stored file trust up to date.

        Each checkout's baseline is the nearest trusted ancestor of its own HEAD (D251). The
        baselines and the stored trust are read from the database when a checkout's HEAD is
        another commit than at the last look, a checkout is new, or `_trust_stale` is set. If
        the database cannot be asked, the states have no baseline and no stored trust until a
        later look succeeds, and a warning is logged. The caller holds `_honesty_lock`.
        """
        cfg = self.config.honesty
        known = {checkout.root: checkout for checkout in self.checkouts.all()}
        for root, state in self._states.items():
            state.max_open_marks = cfg.max_open_marks
            wanted = self._trust_needed(known[root]) if root in known else cfg.sufficient_trust
            if state.sufficient_trust != wanted:
                state.sufficient_trust = wanted
                self._trust_stale = True
        heads = {root: await self._head(root) for root in self._states}
        if not self._trust_stale and heads == self._honesty_heads:
            return
        self._trust_stale = False  # a change announced while this reads sets it again
        try:
            baselines = {
                root: await find_baseline(
                    root, self.store.trusted_among, minimum=state.sufficient_trust
                )
                for root, state in self._states.items()
            }
            hashes = await self.store.trusted_file_sources()
        except Exception as exc:
            log.exception("reading the stored trust failed")
            for state in self._states.values():
                state.baseline = None
            self.honesty.trusted_hashes = {}
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
        for root, state in self._states.items():
            state.baseline = baselines[root]
        self.honesty.trusted_hashes = hashes
        self._honesty_heads = heads

    def _worktree_names(self) -> list[str]:
        """Name the project's worktrees for the clients (`HonestyInfo.worktrees`).

        Returns:
            `worktree_name` of each worktree the registry knows, sorted.
        """
        return sorted(worktree_name(checkout.label) for checkout in self.checkouts.others)

    async def _announce_worktrees(self) -> None:
        """Send `honesty.update` with the new list of worktrees, when it changed.

        The rest of the honesty state is the one last sent; it is computed again at the next
        checked step as before. Never raises.
        """
        names = self._worktree_names()
        if names == self._honesty_sent.worktrees:
            return
        self._honesty_sent = self._honesty_sent.model_copy(update={"worktrees": names})
        await self._emit_safely(HonestyUpdate(**self._honesty_sent.model_dump()))

    async def _emit_honesty(self, snapshot: HonestySnapshot | None) -> None:
        """Tell the clients the honesty state, unless they already have exactly this.

        Side effects: emits `honesty.update` and remembers what it sent.

        Args:
            snapshot: The state as computed; None sends only the mode (for the mode "off").
        """
        info = HonestyInfo(
            mode=self.config.honesty.mode,
            review_running=self._review_running,
            worktrees=self._worktree_names(),
        )
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

        Marks are taken from the honesty checker's run alone (`Checker.judges_honesty`,
        agent/records/PLAN.md D224); marks in the answer of any other checker are ignored. An edit
        verifies its file only if the honesty checker judged it and every checker of the route
        approved: without the honesty checker nothing judges honesty per step, and the file
        stays unreviewed until an honesty review or a person's trust.

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
            if checker.judges_honesty
            and ((run.decision.marks_dirty and not at_once) or run.decision.marks_clean)
        ]
        edit_path: str | None = None
        if step.tool_name in EDIT_TOOLS:
            raw = step.tool_input.get("file_path") or step.tool_input.get("notebook_path")
            edit_path = str(Path(step.cwd or self.workspace) / raw) if raw else None
        checked = (
            decided.route is Route.EDIT
            and any(checker.judges_honesty for checker, _run in decided.runs)
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
        """Record the dirty marks the honesty checker returned about a command or a commit.

        A honesty checker that runs on the command route, or with the changeset checkers of a
        commit, marks code that exists in the project whatever the command does. So its
        `marks_dirty` are recorded for every decision, also for a step that is denied, escalated or
        fails. The marks of an edit are different: they name what the edit would write, and are
        applied only after it ran (`_keep_honesty`, `_apply_honesty`). Marks in the answer of any
        other checker are ignored (agent/records/PLAN.md D224). The shipped honesty checker is on
        the edit route, so with the defaults this records nothing.

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
            if checker.judges_honesty and run.decision.marks_dirty
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
            for state in list(self._states.values()):
                state.review_required = True
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
        self._watch_all(written)
        edited: set[str] = set()
        edit_state: HonestyState | None = None
        if pending.edit_path is not None:
            edit_state = self._state_for_path(pending.edit_path)
            edited.add(edit_state.watch(pending.edit_path))
        # Each mark goes to the state of its file's checkout (D251). Within a state, all
        # checkers' clean marks come before any dirty mark (`record_step`): one checker's
        # HONEST must not close the mark another checker opens in the same step.
        parts: dict[int, tuple[HonestyState, list[tuple[str, list[CodeMark], list[CodeMark]]]]]
        parts = {}
        for name, dirty, clean in pending.marks:
            for kind, marks in (("dirty", dirty), ("clean", clean)):
                for one in marks:
                    state = self._state_for_path(one.path.strip() or ".")
                    answers = parts.setdefault(id(state), (state, []))[1]
                    if not answers or answers[-1][0] != name:
                        answers.append((name, [], []))
                    _name, dirty_part, clean_part = answers[-1]
                    (dirty_part if kind == "dirty" else clean_part).append(one)
        total = MarkChanges()
        for state, answers in parts.values():
            _add_changes(total, state.record_step(answers, edited))
        store: list[tuple[str, str]] = []
        if pending.edit_path is not None and edit_state is not None:
            edit_state.record_edit(
                pending.edit_path, pending.expected_sha256 if pending.checked else None
            )
            held = edit_state.storable(pending.edit_path)
            if held is not None:
                store.append((held.sha256, edit_state.key(pending.edit_path)))
        for state in list(self._states.values()):
            state.close_stale()
        total.review_required = any(state.review_required for state in self._states.values())
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
        root = pending.root or self.workspace
        head = await self._head(root)
        if head is None or head == pending.head_before:
            return
        state = self._states.get(root, self.honesty)
        verdict = await anyio.to_thread.run_sync(state.certification, head)
        if verdict.problem or verdict.source is None:
            await self._emit_safely(
                Log(
                    level="info",
                    message=f"honesty: commit {head[:12]} is not certified: {verdict.problem}",
                )
            )
            return
        source = "review" if verdict.source == "user" else verdict.source
        commit = await anyio.to_thread.run_sync(resolve_commit, root, head)
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

        Like `StepGate.run_checkers`, with two additions while `honesty.mode` is not "off": the
        checkers get the note of the honesty state, and the marks of a honesty checker among them
        are recorded at once, because the command has already run (`_apply_record`); marks in the
        answer of any other checker are ignored (agent/records/PLAN.md D224). A change the checkers
        deny is undone, so the marks of a DENY, which name that change, are not recorded.

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
        state = await self._gate_honesty(step)
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
                    if checker.judges_honesty
                    and (run.decision.marks_dirty or run.decision.marks_clean)
                ]
            )
            if pending.marks:
                await self._apply_record(pending)
        return decision, runs

    async def reload_summary(self) -> None:
        """Read the chat summary again, in case an operator corrected it.

        See agent/records/PLAN.md D40.

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
        escalation it has. The change lives in the running session only -- it is not written to the
        project's settings -- so a restart goes back to what `calu config` holds. Switching a
        category off weakens the session, so the change goes into the audit log with the name of the
        client that asked for it (agent/records/PLAN.md D43); a change that changes nothing is not
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
        instructions change, and the rules object is replaced as a whole. Changed `locking`
        settings apply to the calls whose check begins afterwards (`CallLocks.reconfigure`). The
        settings of the running Claude session itself (model, effort, permission mode) are not
        changed, because Claude Code reads them when the session starts; they apply to the next
        `start`. A changed `traces` section ends the running trace file and, if tracing is on,
        starts a new one (`_follow_traces`).

        Args:
            settings: The newly loaded settings.
        """
        claude = self.config.claude
        traced = self._trace_settings()
        self.settings = settings
        self.config = settings.config.model_copy(update={"claude": claude})
        self.rules = settings.rules
        self.policy = ToolPolicy(
            overrides=self.config.tool_routes, default=self.config.default_route
        )
        self.context.config = self.config.context
        # Locking follows a changed setting from the next call whose check begins (D276).
        self.locks.reconfigure(self.config.locking)
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
        self._follow_traces(traced)

    # ------------------------------------------------------------------ client requests

    def step_times(self) -> Sequence[StepTime]:
        """Return when the recent steps of this session ran, oldest first.

        See agent/records/PLAN.md D32.

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

        With locking on (D276), the command still holds its locks here, also while a person
        decides about its write set: they are given back with its tool result. So no other
        call writes in the command's checkouts until the change stands or was undone, and the
        undo cannot overwrite another call's edit. Other sessions that share the locks then
        wait, too (agent/records/notes/locking.md, section 9).

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

        A killed process cannot set its own state, so without this a row stays at its last state for
        ever and a herd cannot tell a working session from a dead one (agent/records/PLAN.md D56).
        Runs until cancelled; a failed heartbeat is logged and the loop goes on, because losing the
        stamp is not a reason to stop a session that is otherwise fine.

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
        """Tell whether a client may attach to this session with `token`.

        See agent/records/PLAN.md D30.

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
            permission_mode=self._permission_mode,
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

        The turns include one that has not ended, with its inputs and its prompt uuid only
        (`SessionHistoryResult`): the running turn, so that a client tells the replayed events
        of that turn from the record by the prompt uuid, and a turn a fork inherited while it
        waited on a decision.

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
        (agent/records/PLAN.md §6.2), so this is read-only.

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
            fork: Where to branch off the resumed conversation (agent/records/PLAN.md D36); None
                resumes it whole. Needs `resume_id`; the forked conversation gets its own session
                id, so the one that was forked from is left untouched.
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
        await self._refresh_checkouts()
        # The baseline of this run: the nearest trusted ancestor of HEAD as it is now.
        self._trust_stale = True
        await self.announce_honesty()
        for warning in self.startup_warnings:
            await self.emit(Log(level="warning", message=warning))
        self._permission_mode = None  # the new run reports its own
        runner = self.runner = self.runner_factory()
        # After the runner is set and without an await in between: the end of an earlier run
        # closes the trace only while its own runner is the current one (`_run`).
        self._start_traces(tg, resume_id, fork is not None, chosen)
        if self.config.human_diffs.enabled:
            tg.start_soon(self._clean_human_diffs)
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
        # id calu chose is known now, and so is the id of a plain resume, which Claude Code
        # keeps; Claude Code reports it with the first turn only. Without the resumed id, a
        # timeout in the first turn of a resumed run named `calu sessions` instead of
        # `calu run --resume <id>` (test plan M15).
        plain_resume = resume_id if fork is None else None
        await self._record_claude_session(runner.session_id or chosen or plain_resume)
        if self._permission_mode is None and runner.permission_mode is not None:
            # Claude Code reports its mode with the first turn; until then the mode calu
            # started it in is the best a client can be shown.
            await self._note_permission_mode(runner.permission_mode, None)
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

        Never raises: runner errors are logged and reported as a `log` event. A run that
        ended without a `stop` since it began -- Claude Code ended or failed on its own -- is
        resumed afterwards in a task of its own with `auto_resume` (`_resume_after_end`,
        D282).

        Args:
            runner: The runner of this session.
            resume_id: Claude session id to resume, or None.
            fork: Where to branch off the resumed conversation, or None (agent/records/PLAN.md D36).
            session_id: The id a new or forked conversation gets, or None (D203).
        """
        stops = self._stops
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
                self.locks.close()  # every lock and ticket of the session (D276)
                await self.desk.cancel_all("The Claude session ended.")
                if self._run_state.queue:  # messages Claude did not read are lost with the session
                    self._run_state.queue.clear()
                    self._run_state.held.clear()
                    await self._emit_safely(ChatQueue(messages=[]))
                await self._wait_quiet()  # the last turn is recorded before the session ends
                final: State = "failed" if self._run_state.run_error else "stopped"
                await self._set_state_safely(final)
                if self.runner is runner:
                    await self._end_traces(f"The session ended ({final}).")
                await self.recorder.attempt(
                    "recording the end of the session", self.store.end(final)
                )
                await self.write_sets.discard()
            tg = self._run_state.tg
            if self.auto_resume and self._stops == stops and tg is not None:
                why = self._run_state.run_error or "Claude Code ended the session"
                tg.start_soon(self._resume_after_end, tg, stops, why)

    async def stop(self) -> None:
        """Stop the Claude session and wait until its runner has ended.

        Open escalations are cancelled (their tool calls are denied). Claude's text that was
        not narrated yet is dropped. Waits for the background work of the last turn (narration,
        storing the turn, folding), at most `BACKGROUND_TIMEOUT_S` seconds, and for the
        session trace to be written to its end, at most `TRACE_END_TIMEOUT_S` seconds. Does
        nothing if no session runs and nothing is pending. A run that ends after this call was
        ended by calu, and is not resumed automatically (`auto_resume`).
        """
        self._stops += 1
        self._run_state.claude_outputs.clear()
        if not self._run_state.run_done.is_set():
            await self.desk.cancel_all("The session was stopped.")
            assert self.runner is not None
            await self.runner.stop()
            await self._run_state.run_done.wait()
        await self._wait_quiet()
        with anyio.move_on_after(TRACE_END_TIMEOUT_S):
            # The run's last act is to write what its trace still holds (`_run`).
            await self._trace_closed.wait()
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
            newest state of each usage limit Claude Code reported in this calu process, and cost
            and turn count of the last turn result.
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

    async def usage_panel(self) -> UsagePanelResult:
        """Return what a client's usage panel shows (`usage.panel`, agent/records/PLAN.md D248).

        Claude Code's `/usage` text and the account's limits are read with control requests
        (`ClaudeRunner.usage_report`), not sent as a chat message: no turn starts, nothing is
        recorded in the history, no event is published, and the checkers see nothing of it.
        The limits kept from Claude Code's reports are not changed by it.

        Returns:
            The limits Claude Code reported in this calu process, each with when it was
            reported; Claude Code's `/usage` text and the account's limits as it reads them
            now (empty, with the reason in `claude_usage_problem`, when no session runs); when
            the account's limits cannot be read now (`get_usage` failed or answered in an
            unknown shape, or no session runs), the reason in `account_limits_problem`,
            pointing to the reported limits (D250); the context fill, cost and turn count of
            `usage`.
        """
        usage = await self.usage()
        stopped = "no Claude session is running"
        report = UsageReport(problem=stopped, account_problem=stopped)
        if self.runner is not None and self.state not in ("stopped", "failed", "starting"):
            report = await self.runner.usage_report()
        problem = report.account_problem
        if problem:
            problem += (
                "; the limits above are the last ones Claude Code reported in this session"
                if usage.rate_limits
                else "; Claude Code has not reported a limit in this session yet either"
            )
        return UsagePanelResult(
            limits=usage.rate_limits,
            account_limits=[] if report.account_problem else list(report.account_limits),
            account_limits_problem=problem,
            subscription=report.subscription,
            claude_usage=report.text,
            claude_usage_problem=report.problem,
            context=usage.context,
            cost_usd=usage.cost_usd,
            num_turns=usage.num_turns,
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

    @property
    def permission_mode(self) -> str | None:
        """Claude Code's permission mode as last reported (see `SessionSnapshot`).

        Returns:
            The mode, or None before the first run reported or started one.
        """
        return self._permission_mode

    async def set_permission_mode(self, mode: str, client: str = "unknown") -> None:
        """Switch Claude Code's permission mode while the session runs (`permission_mode.set`).

        The live counterpart of Claude Code's Shift+Tab (agent/records/PLAN.md D247). Order:

            refuse? -> audit entry -> runner.set_permission_mode -> permission_mode.changed

        The audit entry comes first, as for `set_auto_escalate`: a change nobody can see
        afterwards is worse than a refused request. If Claude Code then refuses the mode, a
        second entry records that it stayed as it was. The change applies from the next tool
        call on, between turns and inside a running turn, and to a call whose check is still
        running: the runner answers it by the mode at that moment (D250). The mode is also kept as
        `claude.permission_mode` of this process, so that a later start (a resume, a fork, a
        new conversation) begins in it; the project's settings are not changed.

        Args:
            mode: The mode the client asked for.
            client: Name of the client that asked, for the audit entry and the event.

        Raises:
            SessionError: If the mode is not one of `SWITCHABLE_PERMISSION_MODES` (with the
                reason of `REFUSED_PERMISSION_MODES`), if `claude.require_auto_mode` is set
                and the mode is not "auto", if no session runs, or if Claude Code refused the
                mode.
            psycopg.Error: If the audit entry cannot be written; nothing was changed then.

        Side effects:
            Writes one audit entry (two if Claude Code refused), switches Claude Code's mode,
            changes `config.claude.permission_mode` and emits `permission_mode.changed` with
            the client's name, unless Claude Code's own report of the mode came first (that
            report then carries the name). A mode the session already runs in changes nothing.
        """
        if mode not in SWITCHABLE_PERMISSION_MODES:
            reason = REFUSED_PERMISSION_MODES.get(mode, "Claude Code has no such mode")
            offered = ", ".join(SWITCHABLE_PERMISSION_MODES)
            raise SessionError(
                f"permission mode {mode!r} refused: {reason}. calu offers {offered}."
            )
        if self.config.claude.require_auto_mode and mode != "auto":
            raise SessionError(
                f"permission mode {mode!r} refused: claude.require_auto_mode is set, so a step "
                "needs auto mode's classifier as well as the checkers and the session stays in "
                '"auto"'
            )
        runner = self._require_running()
        before = self._permission_mode
        if before == mode:
            return
        await self.store.note_setting_change(
            "permission_mode", {"mode": before}, {"mode": mode}, client
        )
        reports = self._mode_reports
        self._mode_request = (mode, client)
        try:
            await runner.set_permission_mode(mode)
        except PermissionModeChangeFailed as exc:
            await self.recorder.attempt(
                "recording that Claude Code refused a permission mode",
                self.store.note_setting_change(
                    "permission_mode",
                    {"mode": mode},
                    {"mode": before},
                    "session: Claude Code refused the change",
                ),
            )
            raise SessionError(str(exc)) from exc
        finally:
            self._mode_request = None
        self.config.claude = self.config.claude.model_copy(update={"permission_mode": mode})
        if self._mode_reports == reports:
            # Claude Code's status message has not arrived yet; it confirms the mode (and
            # changes nothing) or reports the one it really took.
            await self._note_permission_mode(mode, client)

    async def _note_permission_mode(self, mode: str, client: str | None) -> None:
        """Take a permission mode as the current one and tell every client if it is new.

        Args:
            mode: The mode Claude Code reported, or the one it accepted.
            client: The client that asked for it; None for Claude Code's own report.
        """
        if mode == self._permission_mode:
            return
        self._permission_mode = mode
        await self._emit_safely(PermissionModeChanged(mode=mode, client=client))

    async def send_chat(self, params: ChatSendParams) -> None:
        """Send a chat message to the main agent, or relayed to a background subagent.

        "queued" messages are read after the current turn, "inject" messages as soon as the
        running tool calls finish. A message to a subagent is wrapped in a request to the main
        agent to relay it with SendMessage and is always injected.

        A "queued" message for the main agent that is sent while a turn runs is **held** by
        calu (D283): it is listed in `chat.queue` with `held` set and written to Claude Code
        when the turn ends (`_release_held`), so that until then the user can withdraw it
        (`retract_queued`) or send it at once (`send_queued_now`). Claude reads such a
        message at the end of the turn either way. Every other message is written at once.

        Args:
            params: Text, delivery and target of the message.

        Raises:
            SessionError: If no session runs, the target subagent is unknown, or it is not a
                running background subagent.
        """
        runner = self._require_running()
        if params.delivery == "queued" and params.to == "main" and self._run_state.busy:
            queued = QueuedMessage(
                id=f"q{next(self._queue_ids)}",
                text=params.text,
                delivery="queued",
                to="main",
                held=True,
            )
            self._run_state.queue.append((queued, params.text))
            self._run_state.held[queued.id] = list(params.images)
            await self._emit_queue()
            return
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
            # The main agent relays the user's words with SendMessage; that call is not
            # reviewed as Claude's own task for the subagent (`_reviews_task`).
            self._run_state.relays.append(params.text)
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

    def _held_index(self, message_id: str) -> int:
        """Find a held queued message by its id.

        Args:
            message_id: The `QueuedMessage.id`.

        Returns:
            Its index in the queue.

        Raises:
            SessionError: If no queued message has that id, or calu no longer holds it (it
                was handed to Claude Code, which cannot give a message back).
        """
        for i, (queued, _) in enumerate(self._run_state.queue):
            if queued.id == message_id:
                if message_id not in self._run_state.held:
                    raise SessionError(
                        "Claude Code has this message already; it can no longer be withdrawn "
                        "or sent at once"
                    )
                return i
        raise SessionError(f"no queued message {message_id!r}")

    async def retract_queued(self, message_id: str) -> str:
        """Withdraw a message that calu still holds (`chat.retract`, D283).

        Side effects: removes it from the queue and sends `chat.queue`. Claude never sees it.

        Args:
            message_id: The `QueuedMessage.id`.

        Returns:
            The user's text of the message.

        Raises:
            SessionError: As `_held_index`.
        """
        queued, _ = self._run_state.queue.pop(self._held_index(message_id))
        self._run_state.held.pop(message_id, None)
        await self._emit_queue()
        return queued.text

    async def send_queued_now(self, message_id: str) -> None:
        """Send a held message at once: stop the running turn, then send it (`chat.send_now`).

        This is what the user's stop followed by sending the message does (D283). The message
        moves to the front of the held ones and the turn is interrupted (`interrupt`); the end
        of the turn writes the held messages to Claude Code (`_release_held`), this one first.
        If no turn runs any more, they are written at once.

        Side effects: sends `chat.queue`; interrupts the turn.

        Args:
            message_id: The `QueuedMessage.id`.

        Raises:
            SessionError: As `_held_index`, or if no session runs.
        """
        self._require_running()
        entry = self._run_state.queue.pop(self._held_index(message_id))
        first = next((i for i, (q, _) in enumerate(self._run_state.queue) if q.held), None)
        self._run_state.queue.insert(len(self._run_state.queue) if first is None else first, entry)
        await self._emit_queue()
        if not self._run_state.busy:
            await self._release_held()
            return
        await self.interrupt()

    async def _release_held(self) -> None:
        """Write the held messages to Claude Code, oldest first, once a turn has ended (D283).

        They go as "later" messages, so Claude reads them as it would have at the end of the
        turn; they stay in the queue, no longer held, until Claude takes them in. Does nothing
        without held messages or without a running session. A failed write is reported as a
        `log` error, and the message is dropped from the queue.

        Side effects: writes to the runner; sends `chat.queue`; marks the session busy.
        """
        if not self._run_state.held or self.runner is None or self._run_state.run_done.is_set():
            return
        runner = self.runner
        for queued, text in list(self._run_state.queue):
            images = self._run_state.held.pop(queued.id, None)
            if images is None:
                continue
            index = next(j for j, (q, _) in enumerate(self._run_state.queue) if q.id == queued.id)
            try:
                await runner.send(text, "later", images)
            except Exception as exc:
                log.exception("sending a held message failed")
                del self._run_state.queue[index]
                await self._emit_safely(
                    Log(
                        level="error",
                        message=f"A queued message could not be sent ({one_line(str(exc))}): "
                        f"{one_line(queued.text, 200)}",
                    )
                )
                continue
            self._run_state.queue[index] = (queued.model_copy(update={"held": False}), text)
        await self._emit_queue()
        await self._mark_busy()

    async def _take_from_queue(self, text: str) -> QueuedMessage | None:
        """Remove the oldest queued message whose sent text is `text`, then send `chat.queue`.

        Does nothing if no queued message matches.

        Args:
            text: The text of a user message that Claude took in.

        Returns:
            The removed message as the user sent it (its `text` is the user's own text, also
            for a message that was wrapped for a subagent); None if none matched.
        """
        for i, (queued, sent) in enumerate(self._run_state.queue):
            if sent.strip() == text.strip():
                del self._run_state.queue[i]
                await self._emit_queue()
                return queued
        return None

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
        `resolved_by` saying that a client of this session decided and which one
        (agent/records/PLAN.md D28). A client needs no database login for that: it authenticates to
        the session with its token, and the session speaks to the database for it. An operator who
        resolves the row in the database instead reaches the session through `note_resolution` or
        its poll.

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
            answer: The answer to a question of the agent (`escalation.request` with `questions`,
                agent/records/PLAN.md D35). The call does not run; Claude receives this text as its
                result, and the row is recorded as "answered".
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

    async def _refresh_checkouts(self) -> None:
        """Read the project's worktrees again (`Checkouts.refresh`, in a worker thread).

        Called at the start and before every step: one `git worktree list` costs a few
        milliseconds, and so a worktree that `EnterWorktree`, a subagent, `ExitWorktree` or a
        shell command created or removed is known when the next step is judged (D251). When
        the list changed, the clients get the new worktree names (`_announce_worktrees`). Never
        raises; when git cannot be asked the registry keeps what it had.
        """
        try:
            if await anyio.to_thread.run_sync(self.checkouts.refresh):
                self._exclude_from_states()
                await self._announce_worktrees()
        except Exception:
            log.exception("reading the worktrees failed")

    def step_checkouts(self, step: Step) -> list[Checkout]:
        """Name the checkouts a tool call belongs to (`Checkouts.step_checkouts`, D251).

        Args:
            step: The tool call.

        Returns:
            The checkout of its working directory first, then for a shell command
            (`SHELL_TOOLS`) the checkout of each `cd`/`pushd` target, each once.
        """
        cwd = Path(step.cwd or self.workspace)
        directories: list[Path] = []
        command = step.tool_input.get("command")
        if step.tool_name in SHELL_TOOLS and isinstance(command, str) and command:
            directories = parse_command(command, cwd).directories
        return self.checkouts.step_checkouts(cwd, directories)

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
            final trace pass of an        the pass fails               DENY
            approved plain commit
            (`_finalize_for_commit`)
            last look before the answer   an escalation opened since   the call waits again;
            (`StepGate.last_look`)        the gate looked              ASK instead of PASS/ALLOW
                                                                       only after `HOLD_BUDGET_S`
                                          exception while it waits     ASK

        A subagent's `SubagentHandback` call is not routed: its report is reviewed by the
        subagent-result checker instead (`_handback`, D252).

        Args:
            step: The tool call.

        Returns:
            ALLOW, DENY (with the reason for Claude) or ASK. Any internal error yields ASK, so
            a human decides instead of the call running unchecked.
        """
        if step.tool_name == HANDBACK_TOOL and step.agent_id is not None:
            return await self._handback(step)
        started = time.monotonic()
        self.locks.issue(step)  # its place in its agent's order, before its check (D276)
        try:
            return await self._checked(step, started)
        except BaseException:
            # A cancelled hook or an unexpected error: the call gives up its turn, so that its
            # agent's later calls need not wait for it.
            self.locks.withdraw(step)
            raise

    async def _checked(self, step: Step, started: float) -> GateResult:
        """Implement `pre_tool_use` after the call's ticket was issued.

        Args:
            step: The tool call.
            started: When `pre_tool_use` began (`time.monotonic`), for the lock wait's budget.

        Returns:
            As `pre_tool_use`.
        """
        pending = PendingNarration(self._run_state.claude_outputs.pop(step.agent_id, []))
        step_id: int | None = None
        expected: str | None = None
        try:
            await self._refresh_checkouts()
            own = await anyio.to_thread.run_sync(self.step_checkouts, step)
            step.checkout, step.other_checkouts = self.checkouts.header(own)
            if step.tool_name == "Bash":
                step.sandbox = self.config.claude.sandbox.outside_reason(
                    str(step.tool_input.get("command", "")),
                    step.tool_input.get("dangerouslyDisableSandbox") is True,
                )
            step_id = await self.recorder.add_step(step)
            expected = await self._expect_edit(step)
            decided = await self.gate.decide(step, pending)
        except Exception as exc:
            log.exception("pre_tool_use failed")
            decided = self.gate.failed(step, exc)
        if any(
            outcome.decision.writes_outside_worktree is True
            for checker, outcome in decided.runs
            if not checker.judges_honesty
        ):
            # A command that writes code or configuration outside its checkouts takes the
            # global lock exclusively (D276); the mark holds for a later take in `ask` too.
            self.locks.mark_writes_outside(step)
        await self._mark_at_once(step, decided)
        try:
            self._keep_honesty(step, decided, expected)
            if decided.clean_commit and step.tool_use_id:
                kept = self._run_state.peek(step.tool_use_id).honesty
                if kept is not None:
                    kept.root = self.step_checkouts(step)[0].root
                    kept.head_before = await self._head(kept.root)
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
        traces_note = ""
        if result.decision.approves:
            refused, traces_note = await self._finalize_for_commit(step)
            if refused:
                result = CheckDecision(
                    decision=Decision.DENY, reason=refused, step_summary=result.step_summary
                )
                source, comment = "traces", ""
                self._run_state.peek(step.tool_use_id or "").running = False
                self._run_state.release(step.tool_use_id or "")
        key = step.tool_use_id or ""
        while True:
            if self.gate.last_look(result, source):
                # A hold after a lock wait gets what is left of the hook's time, at most.
                hook_left = HOOK_TIMEOUT_S - LOCK_HOOK_MARGIN_S - (time.monotonic() - started)
                decided.hold_left = max(0.0, min(decided.hold_left, hook_left))
                result, source, kept = await self._held_late(step, decided, result, source)
                comment = comment if kept else ""
            if not result.decision.approves:
                break
            # An approved command or edit takes its locks before it runs (D276), at most for
            # what is left of the PreToolUse hook's time.
            left = HOOK_TIMEOUT_S - LOCK_HOOK_MARGIN_S - (time.monotonic() - started)
            busy = await self.locks.take(step, left)
            if busy:
                result = CheckDecision(
                    decision=Decision.DENY, reason=busy, step_summary=result.step_summary
                )
                source, comment = "locks", ""
                self._run_state.peek(key).running = False
                self._run_state.release(key)
                break
            if decided.route in (Route.COMMAND, Route.CHANGESET):
                # The "before" of the command's write set (D31). It holds its locks now, so what
                # other calls wrote while it waited for them is not counted as its write set.
                # Edits need no snapshot: their content came through the gate. (The changeset
                # route is only reached by a tool that the tool policy sends there.)
                try:
                    await self.write_sets.snapshot_before(step, step_id, result.step_summary)
                except Exception as exc:
                    log.exception("the snapshot before a command failed")
                    result, source, comment = unrecorded(result, exc), "internal", ""
                    self.locks.release(step)
                    self.write_sets.drop_before(key)
                    self._run_state.peek(key).running = False
                    self._run_state.release(key)
                    break
            if not self.gate.last_look(result, source):
                break
            # An escalation came up while the call waited for its locks: a held call holds no
            # lock and has no open write-set window, so it gives both up and is looked at again.
            self.locks.release(step)
            self.write_sets.drop_before(key)
        if result.decision is Decision.DENY:
            self.locks.withdraw(step)
        # No await stands between the last look (above, or inside `_held_late`) and the answer
        # to Claude Code.
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
        notes: list[str] = []
        if comment:
            notes.append(
                CHECKER_COMMENT_TEMPLATE.format(route=decided.route.value, comment=comment)
            )
        if traces_note and result.decision.approves:
            notes.append(traces_note)
        return GateResult(verdict=verdict, reason=result.reason, context="\n\n".join(notes))

    async def _held_late(
        self,
        step: Step,
        decided: Decided,
        result: CheckDecision,
        source: str,
    ) -> tuple[CheckDecision, str, bool]:
        """Hold an approved step because an escalation came up while its approval was recorded.

        The gate looked last before the decision was recorded, and recording takes time. The
        step waits here until no escalation is open or being opened, and is looked at again
        after every wait, because the bookkeeping after a wait takes time too:

            look (`StepGate.last_look`) ── nothing holds ──► return: the approval stands
              │ held
              ▼
            the call no longer counts as running, its "before" snapshot (if any) is dropped
              │
              ▼
            wait (`StepGate.wait_again`) ── `decided.hold_left` used up ──► ESCALATE
              │ released                                                  (source "held")
              ▼
            the call counts as running again ──► look

        The caller then takes the call's locks and its "before" snapshot (`_checked`, D276).

        Side effects: those of the diagram, and the session state follows them;
        `decided.hold_left` is lowered by the time spent waiting. No await stands between the
        look that ends the loop and the return, so the caller answers Claude Code without
        another look.

        Args:
            step: The tool call.
            decided: What `StepGate.decide` returned (route and what is left of the hold).
            result: The recorded approval.
            source: Who decided it.

        Returns:
            (the decision to answer, its source, whether the approval stands). An exception
            in here yields an ESCALATE with source "internal", never an approval.
        """
        key = step.tool_use_id or ""
        try:
            while self.gate.last_look(result, source):
                self._run_state.peek(key).running = False
                self.write_sets.drop_before(key)
                await self._refresh_state()
                replaced, decided.hold_left = await self.gate.wait_again(
                    step, result, source, decided.hold_left
                )
                if replaced is not None:
                    self._run_state.release(key)
                    return replaced, "held", False
                if step.tool_use_id:
                    self._run_state.call(step.tool_use_id).running = True
                await self._refresh_state()
        except Exception as exc:
            log.exception("holding an approved step failed")
            self._run_state.peek(key).running = False
            self.write_sets.drop_before(key)
            self._run_state.release(key)
            return unrecorded(result, exc), "internal", False
        return result, source, True

    async def _record_check(
        self, step: Step, step_id: int | None, pending: PendingNarration, decided: Decided
    ) -> str:
        """Record the decision about a step everywhere it belongs, after it was made.

        Side effects: adds the step to the checkers' context, records the checker runs and the
        decision in the database, emits `check.result`, and counts an approved call as running.
        The "before" snapshot of an approved command (D31) comes later, once the command holds
        its locks (`_checked`, D276).

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
        self._fold_if_due()
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
                **_honesty_result(decided.runs),
            )
        )
        if result.decision.approves and step.tool_use_id:
            self._run_state.call(step.tool_use_id).running = True
        await self._refresh_state()
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

        A plain `git commit` that the human allowed gets the final redaction pass of the
        session traces before it runs (`_finalize_for_commit`); if that pass fails, the
        answer becomes a denial with the reason.

        Returns:
            The human's answer. An escalation that ended without a human decision (stop,
            timeout, interrupt) counts as a denial and has `cancelled` set. If the escalation
            cannot be stored, the step is denied with `DB_UNREACHABLE`, because no human could
            see it.
        """
        # No call holds locks while a person decides about it (D276): a call that Claude Code
        # asks about after the hook allowed it gives them back first, and closes its write-set
        # window (D31) until it may run. (A command whose write set a person reviews after it
        # ran keeps its locks: see `review_write_set`.)
        self.locks.release(step)
        before = self.write_sets.drop_before(step.tool_use_id or "")
        allowed = False
        try:
            answer = (await self._ask(step)).answer
            if not answer.allow:
                return answer
            refused, _ = await self._finalize_for_commit(step)
            if refused:
                return HumanAnswer(allow=False, message=refused, note=answer.note)
            busy = await self.locks.take(step, None)
            if busy:
                return HumanAnswer(allow=False, message=busy, note=answer.note)
            allowed = True
        finally:
            if not allowed:
                # Denied, refused, or this call was cancelled: the call's turn is given up, so
                # that its agent's later calls need not wait for it.
                self.locks.withdraw(step)
        if before is not None:
            await self.write_sets.snapshot_before(step, *before)
        return answer

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
        note = asked.answer.note.strip()
        if note and asked.outcome in ("allowed", "denied"):
            # The note is the user's own text, and Claude acts on it ("not there, write it to
            # X instead"). Without it the checkers see Claude follow an instruction nobody
            # gave and refuse the step the user just asked for.
            self.context.user_input(
                DECISION_NOTE_TEMPLATE.format(
                    decision=asked.outcome, tool=safe_name(step.tool_name), note=note
                )
            )
        if asked.outcome == "answered" and asked.answer.answer.strip():
            # The same holds for the answer to a question: Claude acts on it next.
            self.context.user_input(
                QUESTION_ANSWER_TEMPLATE.format(answer=asked.answer.answer.strip())
            )
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
            self._run_state.tg.start_soon(self._stop_quietly)

    async def _stop_quietly(self) -> None:
        """Stop the session (too many failed store writes, `calu.recorder`); never raises."""
        try:
            await self.stop()
        except Exception:
            log.exception("stopping the session failed")

    def _stop_after_timeout_soon(self) -> None:
        """Start stopping the session after an escalation timeout, in a task of its own.

        For the desk, which calls this inside the tool call that is being held. With
        `auto_resume` the task resumes the conversation after the stop
        (`_stop_after_timeout`).
        """
        tg = self._run_state.tg
        if tg is not None:
            # Not a tracked background task: `stop` waits for those, and this one calls it.
            tg.start_soon(self._stop_after_timeout, tg)

    async def _stop_after_timeout(self, tg: TaskGroup) -> None:
        """Stop the session after an escalation timed out, then resume it if so configured.

        The stop ends the turn and Claude Code's process (D27). With `auto_resume` the same
        conversation is started again at once (`_resume_now`). Never raises: a failure is
        logged.

        Side effects: those of `stop` and of `_resume_now`.

        Args:
            tg: The task group of the run that timed out; it outlives the run and runs the
                resumed one.
        """
        try:
            await self.stop()
        except Exception:
            log.exception("stopping the session after an escalation timeout failed")
            return
        if self.auto_resume:
            await self._resume_now(
                tg,
                "The session was stopped by the escalation timeout: nobody decided the step in "
                "time, so it was denied and the turn ended.",
            )

    async def _resume_after_end(self, tg: TaskGroup, stops: int, why: str) -> None:
        """Resume a conversation whose run Claude Code ended on its own (D282).

        Called by `_run` in a task of its own for a run that ended without `stop`. Nothing is
        resumed if `stop` was called since (a client or the end of `calu run` stopped the
        session in the meantime), if a run already started again, or if this would be the
        `AUTO_RESUME_LIMIT`+1-th automatic resume within `AUTO_RESUME_WINDOW_S`; in that last
        case a `log` error says so and names how to resume by hand. Never raises.

        Side effects: those of `_resume_now`; notes the time of the resume.

        Args:
            tg: The task group of the run that ended; it runs the resumed one.
            stops: The number of `stop` calls when the run began (`Session._stops`).
            why: What ended the run: the runner's error, or that Claude Code ended it.
        """
        claude_id = self.session_id
        if self._stops != stops or self.state not in ("stopped", "failed") or claude_id is None:
            return
        now = anyio.current_time()
        self._auto_resumed = [t for t in self._auto_resumed if now - t < AUTO_RESUME_WINDOW_S]
        if len(self._auto_resumed) >= AUTO_RESUME_LIMIT:
            await self._emit_safely(
                Log(
                    level="error",
                    message=f"Claude Code ended the session again ({one_line(why)}); it was "
                    f"resumed {AUTO_RESUME_LIMIT} times within "
                    f"{AUTO_RESUME_WINDOW_S / 60:.0f} minutes, so it stays stopped. "
                    f"`/start {claude_id}` in a client or `calu run --resume {claude_id}` takes "
                    "the conversation up again.",
                )
            )
            return
        self._auto_resumed.append(now)
        await self._resume_now(tg, f"Claude Code ended the session ({one_line(why)}).")

    async def _resume_now(self, tg: TaskGroup, cause: str) -> None:
        """Start the stopped session's conversation again at once, waiting for the user (D282).

        `start` with the Claude session id and the model and effort of the last run: a new
        `sessions` row that reads the turns of the earlier ones (D65), in state "idle". The
        session counts as stopped by `cause`: nothing is sent to Claude, so it waits for the
        user's next message. Never raises.

        Side effects: those of `start`; emits a `log` warning that names `cause` and says that
        the conversation was resumed, or a `log` error if the start failed, naming how to
        resume by hand. Nothing happens without a Claude session id.

        Args:
            tg: The task group that runs the resumed session.
            cause: Why the session stopped, one or two sentences for the user.
        """
        claude_id = self.session_id
        if claude_id is None:
            return
        try:
            await self.start(tg, resume_id=claude_id)
        except Exception as exc:
            log.exception("resuming the session failed")
            await self._emit_safely(
                Log(
                    level="error",
                    message=f"{cause} It could not be resumed ({one_line(str(exc))}); "
                    f"`/start {claude_id}` in a client or `calu run --resume {claude_id}` takes "
                    "the conversation up again.",
                )
            )
            return
        await self._emit_safely(
            Log(
                level="warning",
                message=f"{cause} The conversation is resumed and waits for you; send a "
                'message (for example "continue") to go on from where it stopped.',
            )
        )

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
        return (await self._reviewed(result))[0]

    async def _reviewed(self, result: SubagentResult) -> tuple[str, bool]:
        """Review a subagent's result or report and remove the step's per-call record after.

        Side effects: as `review_subagent_result`.

        Args:
            result: The subagent's task and its final result or handed-back report.

        Returns:
            (the note for the main agent, as `review_subagent_result` returns it; True if a
            person denied the result after an escalation).
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

    async def _handback(self, step: Step) -> GateResult:
        """Review the report a subagent hands back with `SubagentHandback` (D252).

        Claude Code delivers a background subagent's report through this tool call, and the
        subagent's final result after it is empty (measured with CLI 2.1.327, test plan L3).
        So the report is reviewed here, before the call runs, like a final result
        (`review_subagent_result`): the result checker's note goes to the main agent
        (`GateResult.main_note`), an escalation waits for a person, and a person's denial
        denies the call, so the report is not handed back. The subagent is remembered as
        having handed back (`RunState.handed_back`).

            review                          verdict      main_note
            accepted, with or without note  PASS         the note ("" for none)
            escalated, person allowed       PASS         the note and the decision
            escalated, nobody decided       PASS         the note: do not act on it yet
            escalated, person denied        DENY         the note and the decision; the
                                                         subagent reads `HANDBACK_REFUSED`
            internal error                  ASK          ""

        Args:
            step: The `SubagentHandback` call of a subagent (`agent_id` set); its input
                `message` is the report.

        Returns:
            The verdict for the call.
        """
        agent_id = step.agent_id or ""
        try:
            info = self.runner.agents.get(agent_id) if self.runner is not None else None
            result = SubagentResult(
                agent_id=agent_id,
                description=info.description if info is not None else "",
                prompt=self._run_state.agent_prompts.get(agent_id, ""),
                result=str(step.tool_input.get("message", ""))[:HANDBACK_REPORT_CHARS],
                background=info.background if info is not None else True,
                handback=True,
            )
            note, denied = await self._reviewed(result)
            self._run_state.handed_back.add(agent_id)
        except Exception as exc:
            log.exception("reviewing a handed-back report failed")
            return GateResult(
                Verdict.ASK,
                reason=f"Reviewing the handed-back report failed ({type(exc).__name__}: {exc}).",
            )
        if denied:
            return GateResult(
                Verdict.DENY, reason=HANDBACK_REFUSED.format(note=note).strip(), main_note=note
            )
        return GateResult(Verdict.PASS, main_note=note)

    async def _review_subagent_result(self, result: SubagentResult) -> tuple[str, bool]:
        """Implement `_reviewed`; the caller removes the step's per-call record.

        An empty final result of a subagent whose report was reviewed when it was handed back
        (`RunState.handed_back`) is recorded as passed, with `HANDED_BACK_EMPTY` as the
        reason, without a checker; so is Claude Code's pointer to that report
        (`HANDBACK_POINTER`), the final result of a foreground subagent.

        Side effects: as `review_subagent_result`.

        Args:
            result: The subagent's task and final result.

        Returns:
            (the note for the main agent, as `review_subagent_result` returns it; True if a
            person denied the result after an escalation).
        """
        # The subagent's text after its last tool call is its result, reviewed below.
        self._run_state.claude_outputs.pop(result.agent_id, None)
        if not self.config.check_subagent_results:
            return "", False
        label = result.description or "subagent"
        step = Step(
            tool_name="SubagentResult",
            tool_input={
                "description": label,
                "background": result.background,
                "result": result.result[:4_000],
                **({"handback": True} if result.handback else {}),
            },
            tool_use_id=f"result-{result.agent_id}",
            agent_id=result.agent_id,
            cwd=str(self.workspace),
        )
        step_id = await self.recorder.add_step(step)
        if (
            not result.handback
            and (not result.result.strip() or HANDBACK_POINTER.match(result.result.strip()))
            and (result.agent_id or "") in self._run_state.handed_back
        ):
            await self._pass_handed_back(step, step_id)
            return "", False
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
        self._fold_if_due()
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
        denied = False
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
                return "", False  # the turn or the session is over; there is nobody to tell
            if asked.outcome in ("allowed", "denied"):
                denied = not answer.allow
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
            return "", denied
        return SUBAGENT_NOTE_TEMPLATE.format(label=label, note=note), denied

    async def _pass_handed_back(self, step: Step, step_id: int | None) -> None:
        """Record an empty final result after a reviewed handback as passed without a check.

        Side effects: records the decision and the step's result (`HANDED_BACK_EMPTY`), emits
        `check.result` (route "subagent", PASS, the reason as the summary) and a `log` line.

        Args:
            step: The "SubagentResult" step of the empty result.
            step_id: Its recorded id; None if recording it failed.
        """
        if step_id is not None:
            await self.recorder.attempt(
                "recording a decision",
                self.store.decide_step(
                    step_id,
                    StepDecision(
                        decision="pass", reason=HANDED_BACK_EMPTY, summary=HANDED_BACK_EMPTY
                    ),
                ),
            )
            await self.recorder.attempt(
                "recording a step result", self.store.finish_step(step_id, HANDED_BACK_EMPTY)
            )
        await self._emit_safely(
            CheckResultEvent(
                tool_use_id=step.agent_id,
                agent_id=step.agent_id,
                tool_name="Agent",
                route=Route.SUBAGENT,
                decision=Decision.PASS,
                step_summary=HANDED_BACK_EMPTY,
            )
        )
        await self._emit_safely(
            Log(
                level="info",
                message=f"subagent {step.agent_id}: empty final result passed: {HANDED_BACK_EMPTY}",
            )
        )

    def _reviews_task(self, step: Step) -> bool:
        """Tell the gate whether a step tells a subagent something to review first.

            Agent or Task call                             ──► reviewed
            SendMessage to a subagent of this session
              carrying a text calu relays for the user     ──► not reviewed (the relay is
                                                               consumed)
              any other text                               ──► reviewed
            anything else, or `check_subagent_tasks` off   ──► not reviewed

        Side effect: removes a matched relay from `RunState.relays`.

        Args:
            step: A tool call that the tool policy lets run unchecked.

        Returns:
            True if the subagent checker reviews the step before it runs.
        """
        if not self.config.check_subagent_tasks:
            return False
        if step.tool_name in AGENT_TOOLS:
            return True
        if step.tool_name != "SendMessage" or not self._own_agent(step):
            return False
        message = _message_text(step.tool_input).strip()
        relays = self._run_state.relays
        for index, text in enumerate(relays):
            if text.strip() == message:
                del relays[index]
                return False
        return True

    async def _review_task(self, step: Step) -> CheckDecision:
        """Review what Claude tells a subagent, before the subagent gets it; never raises.

        The subagent checker's verdict becomes the step's decision: ACCEPT -> PASS, ANNOTATE
        -> PASS with the note as the comment to Claude, ESCALATE -> ESCALATE with the note as
        the reason (`auto_escalate.subagents` covers results only, not tasks). The step
        summary is the checker's retelling of the task, headed "Task for subagent <id>" or
        "Message to subagent <id>", so that every later check, of the subagent's steps and of
        the main agent's, knows the task, also while a background subagent still runs.

        Args:
            step: An Agent or Task call, or a `SendMessage` to one of the session's subagents;
                its `narration` holds Claude's narrated thinking and text before the call.

        Returns:
            The decision about the step.
        """
        if step.tool_name in AGENT_TOOLS:
            task = SubagentTask(
                agent_id=step.tool_use_id,
                description=str(step.tool_input.get("description") or ""),
                prompt=str(step.tool_input.get("prompt") or ""),
                kind="start",
                background=runs_in_background(step.tool_input),
                narration=step.narration,
            )
            who = f"Task for subagent {safe_name(step.tool_use_id or 'new')}"
        else:
            target = str(step.tool_input.get("to") or "")
            task = SubagentTask(
                agent_id=target,
                description=target,
                prompt=_message_text(step.tool_input),
                kind="message",
                narration=step.narration,
            )
            who = f"Message to subagent {safe_name(target)}"
        try:
            async with self._checking_scope():
                outcome = await self.result_checker.run_task(
                    task, self.context.check_context(), self.config.context.escalate_on_cut
                )
            review = outcome.review
        except Exception as exc:
            log.exception("reviewing a subagent task failed")
            review = ResultReview(
                verdict=ResultVerdict.ESCALATE,
                note=f"Reviewing the task failed ({type(exc).__name__}: {exc}).",
                summary="Subagent task not reviewed because of an internal error.",
            )
        summary = f"{who}: {review.summary.strip()}"
        if review.verdict is ResultVerdict.ESCALATE:
            return CheckDecision(
                decision=Decision.ESCALATE,
                reason=review.note.strip() or "The subagent checker escalated the task.",
                step_summary=summary,
            )
        comment = review.note.strip() if review.verdict is ResultVerdict.ANNOTATE else ""
        return CheckDecision(
            decision=Decision.PASS, reason="", comment=comment, step_summary=summary
        )

    async def _review(self, result: SubagentResult) -> ResultOutcome:
        """Run the result checker; never raises.

        Args:
            result: The subagent's task and final result.

        Returns:
            The review with the cost of the call; an internal error yields an ESCALATE review.
        """
        try:
            async with self._checking_scope():
                return await self.result_checker.run(
                    result, self.context.check_context(), self.config.context.escalate_on_cut
                )
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
        `turn.result` the session is therefore never left "running" with a finished Claude,
        and the messages calu held during the turn are written to Claude Code then
        (`_release_held`, D283).

        Args:
            event: The event from the runner.

        Raises:
            Exception: Whatever the handling of the event itself raised (recording it in the
                context or the store); the forwarding and the state update have happened then.
        """
        if isinstance(event, PermissionModeChanged):
            # Claude Code's report of its mode; no work of Claude, and repeated reports of the
            # same mode are not passed on.
            self._mode_reports += 1
            asked = self._mode_request
            await self._note_permission_mode(
                event.mode, asked[1] if asked is not None and asked[0] == event.mode else None
            )
            return
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
                    started = self.context.current
                    if started is not None and len(started.user_inputs) == 1 and not started.steps:
                        # The turn begins with this input. It is stored now, without an end,
                        # so that a conversation stopped during its first turn has its input
                        # and is listed; `_finish_turn` stores the same row again, complete.
                        await self.recorder.attempt(
                            "storing the start of a turn",
                            self.store.save_turn(started.model_copy(deep=True)),
                        )
                    queued = await self._take_from_queue(event.text)
                    if queued is not None and queued.to != "main":
                        # Claude received the request to relay the text; the clients are
                        # told what the user wrote and to whom.
                        event = event.model_copy(update={"text": queued.text, "to": queued.to})
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
                    # The task, for the review of a report the subagent hands back (D252).
                    self._run_state.agent_prompts[event.tool_use_id] = str(
                        event.input.get("prompt", "")
                    )
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
                    self._release_commit_hold(event.tool_use_id)
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
                        # After the write set was measured, so that no other call's edit lands
                        # in it (D276).
                        self.locks.call_ended(event.tool_use_id)
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
                    # The turn is over: what the user queued meanwhile goes to Claude now (D283).
                    await self._release_held()
                elif not isinstance(event, (UsageUpdate, Log)):
                    # A report about the account's limits is no work of Claude, nor is a
                    # diagnostic line: the one that follows a switch of the permission mode
                    # between turns left the session "running" without a turn.
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
        narration = ""
        try:
            async with self._checking_scope():
                with anyio.fail_after(NARRATOR_DEADLINE_S):
                    narration = await narrator.narrate(outputs)
        except Exception:
            log.exception("narrating Claude's output failed")
        if self.traces is not None:
            # The trace of a narrated session holds what the checkers read (D232).
            self.traces.narration(outputs, narration)
        return narration

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
        self._fold_if_due()
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

    def _fold_if_due(self) -> None:
        """Start a fold in the background when the context outgrew its limit mid-turn (D272).

        A step can make the unfolded turns and the running turn longer than
        `context.max_recent_chars`; folding then must not wait for the end of the turn. At most
        one such fold runs at a time (`_folding`); `ContextBuilder.fold` checks again whether it
        is still due.
        """
        if self._folding or not self.context.over_limit():
            return
        self._folding = True
        self._spawn(self._fold)

    async def _fold(self) -> None:
        """Fold old turns into the chat summary; never raises (errors are logged)."""
        try:
            await self.context.fold()
        except Exception:
            log.exception("folding the context failed")
        finally:
            self._folding = False

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

        A turn the fork copies that has not ended (it waited on a decision when the fork was
        made, or its session was cut off) is copied with its inputs and its prompt uuid,
        without steps and answer (its row holds none before it ends), and with `ended_at` set
        to now: Claude Code's copy of the transcript holds its prompt, and the fork never
        continues that turn, its own first input starts the next one. Its steps stay with
        the session that ran them (a fork inherits no `steps` rows, D65).

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
        if fork is not None:
            forked_at = datetime.now(UTC)
            turns = [
                turn
                if turn.ended_at is not None
                else turn.model_copy(update={"ended_at": forked_at})
                for turn in turns
            ]
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
            os_user=session_os_user(),
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

        Side effect: while a session trace is written, the event is queued for it first
        (`calu.traces.TraceWriter.feed`, which neither waits nor raises).

        Args:
            event: The event.

        Raises:
            Exception: Whatever the client sink raises (e.g. a closed connection).
        """
        resolved_by = ""
        if isinstance(event, EscalationResolved):
            resolved_by = self.desk.resolved_by.pop(event.escalation_id, "")
        if self._trace_stale and isinstance(event, TRACE_BLOCK_EVENTS):
            # The `traces` settings changed while the session was idle (`_follow_traces`).
            self._switch_traces()
        if self.traces is not None:
            self.traces.feed(event, resolved_by)
        await self._sink(event)

    # ------------------------------------------------------------------ session traces

    def add_trace_secret(self, value: str, kind: str) -> None:
        """Name a value that a session trace must never hold (agent/records/PLAN.md D232).

        Args:
            value: The secret, e.g. the client token of this session.
            kind: The type its placeholder names, e.g. "client token".
        """
        self._trace_secrets.add(value, kind)

    def _guard_traces(self) -> None:
        """Tell the rules, the honesty state and the write sets where the traces are.

        The rules also get the registry of checkouts here (D251), because a settings reload
        replaces them.

        While `traces.enabled` is on, the trace directory is calu's own output inside the
        project: a tool call that would write there waits for a person, the checkers' code
        tools do not read it, and its files are left out of the write sets and of the
        honesty state. With tracing off, the directory is an ordinary directory.
        """
        cfg = self.config.traces
        directory = cfg.directory if cfg.enabled else None
        self.rules.trace_directory = None if directory is None else self.workspace / directory
        self.rules.checkouts = self.checkouts  # worktrees are judged as checkouts (D251)
        self._trace_dir = directory
        # The human diffs (D253) are calu's own output too, while they are recorded.
        diffs = self.config.human_diffs
        self._human_diffs_dir = diffs.directory if diffs.enabled else None
        self.rules.human_diffs_directory = (
            None if self._human_diffs_dir is None else self.workspace / self._human_diffs_dir
        )
        self._exclude_from_states()
        own = [path for path in (directory, self._human_diffs_dir) if path is not None]
        self.write_sets.excluded = tuple(own)

    def _trace_settings(self) -> tuple[object, ...]:
        """Return what decides which trace file is written and how.

        Returns:
            A tuple that is equal for two moments exactly if a trace started at either would
            hold the same text in the same place: the settings of the `traces` section that
            decide where the file is and what a block holds (empty while tracing is off)
            and whether a narrator exists.
        """
        cfg = self.config.traces
        if not cfg.enabled:
            return ("", self.narrator is not None)
        # `redactor_timeout_s` and `queue_blocks` bound the writer's work and change nothing
        # of what a block holds: a new value counts from the next trace file on.
        shown = cfg.model_dump(
            exclude={"redactor_timeout_s", "queue_blocks", "final_redaction", "final_timeout_s"}
        )
        return (json.dumps(shown, sort_keys=True), self.narrator is not None)

    def _start_traces(
        self,
        tg: TaskGroup,
        resume_id: str | None = None,
        forked: bool = False,
        claude_session: str | None = None,
        note: str = "",
        after: TraceWriter | None = None,
    ) -> None:
        """Start the trace file of this run if `traces.enabled` is on; never raises.

        Side effects: sets `self.traces` and starts the writer's task in `tg`, which creates
        the file `<traces.directory>/<UTC date>/<user>/session_NNN.md` (in the default
        `traces.layout` and `traces.file_names`) with this process's
        file access (as root, that of the session's user, D206). A failure is logged, and
        the session runs without a trace.

        Args:
            tg: The task group of the run.
            resume_id: The Claude session this run resumes or forks, or None; the header then
                names that conversation's trace file if one is found.
            forked: True if `resume_id` is forked, not resumed.
            claude_session: The Claude session id of this run if it is known already.
            note: A sentence for the header, e.g. that the trace starts in mid-session.
            after: The writer this one follows in the same run, or None; the new file is
                created after that writer's file, so that it gets the higher number, and its
                header names that file.
        """
        self._trace_as = self._trace_settings()
        self._trace_stale = False
        left, self.traces = self.traces, None
        if left is not None:
            # A run that ended while this one started did not get to close its trace.
            tg.start_soon(left.close, "The session ended.")
        cfg = self.config.traces
        if not cfg.enabled:
            return
        try:
            names = {
                *PROVIDER_KEYS,
                *(name for names in PROVIDER_KEYS_BY_PREFIX.values() for name in names),
                *self._secret_env,
                *self.config.hidden_env,
            }
            self._trace_secrets.update(Secrets.from_environment(names))
            narrated = cfg.content == "narrated" and self.narrator is not None
            redactor: Redactor | None = None
            redactor_name: str | None = None
            if cfg.redact:
                model = self._models.get("trace-redactor") or (
                    cfg.redactor_model.strip() or default_model(self.config.claude.use_bedrock)
                )
                redactor_name = model_name(model)
                redactor = Redactor(
                    redaction_instructions(cfg.redactor_instructions),
                    resolve_model(model),
                    timeout_s=cfg.redactor_timeout_s,
                )
            started = datetime.now(UTC)
            user = session_os_user()
            if resume_id is not None and not forked:
                claude_session = resume_id
            header = Header(
                calu_session=self.store.session_id,
                claude_session=claude_session,
                project=self.project or self.workspace.name,
                started=started if cfg.header_times else None,
                user=user if cfg.header_user else None,
                claude_model=self.config.claude.model or "",
                calu_version=__version__,
                content="narrated" if narrated else "plain",
                content_note=(
                    "`traces.content` is `narrated`, but `context.narrate` is off, so there "
                    "is no narrator"
                    if cfg.content == "narrated" and not narrated
                    else ""
                ),
                redactor=redactor_name,
                note=note,
            )
            writer = TraceWriter(
                self.workspace,
                session_directory(self.workspace, cfg.directory, started, user, cfg.layout),
                header,
                TraceRenderer(narrated, claude_session, turn_numbers=cfg.turn_numbers),
                self._trace_secrets,
                redactor=redactor,
                narrate=self._trace_narrate,
                report=self._trace_report,
                continues=None
                if resume_id is None
                else (resume_id, "forked" if forked else "resumed"),
                queue_blocks=cfg.queue_blocks,
                after=after,
                trace_root=self.workspace / cfg.directory,
                random_name=cfg.file_names == "random",
            )
            writer.start(tg)
            self.traces = writer
            self._trace_closed = anyio.Event()
        except Exception:
            log.exception("starting the session trace failed; the session runs without one")

    async def _trace_narrate(self, outputs: list[ClaudeOutput]) -> str:
        """Narrate text for the trace that the session did not narrate for a checker.

        Args:
            outputs: Claude's thinking and text of one block of the trace.

        Returns:
            The narration, or "" without a narrator.

        Raises:
            Exception: Whatever the narrator raises, and `TimeoutError` after
                `NARRATOR_DEADLINE_S`; the trace writer catches both.
        """
        narrator = self.narrator
        if narrator is None:
            return ""
        with anyio.fail_after(NARRATOR_DEADLINE_S):
            return await narrator.narrate(outputs)

    def _known_secrets(self) -> Secrets:
        """Collect the values a trace never holds, from the environment and the session.

        Side effects: adds the environment's secret values to `_trace_secrets`.

        Returns:
            `_trace_secrets`.
        """
        names = {
            *PROVIDER_KEYS,
            *(name for names in PROVIDER_KEYS_BY_PREFIX.values() for name in names),
            *self._secret_env,
            *self.config.hidden_env,
        }
        self._trace_secrets.update(Secrets.from_environment(names))
        return self._trace_secrets

    async def _finalize_for_commit(self, step: Step) -> tuple[str, str]:
        """Run the final redaction pass of the trace files before a commit of Claude's (D246).

        For a plain `git commit` (`classify_commit`) while `traces.final_redaction` is on:
        every trace file below `traces.directory` that differs from HEAD gets its new part
        redacted as a whole (`calu.traces.finalize`), is written back and is staged, so the
        commit records the finalized text. Before the pass reads the file, it waits up to
        `traces.redactor_timeout_s` for the blocks queued so far to be written
        (`TraceWriter.settle`, D262), so that the commit also records the block of its own
        tool call. The session's trace writer is held from here
        until the commit's tool result arrives (`TraceWriter.hold`), so that the file on
        disk is what the commit records also for `git commit -a`. Any other step, a
        directory outside a git work tree and a trace directory outside the repository do
        nothing. So does a commit in a git worktree (D251): the live trace is written only in
        the main checkout's `traces.directory`, which lies outside the worktree's working
        tree, so such a commit records no trace and needs no pass.

        Side effects: model calls, rewritten and staged trace files, a `log` event with
        what was done or what failed. Never raises.

        Args:
            step: The approved tool call.

        Returns:
            (refusal, note). `refusal` is "" if the commit may run; otherwise the reason for
            Claude why it is refused: the pass failed for a file, and committing it
            unfinalized is not allowed. `note` is `TRACES_STAGED_NOTE` naming the files when
            the pass staged trace files for the commit, else "": a note for Claude, so that it
            does not take those files for a mistake.
        """
        cfg = self.config.traces
        if not cfg.final_redaction or step.tool_name != "Bash":
            return "", ""
        cwd = Path(step.cwd or self.workspace)
        try:
            line = classify_commit(str(step.tool_input.get("command") or ""), cwd)
            if line.kind is not CommitKind.PLAIN:
                return "", ""
            repo = await anyio.to_thread.run_sync(repository_of, cwd)
            if repo is None:
                return "", ""
            directory = (self.workspace / cfg.directory).resolve()
            relative = directory.relative_to(repo.resolve()).as_posix()
        except Exception:
            return "", ""
        writer = self.traces
        if writer is not None and step.tool_use_id:
            # The blocks queued before the commit (its own tool call's block among them) may
            # still be in the per-block redaction; the pass reads the file on disk (D262).
            # No `await` between `settle` and `hold`, so that no block is appended in between.
            await writer.settle(cfg.redactor_timeout_s)
            writer.hold(cfg.final_timeout_s + COMMIT_HOLD_S)
            self._commit_hold = step.tool_use_id
        try:
            found = await anyio.to_thread.run_sync(plans, repo, "worktree", relative)
            if not found:
                return "", ""
            redactor: Redactor | None = None
            if cfg.redact:
                model = self._models.get("trace-redactor") or (
                    cfg.redactor_model.strip() or default_model(self.config.claude.use_bedrock)
                )
                redactor = final_redactor(
                    final_redaction_instructions(cfg.redactor_instructions),
                    resolve_model(model),
                    cfg.final_timeout_s,
                )
            outcome = await finalize(
                repo, "worktree", redactor, self._known_secrets(), staging=True, found=found
            )
            problem = "; ".join(f"{path}: {why}" for path, why in outcome.failures)
        except Exception as exc:
            log.exception("the final redaction of the session traces failed")
            problem = f"{type(exc).__name__}: {exc}"
            outcome = None
        if not problem:
            note = ""
            if outcome is not None and outcome.results:
                summary = "; ".join(outcome.summary().splitlines())
                await self._emit_safely(
                    Log(level="info", message=f"session traces finalized for the commit: {summary}")
                )
                staged = [f"`{result.path}`" for result in outcome.results if result.staged]
                if staged:
                    note = TRACES_STAGED_NOTE.format(paths=", ".join(staged))
            return "", note
        self._release_commit_hold(step.tool_use_id)
        await self._emit_safely(
            Log(
                level="warning",
                message=(
                    f"the final redaction of the session traces failed ({problem}); the commit "
                    "was refused"
                ),
            )
        )
        return FINAL_PASS_FAILED.format(problem=problem), ""

    def _release_commit_hold(self, tool_use_id: str | None) -> None:
        """Let the trace writer append again once the commit it was held for is over.

        Args:
            tool_use_id: The call that ended (its tool result) or was refused.
        """
        if tool_use_id and tool_use_id == self._commit_hold:
            self._commit_hold = None
            if self.traces is not None:
                self.traces.release()

    async def _trace_report(self, level: Literal["info", "warning"], message: str) -> None:
        """Pass a message of the trace writer to the clients' log; never raises.

        Args:
            level: "info" for the file's path, "warning" for a problem.
            message: The sentence.
        """
        await self._emit_safely(Log(level=level, message=message))

    async def _end_traces(self, note: str) -> None:
        """Write what the trace still holds and end it; never raises.

        Args:
            note: The last line of the file.
        """
        writer, self.traces = self.traces, None
        try:
            if writer is not None:
                await writer.close(note)
        except Exception:
            log.exception("ending the session trace failed")
        finally:
            self._trace_closed.set()

    def _follow_traces(self, before: tuple[object, ...]) -> None:
        """Bring the trace in line with a reloaded configuration.

            what the trace is written with    session              afterwards
            (`_trace_settings`)
            unchanged                         any                  unchanged
            changed, tracing now off          any                  the file is ended
            changed, tracing on               Claude is working    the file is ended, a new one
                                                                   is started (`_switch_traces`)
            changed, tracing on               idle                 nothing yet: the switch
                                                                   happens with the next event
                                                                   that adds a block (`emit`)

        An idle session writes nothing, so a file started for it now would hold a header
        and no step; one change of a `traces` setting would add such a file to the project
        for every session that runs. A setting that is changed back before the session's
        next block leaves its file as it is.

        Args:
            before: `_trace_settings` of the configuration that was replaced; used only if
                no trace was started in this session object yet.
        """
        self._guard_traces()
        written_with = before if self._trace_as is None else self._trace_as
        self._trace_stale = self._trace_settings() != written_with
        if not self._trace_stale:
            return
        if self.config.traces.enabled and not self._run_state.busy:
            return
        self._switch_traces()

    def _switch_traces(self) -> None:
        """End the running trace file and, if tracing is on, start a new one.

        Side effects: the old file gets a last line and is closed in the background (and
        removed if no event added a block to it); a new writer is started in the run's task
        group, whose header says that it starts in the middle of a session and names the
        old file.
        """
        self._trace_stale = False
        old, self.traces = self.traces, None
        tg = self._run_state.tg
        note = (
            "this trace starts in the middle of a session, because the `traces` settings "
            "were switched on or changed while it ran"
        )
        if old is not None:
            self._spawn(old.close, "The trace ends here: the `traces` settings changed.")
            self._trace_closed.set()
        if tg is not None and not self._run_state.run_done.is_set():
            self._start_traces(tg, claude_session=self.session_id, note=note, after=old)
        else:
            self._trace_as = self._trace_settings()

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
            state = await anyio.to_thread.run_sync(self._state_for_path, path)
            return await anyio.to_thread.run_sync(
                state.expect_edit, path, step.tool_name, step.tool_input
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


def _add_changes(total: MarkChanges, part: MarkChanges) -> None:
    """Add what one checkout's honesty state changed to the changes of a whole step (D251).

    Args:
        total: The changes so far; changed in place: opened, closed, ignored and withdrawn
            are extended (each withdrawn content once). `dropped` and `review_required` are
            left to the caller.
        part: What one state's call changed.
    """
    total.opened += part.opened
    total.closed += part.closed
    total.ignored += part.ignored
    total.withdrawn += [one for one in part.withdrawn if one not in total.withdrawn]


def _some(items: Sequence[str]) -> str:
    """Join the first five of a list of names for a sentence a person reads.

    Args:
        items: The names, each cut to `NOTE_PATH_CHARS` characters.

    Returns:
        E.g. "a.py; b.py; and 3 more" for five names and three further ones.
    """
    rest = f"; and {len(items) - 5} more" if len(items) > 5 else ""
    return "; ".join(shown(one, NOTE_PATH_CHARS) for one in items[:5]) + rest


def _honesty_result(runs: Sequence[tuple[Checker, CheckOutcome]]) -> dict[str, Any]:
    """Return the honesty checker's own result of a step for the `check.result` event.

    Args:
        runs: The checkers that judged the step, with their outcomes.

    Returns:
        {} if no honesty checker answered. Otherwise the fields `honesty` (one
        `HonestyFileStatus` per file of its answer) and `honesty_escalation` (its reason if it
        asked for a person, else "") of `calu.protocol.messages.CheckResult`.
    """
    for _checker, run in runs:
        answer = run.honesty
        if answer is None:
            continue
        return {
            "honesty": [
                HonestyFileStatus(
                    path=entry.path,
                    verdict=entry.verdict,
                    reason=entry.reason,
                    units=[
                        HonestyUnitStatus(symbol=u.symbol, honest=u.honest, reason=u.reason)
                        for u in entry.units
                    ],
                )
                for entry in answer.files
            ],
            "honesty_escalation": (
                (answer.escalate_reason or "The honesty checker asks for a human.")
                if answer.escalate
                else ""
            ),
        }
    return {}


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
