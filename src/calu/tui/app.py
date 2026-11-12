"""The full-screen client of a running session (`calu attach` on a terminal).

    Link (connection worker) ──Change──► CaluApp._changed ──► the widgets render the model
              ▲                                                        │
              └──────── Actions.request ◄── keys, slash commands, palette, clicks

The app holds no session data: `calu.tui.model.SessionModel` does. The app holds what belongs
to this view only -- the selected agent tab, the send mode of the composer, which open decision
the card shows, whether the side questions are open -- and turns keys into calls of
`calu.tui.actions.Actions`.

The screen, top to bottom: status bar, agent tabs, the transcript with the decision card under
it while something waits for the user (and the side panel beside both on a wide terminal), the
composer over the full width, a line of keys. Nothing that asks for a decision takes the
keyboard away: the card sits between transcript and composer, the transcript stays scrollable
and the composer usable.

Keys while the composer is empty and a decision is open: `a a` allows, `d d` denies (the second
press confirms, any other key types), `n` writes a note first, `w` shows what the checker saw;
for a question `1`-`9` choose and Enter sends. `alt+a`, `alt+d`, `alt+n`, `alt+w` do the same
whatever the composer holds.
"""

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from calu.acp.modes import LADDER
from calu.tui import completion, gittool, render, settings
from calu.tui.actions import Actions, Outcome
from calu.tui.adminaccess import (
    MODES,
    AdminAccess,
    AdminFailed,
    PasswordNeeded,
    StoreAdmin,
    weakens,
)
from calu.tui.capabilities import CAPABILITIES, Capabilities
from calu.tui.commands import COMMANDS, CaluCommands, by_slash, completions
from calu.tui.connection import Connector, Link
from calu.tui.difftools import unified_text
from calu.tui.export import transcript_markdown
from calu.tui.model import (
    ALL,
    MAIN,
    Block,
    Change,
    Decision,
    SessionModel,
    StepBlock,
    TextBlock,
    TurnEndBlock,
)
from calu.tui.screens.checkers import CheckersScreen
from calu.tui.screens.confirm import ConfirmScreen
from calu.tui.screens.detail import DetailScreen, proposed_diffs
from calu.tui.screens.diff import DiffScreen
from calu.tui.screens.password import PasswordScreen
from calu.tui.screens.text import TextScreen
from calu.tui.theme import CALU_DARK, CALU_LIGHT, DARK, Palette, palette_for
from calu.tui.widgets.chrome import AgentTabs, Line, Lines, NewBelow, PanelList, Picked
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
    toasts: bool = True
    """Show a short notice for what happened elsewhere (a decision by another client, a
    warning of the session, a changed supervision level)."""
    panel_width: int = 36
    """Columns of the side panel on a wide terminal."""
    git_tool: str = ""
    """`git_tool` of `tui.toml`: the command line `f4` starts ("" picks an installed one)."""
    keys: dict[str, str] = field(default_factory=dict)
    """Binding id -> the keys that replace its default (`[keys]` of `tui.toml`)."""
    settings_path: Path | None = None
    """Where `/theme`, `/bell` and `alt+t` store their choice; None stores nothing."""
    history_path: Path | None = None
    """Where the sent texts are kept between runs; None keeps them for this run only."""
    history: bool = True
    """Whether the stored history of the session is shown above the replayed events."""
    problems: list[str] = field(default_factory=list)
    """What was wrong with the settings file; shown once after the start."""


