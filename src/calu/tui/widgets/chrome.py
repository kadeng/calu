"""The small parts of the screen: lines and boxes that show a rendering of the model.

Each widget here is a `Static` that the app updates with lines from `calu.tui.render`. Those
that react to the mouse remember which column or line stands for what, and post a `Picked`
message with it; they hold no session data.
"""

from rich.console import Group
from rich.text import Text
from textual import events
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

    def show(self, line: Text) -> None:
        """Replace the line.

        Args:
            line: The new text.
        """
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

    def show(self, line: Text, spans: list[tuple[int, int, str]]) -> None:
        """Replace the row.

        Args:
            line: The rendered row.
            spans: `(first column, end column, tab key)` per tab.
        """
        self._spans = spans
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

    def show(self, lines: list[Text], targets: dict[int, tuple[str, str]] | None = None) -> None:
        """Replace the lines.

        Args:
            lines: The rendered lines.
            targets: Line index -> `(kind, key)` of what a click on that line selects.
        """
        self._targets = dict(targets or {})
        self.update(Group(*lines) if lines else "")

    def on_click(self, event: events.Click) -> None:
        """Select what the clicked line stands for.

        Args:
            event: The click.
        """
        target = self._targets.get(event.y)
        if target is not None:
            self.post_message(Picked(*target))


class NewBelow(Static):
    """The marker that says how much arrived below while the user reads further up."""

    def on_click(self) -> None:
        """Jump back to the end of the transcript."""
        self.post_message(Picked("follow"))
