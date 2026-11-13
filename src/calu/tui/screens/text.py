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

from calu.tui.render import fit_hints
from calu.tui.widgets.chrome import KeyLine


class TextScreen(Screen[None]):
    """Shows a title and a body that scrolls.

    A page may carry keys of its own (`keys`), shown in its last line, and may be filtered
    (`build`): `/` then asks for a text in the title line and the body shows what `build`
    returns for it.
    """

    def __init__(
        self,
        title: str,
        body: list[Text] | RenderableType,
        reload: Callable[[], None] | None = None,
        keys: dict[str, tuple[str, Callable[[], None]]] | None = None,
        build: Callable[[str], list[Text]] | None = None,
    ) -> None:
        """Create the page.

        Args:
            title: The title line.
            body: The lines, or any Rich renderable (Markdown for instance).
            reload: Called when the user presses `r`: it asks the session again and replaces
                the body with `set_body`. None for a page that has nothing to ask again.
            keys: Key -> `(what it does, the function to call)`: keys of this page beyond
                scrolling and leaving. The page stays open; the function may close it.
            build: Returns the body for a filter text ("" for all of it). With it, `/`
                starts typing a filter and Escape clears a filter before it leaves.
        """
        super().__init__()
        self.title_text = title
        self.body = body
        self.reload = reload
        self.keys = dict(keys or {})
        self.build = build
        self.filter = ""
        self.filtering = False
        """True while the keys typed go into the filter."""

    @property
    def text(self) -> str:
        """The plain text of the body, for tests.

        Returns:
            The lines joined with newlines; for another renderable its Markdown source or "".
        """
        if isinstance(self.body, list):
            return "\n".join(line.plain for line in self.body)
        return str(getattr(self.body, "markup", ""))

    def _title(self) -> Text:
        """Render the title line, with the filter while one is typed or set.

        Returns:
            The line.
        """
        line = Text(f" {self.title_text}", "bold")
        if self.filtering:
            line.append(f"   / {self.filter}▍", "bold reverse")
        elif self.filter:
            line.append(f"   filter: {self.filter} (esc clears)", "italic")
        return line

    def _keys_line(self) -> str:
        """Render the line of keys.

        Returns:
            The page's own keys, then leaving, scrolling, the filter and asking again; as
            many of them as fit the width, none cut.
        """
        if self.filtering:
            return " type to filter · enter keeps the filter · esc clears it"
        parts = [(9, f"{key} {what}") for key, (what, _) in self.keys.items()]
        parts += [(8, "esc or q back"), (3, "↑↓ pgup pgdn scroll")]
        if self.build is not None:
            parts.append((6, "/ filter"))
        if self.reload is not None:
            parts.append((5, "r asks the session again"))
        return " " + fit_hints(parts, max(20, self.size.width - 2))

    def compose(self) -> ComposeResult:
        """Build the page: title, scrollable body, one line of keys.

        Yields:
            The three parts.
        """
        yield Static(self._title(), id="text-title")
        with VerticalScroll(id="text-scroll"):
            body = Group(*self.body) if isinstance(self.body, list) else self.body
            yield Static(body, id="text-body")
        yield KeyLine(self._keys_line(), id="text-keys")

    def on_mount(self) -> None:
        """Focus the body, so that the scroll keys work."""
        self.query_one("#text-scroll").focus()

    def on_resize(self) -> None:
        """Fit the line of keys to the new width."""
        for widget in self.query("#text-keys").results(Static):
            widget.update(self._keys_line())

    def set_body(self, body: list[Text] | RenderableType) -> None:
        """Replace the body, e.g. when the answer of the session arrived.

        Before the page is built (it was pushed a moment ago) the body is only kept; the
        page then starts with it.

        Args:
            body: The lines, or any Rich renderable.
        """
        self.body = body
        shown = Group(*body) if isinstance(body, list) else body
        for widget in self.query("#text-body").results(Static):  # none before the page is built
            widget.update(shown)

    def say(self, message: str, error: bool = False) -> None:
        """Show a message in the line of keys until the next key.

        Args:
            message: The message; "" shows the keys again.
            error: Whether it reports a failure.
        """
        if not self.is_attached:
            return
        line = Text(" " + message, "bold red" if error else "bold") if message else None
        self.query_one("#text-keys", Static).update(line or self._keys_line())

    def _refilter(self) -> None:
        """Draw the title, the keys and the body for the current filter."""
        self.query_one("#text-title", Static).update(self._title())
        self.query_one("#text-keys", Static).update(self._keys_line())
        if self.build is not None:
            self.set_body(self.build(self.filter))
            self.query_one("#text-scroll").scroll_home(animate=False)

    def on_key(self, event: events.Key) -> None:
        """Handle the keys of the page.

        While a filter is typed, printable keys extend it, Backspace shortens it, Enter keeps
        it and Escape clears it. Otherwise Escape (after it cleared a filter) and `q` close
        the page, `r` asks the session again, `/` starts a filter, and the page's own keys
        call their functions.

        Args:
            event: The key.
        """
        self.query_one("#text-keys", Static).update(self._keys_line())
        if self.filtering:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                self.filtering = False
            elif event.key == "escape":
                self.filtering, self.filter = False, ""
            elif event.key == "backspace":
                self.filter = self.filter[:-1]
            elif event.is_printable and event.character:
                self.filter += event.character
            self._refilter()
            return
        if event.key == "escape" and self.filter:
            event.stop()
            self.filter = ""
            self._refilter()
        elif event.key in {"escape", "q"}:
            event.stop()
            self.dismiss()
        elif event.key == "r" and self.reload is not None:
            event.stop()
            self.reload()
        elif event.key == "slash" and self.build is not None:
            event.stop()
            self.filtering = True
            self._refilter()
        elif event.key in self.keys:
            event.stop()
            self.keys[event.key][1]()
