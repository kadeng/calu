"""The checkers of the session: their instructions, to read and to change.

    +-----------------------------------------------------------------------------+
    | Checkers                                                                    |
    +---------------------+-------------------------------------------------------+
    | > command      v12  | command · route command · model … · version 12        |
    |   edit         v7   |                                                       |
    |   narrator     v3   | (the instructions as Markdown, or the editor)         |
    +---------------------+-------------------------------------------------------+
    | message                                                                     |
    | ↑↓ checker · pgup pgdn scroll · e edit · r read again · esc back            |
    +-----------------------------------------------------------------------------+

Reading goes through the session (`checkers.get`): the list shows what the session judges
with. Writing does not: a session cannot change what it is judged by. `e` opens the
instructions in an editor, and saving stores them with the `admin` credential of this machine
(`calu.tui.adminaccess.AdminAccess`), as `calu checker edit` does; the screen asks for the
credential's password if it has one. Without the credential the screen only reads and says
which command changes the instructions. A running session uses saved instructions from its
next step on.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static, TextArea

from calu.tui.adminaccess import AdminAccess, AdminFailed, PasswordNeeded
from calu.tui.render import markdown_lines
from calu.tui.screens.password import PasswordScreen
from calu.tui.theme import Palette
from calu.tui.widgets.chrome import KeyLine, Lines, Picked

NARROW = 100
"""Below this width the list of checkers is narrower."""

Load = Callable[[], Awaitable[tuple[list[dict[str, Any]], str]]]
"""Asks the session for its checkers: `(the checkers of checkers.get, "")`, or `([], why
not)`."""


class CheckersScreen(Screen[None]):
    """Lists the checkers and shows or edits the instructions of the selected one."""

    def __init__(self, load: Load, admin: AdminAccess, palette: Palette) -> None:
        """Create the screen; it asks the session when it is mounted.

        Args:
            load: Asks the session for its checkers.
            admin: Stores changed instructions; its `problem()` makes the screen read-only.
            palette: The colours of the texts.
        """
        super().__init__()
        self.load = load
        self.admin = admin
        self.palette = palette
        self.checkers: list[dict[str, Any]] = []
        self.index = 0
        self.editing = False
        self.message = "asking the session…"
        self.message_error = False
        self._leave_asked = False
        self._base_version: int | None = None
        """The version of the checker whose instructions the editor was opened on."""

    # ------------------------------------------------------------------ what tests read

    @property
    def current(self) -> dict[str, Any] | None:
        """The selected checker.

        Returns:
            Its definition as the session sent it, or None while there is none.
        """
        return self.checkers[self.index] if 0 <= self.index < len(self.checkers) else None

    @property
    def text(self) -> str:
        """The plain text of the screen's own lines, for tests.

        Returns:
            The list, the head of the selected checker, its instructions and the message.
        """
        current = self.current or {}
        return "\n".join(
            [
                *[line.plain for line in self._list_lines()],
                self._head(current).plain if current else "",
                str(current.get("instructions") or ""),
                self.message,
            ]
        )

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        """Build the screen: title, list beside instructions or editor, message, keys.

        Yields:
            The parts, top to bottom.
        """
        yield Static(Text(" Checkers and their instructions", "bold"), id="checkers-title")
        with Horizontal(id="checkers-body"):
            yield Lines(id="checkers-list")
            with VerticalScroll(id="checkers-scroll"):
                yield Static("", id="checkers-text")
            yield TextArea(
                "", id="checkers-editor", soft_wrap=True, show_line_numbers=True, language=None
            )
        yield Static("", id="checkers-message")
        yield KeyLine("", id="checkers-keys")

    def on_mount(self) -> None:
        """Ask the session for the checkers and draw what is known so far."""
        self.query_one("#checkers-scroll").focus()
        self.set_class(self.size.width < NARROW, "narrow")
        self._draw()
        self.run_worker(self.reload(), exclusive=True, group="checkers")

    def on_resize(self) -> None:
        """Fit the list to the new width."""
        self.set_class(self.size.width < NARROW, "narrow")
        self._draw()

    async def reload(self, keep_message: bool = False) -> None:
        """Ask the session for the checkers again and keep the selection by name.

        Args:
            keep_message: True keeps the message line (after a save, which has just said
                what happened).
        """
        name = (self.current or {}).get("name")
        checkers, problem = await self.load()
        if not self.is_attached:
            return
        if problem:
            self.message, self.message_error = problem, True
        else:
            self.checkers = checkers
            names = [checker.get("name") for checker in checkers]
            self.index = names.index(name) if name in names else 0
            if not keep_message:
                self.message, self.message_error = self._standing(), False
        self._draw()

    def _standing(self) -> str:
        """Say whether this machine can change instructions.

        Returns:
            The sentence of the message line while nothing else is to be said.
        """
        problem = self.admin.problem()
        if problem is None:
            return "e edits the instructions · a running session uses a change at its next step"
        name = (self.current or {}).get("name") or "<name>"
        return f"read-only here: {problem} · edit with: calu checker edit {name}"

    def on_picked(self, message: Picked) -> None:
        """Select the checker whose line was clicked; not while its instructions are edited.

        Args:
            message: The click on a line of the list.
        """
        message.stop()
        if message.kind == "checker" and not self.editing and message.key.isdigit():
            self.index = max(0, min(len(self.checkers) - 1, int(message.key)))
            self._draw()

    def _list_lines(self) -> list[Text]:
        """Render the list of checkers.

        Returns:
            One line per checker: a cursor, the name, the version, and "off" if disabled.
        """
        palette = self.palette
        width = (20 if self.size.width < NARROW else 30) - 2
        lines: list[Text] = []
        for index, checker in enumerate(self.checkers):
            chosen = index == self.index
            tail = f"v{checker.get('version', '?')}"
            if not checker.get("enabled", True):
                tail += " off"
            name = str(checker.get("name") or "")
            room = max(4, width - 2 - len(tail) - 1)
            if len(name) > room:
                name = name[: room - 1] + "…"
            line = Text.assemble(
                ("> " if chosen else "  ", palette.accent),
                (f"{name:<{room}} ", f"bold {palette.fg}" if chosen else palette.fg),
                (tail, palette.muted),
            )
            lines.append(line)
        return lines or [Text("no checkers", palette.muted)]

    def _head(self, checker: dict[str, Any]) -> Text:
        """Render the line above the instructions.

        Args:
            checker: The selected checker.

        Returns:
            Name, route, model, version and whether it is disabled.
        """
        palette = self.palette
        head = Text.assemble(
            (str(checker.get("name") or ""), f"bold {palette.fg}"),
            (
                f" · route {checker.get('route')} · model {checker.get('model') or 'default'}"
                f" · version {checker.get('version')}",
                palette.muted,
            ),
        )
        if not checker.get("enabled", True):
            head.append(" · disabled", palette.orange)
        return head

    def _draw(self) -> None:
        """Draw the list, the selected checker, the message and the keys."""
        palette = self.palette
        self.set_class(self.editing, "editing")
        self.query_one("#checkers-list", Lines).show(
            self._list_lines(),
            {index: ("checker", str(index)) for index in range(len(self.checkers))},
        )
        checker = self.current
        body: RenderableType = ""
        if checker is not None:
            source = str(checker.get("instructions") or "").strip()
            list_width = 20 if self.size.width < NARROW else 30
            width = max(20, self.size.width - list_width - 6)
            body = Group(
                self._head(checker),
                Text(""),
                *(
                    markdown_lines(source, width, palette, dark=self.app.current_theme.dark)
                    if source
                    else [Text("no instructions", palette.muted)]
                ),
            )
        self.query_one("#checkers-text", Static).update(body)
        self.query_one("#checkers-message", Static).update(
            Text(" " + self.message, palette.red if self.message_error else palette.muted)
        )
        if self.editing:
            keys = " ctrl+s or alt+s saves · esc leaves the editor (asks if the text changed)"
        elif self.admin.problem() is None:
            keys = " ↑↓ checker · pgup pgdn scroll · e edit · r read again · esc or q back"
        else:
            keys = " ↑↓ checker · pgup pgdn scroll · r read again · esc or q back"
        self.query_one("#checkers-keys", Static).update(keys)

    # ------------------------------------------------------------------ keys

    def on_key(self, event: events.Key) -> None:
        """Move in the list, open and leave the editor, save, go back.

        Args:
            event: The key.
        """
        key = event.key
        if self.editing:
            if key in {"ctrl+s", "alt+s"}:
                event.stop()
                event.prevent_default()
                self.save()
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self._leave_editor()
            else:
                self._leave_asked = False
            return
        handled = True
        if key in {"escape", "q"}:
            self.dismiss()
        elif key in {"up", "k"}:
            self._select(-1)
        elif key in {"down", "j"}:
            self._select(1)
        elif key == "e":
            self.edit()
        elif key == "r":
            self.message, self.message_error = "asking the session…", False
            self._draw()
            self.run_worker(self.reload(), exclusive=True, group="checkers")
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()

    def _select(self, step: int) -> None:
        """Select another checker and show its instructions from the top.

        Args:
            step: -1 for the one above, 1 for the one below.
        """
        if not self.checkers:
            return
        self.index = max(0, min(len(self.checkers) - 1, self.index + step))
        self.message, self.message_error = self._standing(), False
        self.query_one("#checkers-scroll").scroll_home(animate=False)
        self._draw()

    def edit(self) -> None:
        """Open the selected checker's instructions in the editor.

        Without the `admin` credential nothing opens and the message line says how the
        instructions are changed instead. The version the text was read at is kept, so that
        the save can tell whether somebody changed the checker meanwhile.
        """
        checker = self.current
        if checker is None:
            return
        problem = self.admin.problem()
        if problem is not None:
            self.message = (
                f"not editable here: {problem} · edit with: calu checker edit "
                f"{checker.get('name')}, or on the settings page of calu web"
            )
            self.message_error = True
            self._draw()
            return
        editor = self.query_one("#checkers-editor", TextArea)
        editor.load_text(str(checker.get("instructions") or ""))
        version = checker.get("version")
        self._base_version = version if isinstance(version, int) else None
        self.editing = True
        self._leave_asked = False
        self.message = f"editing the instructions of {checker.get('name')}"
        self.message_error = False
        self._draw()
        editor.focus()

    def _leave_editor(self) -> None:
        """Close the editor; a changed text is dropped only on the second Escape."""
        checker = self.current or {}
        editor = self.query_one("#checkers-editor", TextArea)
        changed = editor.text != str(checker.get("instructions") or "")
        if changed and not self._leave_asked:
            self._leave_asked = True
            self.message = "the text changed: esc again drops the change, ctrl+s saves it"
            self.message_error = True
            self._draw()
            return
        self.editing = False
        self.message, self.message_error = self._standing(), False
        self._draw()
        self.query_one("#checkers-scroll").focus()

    def save(self) -> None:
        """Store the editor's text, asking for the credential's password first if it has one."""
        checker = self.current
        if checker is None or not self.editing:
            return
        name = str(checker.get("name") or "")
        text = self.query_one("#checkers-editor", TextArea).text
        if text == str(checker.get("instructions") or ""):
            self.message, self.message_error = "unchanged; nothing was stored", False
            self._draw()
            return
        if not text.strip():
            self.message, self.message_error = "the instructions must not be empty", True
            self._draw()
            return
        self._store(name, text, problem="")

    def _store(self, name: str, text: str, problem: str) -> None:
        """Ask for the password if one is needed, then store the text.

        Args:
            name: The checker's name.
            text: The new instructions.
            problem: What was wrong with the password given before, or "".
        """

        async def store(password: str | None) -> None:
            """Store the text and show the new version, or why it was not stored.

            After the save the session is asked again. If it does not report the saved
            text for this checker, the message says that the change went to the database of
            this machine's credential and that the session does not show it.

            Args:
                password: The credential's password, or None if it has none.
            """
            try:
                saved = await self.admin.save_instructions(name, text, password, self._base_version)
            except PasswordNeeded as error:
                self._store(name, text, problem=str(error))
                return
            except AdminFailed as error:
                self.message, self.message_error = f"not saved: {error}", True
                self._draw()
                return
            self.editing = False
            self.message = (
                f"saved: {saved.name} is now version {saved.version} · a running session "
                "uses it at its next step"
            )
            self.message_error = False
            self._draw()
            self.query_one("#checkers-scroll").focus()
            await self.reload(keep_message=True)
            if not self.is_attached:
                return
            seen = next((c for c in self.checkers if c.get("name") == name), None)
            if seen is None or str(seen.get("instructions") or "") != text:
                reads = f"version {seen.get('version')}" if seen is not None else "no such checker"
                self.message = (
                    f"stored as version {saved.version} in the database of this machine's "
                    f"admin credential, but the attached session reads {reads} with other "
                    "instructions: it uses another database, or has not read the change"
                )
                self.message_error = True
                self._draw()

        def answered(password: str | None) -> None:
            """Go on with the password, or keep the editor open after Escape.

            Args:
                password: What the user typed, or None.
            """
            if password is None:
                self.message, self.message_error = "not saved: no password given", True
                self._draw()
                return
            self.run_worker(store(password), group="checkers-save")

        if self.admin.needs_password():
            self.app.push_screen(
                PasswordScreen(
                    f"It stores the new instructions of the checker {name}.",
                    self.palette,
                    problem,
                ),
                answered,
            )
        else:
            self.run_worker(store(None), group="checkers-save")
