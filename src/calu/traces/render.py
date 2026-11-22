"""What a session trace says: the events of a session as Markdown blocks, the header, the path.

Everything here is rendering without input or output, except `create_trace_file` and
`find_trace_of`, which touch the trace directory. `TraceRenderer` turns the session's events
(`calu.protocol.messages`), in the order the session emits them, into `Block`s:

    event                            block (heading; body)
    user.input, source user          "User"; the message, quoted; a line counting the images
                                     (`Block.removable`: the redaction agent may leave it out)
    user.input, other sources        "Message from another Claude session" / "Automatic
                                     message"; the text, quoted (narrated: its narration)
    assistant.text                   plain: "Claude"; the text, quoted
    assistant.thinking, .delta       plain: "Claude's thinking"; the final text of one thinking
                                     block, quoted, written once the block is complete
                                     (`_PendingThinking`)
    assistant.text, .thinking        narrated: one block per run of them up to the agent's
                                     next tool call or the end of the turn; the narration
    tool.use                         nothing; numbers the call. A subagent tool: "Tool call N:
                                     Agent (starts subagent K)", a heading without the task
    check.result                     "Tool call N: <tool>: <DECISION>"; the checker's summary
                                     of the step, its reason and its comment to Claude where
                                     there is one (for a subagent's result: "Result handed
                                     back by tool call N (<tool>): <DECISION>")
    escalation.request               "Escalation E: tool call N (<tool>) waits for a person";
                                     the reason; the questions of a question of the agent
    escalation.resolved              "Escalation E: allowed | denied | answered | ended
                                     without a decision"; who decided, the note
    tool.result                      nothing
    turn.result                      "End of turn T" ("End of turn" without turn numbers)

    every other event                nothing (streaming fragments, states, diffs, lists)

The trace holds no tool input (commands, edits) and no tool result: the checker's summary of a
step says what it did (the owner's decision of 2026-11-22). Headings are calu's own words.
Text that a person, Claude or a checker wrote stands below them, quoted line by line
(`quote`), so that it cannot pass for a heading of the trace.

Subagents: the tool call that starts a subagent numbers it ("starts subagent 2"), and every
block of that subagent is headed "Subagent 2: ..." one heading level below the blocks of the
agent that started it:

    ## Turn 1
    ### Tool call 3: Agent (starts subagent 1)
    #### Subagent 1: tool call 4: Read: PASS
    #### Subagent 1: result handed back by tool call 3 (Agent): PASS

In the narrated form a block is created where Claude's first words appear and filled later:
`Block.outputs` collects the text and thinking, `Block.closed` says that no more will come,
and the writer (`calu.traces.writer`) sets the narration.

Thinking in the plain form (agent/records/PLAN.md D241). Claude Code returns thinking as a
summary, which can arrive as streamed fragments (`assistant.delta` with `thinking`) and as one
or more completed texts of the same message (`assistant.thinking`; a later one rewrites the
earlier). The renderer keeps one pending thinking block per agent and writes it once, in its
last completed form (else the fragments streamed so far), when the agent's thinking is over:

    pending block of agent A is written when    A's text, tool call, check or escalation
                                                comes; A's thinking of another message
                                                comes; A stops (subagent); the turn or the
                                                trace ends
    an assistant.thinking of A replaces it when it has the pending block's message id, or
                                                the pending block holds fragments only
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from calu.checkers.policy import Route
from calu.context.narrator import ClaudeOutput
from calu.protocol.messages import (
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    SessionState,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)

AGENT_TOOLS = ("Agent", "Task")
"""The tools whose call starts a subagent (as `calu.claude.sdk_runner.AGENT_TOOLS`)."""

NOT_NARRATED = "[Claude's text at this point could not be narrated and is left out]"
"""What a narrated trace holds where the narrator gave no narration."""

MAX_HEADING_LEVEL = 6
"""Markdown has six heading levels; deeper subagents share the last one."""

FILE_NAME = re.compile(r"^session_(?:(\d{3,})|[0-9a-f]{12})\.md$")
"""A trace file's name: `session_NNN.md` (group 1 is its number) or, with
`traces.file_names` = "random", `session_<12 hex digits>.md` (group 1 is None)."""

RANDOM_NAME_BYTES = 6
"""Random bytes in the name of a trace file with `traces.file_names` = "random" (12 hex
digits)."""

LAYOUTS = ("date/user", "user", "date", "flat")
"""The values of `traces.layout`: the folders between the trace directory and a file."""

SEARCHED_FILES = 500
"""How many trace files `find_trace_of` reads at most, newest first."""

SEARCHED_BYTES = 5_000_000
"""How much of one trace file `find_trace_of` reads."""

TRACE_GLOBS = ("session_*.md", "*/session_*.md", "*/*/session_*.md")
"""Where trace files lie below the trace directory, in every layout of `LAYOUTS`."""


@dataclass
class Block:
    """One appended piece of a trace: a heading by calu and the text below it.

    A block of the narrated form starts open and without a body: `outputs` holds what is to
    be narrated, and `closed` turns True when the agent's next tool call or the end of the
    turn shows that no more text belongs to it.
    """

    heading: str
    """The Markdown heading line, calu's own words ("" for a block without a heading)."""
    body: str = ""
    """The Markdown below the heading."""
    fixed: bool = False
    """True if the body holds only calu's own words and numbers: the redaction agent does not
    need to read it (the deterministic stage still does)."""
    outputs: list[ClaudeOutput] | None = None
    """For a block of the narrated form: Claude's text and thinking to narrate, in order."""
    closed: bool = True
    """False while a block of the narrated form may still get more `outputs`."""
    narration: str | None = None
    """The narration of `outputs`, once somebody delivered it; None before that."""
    removable: bool = False
    """True for a message of the user: the redaction agent may leave the whole block out,
    heading included, if its instructions allow that (agent/records/PLAN.md D245)."""


