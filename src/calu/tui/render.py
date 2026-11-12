"""Pure functions from the model to Rich `Text` lines.

Nothing here imports a widget: every function takes model objects, a `Palette` and a width and
returns the lines to show, so the output is tested as plain text. The widgets of
`calu.tui.widgets` turn the lines into what Textual draws.

Layout of a step line (the columns are fixed, so the summaries of consecutive steps align):

    > ESCALATE  Bash   force push of main to origin                    waiting 0:42
    │ │         │      │                                                         │
    │ │         │      └ what the checker says the step does (cut, never wrapped)
    │ │         └ tool name, 6 columns
    │ └ verdict badge, 9 columns
    └ cursor gutter

A line under it carries what Claude was told about the step: the checker's note, the reason of
a denial, the reason of an escalation, the answer to a question.
"""

import difflib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from rich.console import Console
from rich.markdown import Heading, Markdown
from rich.text import Text
from rich.theme import Theme

from calu.tui.capabilities import NOT_AVAILABLE, Capabilities
from calu.tui.model import (
    AGENT_TOOLS,
    ALL,
    MAIN,
    Agent,
    Block,
    Decision,
    Diff,
    JoinBlock,
    Limit,
    SessionModel,
    StepBlock,
    TextBlock,
    ThinkingBlock,
    TurnEndBlock,
    UserBlock,
)
from calu.tui.theme import Palette

BADGE_WIDTH = 9
"""Columns of the verdict badge."""

TOOL_WIDTH = 6
"""Columns of the tool name."""

LABEL_COLUMN = 4
"""Column of the word (`note`, `reason`, ...) of a line under a step."""

TEXT_COLUMN = 2 + BADGE_WIDTH + 1
"""Column where the tool name of a step and the text of the lines under it start."""

DIFF_LINES = 30
"""How many lines of a diff an expanded step shows before it points to the rest."""

RESULT_LINES = 14
"""How many lines of a tool result an expanded step shows."""

TOOL_SHORT = {
    "AskUserQuestion": "Ask",
    "TodoWrite": "Todo",
    "NotebookEdit": "NbEdit",
    "WebFetch": "Fetch",
    "WebSearch": "Search",
    "MultiEdit": "Edit",
    "SendMessage": "Send",
}
"""Tool names that do not fit the tool column, with what the column shows for them."""

LIMIT_NAMES = {
    "five_hour": "5h",
    "seven_day": "week",
    "seven_day_opus": "week opus",
    "seven_day_sonnet": "week sonnet",
    "overage": "extra",
}
"""Short names of the usage limits of a Claude Code account."""

_MEASURE = Console(
    file=io.StringIO(), width=200, color_system="truecolor", force_terminal=True, emoji=False
)
"""A console that is never written to the terminal; it wraps texts and renders Markdown."""


class _LeftHeading(Heading):
    """A Markdown heading that is never centred: a transcript reads down one left edge."""

    LEVEL_ALIGN: ClassVar[dict[str, Any]] = dict.fromkeys(
        ("h1", "h2", "h3", "h4", "h5", "h6"), "left"
    )


class _TranscriptMarkdown(Markdown):
    """Rich's Markdown with left-aligned headings."""

    elements: ClassVar[dict[str, Any]] = {**Markdown.elements, "heading_open": _LeftHeading}


@dataclass
class View:
    """What a rendering depends on besides the model."""

    palette: Palette
    width: int = 80
    now: float | None = None
    """The client clock; None renders no waiting time."""
    tab: str = MAIN
    cursor: int | None = None
    """Id of the block the transcript cursor is on, if the transcript has the focus."""
    expanded: frozenset[int] = frozenset()
    """Ids of the steps shown with their details."""
    thinking_open: bool = True
    """Whether thinking is shown in full; False shows one line per thinking block."""
    toggled: frozenset[int] = field(default_factory=frozenset)
    """Ids of thinking blocks whose state is the opposite of `thinking_open`."""
    dark: bool = True


# --------------------------------------------------------------------------------- small texts


def clock_text(seconds: float) -> str:
    """Format a waiting or working time.

    Args:
        seconds: The time in seconds; negative values count as 0.

    Returns:
        `m:ss`, or `h:mm:ss` from one hour on.
    """
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def duration_text(seconds: float) -> str:
    """Format how long a step or a turn took.

    Args:
        seconds: The duration in seconds.

    Returns:
        `0.2s` below ten seconds, `42s` below a minute, then `1m12s`.
    """
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def cost_text(cost: float | None) -> str:
    """Format a cost in dollars.

    Args:
        cost: The amount, or None.

    Returns:
        `$0.84`, or "" for None.
    """
    return "" if cost is None else f"${cost:.2f}"


def cut(text: str, width: int) -> str:
    """Put a text on one line and cut it to a width.

    Args:
        text: The text.
        width: Maximum number of characters; at least 1.

    Returns:
        The text with runs of whitespace as one space, ending in `…` if it was cut.
    """
    joined = " ".join(text.split())
    width = max(1, width)
    return joined if len(joined) <= width else joined[: width - 1] + "…"


def clip(text: str, width: int) -> str:
    """Cut one line of code or output to a width, keeping its indentation.

    Args:
        text: The line; tabs count as four spaces.
        width: Maximum number of characters; at least 1.

    Returns:
        The line, ending in `…` if it was cut.
    """
    line = text.expandtabs(4).rstrip()
    width = max(1, width)
    return line if len(line) <= width else line[: width - 1] + "…"


def wrap(text: Text, width: int) -> list[Text]:
    """Wrap a text to a width.

    Args:
        text: The text; newlines in it start new lines.
        width: The width in columns; at least 8 is used.

    Returns:
        The lines, at least one.
    """
    lines = list(text.wrap(_MEASURE, max(8, width)))
    return lines or [Text("")]


