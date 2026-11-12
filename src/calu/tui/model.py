"""The state of the full-screen client: a reducer over the events of the protocol.

This module imports nothing from Textual. `SessionModel.apply` takes one protocol event and
returns a `Change` that names what it altered; the widgets render the parts a `Change` names.

    protocol event ──► SessionModel.apply ──► Change ──► widgets

What the model holds:

* `blocks`: the transcript, oldest first. A block is a user message, a text of Claude, a
  thinking text, one tool call (`StepBlock`: the call, every check of it, its result, its
  files and diffs, joined by `tool_use_id`), the end of a turn, or a note of the client.
* `agents`: the main agent and every subagent, each with a state derived from its own events.
  `session.state` is one value for the whole session, so the state per agent is worked out here.
* `decisions`: the open escalations and questions, oldest first.
* the message queue, the honesty marks, the supervision switches, cost and turn count, and the
  state of the connection.

Times are seconds on the clock of the client (`time` of `apply`). A replayed event has no time
(`None`): the protocol client passes on method and params only, so a duration or a waiting time
is shown only for what happened while this client was attached.
"""

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from calu.acp.modes import CUSTOM, LADDER, mode_of
from calu.config import AutoEscalate
from calu.protocol import messages as m
from calu.protocol.answers import compose_answer, question_title

MAIN = "main"
"""Tab key and agent key of the main agent (its `agent_id` in the protocol is None)."""

ALL = "all"
"""Tab key of the view that shows the blocks of every agent in the order they arrived."""

AGENT_TOOLS = frozenset({"Agent", "Task"})
"""Tool names that start a subagent; the id of such a call is the subagent's `agent_id`."""

LOG_LIMIT = 500
"""How many `log` events the model keeps for the log view (the oldest are dropped)."""

EVENT_TYPES: dict[str, type[m.Event]] = {event.method: event for event in m.EVENTS}
"""Event class per method, to validate the params of a received event."""

HANDLED: frozenset[str] = frozenset(
    {
        "session.state",
        "assistant.text",
        "assistant.delta",
        "assistant.thinking",
        "tool.use",
        "tool.result",
        "tool.files",
        "chat.queue",
        "user.input",
        "turn.result",
        "agents.update",
        "check.result",
        "escalation.request",
        "escalation.resolved",
        "edit.diff",
        "auto_escalate.changed",
        "honesty.update",
        "usage.update",
        "log",
    }
)
"""Every event method `SessionModel.apply` handles. A test compares it with the protocol's
`EVENTS`, so a new event cannot reach this client and show nothing."""

WORKING_STATES = frozenset({"running", "checking", "tool_call", "escalated"})
"""Session states in which Claude works on a turn (an interrupt has something to end)."""


@dataclass
class Check:
    """One `check.result` of a step: the route and, if a checker ran, its decision."""

    route: str
    decision: str
    """PASS, ALLOW, DENY or ESCALATE."""
    reason: str = ""
    summary: str = ""
    """What the checker says the step does (`step_summary`)."""
    comment: str = ""
    """A remark of the checker that Claude was told."""


