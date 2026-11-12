"""The input of the full-screen client: a text area that sends on Enter.

Textual's `TextArea` has no "submitted" message and inserts a newline on Enter, so this
subclass takes the keys before the text area does:

    enter                         post `Composer.Submitted` with the text
    ctrl+j, alt+enter, shift+enter insert a newline
    up / down on the first / last line walk the history of sent texts

Before any of that it offers the key to `intercept`, a function the app sets: the app takes a
key there when it is a command rather than text (a decision key while the input is empty, for
instance), and the composer then does nothing with it.
"""

from collections.abc import Callable

from textual import events
from textual.message import Message
from textual.widgets import TextArea

NEWLINE_KEYS = frozenset({"ctrl+j", "alt+enter", "shift+enter"})
"""Keys that insert a newline. Enter itself sends; `shift+enter` only arrives as its own key
in terminals that report it, so `ctrl+j` and `alt+enter` are the keys that always work."""


class Composer(TextArea):
    """The message input (see the module docstring)."""

    class Submitted(Message):
        """The user pressed Enter.

        Attributes:
            text: The text of the input, not stripped; may be empty.
        """

        def __init__(self, text: str) -> None:
            """Create the message.

            Args:
                text: The text of the input.
            """
            super().__init__()
            self.text = text

    def __init__(self, id: str | None = None, max_lines: int = 8) -> None:
        """Create an empty composer.

        Args:
            id: The widget id.
            max_lines: How many lines the input grows to before it scrolls inside.
        """
        super().__init__(
            "",
            id=id,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            highlight_cursor_line=False,
            compact=True,
        )
        self.max_lines = max_lines
        self.intercept: Callable[[events.Key], bool] | None = None
        """Set by the app; returns True for a key the app has handled."""
        self.sent: list[str] = []
        self._position: int | None = None
        self._draft = ""

    def set_text(self, text: str) -> None:
        """Replace the text and put the cursor at its end.

        Args:
            text: The new text.
        """
        self.load_text(text)
        self.move_cursor(self.document.end)
        self.fit()

    def remember(self, text: str) -> None:
        """Add a sent text to the history, unless it repeats the newest entry.

        Args:
            text: The text.
        """
        if text.strip() and (not self.sent or self.sent[-1] != text):
            self.sent.append(text)
        self._position = None

    def fit(self) -> None:
        """Set the height to the number of lines of the text, between 1 and `max_lines`."""
        self.styles.height = min(self.max_lines, max(1, self.wrapped_document.height))

    def on_text_area_changed(self) -> None:
        """Grow or shrink with the text."""
        self.fit()

    async def _on_key(self, event: events.Key) -> None:
        """Take Enter, the newline keys and whatever the app intercepts before the text area.

        Args:
            event: The key.
        """
        if self.intercept is not None and self.intercept(event):
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    def _walk(self, delta: int) -> None:
        """Show an older or newer entry of the history.

        Args:
            delta: -1 for the older entry, +1 for the newer one. Beyond the newest entry the
                text that was being typed comes back.
        """
        if not self.sent:
            return
        if self._position is None:
            if delta > 0:
                return
            self._draft = self.text
            self._position = len(self.sent) - 1
        else:
            self._position += delta
        if self._position < 0:
            self._position = 0
        if self._position >= len(self.sent):
            self._position = None
            self.set_text(self._draft)
            return
        self.set_text(self.sent[self._position])

    def action_cursor_up(self, select: bool = False) -> None:
        """Move the cursor up, or walk back in the history from the first line.

        Args:
            select: Extend the selection instead of moving it.
        """
        if not select and self.cursor_location[0] == 0 and self.sent:
            self._walk(-1)
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        """Move the cursor down, or walk forward in the history from the last line.

        Args:
            select: Extend the selection instead of moving it.
        """
        last = self.document.line_count - 1
        if not select and self.cursor_location[0] >= last and self._position is not None:
            self._walk(1)
        else:
            super().action_cursor_down(select)