def markdown_lines(source: str, width: int, palette: Palette, dark: bool = True) -> list[Text]:
    """Render Markdown to lines.

    Args:
        source: The Markdown text.
        width: The width in columns.
        palette: The colours; ordinary text gets `palette.fg`.
        dark: Whether the theme is dark, for the colours of code.

    Returns:
        The rendered lines without trailing spaces; one empty line for an empty text.
    """
    markdown = _TranscriptMarkdown(
        source, code_theme="ansi_dark" if dark else "ansi_light", hyperlinks=False
    )
    options = _MEASURE.options.update(width=max(8, width), height=None)
    lines: list[Text] = []
    colours = Theme(
        {
            "markdown.code": f"bold {palette.cyan}",
            "markdown.link": palette.accent,
            "markdown.link_url": palette.accent,
            "markdown.item.bullet": palette.muted,
            "markdown.item.number": palette.muted,
            "markdown.block_quote": palette.muted,
            "markdown.hr": palette.faint,
            "markdown.h1": f"bold {palette.fg}",
            "markdown.h2": f"bold {palette.fg}",
            "markdown.h3": f"bold {palette.fg}",
        }
    )
    with _MEASURE.use_theme(colours):
        rendered = _MEASURE.render_lines(markdown, options, pad=False)
    for segments in rendered:
        line = Text(style=palette.fg)
        for segment in segments:
            if not segment.control:
                line.append(segment.text, segment.style)
        line.rstrip()
        lines.append(line)
    while lines and not lines[-1].plain:
        lines.pop()
    return lines or [Text("")]


def tool_label(tool: str) -> str:
    """Fit a tool name into the tool column.

    Args:
        tool: The tool name, e.g. "Bash" or "mcp__github__create_issue".

    Returns:
        At most `TOOL_WIDTH` characters: a known short name, the last part of an MCP tool
        name, or the name cut with `…`.
    """
    name = TOOL_SHORT.get(tool, tool)
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    return name if len(name) <= TOOL_WIDTH else name[: TOOL_WIDTH - 1] + "…"


def step_title(step: StepBlock) -> str:
    """Say what a step does in one line.

    Args:
        step: The step.

    Returns:
        The checker's summary; without one the command, the file path, the description or the
        pattern of the tool input, or "".
    """
    if step.summary:
        return step.summary
    for key in ("command", "file_path", "description", "pattern", "url", "query", "skill"):
        value = step.input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def badge(step: StepBlock, palette: Palette) -> Text:
    """Render the verdict badge of a step.

    Args:
        step: The step.
        palette: The colours.

    Returns:
        The verdict word, `BADGE_WIDTH` columns wide: PASS muted, ALLOW green, DENY red,
        ESCALATE orange; `check…` while no check was reported for a running step.
    """
    word = step.verdict
    if step.waiting == "open" and step.tool == "AskUserQuestion":
        word = "QUESTION"
    styles = {
        "PASS": palette.muted,
        "ALLOW": palette.green,
        "ALLOWED": palette.green,
        "DENY": f"bold {palette.red}",
        "DENIED": f"bold {palette.red}",
        "ESCALATE": f"bold {palette.orange}",
        "QUESTION": f"bold {palette.orange}",
        "ANSWERED": palette.muted,
        "CANCELLED": palette.muted,
        "RESOLVED": palette.muted,
    }
    if not word:
        word, style = ("check…", palette.cyan) if step.result is None else ("", palette.muted)
    else:
        style = styles.get(word, palette.muted)
    return Text(f"{word:<{BADGE_WIDTH}}", style=style)


# ----------------------------------------------------------------------------------- the blocks


def _right_of(step: StepBlock, view: View) -> Text:
    """Render the right-aligned part of a step line.

    Args:
        step: The step.
        view: Colours and clock.

    Returns:
        The waiting time of an open escalation, else the size of the diff, else the duration,
        else `running` for a step that was let through and has no result yet; "" otherwise,
        and for a step of the stored history, of which only the verdict is known.
    """
    palette = view.palette
    if step.waiting == "open":
        if step.opened_at is not None and view.now is not None:
            return Text(f"waiting {clock_text(view.now - step.opened_at)}", palette.orange)
        return Text("waits for you", palette.orange)
    if step.diffs:
        added = sum(diff.counts()[0] for diff in step.diffs)
        removed = sum(diff.counts()[1] for diff in step.diffs)
        return Text.assemble((f"+{added}", palette.green), " ", (f"-{removed}", palette.red))
    if step.duration is not None and step.verdict not in {"DENY", "DENIED", "CANCELLED"}:
        return Text(duration_text(step.duration), palette.muted)
    if step.result is None and not step.stored and step.verdict in {"PASS", "ALLOW", "ALLOWED"}:
        return Text("running", palette.accent)
    return Text("")


def _under(label: str, text: str, style: str, view: View, label_style: str = "") -> Text:
    """Render one line under a step: a word in the badge column and a text beside it.

    Args:
        label: The word, e.g. "note" or "reason".
        text: The text; it is cut to the width, never wrapped.
        style: Style of the text.
        view: Width and colours.
        label_style: Style of the word; the muted colour if empty.

    Returns:
        The line.
    """
    line = Text(" " * LABEL_COLUMN)
    line.append(f"{label:<{TEXT_COLUMN - LABEL_COLUMN}}", label_style or view.palette.muted)
    line.append(cut(text, view.width - TEXT_COLUMN - 1), style)
    return line


def step_lines(step: StepBlock, model: SessionModel, view: View) -> list[Text]:
    """Render a step: its line, the lines under it, and its details if it is expanded.

    Args:
        step: The step.
        model: The model, for the subagent an Agent call started.
        view: Width, colours, cursor, clock and which steps are expanded.

    Returns:
        The lines, each at most `view.width` columns.
    """
    palette = view.palette
    waiting = step.waiting == "open"
    line = Text(">" if view.cursor == step.id else " ", style=f"bold {palette.accent}")
    line.append(" ")
    if view.tab == ALL:
        line.append(f"{cut(model.label(step.agent_id), 9):<10}", palette.muted)
    line.append_text(badge(step, palette))
    line.append(" ")
    line.append(f"{tool_label(step.tool):<{TOOL_WIDTH}}", f"bold {palette.fg}")
    line.append(" ")
    right = _right_of(step, view)
    room = view.width - line.cell_len - (right.cell_len + 2 if right.plain else 0) - 1
    title = cut(step_title(step), room)
    line.append(title, palette.fg)
    if right.plain:
        line.append(" " * max(1, view.width - line.cell_len - right.cell_len - 1))
        line.append_text(right)
    if waiting:
        line.stylize(f"on {palette.wait_bg}")
        line.pad_right(max(0, view.width - line.cell_len))
        line.stylize(f"on {palette.wait_bg}")
    lines = [line]

    comment = next((check.comment for check in reversed(step.checks) if check.comment), "")
    reason = next((check.reason for check in reversed(step.checks) if check.reason), "")
    if comment:
        lines.append(_under("note", comment, palette.accent, view, palette.accent))
    if step.verdict in {"DENY", "DENIED"} and reason:
        lines.append(_under("reason", reason, palette.muted, view, palette.red))
    if waiting:
        asked = model.decisions.get(step.escalation_id or "")
        text = asked.reason if asked is not None else reason
        if text and not (asked is not None and asked.is_question):
            lines.append(_under("reason", text, palette.muted, view, palette.orange))
    if step.waiting in {"allowed", "denied", "answered"} and step.note:
        label = "answer" if step.waiting == "answered" else "you"
        lines.append(_under(label, step.note, palette.muted, view))
    if step.is_error and step.result and step.verdict not in {"DENY", "DENIED"}:
        lines.append(_under("error", step.result, palette.muted, view, palette.red))
    started = model.agents.get(step.tool_use_id or "") if step.tool in AGENT_TOOLS else None
    if started is not None:
        lines.append(_under("agent", agent_progress(started, model), palette.muted, view))
    if step.id in view.expanded:
        lines += step_details(step, view)
    return lines