@dataclass
class _PendingThinking:
    """One thinking block of one agent that is not written yet (plain form)."""

    message_id: str | None
    """The id of the message the thinking belongs to, or None if the event carried none."""
    final: str | None = None
    """The last completed text (`assistant.thinking`), or None while only fragments came."""
    partial: str = ""
    """The fragments (`assistant.delta`) streamed since the last completed text."""

    def text(self) -> str:
        """Return what the trace holds of this block.

        Returns:
            The last completed text if there is one, else the fragments joined.
        """
        return self.final if self.final is not None else self.partial


def quote(text: str) -> str:
    """Return `text` as a Markdown block quote.

    Args:
        text: Text written by a person or a model.

    Returns:
        Every line prefixed with "> " (an empty line with ">"), without trailing blank lines;
        ">" for a text without visible characters.
    """
    lines = text.strip("\n").splitlines() or [""]
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def one_line(text: str, limit: int = 200) -> str:
    """Return a short text as one line that cannot leave an inline code span.

    Args:
        text: A name or an id, e.g. the client that decided an escalation.
        limit: The largest number of characters kept.

    Returns:
        The text with white space runs as single spaces and backticks as apostrophes, cut to
        `limit` characters.
    """
    return " ".join(text.replace("`", "'").split())[:limit]


def format_block(heading: str, body: str) -> str:
    """Return the text a block adds to the file.

    Args:
        heading: The heading line, or "".
        body: The Markdown below it, or "".

    Returns:
        Heading and body separated by a blank line, followed by a blank line.
    """
    parts = [part.strip("\n") for part in (heading, body) if part.strip()]
    return "\n\n".join(parts) + "\n\n" if parts else ""


def render_questions(questions: list[dict[str, Any]]) -> str:
    """Render the questions of a question of the agent (`AskUserQuestion`).

    Args:
        questions: The questions as Claude asked them: each with `question`, `header`,
            `options` (each with `label` and `description`) and `multiSelect`.

    Returns:
        Markdown: per question a numbered line and, quoted, its text and its options as a
        list; "" without questions.
    """
    parts: list[str] = []
    for number, question in enumerate(questions, start=1):
        lines = [str(question.get("question", "")).strip()]
        options = question.get("options")
        for option in options if isinstance(options, list) else []:
            if not isinstance(option, dict):
                continue
            label = one_line(str(option.get("label", "")))
            description = one_line(str(option.get("description", "")), 500)
            lines.append(f"- {label}: {description}" if description else f"- {label}")
        parts.append(f"Question {number}:\n{quote(chr(10).join(lines))}")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Header:
    """What the first lines of a trace file say about the session."""

    calu_session: str | None
    """Id of the `sessions` row, or None if the row could not be written."""
    claude_session: str | None
    """Claude Code's session id, or None while it is not known."""
    project: str
    started: datetime | None
    """When the trace started, with a time zone; None leaves the time out of the header
    (`traces.header_times` off)."""
    user: str | None
    """The OS user Claude Code runs as; None leaves the user out of the header
    (`traces.header_user` off)."""
    claude_model: str
    calu_version: str
    content: str
    """"narrated" or "plain": what the file holds of Claude's words."""
    content_note: str = ""
    """Why `content` is not what `traces.content` asked for, or ""."""
    redactor: str | None = None
    """The model of the redaction agent, or None if only the deterministic stage runs."""
    continues: str = ""
    """One sentence that names the conversation or the file this one continues, or ""."""
    note: str = ""
    """A further sentence, e.g. that the trace started in the middle of a session."""


