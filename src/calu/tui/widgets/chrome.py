"""The small parts of the screen: lines and boxes that show a rendering of the model.

Each widget here is a `Static` that the app updates with lines from `calu.tui.render`. Those
that react to the mouse remember which column or line stands for what, and post a `Picked`
message with it; they hold no session data.
"""

from rich.console import Group
from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static


class Picked(Message):
    """The user clicked something that stands for an agent, a decision or an action.

    Attributes:
        kind: "agent", "decision" or "follow".
        key: The agent key or the escalation id; "" for "follow".
    """

    def __init__(self, kind: str, key: str = "") -> None:
        """Create the message.

        Args:
            kind: "agent", "decision" or "follow".
            key: The agent key or the escalation id.
        """
        super().__init__()
        self.kind = kind
        self.key = key


class Line(Static):
    """One line of text: the status bar, the composer's header, the footer."""

    text = ""
    """The plain text of the line as last shown."""

    def show(self, line: Text) -> None:
        """Replace the line.

        Args:
            line: The new text.
        """
        self.text = line.plain
        self.update(line)


class AgentTabs(Static):
    """The row of agent tabs; a click on a tab selects it."""

    def __init__(self, id: str | None = None) -> None:
        """Create the empty row.

        Args:
            id: The widget id.
        """
        super().__init__("", id=id)
        self._spans: list[tuple[int, int, str]] = []
        self.text = ""
        """The plain text of the row as last shown."""

    def show(self, line: Text, spans: list[tuple[int, int, str]]) -> None:
        """Replace the row.

        Args:
            line: The rendered row.
            spans: `(first column, end column, tab key)` per tab.
        """
        self._spans = spans
        self.text = line.plain
        self.update(line)

    def on_click(self, event: events.Click) -> None:
        """Select the tab under the pointer.

        Args:
            event: The click.
        """
        for start, end, key in self._spans:
            if start <= event.x < end:
                self.post_message(Picked("agent", key))
                return


class Lines(Static):
    """Several lines, some of which stand for something a click selects."""

    def __init__(self, id: str | None = None) -> None:
        """Create the empty widget.

        Args:
            id: The widget id.
        """
        super().__init__("", id=id)
        self._targets: dict[int, tuple[str, str]] = {}
        self.text = ""
        """The plain text of the lines as last shown, joined with newlines."""

    def show(self, lines: list[Text], targets: dict[int, tuple[str, str]] | None = None) -> None:
        """Replace the lines.

        Args:
            lines: The rendered lines.
            targets: Line index -> `(kind, key)` of what a click on that line selects.
        """
        self._targets = dict(targets or {})
        self.text = "\n".join(line.plain.rstrip() for line in lines)
        self.update(Group(*lines) if lines else "")

    def on_click(self, event: events.Click) -> None:
        """Select what the clicked line stands for.

        Args:
            event: The click.
        """
        target = self._targets.get(event.y)
        if target is not None:
            self.post_message(Picked(*target))