def agent_head(agent: Agent, model: SessionModel, view: View) -> list[Text]:
    """Render the two lines above a subagent's transcript: who it is and what it was asked.

    Args:
        agent: The subagent.
        model: The model, for the agent's activity and the Agent call that started it.
        view: Width and colours.

    Returns:
        A line with name, type, background flag and activity, and a line with the task the
        agent was started with (the prompt of its Agent call), cut to the width.
    """
    palette = view.palette
    activity = model.activity_of(agent)
    head = Text.assemble(" ", (agent.description or agent.label, f"bold {palette.fg}"))
    for part in (agent.subagent_type, "background" if agent.background else "foreground"):
        if part:
            head.append(f"  ·  {part}", palette.muted)
    head.append("  ·  ", palette.muted)
    head.append(activity, state_style(activity, palette))
    call = model.step(agent.key)
    prompt = call.input.get("prompt") if call is not None else None
    task = Text.assemble((" task  ", palette.muted))
    task.append(
        cut(prompt, view.width - 9)
        if isinstance(prompt, str) and prompt
        else "not seen by this client",
        palette.fg if prompt else palette.faint,
    )
    return [head, task]


def agent_progress(agent: Agent, model: SessionModel) -> str:
    """Describe a subagent in the line under the Agent call that started it.

    Args:
        agent: The subagent.
        model: The model, for the agent's activity.

    Returns:
        For example `explore: running, 7 steps, last Grep`.
    """
    parts = [model.activity_of(agent)]
    if agent.steps:
        parts.append(f"{agent.steps} step{'s' if agent.steps != 1 else ''}")
    if agent.last_tool:
        parts.append(f"last {agent.last_tool}")
    if agent.background:
        parts.append("background")
    return f"{agent.label}: " + ", ".join(parts)


def diff_lines(diff: Diff, view: View, limit: int | None = DIFF_LINES) -> list[Text]:
    """Render the unified diff of one file.

    Args:
        diff: Content before and after.
        view: Width and colours.
        limit: Maximum number of diff lines; None shows all.

    Returns:
        A header line with the path and the counts, the diff lines (added green, removed red,
        context muted), and a last line that says how many lines were left out.
    """
    palette = view.palette
    added, removed = diff.counts()
    header = Text.assemble(
        ("diff ", palette.muted),
        (diff.path, f"bold {palette.fg}"),
        "  ",
        (f"+{added}", palette.green),
        " ",
        (f"-{removed}", palette.red),
    )
    body = list(
        difflib.unified_diff(
            diff.before.splitlines(),
            diff.after.splitlines(),
            lineterm="",
            n=2,
            fromfile="",
            tofile="",
        )
    )[2:]
    lines = [header]
    for raw in body if limit is None else body[:limit]:
        if raw.startswith("@@"):
            style = palette.cyan
        elif raw.startswith("+"):
            style = palette.green
        elif raw.startswith("-"):
            style = palette.red
        else:
            style = palette.muted
        lines.append(Text(clip(raw, view.width - 2), style))
    if limit is not None and len(body) > limit:
        lines.append(
            Text(f"… {len(body) - limit} more lines (w on a waiting step shows all)", palette.faint)
        )
    return lines


def input_lines(step: StepBlock, view: View) -> list[Text]:
    """Render the input of a tool call.

    Args:
        step: The step.
        view: Width and colours.

    Returns:
        For a command the command after `$`; otherwise one `key: value` line per input field,
        long values cut.
    """
    palette = view.palette
    command = step.input.get("command")
    if isinstance(command, str) and command:
        lines: list[Text] = []
        for index, part in enumerate(command.splitlines() or [""]):
            lines.append(
                Text.assemble(
                    ("$ " if index == 0 else "  ", palette.muted),
                    (clip(part, view.width - 2), palette.fg),
                )
            )
        return lines
    lines = []
    for key, value in step.input.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(
            Text.assemble(
                (f"{key}: ", palette.muted),
                (cut(shown, max(20, view.width - 12 - len(key))), palette.fg),
            )
        )
    return lines


def step_details(step: StepBlock, view: View) -> list[Text]:
    """Render what an expanded step shows under its line.

    Args:
        step: The step.
        view: Width and colours.

    Returns:
        The input, every check with its reason and comment, the result (the first
        `RESULT_LINES` lines), the files and the diffs, each line behind a `│` guide.
    """
    palette = view.palette
    inner = View(**{**view.__dict__, "width": view.width - 6})
    body: list[Text] = [*input_lines(step, inner)]
    for check in step.checks:
        head = Text.assemble(
            ("check ", palette.muted),
            (f"{check.route} ", palette.muted),
            (check.decision.upper(), "bold"),
        )
        if check.reason:
            head.append(f"  {check.reason}", palette.fg)
        body += wrap(head, inner.width)
        if check.comment:
            body += wrap(Text(f"note to Claude: {check.comment}", palette.accent), inner.width)
    if step.result is not None:
        result = step.result.rstrip("\n").splitlines()
        body.append(
            Text(
                "result (error)" if step.is_error else "result",
                palette.red if step.is_error else palette.muted,
            )
        )
        body += [
            Text("  " + clip(row, inner.width - 2), palette.fg) for row in result[:RESULT_LINES]
        ]
        if len(result) > RESULT_LINES:
            body.append(Text(f"  … {len(result) - RESULT_LINES} more lines", palette.faint))
    elif step.waiting != "open":
        body.append(Text("no result yet", palette.muted))
    if step.files:
        body.append(Text("files " + ", ".join(step.files), palette.muted))
    for diff in step.diffs:
        body += diff_lines(diff, inner)
    return [Text.assemble(("    │ ", palette.faint), row) for row in body]