@dataclass
class Ask:
    """A yes-or-no question shown in the composer's header until `y`, `n` or Escape."""

    prompt: str
    yes: Callable[[], None]
    no: Callable[[], None] | None = None


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
        Binding("f4", "command('git')", show=False, priority=True, id="git"),
        Binding("alt+a", "decide('allow')", show=False, priority=True, id="allow"),
        Binding("alt+d", "decide('deny')", show=False, priority=True, id="deny"),
        Binding("alt+n", "command('note')", show=False, priority=True, id="note"),
        Binding("alt+w", "command('detail')", show=False, priority=True, id="detail"),
        Binding("alt+t", "command('thinking')", show=False, priority=True, id="thinking"),
        Binding("alt+c", "command('copy')", show=False, priority=True, id="copy"),
        Binding("ctrl+f", "command('search')", show=False, priority=True, id="search"),
        Binding("alt+h", "command('hold')", show=False, priority=True, id="hold"),
        Binding("alt+e", "command('expand')", show=False, priority=True, id="expand"),
        *_digit_bindings(),
    ]

    def __init__(
        self,
        connector: Connector,
        options: TuiOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
        live: bool = True,
        admin: AdminAccess | None = None,
    ) -> None:
        """Create the client; `run` or `run_test` starts it.

        Args:
            connector: Opens the connection to the session (`protocol_connector`, or
                `fake_connector` in tests).
            options: How the client starts; the defaults if None.
            clock: The client clock in seconds; tests pass one they control.
            live: False draws every change at once and starts no timer, so that tests and
                screenshots are deterministic.
            admin: Stores what the client changes in the database with the `admin`
                credential (checker instructions, `honesty.mode`); the local credential
                store if None.
        """
        super().__init__()
        self.admin: AdminAccess = admin if admin is not None else StoreAdmin()
        self.environ: Mapping[str, str] = os.environ
        """The environment `f4` reads `CALU_GIT_TOOL` from."""
        self.which: gittool.Which = shutil.which
        """Finds a program on the search path; tests replace it."""
        self.run_program: Callable[[Sequence[str], str], int] = self._run_program
        """Hands the terminal to a program in a directory and returns its exit code; tests
        replace it."""
        self.describe_commit: Callable[[str, str], str] = describe_commit
        """Names a commit of a repository for the question of `/trust`; tests replace it."""
        self._install_shown = False
        self._honesty_page: TextScreen | None = None
        self.options = options or TuiOptions()
        self.clock = clock
        self.live = live
        self.model = SessionModel()
        self.link = Link(
            connector,
            self.model,
            self._changed,
            clock,
            self.options.after_seq,
            history=self.options.history,
        )
        self.actions = Actions(self.link, self.model)
        self.palette: Palette = palette_for({})
        self.mode = "chat"
        """What Enter does with the composer's text: chat, note, answer, side or search."""
        self.ask: Ask | None = None
        """A yes-or-no question that waits for `y` or `n`; None otherwise."""
        self.held: list[str] = []
        """Drafts kept in this client, sent one per idle moment of the session; they are not
        part of the session's queue and are lost when the client ends."""
        self.next_model: str | None = None
        """The model the next start asks for (`/model` while the session is stopped)."""
        self._was_working = False
        self.terminal_focused = True
        """False while the terminal reports that its window has lost the focus."""
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
            Status bar, agent tabs, the body (transcript and decision card beside the side
            panel), the composer with its header, and the line of keys.
        """
        yield Line("", id="status")
        yield AgentTabs(id="tabs")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Lines(id="agent-head")
                yield Transcript(self.model, self.palette, id="transcript")
                yield NewBelow("", id="new-below")
                yield Lines(id="card")
            with VerticalScroll(id="side"):
                yield PanelList(id="panel-lines")
                yield Lines(id="side-lines")
        yield Line("", id="composer-head")
        with Horizontal(id="composer-row"):
            yield Static(">", id="prompt")
            yield Composer(id="composer")
        yield Line("", id="footer")

    def on_mount(self) -> None:
        """Register the themes, start the connection and the clock, draw the empty screen."""
        self.register_theme(CALU_DARK)
        self.register_theme(CALU_LIGHT)
        self.transcript.thinking_open = self.options.show_thinking
        self.composer.intercept = self._composer_key
        self.composer.cursor_blink = self.live
        if self.options.history_path is not None:
            self.composer.sent = settings.load_history(self.options.history_path)
        if self.options.keys:
            known = {binding.id for binding in self.BINDINGS if isinstance(binding, Binding)}
            unknown = sorted(set(self.options.keys) - known)
            if unknown:
                self.options.problems.append(
                    f"tui.toml: no binding is called {', '.join(unknown)} (f1 lists the ids)"
                )
            self.set_keymap({k: v for k, v in self.options.keys.items() if k in known})
        self._apply_theme(self.options.theme)
        self._breakpoints()
        self.run_worker(self._run_link(), name="link", group="link", exit_on_error=False)
        if self.live:
            self.set_interval(1.0, self._second)
        self.render_all()
        if self.options.problems:
            self.flash(" · ".join(self.options.problems), error=True)

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
        self.query_one("#panel-lines", PanelList).highlight = f"on {self.palette.user_bg}"
        self.transcript.rebuild()
        self.render_all()

    def _breakpoints(self) -> None:
        """Set the classes the layout depends on: narrow, short, panel and side open."""
        self.set_class(self.size.width < WIDE, "-narrow")
        self.set_class(self.size.height < TALL, "-short")
        self.set_class(self.panel_open, "-panel")
        self.set_class(self.side_open, "-side")
        side = self.query_one("#side")
        if self.size.width >= WIDE:
            side.styles.width = self.options.panel_width + (10 if self.side_open else 0)
        else:
            side.styles.width = None
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
        if change.honesty and self._honesty_page is not None and self._honesty_page.is_attached:
            self._honesty_page.set_body(render.honesty_page(self.model, self.view()))
        transcript = self.transcript
        transcript.now = self.clock()
        if change.rebuilt:
            transcript.rebuild()
        else:
            transcript.apply(change.blocks_added, change.blocks_updated)
        if any(isinstance(self.model.blocks[index], TurnEndBlock) for index in change.blocks_added):
            self._usage_asked = False
            if not self.terminal_focused and not change.rebuilt and self.options.bell:
                self.bell()  # a turn ended while the user looks at another window
        if (
            not self._usage_asked
            and self.link.connection is not None
            and self.capabilities.has("usage")
        ):
            self._usage_asked = True
            self._spawn(self._refresh_usage())
        self._check_target()
        self._send_held()
        self.render_all()
        for notice in change.notices if self.options.toasts else ():
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
        stopped = decision is None and self.model.state in {"stopped", "failed"}
        card.set_class(decision is not None or stopped, "-shown")
        if decision is None:
            if self.armed:
                self.disarm()
            width = max(30, self.query_one("#main").size.width - 6)
            card.show(
                render.start_card_lines(
                    self.model, self.view(width), self.next_model, self.next_effort
                )
                if stopped
                else []
            )
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
        panel = self.query_one("#panel-lines", PanelList)
        lines, targets = render.panel_lines(
            self.model,
            self.view(self._side_width()),
            self.capabilities,
            selected.escalation_id if selected else None,
            self.held,
            panel.has_focus,
        )
        panel.show(lines, targets)

    def _side_width(self) -> int:
        """Return the width of the inside of the side column.

        Returns:
            The columns the texts of the panel may use.
        """
        if self.size.width < WIDE:
            return min(44, self.size.width * 9 // 10) - 4
        return self.options.panel_width + (10 if self.side_open else 0) - 4

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
        if self.ask is not None:
            return f"{self.ask.prompt}  y yes · n no · esc cancels"
        if self.mode == "search":
            transcript = self.transcript
            if not transcript.needle:
                return "search the transcript · type the text · esc ends"
            if not transcript.matches:
                return "search: no line contains this · esc ends"
            return (
                f"search: match {transcript.match + 1} of {len(transcript.matches)} · "
                "enter or ↑ older · ↓ newer · esc ends"
            )
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
            return "side question · not part of the conversation · esc closes"
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
        if self.ask is not None:
            line.append(self.mode_text(), f"bold {palette.orange}")
        elif self.message:
            line.append(
                self.message, f"bold {palette.red}" if self.message_error else palette.orange
            )
        elif text.startswith("/") and " " not in text and "\n" not in text and self.ask is None:
            found = completions(text)
            line.append(
                "  ".join(found[:8]) or "no such command (/help lists them)", palette.accent
            )
            if found:
                line.append("  · tab completes", palette.faint)
        elif self.ask is None and (paths := self._path_completions()) is not None:
            if paths[1]:
                line.append("  ".join(paths[1][:6]), palette.accent)
                more = len(paths[1]) - 6
                line.append(f"  +{more} more" if more > 0 else "", palette.faint)
                line.append("  · tab completes", palette.faint)
            elif not completion.complete_path(self.model.workspace, ""):
                line.append(
                    "@ completes project paths only where the project directory is on this machine",
                    palette.faint,
                )
            else:
                line.append("no project path starts like this", palette.faint)
        else:
            special = self.mode != "chat" or self.target != MAIN or self.ask is not None
            line.append(self.mode_text(), f"bold {palette.accent}" if special else palette.muted)
            if self.mode == "chat" and len(self._send_modes()) > 1:
                line.append("  · tab changes", palette.faint)
        line.truncate(max(10, self.size.width - 2), overflow="ellipsis")
        self.query_one("#composer-head", Line).show(line)

    def _render_footer(self) -> None:
        """Draw the line of keys: what works now, most useful first, cut to the width."""
        palette = self.palette
        decision = self.current_decision()
        typed = bool(self.composer.text)
        parts: list[tuple[int, str, str]] = []
        if self._escape_at is not None:
            parts.append((9, "esc", "again to interrupt"))
        if self.ask is not None:
            parts += [(9, "y", "yes"), (9, "n", "no"), (9, "esc", "cancel")]
        elif self.mode == "search":
            parts += [(9, "enter ↑", "older"), (9, "↓", "newer"), (9, "esc", "end search")]
        elif self.focused is self.transcript:
            parts += [
                (9, "j k", "move"),
                (9, "enter", "expand"),
                (8, "e", "next waiting"),
                (8, "D", "diff"),
                (7, "/", "search"),
                (6, "i", "agent task"),
                (7, "t", "thinking"),
                (7, "c", "copy"),
                (9, "esc", "back to input"),
            ]
        elif isinstance(self.focused, PanelList):
            kind = self.focused.cursor[0] if self.focused.cursor else ""
            parts.append((9, "j k", "move"))
            parts += {
                "decision": [(9, "enter", "show on the card")],
                "agent": [(9, "enter", "show its tab"), (8, "i", "its task")],
                "queue": [
                    (9, "enter", "whole text"),
                    (8, "c", "copy"),
                    (8, "r", "reuse in the input"),
                ],
                "held": [(9, "enter", "edit"), (9, "s", "send now"), (9, "d", "delete")],
                "mark": [(9, "enter", "detail"), (9, "x", "clear mark")],
            }.get(kind, [])
            parts.append((9, "esc", "back to input"))
        elif decision is None and self.model.state in {"stopped", "failed"} and not typed:
            parts += [(9, "enter", "start the session")]
        elif self.armed:
            parts.append((9, self.armed[0], f"again to {self.armed}"))
        elif decision is not None and decision.is_question:
            parts += [(9, "1-9", "choose"), (9, "o", "own answer"), (9, "enter", "send")]
        elif decision is not None and typed:
            parts += [(9, "alt+a", "allow"), (9, "alt+d", "deny"), (9, "alt+w", "detail")]
        elif decision is not None:
            parts += [(9, "a a", "allow"), (9, "d d", "deny"), (8, "n", "note"), (9, "w", "detail")]
        stopped = self.model.state in {"stopped", "failed"}
        if self.model.working():
            parts += [(5, "esc esc", "interrupt"), (3, "tab", "send mode")]
        elif (not stopped or typed) and self.mode != "search" and self.ask is None:
            parts += [(5, "enter", "send"), (1, "^j", "newline")]
        parts += [(7, "alt+←/→", "agent"), (4, "pgup/pgdn", "scroll"), (6, "f3", "side question")]
        parts.append((3, "f4", "git"))
        parts.append((6 if self.size.width < WIDE else 1, "f2", "panel"))
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
        elif message.kind == "follow":
            self.transcript.follow()
        self.composer.focus()

    def on_panel_list_moved(self) -> None:
        """Show the keys of the entry the panel's cursor went to."""
        self._render_footer()

    def on_panel_list_act(self, message: PanelList.Act) -> None:
        """Carry out a key pressed on an entry of the side panel.

            decision   enter shows it on the card
            agent      enter or m shows its tab (and aims the composer at it if it takes
                       messages); i shows the task it was given and its result
            queue      enter shows the whole text, c copies it, r copies it into the composer
            held       enter or e moves the draft back into the composer, s sends it now,
                       d deletes it
            mark       enter shows the mark in full, x clears it after a yes

        Args:
            message: The entry and the key.
        """
        kind, key, pressed = message.kind, message.key, message.pressed
        if kind == "decision" and pressed == "enter":
            self.selected = key
            self.disarm()
            self.composer.focus()
        elif kind == "agent" and pressed in {"enter", "m"}:
            self.select_tab(key)
            self.composer.focus()
        elif kind == "agent" and pressed == "i":
            self.run_command("agent_info", key)
        elif kind == "queue":
            entry = next((queued for queued in self.model.queue if queued.id == key), None)
            if entry is None:
                return
            if pressed == "enter":
                to = self.model.label(None if entry.to == MAIN else entry.to)
                how = "after the running tool calls" if entry.delivery == "inject" else "next turn"
                self._page(
                    f"Queued message to {to} ({how}) · Claude has not read it yet",
                    [Text(row, self.palette.fg) for row in entry.text.splitlines()],
                )
            elif pressed == "c":
                self.copy_to_clipboard(entry.text)
                self.flash("queued message copied")
            elif pressed == "r":
                self.composer.set_text(entry.text)
                self.composer.focus()
                self.flash(
                    "copied into the input; the queued message stays queued: "
                    + str(self.capabilities.reason("queue_withdraw"))
                )
        elif kind == "held" and key.isdigit() and int(key) < len(self.held):
            index = int(key)
            if pressed in {"enter", "e", "r"}:
                self.composer.set_text(self.held.pop(index))
                self.composer.focus()
                self.flash("the draft is back in the input and no longer held")
            elif pressed == "d":
                del self.held[index]
                self.flash("draft deleted")
            elif pressed == "s":
                text = self.held.pop(index)
                self._spawn(self._send(text, MAIN, "queued"))
        elif kind == "mark":
            mark = next((entry for entry in self.model.marks if str(entry.id) == key), None)
            if mark is None:
                return
            spot = f"{mark.path}:{mark.symbol}" if mark.symbol else mark.path
            if pressed == "enter":
                rows = [
                    ("mark", str(mark.id)),
                    ("where", spot),
                    ("branch", mark.git_branch or "not recorded"),
                    ("mode", self.model.honesty_mode),
                ]
                body = [
                    Text.assemble((f"{name:<8}", self.palette.muted), (value, self.palette.fg))
                    for name, value in rows
                ]
                body += [Text(""), *render.wrap(Text(mark.reason, self.palette.fg), 90)]
                body += [
                    Text(""),
                    Text(
                        "The checker closes the mark when name and documentation fit the code "
                        f"again. /clear-mark {mark.id} closes it by hand.",
                        self.palette.muted,
                    ),
                ]
                self._page("Honesty mark", body)
            elif pressed == "x":
                self._ask(
                    f"clear the honesty mark on {render.cut(spot, 40)}?",
                    partial(self.run_command, "clear_mark", key),
                )
        self.render_all()

    def on_app_blur(self) -> None:
        """Note that the terminal window lost the focus (the bell then also marks a turn's end)."""
        self.terminal_focused = False

    def on_app_focus(self) -> None:
        """Note that the terminal window has the focus again."""
        self.terminal_focused = True

    def on_descendant_focus(self) -> None:
        """Show the keys of the widget that got the focus, and the panel's hints with it."""
        self._render_footer()
        self._render_panel()

    def on_descendant_blur(self) -> None:
        """Draw the panel without its hints once it has lost the focus."""
        self.call_after_refresh(self._render_panel)

    def on_text_area_changed(self) -> None:
        """Redraw header and footer when the composer's text changes; search as it is typed."""
        if self.mode == "search":
            self.transcript.search(self.composer.text.strip())
        self._render_head()
        self._render_footer()

    def on_key(self, event: events.Key) -> None:
        """Send a printable key pressed outside the composer into the composer.

        Typing therefore never goes nowhere: a letter pressed while the transcript has the
        focus (and that is no key of the transcript) moves the focus back and is typed. In the
        transcript `t` toggles thinking, `c` copies the block under the cursor and `w` opens
        the detail of the open decision.

        Args:
            event: The key.
        """
        if len(self.screen_stack) > 1 or self.focused is self.composer:
            return
        in_transcript = {
            "t": "thinking",
            "c": "copy",
            "w": "detail",
            "D": "diff",
            "i": "agent_info",
            "slash": "search",
        }
        if self.focused is self.transcript and event.key in in_transcript and self.ask is None:
            event.stop()
            self.run_command(in_transcript[event.key])
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
            "detach",
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
        if self.ask is not None:
            ask = self.ask
            if key in {"y", "n", "enter"}:  # Enter is no: the answer of a key pressed unread
                self.ask = None
                chosen = ask.yes if key == "y" else ask.no
                if chosen is not None:
                    chosen()
                self.render_all()
            return True
        if self.mode == "search":
            if key in {"up", "down"}:
                self.transcript.search_step(-1 if key == "up" else 1)
                self._render_head()
                return True
            return False
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
        if key == "up" and self.held and not composer.sent:
            self.query_one("#panel-lines", PanelList).focus()
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
            decision.note = self.composer.value.strip()
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
        typed = self.composer.value.strip()
        if self.mode == "chat" and typed and not typed.startswith("/") and not decision.is_question:

            def with_note() -> None:
                """Decide with the composer's text as the note for Claude."""
                current = self.current_decision()
                if current is not None:
                    current.note = typed
                    self.composer.set_text("")
                    self.decide_now(wanted)

            self._ask(
                f"send the text in the input as a note with the {wanted}?",
                with_note,
                partial(self.decide_now, wanted),
            )
            return
        self.decide_now(wanted)

    def _ask(
        self, prompt: str, yes: Callable[[], None], no: Callable[[], None] | None = None
    ) -> None:
        """Ask a yes-or-no question in the composer's header.

        The next `y` or `n` answers it and is not typed; Escape cancels it. Every other key is
        ignored while the question is open, so that an answer is never given by accident.

        Args:
            prompt: The question.
            yes: Called for `y`.
            no: Called for `n`; None does nothing.
        """
        self.ask = Ask(prompt, yes, no)
        self.composer.focus()
        self.render_all()

    def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Handle Enter in the composer according to the mode.

        Args:
            message: The message with the composer's text.
        """
        text = message.text.strip()
        composer = self.composer
        decision = self.current_decision()
        if self.ask is not None:
            return
        if self.mode == "search":
            self.transcript.search_step(-1)
            self._render_head()
            return
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
            elif self.model.state in {"stopped", "failed"}:
                self.run_command("start")
            return
        self._remember(message.text)
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
        paths = self._path_completions()
        if paths is not None:
            (row, start, column), found = paths
            if found:
                grown = found[0] if len(found) == 1 else completion.common_prefix(found)
                self.composer.replace(
                    grown + ("" if grown.endswith("/") or len(found) > 1 else " "),
                    (row, start + 1),
                    (row, column),
                )
                self._render_head()
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
        text = self.composer.value
        self._remember(text)
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

        In this order: cancel a yes-or-no question; forget a first decision key; end a search;
        leave note or answer mode; close the side
        questions; close the side panel on a narrow screen; return the focus to the composer;
        and while the session works, a second Escape within `DOUBLE_ESCAPE_S` interrupts the
        turn.
        """
        if self.ask is not None:
            self.ask = None
        elif self.armed:
            self.disarm()
        elif self.mode == "search":
            self.mode = "chat"
            self.transcript.search("")
            self.composer.set_text("")
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
            self.composer.focus()
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
        """Leave the client; the session keeps running.

        Drafts that are still held would be lost, so with held drafts the client asks first.
        """
        if self.held and self.ask is None and len(self.screen_stack) == 1:
            count = len(self.held)
            what = "1 held draft is" if count == 1 else f"{count} held drafts are"
            self._ask(f"{what} not sent and will be lost: detach?", self._leave)
            return
        self._leave()

    def _leave(self) -> None:
        """Close the connection and end the app."""
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
        """Start the session, or resume a stored one (`/start [resume id | new]`).

        The start uses the model and the effort chosen with `/model` and `/effort` since the
        session stopped. A running session is not started again: with an id it is stopped
        and resumed with that conversation after a confirmation (`_resume`).

        Args:
            rest: A Claude session id to resume, or `new` for a new conversation; empty
                resumes the conversation of this session if it has one.
        """
        running = self.model.state not in {"stopped", "failed"}
        if running and rest and rest != "new":
            self._resume(rest)
            return
        resume = None if rest == "new" else (rest or self.model.claude_session_id)
        other = resume != self.model.claude_session_id
        self._spawn(self._restart(resume, self.next_model, self.next_effort, reload=other))

    def _cmd_shutdown(self, rest: str) -> None:
        """Shut down calu after a typed confirmation (`/shutdown`, or `/shutdown confirm`).

        Args:
            rest: `confirm` shuts down at once; empty asks to type the word `shutdown`.
        """
        if rest == "confirm":
            self._request(self.actions.shutdown(), "calu shuts down")
            return

        def answered(confirmed: bool | None) -> None:
            """Shut down if the word was typed.

            Args:
                confirmed: The answer of the confirmation screen.
            """
            if confirmed:
                self._request(self.actions.shutdown(), "calu shuts down")

        self.push_screen(
            ConfirmScreen(
                "Shut down calu?",
                [
                    "the calu process of this session ends, and with it Claude's session",
                    "the running turn, its tool calls, background tasks and subagents end",
                    "steps that wait for a decision are cancelled",
                    "calu run has to be started again in the project directory",
                ],
                self.palette,
                typed="shutdown",
            ),
            answered,
        )

    def _cmd_model(self, rest: str) -> None:
        """Change the model, or list the models to choose from (`/model [name]`).

            session runs, it can change the model   the model changes from Claude's next step
            session runs, it cannot                 stop and resume with it, after a yes
            session stopped                         the model is set for the start (Enter)

        Args:
            rest: An alias or a model id; empty opens the list.
        """
        running = self.model.state not in {"stopped", "failed"}
        live = self.capabilities.has("live_model_change")
        if not rest:
            how = (
                "applies from Claude's next step"
                if running and live
                else "stops and resumes the session with it, after a confirmation"
                if running
                else "used by the start (enter)"
            )
            entries = [(name, partial(self.run_command, "model", name), how) for name in MODELS]
            self.search_commands(entries, placeholder="model… (/model <id> takes any model id)")
            return
        if running and live:
            self._request(self.actions.set_model(rest))
        elif running:
            self.next_model = rest
            self.run_command("restart")
        else:
            self.next_model = rest
            self.flash(f"model {rest} is set for the start: enter starts the session")

    def _cmd_effort(self, rest: str) -> None:
        """Choose the effort (`/effort [level]`).

        A session takes its effort when it starts. For a stopped session the choice is used
        by the start; for a running one the client offers to stop and resume the conversation
        with it, and keeps the choice for the next start if the user declines.

        Args:
            rest: One of `EFFORTS`; empty opens the list.
        """
        running = self.model.state not in {"stopped", "failed"}
        if not rest:
            how = (
                "stops and resumes the session with it, after a confirmation"
                if running
                else "used by the start (enter)"
            )
            entries = [(name, partial(self.run_command, "effort", name), how) for name in EFFORTS]
            self.search_commands(entries, placeholder="effort…")
            return
        if rest not in EFFORTS:
            self.flash(f"effort is one of: {', '.join(EFFORTS)}", error=True)
            return
        self.next_effort = rest
        if running:
            self.run_command("restart")
        else:
            self.flash(f"effort {rest} is set for the start: enter starts the session")

    def _cmd_restart(self, rest: str) -> None:
        """Stop the session and resume its conversation (`/restart [model] [effort]`).

        This is how a running session gets another effort (and another model where it cannot
        change the model while it runs). The client lists what the stop ends and asks for a
        yes first. A stopped session is started without a question.

        Args:
            rest: A model, an effort, or both, in any order; empty uses what `/model` and
                `/effort` chose.
        """
        for word in rest.split():
            if word in EFFORTS:
                self.next_effort = word
            else:
                self.next_model = word
        model, effort = self.next_model, self.next_effort
        resume = self.model.claude_session_id
        if self.model.state in {"stopped", "failed"}:
            self._spawn(self._restart(resume, model, effort))
            return
        chosen = f"{model or self.model.model_name or 'default'} / " + (
            effort or self.model.effort or "default"
        )

        def answered(confirmed: bool | None) -> None:
            """Restart after a yes; keep the choice for the next start after a no.

            Args:
                confirmed: The answer of the confirmation screen.
            """
            if confirmed:
                self._spawn(self._restart(resume, model, effort))
            else:
                self.flash(f"not restarted; {chosen} stays chosen for the next start")

        self.push_screen(
            ConfirmScreen(
                f"Stop the session and resume this conversation with {chosen}?",
                [
                    "the running turn ends; tool calls that run now are interrupted",
                    "background tasks and subagents of this session are lost",
                    "messages in the queue that Claude has not read are lost",
                    "steps that wait for a decision are cancelled",
                    "the conversation itself is kept: Claude resumes it"
                    if resume
                    else "the session has no conversation yet: a new one starts",
                ],
                self.palette,
            ),
            answered,
        )

    async def _restart(
        self, resume: str | None, model: str | None, effort: str | None, reload: bool = False
    ) -> None:
        """Stop if needed, start, and show the result.

        After a failed start the chosen model and effort stay, so that Enter on the start
        card tries again with them.

        Args:
            resume: The Claude session id to resume; None starts a new conversation.
            model: The model for the start; None keeps the setting.
            effort: The effort for the start; None keeps the setting.
            reload: The start shows another conversation: forget the transcript and load the
                stored history of the new one.
        """
        outcome = await self.actions.restart(resume, model, effort)
        if outcome.ok:
            self.next_model = self.next_effort = None
            if reload:
                self.link.reload()
        self.report(outcome)
        self.render_all()

    def _resume(self, claude_session_id: str) -> None:
        """Resume another stored conversation, after a yes if a session runs.

        Args:
            claude_session_id: The Claude session id of the conversation.
        """
        if self.model.state in {"stopped", "failed"}:
            self._spawn(
                self._restart(claude_session_id, self.next_model, self.next_effort, reload=True)
            )
            return

        def answered(confirmed: bool | None) -> None:
            """Stop and resume after a yes.

            Args:
                confirmed: The answer of the confirmation screen.
            """
            if confirmed:
                self._spawn(
                    self._restart(claude_session_id, self.next_model, self.next_effort, reload=True)
                )

        self.push_screen(
            ConfirmScreen(
                f"Stop this session and resume the conversation {claude_session_id[:8]}?",
                [
                    "the running turn ends; tool calls that run now are interrupted",
                    "background tasks and subagents of this session are lost",
                    "messages in the queue that Claude has not read are lost",
                    "the conversation shown now stays stored and can be resumed later",
                ],
                self.palette,
            ),
            answered,
        )

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

        screen.reload = lambda: self._spawn(load())
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
        """Open the checkers screen: their instructions, to read and to edit (`/checkers`).

        The list comes from the session (`checkers.get`). Saving an edit uses the `admin`
        credential of this machine (`self.admin`), never the session.

        Args:
            rest: Ignored.
        """

        async def load() -> tuple[list[dict[str, Any]], str]:
            """Ask the session for its checkers.

            Returns:
                `(checkers, "")`, or `([], the refusal)`.
            """
            outcome = await self.actions.read("checkers.get")
            if not outcome.ok:
                return [], outcome.message
            return [dict(entry) for entry in (outcome.data or {}).get("checkers") or []], ""

        self.push_screen(CheckersScreen(load, self.admin, self.palette))

    # ------------------------------------------------------------------ honesty

    def _cmd_honesty(self, rest: str) -> None:
        """Show the honesty state, or change the mode (`/honesty [off | track | enforce]`).

        Without an argument a page shows the mode, the baseline, the marked code, the
        unreviewed files and whether a review is required or running. With a mode the
        setting `honesty.mode` is stored with the `admin` credential of this machine, after a
        confirmation when the new mode enforces less; the session announces the mode it
        then uses. Without the credential the command that does the same is shown.

        Args:
            rest: Empty, or the mode.
        """
        wanted = rest.strip().lower()
        if not wanted:
            self._honesty_page = self._page(
                "Code honesty", render.honesty_page(self.model, self.view())
            )
            return
        if wanted not in MODES:
            self.flash(f"usage: /honesty [{' | '.join(MODES)}]", error=True)
            return
        if wanted == self.model.honesty_mode:
            self.flash(f"honesty.mode is {wanted} already")
            return
        problem = self.admin.problem()
        if problem is not None:
            self.flash(
                f"not changed here; use: calu config set honesty.mode {wanted} · {problem}",
                error=True,
            )
            return

        def store(problem: str = "") -> None:
            """Ask for the password if one is needed, then store the mode.

            Args:
                problem: What was wrong with the password given before, or "".
            """

            async def write(password: str | None) -> None:
                """Store the mode and say where, or why it was not stored.

                Args:
                    password: The credential's password, or None if it has none.
                """
                try:
                    saved = await self.admin.set_honesty_mode(wanted, self.model.project, password)
                except PasswordNeeded as error:
                    store(str(error))
                    return
                except AdminFailed as error:
                    self.flash(f"honesty.mode not changed: {error}", error=True)
                    return
                self.flash(
                    f"honesty.mode is now {saved.mode} ({saved.scope} setting) · the session "
                    "shows the mode it uses in the side panel"
                )

            def answered(password: str | None) -> None:
                """Go on with the password; without one nothing is changed.

                Args:
                    password: What the user typed, or None.
                """
                if password is None:
                    self.flash("honesty.mode not changed: no password given", error=True)
                else:
                    self._spawn(write(password))

            if self.admin.needs_password():
                self.push_screen(
                    PasswordScreen(f"It sets honesty.mode to {wanted}.", self.palette, problem),
                    answered,
                )
            else:
                self._spawn(write(None))

        if not weakens(self.model.honesty_mode, wanted):
            store()
            return
        loses = {
            "track": [
                "code that is marked or unreviewed may run and be committed again",
                "marks and unreviewed files are still recorded and shown",
            ],
            "off": [
                "no marks are recorded and nothing is blocked because of them",
                "commits are no longer certified by the session",
            ],
        }[wanted]

        def confirmed(yes: bool | None) -> None:
            """Store the weaker mode after a yes.

            Args:
                yes: The answer of the confirmation.
            """
            if yes:
                store()
            else:
                self.flash(f"honesty.mode stays {self.model.honesty_mode}")

        self.push_screen(
            ConfirmScreen(
                f"Change honesty.mode from {self.model.honesty_mode} to {wanted}?",
                loses,
                self.palette,
            ),
            confirmed,
        )

    def _cmd_trust(self, rest: str) -> None:
        """Certify a commit as honest after a yes (`/trust [commit or branch]`).

        The question names the commit: its short id and subject line if the project
        directory is on this machine, else the name as typed (HEAD without one). Only `y`
        certifies; `n`, Enter and Escape do not.

        Args:
            rest: A commit id or a branch name; empty means HEAD.
        """
        rev = rest.strip()
        if len(rev.split()) > 1:
            self.flash("usage: /trust [commit or branch]", error=True)
            return
        named = self.describe_commit(self.model.workspace, rev or "HEAD") or (rev or "HEAD")

        def certify() -> None:
            """Send the certification and report what the session recorded."""

            async def run() -> None:
                """Await the request and show the trusted commit or the refusal."""
                outcome = await self.actions.trust(rev)
                if outcome.ok:
                    commit = str((outcome.data or {}).get("commit_id") or "")[:10]
                    self.flash(f"trusted: {commit or named} is certified as honest")
                else:
                    self.flash(f"/trust: {outcome.message}", error=True)
                self.render_all()

            self._spawn(run())

        room = max(12, self.size.width - 46)
        self._ask(
            f"certify {render.cut(named, room)} as honest? · y yes · n no (default)",
            certify,
            lambda: self.flash("nothing was certified"),
        )

    def _cmd_trust_file(self, rest: str) -> None:
        """Certify the present content of one file as honest after a yes (`/trust-file <path>`).

        Args:
            rest: The path, relative to the project directory or absolute.
        """
        path = rest.strip()
        if not path:
            self.flash("usage: /trust-file <path>", error=True)
            return
        room = max(12, self.size.width - 68)
        self._ask(
            f"certify the present content of {render.cut(path, room)} as honest? · "
            "y yes · n no (default)",
            lambda: self._request(
                self.actions.trust_file(path), f"trusted: the content of {path} is certified"
            ),
            lambda: self.flash("nothing was certified"),
        )

    def _cmd_honesty_review(self, rest: str) -> None:
        """Ask calu to review the unreviewed files now (`/honesty-review`).

        Args:
            rest: Ignored.
        """
        self._request(self.actions.honesty_review(), "the honesty review was asked for")

    # ------------------------------------------------------------------ git program

    def _run_program(self, argv: Sequence[str], cwd: str) -> int:
        """Hand the terminal to a program and take it back when the program ends.

        The client draws nothing and reads no key in the meantime. Events of the session
        are not read either; after a long stay the session may have closed the connection,
        and the client then attaches again and catches up, as after any lost connection.

        Args:
            argv: The command line.
            cwd: The directory the program starts in.

        Returns:
            The exit code of the program; 127 if it could not be started.

        Raises:
            textual.app.SuspendNotSupported: If this terminal driver cannot be suspended.
        """
        with self.suspend():
            try:
                return subprocess.call(list(argv), cwd=cwd)
            except OSError:
                return 127

    def _launch(self, argv: Sequence[str], what: str) -> int | None:
        """Run a program in the session's project directory and redraw afterwards.

        Args:
            argv: The command line.
            what: The program's name for the message.

        Returns:
            The exit code, or None if the terminal could not be handed over.
        """
        try:
            code = self.run_program(argv, self.model.workspace)
        except SuspendNotSupported:
            self.flash(f"{what} needs a real terminal: this one cannot be handed over", error=True)
            return None
        self.refresh(layout=True)
        if code == 127:
            self.flash(f"{what} could not be started", error=True)
        return code

    def _cmd_git(self, rest: str) -> None:
        """Open a terminal git program in the session's project directory (`f4`, `/git`).

        The program is `CALU_GIT_TOOL`, `git_tool` of `tui.toml`, or the first installed of
        lazygit, gitui and tig; without any of them `git diff` through the pager, after a
        notice (once per run) that names the command which installs lazygit. That command is
        run only after a yes and only if it needs no administrator rights; otherwise it is
        shown for the user to run. The client has no git viewer of its own.

        Args:
            rest: Ignored.
        """
        problem = gittool.usable_directory(self.model.workspace)
        if problem is not None:
            self.flash(problem, error=True)
            return
        tool = gittool.choose(self.environ, self.options.git_tool, self.which)
        if isinstance(tool, gittool.Missing):
            self.flash(f"f4: {tool.reason}", error=True)
            return

        def run(chosen: gittool.GitTool) -> None:
            """Start the program and say that the client is back.

            Args:
                chosen: The program.
            """
            code = self._launch(chosen.argv, chosen.name)
            if code is not None and code != 127:
                hint = (
                    " · that was git diff; lazygit shows staging, log and branches too"
                    if chosen.source == "fallback"
                    else ""
                )
                self.flash(f"back from {chosen.name}{hint}")

        if tool.source != "fallback" or self._install_shown:
            run(tool)
            return
        self._install_shown = True
        plan = gittool.install_plan(self.which)
        lines = [
            "f4 opens a terminal git program in the project directory and returns here "
            "when it ends",
            "lazygit is recommended: the diff of the working copy, staging, log and "
            "branches on one screen",
            f"install it with:  {plan.command}",
        ]
        if plan.note:
            lines.append(plan.note)
        lines.append("another program: CALU_GIT_TOOL in the environment or git_tool in tui.toml")
        if plan.runnable:
            lines.append("y runs the install command now in this terminal · n opens git diff")
        else:
            lines += [
                "calu does not run a command with sudo: run it in a terminal of your own",
                "until then f4 opens git diff through the pager, also when this notice closes",
            ]

        def answered(yes: bool | None) -> None:
            """Install after a yes, then open what is installed.

            Args:
                yes: The answer; always False for a command that is only shown.
            """
            if yes and plan.runnable:
                code = self._launch(shlex.split(plan.command), plan.manager)
                if code is None:
                    return
                if code != 0:
                    self.flash(f"{plan.command} ended with exit code {code}", error=True)
                    return
            again = gittool.choose(self.environ, self.options.git_tool, self.which)
            if isinstance(again, gittool.GitTool):
                run(again)

        self.push_screen(
            ConfirmScreen(
                "No terminal git program (lazygit, gitui, tig) is installed",
                lines,
                self.palette,
                notice=not plan.runnable,
            ),
            answered,
        )

    def _cmd_sessions(self, rest: str) -> None:
        """List the stored sessions of this project and resume the chosen one (`/sessions`).

        Args:
            rest: Ignored.
        """

        async def load() -> None:
            """Ask for the list and open the picker."""
            outcome = await self.actions.read("session.list", {"limit": 50})
            if not outcome.ok:
                self.flash(outcome.message, error=True)
                return
            listed = (outcome.data or {}).get("sessions") or []
            entries = []
            for entry in listed:
                claude_id = entry.get("claude_session_id")
                if not claude_id:
                    continue
                current = " (shown now)" if claude_id == self.model.claude_session_id else ""
                title = render.cut(
                    " ".join(str(entry.get("first_input") or "no input yet").split()), 70
                )
                when = str(entry.get("started_at") or "")[:16].replace("T", " ")
                entries.append(
                    (
                        f"{when}  {title}",
                        partial(self._resume, claude_id),
                        f"{entry.get('state')} · {claude_id}{current}",
                    )
                )
            if not entries:
                self.flash("no stored session of this project can be resumed")
                return
            self.search_commands(entries, placeholder="resume a stored conversation…")

        self._spawn(load())

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
        """Show thinking in full or as one line per block; the choice is stored.

        Args:
            rest: Ignored.
        """
        if not self.model.saw_thinking:
            self.flash("this session has sent no thinking (claude.thinking_display)")
        self.transcript.toggle_thinking()
        self._store("thinking", self.transcript.thinking_open)

    def _cmd_panel(self, rest: str) -> None:
        """Move into the side panel, or back (`f2`); on a narrow screen this opens and closes it.

        In the panel `j`/`k` move over its entries and Enter opens one; Escape returns.

        Args:
            rest: Ignored.
        """
        panel = self.query_one("#panel-lines", PanelList)
        if self.side_open:
            self.side_open = False
            self.mode = "chat"
        if self.size.width >= WIDE:
            if panel.has_focus:
                self.composer.focus()
            else:
                panel.focus()
        else:
            self.panel_open = not self.panel_open
            if self.panel_open:
                self.call_after_refresh(panel.focus)
            else:
                self.composer.focus()
        self._breakpoints()

    def _cursor_block(self) -> Block | None:
        """Return the block under the transcript's cursor while the transcript has the focus.

        Returns:
            The block, or None.
        """
        transcript = self.transcript
        if transcript.cursor is not None and transcript.has_focus:
            return self.model.blocks[transcript.cursor]
        return None

    def _last(self, wanted: Callable[[Block], bool]) -> Block | None:
        """Find the newest block of the shown tab that fits.

        Args:
            wanted: Tells whether a block fits.

        Returns:
            The block, or None.
        """
        blocks = self.model.blocks_of(self.model.selected_tab)
        return next((block for block in reversed(blocks) if wanted(block)), None)

    def _step_with_diffs(self) -> StepBlock | None:
        """Find the step whose diff a command means.

        Returns:
            The step under the transcript's cursor if it changed a file, else the newest step
            of the shown tab that did; None if there is none.
        """
        block = self._cursor_block()
        if isinstance(block, StepBlock) and block.diffs:
            return block
        found = self._last(lambda b: isinstance(b, StepBlock) and bool(b.diffs))
        return found if isinstance(found, StepBlock) else None

    def _cmd_copy(self, rest: str) -> None:
        """Copy to the clipboard (`/copy [answer | block | command | result | diff]`).

        Args:
            rest: What to copy. Empty: the block under the transcript's cursor, or Claude's
                last text. `answer`: Claude's last text. `command`, `result`: of the step
                under the cursor, or of the last step. `diff`: the unified diff of that step.
        """
        what = rest.strip().lower()
        cursor = self._cursor_block()
        step = cursor if isinstance(cursor, StepBlock) else None
        if step is None:
            last = self._last(lambda b: isinstance(b, StepBlock))
            step = last if isinstance(last, StepBlock) else None
        text = ""
        if what in {"", "block"} and cursor is not None:
            text = render.block_plain(cursor)
        elif what in {"", "answer", "block"}:
            answer = self._last(lambda b: isinstance(b, TextBlock))
            text = render.block_plain(answer) if answer is not None else ""
        elif what == "command" and step is not None:
            command = step.input.get("command") or step.input.get("file_path")
            text = str(command or render.block_plain(step))
        elif what == "result" and step is not None:
            text = step.result or ""
        elif what == "diff":
            changed = self._step_with_diffs()
            if changed is not None:
                text = "\n".join(
                    unified_text(diff.path, diff.before, diff.after) for diff in changed.diffs
                )
        elif what not in {"command", "result"}:
            self.flash("usage: /copy [answer | block | command | result | diff]", error=True)
            return
        if not text:
            self.flash("nothing to copy yet")
            return
        self.copy_to_clipboard(text)
        self.flash("copied (needs a terminal that accepts OSC 52; shift+drag selects natively)")

    def _cmd_diff(self, rest: str) -> None:
        """Open the full-screen diff of a step (`D` in the transcript, `/diff`).

        Args:
            rest: Ignored. The step is the one under the transcript's cursor, else the open
                decision's step if it is an edit, else the newest step that changed a file.
        """
        cursor = self._cursor_block()
        step = cursor if isinstance(cursor, StepBlock) and cursor.diffs else None
        diffs = step.diffs if step is not None else []
        title = render.step_title(step) if step is not None else ""
        decision = self.current_decision()
        if not diffs and decision is not None:
            diffs = proposed_diffs(decision, self.model, {})
            title = decision.tool_name
        if not diffs:
            step = self._step_with_diffs()
            if step is not None:
                diffs, title = step.diffs, render.step_title(step)
        if not diffs:
            self.flash("no step of this tab has changed a file yet")
            return
        self.push_screen(DiffScreen(diffs, title))

    def _cmd_search(self, rest: str) -> None:
        """Search the transcript as the text is typed (`ctrl+f`, `/` in the transcript).

        Args:
            rest: A text to start the search with.
        """
        self.mode = "search"
        self.composer.set_text(rest)
        self.composer.focus()
        self.transcript.search(rest)

    def _cmd_export(self, rest: str) -> None:
        """Write the shown tab's transcript to a Markdown file (`/export [file]`).

        Args:
            rest: The file; empty writes `calu-transcript-<date>-<time>.md` in the directory
                the client was started in. An existing file is not overwritten.
        """
        name = rest.strip() or f"calu-transcript-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path = Path(name).expanduser()
        text = transcript_markdown(self.model, self.model.selected_tab)
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(text)
        except FileExistsError:
            self.flash(f"{path} exists already: /export <other file>", error=True)
        except OSError as error:
            self.flash(f"not written: {error}", error=True)
        else:
            self.flash(f"transcript written to {path.resolve()}")

    def _cmd_agent_info(self, rest: str) -> None:
        """Show the task a subagent was given and what it returned (`i`).

        Args:
            rest: An agent key; empty means the Agent call under the transcript's cursor,
                else the agent whose tab is shown.
        """
        key = rest.strip()
        named = next(
            (agent.key for agent in self.model.agents.values() if key and agent.label == key),
            None,
        )
        key = named or key
        cursor = self._cursor_block()
        if not key and isinstance(cursor, StepBlock) and cursor.tool_use_id in self.model.agents:
            key = cursor.tool_use_id or ""
        key = key or self.model.selected_tab
        agent = self.model.agents.get(key)
        if agent is None or key == MAIN:
            self.flash("put the cursor on an Agent call, or show a subagent's tab first")
            return
        call = self.model.step(key)
        palette = self.palette
        width = min(100, self.size.width - 6)
        body = [
            Text.assemble(
                (f"{agent.label}", f"bold {palette.fg}"),
                (
                    f"  ·  {agent.subagent_type or 'subagent'}"
                    f"{' · background' if agent.background else ''}"
                    f"  ·  {self.model.activity_of(agent)}  ·  {agent.steps} steps",
                    palette.muted,
                ),
            ),
            Text(""),
            Text("The task it was given", f"bold {palette.accent}"),
        ]
        prompt = str(call.input.get("prompt") or "") if call is not None else ""
        body += render.markdown_lines(
            prompt or "_not seen by this client (the call was made before it attached)_",
            width,
            palette,
            self.current_theme.dark,
        )
        body += [Text(""), Text("What it returned", f"bold {palette.accent}")]
        result = (call.result if call is not None else None) or ""
        body += render.markdown_lines(
            result or "_nothing yet_", width, palette, self.current_theme.dark
        )
        self._page(f"Subagent {agent.label}", body)

    def _cmd_hold(self, rest: str) -> None:
        """Keep the composer's text in this client until the session is idle (`alt+h`).

        A held draft is not sent and not in the session's queue: it can be edited or deleted
        in the side panel (`f2`) and is sent as a message for the next turn when the session
        turns idle. It is lost when the client ends.

        Args:
            rest: A text to hold instead of the composer's.
        """
        text = rest.strip() or self.composer.value.strip()
        if not text:
            self.flash("nothing to hold: type the message first")
            return
        if not rest.strip():
            self.composer.set_text("")
        self.held.append(text)
        idle = "" if self.model.working() else " (the session is idle now: enter sends at once)"
        self.flash(
            f"held, not sent: it goes out when the session turns idle{idle} · f2 lists the "
            "drafts · lost on detach"
        )

    def _cmd_expand(self, rest: str) -> None:
        """Show pasted texts in full in the composer in place of their markers (`alt+e`).

        Args:
            rest: Ignored.
        """
        if not self.composer.expand_pastes():
            self.flash("the input holds no pasted text marker")

    def _cmd_bell(self, rest: str) -> None:
        """Switch the bell for a new waiting step on or off; the choice is stored (`/bell`).

        Args:
            rest: `on` or `off`; empty switches.
        """
        word = rest.strip().lower()
        if word not in {"", "on", "off"}:
            self.flash("usage: /bell [on | off]", error=True)
            return
        self.options.bell = (word == "on") if word else not self.options.bell
        self._store("bell", self.options.bell)
        self.flash(f"bell for a new waiting step: {'on' if self.options.bell else 'off'}")

    def _cmd_theme(self, rest: str) -> None:
        """Switch the colour theme, or list the themes (`/theme [name]`); the choice is stored.

        Args:
            rest: A theme name; empty opens the list.
        """
        if rest:
            if rest not in self.available_themes:
                self.flash(f"no theme {rest}", error=True)
            else:
                self._apply_theme(rest)
                self._store("theme", rest)
            return
        names = sorted(self.available_themes, key=lambda name: (not name.startswith("calu"), name))
        self.search_commands(
            [(name, partial(self.run_command, "theme", name)) for name in names],
            placeholder="theme…",
        )

    def _store(self, name: str, value: bool | int | str) -> None:
        """Write one setting to `tui.toml`, if this client has a settings file.

        Args:
            name: The setting.
            value: Its value.
        """
        if self.options.settings_path is None:
            return
        problem = settings.remember(self.options.settings_path, name, value)
        if problem:
            self.flash(problem, error=True)

    def _remember(self, text: str) -> None:
        """Add a sent text to the composer's history, and to the history file if there is one.

        Args:
            text: The text.
        """
        self.composer.remember(text)
        if self.options.history_path is not None:
            settings.append_history(self.options.history_path, text)

    def _path_completions(self) -> tuple[tuple[int, int, int], list[str]] | None:
        """Find the project paths that complete the `@` word at the cursor.

        Returns:
            None if the cursor is not at the end of a word that starts with `@`. Otherwise
            `((row, column of the @, cursor column), paths)`; the list is empty if nothing
            matches or the project directory is not on this machine.
        """
        composer = self.composer
        row, column = composer.cursor_location
        found = completion.at_word(composer.document.get_line(row), column)
        if found is None or self.mode != "chat":
            return None
        start, partial_path = found
        return (row, start, column), completion.complete_path(self.model.workspace, partial_path)

    def _send_held(self) -> None:
        """Send the oldest held draft when the session has just turned idle.

        One draft per idle moment: the message starts a turn, and the next draft waits for
        the end of that turn.
        """
        working = self.model.working()
        turned_idle = self._was_working and not working and self.model.state == "idle"
        self._was_working = working
        if turned_idle and self.held and self.link.connection is not None:
            text = self.held.pop(0)
            self._spawn(self._send(text, MAIN, "queued"))

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


