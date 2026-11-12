"""The full-screen client of a running session (`calu attach` on a terminal).

    Link (connection worker) ──Change──► CaluApp._changed ──► the widgets render the model
              ▲                                                        │
              └──────── Actions.request ◄── keys, slash commands, palette, clicks

The app holds no session data: `calu.tui.model.SessionModel` does. The app holds what belongs
to this view only -- the selected agent tab, the send mode of the composer, which open decision
the card shows, whether the side questions are open -- and turns keys into calls of
`calu.tui.actions.Actions`.

The screen, top to bottom: status bar, agent tabs, transcript (with the side panel beside it on
a wide terminal), the decision card while something waits for the user, the composer, a line of
keys. Nothing that asks for a decision takes the keyboard away: the card sits between
transcript and composer, the transcript stays scrollable and the composer usable.

Keys while the composer is empty and a decision is open: `a a` allows, `d d` denies (the second
press confirms, any other key types), `n` writes a note first, `w` shows what the checker saw;
for a question `1`-`9` choose and Enter sends. `alt+a`, `alt+d`, `alt+n`, `alt+w` do the same
whatever the composer holds.
"""

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from calu.acp.modes import LADDER
from calu.tui import render
from calu.tui.actions import Actions, Outcome
from calu.tui.capabilities import CAPABILITIES, Capabilities
from calu.tui.commands import COMMANDS, CaluCommands, by_slash, completions
from calu.tui.connection import Connector, Link
from calu.tui.model import (
    ALL,
    MAIN,
    Change,
    Decision,
    SessionModel,
    StepBlock,
    TextBlock,
    TurnEndBlock,
)
from calu.tui.screens.detail import DetailScreen
from calu.tui.screens.text import TextScreen
from calu.tui.theme import CALU_DARK, CALU_LIGHT, DARK, Palette, palette_for
from calu.tui.widgets.chrome import AgentTabs, Line, Lines, NewBelow, Picked
from calu.tui.widgets.composer import Composer
from calu.tui.widgets.transcript import Transcript

WIDE = 110
"""From this width on the side panel is a column; below it opens over the transcript (`f2`)."""

TALL = 30
"""Below this height the decision card and the composer are kept lower."""

FLUSH_S = 0.05
"""Changes of the model are drawn at most once per this many seconds (20 times per second)."""

DOUBLE_ESCAPE_S = 1.0
"""Two presses of Escape within this many seconds interrupt the running turn."""

MODELS = ("default", "fable", "opus", "sonnet", "haiku")
"""Claude Code's model aliases offered by the model picker; `/model <id>` takes any model id."""

EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
"""The efforts a session can be started with."""

SPINNER = "◐◓◑◒"
"""Shown before the state word while the session works, one character per second."""


@dataclass
class TuiOptions:
    """How the client starts."""

    after_seq: int = 0
    """The first subscription continues after this sequence number (0: everything buffered)."""
    show_thinking: bool = True
    """Whether thinking is shown in full at the start; `alt+t` changes it."""
    theme: str = DARK
    bell: bool = True
    """Ring the terminal bell when a new decision waits."""


def _digit_bindings() -> list[BindingType]:
    """Build the bindings `alt+1` to `alt+9` and `alt+0` for the agent tabs.

    Returns:
        One binding per digit; `alt+0` selects the view of all agents.
    """
    return [
        Binding(
            f"alt+{digit}", f"tab_number({digit})", show=False, priority=True, id=f"tab-{digit}"
        )
        for digit in range(10)
    ]