def user_lines(block: UserBlock, view: View) -> list[Text]:
    """Render a message Claude has taken in.

    The user's own message is a block in its own background colour, with a bar in the accent
    colour and the label `you`, so that it is told from Claude's output at a glance. A message
    that calu, Claude Code or another session put into the conversation has the plain
    background and says what it is.

    Args:
        block: The message.
        view: Width and colours.

    Returns:
        The lines, each padded to the full width.
    """
    palette = view.palette
    own = block.source == "user"
    if own:
        label = f"you → {block.relayed_to}" if block.relayed_to else "you"
        bar, background, label_style, text_style = (
            palette.accent,
            palette.user_bg,
            f"bold {palette.accent}",
            f"bold {palette.fg}",
        )
    else:
        label = "other session" if block.source == "cross_session" else "automatic message"
        bar, background, label_style, text_style = (
            palette.faint,
            palette.surface,
            palette.muted,
            palette.muted,
        )
    indent = len(label) + 4
    wrapped = wrap(Text(block.text.strip() or " ", text_style), view.width - indent - 1)
    lines: list[Text] = []
    for index, row in enumerate(wrapped):
        line = Text("▌ ", style=bar)
        line.append(f"{label}  " if index == 0 else " " * (len(label) + 2), label_style)
        line.append_text(row)
        line.pad_right(max(0, view.width - line.cell_len))
        line.stylize(f"on {background}")
        lines.append(line)
    return lines


def thinking_lines(block: ThinkingBlock, view: View) -> list[Text]:
    """Render a thinking text of Claude: dimmed, behind a dotted guide.

    Args:
        block: The thinking block.
        view: Width, colours and whether thinking is shown in full.

    Returns:
        A label line (`thinking`, or `thinking…` while it streams) and the text; one line with
        the number of hidden lines if the block is collapsed.
    """
    palette = view.palette
    style = f"italic {palette.muted}"
    rows = [row for row in block.text.strip().splitlines() if row.strip()] or [""]
    shown = view.thinking_open != (block.id in view.toggled)
    cursor = ">" if view.cursor == block.id else " "
    head = Text.assemble((cursor, f"bold {palette.accent}"), (" ┊ ", palette.faint))
    head.append("thinking…" if block.streaming else "thinking", f"italic {palette.faint}")
    if not shown:
        count = f"{len(rows)} line{'s' if len(rows) != 1 else ''}"
        head.append(f" · {count} · alt+t shows", palette.faint)
        return [head]
    lines = [head]
    for row in rows:
        for part in wrap(Text(row, style), view.width - 5):
            lines.append(Text.assemble(("  ┊ ", palette.faint), part))
    return lines


def text_lines(block: TextBlock, model: SessionModel, view: View) -> list[Text]:
    """Render a text of Claude as Markdown.

    Args:
        block: The text block.
        model: The model, for the agent's name in the view of all agents.
        view: Width and colours.

    Returns:
        The lines, indented by two columns; in the view of all agents a subagent's text is
        preceded by a line with its name.
    """
    palette = view.palette
    lines: list[Text] = []
    if view.tab == ALL and block.agent_id:
        lines.append(Text(f"  {model.label(block.agent_id)}:", palette.muted))
    source = block.text.strip() + (" ▍" if block.streaming else "")
    for index, row in enumerate(markdown_lines(source, view.width - 3, palette, view.dark)):
        gutter = ">" if view.cursor == block.id and index == 0 else " "
        lines.append(Text.assemble((gutter, f"bold {palette.accent}"), " ", row))
    return lines


def turn_lines(block: TurnEndBlock, view: View) -> list[Text]:
    """Render the end of a turn as a thin rule with what the turn cost.

    Args:
        block: The end of the turn.
        view: Width and colours.

    Returns:
        One line: `── turn 14 · $0.06 · 1m12s ───`; for a turn that ended with an error or was
        interrupted the rule says so, and it names background tasks that still run.
    """
    palette = view.palette
    parts = [f"turn {block.number}"]
    if block.cost_delta is not None:
        parts.append(cost_text(block.cost_delta))
    elif block.cost is not None:
        parts.append(f"{cost_text(block.cost)} so far")
    if block.duration is not None:
        parts.append(duration_text(block.duration))
    if block.subtype not in {"success", ""}:
        parts.append(block.subtype.replace("_", " "))
    if block.background_tasks:
        tasks = block.background_tasks
        many = tasks != 1
        parts.append(f"{tasks} background task{'s' if many else ''} still run{'' if many else 's'}")
    label = " " + " · ".join(parts) + " "
    style = palette.red if block.is_error else palette.faint
    line = Text("  ──", style)
    line.append(label, palette.red if block.is_error else palette.muted)
    line.append("─" * max(2, view.width - line.cell_len - 2), style)
    lines = [line]
    if block.is_error and block.result:
        lines.append(Text("  " + cut(block.result, view.width - 4), palette.red))
    return lines


def block_lines(block: Block, model: SessionModel, view: View) -> list[Text]:
    """Render one block of the transcript.

    Args:
        block: The block.
        model: The model it belongs to.
        view: Width, colours, cursor, clock and view state.

    Returns:
        The lines of the block, without the blank line that may separate it from its
        predecessor (`gap_before`).
    """
    match block:
        case UserBlock():
            return user_lines(block, view)
        case TextBlock():
            return text_lines(block, model, view)
        case ThinkingBlock():
            return thinking_lines(block, view)
        case StepBlock():
            return step_lines(block, model, view)
        case TurnEndBlock():
            return turn_lines(block, view)
        case JoinBlock():
            return [Text(f"  ·· {cut(block.text, view.width - 10)} ··", view.palette.faint)]
        case _:
            return []