def render_header(header: Header) -> str:
    """Return the first lines of a trace file.

    Args:
        header: What to say.

    Returns:
        Markdown: the title, a list of the session's facts, the caveat about redaction and a
        horizontal rule, followed by a blank line. The lines "Started" and "User" are left
        out when `header.started` or `header.user` is None.
    """
    known = (
        f"`{one_line(header.claude_session)}`"
        if header.claude_session
        else "not known yet (a later line names it)"
    )
    content = {
        "narrated": "narrated (Claude's text and thinking retold in indirect speech by the "
        "narrator; no images)",
        "plain": "plain (Claude's text and the summaries of its thinking as written; no images)",
    }.get(header.content, header.content)
    if header.content_note:
        content += f". {header.content_note}"
    if header.redactor:
        redaction = (
            "the secret values calu knows and common shapes of keys and tokens are replaced; "
            "secrets and personal data are replaced by the redaction agent "
            f"(`{one_line(header.redactor)}`)"
        )
    else:
        redaction = (
            "only the secret values calu knows and common shapes of keys and tokens are "
            "replaced (`traces.redact` is off): personal data is not removed"
        )
    lines = [
        "# calu session trace",
        "",
        f"- calu session: {one_line(header.calu_session or '') or '?'}",
        f"- Claude session: {known}",
        f"- Project: {one_line(header.project) or '?'}",
    ]
    if header.started is not None:
        lines.append(f"- Started: {header.started.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if header.user is not None:
        lines.append(f"- User: {one_line(header.user)}")
    lines += [
        f"- Claude model: {one_line(header.claude_model) or 'the default of Claude Code'}",
        f"- calu version: {header.calu_version}",
        f"- Content: {content}",
        f"- Redaction: {redaction}",
    ]
    if header.continues:
        lines.append(f"- Continues: {header.continues}")
    if header.note:
        lines.append(f"- Note: {header.note}")
    lines += [
        "",
        "A block that could not be redacted is replaced by a line that says it was withheld. "
        "Redaction by a model is not a guarantee: read this file before you publish it.",
        "",
        "---",
        "",
        "",
    ]
    return "\n".join(lines)


def session_marker(claude_session_id: str) -> str:
    """Return the text by which a trace file names the Claude session it records.

    Args:
        claude_session_id: Claude Code's session id.

    Returns:
        "Claude session: `<id>`", as the header and the later note write it and
        `find_trace_of` searches it.
    """
    return f"Claude session: `{one_line(claude_session_id)}`"


class TraceRenderer:
    """Turns the events of one session into the blocks of its trace, in order.

    The object keeps what the blocks refer to: the number of the turn, the numbers of the
    tool calls and of the subagents, the open blocks of the narrated form.
    """

    def __init__(
        self,
        narrated: bool,
        claude_session: str | None = None,
        turn_numbers: bool = True,
    ) -> None:
        """Create a renderer for a trace that starts now.

        Args:
            narrated: True for the narrated form (Claude's text and thinking as blocks to be
                narrated), False for the plain form (text and thinking verbatim).
            claude_session: The Claude session id the header already names, or None.
            turn_numbers: True for "## Turn 3" ... "### End of turn 3"; False for "## Turn"
                ... "### End of turn" (`traces.turn_numbers`).
        """
        self.narrated = narrated
        self.turn_numbers = turn_numbers
        self.claude_session = claude_session
        self._turn = 0
        self._in_turn = False
        self._calls: dict[str, tuple[int, str]] = {}
        """tool_use_id -> (number, tool name)."""
        self._agents: dict[str, tuple[int, int]] = {}
        """agent id (the id of the tool call that started it) -> (number, depth from 1)."""
        self._open: dict[str | None, Block] = {}
        """Agent id (None: the main agent) -> its open block of the narrated form."""
        self._thinking: dict[str | None, _PendingThinking] = {}
        """Agent id (None: the main agent) -> its thinking block not written yet (plain form)."""
        self._escalations: dict[str, tuple[str | None, bool]] = {}
        """Id of an open escalation -> (the agent of its step, whether it is a question of the
        agent)."""

    def render(self, event: Event, resolved_by: str = "") -> list[Block]:
        """Return the blocks that `event` adds to the trace.

        Side effects: numbers turns, tool calls and subagents; in the narrated form, adds
        text to an open block or closes one (`Block.closed`) instead of returning a new one;
        in the plain form, keeps an agent's thinking until its block is complete (module
        docstring) and returns it then, before the block that showed the end.

        Args:
            event: An event of the session, in the order the session emitted it.
            resolved_by: For `escalation.resolved`: who decided, as the escalation's row
                records it (e.g. "client:tui"), or "" if that is not known.

        Returns:
            The new blocks in the order they belong in the file; often none.
        """
        blocks: list[Block] = []
        match event:
            case SessionState():
                blocks += self._session_id(event.session_id)
            case UserInput():
                blocks += self._start_turn()
                blocks += self._user_input(event)
            case AssistantDelta() if event.thinking and not self.narrated:
                blocks += self._thinking_fragment(event)
            case AssistantThinking() if not self.narrated:
                blocks += self._thinking_done(event)
            case AssistantText() | AssistantThinking():
                blocks += self._claude_output(event)
            case ToolUse():
                self._close(event.agent_id)
                blocks += self._flush_thinking(event.agent_id)
                blocks += self._start_turn()
                blocks += self._tool_use(event)
            case CheckResult():
                self._close(event.agent_id)
                blocks += self._flush_thinking(event.agent_id)
                blocks += self._start_turn()
                blocks.append(self._check(event))
            case EscalationRequest():
                self._close(event.agent_id)
                blocks += self._flush_thinking(event.agent_id)
                blocks += self._start_turn()
                blocks.append(self._escalation(event))
            case EscalationResolved():
                blocks.append(self._resolved(event, resolved_by))
            case ToolResult():
                pass  # the result is not part of the trace (module docstring)
            case AgentsUpdate():
                for agent in event.agents:
                    if agent.status != "running":
                        self._close(agent.agent_id)
                        blocks += self._flush_thinking(agent.agent_id)
            case TurnResult():
                blocks += self.close_all()
                blocks += self._session_id(event.session_id)
                blocks.append(self._turn_end(event))
            case _:
                pass
        return blocks

    def close_all(self) -> list[Block]:
        """Close every open block (the end of a turn or of the trace).

        Side effects: closes the open blocks of the narrated form and forgets the pending
        thinking of every agent (plain form).

        Returns:
            The pending thinking blocks of the plain form, main agent first, then the
            subagents in the order their thinking began; they belong in the file now.
        """
        for block in self._open.values():
            block.closed = True
        self._open.clear()
        blocks: list[Block] = []
        for agent in sorted(self._thinking, key=lambda a: a is not None):
            blocks += self._flush_thinking(agent)
        return blocks

    def _thinking_fragment(self, event: AssistantDelta) -> list[Block]:
        """Keep a streamed fragment of thinking (plain form).

        Args:
            event: An `assistant.delta` with `thinking` true.

        Returns:
            The agent's pending thinking block if this fragment belongs to another message
            (that block is complete), else nothing.
        """
        pending = self._thinking.get(event.agent_id)
        blocks: list[Block] = []
        if pending is not None and pending.message_id != event.message_id:
            blocks = self._flush_thinking(event.agent_id)
            pending = None
        if pending is None:
            pending = self._thinking[event.agent_id] = _PendingThinking(event.message_id)
        pending.partial += event.text
        return blocks

    def _thinking_done(self, event: AssistantThinking) -> list[Block]:
        """Keep a completed thinking text, replacing the version it rewrites (plain form).

        The text replaces the agent's pending block when both carry the same message id or
        when the pending block holds fragments only (the fragments' id and the completed
        message's id need not agree); otherwise the pending block is complete and this text
        starts the next one.

        Args:
            event: An `assistant.thinking`.

        Returns:
            The agent's earlier pending thinking block if this text does not replace it, else
            nothing.
        """
        pending = self._thinking.get(event.agent_id)
        blocks: list[Block] = []
        same = pending is not None and (
            pending.final is None
            or (event.message_id is not None and pending.message_id == event.message_id)
        )
        if pending is not None and not same:
            blocks = self._flush_thinking(event.agent_id)
            pending = None
        if pending is None:
            pending = self._thinking[event.agent_id] = _PendingThinking(event.message_id)
        pending.message_id = event.message_id or pending.message_id
        pending.final, pending.partial = event.text, ""
        return blocks

    def _flush_thinking(self, agent_id: str | None) -> list[Block]:
        """Return an agent's pending thinking as a block and forget it (plain form).

        Args:
            agent_id: The agent; None is the main agent.

        Returns:
            The turn's heading if no turn runs, and one block "Claude's thinking" (for a
            subagent "Subagent K: thinking") with the text quoted; nothing if the agent has no
            pending thinking or its text is blank.
        """
        pending = self._thinking.pop(agent_id, None)
        if pending is None or not pending.text().strip():
            return []
        said = "Claude's thinking" if agent_id is None else "thinking"
        return [*self._start_turn(), Block(self._heading(agent_id, said), quote(pending.text()))]

    def _close(self, agent_id: str | None) -> None:
        """Close the open block of one agent, if it has one.

        Args:
            agent_id: The agent; None is the main agent.
        """
        block = self._open.pop(agent_id, None)
        if block is not None:
            block.closed = True

    def _start_turn(self) -> list[Block]:
        """Return the heading of a new turn if none is running.

        Returns:
            One block "## Turn T" ("## Turn" without `turn_numbers`), or nothing while a
            turn runs.
        """
        if self._in_turn:
            return []
        self._in_turn = True
        self._turn += 1
        return [Block(f"## Turn {self._turn}" if self.turn_numbers else "## Turn", fixed=True)]

    def _session_id(self, session_id: str | None) -> list[Block]:
        """Return a line that names Claude's session id when it becomes known or changes.

        Args:
            session_id: The id an event carries, or None.

        Returns:
            One block without a heading, or nothing if the id is the one already named.
        """
        if not session_id or session_id == self.claude_session:
            return []
        self.claude_session = session_id
        return [Block("", f"_{session_marker(session_id)}_", fixed=True)]

    def _agent(self, agent_id: str | None) -> tuple[int, int]:
        """Return the number and the depth of an agent, numbering one that is new.

        Args:
            agent_id: The agent; None is the main agent.

        Returns:
            (0, 0) for the main agent; else (number from 1, depth from 1). A subagent whose
            start this trace did not see gets the depth 1.
        """
        if agent_id is None:
            return 0, 0
        if agent_id not in self._agents:
            self._agents[agent_id] = (len(self._agents) + 1, 1)
        return self._agents[agent_id]

    def _heading(self, agent_id: str | None, text: str) -> str:
        """Return the heading line of a block of one agent.

        Args:
            agent_id: The agent the block belongs to; None is the main agent.
            text: What the block is, starting in lower case, e.g. "tool call 3: Bash".

        Returns:
            For the main agent "### Tool call 3: Bash"; for a subagent one level deeper per
            depth and with its number: "#### Subagent 1: tool call 3: Bash".
        """
        number, depth = self._agent(agent_id)
        level = min(MAX_HEADING_LEVEL, 3 + depth)
        if number:
            return f"{'#' * level} Subagent {number}: {text}"
        return f"{'#' * level} {text[:1].upper()}{text[1:]}"

    def _call(self, tool_use_id: str | None, name: str = "") -> str:
        """Return the words that refer to a tool call, numbering one that is new.

        Args:
            tool_use_id: The call's id, or None if the event carries none.
            name: The tool's name, if the event carries it.

        Returns:
            "tool call N (<tool>)", "tool call N" if the tool is not known, or "a tool call"
            without an id.
        """
        if not tool_use_id:
            return f"a tool call ({one_line(name)})" if name else "a tool call"
        number, known = self._calls.get(tool_use_id, (len(self._calls) + 1, ""))
        known = known or one_line(name, 80)
        self._calls[tool_use_id] = (number, known)
        return f"tool call {number} ({known})" if known else f"tool call {number}"

    def _call_title(self, tool_use_id: str | None, name: str = "") -> str:
        """Return the heading words of a tool call, numbering one that is new.

        Args:
            tool_use_id: The call's id, or None if the event carries none.
            name: The tool's name, if the event carries it.

        Returns:
            "tool call N: <tool>" (with "(starts subagent K)" for a subagent tool), "tool
            call N" if the tool is not known, or "a tool call (<tool>)" without an id.
        """
        if not tool_use_id:
            return f"a tool call ({one_line(name)})" if name else "a tool call"
        self._call(tool_use_id, name)
        number, known = self._calls[tool_use_id]
        title = f"tool call {number}: {known}" if known else f"tool call {number}"
        if tool_use_id in self._agents:
            title += f" (starts subagent {self._agents[tool_use_id][0]})"
        return title

    def _user_input(self, event: UserInput) -> list[Block]:
        """Render a message Claude took in.

        Args:
            event: The event.

        Returns:
            One block: the user's message quoted, with a line that counts attached images; or
            a message that is not the user's, labelled as such (in the narrated form to be
            narrated, as the checkers read it).
        """
        if event.source == "user":
            body = quote(event.text)
            if event.images:
                count = len(event.images)
                noun = "image was" if count == 1 else "images were"
                body += f"\n\n_{count} {noun} attached; images are not part of the trace._"
            return [Block("### User", body, removable=True)]
        cross = event.source == "cross_session"
        heading = (
            "### Message from another Claude session (not from the user)"
            if cross
            else "### Automatic message from calu or Claude Code (not from the user)"
        )
        if self.narrated:
            kind = "message" if cross else "notice"
            return [Block(heading + ", narrated", outputs=[ClaudeOutput(kind, event.text)])]
        return [Block(heading, quote(event.text))]

    def _claude_output(self, event: AssistantText | AssistantThinking) -> list[Block]:
        """Render a block of Claude's text or thinking.

        Args:
            event: The event.

        Returns:
            Plain form: the agent's pending thinking block (`_flush_thinking`), then one
            block with the text quoted (thinking itself is handled by `_thinking_done`).
            Narrated form: a new open block if the agent has none (else the text joins the
            open one).
        """
        thinking = isinstance(event, AssistantThinking)
        if not self.narrated:
            blocks = self._flush_thinking(event.agent_id)
            if thinking or not event.text.strip():
                return blocks
            said = "Claude" if event.agent_id is None else "text"
            return [
                *blocks,
                *self._start_turn(),
                Block(self._heading(event.agent_id, said), quote(event.text)),
            ]
        output = ClaudeOutput("thinking" if thinking else "text", event.text)
        block = self._open.get(event.agent_id)
        if block is not None and block.outputs is not None:
            block.outputs.append(output)
            block.narration = None  # a narration delivered so far did not cover this text
            return []
        said = "Claude, narrated" if event.agent_id is None else "text and thinking, narrated"
        block = Block(self._heading(event.agent_id, said), outputs=[output], closed=False)
        self._open[event.agent_id] = block
        return [*self._start_turn(), block]

    def _tool_use(self, event: ToolUse) -> list[Block]:
        """Number a tool call, and the subagent it starts.

        Side effects: records the call's number and tool name, and for a subagent tool the
        subagent's number, which the check, the escalation and later blocks refer to.

        Args:
            event: The event.

        Returns:
            For a subagent tool, one block with only a heading ("Tool call N: Agent (starts
            subagent K)"), so that the subagent's blocks below have a start; no block for any
            other call. The input (the task, a command, an edit) is never written.
        """
        number, _ = self._calls.get(event.tool_use_id, (len(self._calls) + 1, ""))
        self._calls[event.tool_use_id] = (number, one_line(event.name, 80))
        if event.name not in AGENT_TOOLS:
            return []
        _, depth = self._agent(event.agent_id)
        self._agents.setdefault(event.tool_use_id, (len(self._agents) + 1, depth + 1))
        title = self._call_title(event.tool_use_id, event.name)
        return [Block(self._heading(event.agent_id, title), "", fixed=True)]

    def _check(self, event: CheckResult) -> Block:
        """Render a checked step: the checker's summary of it and the decision.

        Args:
            event: The event.

        Returns:
            The block: "Tool call N: <tool>: <DECISION>" (for a subagent's result "Result
            handed back by tool call N (<tool>): <DECISION>"); quoted, the checker's summary
            of the step, and its reason and comment to Claude where there is one.
        """
        if event.route is Route.SUBAGENT:
            title = f"result handed back by {self._call(event.tool_use_id, event.tool_name)}"
        else:
            title = self._call_title(event.tool_use_id, event.tool_name)
        heading = self._heading(event.agent_id, f"{title}: {event.decision.value}")
        parts: list[str] = []
        for label, text in (
            ("Summary", event.step_summary),
            ("Reason", event.reason),
            ("Comment to Claude", event.comment),
        ):
            if text.strip():
                parts.append(f"{label}:\n{quote(text)}")
        if not parts:
            return Block(heading, "_No summary._", fixed=True)
        return Block(heading, "\n\n".join(parts))

    def _escalation(self, event: EscalationRequest) -> Block:
        """Render a tool call that waits for a person.

        Args:
            event: The event.

        Returns:
            The block: the reason quoted and, for a question of the agent, its questions. The
            step as the checker saw it (`rendered`) is not part of the trace.
        """
        call = self._call(event.tool_use_id, event.tool_name)
        number = one_line(event.escalation_id, 40)
        self._escalations[event.escalation_id] = (event.agent_id, bool(event.questions))
        if event.questions:
            heading = f"escalation {number}: Claude asks the user ({call})"
        else:
            heading = f"escalation {number}: {call} waits for a person"
        parts = [f"Reason:\n{quote(event.reason)}"]
        questions = render_questions(event.questions)
        if questions:
            parts.append(questions)
        return Block(self._heading(event.agent_id, heading), "\n\n".join(parts))

    def _resolved(self, event: EscalationResolved, resolved_by: str) -> Block:
        """Render the end of an escalation.

        Args:
            event: The event.
            resolved_by: Who decided, or "".

        Returns:
            The block: the outcome in the heading; who decided, and the note quoted.
        """
        agent_id, question = self._escalations.pop(event.escalation_id, (None, False))
        answered = question and event.decision != "cancelled"
        outcome = (
            "answered"
            if answered
            else {
                "allow": "allowed",
                "deny": "denied",
                "cancelled": "ended without a decision",
            }[event.decision]
        )
        parts: list[str] = []
        if event.decision != "cancelled":
            who = one_line(resolved_by)
            parts.append(f"Decided by: `{who}`" if who else "Decided by: not recorded")
        if event.note and event.note.strip():
            label = "Answer, as Claude received it" if answered else "Note"
            parts.append(f"{label}:\n{quote(event.note)}")
        number = one_line(event.escalation_id, 40)
        return Block(
            self._heading(agent_id, f"escalation {number}: {outcome}"),
            "\n\n".join(parts),
            fixed=not (event.note and event.note.strip()),
        )

    def _turn_end(self, event: TurnResult) -> Block:
        """Render the end of a turn.

        Args:
            event: The event.

        Returns:
            The block: whether the turn ended with an error, and how many background tasks
            still run. Claude's final answer is in the blocks before it.
        """
        self._in_turn = False
        turn = max(self._turn, 1)
        self._turn = turn
        lines = [
            f"Result: error (`{one_line(event.subtype, 60)}`)"
            if event.is_error
            else "Result: success"
        ]
        if event.background_tasks:
            lines.append(f"Background tasks still running: {event.background_tasks}")
        heading = f"### End of turn {turn}" if self.turn_numbers else "### End of turn"
        return Block(heading, "\n\n".join(lines), fixed=True)


def user_component(user: str) -> str:
    """Return a user name as one safe path component.

    Args:
        user: The OS user's login name, or its uid as text.

    Returns:
        The name with every character outside `[A-Za-z0-9._-]` replaced by "_", without
        leading dots, at most 64 characters; "unknown" if nothing is left.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", user).lstrip(".")[:64]
    return safe or "unknown"


def session_directory(
    workspace: Path, directory: str, started: datetime, user: str, layout: str = "date/user"
) -> Path:
    """Return the directory a session's trace file goes to.

    Args:
        workspace: The project directory.
        directory: `traces.directory`, relative to the project.
        started: When the session started; its date names a folder in the layouts with a
            date (the caller gives it in UTC; it is not converted here).
        user: The OS user Claude Code runs as.
        layout: `traces.layout`, one of `LAYOUTS`; an unknown value counts as "date/user".

    Returns:
        "date/user": `<workspace>/<directory>/<YYYY-MM-DD>/<user>`; "user":
        `<workspace>/<directory>/<user>`; "date": `<workspace>/<directory>/<YYYY-MM-DD>`;
        "flat": `<workspace>/<directory>`.
    """
    root = workspace / directory
    date, name = started.strftime("%Y-%m-%d"), user_component(user)
    match layout:
        case "user":
            return root / name
        case "date":
            return root / date
        case "flat":
            return root
        case _:
            return root / date / name


class TraceFileError(Exception):
    """A trace file could not be created; the message says why."""


def create_trace_file(workspace: Path, target: Path, random_name: bool = False) -> tuple[Path, int]:
    """Create the next trace file of a directory and open it for appending.

    The file is `session_NNN.md` with NNN one above the highest number in `target` (001 in an
    empty directory; four digits after 999), or with `random_name` `session_<12 random hex
    digits>.md`. It is created with an exclusive open, so two sessions that start at the same
    moment get two files.

    Side effects: creates `target` with its parents, and the file (mode 0644, less the umask).
    Nothing is created when the deepest existing directory of `target` lies outside the
    project.

    Args:
        workspace: The project directory; `target` must lie inside it once links are
            resolved.
        target: The directory for the file (`session_directory`).
        random_name: True for a random name (`traces.file_names` = "random"), False for the
            next number.

    Returns:
        (the file's path, a file descriptor open for appending). The caller closes it.

    Raises:
        TraceFileError: If the directory, with its links resolved, is not inside the project
            directory, or the directory or the file cannot be created.
    """
    root = workspace.resolve()

    def refuse_outside(directory: Path) -> None:
        """Refuse a directory that lies outside the project once its links are resolved.

        Args:
            directory: An existing directory.

        Raises:
            TraceFileError: If it lies outside `root`.
        """
        real = directory.resolve()
        if real != root and root not in real.parents:
            raise TraceFileError(f"{target} leads out of the project directory ({real})")

    try:
        # The deepest directory that exists is checked before anything is created, so that a
        # link in the project cannot make calu create directories outside it; the target is
        # checked again once it exists.
        existing = target
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        refuse_outside(existing)
        target.mkdir(parents=True, exist_ok=True)
        refuse_outside(target)
        number = 0
        if not random_name:
            for entry in os.listdir(target):
                found = FILE_NAME.match(entry)
                if found and found.group(1):
                    number = max(number, int(found.group(1)))
        while True:
            number += 1
            name = secrets.token_hex(RANDOM_NAME_BYTES) if random_name else f"{number:03d}"
            path = target / f"session_{name}.md"
            try:
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW,
                    0o644,
                )
            except FileExistsError:
                continue
            return path, fd
    except OSError as exc:
        raise TraceFileError(f"cannot create a trace file in {target}: {exc}") from exc


def find_trace_of(root: Path, claude_session_id: str, skip: Path | None = None) -> Path | None:
    """Find the newest trace file that records a Claude session.

    Reads at most `SEARCHED_FILES` files below `root`, newest first, and of each at most
    `SEARCHED_BYTES`.

    Args:
        root: The trace directory (`<workspace>/<traces.directory>`).
        claude_session_id: Claude Code's session id.
        skip: A file to leave out (the one being written), or None.

    Returns:
        The file whose text holds `session_marker(claude_session_id)` and that was changed
        last; None if there is none, or the directory cannot be read.
    """
    marker = session_marker(claude_session_id).encode()
    candidates: list[tuple[float, Path]] = []
    try:
        # Every layout's depth: the layout may have changed between two sessions.
        for pattern in TRACE_GLOBS:
            for path in root.glob(pattern):
                if path != skip and FILE_NAME.match(path.name) and path.is_file():
                    candidates.append((path.stat().st_mtime, path))
    except OSError:
        return None
    for _, path in sorted(candidates, reverse=True)[:SEARCHED_FILES]:
        try:
            with open(path, "rb") as file:
                if marker in file.read(SEARCHED_BYTES):
                    return path
        except OSError:
            continue
    return None


@dataclass
class Counted:
    """How many blocks a trace wrote, withheld, left out and let the redaction agent remove."""

    written: int = 0
    withheld: int = 0
    left_out: int = 0
    removed: int = 0
    """Messages of the user that the redaction agent left out as a whole (D245)."""
    failures: list[str] = field(default_factory=list)
    """The reasons of the last failed redactions, newest last (at most ten)."""
