"""The full-screen view of one escalation: everything the checker saw and said.

The decision card shows what a step is and why it was stopped. This screen adds what is needed
to **judge** it, from `escalation.context`: the step as the checker saw it, the complete diff
of an edit, each checker run with its model, decision and reason, and the conversation and the
chat summary as they were when the step was checked. The key that opens it (`w`) closes it
again; the step can be allowed or denied from here with the same keys as on the card. The
keys act on the escalation this screen was opened for and on no other. A printable key that
is no key of this screen returns to the session and is typed into the input.
"""

from typing import Any

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from calu.tui import render
from calu.tui.model import Decision, Diff, SessionModel
from calu.tui.widgets.chrome import KeyLine


def _heading(title: str, view: render.View) -> list[Text]:
    """Render the heading of a section of the detail view.

    Args:
        title: The heading.
        view: Width and colours.

    Returns:
        A blank line and the heading with a rule to the right edge.
    """
    line = Text(f"── {title} ", f"bold {view.palette.accent}")
    line.append("─" * max(2, view.width - line.cell_len - 1), view.palette.faint)
    return [Text(""), line]


def proposed_diffs(decision: Decision, model: SessionModel, files: dict[str, str]) -> list[Diff]:
    """Work out the diffs of an escalated edit.

    Args:
        decision: The open item.
        model: The model; the step's `edit.diff` events are used if it has them.
        files: Content of the file the edit would change, before the change, by path
            (`escalation.context.files`).

    Returns:
        The diffs of the step; without them, for an edit whose file content is known, the
        diff of that file against the content the edit asks for; else an empty list.
    """
    step = model.step(decision.tool_use_id)
    if step is not None and step.diffs:
        return step.diffs
    path = decision.tool_input.get("file_path")
    if not isinstance(path, str):
        return []
    before = files.get(path)
    if before is None and len(files) == 1:
        before = next(iter(files.values()))
    if before is None:
        return []
    content = decision.tool_input.get("content")
    old, new = decision.tool_input.get("old_string"), decision.tool_input.get("new_string")
    if isinstance(content, str):
        return [Diff(path, before, content)]
    if isinstance(old, str) and isinstance(new, str) and old in before:
        count = -1 if decision.tool_input.get("replace_all") else 1
        return [Diff(path, before, before.replace(old, new, count))]
    return []


def detail_lines(
    decision: Decision,
    model: SessionModel,
    context: dict[str, Any] | None,
    view: render.View,
    error: str = "",
) -> list[Text]:
    """Render the detail view of an open escalation or question.

    Args:
        decision: The open item.
        model: The model.
        context: The result of `escalation.context`, or None while it is being fetched or if
            the session did not return it.
        view: Width, colours and clock.
        error: Why the context is missing, if the request failed.

    Returns:
        The lines: what waits and why, the step, the diff, the checker runs, the conversation
        as the checker saw it and the chat summary.
    """
    palette = view.palette
    kind = "Question of the agent" if decision.is_question else "Step waiting for your decision"
    head = Text.assemble(
        (kind, f"bold {palette.orange}"),
        ("  ·  ", palette.faint),
        (model.label(decision.agent_id), f"bold {palette.fg}"),
        ("  ·  ", palette.faint),
        (decision.tool_name, palette.fg),
    )
    if decision.opened_at is not None and view.now is not None:
        waited = f"waiting {render.clock_text(view.now - decision.opened_at)}"
        head.append(" " * max(2, view.width - head.cell_len - len(waited) - 1))
        head.append(waited, palette.orange)
    lines = [head]
    lines += _heading("why it was stopped", view)
    lines += render.wrap(Text(decision.reason or "no reason given", palette.fg), view.width - 2)
    runs_known = (context or {}).get("checker_runs") or []
    if decision.by_rule and not runs_known:
        lines += render.wrap(
            Text("A deny rule stopped this step before any checker ran.", palette.muted),
            view.width - 2,
        )
    elif decision.by_rule:
        lines += render.wrap(Text("A deny rule matched this step.", palette.muted), view.width - 2)
    lines += _heading("the step as the checker saw it", view)
    rendered = (context or {}).get("rendered") or decision.rendered
    shown = render.markdown_lines(str(rendered), view.width - 2, palette, view.dark)
    for index, line in enumerate(shown):
        following = shown[index + 1].plain if index + 1 < len(shown) else ""
        before_code = following != following.rstrip()  # a code line is padded to the width
        if line.plain or not before_code:  # no blank line between the tool name and its code
            lines.append(line)
    if decision.is_question:
        for number, question in enumerate(decision.questions, start=1):
            lines.append(Text(""))
            lines += render.wrap(
                Text(f"{number}. {question.question}", f"bold {palette.fg}"), view.width - 2
            )
            for option in question.options:
                row = Text.assemble("   · ", (option.label, palette.fg))
                if option.description:
                    row.append(f"  {option.description}", palette.muted)
                lines += render.wrap(row, view.width - 2)
    diffs = proposed_diffs(decision, model, dict((context or {}).get("files") or {}))
    if diffs:
        lines += _heading("the change", view)
        for diff in diffs:
            lines += render.diff_lines(diff, view, limit=None)
    if context is None:
        lines += _heading("what the checker knew", view)
        text = f"not available: {error}" if error else "asking the session…"
        lines.append(Text(text, palette.red if error else palette.muted))
        return lines
    runs = context.get("checker_runs") or []
    lines += _heading(f"checker runs ({len(runs)})", view)
    if not runs:
        lines.append(Text("no checker model ran for this step", palette.muted))
    for run in runs:
        head = Text.assemble(
            (str(run.get("checker_name", "")), f"bold {palette.fg}"),
            (f" v{run.get('checker_version', '?')}  ", palette.muted),
            (str(run.get("model", "")), palette.muted),
            "  ",
            (str(run.get("decision", "")).upper(), f"bold {palette.orange}"),
        )
        figures = []
        if run.get("input_tokens") is not None:
            figures.append(f"{run['input_tokens']} in / {run.get('output_tokens') or 0} out tokens")
        if run.get("duration_ms") is not None:
            figures.append(f"{run['duration_ms']} ms")
        if figures:
            head.append("  " + ", ".join(figures), palette.faint)
        lines.append(head)
        lines += [
            Text.assemble("  ", row)
            for row in render.wrap(Text(str(run.get("reason") or ""), palette.fg), view.width - 4)
        ]
    recent = str(context.get("recent") or "").strip()
    lines += _heading("the conversation as the checker saw it", view)
    lines += (
        render.markdown_lines(recent, view.width - 2, palette, view.dark)
        if recent
        else [Text("empty", palette.muted)]
    )
    summary = str(context.get("chat_summary") or "").strip()
    lines += _heading("chat summary (the older turns, folded)", view)
    lines += (
        render.markdown_lines(summary, view.width - 2, palette, view.dark)
        if summary
        else [Text("empty", palette.muted)]
    )
    return lines