def gap_before(previous: Block | None, block: Block) -> bool:
    """Tell whether a blank line separates a block from the one before it.

    Args:
        previous: The block shown before, or None for the first one.
        block: The block.

    Returns:
        False for the first block and between two steps; True otherwise, so that steps form
        one dense trail and every other block stands apart.
    """
    if previous is None:
        return False
    return not (isinstance(previous, StepBlock) and isinstance(block, StepBlock))


def block_plain(block: Block) -> str:
    """Return the text a copy of a block puts on the clipboard.

    Args:
        block: The block.

    Returns:
        The message or text itself; for a step its command or input and its result.
    """
    match block:
        case UserBlock() | TextBlock() | ThinkingBlock() | JoinBlock():
            return block.text
        case StepBlock():
            command = block.input.get("command")
            head = command if isinstance(command, str) else json.dumps(block.input, indent=2)
            return head if block.result is None else f"{head}\n\n{block.result}"
        case _:
            return ""


# ------------------------------------------------------------------------- status, tabs, footer

STATE_WORDS = {
    "starting": "starting",
    "idle": "idle",
    "running": "running",
    "checking": "checking",
    "tool_call": "tool call",
    "escalated": "ESCALATED",
    "stopped": "stopped",
    "failed": "FAILED",
}
"""The word the status bar shows for each session state."""


def state_style(state: str, palette: Palette) -> str:
    """Return the style of a session or agent state word.

    Args:
        state: A session state or an agent activity.
        palette: The colours.

    Returns:
        Idle, stopped and completed muted; running and tool call in the accent colour;
        checking cyan; escalated or "waits for you" bold orange; failed bold red.
    """
    word = state.lower()
    if word.startswith(("escalated", "waits")):
        return f"bold {palette.orange}"
    if word.startswith("failed"):
        return f"bold {palette.red}"
    if word.startswith("checking"):
        return palette.cyan
    if word.startswith(("running", "tool", "thinking", "starting")):
        return palette.accent
    return palette.muted


def limit_cell(limit: Limit, palette: Palette) -> Text:
    """Render one usage limit as a short cell.

    Args:
        limit: The limit.
        palette: The colours.

    Returns:
        For example `5h 82%`: muted while allowed, orange close to the limit, red when reached.
    """
    name = LIMIT_NAMES.get(limit.type, limit.type or "limit")
    style = {"allowed_warning": palette.orange, "rejected": f"bold {palette.red}"}.get(
        limit.status, palette.muted
    )
    if limit.utilization is not None:
        return Text(f"{name} {round(limit.utilization * 100)}%", style)
    word = {"allowed": "ok", "allowed_warning": "close", "rejected": "reached"}.get(
        limit.status, ""
    )
    return Text(f"{name} {word}".strip(), style)


def status_line(model: SessionModel, view: View, spinner: str = "") -> Text:
    """Render the status bar.

    Cells that do not fit are left out from the least important one: connection, usage
    limits, context fill, cost, supervision level, model. The state and the number of open
    decisions always stay.

    Args:
        model: The model.
        view: Width, colours and clock.
        spinner: One character shown before the state while the session works; "" for none.

    Returns:
        One line of at most `view.width` columns.
    """
    palette = view.palette
    word = STATE_WORDS.get(model.state, model.state)
    state = Text()
    if spinner and model.working():
        state.append(f"{spinner} ", state_style(model.state, palette))
    state.append(word, state_style(model.state, palette))
    if model.working() and model.state_since is not None and view.now is not None:
        state.append(f" {clock_text(view.now - model.state_since)}", palette.muted)
    name = Text("calu", f"bold {palette.fg}")
    if model.project:
        name.append(f"  {model.project}", palette.muted)
    cells: list[tuple[int, Text]] = [
        (0, name),
        (0, state),
    ]
    open_count = len(model.decisions)
    if open_count:
        cells.append(
            (
                0,
                Text(
                    f"{open_count} need{'s' if open_count == 1 else ''} you",
                    f"bold {palette.orange}",
                ),
            )
        )
    cells.append(
        (5, Text(f"{model.model_name or 'default'} / {model.effort or 'default'}", palette.fg))
    )
    cells.append((4, Text(model.level(), palette.muted)))
    if model.usage.cost is not None:
        turns = model.usage.turns
        cells.append(
            (
                3,
                Text(
                    f"{cost_text(model.usage.cost)} · {turns} turn{'s' if turns != 1 else ''}",
                    palette.muted,
                ),
            )
        )
    if model.usage.context is not None:
        fill = model.usage.context.percent
        style = palette.orange if fill >= 80 else palette.muted
        cells.append((2, Text(f"ctx {fill:.0f}%", style)))
    for limit in model.usage.limits:
        cells.append((1, limit_cell(limit, palette)))
    connection = model.connection
    if connection.status == "attached":
        cells.append((1, Text("attached", palette.muted)))
    elif connection.status == "reconnecting":
        cells.append(
            (0, Text(f"reconnecting {connection.attempt}/{connection.limit}", palette.orange))
        )
    elif connection.status == "detached":
        cells.append((0, Text(cut(f"detached: {connection.reason}", 40), f"bold {palette.red}")))
    else:
        cells.append((1, Text("connecting", palette.muted)))

    def joined(parts: list[tuple[int, Text]]) -> Text:
        """Join the cells with three spaces.

        Args:
            parts: The cells with their priority.

        Returns:
            The line.
        """
        return Text("   ").join(cell for _, cell in parts)

    while joined(cells).cell_len > view.width - 2 and any(priority for priority, _ in cells):
        lowest = min(priority for priority, _ in cells if priority)
        for index in range(len(cells) - 1, -1, -1):
            if cells[index][0] == lowest:
                del cells[index]
                break
    line = Text(" ")
    line.append_text(joined(cells))
    line.truncate(view.width, overflow="ellipsis")
    return line


def tab_cells(model: SessionModel, selected: str) -> list[tuple[str, str]]:
    """List the agent tabs with their labels.

    Args:
        model: The model.
        selected: The key of the selected tab.

    Returns:
        `(key, label)` per tab in display order. A label is the agent's name, `bg` for a
        background subagent, `N!` for N open decisions and `*` for unread activity; a finished
        subagent is marked `done`.
    """
    cells: list[tuple[str, str]] = []
    for key in model.tabs():
        if key == ALL:
            cells.append((key, "all"))
            continue
        agent = model.agents[key]
        label = agent.label or key
        if agent.background:
            label += " bg"
        waiting = len(model.open_decisions(key))
        if waiting:
            label += f" {waiting}!"
        elif key != MAIN and not agent.running:
            label += " ✓" if agent.status == "completed" else f" {agent.status}"
        if agent.unread and key != selected:
            label += " *"
        cells.append((key, label))
    return cells