class CaluApp(App[None]):
    """The full-screen client (see the module docstring)."""

    CSS_PATH = "tui.tcss"
    TITLE = "calu"
    COMMANDS: ClassVar = {CaluCommands}
    COMMAND_PALETTE_BINDING = "ctrl+p"
    AUTO_FOCUS = "#composer"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+d", "detach", "detach", show=False, priority=True, id="detach"),
        Binding("escape", "escape", show=False, priority=True, id="escape"),
        Binding("tab", "cycle_mode(1)", show=False, priority=True, id="mode-next"),
        Binding("shift+tab", "cycle_mode(-1)", show=False, priority=True, id="mode-previous"),
        Binding("pageup", "scroll(-1.0)", show=False, priority=True, id="page-up"),
        Binding("pagedown", "scroll(1.0)", show=False, priority=True, id="page-down"),
        Binding("shift+up", "scroll_line(-1)", show=False, priority=True, id="line-up"),
        Binding("shift+down", "scroll_line(1)", show=False, priority=True, id="line-down"),
        Binding("ctrl+home", "top", show=False, priority=True, id="top"),
        Binding("ctrl+end", "follow", show=False, priority=True, id="follow"),
        Binding("alt+left", "agent(-1)", show=False, priority=True, id="agent-previous"),
        Binding("alt+right", "agent(1)", show=False, priority=True, id="agent-next"),
        Binding("ctrl+o", "command('pick_agent')", show=False, priority=True, id="agent-pick"),
        Binding("ctrl+g", "send_other", show=False, priority=True, id="send-other"),
        Binding("ctrl+up", "focus_transcript", show=False, priority=True, id="focus-transcript"),
        Binding("f1", "command('help')", show=False, priority=True, id="help"),
        Binding("f2", "command('panel')", show=False, priority=True, id="panel"),
        Binding("f3", "command('side')", show=False, priority=True, id="side"),
        Binding("alt+a", "decide('allow')", show=False, priority=True, id="allow"),
        Binding("alt+d", "decide('deny')", show=False, priority=True, id="deny"),
        Binding("alt+n", "command('note')", show=False, priority=True, id="note"),
        Binding("alt+w", "command('detail')", show=False, priority=True, id="detail"),
        Binding("alt+t", "command('thinking')", show=False, priority=True, id="thinking"),
        Binding("alt+c", "command('copy')", show=False, priority=True, id="copy"),
        *_digit_bindings(),
    ]

    def __init__(
        self,
        connector: Connector,
        options: TuiOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
        live: bool = True,
    ) -> None:
        """Create the client; `run` or `run_test` starts it.

        Args:
            connector: Opens the connection to the session (`protocol_connector`, or
                `fake_connector` in tests).
            options: How the client starts; the defaults if None.
            clock: The client clock in seconds; tests pass one they control.
            live: False draws every change at once and starts no timer, so that tests and
                screenshots are deterministic.
        """
        super().__init__()
        self.options = options or TuiOptions()
        self.clock = clock
        self.live = live
        self.model = SessionModel()
        self.link = Link(connector, self.model, self._changed, clock, self.options.after_seq)
        self.actions = Actions(self.link, self.model)
        self.palette: Palette = palette_for({})
        self.mode = "chat"
        """What Enter does with the composer's text: chat, note, answer or side."""
        self.target = MAIN
        """The agent a chat message goes to: `MAIN` or a running background subagent."""
        self.delivery = "queued"
        """How a message to the main agent is delivered: queued (next turn) or inject."""
        self.selected: str | None = None
        """The escalation the card shows; None shows the oldest open one."""
        self.armed = ""
        """"allow" or "deny" after the first press of a decision key; "" otherwise."""
        self.armed_for: str | None = None
        self.message = ""
        self.message_error = False
        self.panel_open = False
        self.side_open = False
        self.next_effort: str | None = None
        """The effort the next `/start` asks for (`/effort`)."""
        self._pending = Change()
        self._draw_pending_scheduled = False
        self._escape_at: float | None = None
        self._tick = 0
        self._usage_asked = False

    # ------------------------------------------------------------------ layout and start

    def compose(self) -> ComposeResult:
        """Build the screen.

        Yields:
            Status bar, agent tabs, the body (transcript, card and composer beside the side
            panel) and the line of keys.
        """
        yield Line("", id="status")
        yield AgentTabs(id="tabs")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Lines(id="agent-head")
                yield Transcript(self.model, self.palette, id="transcript")
                yield NewBelow("", id="new-below")
                yield Lines(id="card")
                yield Line("", id="composer-head")
                with Horizontal(id="composer-row"):
                    yield Static(">", id="prompt")
                    yield Composer(id="composer")
            with VerticalScroll(id="side"):
                yield Lines(id="panel-lines")
                yield Lines(id="side-lines")
        yield Line("", id="footer")

    def on_mount(self) -> None:
        """Register the themes, start the connection and the clock, draw the empty screen."""
        self.register_theme(CALU_DARK)
        self.register_theme(CALU_LIGHT)
        self.transcript.thinking_open = self.options.show_thinking
        self.composer.intercept = self._composer_key
        self.composer.cursor_blink = self.live
        self._apply_theme(self.options.theme)
        self._breakpoints()
        self.run_worker(self._run_link(), name="link", group="link", exit_on_error=False)
        if self.live:
            self.set_interval(1.0, self._second)
        self.render_all()

    async def _run_link(self) -> None:
        """Keep the connection until the user detaches; leave the app if it never opened."""
        await self.link.run()
        if self.link.error is not None:
            self.exit()

    @property
    def transcript(self) -> Transcript:
        """The transcript widget.

        Returns:
            The widget with the id `transcript`.
        """
        return self.query_one("#transcript", Transcript)

    @property
    def composer(self) -> Composer:
        """The composer widget.

        Returns:
            The widget with the id `composer`.
        """
        return self.query_one("#composer", Composer)

    @property
    def capabilities(self) -> Capabilities:
        """What the attached session offers.

        Returns:
            The capabilities derived from the newest snapshot.
        """
        return self.link.capabilities

    def view(self, width: int | None = None) -> render.View:
        """Build the rendering parameters for a widget.

        Args:
            width: The width to render for; the screen width if None.

        Returns:
            The view with the palette, the clock and the selected tab.
        """
        return render.View(
            palette=self.palette,
            width=width if width is not None else self.size.width,
            now=self.clock(),
            tab=self.model.selected_tab,
            dark=self.current_theme.dark,
        )

    def _apply_theme(self, name: str) -> None:
        """Switch the theme and take the colours of the texts from it.

        Args:
            name: A registered theme name; an unknown one keeps the current theme.
        """
        if name in self.available_themes:
            self.theme = name
        self.palette = palette_for(self.get_css_variables())
        self.transcript.palette = self.palette
        self.transcript.dark = self.current_theme.dark
        self.transcript.rebuild()
        self.render_all()

    def _breakpoints(self) -> None:
        """Set the classes the layout depends on: narrow, short, panel and side open."""
        self.set_class(self.size.width < WIDE, "-narrow")
        self.set_class(self.size.height < TALL, "-short")
        self.set_class(self.panel_open, "-panel")
        self.set_class(self.side_open, "-side")
        self.composer.max_lines = 3 if self.size.height < TALL else 8
        self.composer.fit()

    def on_resize(self) -> None:
        """Adapt the layout to the new size."""
        self._breakpoints()
        self.call_after_refresh(self.render_all)

    # ------------------------------------------------------------------ model changes

    def _changed(self, change: Change) -> None:
        """Take a change of the model from the connection and schedule drawing it.

        Called for every event, so it only merges; the drawing happens at most every
        `FLUSH_S` seconds, and a burst of events (a replay) is drawn once.

        Args:
            change: What the event altered.
        """
        self._pending.merge(change)
        if self._draw_pending_scheduled:
            return
        self._draw_pending_scheduled = True
        if self.live:
            self.set_timer(FLUSH_S, self._draw_pending)
        else:
            self.call_next(self._draw_pending)

    def _draw_pending(self) -> None:
        """Draw everything that changed since the last flush."""
        self._draw_pending_scheduled = False
        change, self._pending = self._pending, Change()
        if not self.is_running or not change:
            return
        transcript = self.transcript
        transcript.now = self.clock()
        if change.rebuilt:
            transcript.rebuild()
        else:
            transcript.apply(change.blocks_added, change.blocks_updated)
        if any(isinstance(self.model.blocks[index], TurnEndBlock) for index in change.blocks_added):
            self._usage_asked = False
        if (
            not self._usage_asked
            and self.link.connection is not None
            and self.capabilities.has("usage")
        ):
            self._usage_asked = True
            self._spawn(self._refresh_usage())
        self._check_target()
        self.render_all()
        for notice in change.notices:
            self.notify(notice.text, severity=notice.severity)  # type: ignore[arg-type]
        if change.bell and self.options.bell:
            self.bell()
        screen = self.screen
        if isinstance(screen, DetailScreen) and change.decisions:
            screen.redraw()

    async def _refresh_usage(self) -> None:
        """Ask the session for cost, context fill and usage limits and draw them."""
        await self.actions.refresh_usage()
        self.render_all()

    def _second(self) -> None:
        """Advance what shows a time: the waiting steps, the status bar, the card."""
        self._tick += 1
        if not self.model.decisions and not self.model.working():
            return
        transcript = self.transcript
        transcript.now = self.clock()
        waiting = {
            block.id
            for decision in self.model.decisions.values()
            if (block := self.model.step(decision.tool_use_id)) is not None
        }
        if waiting:
            transcript.refresh_blocks(waiting)
        self._render_status()
        self._render_card()
        self._render_panel()

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        """Run a request beside the UI, so that no key waits for the session.

        Args:
            work: The coroutine to run.
        """
        self.run_worker(work, group="requests", exit_on_error=False)

    # ------------------------------------------------------------------ drawing

    def render_all(self) -> None:
        """Draw every part of the screen except the transcript from the model."""
        if not self.is_running:
            return
        self._render_status()
        self._render_tabs()
        self._render_agent_head()
        self._render_card()
        self._render_panel()
        self._render_side()
        self._render_head()
        self._render_footer()
        self._render_below()

    def _render_status(self) -> None:
        """Draw the status bar."""
        spinner = SPINNER[self._tick % len(SPINNER)] if self.live else "●"
        self.query_one("#status", Line).show(render.status_line(self.model, self.view(), spinner))

    def _render_tabs(self) -> None:
        """Draw the agent tabs; on a narrow screen with what the hidden side panel counts."""
        extra = "alt+←/→ switches"
        if self.size.width < WIDE:
            counts = []
            if self.model.queue:
                counts.append(f"{len(self.model.queue)} queued")
            if self.model.marks:
                counts.append(
                    f"{len(self.model.marks)} mark{'s' if len(self.model.marks) != 1 else ''}"
                )
            extra = " · ".join([*counts, "f2 panel"])
        line, spans = render.tabs_line(self.model, self.view(), extra)
        self.query_one("#tabs", AgentTabs).show(line, spans)

    def _render_agent_head(self) -> None:
        """Draw who a subagent is and what it was asked, above its transcript."""
        head = self.query_one("#agent-head", Lines)
        agent = self.model.agents.get(self.model.selected_tab)
        shown = agent is not None and agent.key != MAIN
        head.set_class(shown, "-shown")
        if shown and agent is not None:
            width = max(30, self.query_one("#main").size.width - 2)
            head.show(render.agent_head(agent, self.model, self.view(width)))

    def current_decision(self) -> Decision | None:
        """Return the open item the card shows.

        Returns:
            The selected one if it is still open, else the oldest open one, else None.
        """
        if self.selected in self.model.decisions:
            return self.model.decisions[self.selected]
        self.selected = None
        return next(iter(self.model.decisions.values()), None)

    def _render_card(self) -> None:
        """Draw the decision card, or hide it when nothing waits."""
        card = self.query_one("#card", Lines)
        decision = self.current_decision()
        card.set_class(decision is not None, "-shown")
        card.set_class(decision is not None and decision.is_question, "-question")
        if decision is None:
            if self.armed:
                self.disarm()
            card.show([])
            return
        waiting = list(self.model.decisions)
        position = (waiting.index(decision.escalation_id) + 1, len(waiting))
        width = max(30, self.query_one("#main").size.width - 6)
        short = self.size.height < TALL
        room = (
            (10 if short else self.size.height // 2 - 2)
            if decision.is_question
            else (6 if short else 9)
        )
        armed = self.armed if self.armed_for == decision.escalation_id else ""
        card.show(render.card_lines(decision, self.model, self.view(width), position, room, armed))

    def _render_panel(self) -> None:
        """Draw the supervision panel."""
        selected = self.current_decision()
        lines, targets = render.panel_lines(
            self.model,
            self.view(self._side_width()),
            self.capabilities,
            selected.escalation_id if selected else None,
        )
        self.query_one("#panel-lines", Lines).show(lines, targets)

    def _side_width(self) -> int:
        """Return the width of the inside of the side column.

        Returns:
            The columns the texts of the panel may use.
        """
        if self.size.width < WIDE:
            return min(44, self.size.width * 9 // 10) - 4
        return (46 if self.side_open else 36) - 4

    def _render_side(self) -> None:
        """Draw the side questions and scroll to the newest one."""
        if not self.side_open:
            return
        self.query_one("#side-lines", Lines).show(
            render.side_lines(self.model, self.view(self._side_width()))
        )
        self.query_one("#side", VerticalScroll).scroll_end(animate=False)

    def _send_modes(self) -> list[tuple[str, str]]:
        """List the send modes Tab cycles through.

        Returns:
            `(target, delivery)`: the main agent's next turn; into its running turn while the
            session works; then every running background subagent.
        """
        modes = [(MAIN, "queued")]
        if self.model.working():
            modes.append((MAIN, "inject"))
        modes += [
            (agent.key, "inject")
            for agent in self.model.agents.values()
            if agent.key != MAIN and agent.takes_messages
        ]
        return modes

    def _check_target(self) -> None:
        """Fall back to the main agent when the targeted subagent takes no messages any more."""
        agent = self.model.agents.get(self.target)
        if self.target != MAIN and (agent is None or not agent.takes_messages):
            name = self.model.label(self.target)
            self.target = MAIN
            self.flash(f"{name} takes no more messages: sending to main")

    def mode_text(self) -> str:
        """Say in words where the composer's text goes.

        Returns:
            The text of the composer's header for the current mode.
        """
        decision = self.current_decision()
        if self.model.connection.status in {"detached", "reconnecting"}:
            return "not attached: sending is off, the text is kept"
        if self.mode == "note":
            return (
                "note for Claude with the decision · alt+a allow · alt+d deny · "
                "enter keeps it · esc cancels"
            )
        if self.mode == "answer" and decision is not None and decision.is_question:
            header = decision.questions[decision.current].header
            return f"your own answer to: {header} · enter keeps it · esc cancels"
        if self.mode == "side":
            return (
                "side question · answered beside the work, not part of the conversation · "
                "esc closes"
            )
        if self.target != MAIN:
            return f"to {self.model.label(self.target)} · relayed by the main agent"
        note = ""
        shown = self.model.agents.get(self.model.selected_tab)
        if shown is not None and shown.key != MAIN and not shown.takes_messages:
            note = f"{shown.label} takes no messages (only running background subagents do) · "
        if not self.model.working():
            return f"{note}to main"
        if self.delivery == "inject":
            return f"{note}to main · into the running turn (read after the running tool calls)"
        return f"{note}to main · next turn"

    def _render_head(self) -> None:
        """Draw the composer's header: the send mode, slash completions or a message."""
        palette = self.palette
        text = self.composer.text
        line = Text()
        if self.message:
            line.append(
                self.message, f"bold {palette.red}" if self.message_error else palette.orange
            )
        elif text.startswith("/") and " " not in text and "\n" not in text:
            found = completions(text)
            line.append(
                "  ".join(found[:8]) or "no such command (/help lists them)", palette.accent
            )
            if found:
                line.append("  · tab completes", palette.faint)
        else:
            special = self.mode != "chat" or self.target != MAIN
            line.append(self.mode_text(), f"bold {palette.accent}" if special else palette.muted)
            if self.mode == "chat" and len(self._send_modes()) > 1:
                line.append("  · tab changes", palette.faint)
        line.truncate(max(10, self.query_one("#main").size.width - 2), overflow="ellipsis")
        self.query_one("#composer-head", Line).show(line)

    def _render_footer(self) -> None:
        """Draw the line of keys: what works now, most useful first, cut to the width."""
        palette = self.palette
        decision = self.current_decision()
        typed = bool(self.composer.text)
        parts: list[tuple[int, str, str]] = []
        if self._escape_at is not None:
            parts.append((9, "esc", "again to interrupt"))
        if self.focused is self.transcript:
            parts += [
                (9, "j k", "move"),
                (9, "enter", "expand"),
                (8, "e", "next waiting"),
                (9, "esc", "back to input"),
            ]
        elif self.armed:
            parts.append((9, self.armed[0], f"again to {self.armed}"))
        elif decision is not None and decision.is_question:
            parts += [(9, "1-9", "choose"), (9, "o", "own answer"), (9, "enter", "send")]
        elif decision is not None and typed:
            parts += [(9, "alt+a", "allow"), (9, "alt+d", "deny"), (9, "alt+w", "detail")]
        elif decision is not None:
            parts += [(9, "a a", "allow"), (9, "d d", "deny"), (8, "n", "note"), (9, "w", "detail")]
        if self.model.working():
            parts += [(5, "esc esc", "interrupt"), (3, "tab", "send mode")]
        else:
            parts += [(5, "enter", "send"), (1, "^j", "newline")]
        parts += [(7, "alt+←/→", "agent"), (4, "pgup/pgdn", "scroll"), (6, "f3", "side question")]
        if self.size.width < WIDE:
            parts.append((6, "f2", "panel"))
        parts += [(8, "^p", "palette"), (8, "f1", "help"), (2, "^d", "detach")]

        def length() -> int:
            """Measure the footer with the current parts.

            Returns:
                Its width in columns.
            """
            return sum(len(key) + len(what) + 4 for _, key, what in parts)

        while len(parts) > 3 and length() > self.size.width:
            lowest = min(priority for priority, _, _ in parts)
            parts.remove(next(part for part in reversed(parts) if part[0] == lowest))
        line = Text(" ")
        for index, (_, key, what) in enumerate(parts):
            if index:
                line.append("   ")
            line.append(key, f"bold {palette.fg}")
            line.append(f" {what}", palette.muted)
        line.truncate(self.size.width, overflow="ellipsis")
        self.query_one("#footer", Line).show(line)

    def _render_below(self) -> None:
        """Show or hide the marker for new content below the visible part of the transcript."""
        transcript = self.transcript
        marker = self.query_one("#new-below", NewBelow)
        shown = not transcript.following
        marker.set_class(shown, "-shown")
        if shown:
            count = transcript.new_below
            what = f"{count} new below" if count else "scrolled up"
            marker.update(f"▼ {what} · End follows")

    def flash(self, message: str, error: bool = False) -> None:
        """Show a message in the composer's header until the user types or sends.

        Args:
            message: The message; "" clears it.
            error: Whether it reports a failure.
        """
        self.message, self.message_error = message, error
        if self.is_running:
            self._render_head()

    def report(self, outcome: Outcome, done: str = "") -> None:
        """Show the result of a request.

        Args:
            outcome: The outcome.
            done: What to say on success if the outcome carries no message.
        """
        if outcome.ok:
            self.flash(outcome.message or done)
        else:
            self.flash(outcome.message or "the session refused the request", error=True)

    # ------------------------------------------------------------------ widget messages

    def on_transcript_moved(self) -> None:
        """Update the marker for new content when the transcript scrolled."""
        self._render_below()

    def on_transcript_opened(self, message: Transcript.Opened) -> None:
        """Show the tab of the subagent whose Agent call the user opened.

        Args:
            message: The message with the block id.
        """
        block = self.model.blocks[message.block_id]
        if isinstance(block, StepBlock) and block.tool_use_id in self.model.agents:
            self.select_tab(block.tool_use_id)

    def on_picked(self, message: Picked) -> None:
        """React to a click on a tab, a panel row or the marker for new content.

        Args:
            message: What was clicked.
        """
        if message.kind == "agent":
            self.select_tab(message.key)
        elif message.kind == "decision":
            self.selected = message.key
            self.disarm()
            self.render_all()
        elif message.kind == "follow":
            self.transcript.follow()
        self.composer.focus()

    def on_descendant_focus(self) -> None:
        """Show the keys of the widget that got the focus."""
        self._render_footer()

    def on_text_area_changed(self) -> None:
        """Redraw header and footer when the composer's text changes."""
        self._render_head()
        self._render_footer()

    def on_key(self, event: events.Key) -> None:
        """Send a printable key pressed outside the composer into the composer.

        Typing therefore never goes nowhere: a letter pressed while the transcript has the
        focus (and that is no key of the transcript) moves the focus back and is typed.

        Args:
            event: The key.
        """
        if len(self.screen_stack) > 1 or self.focused is self.composer:
            return
        if event.is_printable and event.character:
            event.stop()
            self.composer.focus()
            if not self._composer_key(event):
                self.composer.insert(event.character)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let the keys of the main screen through only while the main screen is shown.

        A full-screen view or the command palette then gets Escape, the scroll keys and Tab
        for itself.

        Args:
            action: The action name.
            parameters: Its parameters.

        Returns:
            False for an action of this app while another screen is on top; True otherwise.
        """
        own = {
            "escape",
            "cycle_mode",
            "scroll",
            "scroll_line",
            "top",
            "follow",
            "agent",
            "send_other",
            "focus_transcript",
            "decide",
            "tab_number",
            "command",
        }
        if action in own and len(self.screen_stack) > 1:
            return (
                action == "command"
                and bool(parameters)
                and parameters[0] == "detail"
                and isinstance(self.screen, DetailScreen)
            )
        return True

    # ------------------------------------------------------------------ composer

    def _composer_key(self, event: events.Key) -> bool:
        """Decide whether a key pressed in the composer is a command rather than text.

        Only with an empty composer: `end` and `home` scroll the transcript, `?` opens the
        help, and while a decision is open its keys act on the card (see the module
        docstring). After the first `a` or `d` the next key decides: the same key confirms,
        any other key types -- the first letter included, so no typing is lost.

        Args:
            event: The key.

        Returns:
            True if the key was handled here and must not be typed.
        """
        key = event.key
        composer = self.composer
        if self.message:
            self.message = ""  # a message stays until the next key
            self._render_head()
        if self.armed:
            letter = self.armed[0]
            if key == letter:
                self.decide_now(self.armed)
                return True
            self.disarm()
            if event.is_printable and event.character and self.mode == "chat":
                composer.insert(letter)
            self.render_all()
            return False
        if composer.text:
            return False
        if key == "end":
            self.transcript.follow()
            return True
        if key == "home":
            self.transcript.scroll_top()
            return True
        if key == "question_mark":
            self.run_command("help")
            return True
        decision = self.current_decision()
        if decision is None or self.mode != "chat":
            return False
        if key in {"right_square_bracket", "left_square_bracket"}:
            self.run_command(
                "next_decision" if key == "right_square_bracket" else "previous_decision"
            )
            return True
        if key == "w":
            self.run_command("detail")
            return True
        if decision.is_question:
            question = decision.questions[decision.current]
            if key.isdigit() and key != "0" and int(key) <= len(question.options):
                self.pick_option(decision, int(key) - 1)
                return True
            if key == "o":
                self.mode = "answer"
                composer.set_text(decision.free[decision.current])
                self.render_all()
                return True
            if key in {"left", "right"} and len(decision.questions) > 1:
                step = 1 if key == "right" else -1
                decision.current = (decision.current + step) % len(decision.questions)
                self.render_all()
                return True
            return False
        if key in {"a", "d", "n"}:
            self.decision_key(key)
            return True
        return False

    def pick_option(self, decision: Decision, option: int) -> None:
        """Choose an option of the question the card shows.

        A question with one choice takes the option, drops an own answer and moves on to the
        next unanswered question; one with several choices switches the option on or off.

        Args:
            decision: The open question set.
            option: The index of the option, from 0.
        """
        current = decision.current
        if decision.questions[current].multi:
            decision.picks[current] ^= {option}
        else:
            decision.picks[current] = {option}
            decision.free[current] = ""
            self._next_unanswered(decision)
        self.render_all()

    def _next_unanswered(self, decision: Decision) -> None:
        """Show the next question of a set that has no answer yet, if there is one.

        Args:
            decision: The open question set.
        """
        answers = decision.answers()
        count = len(decision.questions)
        for offset in range(1, count):
            index = (decision.current + offset) % count
            if not answers[index]:
                decision.current = index
                return

    def decision_key(self, letter: str) -> None:
        """Handle `a`, `d` or `n` for the open item the card shows.

        Args:
            letter: `a` or `d` arm the decision (the second press carries it out); `n` puts
                the composer into note mode.
        """
        decision = self.current_decision()
        if decision is None:
            return
        if letter == "n":
            self.disarm()
            if decision.is_question:
                self.flash("a question is answered with 1-9 or o; alt+d declines it")
                return
            self.mode = "note"
            self.composer.set_text(decision.note)
            self.composer.focus()
            self.render_all()
            return
        wanted = "allow" if letter == "a" else "deny"
        if decision.is_question and wanted == "allow":
            self.flash("a question is answered with 1-9 or o, then enter")
            return
        if self.armed == wanted and self.armed_for == decision.escalation_id:
            self.decide_now(wanted)
        else:
            self.armed, self.armed_for = wanted, decision.escalation_id
            self.render_all()

    def disarm(self) -> None:
        """Forget the first press of a decision key."""
        self.armed, self.armed_for = "", None

    def decide_now(self, wanted: str) -> None:
        """Send the decision for the open item the card shows.

        The note of the item goes along; in note mode the composer's text is the note.

        Args:
            wanted: "allow" or "deny".
        """
        decision = self.current_decision()
        self.disarm()
        if decision is None:
            return
        if self.mode == "note":
            decision.note = self.composer.text.strip()
            self.mode = "chat"
            self.composer.set_text("")
        self._spawn(self._decide(decision.escalation_id, wanted, decision.note))
        self.render_all()

    async def _decide(self, escalation_id: str, wanted: str, note: str) -> None:
        """Send one decision and show a refusal on the card.

        Args:
            escalation_id: The escalation.
            wanted: "allow" or "deny".
            note: The note for Claude; "" for none.
        """
        outcome = await self.actions.decide(escalation_id, wanted, note or None)
        if not outcome.ok:
            self.flash(outcome.message, error=True)
        self.render_all()

    def action_decide(self, wanted: str) -> None:
        """Allow or deny with the chord key (`alt+a`, `alt+d`), whatever the composer holds.

        A step that a deny rule stopped is allowed only by a second press; every other
        decision is sent at once.

        Args:
            wanted: "allow" or "deny".
        """
        decision = self.current_decision()
        if decision is None:
            self.flash("nothing waits for a decision")
            return
        if decision.is_question and wanted == "allow":
            self.flash("a question is answered with 1-9 or o, then enter")
            return
        again = self.armed == wanted and self.armed_for == decision.escalation_id
        if wanted == "allow" and decision.by_rule and not again:
            self.armed, self.armed_for = wanted, decision.escalation_id
            self.render_all()
            return
        self.decide_now(wanted)

    def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Handle Enter in the composer according to the mode.

        Args:
            message: The message with the composer's text.
        """
        text = message.text.strip()
        composer = self.composer
        decision = self.current_decision()
        if self.mode == "note":
            if decision is not None:
                decision.note = text
            self.mode = "chat"
            composer.set_text("")
            self.render_all()
            return
        if self.mode == "answer":
            if decision is not None and decision.is_question:
                decision.free[decision.current] = text
                if text and not decision.questions[decision.current].multi:
                    decision.picks[decision.current] = set()
                self._next_unanswered(decision)
            self.mode = "chat"
            composer.set_text("")
            self.render_all()
            return
        if not text:
            if decision is not None and decision.is_question and decision.complete():
                self._spawn(self._answer(decision.escalation_id))
            elif decision is not None and decision.is_question:
                self.flash(f"{decision.answered()} of {len(decision.questions)} questions answered")
            elif decision is not None:
                self.jump_to(decision)
            return
        composer.remember(message.text)
        if self.mode == "side":
            composer.set_text("")
            self._spawn(self._ask_side(text))
            return
        if text.startswith("/"):
            word, _, rest = text.partition(" ")
            command = by_slash(word)
            if command is None:
                self.flash(f"no command {word} (/help lists them)", error=True)
                return
            composer.set_text("")
            self.run_command(command.id, rest.strip())
            return
        composer.set_text("")
        self._spawn(self._send(message.text, self.target, self.delivery))

    async def _send(self, text: str, target: str, delivery: str) -> None:
        """Send a chat message; on a refusal put the text back and say why.

        Args:
            text: The message.
            target: `MAIN` or the id of a running background subagent.
            delivery: "queued" or "inject".
        """
        outcome = await self.actions.send(text, target, delivery)
        if outcome.ok:
            self.flash("")
            self.transcript.follow()
            return
        if not self.composer.text:
            self.composer.set_text(text)
        self.flash(outcome.message, error=True)

    async def _answer(self, escalation_id: str, text: str | None = None) -> None:
        """Send the answer to a question of the agent.

        Args:
            escalation_id: The escalation of the question.
            text: The answer; None composes it from what was chosen on the card.
        """
        outcome = await self.actions.answer(escalation_id, text)
        if not outcome.ok:
            self.flash(outcome.message, error=True)
        self.render_all()

    async def _ask_side(self, question: str) -> None:
        """Ask a side question and show the exchange in the side panel.

        Args:
            question: The question.
        """
        if not self.side_open:
            self.side_open = True
            self.mode = "side"
            self._breakpoints()
        self.call_next(self.render_all)
        await self.actions.ask_side(question)
        self.render_all()

    def jump_to(self, decision: Decision) -> None:
        """Show the step of an open item in the transcript, in the tab of its agent.

        Args:
            decision: The open item.
        """
        step = self.model.step(decision.tool_use_id)
        if step is None:
            return
        key = decision.agent_id or MAIN
        if self.model.selected_tab not in {ALL, key}:
            self.select_tab(key)
        self.transcript.focus()
        self.transcript.set_cursor(step.id)

    # ------------------------------------------------------------------ agents and modes

    def select_tab(self, key: str) -> None:
        """Show an agent's transcript and aim the composer at that agent if it takes messages.

        Args:
            key: `MAIN`, `ALL` or an `agent_id`; an unknown key does nothing.
        """
        if key not in self.model.tabs():
            return
        self.model.select(key)
        agent = self.model.agents.get(key)
        self.target = key if agent is not None and agent.takes_messages else MAIN
        own = self.model.open_decisions(key) if key != ALL else []
        if own:
            self.selected = own[0].escalation_id
            self.disarm()
        self.transcript.now = self.clock()
        self.transcript.show_tab(key)
        self.render_all()

    def action_agent(self, step: int) -> None:
        """Show the next or the previous agent tab.

        Args:
            step: +1 for the next tab, -1 for the previous one; the row wraps around.
        """
        tabs = self.model.tabs()
        index = tabs.index(self.model.selected_tab) if self.model.selected_tab in tabs else 0
        self.select_tab(tabs[(index + step) % len(tabs)])

    def action_tab_number(self, number: int) -> None:
        """Show the tab with a number.

        Args:
            number: 1 is the main agent, 2 the first subagent and so on; 0 is the view of all
                agents.
        """
        tabs = self.model.tabs()
        if number == 0:
            self.select_tab(ALL)
        elif number <= len(tabs):
            self.select_tab(tabs[number - 1])

    def action_cycle_mode(self, step: int) -> None:
        """Change the send mode, or complete a slash command that is being typed.

        Args:
            step: +1 for the next mode, -1 for the previous one.
        """
        text = self.composer.text
        if text.startswith("/") and " " not in text:
            found = completions(text)
            if found:
                self.composer.set_text(found[0] + " ")
            return
        if self.mode != "chat":
            return
        modes = self._send_modes()
        current = (self.target, self.delivery if self.target == MAIN else "inject")
        index = modes.index(current) if current in modes else 0
        self.target, delivery = modes[(index + step) % len(modes)]
        if self.target == MAIN:
            self.delivery = delivery
        self.message = ""
        self.render_all()

    def action_send_other(self) -> None:
        """Send the composer's text once with the other delivery to the main agent."""
        text = self.composer.text
        if not text.strip() or self.mode != "chat":
            return
        other = "queued" if self.delivery == "inject" else "inject"
        self.composer.remember(text)
        self.composer.set_text("")
        self._spawn(self._send(text, MAIN, other))

    # ------------------------------------------------------------------ scrolling and focus

    def action_scroll(self, pages: float) -> None:
        """Scroll the transcript by pages while the composer keeps the focus.

        Args:
            pages: 1.0 is one page towards the end, -1.0 one towards the start.
        """
        self.transcript.scroll_pages(pages)

    def action_scroll_line(self, lines: int) -> None:
        """Scroll the transcript by lines while the composer keeps the focus.

        Args:
            lines: Positive scrolls towards the end.
        """
        self.transcript.scroll_lines(lines)

    def action_top(self) -> None:
        """Jump to the start of the transcript."""
        self.transcript.scroll_top()

    def action_follow(self) -> None:
        """Jump to the end of the transcript and follow new output."""
        self.transcript.follow()

    def action_focus_transcript(self) -> None:
        """Move the focus into the transcript; Escape brings it back."""
        self.transcript.focus()

    def action_escape(self) -> None:
        """Step back once: cancel, close, return to the composer, or count towards an interrupt.

        In this order: forget a first decision key; leave note or answer mode; close the side
        questions; close the side panel on a narrow screen; return the focus to the composer;
        and while the session works, a second Escape within `DOUBLE_ESCAPE_S` interrupts the
        turn.
        """
        if self.armed:
            self.disarm()
        elif self.mode in {"note", "answer"}:
            self.mode = "chat"
            self.composer.set_text("")
        elif self.side_open:
            self.side_open = False
            self.mode = "chat"
            self._breakpoints()
        elif self.panel_open:
            self.panel_open = False
            self._breakpoints()
        elif self.focused is not self.composer:
            self.composer.focus()
        elif self.model.working():
            now = self.clock()
            if self._escape_at is not None and now - self._escape_at <= DOUBLE_ESCAPE_S:
                self._escape_at = None
                self.run_command("interrupt")
            else:
                self._escape_at = now
                if self.live:
                    self.set_timer(DOUBLE_ESCAPE_S, self._forget_escape)
        self.message = ""
        self.render_all()

    def _forget_escape(self) -> None:
        """Forget a single Escape after `DOUBLE_ESCAPE_S`."""
        self._escape_at = None
        self._render_footer()

    def action_detach(self) -> None:
        """Leave the client; the session keeps running."""
        self.link.detach()
        self.exit()

    async def action_quit(self) -> None:
        """Leave the client as `detach` does (Textual's own quit key)."""
        self.action_detach()

    def action_help_quit(self) -> None:
        """Say how to leave instead of quitting on ctrl+c, which copies in the composer."""
        self.flash("ctrl+d detaches (the session keeps running) · esc esc interrupts the turn")

    # ------------------------------------------------------------------ commands

    def action_command(self, command_id: str) -> None:
        """Run a command of `COMMANDS` from a key binding.

        Args:
            command_id: The id of the command.
        """
        self.run_command(command_id)

    def run_command(self, command_id: str, rest: str = "") -> None:
        """Carry out a command: from a key, the palette or a slash command.

        A command the session cannot carry out shows its reason and does nothing else.

        Args:
            command_id: The id of a `Command`.
            rest: What followed the slash command on the line; "" from a key or the palette.
        """
        command = next((entry for entry in COMMANDS if entry.id == command_id), None)
        if command is None:
            return
        reason = command.unavailable(self.capabilities)
        if reason:
            self.flash(f"{command.name or command.title}: {reason}", error=True)
            return
        handler = getattr(self, f"_cmd_{command_id}", None)
        if handler is None:
            self.flash(f"{command.title}: {command.key or command.slash}")
            return
        handler(rest)
        if self.is_running:
            self.render_all()

    def _find_decision(self, rest: str) -> tuple[Decision | None, str]:
        """Read an optional escalation id from the start of a command's arguments.

        Args:
            rest: The arguments.

        Returns:
            The open item the id names (or the one the card shows if no id leads the
            arguments) and the remaining text.
        """
        word, _, tail = rest.partition(" ")
        if word in self.model.decisions:
            return self.model.decisions[word], tail.strip()
        return self.current_decision(), rest.strip()

    def _cmd_allow(self, rest: str) -> None:
        """Allow an open step at once (`/allow [id] [note]`).

        Args:
            rest: An optional escalation id and note.
        """
        decision, note = self._find_decision(rest)
        if decision is None:
            self.flash("nothing waits for a decision")
        elif decision.is_question:
            self.flash("a question is answered with /answer <text>", error=True)
        else:
            self._spawn(self._decide(decision.escalation_id, "allow", note or decision.note))

    def _cmd_deny(self, rest: str) -> None:
        """Deny an open step at once (`/deny [id] [note]`).

        Args:
            rest: An optional escalation id and note.
        """
        decision, note = self._find_decision(rest)
        if decision is None:
            self.flash("nothing waits for a decision")
        else:
            self._spawn(self._decide(decision.escalation_id, "deny", note or decision.note))

    def _cmd_note(self, rest: str) -> None:
        """Put the composer into note mode for the open step.

        Args:
            rest: Ignored.
        """
        self.decision_key("n")

    def _cmd_answer(self, rest: str) -> None:
        """Answer a question in the user's own words (`/answer [id] <text>`).

        Args:
            rest: An optional escalation id and the answer; without a text the answer chosen
                on the card is sent.
        """
        decision, text = self._find_decision(rest)
        if decision is None or not decision.is_question:
            self.flash("no question of the agent is open", error=True)
        else:
            self._spawn(self._answer(decision.escalation_id, text or None))

    def _cmd_detail(self, rest: str) -> None:
        """Open the full-screen view of an open item, or close it if it is shown.

        Args:
            rest: An optional escalation id.
        """
        if isinstance(self.screen, DetailScreen):
            self.screen.dismiss()
            return
        decision, _ = self._find_decision(rest)
        if decision is None:
            self.flash("nothing waits for a decision")
            return
        self.selected = decision.escalation_id
        screen = DetailScreen(decision.escalation_id)
        self.push_screen(screen)
        self._spawn(self._load_detail(screen))

    async def _load_detail(self, screen: DetailScreen) -> None:
        """Fetch what the checker saw and hand it to the detail view.

        Args:
            screen: The open detail view.
        """
        outcome = await self.actions.why(screen.escalation_id)
        if screen.is_attached:
            data = outcome.data if outcome.ok and isinstance(outcome.data, dict) else None
            screen.set_context(data, "" if data else outcome.message or "no answer")

    def _step_decision(self, step: int) -> None:
        """Show another open item on the card.

        Args:
            step: +1 for the next one, -1 for the previous one.
        """
        waiting = list(self.model.decisions)
        current = self.current_decision()
        if current is None or len(waiting) < 2:
            return
        index = waiting.index(current.escalation_id)
        self.selected = waiting[(index + step) % len(waiting)]
        self.disarm()

    def _cmd_next_decision(self, rest: str) -> None:
        """Show the next open item on the card.

        Args:
            rest: Ignored.
        """
        self._step_decision(1)

    def _cmd_previous_decision(self, rest: str) -> None:
        """Show the previous open item on the card.

        Args:
            rest: Ignored.
        """
        self._step_decision(-1)

    def _cmd_next_agent(self, rest: str) -> None:
        """Show the next agent tab.

        Args:
            rest: Ignored.
        """
        self.action_agent(1)

    def _cmd_previous_agent(self, rest: str) -> None:
        """Show the previous agent tab.

        Args:
            rest: Ignored.
        """
        self.action_agent(-1)

    def _cmd_pick_agent(self, rest: str) -> None:
        """Switch to an agent by name, or list the agents to choose from (`/agent [name]`).

        Args:
            rest: A part of the agent's name; empty opens the list.
        """
        if rest:
            wanted = rest.lower()
            for key in self.model.tabs():
                if wanted in (ALL if key == ALL else self.model.label(key)).lower():
                    self.select_tab(key)
                    return
            self.flash(f"no agent matches {rest}", error=True)
            return
        entries = []
        for key in self.model.tabs():
            if key == ALL:
                entries.append(
                    (
                        "all agents",
                        partial(self.select_tab, key),
                        "every agent's blocks in the order they arrived",
                    )
                )
                continue
            agent = self.model.agents[key]
            waiting = len(self.model.open_decisions(key))
            about = [self.model.activity_of(agent)]
            if agent.subagent_type:
                about.append(agent.subagent_type)
            if agent.background:
                about.append("background")
            if waiting:
                about.append(f"{waiting} decision{'s' if waiting != 1 else ''} wait")
            entries.append((agent.label, partial(self.select_tab, key), ", ".join(about)))
        self.search_commands(entries, placeholder="agent…")

    def _cmd_cycle_mode(self, rest: str) -> None:
        """Change the send mode.

        Args:
            rest: Ignored.
        """
        self.action_cycle_mode(1)

    def _cmd_send_other(self, rest: str) -> None:
        """Send the composer's text once with the other delivery.

        Args:
            rest: Ignored.
        """
        self.action_send_other()

    def _cmd_inject(self, rest: str) -> None:
        """Send a message into the running turn (`/inject <text>`).

        Args:
            rest: The message.
        """
        if not rest:
            self.flash("usage: /inject <text>", error=True)
        else:
            self._spawn(self._send(rest, MAIN, "inject"))

    def _cmd_to(self, rest: str) -> None:
        """Send a message to a running background subagent (`/to <agent> <text>`).

        Args:
            rest: The agent's name or id, then the message.
        """
        name, _, text = rest.partition(" ")
        if not name or not text.strip():
            self.flash("usage: /to <agent> <text>", error=True)
            return
        found = next(
            (
                agent.key
                for agent in self.model.agents.values()
                if agent.key != MAIN and (name == agent.key or name.lower() in agent.label.lower())
            ),
            name,
        )
        self._spawn(self._send(text.strip(), found, "inject"))

    def _cmd_side(self, rest: str) -> None:
        """Ask a side question, or open and close the side questions (`/btw [question]`).

        Args:
            rest: The question; empty toggles the panel.
        """
        if rest:
            self._spawn(self._ask_side(rest))
            return
        self.side_open = not self.side_open
        self.mode = "side" if self.side_open else "chat"
        self._breakpoints()
        self.composer.focus()

    def _cmd_comment(self, rest: str) -> None:
        """Comment on lines of a project file (`/comment <path> <from> <to> <text>`).

        Args:
            rest: Path, first line, last line and the comment.
        """
        parts = rest.split(" ", 3)
        if len(parts) < 4 or not (parts[1].isdigit() and parts[2].isdigit()):
            self.flash("usage: /comment <path> <from> <to> <text>", error=True)
            return

        async def send() -> None:
            """Send the comment and show the result."""
            self.report(
                await self.actions.comment(parts[0], int(parts[1]), int(parts[2]), parts[3])
            )

        self._spawn(send())

    def _request(self, work: Coroutine[Any, Any, Outcome], done: str = "") -> None:
        """Run a request and show its result in the composer's header.

        Args:
            work: The action's coroutine.
            done: What to say on success.
        """

        async def run() -> None:
            """Await the action and report."""
            self.report(await work, done)
            self.render_all()

        self._spawn(run())

    def _cmd_interrupt(self, rest: str) -> None:
        """Interrupt the running turn.

        Args:
            rest: Ignored.
        """
        self._request(self.actions.interrupt(), "interrupt sent")

    def _cmd_stop(self, rest: str) -> None:
        """Stop the Claude session.

        Args:
            rest: Ignored.
        """
        self._request(self.actions.stop(), "the session stopped (/start starts it again)")

    def _cmd_start(self, rest: str) -> None:
        """Start the session, or resume a stored one (`/start [resume id]`).

        Args:
            rest: A Claude session id to resume; empty resumes the conversation of this
                session if it has one.
        """
        resume = rest or self.model.claude_session_id
        self._request(self.actions.start(resume, effort=self.next_effort), "the session starts")
        self.next_effort = None

    def _cmd_shutdown(self, rest: str) -> None:
        """Shut down calu after a typed confirmation (`/shutdown confirm`).

        Args:
            rest: Must be `confirm`.
        """
        if rest != "confirm":
            self.flash(
                "this ends the calu process and the session: type /shutdown confirm", error=True
            )
        else:
            self._request(self.actions.shutdown(), "calu shuts down")

    def _cmd_model(self, rest: str) -> None:
        """Change the model, or list the models to choose from (`/model [name]`).

        Args:
            rest: An alias or a model id; empty opens the list.
        """
        if rest:
            self._request(self.actions.set_model(rest))
            return
        running = self.model.state not in {"stopped", "failed"}
        if running and not self.capabilities.has("live_model_change"):
            self.flash(str(self.capabilities.reason("live_model_change")), error=True)
            return
        how = "applies from Claude's next step" if running else "starts the session with it"
        entries = [(name, partial(self.run_command, "model", name), how) for name in MODELS]
        self.search_commands(entries, placeholder="model… (/model <id> takes any model id)")

    def _cmd_effort(self, rest: str) -> None:
        """Choose the effort for the next start (`/effort [level]`).

        Args:
            rest: One of `EFFORTS`; empty opens the list.
        """
        if not rest:
            entries = [
                (name, partial(self.run_command, "effort", name), "used by the next /start")
                for name in EFFORTS
            ]
            self.search_commands(entries, placeholder="effort…")
            return
        if rest not in EFFORTS:
            self.flash(f"effort is one of: {', '.join(EFFORTS)}", error=True)
            return
        self.next_effort = rest
        running = self.model.state not in {"stopped", "failed"}
        tail = (
            ": /stop and /start apply it (the conversation is resumed)"
            if running
            else ": /start applies it"
        )
        self.flash(f"effort {rest} is set for the next start{tail}")

    def _cmd_level(self, rest: str) -> None:
        """Set the supervision level, or list the levels (`/level [name]`).

        Args:
            rest: The id or a part of the name of a ladder mode; empty opens the list.
        """
        if not rest:
            entries = [
                (mode.name, partial(self.run_command, "level", mode.id), mode.description)
                for mode in LADDER
            ]
            self.search_commands(entries, placeholder="supervision level…")
            return
        wanted = rest.lower()
        mode = next((m for m in LADDER if wanted == m.id or wanted in m.name.lower()), None)
        if mode is None:
            self.flash("levels: " + ", ".join(f"{m.id} ({m.name})" for m in LADDER), error=True)
            return
        self._request(self.actions.set_level(mode.id), f"supervision level: {mode.name}")

    def _page(self, title: str, body: Any) -> TextScreen:
        """Open a full-screen page of text.

        Args:
            title: The title.
            body: Lines or a Rich renderable.

        Returns:
            The screen, so that its body can be replaced when a request answers.
        """
        screen = TextScreen(title, body)
        self.push_screen(screen)
        return screen

    def _read_page(
        self,
        title: str,
        method: str,
        shape: Callable[[Any], Any],
        params: dict[str, Any] | None = None,
    ) -> None:
        """Open a page and fill it with the result of a request that only reads.

        Args:
            title: The title of the page.
            method: The request method.
            shape: Turns the result into the page's body.
            params: The request params.
        """
        screen = self._page(title, [Text("asking the session…", self.palette.muted)])

        async def load() -> None:
            """Send the request and show its result or the refusal."""
            outcome = await self.actions.read(method, params)
            if not screen.is_attached:
                return
            if outcome.ok:
                screen.set_body(shape(outcome.data))
            else:
                screen.set_body([Text(outcome.message, self.palette.red)])

        self._spawn(load())

    def _cmd_usage(self, rest: str) -> None:
        """Show cost, context fill and usage limits on a page.

        Args:
            rest: Ignored.
        """

        def shape(data: Any) -> list[Text]:
            """Load the result into the model and render the SESSION figures.

            Args:
                data: The result of `usage.get`.

            Returns:
                The lines of the page.
            """
            if isinstance(data, dict):
                self.model.set_usage(data)
            lines, _ = render.panel_lines(
                self.model, self.view(min(70, self.size.width - 6)), self.capabilities
            )
            start = next(i for i, line in enumerate(lines) if line.plain.startswith("SESSION"))
            context = self.model.usage.context
            extra = (
                [
                    Text(""),
                    Text(f"model the context is counted for: {context.model}", self.palette.muted),
                ]
                if context
                else []
            )
            return [*lines[start + 1 :], *extra]

        self._read_page(
            "Usage: cost as Claude Code reports it, context fill, usage limits", "usage.get", shape
        )

    def _cmd_state(self, rest: str) -> None:
        """Show the session's state on a page.

        Args:
            rest: Ignored.
        """
        model = self.model
        rows = [
            ("state", render.STATE_WORDS.get(model.state, model.state)),
            ("project", model.project or ""),
            ("directory", model.workspace),
            ("model / effort", f"{model.model_name or 'default'} / {model.effort or 'default'}"),
            ("supervision level", model.level()),
            ("Claude session", model.claude_session_id or "none yet"),
            ("calu session", model.calu_session_id or "none yet"),
            ("open decisions", str(len(model.decisions))),
            ("queued messages", str(len(model.queue))),
            (
                "agents",
                ", ".join(f"{a.label} ({model.activity_of(a)})" for a in model.agents.values()),
            ),
            ("honesty", f"{model.honesty_mode}, {len(model.marks)} open marks"),
            ("connection", model.connection.status),
        ]
        body = [
            Text.assemble((f"{name:<20}", self.palette.muted), (value, self.palette.fg))
            for name, value in rows
        ]
        missing = self.capabilities.missing()
        if missing:
            body += [Text(""), Text("Not offered by this session:", f"bold {self.palette.fg}")]
            body += [
                Text(f"  {entry.title}: {entry.reason}", self.palette.muted) for entry in missing
            ]
        self._page("Session state", body)

    def _cmd_context(self, rest: str) -> None:
        """Show the conversation as the checkers see it.

        Args:
            rest: Ignored.
        """

        def shape(data: Any) -> Markdown:
            """Render the checker context as Markdown.

            Args:
                data: The result of `context.get`.

            Returns:
                The chat summary and the recent turns.
            """
            summary = str(data.get("chat_summary") or "").strip() or "_empty_"
            recent = str(data.get("recent") or "").strip() or "_empty_"
            return Markdown(
                f"## Chat summary (the older turns, folded)\n\n{summary}\n\n"
                f"## Recent turns\n\n{recent}"
            )

        self._read_page(
            "The conversation as the checkers see it (calu sends nothing to Claude here)",
            "context.get",
            shape,
        )

    def _cmd_checkers(self, rest: str) -> None:
        """Show the checkers and their instructions.

        Args:
            rest: Ignored.
        """

        def shape(data: Any) -> Markdown:
            """Render the checker definitions as Markdown.

            Args:
                data: The result of `checkers.get`.

            Returns:
                One section per checker.
            """
            parts = [
                "Changed with `calu checker`, `calu rules` and `calu config`, not from a session.\n"
            ]
            for checker in data.get("checkers") or []:
                state = "" if checker.get("enabled", True) else " (disabled)"
                parts.append(
                    f"## {checker.get('name')}{state}\n\nroute `{checker.get('route')}` · model "
                    f"`{checker.get('model')}` · version {checker.get('version')}\n\n"
                    f"{checker.get('instructions') or '_no instructions_'}"
                )
            return Markdown("\n\n".join(parts))

        self._read_page("Checkers of this session", "checkers.get", shape)

    def _cmd_sessions(self, rest: str) -> None:
        """List the stored sessions of this project.

        Args:
            rest: Ignored.
        """

        def shape(data: Any) -> list[Text]:
            """Render the session list.

            Args:
                data: The result of `session.list`.

            Returns:
                Two lines per session: id, state and start, then its first input.
            """
            lines = [
                Text("/start <claude session id> resumes one of them.", self.palette.muted),
                Text(""),
            ]
            for entry in data.get("sessions") or []:
                lines.append(
                    Text.assemble(
                        (
                            str(entry.get("claude_session_id") or "no claude id"),
                            f"bold {self.palette.fg}",
                        ),
                        (
                            f"  {entry.get('state')}  {str(entry.get('started_at'))[:16]}",
                            self.palette.muted,
                        ),
                    )
                )
                lines.append(
                    Text(
                        "  " + render.cut(str(entry.get("first_input") or ""), 100), self.palette.fg
                    )
                )
            return lines

        self._read_page("Stored sessions of this project", "session.list", shape, {"limit": 30})

    def _cmd_history(self, rest: str) -> None:
        """Show the stored steps of a session (`/history [id]`).

        Args:
            rest: A Claude session id; empty is the running session.
        """

        def shape(data: Any) -> list[Text]:
            """Render the stored steps, one line each.

            Args:
                data: The result of `session.history`.

            Returns:
                The chat summary and one line per step.
            """
            lines = []
            summary = str(data.get("chat_summary") or "").strip()
            if summary:
                lines += render.markdown_lines(summary, min(100, self.size.width - 6), self.palette)
                lines.append(Text(""))
            for step in data.get("steps") or []:
                lines.append(
                    Text.assemble(
                        (f"{str(step.get('decision') or '').upper():<9} ", self.palette.muted),
                        (
                            f"{render.tool_label(str(step.get('tool_name')))::<6} ",
                            f"bold {self.palette.fg}",
                        ),
                        (
                            render.cut(
                                str(step.get("summary") or step.get("tool_input") or ""), 120
                            ),
                            self.palette.fg,
                        ),
                    )
                )
            return lines or [Text("no stored steps", self.palette.muted)]

        params = {"claude_session_id": rest} if rest else {}
        self._read_page(
            "Stored history (as recorded in the database)", "session.history", shape, params
        )

    def _cmd_log(self, rest: str) -> None:
        """Show the log lines of the session and of this client.

        Args:
            rest: Ignored.
        """
        styles = {"warning": self.palette.orange, "error": self.palette.red}
        body = [
            Text(f"[{entry.level}] {entry.message}", styles.get(entry.level, self.palette.muted))
            for entry in self.model.logs
        ]
        self._page("Log", body or [Text("no log lines yet", self.palette.muted)])

    def _cmd_clear_mark(self, rest: str) -> None:
        """Clear an honesty mark (`/clear-mark <id>`).

        Args:
            rest: The id of the mark.
        """
        if not rest.isdigit():
            marks = (
                ", ".join(f"{mark.id} ({mark.path})" for mark in self.model.marks) or "none open"
            )
            self.flash(f"usage: /clear-mark <id> · open marks: {marks}", error=True)
        else:
            self._request(self.actions.clear_mark(int(rest)), f"mark {rest} cleared")

    def _cmd_follow(self, rest: str) -> None:
        """Jump to the end of the transcript.

        Args:
            rest: Ignored.
        """
        self.transcript.follow()

    def _cmd_focus_transcript(self, rest: str) -> None:
        """Move the focus into the transcript.

        Args:
            rest: Ignored.
        """
        self.transcript.focus()

    def _cmd_thinking(self, rest: str) -> None:
        """Show thinking in full or as one line per block.

        Args:
            rest: Ignored.
        """
        if not self.model.saw_thinking:
            self.flash("this session has sent no thinking (claude.thinking_display)")
        self.transcript.toggle_thinking()

    def _cmd_panel(self, rest: str) -> None:
        """Show or hide the side panel on a narrow screen.

        Args:
            rest: Ignored.
        """
        if self.size.width >= WIDE and not self.side_open:
            self.flash("the side panel is always shown at this width")
            return
        if self.side_open:
            self.side_open = False
            self.mode = "chat"
            self.panel_open = True
        else:
            self.panel_open = not self.panel_open
        self._breakpoints()

    def _cmd_copy(self, rest: str) -> None:
        """Copy the block under the transcript's cursor, or Claude's last text, to the clipboard.

        Args:
            rest: Ignored.
        """
        transcript = self.transcript
        block = (
            self.model.blocks[transcript.cursor]
            if transcript.cursor is not None and transcript.has_focus
            else None
        )
        if block is None:
            block = next(
                (
                    b
                    for b in reversed(self.model.blocks_of(self.model.selected_tab))
                    if isinstance(b, TextBlock)
                ),
                None,
            )
        if block is None:
            self.flash("nothing to copy yet")
            return
        self.copy_to_clipboard(render.block_plain(block))
        self.flash("copied (needs a terminal that accepts OSC 52; shift+drag selects natively)")

    def _cmd_theme(self, rest: str) -> None:
        """Switch the colour theme, or list the themes (`/theme [name]`).

        Args:
            rest: A theme name; empty opens the list.
        """
        if rest:
            if rest not in self.available_themes:
                self.flash(f"no theme {rest}", error=True)
            else:
                self._apply_theme(rest)
            return
        names = sorted(self.available_themes, key=lambda name: (not name.startswith("calu"), name))
        self.search_commands(
            [(name, partial(self._apply_theme, name)) for name in names], placeholder="theme…"
        )

    def _cmd_help(self, rest: str) -> None:
        """Show every key and command, and what this session does not offer.

        Args:
            rest: Ignored.
        """
        self._page("Help", help_lines(self.palette, self.capabilities, self.size.width - 6))

    def _cmd_detach(self, rest: str) -> None:
        """Leave the client; the session keeps running.

        Args:
            rest: Ignored.
        """
        self.action_detach()


def help_lines(palette: Palette, caps: Capabilities, width: int = 100) -> list[Text]:
    """Render the help page from the command table.

    Args:
        palette: The colours.
        caps: What the attached session offers.
        width: The width to lay the lines out for.

    Returns:
        The commands by group, each with its key, what it does and its slash command; the
        rules of typing and selecting; and the functions the session does not offer, with the
        reason for each.
    """
    lines: list[Text] = []
    group = ""
    key_width = 30
    for command in COMMANDS:
        if command.group != group:
            group = command.group
            lines += [Text(""), Text(group, f"bold {palette.accent}")]
        reason = command.unavailable(caps)
        title = Text(command.title, palette.faint if reason else palette.fg)
        if command.slash:
            title.append(f"   {command.slash}", palette.faint if reason else palette.accent)
        if reason:
            title.append(f"   ({reason})", palette.faint)
        for index, row in enumerate(render.wrap(title, max(20, width - key_width - 4))):
            head = f"  {command.key:<{key_width}}" if index == 0 else " " * (key_width + 2)
            lines.append(
                Text.assemble((head, palette.faint if reason else f"bold {palette.fg}"), row)
            )
    typing = (
        "enter sends · ctrl+j or alt+enter starts a new line · up/down on the first/last line "
        "walk the history of sent texts. The decision keys (a, d, n, w, 1-9) act only while "
        "the input is empty; a and d ask for a second press, and any other key after the first "
        "one is typed, the first letter included. Select text with the mouse while holding "
        "shift (option in iTerm); alt+c copies a block."
    )
    lines += [Text(""), Text("Typing and selecting", f"bold {palette.accent}")]
    lines += [Text.assemble("  ", row) for row in render.wrap(Text(typing, palette.fg), width - 4)]
    missing = [entry for entry in CAPABILITIES if not caps.has(entry.key)]
    if missing:
        lines += [Text(""), Text("Not offered by this session", f"bold {palette.accent}")]
        for entry in missing:
            text = Text(f"{entry.title}: {entry.reason}", palette.muted)
            lines += [Text.assemble("  ", row) for row in render.wrap(text, width - 4)]
    return lines