@dataclass
class Diff:
    """The content of one file before and after an edit (`edit.diff`)."""

    path: str
    before: str
    after: str
    start_line: int | None = None
    end_line: int | None = None

    def counts(self) -> tuple[int, int]:
        """Count the changed lines.

        Returns:
            `(added, removed)`: the number of lines only in `after` and only in `before`.
        """
        added = removed = 0
        matcher = difflib.SequenceMatcher(
            None, self.before.splitlines(), self.after.splitlines(), autojunk=False
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                removed += i2 - i1
                added += j2 - j1
        return added, removed


@dataclass(kw_only=True)
class Block:
    """One entry of the transcript. `id` is its index in `SessionModel.blocks`."""

    id: int
    agent_id: str | None = None
    """None for the main agent."""
    turn: int = 0
    """How many turns of the main agent had ended when the block was created."""
    time: float | None = None
    """Client clock when the block was created; None for a replayed event."""


@dataclass(kw_only=True)
class UserBlock(Block):
    """A message Claude has taken in (`user.input`)."""

    text: str
    source: str = "user"
    """`user`, `system` (put in by calu or Claude Code) or `cross_session`."""
    relayed_to: str | None = None
    """Label of the subagent the main agent was asked to relay this message to."""


@dataclass(kw_only=True)
class TextBlock(Block):
    """A text of Claude; `streaming` while only fragments of it have arrived."""

    text: str
    message_id: str | None = None
    streaming: bool = False


@dataclass(kw_only=True)
class ThinkingBlock(Block):
    """A thinking text of Claude; `streaming` while only fragments of it have arrived."""

    text: str
    message_id: str | None = None
    streaming: bool = False


@dataclass(kw_only=True)
class StepBlock(Block):
    """One tool call with everything the session reported about it."""

    tool_use_id: str | None
    tool: str
    input: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    result: str | None = None
    """The tool's result text (possibly shortened by the session); None while it runs."""
    is_error: bool = False
    files: list[str] = field(default_factory=list)
    diffs: list[Diff] = field(default_factory=list)
    ended: float | None = None
    """Client clock when the result arrived; None for a replayed result."""
    escalation_id: str | None = None
    waiting: str = ""
    """State of the step's escalation: "" (never escalated), "open", "allowed", "denied",
    "cancelled", "answered" (a question) or "resolved" (ended while this client was away)."""
    opened_at: float | None = None
    """Client clock when the escalation arrived; None if it was replayed."""
    note: str = ""
    """The note or answer that went to Claude with the decision."""

    @property
    def verdict(self) -> str:
        """The word shown in the step's badge.

        Returns:
            For an escalated step its state: ESCALATE while it is open, then ALLOWED, DENIED,
            CANCELLED, ANSWERED or RESOLVED. Otherwise the strictest decision of the step's
            checks (DENY, ESCALATE, ALLOW, PASS), or "" for a step no check was reported for.
        """
        if self.waiting == "open":
            return "ESCALATE"
        if self.waiting:
            return self.waiting.upper()
        decisions = {check.decision.upper() for check in self.checks}
        for word in ("DENY", "ESCALATE", "ALLOW", "PASS"):
            if word in decisions:
                return word
        return ""

    @property
    def summary(self) -> str:
        """What the step does, in the words of the checker that judged it.

        Returns:
            The newest non-empty `step_summary` of the step's checks, or "".
        """
        for check in reversed(self.checks):
            if check.summary:
                return check.summary
        return ""

    @property
    def duration(self) -> float | None:
        """How long the step took, as this client measured it.

        Returns:
            Seconds from the `tool.use` to the `tool.result`, or None while the step runs or
            when one of the two events was replayed.
        """
        if self.time is None or self.ended is None:
            return None
        return max(0.0, self.ended - self.time)


@dataclass(kw_only=True)
class TurnEndBlock(Block):
    """The end of a turn of the main agent (`turn.result`)."""

    number: int
    """Number of the turn among the turn ends this client has seen, counted from 1."""
    cost: float | None = None
    """Cost of the Claude session so far, as Claude Code reports it."""
    cost_delta: float | None = None
    """Cost of this turn: the difference to the turn before; None if it cannot be told."""
    duration: float | None = None
    background_tasks: int = 0
    is_error: bool = False
    subtype: str = "success"
    result: str = ""


@dataclass(kw_only=True)
class JoinBlock(Block):
    """A line of the client itself, e.g. where the replayed events begin."""

    text: str


@dataclass
class Option:
    """One option of a question of the agent."""

    label: str
    description: str = ""


@dataclass
class Question:
    """One question of an `AskUserQuestion` call, validated for display."""

    question: str
    header: str
    options: list[Option] = field(default_factory=list)
    multi: bool = False
    """True if several options may be chosen (`multiSelect`)."""


def parse_questions(raw: list[dict[str, Any]]) -> list[Question]:
    """Turn the `questions` of an `escalation.request` into `Question` objects.

    The list comes from Claude's tool input, so nothing about its shape is guaranteed. An entry
    that is not an object with a question text, or whose options are not a list of objects with
    a `label`, makes the whole set fall back to one question without options that shows the raw
    text; the user then answers in their own words.

    Args:
        raw: The `questions` field as received.

    Returns:
        One `Question` per entry, or a single free-text question for a malformed set. An empty
        list for an empty `raw`.
    """
    parsed: list[Question] = []
    for index, entry in enumerate(raw):
        text = entry.get("question") if isinstance(entry, dict) else None
        options = entry.get("options", []) if isinstance(entry, dict) else None
        well_formed = (
            isinstance(text, str)
            and isinstance(options, list)
            and all(isinstance(o, dict) and isinstance(o.get("label"), str) for o in options)
        )
        if not well_formed:
            return [Question(question=str(raw), header="Answer")]
        assert isinstance(text, str) and isinstance(options, list)
        parsed.append(
            Question(
                question=text,
                header=question_title(entry, index),
                options=[
                    Option(str(option["label"]), str(option.get("description") or ""))
                    for option in options
                ],
                multi=bool(entry.get("multiSelect")),
            )
        )
    return parsed


@dataclass
class Decision:
    """An open escalation or question, with what the user has prepared as the answer."""

    escalation_id: str
    tool_use_id: str | None
    agent_id: str | None
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    rendered: str
    questions: list[Question] = field(default_factory=list)
    raw_questions: list[dict[str, Any]] = field(default_factory=list)
    opened_at: float | None = None
    """Client clock when the request arrived; None if it was open before this client attached
    or was replayed."""
    by_rule: bool = False
    """True if a deny rule stopped the step before any checker ran."""
    own: bool = False
    """True once this client has sent its decision; the `escalation.resolved` that follows is
    then not reported as decided elsewhere."""
    note: str = ""
    """The note the user wrote for Claude, sent with the next allow or deny."""
    armed: bool = False
    """True after the first allow key on a step a deny rule stopped; the second one allows."""
    error: str = ""
    """What the session answered when it refused the decision."""
    current: int = 0
    """Index of the question the card shows."""
    picks: list[set[int]] = field(default_factory=list)
    """Per question the indexes of the chosen options."""
    free: list[str] = field(default_factory=list)
    """Per question the user's own answer text ("" for none)."""

    @property
    def is_question(self) -> bool:
        """Whether the item is a question of the agent rather than a step to allow or deny.

        Returns:
            True if the request carried questions.
        """
        return bool(self.questions)

    def answers(self) -> list[list[str]]:
        """Return what the user has answered so far.

        Returns:
            Per question the chosen option labels in the order of the options, followed by the
            user's own text if there is one. An empty list for an unanswered question.
        """
        found: list[list[str]] = []
        for index, question in enumerate(self.questions):
            chosen = [
                question.options[i].label
                for i in sorted(self.picks[index])
                if i < len(question.options)
            ]
            if self.free[index].strip():
                chosen.append(self.free[index].strip())
            found.append(chosen)
        return found

    def answered(self) -> int:
        """Count the answered questions.

        Returns:
            The number of questions with at least one chosen option or an own text.
        """
        return sum(1 for chosen in self.answers() if chosen)

    def complete(self) -> bool:
        """Tell whether the answer can be sent.

        Returns:
            True if the item is a question and every question of it has an answer.
        """
        return self.is_question and self.answered() == len(self.questions)

    def answer_text(self) -> str:
        """Compose the one answer text the protocol takes (`calu.protocol.answers`).

        Returns:
            The text for `escalation.decide {answer}`.
        """
        titles = [{"header": question.header} for question in self.questions]
        return compose_answer(titles, self.answers())


@dataclass
class Agent:
    """The main agent or a subagent, with a state derived from its own events."""

    key: str
    """`MAIN` for the main agent, else the `agent_id`."""
    description: str = ""
    subagent_type: str | None = None
    background: bool = False
    status: str = "running"
    """The session's word for a subagent: running, completed, failed or stopped."""
    activity: str = ""
    """What the agent does now: "running", "checking", "tool call", "waits for you", or "" if
    no event of it was seen since this client attached."""
    last_tool: str = ""
    steps: int = 0
    unread: bool = False
    """True if the agent produced something while another tab was selected."""
    label: str = ""
    """Short name for tabs and lists."""

    @property
    def running(self) -> bool:
        """Whether the session reports the agent as running.

        Returns:
            True for the main agent and for a subagent whose status is "running".
        """
        return self.key == MAIN or self.status == "running"

    @property
    def takes_messages(self) -> bool:
        """Whether `chat.send {to}` accepts this agent as the target.

        Returns:
            True for the main agent and for a running background subagent.
        """
        return self.key == MAIN or (self.background and self.status == "running")


@dataclass
class Queued:
    """A chat message the session holds until Claude takes it in (`chat.queue`)."""

    id: str
    text: str
    delivery: str
    to: str = MAIN


@dataclass
class Mark:
    """One open entry of the honesty register (`honesty.update`)."""

    id: int
    path: str
    symbol: str
    reason: str
    git_branch: str | None = None


@dataclass
class Limit:
    """The state of one usage limit of the Claude Code account (`usage.update`)."""

    type: str
    """five_hour, seven_day, seven_day_opus, seven_day_sonnet, overage or ""."""
    status: str = "allowed"
    """allowed, allowed_warning or rejected."""
    utilization: float | None = None
    """Used part, 0.0 to 1.0; None if Claude Code did not report it."""
    resets_at: datetime | None = None


@dataclass
class ContextFill:
    """How full the context window of the main agent is (`usage.get`)."""

    tokens: int
    max_tokens: int
    percent: float
    model: str = ""
    """The resolved model id the numbers were counted for."""


@dataclass
class Usage:
    """What the session reports about cost and use.

    `context` is None until a `usage.get` answered with it; `limits` is empty for an account
    without usage limits and for a session that does not report them.
    """

    cost: float | None = None
    turns: int = 0
    background_tasks: int = 0
    context: ContextFill | None = None
    limits: list[Limit] = field(default_factory=list)


@dataclass
class SideExchange:
    """One side question of this client and what came back (`side.ask`).

    A side question is answered beside the running work and is not part of the conversation;
    the model keeps the exchanges so that the side panel can show them and a follow-up
    question can carry them as history.
    """

    question: str
    answer: str = ""
    error: str = ""
    """Why no answer came, if the session refused or failed."""
    pending: bool = True
    """True until the answer or the error arrived."""


@dataclass
class ConnectionState:
    """Where the connection to the session stands."""

    status: str = "connecting"
    """connecting, attached, reconnecting or detached."""
    attempt: int = 0
    limit: int = 0
    reason: str = ""


@dataclass
class Notice:
    """Something that happened elsewhere and is shown as a short message."""

    text: str
    severity: str = "information"
    """information, warning or error."""


@dataclass
class LogLine:
    """One `log` event, or a line the client itself adds to the log view."""

    level: str
    message: str


@dataclass
class Change:
    """What one or several events altered, so that the view updates only that."""

    blocks_added: list[int] = field(default_factory=list)
    blocks_updated: set[int] = field(default_factory=set)
    agents: bool = False
    decisions: bool = False
    queue: bool = False
    honesty: bool = False
    status: bool = False
    side: bool = False
    """True if the side questions changed."""
    rebuilt: bool = False
    """True if the model was reset; every view renders from scratch."""
    bell: bool = False
    notices: list[Notice] = field(default_factory=list)

    def merge(self, other: Change) -> None:
        """Add what another change altered to this one.

        Args:
            other: The later change; it is not modified.
        """
        self.blocks_added.extend(other.blocks_added)
        self.blocks_updated |= other.blocks_updated
        self.agents |= other.agents
        self.decisions |= other.decisions
        self.queue |= other.queue
        self.honesty |= other.honesty
        self.status |= other.status
        self.side |= other.side
        self.rebuilt |= other.rebuilt
        self.bell |= other.bell
        self.notices.extend(other.notices)

    def __bool__(self) -> bool:
        """Tell whether anything changed.

        Returns:
            False for a change that names nothing.
        """
        return bool(
            self.blocks_added
            or self.blocks_updated
            or self.agents
            or self.decisions
            or self.queue
            or self.honesty
            or self.status
            or self.side
            or self.rebuilt
            or self.bell
            or self.notices
        )


def _short(text: str, limit: int) -> str:
    """Put a text on one line and cut it.

    Args:
        text: The text.
        limit: Maximum number of characters.

    Returns:
        The text with runs of whitespace as one space, cut to `limit` characters.
    """
    joined = " ".join(text.split())
    return joined if len(joined) <= limit else joined[: max(1, limit - 1)] + "…"


class SessionModel:
    """Everything the client knows about the session, built from a snapshot and events.

    Invariants: `blocks[i].id == i`; a `tool_use_id` names at most one `StepBlock`; `agents`
    always holds the main agent first; every entry of `decisions` is an open escalation.
    """

    def __init__(self) -> None:
        """Create an empty model: no session data, connection state "connecting"."""
        self.connection = ConnectionState()
        self.selected_tab = MAIN
        """The tab the user looks at; events of other agents set their `unread` flag."""
        self.logs: list[LogLine] = []
        self.side: list[SideExchange] = []
        """The side questions of this client, oldest first; they survive a rebuild."""
        self.reset()

    def reset(self) -> None:
        """Forget all session data; keep the connection state, the selected tab and the log.

        Called before the model is rebuilt from a new snapshot, when the session could not
        replay the events this client missed.
        """
        self.state = "starting"
        self.state_since: float | None = None
        self.workspace = ""
        self.project: str | None = None
        self.model_name: str | None = None
        self.effort: str | None = None
        self.claude_session_id: str | None = None
        self.calu_session_id: str | None = None
        self.auto_escalate = AutoEscalate()
        self.honesty_mode = "off"
        self.marks: list[Mark] = []
        self.queue: list[Queued] = []
        self.usage = Usage()
        self.blocks: list[Block] = []
        self.agents: dict[str, Agent] = {MAIN: Agent(MAIN, label=MAIN)}
        self.decisions: dict[str, Decision] = {}
        self.turn = 0
        self.turn_started: float | None = None
        self.own_level_change = False
        """Set by the client before it sends `auto_escalate.set`, so that the event which
        follows is not reported as a change made elsewhere."""
        self.saw_thinking = False
        self._steps: dict[str, StepBlock] = {}
        self._by_agent: dict[str, list[int]] = {MAIN: []}
        self._pending_diffs: dict[str, list[Diff]] = {}
        self._streams: dict[tuple[str, str, bool], Block] = {}
        self._left_queue: list[Queued] = []
        self._previous_cost: float | None = None

    # ------------------------------------------------------------------ reading

    def blocks_of(self, tab: str) -> list[Block]:
        """Return the blocks a tab shows, oldest first.

        Args:
            tab: `MAIN`, `ALL` or the `agent_id` of a subagent.

        Returns:
            The blocks of that agent (all blocks for `ALL`); an empty list for an unknown tab.
        """
        if tab == ALL:
            return self.blocks
        return [self.blocks[index] for index in self._by_agent.get(tab, ())]

    def block_in_tab(self, block: Block, tab: str) -> bool:
        """Tell whether a tab shows a block.

        Args:
            block: The block.
            tab: `MAIN`, `ALL` or an `agent_id`.

        Returns:
            True if `blocks_of(tab)` contains the block.
        """
        return tab == ALL or (block.agent_id or MAIN) == tab

    def step(self, tool_use_id: str | None) -> StepBlock | None:
        """Find the step of a tool call.

        Args:
            tool_use_id: The id of the call; None finds nothing.

        Returns:
            The step block, or None if no event of that call was seen.
        """
        return self._steps.get(tool_use_id) if tool_use_id else None

    def open_decisions(self, agent: str | None = None) -> list[Decision]:
        """Return the open escalations and questions, oldest first.

        Args:
            agent: Only those of this agent key (`MAIN` or an `agent_id`); None for all.

        Returns:
            The matching open items.
        """
        found = list(self.decisions.values())
        if agent is None:
            return found
        return [decision for decision in found if (decision.agent_id or MAIN) == agent]

    def tabs(self) -> list[str]:
        """Return the tab keys in display order.

        Returns:
            `MAIN`, then every subagent in the order the session listed them, then `ALL` if
            there is at least one subagent.
        """
        keys = list(self.agents)
        return [*keys, ALL] if len(keys) > 1 else keys

    def label(self, agent_id: str | None) -> str:
        """Return the short name of an agent.

        Args:
            agent_id: The agent id, None or `MAIN` for the main agent.

        Returns:
            The agent's label, or `subagent <first 6 characters of the id>` for an agent the
            session has not described.
        """
        key = agent_id or MAIN
        agent = self.agents.get(key)
        return agent.label if agent else f"subagent {key[-6:]}"

    def working(self) -> bool:
        """Tell whether Claude works on a turn.

        Returns:
            True in the states running, checking, tool_call and escalated.
        """
        return self.state in WORKING_STATES

    def level(self) -> str:
        """Name the supervision level the switches stand for.

        Returns:
            The name of the matching ladder mode of `calu.acp.modes`, or "Custom".
        """
        mode_id = mode_of(self.auto_escalate)
        if mode_id == CUSTOM:
            return "Custom"
        return next(mode.name for mode in LADDER if mode.id == mode_id)

    def activity_of(self, agent: Agent) -> str:
        """Describe what an agent does now, in a few words.

        Args:
            agent: The agent.

        Returns:
            For the main agent outside a turn the session state (idle, stopped, failed,
            starting). For a finished subagent its status. Otherwise the derived activity with
            the tool name, or "running, no activity seen since attach".
        """
        if self.open_decisions(agent.key):
            return "waits for you"
        if agent.key == MAIN and not self.working():
            return self.state
        if agent.key != MAIN and agent.status != "running":
            return agent.status
        if not agent.activity:
            return "running" if agent.key == MAIN else "running, no activity seen since attach"
        if agent.activity in {"checking", "tool call"} and agent.last_tool:
            return f"{agent.activity} {agent.last_tool}"
        return agent.activity

    def select(self, tab: str) -> Change:
        """Record which tab the user looks at and clear its unread flag.

        Args:
            tab: `MAIN`, `ALL` or an `agent_id`.

        Returns:
            A change that names the agents.
        """
        self.selected_tab = tab
        for agent in self.agents.values():
            if tab in {ALL, agent.key}:
                agent.unread = False
        return Change(agents=True)

    # ------------------------------------------------------------------ writing

    def note(self, text: str, time: float | None = None) -> Change:
        """Add a line of the client itself to the transcript of the main agent.

        Args:
            text: The line.
            time: Client clock, if known.

        Returns:
            A change that names the new block.
        """
        block = JoinBlock(id=len(self.blocks), turn=self.turn, time=time, text=text)
        return self._add(block, Change())

    def set_connection(
        self, status: str, attempt: int = 0, limit: int = 0, reason: str = ""
    ) -> Change:
        """Record the state of the connection.

        Args:
            status: connecting, attached, reconnecting or detached.
            attempt: Which reconnect in a row this is (for "reconnecting").
            limit: How many reconnects in a row are tried.
            reason: Why the connection ended (for "detached").

        Returns:
            A change that names the status line.
        """
        self.connection = ConnectionState(status, attempt, limit, reason)
        return Change(status=True)

    def load_snapshot(self, raw: dict[str, Any], time: float | None = None) -> Change:
        """Take over the result of `state.get`.

        The snapshot replaces every value it carries: state, ids, model, effort, agents, queue,
        switches and honesty marks. An open escalation the model does not know yet becomes a
        decision (with a step block if its tool call was never seen); a decision the snapshot
        no longer lists has ended while this client was away and is closed as "resolved".

        Args:
            raw: The snapshot as the session returned it.
            time: Client clock, if known.

        Returns:
            A change that names everything; an invalid snapshot only adds a log line and a
            notice.
        """
        try:
            snapshot = m.SessionSnapshot.model_validate(raw)
        except ValidationError as error:
            return self._invalid("state.get", error)
        change = Change(agents=True, decisions=True, queue=True, honesty=True, status=True)
        if snapshot.state != self.state:
            self.state_since = time
        self.state = snapshot.state
        self.workspace = snapshot.workspace
        self.project = snapshot.project
        self.model_name = snapshot.model
        self.effort = snapshot.effort
        self.claude_session_id = snapshot.claude_session_id
        self.calu_session_id = snapshot.calu_session_id
        self.auto_escalate = AutoEscalate.model_validate(snapshot.auto_escalate.model_dump())
        self._set_agents(snapshot.agents)
        self.queue = [Queued(q.id, q.text, q.delivery, q.to) for q in snapshot.queue]
        self.honesty_mode = snapshot.honesty_mode
        self.marks = [
            Mark(k.id, k.path, k.symbol, k.reason, k.git_branch) for k in snapshot.honesty_marks
        ]
        self.usage.limits = self._limits(snapshot.rate_limits)
        listed = {request.escalation_id for request in snapshot.escalations}
        for escalation_id in [key for key in self.decisions if key not in listed]:
            self._close(escalation_id, "resolved", None, change)
        for request in snapshot.escalations:
            if request.escalation_id not in self.decisions:
                self._open(request, None, change)
        return change

    def apply(self, method: str, params: dict[str, Any], time: float | None = None) -> Change:
        """Apply one protocol event.

        Never raises for an event of the session: params that do not fit the event's model and
        methods this client does not know become a line of the log view.

        Args:
            method: The notification method, e.g. "tool.use".
            params: Its params as received.
            time: Client clock in seconds for an event that arrived live; None for a replayed
                event, which then gets no duration and no waiting time.

        Returns:
            What the event changed.
        """
        model = EVENT_TYPES.get(method)
        if model is None or method not in HANDLED:
            self._log("warning", f"event {method} is not known to this client")
            return Change()
        try:
            event = model.model_validate(params)
        except ValidationError as error:
            return self._invalid(method, error)
        change = Change()
        match event:
            case m.SessionState():
                self._session_state(event, time, change)
            case m.AssistantDelta():
                self._delta(event, time, change)
            case m.AssistantText():
                self._text(event, time, change, thinking=False)
            case m.AssistantThinking():
                self._text(event, time, change, thinking=True)
            case m.ToolUse():
                self._tool_use(event, time, change)
            case m.ToolResult():
                self._tool_result(event, time, change)
            case m.ToolFiles():
                step = self.step(event.tool_use_id)
                if step is not None:
                    step.files = [link.path for link in event.files]
                    change.blocks_updated.add(step.id)
            case m.EditDiff():
                self._edit_diff(event, change)
            case m.CheckResult():
                self._check(event, time, change)
            case m.EscalationRequest():
                if event.escalation_id not in self.decisions:
                    self._open(event, time, change)
                    if time is not None:
                        change.bell = True
            case m.EscalationResolved():
                self._resolved(event, change)
            case m.ChatQueue():
                self._queue(event, change)
            case m.UserInput():
                self._user_input(event, time, change)
            case m.TurnResult():
                self._turn_result(event, time, change)
            case m.AgentsUpdate():
                self._set_agents(event.agents)
                change.agents = True
                for agent in event.agents:
                    call = self.step(agent.agent_id)
                    if call is not None:
                        change.blocks_updated.add(call.id)
            case m.HonestyUpdate():
                self.honesty_mode = event.mode
                self.marks = [
                    Mark(k.id, k.path, k.symbol, k.reason, k.git_branch) for k in event.marks
                ]
                change.honesty = True
            case m.UsageUpdate():
                self.usage.limits = self._limits(event.rate_limits)
                change.status = True
                if time is not None:
                    for limit in self.usage.limits:
                        if limit.status == "rejected":
                            change.notices.append(
                                Notice(
                                    "a usage limit of the Claude Code account is reached", "error"
                                )
                            )
            case m.AutoEscalateChanged():
                self.auto_escalate = AutoEscalate.model_validate(event.settings.model_dump())
                change.status = True
                if time is not None and not self.own_level_change:
                    change.notices.append(Notice(f"supervision level changed: {self.level()}"))
                self.own_level_change = False
            case m.Log():
                self._log(event.level, event.message)
                if time is not None and event.level in {"warning", "error"}:
                    change.notices.append(Notice(_short(event.message, 200), event.level))
            case _:  # pragma: no cover - HANDLED and the match cover the same methods
                self._log("warning", f"event {method} is not shown by this client")
        return change

    def set_usage(self, raw: dict[str, Any]) -> Change:
        """Take over the result of `usage.get`.

        Args:
            raw: The result as the session returned it.

        Returns:
            A change that names the status line; an invalid result only adds a log line and a
            notice.
        """
        try:
            result = m.UsageResult.model_validate(raw)
        except ValidationError as error:
            return self._invalid("usage.get", error)
        context = result.context
        self.usage.context = (
            None
            if context is None
            else ContextFill(context.tokens, context.max_tokens, context.percent, context.model)
        )
        self.usage.limits = self._limits(result.rate_limits)
        if result.cost_usd is not None:
            self.usage.cost = result.cost_usd
        return Change(status=True)

    def ask_side(self, question: str) -> tuple[SideExchange, Change]:
        """Add a side question that waits for its answer.

        Args:
            question: The question.

        Returns:
            The new exchange and a change that names the side questions.
        """
        exchange = SideExchange(question)
        self.side.append(exchange)
        return exchange, Change(side=True)

    def side_history(self) -> list[dict[str, str]]:
        """Return the answered side questions, as `side.ask {history}` takes them.

        Returns:
            The newest 20 exchanges that got an answer, oldest first.
        """
        answered = [entry for entry in self.side if entry.answer and not entry.pending]
        return [{"question": entry.question, "answer": entry.answer} for entry in answered[-20:]]

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _limits(listed: Iterable[m.RateLimit]) -> list[Limit]:
        """Copy the usage limits of a snapshot, an event or a result.

        Args:
            listed: The limits as the protocol carries them.

        Returns:
            One `Limit` per entry, in the same order.
        """
        return [Limit(k.type, k.status, k.utilization, k.resets_at) for k in listed]

    def _log(self, level: str, message: str) -> None:
        """Append a line to the log view, dropping the oldest beyond `LOG_LIMIT`.

        Args:
            level: debug, info, warning or error.
            message: The text.
        """
        self.logs.append(LogLine(level, message))
        del self.logs[:-LOG_LIMIT]

    def _invalid(self, method: str, error: ValidationError) -> Change:
        """Record an event or result whose params do not fit the protocol's model.

        Args:
            method: The method the params came with.
            error: What the validation said.

        Returns:
            A change with one warning notice.
        """
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        self._log("warning", f"{method} did not fit the protocol: {where} {first.get('msg', '')}")
        return Change(
            notices=[Notice(f"{method}: unexpected content from the session (see log)", "warning")]
        )

    def _add(self, block: Block, change: Change) -> Change:
        """Append a block to the transcript and to its agent's index.

        Args:
            block: The block; its `id` must be `len(self.blocks)`.
            change: The change to add the block to.

        Returns:
            `change`.
        """
        self.blocks.append(block)
        self._by_agent.setdefault(block.agent_id or MAIN, []).append(block.id)
        change.blocks_added.append(block.id)
        return change

    def _agent(self, agent_id: str | None) -> Agent:
        """Return the agent of an event, creating an entry for one the session has not listed.

        Args:
            agent_id: The event's `agent_id`; None for the main agent.

        Returns:
            The agent's entry in `agents`.
        """
        key = agent_id or MAIN
        agent = self.agents.get(key)
        if agent is None:
            agent = self.agents[key] = Agent(key, label=f"subagent {key[-6:]}")
            self._by_agent.setdefault(key, [])
        return agent

    def _touch(
        self, agent_id: str | None, activity: str, change: Change, tool: str | None = None
    ) -> None:
        """Record that an agent did something.

        Side effects: sets the agent's activity, its unread flag if another tab is selected,
        and marks the Agent call that started a subagent as updated, because its line shows
        the subagent's progress.

        Args:
            agent_id: The event's `agent_id`.
            activity: The agent's new activity word.
            change: The change to mark.
            tool: The tool the activity is about, if any.
        """
        agent = self._agent(agent_id)
        agent.activity = activity
        if tool is not None:
            agent.last_tool = tool
        if self.selected_tab not in {ALL, agent.key}:
            agent.unread = True
        change.agents = True
        call = self.step(agent_id)
        if call is not None:
            change.blocks_updated.add(call.id)

    def _set_agents(self, listed: Iterable[m.AgentInfo]) -> None:
        """Take over the session's list of subagents, keeping what the client derived.

        Args:
            listed: The subagents as `agents.update` or the snapshot carry them.
        """
        for info in listed:
            agent = self.agents.get(info.agent_id)
            if agent is None:
                agent = self.agents[info.agent_id] = Agent(info.agent_id)
                self._by_agent.setdefault(info.agent_id, [])
            agent.description = info.description
            agent.subagent_type = info.subagent_type
            agent.background = info.background
            agent.status = info.status
        taken: dict[str, int] = {}
        for agent in self.agents.values():
            if agent.key == MAIN:
                continue
            base = _short(agent.description or agent.subagent_type or "", 16)
            base = base or f"subagent {agent.key[-6:]}"
            taken[base] = taken.get(base, 0) + 1
            agent.label = base if taken[base] == 1 else f"{base} #{taken[base]}"

    def _session_state(self, event: m.SessionState, time: float | None, change: Change) -> None:
        """Apply `session.state`.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        if event.state != self.state:
            self.state_since = time
        self.state = event.state
        if event.session_id:
            self.claude_session_id = event.session_id
        if event.state == "running" and self.turn_started is None:
            self.turn_started = time
        if not self.working():
            self.agents[MAIN].activity = ""
        change.status = True
        change.agents = True

    def _stream(
        self, agent_id: str | None, message_id: str | None, thinking: bool
    ) -> TextBlock | ThinkingBlock | None:
        """Find the block that collects the fragments of one message.

        Args:
            agent_id: The agent the message belongs to.
            message_id: The message id; None finds nothing.
            thinking: Whether the thinking block of the message is meant.

        Returns:
            The streaming block, or None if no fragment of that message was seen.
        """
        if message_id is None:
            return None
        block = self._streams.get((agent_id or MAIN, message_id, thinking))
        return block if isinstance(block, TextBlock | ThinkingBlock) else None

    def _delta(self, event: m.AssistantDelta, time: float | None, change: Change) -> None:
        """Apply `assistant.delta`: append the fragment to its streaming block.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        block = self._stream(event.agent_id, event.message_id, event.thinking)
        if block is None:
            kind = ThinkingBlock if event.thinking else TextBlock
            block = kind(
                id=len(self.blocks),
                agent_id=event.agent_id,
                turn=self.turn,
                time=time,
                text=event.text,
                message_id=event.message_id,
                streaming=True,
            )
            self._streams[event.agent_id or MAIN, event.message_id, event.thinking] = block
            self._add(block, change)
            self._touch(event.agent_id, "thinking" if event.thinking else "running", change)
        else:
            block.text += event.text
            change.blocks_updated.add(block.id)
        self.saw_thinking |= event.thinking

    def _text(
        self,
        event: m.AssistantText | m.AssistantThinking,
        time: float | None,
        change: Change,
        thinking: bool,
    ) -> None:
        """Apply `assistant.text` or `assistant.thinking`: the complete block.

        The text replaces the fragments collected for the same message. An empty text without
        fragments adds nothing.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
            thinking: True for `assistant.thinking`.
        """
        self.saw_thinking |= thinking
        block = self._stream(event.agent_id, event.message_id, thinking)
        if block is not None:
            block.text = event.text
            block.streaming = False
            change.blocks_updated.add(block.id)
            assert event.message_id is not None
            del self._streams[event.agent_id or MAIN, event.message_id, thinking]
        elif event.text.strip():
            kind = ThinkingBlock if thinking else TextBlock
            self._add(
                kind(
                    id=len(self.blocks),
                    agent_id=event.agent_id,
                    turn=self.turn,
                    time=time,
                    text=event.text,
                    message_id=event.message_id,
                ),
                change,
            )
        self._touch(event.agent_id, "running", change)

    def _new_step(
        self,
        tool_use_id: str | None,
        agent_id: str | None,
        tool: str,
        tool_input: dict[str, Any],
        time: float | None,
        change: Change,
    ) -> StepBlock:
        """Create the block of a tool call and attach diffs that arrived before it.

        Args:
            tool_use_id: The id of the call; None for a check that belongs to no call.
            agent_id: The agent that made the call.
            tool: The tool name.
            tool_input: The tool input.
            time: Client clock, if the event arrived live.
            change: The change to add the block to.

        Returns:
            The new step block.
        """
        step = StepBlock(
            id=len(self.blocks),
            agent_id=agent_id,
            turn=self.turn,
            time=time,
            tool_use_id=tool_use_id,
            tool=tool,
            input=tool_input,
        )
        if tool_use_id:
            self._steps[tool_use_id] = step
            step.diffs = self._pending_diffs.pop(tool_use_id, [])
        self._add(step, change)
        self._agent(agent_id).steps += 1
        return step

    def _tool_use(self, event: m.ToolUse, time: float | None, change: Change) -> None:
        """Apply `tool.use`.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        step = self.step(event.tool_use_id)
        if step is None:
            self._new_step(event.tool_use_id, event.agent_id, event.name, event.input, time, change)
        else:  # a check or an escalation of the call arrived first
            step.tool = event.name
            step.input = event.input
            change.blocks_updated.add(step.id)
        self._touch(event.agent_id, "checking", change, tool=event.name)

    def _tool_result(self, event: m.ToolResult, time: float | None, change: Change) -> None:
        """Apply `tool.result`.

        The result of an Agent call also ends its subagent if that runs in the foreground: the
        session's own status of such an agent can arrive later.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        step = self.step(event.tool_use_id)
        if step is not None:
            step.result = event.content
            step.is_error = event.is_error
            step.ended = time
            change.blocks_updated.add(step.id)
        started = self.agents.get(event.tool_use_id)
        if started is not None and not started.background and started.status == "running":
            started.status = "failed" if event.is_error else "completed"
        self._touch(event.agent_id, "running", change)

    def _edit_diff(self, event: m.EditDiff, change: Change) -> None:
        """Apply `edit.diff`: attach it to its step, or keep it until the step arrives.

        Args:
            event: The event.
            change: The change to mark.
        """
        diff = Diff(event.path, event.before, event.after, event.start_line, event.end_line)
        step = self.step(event.tool_use_id)
        if step is not None:
            step.diffs.append(diff)
            change.blocks_updated.add(step.id)
        elif event.tool_use_id:
            self._pending_diffs.setdefault(event.tool_use_id, []).append(diff)

    def _check(self, event: m.CheckResult, time: float | None, change: Change) -> None:
        """Apply `check.result`: add the check to its step.

        A check without a tool call (the review of a subagent's result, for instance) gets a
        step block of its own.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        step = self.step(event.tool_use_id)
        if step is None:
            step = self._new_step(
                event.tool_use_id, event.agent_id, event.tool_name, {}, time, change
            )
        step.checks.append(
            Check(
                str(event.route.value),
                str(event.decision.value),
                event.reason,
                event.step_summary,
                event.comment,
            )
        )
        change.blocks_updated.add(step.id)
        decision = step.checks[-1].decision.upper()
        activity = "tool call" if decision in {"PASS", "ALLOW"} else "running"
        if event.tool_use_id is None or decision == "ESCALATE":
            activity = self._agent(event.agent_id).activity or "running"
        self._touch(event.agent_id, activity, change, tool=event.tool_name)

    def _open(self, request: m.EscalationRequest, time: float | None, change: Change) -> Decision:
        """Add an open escalation or question and mark its step as waiting.

        Args:
            request: The request, from an event or from the snapshot.
            time: Client clock, if the request arrived live.
            change: The change to mark.

        Returns:
            The new decision.
        """
        questions = parse_questions(request.questions)
        step = self.step(request.tool_use_id)
        if step is None:
            step = self._new_step(
                request.tool_use_id,
                request.agent_id,
                request.tool_name,
                request.tool_input,
                time,
                change,
            )
        step.waiting = "open"
        step.escalation_id = request.escalation_id
        step.opened_at = time
        change.blocks_updated.add(step.id)
        by_rule = any(check.route == "escalate" for check in step.checks) or (
            "deny rule" in request.reason or "deny list" in request.reason
        )
        decision = Decision(
            escalation_id=request.escalation_id,
            tool_use_id=request.tool_use_id,
            agent_id=request.agent_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            reason=request.reason,
            rendered=request.rendered,
            questions=questions,
            raw_questions=request.questions,
            opened_at=time,
            by_rule=by_rule and not questions,
            picks=[set() for _ in questions],
            free=["" for _ in questions],
        )
        self.decisions[request.escalation_id] = decision
        change.decisions = True
        change.status = True
        self._touch(request.agent_id, "waits for you", change)
        return decision

    def _close(
        self, escalation_id: str, outcome: str, note: str | None, change: Change
    ) -> Decision | None:
        """Remove an open item and record the outcome on its step.

        Args:
            escalation_id: The escalation.
            outcome: allowed, denied, cancelled, answered or resolved.
            note: The note that went to Claude, if any.
            change: The change to mark.

        Returns:
            The removed decision, or None if it was not open.
        """
        decision = self.decisions.pop(escalation_id, None)
        if decision is None:
            return None
        step = self.step(decision.tool_use_id)
        if step is None:
            step = next(
                (
                    block
                    for block in self.blocks
                    if isinstance(block, StepBlock) and block.escalation_id == escalation_id
                ),
                None,
            )
        if step is not None:
            step.waiting = outcome
            step.note = note or ""
            change.blocks_updated.add(step.id)
        change.decisions = True
        change.status = True
        activity = "tool call" if outcome == "allowed" else "running"
        self._touch(decision.agent_id, activity, change)
        return decision

    def _resolved(self, event: m.EscalationResolved, change: Change) -> None:
        """Apply `escalation.resolved`.

        The session reports an answered question as "deny" with the answer in the note; the
        model knows from the request that the item was a question and records "answered".

        Args:
            event: The event.
            change: The change to mark.
        """
        known = self.decisions.get(event.escalation_id)
        if known is None:
            return
        outcome = {"allow": "allowed", "deny": "denied", "cancelled": "cancelled"}[event.decision]
        if known.is_question and event.decision != "cancelled":
            outcome = "answered"
        note = known.answer_text() if outcome == "answered" and known.own else event.note
        self._close(event.escalation_id, outcome, note, change)
        if not known.own:
            change.notices.append(Notice(f"decided elsewhere: {outcome}"))

    def _queue(self, event: m.ChatQueue, change: Change) -> None:
        """Apply `chat.queue`: replace the list and remember which messages left it.

        Args:
            event: The event.
            change: The change to mark.
        """
        kept = {message.id for message in event.messages}
        self._left_queue = [entry for entry in self.queue if entry.id not in kept]
        self.queue = [Queued(q.id, q.text, q.delivery, q.to) for q in event.messages]
        change.queue = True

    def _user_input(self, event: m.UserInput, time: float | None, change: Change) -> None:
        """Apply `user.input`: add the message Claude has taken in.

        A message for a subagent reaches Claude as a request to relay it. `user.input` does not
        name its target, so the block is matched with the queue entry for a subagent whose text
        the input contains, and then shows that text with "relayed to <agent>".

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        text = event.text
        relayed: str | None = None
        if event.source == "user":
            for entry in [*self._left_queue, *self.queue]:
                if entry.to != MAIN and entry.text and entry.text in text:
                    relayed = self.label(entry.to)
                    text = entry.text
                    break
        self._add(
            UserBlock(
                id=len(self.blocks),
                turn=self.turn,
                time=time,
                text=text,
                source=event.source,
                relayed_to=relayed,
            ),
            change,
        )
        if self.turn_started is None:
            self.turn_started = time

    def _turn_result(self, event: m.TurnResult, time: float | None, change: Change) -> None:
        """Apply `turn.result`: end the turn, close streaming blocks, record cost and turns.

        Args:
            event: The event.
            time: Client clock, if the event arrived live.
            change: The change to mark.
        """
        for block in self._streams.values():
            if isinstance(block, TextBlock | ThinkingBlock):
                block.streaming = False
                change.blocks_updated.add(block.id)
        self._streams.clear()
        delta: float | None = None
        if event.cost_usd is not None:
            previous = self._previous_cost
            if previous is not None and event.cost_usd >= previous:
                delta = event.cost_usd - previous
            self._previous_cost = event.cost_usd
            self.usage.cost = event.cost_usd
        self.turn += 1
        self.usage.turns = self.turn
        self.usage.background_tasks = event.background_tasks
        duration = None
        if time is not None and self.turn_started is not None:
            duration = max(0.0, time - self.turn_started)
        self.turn_started = None
        if event.session_id:
            self.claude_session_id = event.session_id
        self._add(
            TurnEndBlock(
                id=len(self.blocks),
                turn=self.turn - 1,
                time=time,
                number=self.turn,
                cost=event.cost_usd,
                cost_delta=delta,
                duration=duration,
                background_tasks=event.background_tasks,
                is_error=event.is_error,
                subtype=event.subtype,
                result=event.result if event.is_error else "",
            ),
            change,
        )
        self.agents[MAIN].activity = ""
        change.status = True
        change.agents = True