class PanelList(Lines, can_focus=True):
    """The supervision panel: lines with a cursor that moves over what they stand for.

    While the panel has the focus, the lines of the entry under the cursor are drawn on their
    own background and with a `▸` in the first column. `j`/`k` and the arrows move the cursor
    from entry to entry (a decision, an agent, a queued message, a held draft, a mark). Enter
    and the letters of `KEYS` are posted as `PanelList.Act` with the entry; the app decides
    what they do for that kind of entry. Other keys are left to the app.
    """

    KEYS = frozenset({"enter", "c", "r", "x", "d", "s", "e", "i", "m"})
    """Keys that act on the entry under the cursor."""

    class Act(Message):
        """The user pressed a key on an entry of the panel.

        Attributes:
            kind: What the entry is: "decision", "agent", "queue", "held" or "mark".
            key: Its id (escalation id, agent key, message id, index of the draft, mark id).
            pressed: The key: "enter" or a letter of `PanelList.KEYS`.
        """

        def __init__(self, kind: str, key: str, pressed: str) -> None:
            """Create the message.

            Args:
                kind: What the entry is.
                key: Its id.
                pressed: The key.
            """
            super().__init__()
            self.kind = kind
            self.key = key
            self.pressed = pressed

    class Moved(Message):
        """The cursor of the panel went to another entry."""

    def __init__(self, id: str | None = None) -> None:
        """Create the empty panel.

        Args:
            id: The widget id.
        """
        super().__init__(id=id)
        self.cursor: tuple[str, str] | None = None
        """`(kind, key)` of the entry under the cursor; None while the panel has no entry."""
        self.highlight = ""
        """Rich style of the cursor's lines, set by the app from the theme."""
        self._lines: list[Text] = []
        self._placed = False
        """Whether the user has put the cursor somewhere; until then it is on the first entry."""

    def entries(self) -> list[tuple[str, str]]:
        """List what the lines stand for, top to bottom.

        Returns:
            Every `(kind, key)` once, in the order of its first line.
        """
        found: list[tuple[str, str]] = []
        for index in sorted(self._targets):
            if self._targets[index] not in found:
                found.append(self._targets[index])
        return found

    def show(self, lines: list[Text], targets: dict[int, tuple[str, str]] | None = None) -> None:
        """Replace the lines and keep the cursor on its entry, or on the nearest one left.

        Until the user has moved the cursor it stays on the first entry, which is the oldest
        open decision while one waits.

        Args:
            lines: The rendered lines.
            targets: Line index -> `(kind, key)` of what the line stands for.
        """
        self._targets = dict(targets or {})
        self._lines = list(lines)
        entries = self.entries()
        if not self._placed:
            self.cursor = entries[0] if entries else None
        elif self.cursor not in entries:
            same = [entry for entry in entries if self.cursor and entry[0] == self.cursor[0]]
            self.cursor = (same or entries or [None])[0]
        self._draw()

    def _draw(self) -> None:
        """Draw the lines, with the cursor's entry marked while the panel has the focus."""
        shown: list[Text] = []
        first = self.cursor_line()
        for index, line in enumerate(self._lines):
            if self.has_focus and self.cursor and self._targets.get(index) == self.cursor:
                marked = line.copy()
                if marked.plain[:1] == " " and index == first:
                    marked = Text.assemble(("▸", "bold"), marked[1:])
                marked.pad_right(max(0, self.size.width - marked.cell_len))
                marked.stylize(self.highlight)
                shown.append(marked)
            else:
                shown.append(line)
        self.text = "\n".join(line.plain.rstrip() for line in shown)
        self.update(Group(*shown) if shown else "")

    def cursor_line(self) -> int | None:
        """Return the first line of the entry under the cursor.

        Returns:
            The line index, or None without a cursor.
        """
        return next(
            (index for index in sorted(self._targets) if self._targets[index] == self.cursor),
            None,
        )

    def move(self, delta: int) -> None:
        """Move the cursor to another entry.

        Args:
            delta: +1 for the next entry, -1 for the previous one; the ends do not wrap.
        """
        entries = self.entries()
        if not entries:
            return
        index = entries.index(self.cursor) if self.cursor in entries else 0
        self.cursor = entries[min(len(entries) - 1, max(0, index + delta))]
        self._placed = True
        self._draw()
        self.post_message(self.Moved())
        line = self.cursor_line()
        scroll = self.parent
        if line is None or not isinstance(scroll, ScrollableContainer):
            return
        top, height = round(scroll.scroll_y), max(1, scroll.size.height)
        if line < top:
            scroll.scroll_to(y=line, animate=False)
        elif line + 1 >= top + height:
            scroll.scroll_to(y=line + 2 - height, animate=False)

    def on_focus(self) -> None:
        """Show the cursor."""
        self._draw()

    def on_blur(self) -> None:
        """Hide the cursor."""
        self._draw()

    def on_click(self, event: events.Click) -> None:
        """Put the cursor on the clicked entry and select it as Enter does.

        Args:
            event: The click.
        """
        target = self._targets.get(event.y)
        if target is not None:
            self.cursor = target
            self._placed = True
            self._draw()
            self.post_message(self.Act(*target, "enter"))

    def on_key(self, event: events.Key) -> None:
        """Move the cursor, or post the key for the entry under it.

        Args:
            event: The key.
        """
        if event.key in {"j", "down"}:
            self.move(1)
        elif event.key in {"k", "up"}:
            self.move(-1)
        elif event.key in self.KEYS and self.cursor is not None:
            self.post_message(self.Act(*self.cursor, event.key))
        else:
            return
        event.stop()
        event.prevent_default()


class NewBelow(Static):
    """The marker that says how much arrived below while the user reads further up."""

    def on_click(self) -> None:
        """Jump back to the end of the transcript."""
        self.post_message(Picked("follow"))
