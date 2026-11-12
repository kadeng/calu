"""A full-screen page of text: help, usage, the checkers, the context, a stored history.

These pages only read: nothing on them sends a message to Claude. Escape or `q` returns to the
session, which goes on in the meantime.
"""

from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static


class TextScreen(Screen[None]):
    """Shows a title and a body that scrolls."""

    def __init__(
        self,
        title: str,
        body: list[Text] | RenderableType,
        reload: Callable[[], None] | None = None,
    ) -> None:
        """Create the page.

        Args:
            title: The title line.
            body: The lines, or any Rich renderable (Markdown for instance).
            reload: Called when the user presses `r`: it asks the session again and replaces
                the body with `set_body`. None for a page that has nothing to ask again.
        """
        super().__init__()
        self.title_text = title
        self.body = body
        self.reload = reload

    @property
    def text(self) -> str:
        """The plain text of the body, for tests.

        Returns:
            The lines joined with newlines; for another renderable its Markdown source or "".
        """
        if isinstance(self.body, list):
            return "\n".join(line.plain for line in self.body)
        return str(getattr(self.body, "markup", ""))

    def compose(self) -> ComposeResult:
        """Build the page: title, scrollable body, one line of keys.

        Yields:
            The three parts.
        """
        yield Static(Text(f" {self.title_text}", "bold"), id="text-title")
        with VerticalScroll(id="text-scroll"):
            body = Group(*self.body) if isinstance(self.body, list) else self.body
            yield Static(body, id="text-body")
        again = " · r asks the session again" if self.reload is not None else ""
        yield Static(f" esc or q back · ↑↓ pgup pgdn scroll{again}", id="text-keys")

    def on_mount(self) -> None:
        """Focus the body, so that the scroll keys work."""
        self.query_one("#text-scroll").focus()

    def set_body(self, body: list[Text] | RenderableType) -> None:
        """Replace the body, e.g. when the answer of the session arrived.

        Args:
            body: The lines, or any Rich renderable.
        """
        self.body = body
        shown = Group(*body) if isinstance(body, list) else body
        self.query_one("#text-body", Static).update(shown)

    def on_key(self, event: events.Key) -> None:
        """Close the page on Escape or `q`; ask the session again on `r`.

        Args:
            event: The key.
        """
        if event.key in {"escape", "q"}:
            event.stop()
            self.dismiss()
        elif event.key == "r" and self.reload is not None:
            event.stop()
            self.reload()