def tabs_line(
    model: SessionModel, view: View, extra: str = ""
) -> tuple[Text, list[tuple[int, int, str]]]:
    """Render the row of agent tabs.

    Args:
        model: The model.
        view: Width, colours and the selected tab (`view.tab`).
        extra: A text shown right-aligned if there is room (counts of the side panel on a
            narrow screen, the key that switches agents).

    Returns:
        The line and, for mouse clicks, `(first column, end column, tab key)` per tab.
    """
    palette = view.palette
    line = Text(" ")
    spans: list[tuple[int, int, str]] = []
    for key, label in tab_cells(model, view.tab):
        start = line.cell_len
        if key == view.tab:
            line.append(f" {label} ", f"bold {palette.bg} on {palette.accent}")
        else:
            waiting = "!" in label
            agent = model.agents.get(key)
            done = agent is not None and key != MAIN and not agent.running
            style = (
                f"bold {palette.orange}" if waiting else palette.faint if done else palette.muted
            )
            line.append(f" {label} ", style)
        spans.append((start, line.cell_len, key))
        line.append(" ")
    if extra and line.cell_len + len(extra) + 2 <= view.width:
        line.append(" " * (view.width - line.cell_len - len(extra) - 1))
        line.append(extra, palette.faint)
    line.truncate(view.width, overflow="ellipsis")
    return line, spans


# ------------------------------------------------------------------------------ decision card


def card_lines(
    decision: Decision,
    model: SessionModel,
    view: View,
    position: tuple[int, int],
    max_lines: int = 9,
    armed: str = "",
) -> list[Text]:
    """Render the card of the selected open escalation or question.

    Args:
        decision: The open item.
        model: The model, for the agent's name.
        view: Width (of the card's inside), colours and clock.
        position: `(index from 1, number of open items)`.
        max_lines: The most lines the card may take.
        armed: "allow" or "deny" after the first press of the key, when the card asks for the
            second one; "" otherwise.

    Returns:
        The lines: a head with kind, agent, tool and waiting time; the step or the question
        with its options; the reason; the user's note and the session's refusal if any; and
        the keys.
    """
    palette = view.palette
    index, total = position
    kind = "QUESTION" if decision.is_question else "DECISION"
    head = Text.assemble(
        (f"{kind} {index} of {total}", f"bold {palette.orange}"),
        ("  ·  ", palette.faint),
        (model.label(decision.agent_id), f"bold {palette.fg}"),
        ("  ·  ", palette.faint),
        (decision.tool_name, palette.fg),
    )
    if decision.opened_at is not None and view.now is not None:
        waited = f"waiting {clock_text(view.now - decision.opened_at)}"
    else:
        waited = "waiting since before this client attached"
    if head.cell_len + len(waited) + 2 <= view.width:
        head.append(" " * (view.width - head.cell_len - len(waited)))
        head.append(waited, palette.orange)
    lines = [head]
    if decision.is_question:
        lines += _question_lines(decision, view, max_lines - 3)
        keys = "1-9 choose · o own answer · ←/→ question · enter send · alt+d decline · w detail"
        if decision.complete():
            keys = "enter sends the answers · 1-9 change · o own answer · ←/→ question · w detail"
    else:
        step = model.step(decision.tool_use_id)
        what = decision.tool_input.get("command") or decision.tool_input.get("file_path")
        shown = what if isinstance(what, str) and what else (step_title(step) if step else "")
        for row in (shown.splitlines() or [""])[:2]:
            lines.append(Text.assemble("  ", (cut(row, view.width - 4), f"bold {palette.fg}")))
        reason = Text.assemble(("Stopped: ", palette.muted), (decision.reason, palette.fg))
        room = max(1, max_lines - len(lines) - 2)
        wrapped = wrap(reason, view.width - 4)
        if len(wrapped) > room:
            wrapped = wrapped[:room]
            wrapped[-1].truncate(max(1, view.width - 6))
            wrapped[-1].append(" …", palette.muted)
        lines += [Text.assemble("  ", row) for row in wrapped]
        keys = "a a allow · d d deny · n note · w detail (what the checker saw)"
        if total > 1:
            keys += " · ] next"
    if decision.note:
        lines.append(
            Text.assemble(
                ("  note for Claude: ", palette.accent),
                (cut(decision.note, view.width - 22), palette.fg),
            )
        )
    if decision.error:
        lines.append(
            Text(
                "  " + cut(f"the session refused: {decision.error}", view.width - 4),
                f"bold {palette.red}",
            )
        )
    if armed:
        again = "a" if armed == "allow" else "d"
        text = f"press {again} again to {armed} · any other key types"
        if decision.by_rule and armed == "allow":
            text = f"Stopped by a deny rule: {text}"
        else:
            text = text.capitalize()
        style = f"bold {palette.orange if armed == 'allow' else palette.red}"
        lines.append(Text("  " + cut(text, view.width - 4), style))
    else:
        lines.append(Text("  " + cut(keys, view.width - 4), palette.muted))
    return lines


