"""A question the user must answer before the client carries out something that ends work.

This is the only modal screen of the client. It is used for actions that end a running turn or
the whole process (stopping and resuming a session with another model or effort, resuming
another conversation, shutting calu down), never for a decision about a step: those are
answered on the card, which takes no keys away.
"""

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from calu.tui.theme import Palette


class ConfirmScreen(ModalScreen[bool]):
    """Shows what an action does and waits for yes or no.

    With `typed` empty, `y` confirms. With a word in `typed`, the user has to type that word
    and press Enter; this is for an action that cannot be taken back. `n` or Escape declines
    in both cases. The screen is dismissed with True or False.
    """

    def __init__(
        self, title: str, consequences: list[str], palette: Palette, typed: str = ""
    ) -> None:
        """Create the question.

        Args:
            title: What is about to happen, as a question.
            consequences: What the action does, one sentence each.
            palette: The colours of the texts.
            typed: A word the user must type to confirm; "" asks for `y`.
        """
        super().__init__()
        self.title_text = title
        self.consequences = consequences
        self.palette = palette
        self.typed = typed
        self.entered = ""

    def compose(self) -> ComposeResult:
        """Build the box with the question.

        Yields:
            One container with the text.
        """
        with Vertical(id="confirm-box"):
            yield Static(self._body(), id="confirm-body")

    def _body(self) -> Group:
        """Render the question, the consequences and the keys.

        Returns:
            The lines of the box.
        """
        palette = self.palette
        lines = [Text(self.title_text, f"bold {palette.orange}"), Text("")]
        lines += [Text(f"· {sentence}", palette.fg) for sentence in self.consequences]
        lines.append(Text(""))
        if self.typed:
            lines.append(
                Text.assemble(
                    (f"type {self.typed} and press enter: ", palette.muted),
                    (self.entered, f"bold {palette.fg}"),
                    ("▍", palette.accent),
                )
            )
            lines.append(Text("esc cancels", palette.faint))
        else:
            lines.append(
                Text.assemble(
                    ("y", f"bold {palette.fg}"),
                    (" yes   ", palette.muted),
                    ("n", f"bold {palette.fg}"),
                    (" or ", palette.muted),
                    ("esc", f"bold {palette.fg}"),
                    (" no", palette.muted),
                )
            )
        return Group(*lines)

    def on_key(self, event: events.Key) -> None:
        """Take the answer.

        Args:
            event: The key.
        """
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(False)
            return
        if not self.typed:
            if event.key == "y":
                self.dismiss(True)
            elif event.key == "n":
                self.dismiss(False)
            return
        if event.key == "enter":
            if self.entered == self.typed:
                self.dismiss(True)
            return
        if event.key == "backspace":
            self.entered = self.entered[:-1]
        elif event.is_printable and event.character:
            self.entered += event.character
        self.query_one("#confirm-body", Static).update(self._body())
