"""The full-screen diff of a step, with comments on lines.

    D on a step  ──►  DiffScreen ──(v, enter, a text)──► comment.add {kind: diff} ──► Claude

The screen shows the files a step changed, one at a time: side by side from `SPLIT_WIDTH`
columns on (the old content left, the new content right), unified below that; `s` switches.
A cursor moves by rows, `]` and `[` jump to the next and the previous block of changes, `v`
starts a selection, and Enter asks for a comment on the selected lines (or on the line under
the cursor). The comment goes to Claude with the selected lines of the old and of the new
content, as `calu.comments` builds it. Nothing else on this screen sends anything.
"""

from typing import Any

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from calu.tui import render
from calu.tui.difftools import Row, diff_rows, hunk_starts, selection_range, unified_text
from calu.tui.model import Diff

SPLIT_WIDTH = 120
"""From this width on the diff opens side by side."""


def row_line(row: Row, split: bool, width: int, palette: Any, cursor: bool, chosen: bool) -> Text:
    """Render one row of the diff.

    Args:
        row: The row.
        split: True for the side-by-side layout.
        width: The width of the line.
        palette: The colours (`calu.tui.theme.Palette`).
        cursor: Whether the cursor is on the row (a `>` in the first column).
        chosen: Whether the row is part of the selection (its own background).

    Returns:
        Unified: `> old new │ sign text`. Split: `> old text │ new text`, each half cut to
        half the width. A gap row is one muted line.
    """
    mark = Text(">" if cursor else " ", f"bold {palette.accent}")
    if row.kind == "gap":
        line = Text.assemble(mark, (f"      ┄ {row.before} ┄", palette.faint))
    elif split:
        half = max(10, (width - 4) // 2)
        left_style = palette.red if row.kind in {"removed", "changed"} else palette.muted
        right_style = palette.green if row.kind in {"added", "changed"} else palette.muted
        left_no = f"{row.before_no:>4} " if row.before_no is not None else "     "
        right_no = f"{row.after_no:>4} " if row.after_no is not None else "     "
        left = render.clip(row.before if row.before_no is not None else "", half - 5)
        right = render.clip(row.after if row.after_no is not None else "", half - 5)
        line = Text.assemble(
            mark,
            (left_no, palette.faint),
            (f"{left:<{half - 5}}", left_style),
            (" │ ", palette.faint),
            (right_no, palette.faint),
            (right, right_style),
        )
    else:
        sign, style = {
            "removed": ("-", palette.red),
            "added": ("+", palette.green),
        }.get(row.kind, (" ", palette.muted))
        old = f"{row.before_no:>4}" if row.before_no is not None else "    "
        new = f"{row.after_no:>4}" if row.after_no is not None else "    "
        line = Text.assemble(
            mark,
            (f"{old} {new} │ ", palette.faint),
            (sign + render.clip(row.text, width - 14), style),
        )
    if chosen or cursor:
        line.pad_right(max(0, width - line.cell_len))
        line.stylize(f"on {palette.user_bg if chosen else palette.surface}")
    return line


class DiffScreen(Screen[None]):
    """Shows the diffs of one step (see the module docstring)."""

    def __init__(self, diffs: list[Diff], title: str) -> None:
        """Create the screen.

        Args:
            diffs: The files the step changed, each with its content before and after; must
                not be empty.
            title: What the step is, for the title line.
        """
        super().__init__()
        self.diffs = diffs
        self.title_text = title
        self.file = 0
        self.split: bool | None = None
        """The layout; None until the first drawing, which chooses by the width."""
        self.rows: list[Row] = []
        self.cursor = 0
        self.mark_row: int | None = None
        """The row a selection started on; None without a selection."""
        self.commenting = False
        self.message = ""
        self.text = ""
        """The plain text of the rows as last drawn."""

    def compose(self) -> ComposeResult:
        """Build the screen: title, the rows, the comment input and one line of keys.

        Yields:
            The four parts.
        """
        yield Static("", id="diff-title")
        with VerticalScroll(id="diff-scroll"):
            yield Static("", id="diff-body")
        yield Input(placeholder="comment for Claude on the selected lines", id="diff-comment")
        yield Static("", id="diff-keys")

    def on_mount(self) -> None:
        """Choose the layout by the width, draw, and put the cursor on the first change."""
        self.split = self.size.width >= SPLIT_WIDTH
        self.query_one("#diff-scroll").focus()
        self._load()

    def on_resize(self) -> None:
        """Draw again for the new width."""
        self.redraw()

    @property
    def diff(self) -> Diff:
        """The file that is shown.

        Returns:
            The diff of the current file.
        """
        return self.diffs[self.file]

    def _load(self) -> None:
        """Compute the rows of the current file and put the cursor on its first change."""
        diff = self.diff
        self.rows = diff_rows(diff.before, diff.after, pair=bool(self.split))
        starts = hunk_starts(self.rows)
        self.cursor = starts[0] if starts else 0
        self.mark_row = None
        self.redraw()

    def selected(self) -> tuple[int, int]:
        """Return the selected rows.

        Returns:
            `(first, last)` row index, inclusive: the selection, or the cursor's row alone.
        """
        if self.mark_row is None:
            return self.cursor, self.cursor
        return min(self.mark_row, self.cursor), max(self.mark_row, self.cursor)

    def redraw(self) -> None:
        """Render title, rows and keys again and keep the cursor's row visible."""
        if not self.is_mounted:
            return
        app: Any = self.app
        palette = app.palette
        diff = self.diff
        added, removed = diff.counts()
        title = Text.assemble(
            (" diff ", palette.muted),
            (diff.path, f"bold {palette.fg}"),
            "  ",
            (f"+{added}", palette.green),
            " ",
            (f"-{removed}", palette.red),
            (
                f"   file {self.file + 1} of {len(self.diffs)}" if len(self.diffs) > 1 else "",
                palette.muted,
            ),
            (f"   {render.cut(self.title_text, 60)}", palette.faint),
        )
        self.query_one("#diff-title", Static).update(title)
        width = max(40, self.size.width - 2)
        first, last = self.selected()
        chosen = self.mark_row is not None
        lines = [
            row_line(
                row,
                bool(self.split),
                width,
                palette,
                index == self.cursor,
                chosen and first <= index <= last,
            )
            for index, row in enumerate(self.rows)
        ] or [Text(" the step did not change this file", palette.muted)]
        self.text = "\n".join(line.plain.rstrip() for line in lines)
        self.query_one("#diff-body", Static).update(Group(*lines))
        comment = self.query_one("#diff-comment", Input)
        comment.display = self.commenting
        if self.message:
            keys = Text(f" {self.message}", palette.orange)
        elif self.commenting:
            keys = Text(" enter sends the comment to Claude · esc cancels", palette.muted)
        else:
            wide = (
                " ↑↓ move · ] [ next/previous change · v select lines · enter comment · "
                "s split/unified · f next file · c copy · esc back"
            )
            narrow = " ↑↓ move · ] [ change · v select · enter comment · s split · c copy · esc"
            keys = Text(wide if self.size.width >= len(wide) else narrow, palette.muted)
        self.query_one("#diff-keys", Static).update(keys)
        self.call_after_refresh(self._keep_visible)

    def _keep_visible(self) -> None:
        """Scroll so that the cursor's row is on the screen."""
        scroll = self.query_one("#diff-scroll", VerticalScroll)
        height = max(1, scroll.size.height)
        top = round(scroll.scroll_y)
        if self.cursor < top:
            scroll.scroll_to(y=self.cursor, animate=False)
        elif self.cursor >= top + height:
            scroll.scroll_to(y=self.cursor - height + 1, animate=False)

    def move(self, delta: int) -> None:
        """Move the cursor by rows.

        Args:
            delta: Rows towards the end (positive) or the start (negative).
        """
        if self.rows:
            self.cursor = min(len(self.rows) - 1, max(0, self.cursor + delta))
        self.message = ""
        self.redraw()

    def jump_hunk(self, step: int) -> None:
        """Put the cursor on the next or the previous block of changes.

        Args:
            step: +1 for the next block, -1 for the previous one; the ends do not wrap.
        """
        starts = hunk_starts(self.rows)
        later = [index for index in starts if index > self.cursor]
        earlier = [index for index in starts if index < self.cursor]
        if step > 0 and later:
            self.cursor = later[0]
        elif step < 0 and earlier:
            self.cursor = earlier[-1]
        else:
            self.message = "no further change in this file" + (
                " · f shows the next file" if len(self.diffs) > 1 else ""
            )
            self.redraw()
            return
        self.message = ""
        self.redraw()

    def _start_comment(self) -> None:
        """Open the comment input for the selected lines, if they name a line."""
        first, last = self.selected()
        if not self.rows or selection_range(self.rows, first, last) is None:
            self.message = "no line is selected here"
            self.redraw()
            return
        self.commenting = True
        self.message = ""
        self.redraw()
        self.query_one("#diff-comment", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send the comment on the selected lines to Claude.

        Args:
            event: The input's message with the comment text.
        """
        event.stop()
        text = event.value.strip()
        first, last = self.selected()
        chosen = selection_range(self.rows, first, last)
        if not text or chosen is None:
            return
        side, start, end = chosen
        app: Any = self.app
        outcome = await app.actions.comment_diff(self.diff, side, start, end, text)
        self.commenting = False
        event.input.value = ""
        self.query_one("#diff-scroll").focus()
        if outcome.ok:
            self.mark_row = None
            self.message = outcome.message
        else:
            self.message = f"not sent: {outcome.message}"
        self.redraw()

    def on_key(self, event: events.Key) -> None:
        """Handle the keys of the diff screen (listed in its last line).

        Args:
            event: The key.
        """
        key = event.key
        if self.commenting:
            if key == "escape":
                event.stop()
                self.commenting = False
                self.query_one("#diff-scroll").focus()
                self.redraw()
            return
        page = max(1, self.query_one("#diff-scroll").size.height - 2)
        handled = True
        match key:
            case "escape" | "q" | "D":
                if key == "escape" and self.mark_row is not None:
                    self.mark_row = None
                    self.redraw()
                else:
                    self.dismiss()
            case "j" | "down":
                self.move(1)
            case "k" | "up":
                self.move(-1)
            case "pagedown" | "space":
                self.move(page)
            case "pageup":
                self.move(-page)
            case "home" | "g":
                self.move(-len(self.rows))
            case "end" | "G":
                self.move(len(self.rows))
            case "right_square_bracket":
                self.jump_hunk(1)
            case "left_square_bracket":
                self.jump_hunk(-1)
            case "v":
                self.mark_row = None if self.mark_row is not None else self.cursor
                self.redraw()
            case "enter":
                self._start_comment()
            case "s":
                self.split = not self.split
                self._load()
            case "f" | "F":
                step = 1 if key == "f" else -1
                self.file = (self.file + step) % len(self.diffs)
                self._load()
            case "c":
                diff = self.diff
                self.app.copy_to_clipboard(unified_text(diff.path, diff.before, diff.after))
                self.message = "diff copied (needs a terminal that accepts OSC 52)"
                self.redraw()
            case _:
                handled = False
        if handled:
            event.stop()
            event.prevent_default()
