"""Asks for the password of a credential of the local credential store.

Used before the client changes something in the database with the `admin` credential
(`calu.tui.adminaccess`): saving a checker's instructions, setting `honesty.mode`. The typed
password is shown as dots, is passed to the caller once and is not kept by the screen.
"""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from calu.tui.theme import Palette
from calu.tui.widgets.chrome import KeyLine


class PasswordScreen(ModalScreen[str | None]):
    """A box with one hidden input; dismissed with the password, or None after Escape."""

    def __init__(self, purpose: str, palette: Palette, problem: str = "") -> None:
        """Create the question.

        Args:
            purpose: What the password is needed for, one sentence.
            palette: The colours of the texts.
            problem: What went wrong with an earlier answer ("wrong password"), or "".
        """
        super().__init__()
        self.purpose = purpose
        self.palette = palette
        self.problem = problem

    def compose(self) -> ComposeResult:
        """Build the box: the purpose, the input, the keys.

        Yields:
            One container with the three parts.
        """
        palette = self.palette
        with Vertical(id="password-box"):
            yield Static(Text("Password of the 'admin' credential", f"bold {palette.orange}"))
            yield Static(Text(self.purpose, palette.fg))
            if self.problem:
                yield Static(Text(self.problem, palette.red))
            yield Input(password=True, id="password-input")
            yield KeyLine(Text("enter confirms · esc cancels", palette.faint))

    def on_mount(self) -> None:
        """Put the cursor into the input."""
        self.query_one(Input).focus()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Hand the password to the caller.

        Args:
            message: The submitted input.
        """
        message.stop()
        self.dismiss(message.value or None)

    def key_escape(self) -> None:
        """Close without a password."""
        self.dismiss(None)