def describe_commit(workspace: str, rev: str) -> str:
    """Name a commit of the project for a question: short id and subject line.

    Args:
        workspace: The project directory; it does not have to exist on this machine.
        rev: A commit id, a branch name or `HEAD`.

    Returns:
        `HEAD = 4f2a9c1 Rename the helper` for HEAD, `4f2a9c1 Rename the helper` otherwise;
        "" if the directory is not on this machine, git is missing or does not know `rev`.
    """
    if not workspace or not Path(workspace).is_dir():
        return ""
    try:
        done = subprocess.run(
            ["git", "-C", workspace, "log", "-1", "--format=%h %s", rev, "--"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    line = done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout else ""
    if not line:
        return ""
    return f"HEAD = {line}" if rev == "HEAD" else line


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
        "shift (option in iTerm); alt+c copies a block. @ at the start of a word completes a "
        "path of the project with tab. A large pasted text is shown as one marker and sent in "
        "full; alt+e shows it. alt+h holds the input as a draft until the session is idle."
    )
    lines += [Text(""), Text("Typing and selecting", f"bold {palette.accent}")]
    lines += [Text.assemble("  ", row) for row in render.wrap(Text(typing, palette.fg), width - 4)]
    rebind = (
        "Other keys are set in the table [keys] of tui.toml in calu's configuration directory, "
        'by the name of the binding, e.g. detach = "ctrl+q". The names: '
        + ", ".join(
            f"{binding.id} ({binding.key})"
            for binding in CaluApp.BINDINGS
            if isinstance(binding, Binding) and binding.id and not binding.id.startswith("tab-")
        )
        + ", tab-0 to tab-9 (alt+0 to alt+9). The same file holds theme, bell, toasts, "
        "thinking, panel_width and git_tool: the program f4 starts in the project directory "
        "(the environment variable CALU_GIT_TOOL overrides it). Left unset, f4 starts the "
        "first installed of lazygit (recommended), gitui and tig, else git diff."
    )
    lines += [Text(""), Text("Settings and other keys", f"bold {palette.accent}")]
    lines += [Text.assemble("  ", row) for row in render.wrap(Text(rebind, palette.fg), width - 4)]
    missing = [entry for entry in CAPABILITIES if not caps.has(entry.key)]
    if missing:
        lines += [Text(""), Text("Not offered by this session", f"bold {palette.accent}")]
        for entry in missing:
            text = Text(f"{entry.title}: {entry.reason}", palette.muted)
            lines += [Text.assemble("  ", row) for row in render.wrap(text, width - 4)]
    return lines
