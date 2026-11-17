"""The small parts of the screen: lines and boxes that show a rendering of the model.

Each widget here is a `Static` that the app updates with lines from `calu.tui.render`. Those
that react to the mouse remember which column or line stands for what, and post a `Picked`
message with it; they hold no session data.
"""

import re
from typing import Any

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static


class Picked(Message):
    """The user clicked something that stands for an agent, a decision or an action.

    Attributes:
        kind: "agent", "decision", "follow", or of a button of the decision card: "card" (a
            letter of the card), "option" (an option of a question), "answers" (send the
            answers) or "decline"; "slash" (a line of the command list) or "checker" (a line
            of the checkers screen's list), with the line's index as the key.
        key: The agent key or the escalation id; for a button of the decision card
            `<escalation id>:q<question index>:<letter or option index>` (the value is ""
            for "answers" and "decline"); "" for "follow".
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


KEY_NAMES = {
    "esc": "escape",
    "enter": "enter",
    "tab": "tab",
    "space": "space",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "home": "home",
    "end": "end",
    "/": "slash",
    "?": "question_mark",
    "]": "right_square_bracket",
    "[": "left_square_bracket",
    "↑": "up",
    "↓": "down",
    "←": "left",
    "→": "right",
    "↑↓": "down",
}
"""How a line of key hints writes a key -> the name Textual knows it by."""

SEPARATOR = re.compile(r"\s·\s")
"""What separates two hints besides two or more spaces."""

HINT = re.compile(r"\S+(?: \S+)*")
"""One hint of a line whose ` · ` separators were replaced by spaces: words one space apart."""


def key_of(token: str) -> str | None:
    """Translate the way a hint writes a key into the key's name.

        "a,a" -> "a"      "esc" -> "escape"    "^p" -> "ctrl+p"    "alt+d" -> "alt+d"
        "f4" -> "f4"      "j/k" -> "j"         "pgup" -> "pageup"  "y" -> "y"

    Args:
        token: The first word of a hint.

    Returns:
        The key's name, or None if the word is no key (a title, a number range, a state).
    """
    token = token.strip().rstrip(",:")
    first, _, second = token.partition(",")
    if second and first == second:
        token = first
    token = token.split("/")[0] if len(token) > 1 and "+" not in token else token
    if token in KEY_NAMES:
        return KEY_NAMES[token]
    if token.startswith("^") and len(token) == 2:
        return f"ctrl+{token[1].lower()}"
    if re.fullmatch(r"(ctrl|alt|shift)\+[a-z0-9]+(\+[a-z0-9]+)?", token) or re.fullmatch(
        r"f\d{1,2}", token
    ):
        return token
    if len(token) == 1 and token.isascii() and token.isalnum():
        return token
    return None


def key_at(text: str, column: int) -> str | None:
    """Find the key of the hint under a column of a line of key hints.

    Args:
        text: The line as shown, e.g. " a,a allow   d,d deny · esc back".
        column: The clicked column, from 0.

    Returns:
        The name of the key the hint at that column starts with, or None if the column is
        between hints or the hint names no key.
    """
    for found in HINT.finditer(SEPARATOR.sub("   ", text)):
        if found.start() <= column < found.end():
            words = found.group().split()
            return key_of(words[0]) if words else None
    return None


class KeyLine(Static):
    """A line of key hints: a click on a hint presses the key it names.

    The line is drawn like any other text; what a click does is read from the hints last set
    with `update` or `show` (`key_at`), so a screen only has to show its hints here to make
    them work with the mouse. A message shown with `say` covers the hints until the next
    `update`; a click on it presses nothing, so a message such as "a page ..." is no key.
    """

    def __init__(self, content: Text | str = "", id: str | None = None) -> None:
        """Create the line.

        Args:
            content: The hints to start with.
            id: The widget id.
        """
        super().__init__(content, id=id)
        self.text = content.plain if isinstance(content, Text) else str(content)
        """The plain text of the line as last shown (hints or a message)."""
        self.hints = self.text
        """The plain text of the hints a click acts on; "" while a message is shown."""

    def update(self, content: Any = "", *, layout: bool = True) -> None:
        """Replace the hints.

        Args:
            content: The new line of hints.
            layout: Passed on to `Static.update`.
        """
        self.text = content.plain if isinstance(content, Text) else str(content)
        self.hints = self.text
        super().update(content, layout=layout)

    def say(self, message: Text) -> None:
        """Show a message in place of the hints until the next `update`.

        Side effects: a click on the line presses no key while the message is shown.

        Args:
            message: The message as it is drawn.
        """
        super().update(message)
        self.text, self.hints = message.plain, ""

    def show(self, line: Text) -> None:
        """Replace the hints (the name `Line` uses).

        Args:
            line: The new line.
        """
        self.update(line)

    def on_click(self, event: events.Click) -> None:
        """Press the key of the hint under the pointer.

        Args:
            event: The click.
        """
        key = key_at(self.hints, event.x)
        if key is not None:
            event.stop()
            self.app.simulate_key(key)


class Buttons(Static):
    """Rows of buttons for the mouse: each stands for what a key does.

    The buttons are laid out left to right and wrap onto as many lines as the widget's width
    needs (the stylesheet gives the widget `height: auto`), so that on a narrow terminal no
    button is cut off. A button wider than the whole width gets a line of its own and is cut
    by the terminal. A click posts `Picked(kind, key)` of the button under the pointer, found
    by the clicked line (`event.y`) and column (`event.x`).
    """

    GAP = 2
    """Columns between two buttons on the same line."""

    def __init__(self, id: str | None = None) -> None:
        """Create the empty row.

        Args:
            id: The widget id.
        """
        super().__init__("", id=id)
        self._buttons: list[tuple[str, str, str]] = []
        self._style = ""
        self._strong = ""
        self._laid_for = -1
        """The width the buttons were last laid out for; -1 before the first layout."""
        self._spans: list[tuple[int, int, int, str, str]] = []
        """`(line, first column, end column, kind, key)` per button."""
        self.text = ""
        """The plain text of the buttons as last shown, one line per row joined with newlines."""

    def show(self, buttons: list[tuple[str, str, str]], style: str, strong: str = "") -> None:
        """Replace the buttons and lay them out for the current width.

        Before the widget has a size (it was just mounted), the buttons are laid out on one
        line; `on_resize` lays them out again once the width is known.

        Args:
            buttons: `(label, kind, key)` per button, left to right.
            style: The Rich style of a button.
            strong: The key of the button drawn bold (the one a second click carries out);
                "" for none.
        """
        self._buttons, self._style, self._strong = list(buttons), style, strong
        self._lay_out(self.size.width)

    def _lay_out(self, width: int) -> None:
        """Place the buttons on lines no wider than `width` and draw them.

        Side effects: replaces `text`, the click spans and the shown content.

        Args:
            width: The widget's width in columns; 0 or less means "unknown": one line.
        """
        self._laid_for = width
        lines: list[Text] = [Text()]
        spans: list[tuple[int, int, int, str, str]] = []
        for label, kind, key in self._buttons:
            cell = f" {label} "
            line = lines[-1]
            gap = self.GAP if line.plain else 0
            if width > 0 and line.plain and line.cell_len + gap + cell_len(cell) > width:
                line, gap = Text(), 0
                lines.append(line)
            line.append(" " * gap)
            start = line.cell_len
            line.append(cell, f"{self._style} bold" if key == self._strong else self._style)
            spans.append((len(lines) - 1, start, line.cell_len, kind, key))
        self._spans = spans
        self.text = "\n".join(line.plain for line in lines) if self._buttons else ""
        self.update(Group(*lines) if self._buttons else "")

    def on_resize(self, event: events.Resize) -> None:
        """Lay the buttons out again when the width changed.

        Args:
            event: The resize, with the new size.
        """
        if event.size.width != self._laid_for:
            self._lay_out(event.size.width)

    def on_click(self, event: events.Click) -> None:
        """Post what the button under the pointer stands for.

        Args:
            event: The click.
        """
        for line, start, end, kind, key in self._spans:
            if line == event.y and start <= event.x < end:
                self.post_message(Picked(kind, key))
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