def _question_lines(decision: Decision, view: View, room: int) -> list[Text]:
    """Render the question the card shows: tabs, the question, its options.

    Args:
        decision: The open question set.
        view: Width and colours.
        room: The most lines to use.

    Returns:
        The lines.
    """
    palette = view.palette
    lines: list[Text] = []
    answers = decision.answers()
    if len(decision.questions) > 1:
        tabs = Text("  ")
        for number, question in enumerate(decision.questions):
            mark = " ✓" if answers[number] else ""
            style = (
                f"bold {palette.bg} on {palette.accent}"
                if number == decision.current
                else palette.green
                if answers[number]
                else palette.muted
            )
            tabs.append(f" {question.header}{mark} ", style)
            tabs.append(" ")
        tabs.append(f"  {decision.answered()} of {len(decision.questions)} answered", palette.muted)
        lines.append(tabs)
    question = decision.questions[decision.current]
    many = "  (several may be chosen)" if question.multi else ""
    lines.append(
        Text.assemble(
            "  ",
            (cut(question.question, view.width - 4 - len(many)), f"bold {palette.fg}"),
            (many, palette.muted),
        )
    )
    picked = decision.picks[decision.current]
    for number, option in enumerate(question.options[:9]):
        chosen = number in picked
        box = ("[x]" if chosen else "[ ]") if question.multi else ("(•)" if chosen else "( )")
        row = Text.assemble(
            (f"   {number + 1} ", palette.accent),
            (f"{box} ", palette.green if chosen else palette.muted),
            (option.label, f"bold {palette.fg}" if chosen else palette.fg),
        )
        if option.description:
            row.append(
                f"  {cut(option.description, max(8, view.width - row.cell_len - 4))}", palette.muted
            )
        lines.append(row)
    own = decision.free[decision.current]
    if own:
        lines.append(
            Text.assemble(
                ("   o ", palette.accent),
                ("your answer: ", palette.muted),
                (cut(own, view.width - 22), f"bold {palette.fg}"),
            )
        )
    return lines[: max(2, room)]


# --------------------------------------------------------------------------------- side panel


def _section(title: str, count: int | None, view: View, extra: str = "") -> Text:
    """Render the title line of a section of the side panel.

    Args:
        title: The title in capitals.
        count: The number of entries, or None for a section without a count.
        view: Colours.
        extra: A muted word after the count.

    Returns:
        The line.
    """
    palette = view.palette
    line = Text(title, f"bold {palette.fg}" if count else palette.muted)
    if count is not None:
        line.append(f" ({count})", palette.muted)
    if extra:
        line.append(f"  {extra}", palette.muted)
    return line


def panel_lines(
    model: SessionModel,
    view: View,
    caps: Capabilities,
    selected: str | None = None,
    held: Sequence[str] = (),
    focused: bool = False,
) -> tuple[list[Text], dict[int, tuple[str, str]]]:
    """Render the supervision panel: what needs the user, the agents, the queue, the session.

    Args:
        model: The model.
        view: Width (of the panel's inside), colours, clock and the selected tab.
        caps: What the session offers; a missing function is named as not available.
        selected: The id of the escalation the decision card shows.
        held: The drafts this client holds back until the session is idle, oldest first.
        focused: Whether the panel has the focus; each section then names its keys.

    Returns:
        The lines, and a map from line index to what the line stands for: `("decision", id)`,
        `("agent", key)`, `("queue", message id)`, `("held", index)` or `("mark", id)`. A
        click or the panel's cursor selects by it.
    """
    palette = view.palette
    width = view.width
    lines: list[Text] = []
    targets: dict[int, tuple[str, str]] = {}
    waiting = model.open_decisions()
    lines.append(_section("NEEDS YOU", len(waiting), view))
    for number, decision in enumerate(waiting, start=1):
        mark = ">" if decision.escalation_id == selected else " "
        waited = (
            clock_text(view.now - decision.opened_at)
            if decision.opened_at is not None and view.now is not None
            else ""
        )
        head = Text.assemble(
            (mark, f"bold {palette.orange}"),
            (f"{number} ", palette.orange),
            (f"{cut(model.label(decision.agent_id), 10)} ", f"bold {palette.fg}"),
            (cut("Question" if decision.is_question else decision.tool_name, 10), palette.fg),
        )
        if waited:
            head.append(" " * max(1, width - head.cell_len - len(waited)))
            head.append(waited, palette.orange)
        targets[len(lines)] = ("decision", decision.escalation_id)
        lines.append(head)
        about = decision.questions[0].question if decision.is_question else decision.reason
        targets[len(lines)] = ("decision", decision.escalation_id)
        lines.append(Text("   " + cut(about, width - 4), palette.muted))
    lines.append(Text(""))

    subagents = [agent for agent in model.agents.values() if agent.key != MAIN]
    lines.append(_section("AGENTS", len(model.agents), view, "alt+←/→"))
    for agent in model.agents.values():
        activity = model.activity_of(agent)
        mark = ">" if agent.key == view.tab else " "
        row = Text.assemble(
            (mark, f"bold {palette.accent}"), (f"{cut(agent.label, 12):<12} ", f"bold {palette.fg}")
        )
        tail = " bg" if agent.background else ""
        row.append(
            cut(activity, max(4, width - row.cell_len - len(tail))), state_style(activity, palette)
        )
        row.append(tail, palette.muted)
        targets[len(lines)] = ("agent", agent.key)
        lines.append(row)
    if not subagents:
        lines.append(Text("  no subagents", palette.faint))
    lines.append(Text(""))

    lines.append(_section("QUEUE", len(model.queue), view))
    for entry in model.queue:
        delivery = "next  " if entry.delivery == "inject" else "queued"
        row = Text.assemble(
            (f" {delivery} ", palette.accent if entry.delivery == "inject" else palette.muted),
            (f"→ {cut(model.label(None if entry.to == MAIN else entry.to), 8)} ", palette.muted),
        )
        row.append(cut(" ".join(entry.text.split()), max(4, width - row.cell_len)), palette.fg)
        targets[len(lines)] = ("queue", entry.id)
        lines.append(row)
    if model.queue:
        hint = "enter text · c copy · r reuse" if focused else "a sent message cannot be withdrawn"
        lines.append(Text(" " + cut(hint, width - 2), palette.faint))
    lines.append(Text(""))

    if held:
        lines.append(_section("HELD", len(held), view, "not sent"))
        for index, draft in enumerate(held):
            row = Text.assemble((" draft  ", palette.orange))
            row.append(cut(" ".join(draft.split()), max(4, width - row.cell_len)), palette.fg)
            targets[len(lines)] = ("held", str(index))
            lines.append(row)
        hint = (
            "enter edit · s send · d delete"
            if focused
            else "sent when the session is idle · lost on detach"
        )
        lines.append(Text(" " + cut(hint, width - 2), palette.faint))
        lines.append(Text(""))

    if model.honesty_mode != "off" or model.marks:
        lines.append(_section("HONESTY", len(model.marks), view, model.honesty_mode))
        for mark_entry in model.marks:
            spot = (
                f"{mark_entry.path}:{mark_entry.symbol}" if mark_entry.symbol else mark_entry.path
            )
            targets[len(lines)] = ("mark", str(mark_entry.id))
            lines.append(Text(" " + cut(spot, width - 2), palette.fg))
            targets[len(lines)] = ("mark", str(mark_entry.id))
            lines.append(Text("   " + cut(mark_entry.reason, width - 4), palette.muted))
        if model.marks and focused:
            lines.append(Text(" " + cut("enter detail · x clear", width - 2), palette.faint))
        lines.append(Text(""))

    lines.append(_section("SESSION", None, view))
    usage = model.usage

    def figure(name: str, value: str, style: str = "") -> Text:
        """Render one `name value` row of the SESSION section.

        Args:
            name: The name of the figure.
            value: Its value.
            style: Style of the value; the ordinary text colour if empty.

        Returns:
            The line.
        """
        return Text.assemble(
            (f" {name:<8} ", palette.muted), (cut(value, width - 11), style or palette.fg)
        )

    missing = NOT_AVAILABLE if width >= len(NOT_AVAILABLE) + 11 else "not available"
    cost = cost_text(usage.cost) or "no turn has ended yet"
    lines.append(
        figure(
            "cost",
            cost
            + (
                f" · {usage.turns} turn{'s' if usage.turns != 1 else ''}"
                if usage.cost is not None
                else ""
            ),
        )
    )
    if not caps.has("context_fill"):
        lines.append(figure("context", missing, palette.faint))
    elif usage.context is None:
        lines.append(figure("context", "not known yet", palette.faint))
    else:
        context = usage.context
        lines.append(
            figure(
                "context",
                f"{context.percent:.0f}% of {context.max_tokens // 1000}k tokens",
                palette.orange if context.percent >= 80 else "",
            )
        )
        lines.append(Text("          " + meter(context.percent / 100, width - 12), palette.accent))
    if not caps.has("rate_limits"):
        lines.append(figure("quota", missing, palette.faint))
    elif not usage.limits:
        lines.append(figure("quota", "no limit reported", palette.faint))
    for limit in usage.limits:
        name = LIMIT_NAMES.get(limit.type, limit.type or "limit")
        style = {"allowed_warning": palette.orange, "rejected": palette.red}.get(limit.status, "")
        if limit.utilization is None:
            word = {
                "allowed": "within the limit",
                "allowed_warning": "close to the limit",
                "rejected": "limit reached",
            }
            lines.append(figure(name, word.get(limit.status, limit.status), style))
        else:
            reset = f", resets {reset_text(limit.resets_at)}" if limit.resets_at else ""
            lines.append(figure(name, f"{round(limit.utilization * 100)}% used{reset}", style))
            lines.append(
                Text("          " + meter(limit.utilization, width - 12), style or palette.accent)
            )
    if usage.background_tasks:
        lines.append(figure("bg tasks", f"{usage.background_tasks} still running"))
    return lines, targets