class DetailScreen(Screen[None]):
    """Shows one open escalation in full (see the module docstring)."""

    def __init__(self, escalation_id: str, notice: str = "") -> None:
        """Create the screen for one escalation.

        Args:
            escalation_id: The escalation; the screen closes when it is no longer open.
            notice: A sentence shown in the line of keys until the first key.
        """
        super().__init__()
        self.escalation_id = escalation_id
        self.notice = notice
        self.context: dict[str, Any] | None = None
        self.error = ""
        self.text = ""
        """The plain text of the body as last drawn."""

    def compose(self) -> ComposeResult:
        """Build the screen: a scrollable body and one line of keys.

        Yields:
            The body and the key line.
        """
        with VerticalScroll(id="detail-scroll"):
            yield Static("", id="detail-body")
        yield KeyLine("", id="detail-keys")

    def on_mount(self) -> None:
        """Draw what is known and focus the body, so that the scroll keys work."""
        self.query_one("#detail-scroll").focus()
        self.redraw()

    def on_resize(self) -> None:
        """Draw again for the new width."""
        self.redraw()

    def set_context(self, context: dict[str, Any] | None, error: str = "") -> None:
        """Take over the result of `escalation.context`.

        Args:
            context: The result, or None if the request failed.
            error: The session's message for a failed request.
        """
        self.context, self.error = context, error
        self.redraw()

    def redraw(self) -> None:
        """Render the escalation again; close the screen if it is no longer open."""
        app: Any = self.app
        decision = app.model.decisions.get(self.escalation_id)
        if decision is None:
            self.dismiss()
            return
        view = app.view(width=max(30, self.size.width - 4))
        lines = detail_lines(decision, app.model, self.context, view, self.error)
        self.text = "\n".join(line.plain.rstrip() for line in lines)
        self.query_one("#detail-body", Static).update(Group(*lines))
        armed = app.armed if app.armed_for == self.escalation_id else ""
        width = max(20, self.size.width - 2)
        style = app.palette.muted
        if armed in {"allow", "deny"}:
            hints = [(9, f"press {armed[0]} again to {armed}"), (5, "any other key cancels")]
            style = f"bold {app.palette.red if armed == 'deny' else app.palette.orange}"
        elif self.notice:
            hints = [(9, part) for part in self.notice.split(" · ")]
            style = f"bold {app.palette.orange}"
        elif decision.is_question:
            hints = [
                (9, "w or esc back to the card, where the question is answered"),
                (5, "↑↓ pgup pgdn scroll"),
            ]
        else:
            hints = [
                (9, "a,a allow"),
                (9, "d,d deny"),
                (7, "n note first"),
                (8, "w or esc back"),
                (5, "↑↓ pgup pgdn scroll"),
                (3, "typing returns to the input"),
            ]
        self.query_one("#detail-keys", Static).update(
            Text(" " + render.fit_hints(hints, width), style)
        )

    def on_key(self, event: events.Key) -> None:
        """Handle the keys of the detail view.

        `w`, `q` and Escape return. `a` and `d` act on this screen's escalation as on the
        card (two presses), `n` begins a note for it. Any other printable key (but space,
        which scrolls) returns to the session and is typed into the input, so a reply begun
        here is not lost.

        Args:
            event: The key.
        """
        app: Any = self.app
        self.notice = ""
        if event.key in {"w", "escape", "q"}:
            event.stop()
            if app.armed:
                app.disarm()
                self.redraw()
            else:
                self.dismiss()
        elif event.key in {"a", "d"}:
            event.stop()
            app.decision_key(event.key, self.escalation_id, detail=True)
            if self.is_attached:
                self.redraw()
        elif event.key == "n":
            event.stop()
            self.dismiss()
            app.begin_note(self.escalation_id)
        elif event.is_printable and event.character and event.key != "space":
            event.stop()
            app.disarm()
            self.dismiss()
            app.composer.focus()
            app.composer.insert(event.character)
        elif app.armed:
            app.disarm()
            self.redraw()