def start_card_lines(
    model: SessionModel, view: View, next_model: str | None, next_effort: str | None
) -> list[Text]:
    """Render the card shown in place of a decision while the session is stopped or failed.

    Args:
        model: The model.
        view: Width (of the card's inside) and colours.
        next_model: The model the next start asks for; None keeps the configured one.
        next_effort: The effort the next start asks for; None keeps the configured one.

    Returns:
        The lines: the state, what Enter starts (the conversation, model and effort), and the
        commands that start something else.
    """
    palette = view.palette
    failed = model.state == "failed"
    head = Text(
        "SESSION FAILED" if failed else "SESSION STOPPED",
        f"bold {palette.red if failed else palette.orange}",
    )
    head.append("  ·  calu keeps running, Claude does not", palette.muted)
    resumes = (
        f"resumes the conversation {model.claude_session_id[:8]}"
        if model.claude_session_id
        else "starts a new conversation"
    )
    chosen = f"{next_model or model.model_name or 'default'} / " + (
        next_effort or model.effort or "default"
    )
    lines = [
        head,
        Text.assemble(
            ("  enter", f"bold {palette.fg}"),
            (f" {resumes} with ", palette.muted),
            (chosen, f"bold {palette.fg}"),
        ),
        *[
            Text.assemble("  ", row)
            for row in wrap(
                Text(
                    "/model and /effort choose for that start · /sessions resumes another "
                    "conversation · /start new begins a new one",
                    palette.muted,
                ),
                view.width - 4,
            )
        ],
    ]
    return lines


def reset_text(moment: datetime) -> str:
    """Say when a usage limit starts again.

    Args:
        moment: The time, with its time zone.

    Returns:
        Weekday and time in UTC, e.g. `Thu 14:00 UTC`.
    """
    return f"{moment:%a %H:%M} UTC"


def meter(share: float, width: int) -> str:
    """Draw a bar for a used share.

    Args:
        share: The used part, 0.0 to 1.0 (clamped).
        width: The width of the bar in columns; at least 4 is used.

    Returns:
        `width` characters: `━` for the used part and `╌` for the rest.
    """
    width = max(4, width)
    used = round(min(1.0, max(0.0, share)) * width)
    return "━" * used + "╌" * (width - used)


def side_lines(model: SessionModel, view: View) -> list[Text]:
    """Render the side questions of this client and their answers.

    Args:
        model: The model.
        view: Width (of the panel's inside) and colours.

    Returns:
        A title, a line that says what a side question is, and per exchange the question (in
        the colours of the user's input) and the answer as Markdown, an `asking…` line while
        it is pending, or the reason no answer came.
    """
    palette = view.palette
    width = view.width
    lines = [Text("SIDE QUESTIONS", f"bold {palette.fg}")]
    lines += wrap(
        Text(
            "Answered beside the running work. Not part of the conversation: Claude's work "
            "goes on and does not see them.",
            palette.faint,
        ),
        width,
    )
    if not model.side:
        lines.append(Text(""))
        lines += wrap(
            Text("Type a question below, or /btw <question> anywhere.", palette.muted), width
        )
    for exchange in model.side:
        lines.append(Text(""))
        lines += user_lines(
            UserBlock(id=-1, text=exchange.question), View(**{**view.__dict__, "width": width})
        )
        if exchange.pending:
            lines.append(Text("  asking…", f"italic {palette.accent}"))
        elif exchange.error:
            lines += wrap(Text(f"  no answer: {exchange.error}", palette.red), width)
        else:
            lines += [
                Text.assemble("  ", line)
                for line in markdown_lines(exchange.answer, width - 2, palette, view.dark)
            ]
    return lines
