"""The transcript of one agent tab: every block of the model as lines, drawn on demand.

The widget draws lines, not child widgets (Textual's line API): each block of the selected tab
is rendered once to a list of lines (`calu.tui.render.block_lines`), the widget keeps where
each block starts, and Textual asks for the visible lines only. Appending a block therefore
costs the same after two thousand steps as after ten, and nothing has to be folded away.

Scrolling:

* While the view is at the end (`following`), new content keeps it at the end.
* Once the user has scrolled up, new content is appended below and the visible lines do not
  move: the scroll offset stays, and if a block **above** the first visible line changes its
  height, the offset moves by that difference, so the same lines stay under the eye.
* `new_below` counts the blocks that arrived since; returning to the end clears it and
  resumes following.
"""

from bisect import bisect_right
from dataclasses import dataclass, field

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer

from calu.tui import render
from calu.tui.model import (
    ALL,
    Block,
    SessionModel,
    StepBlock,
    ThinkingBlock,
)
from calu.tui.theme import Palette


@dataclass
class _Entry:
    """One block as the transcript shows it: its lines and where they start."""

    block_id: int
    top: int = 0
    strips: list[Strip] = field(default_factory=list)


class Transcript(ScrollView, can_focus=True):
    """Shows the blocks of one tab of a `SessionModel` (see the module docstring)."""

    class Moved(Message):
        """The view scrolled, the cursor moved or the number of new blocks below changed."""

    class Resized(Message):
        """The widget has its new size after a resize of the terminal or of the layout.

        The app draws the parts whose text depends on the width when this arrives: only
        then are the sizes of the widgets beside the transcript the new ones as well.
        """

    class Opened(Message):
        """The user asked for the block under the cursor (enter on an Agent call or a step).

        Attributes:
            block_id: The id of the block.
        """

        def __init__(self, block_id: int) -> None:
            """Create the message.

            Args:
                block_id: The id of the block.
            """
            super().__init__()
            self.block_id = block_id

    def __init__(self, model: SessionModel, palette: Palette, id: str | None = None) -> None:
        """Create an empty transcript; `rebuild` fills it.

        Args:
            model: The model whose blocks are shown.
            palette: The colours of the texts.
            id: The widget id.
        """
        super().__init__(id=id)
        self.model = model
        self.palette = palette
        self.dark = True
        self.tab = model.selected_tab
        self.now: float | None = None
        self.following = True
        """True while the view is at the end and stays there as content arrives."""
        self.new_below = 0
        """Blocks that arrived below while the user was scrolled up."""
        self.cursor: int | None = None
        """Id of the block under the cursor; None while the transcript has no cursor."""
        self.expanded: set[int] = set()
        self.thinking_open = True
        self.toggled: set[int] = set()
        self._entries: list[_Entry] = []
        self._index: dict[int, int] = {}
        self._tops: list[int] = []
        self._width = 0
        self._moving = False
        self._cache: dict[int, tuple[tuple[object, ...], list[Strip]]] = {}
        """Block id -> what its lines were rendered for and the lines, so that a tab change
        or a resize back to a width seen before renders only what changed."""
        self._resize_timer: Timer | None = None
        self.resize_delay = 0.0
        """Seconds to wait after a resize before the blocks are rendered for the new width
        (the app sets it; 0 renders at once)."""
        self.needle = ""
        """The text the transcript is searched for; "" while no search is on."""
        self.matches: list[int] = []
        """The lines (counted from the start of the tab) that contain `needle`, top to bottom."""
        self.match = -1
        """Index into `matches` of the line the search stands on; -1 without a match."""

    # ------------------------------------------------------------------ rendering

    def _view(self) -> render.View:
        """Build the rendering parameters for the current state.

        Returns:
            The view: width, colours, clock, tab, cursor and what is expanded.
        """
        return render.View(
            palette=self.palette,
            width=max(20, self._width),
            now=self.now,
            tab=self.tab,
            cursor=self.cursor if self.has_focus else None,
            expanded=frozenset(self.expanded),
            thinking_open=self.thinking_open,
            toggled=frozenset(self.toggled),
            dark=self.dark,
        )

    def _strips(self, block: Block, previous: Block | None, view: render.View) -> list[Strip]:
        """Render one block to the lines the widget draws, or take them from the cache.

        The cache holds the lines of a block together with everything they depend on besides
        the block itself: width, whether the tab shows all agents, cursor, expansion, the
        thinking state and the blank line before it. A block that changes is rendered again
        by `apply`, which drops its entry first. A step that waits shows a running time and
        is never cached.

        Args:
            block: The block.
            previous: The block shown before it in this tab, or None.
            view: The rendering parameters.

        Returns:
            The lines, preceded by a blank one where `render.gap_before` says so.
        """
        gap = render.gap_before(previous, block)
        key = (
            view.width,
            view.tab == ALL,
            view.cursor == block.id,
            block.id in view.expanded,
            view.thinking_open != (block.id in view.toggled),
            gap,
        )
        waiting = isinstance(block, StepBlock) and block.waiting == "open"
        cached = self._cache.get(block.id)
        if cached is not None and cached[0] == key and not waiting:
            return cached[1]
        lines: list[Text] = [Text("")] if gap else []
        lines += render.block_lines(block, self.model, view)
        console = self.app.console
        strips = [Strip(list(line.render(console))) for line in lines]
        if not waiting:
            self._cache[block.id] = (key, strips)
        return strips

    def _layout(self, first: int = 0) -> None:
        """Recompute where the blocks start, from one entry on, and the scrollable size.

        Args:
            first: Index of the first entry whose position may have changed.
        """
        top = self._entries[first - 1].top + len(self._entries[first - 1].strips) if first else 0
        for entry in self._entries[first:]:
            entry.top = top
            top += len(entry.strips)
        self._tops = [entry.top for entry in self._entries]
        self.virtual_size = Size(0, top)
        if self.needle:
            self._find()

    def rebuild(self, fresh: bool = False) -> None:
        """Lay out every block of the tab again: after a tab change, a resize or a reset.

        Blocks whose lines are cached for the same width and view are not rendered again.
        Keeps the view at the end if it was following; otherwise keeps the first visible
        block where it was.

        Args:
            fresh: True forgets every cached line first: after the model was reset (block
                ids are then used again) and after a change of the colours.
        """
        self._width = self.scrollable_content_region.width or self.size.width
        anchor = self._anchor()
        blocks = self.model.blocks_of(self.tab)
        if fresh:
            self._cache.clear()
        elif self.model.blocks.base and len(self._cache) > len(self.model.blocks.kept):
            base = self.model.blocks.base
            self._cache = {key: value for key, value in self._cache.items() if key >= base}
        view = self._view()
        self._entries = []
        previous: Block | None = None
        for block in blocks:
            self._entries.append(_Entry(block.id, strips=self._strips(block, previous, view)))
            previous = block
        self._index = {entry.block_id: index for index, entry in enumerate(self._entries)}
        if self.cursor is not None and self.cursor not in self._index:
            self.cursor = None
        self._layout()
        self._restore(anchor)
        self.refresh()

    def show_tab(self, tab: str) -> None:
        """Show another tab, at its end.

        Args:
            tab: `MAIN`, `ALL` or an `agent_id`.
        """
        self.tab = tab
        self.cursor = None
        self.following = True
        self.new_below = 0
        self.rebuild()
        self.post_message(self.Moved())

    def apply(self, added: list[int], updated: set[int]) -> None:
        """Take over new and changed blocks of the model.

        Args:
            added: Ids of new blocks, in order; those of other tabs are ignored.
            updated: Ids of blocks whose content changed.
        """
        if not self._width:
            self.rebuild()
            return
        anchor = self._anchor()
        view = self._view()
        first_changed: int | None = None
        for block_id in sorted(updated - set(added)):
            index = self._index.get(block_id)
            self._cache.pop(block_id, None)
            block = self.model.block(block_id)
            if index is None or block is None:
                continue
            previous = self.model.block(self._entries[index - 1].block_id) if index else None
            self._entries[index].strips = self._strips(block, previous, view)
            first_changed = index if first_changed is None else min(first_changed, index)
        appended = 0
        for block_id in added:
            block = self.model.block(block_id)
            if block is None or block_id in self._index:
                continue
            if not self.model.block_in_tab(block, self.tab):
                continue
            previous = self.model.block(self._entries[-1].block_id) if self._entries else None
            self._index[block_id] = len(self._entries)
            self._entries.append(_Entry(block_id, strips=self._strips(block, previous, view)))
            if first_changed is None:
                first_changed = len(self._entries) - 1
            appended += 1
        if first_changed is None:
            return
        self._layout(first_changed)
        if not self.following:
            self.new_below += appended
        self._restore(anchor)
        self.refresh()
        self.post_message(self.Moved())

    def refresh_blocks(self, block_ids: set[int]) -> None:
        """Render some blocks again, e.g. the waiting steps when the clock ticks.

        Args:
            block_ids: Ids of the blocks; unknown ones are ignored.
        """
        self.apply([], set(block_ids))

    def _anchor(self) -> tuple[int, int] | None:
        """Remember which line is the first visible one, relative to its block.

        Returns:
            `(block id, line within the block)`, or None while the view follows the end or is
            empty.
        """
        if self.following or not self._entries:
            return None
        y = round(self.scroll_y)
        index = max(0, bisect_right(self._tops, y) - 1)
        entry = self._entries[index]
        return entry.block_id, y - entry.top

    def _restore(self, anchor: tuple[int, int] | None) -> None:
        """Put the view back: at the end if it follows, else on the remembered line.

        Args:
            anchor: What `_anchor` returned before the content changed.
        """
        self._moving = True
        try:
            if anchor is None:
                if self.following:
                    self.scroll_to(y=self.max_scroll_y, animate=False, immediate=True)
                return
            block_id, offset = anchor
            index = self._index.get(block_id)
            if index is None:
                return
            entry = self._entries[index]
            target = entry.top + min(offset, max(0, len(entry.strips) - 1))
            if target != round(self.scroll_y):
                self.scroll_to(y=target, animate=False, immediate=True)
        finally:
            self._moving = False

    def render_line(self, y: int) -> Strip:
        """Draw one visible line (Textual's line API).

        Args:
            y: The line within the widget, counted from 0.

        Returns:
            The line of the block at that position, or a blank one beyond the end.
        """
        width = self.scrollable_content_region.width
        line = round(self.scroll_y) + y
        index = bisect_right(self._tops, line) - 1
        if index < 0 or index >= len(self._entries):
            return Strip.blank(width, self.rich_style)
        entry = self._entries[index]
        offset = line - entry.top
        if offset >= len(entry.strips):
            return Strip.blank(width, self.rich_style)
        strip = (
            entry.strips[offset].crop_extend(0, width, self.rich_style).apply_style(self.rich_style)
        )
        if self.needle and self.matches:
            place = bisect_right(self.matches, line) - 1
            if place >= 0 and self.matches[place] == line:
                strip = self._marked(strip, place == self.match)
        return strip

    def _marked(self, strip: Strip, current: bool) -> Strip:
        """Draw every occurrence of the search text in a line on its own background.

        Args:
            strip: The line.
            current: Whether the search stands on this line (its matches are orange, those of
                other lines have the accent colour).

        Returns:
            The line with the occurrences marked.
        """
        text, wanted = strip.text, self.needle.lower()
        lowered = text.lower()
        cuts: list[int] = []
        position = lowered.find(wanted)
        while position >= 0:
            cuts += [cell_len(text[:position]), cell_len(text[: position + len(wanted)])]
            position = lowered.find(wanted, position + len(wanted))
        if not cuts:
            return strip
        colour = self.palette.orange if current else self.palette.accent
        mark = Style(bgcolor=colour, color=self.palette.bg, bold=True)
        parts = strip.divide([*cuts, strip.cell_length])
        marked = [
            Strip([Segment(seg.text, (seg.style or Style()) + mark) for seg in part])
            if index % 2
            else part
            for index, part in enumerate(parts)
        ]
        return Strip.join(marked)

    # ------------------------------------------------------------------ search

    def _find(self) -> None:
        """Recompute which lines contain the query and keep the search on its line."""
        wanted = self.needle.lower()
        before = self.matches[self.match] if 0 <= self.match < len(self.matches) else None
        self.matches = [
            entry.top + offset
            for entry in self._entries
            for offset, strip in enumerate(entry.strips)
            if wanted in strip.text.lower()
        ]
        if before in self.matches:
            self.match = self.matches.index(before)
        elif self.match >= len(self.matches):
            self.match = len(self.matches) - 1

    def search(self, query: str) -> int:
        """Search the lines of the tab for a text, ignoring case, and show the newest match.

        The lines that contain the text get their own background. The search stands on the
        last match at or above the bottom of the view and scrolls it into view; `search_step`
        moves it. An empty query ends the search and removes the marks.

        Args:
            query: The text to look for.

        Returns:
            The number of lines that contain it.
        """
        self.needle = query
        self.matches, self.match = [], -1
        if query:
            self._find()
            bottom = round(self.scroll_y) + self.scrollable_content_region.height - 1
            self.match = bisect_right(self.matches, bottom) - 1
            if self.match < 0 and self.matches:
                self.match = 0
            self._show_match()
        self.refresh()
        return len(self.matches)

    def search_step(self, delta: int) -> None:
        """Move the search to another match and scroll it into view.

        Args:
            delta: -1 for the match above (older), +1 for the one below; the ends wrap.
        """
        if not self.matches:
            return
        self.match = (self.match + delta) % len(self.matches)
        self._show_match()
        self.refresh()

    def _show_match(self) -> None:
        """Scroll so that the match the search stands on is in the upper half of the view."""
        if not 0 <= self.match < len(self.matches):
            return
        line = self.matches[self.match]
        height = self.scrollable_content_region.height
        top = round(self.scroll_y)
        if not top <= line < top + height:
            target = min(self.max_scroll_y, max(0, line - height // 3))
            self.scroll_to(y=target, animate=False, immediate=True)

    def visible_text(self) -> list[str]:
        """Return the text of the visible lines, for tests of what the user sees.

        Returns:
            One string per line of the widget, top to bottom, without trailing spaces.
        """
        height = self.scrollable_content_region.height
        return [self.render_line(y).text.rstrip() for y in range(height)]

    def all_text(self) -> list[str]:
        """Return the text of every line of the tab, visible or not.

        Returns:
            One string per line, top to bottom, without trailing spaces.
        """
        return [strip.text.rstrip() for entry in self._entries for strip in entry.strips]

    # ------------------------------------------------------------------ scrolling

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Note whether the view is at the end after a scroll.

        Args:
            old_value: The previous offset.
            new_value: The new offset.
        """
        super().watch_scroll_y(old_value, new_value)
        if self._moving:
            return
        at_end = round(new_value) >= self.max_scroll_y
        if at_end != self.following or (at_end and self.new_below):
            self.following = at_end
            if at_end:
                self.new_below = 0
            self.post_message(self.Moved())

    def follow(self) -> None:
        """Jump to the end and follow new content again."""
        self.following = True
        self.new_below = 0
        self.scroll_to(y=self.max_scroll_y, animate=False, immediate=True)
        self.post_message(self.Moved())

    def scroll_lines(self, lines: int) -> None:
        """Scroll by a number of lines; the view stops following unless it reaches the end.

        Args:
            lines: Positive scrolls towards the end, negative towards the start.
        """
        target = min(self.max_scroll_y, max(0, round(self.scroll_y) + lines))
        self.scroll_to(y=target, animate=False, immediate=True)

    def scroll_pages(self, pages: float) -> None:
        """Scroll by a share of the visible height.

        Args:
            pages: 1 is one page towards the end, -1 one towards the start.
        """
        height = max(2, self.scrollable_content_region.height - 2)
        self.scroll_lines(round(pages * height))

    def scroll_top(self) -> None:
        """Jump to the first line."""
        self.scroll_to(y=0, animate=False, immediate=True)

    def on_resize(self, event: events.Resize) -> None:
        """Render again when the width changed, once per burst of resizes.

        A window that is dragged sends many resizes. With `resize_delay` above 0 the blocks
        are rendered `resize_delay` seconds after the last of them; until then the lines of
        the old width are drawn, cut or padded to the new one.

        Args:
            event: The resize event.
        """
        self.post_message(self.Resized())
        if self.scrollable_content_region.width != self._width:
            if self._resize_timer is not None:
                self._resize_timer.stop()
                self._resize_timer = None
            if self.resize_delay > 0:
                self._resize_timer = self.set_timer(self.resize_delay, self._resized)
            else:
                self.rebuild()
        elif self.following:
            self.scroll_to(y=self.max_scroll_y, animate=False, immediate=True)

    def _resized(self) -> None:
        """Render for the width the widget has now, after the resizes have stopped."""
        self._resize_timer = None
        if self.scrollable_content_region.width != self._width:
            self.rebuild()

    def on_focus(self) -> None:
        """Give the transcript a cursor on the last visible block when it gets the focus."""
        if self.cursor is None and self._entries:
            bottom = round(self.scroll_y) + self.scrollable_content_region.height - 1
            index = min(len(self._entries) - 1, max(0, bisect_right(self._tops, bottom) - 1))
            self.cursor = self._entries[index].block_id
        if self.cursor is not None:
            self.refresh_blocks({self.cursor})

    def on_blur(self) -> None:
        """Hide the cursor when the focus leaves."""
        if self.cursor is not None:
            self.refresh_blocks({self.cursor})

    # ------------------------------------------------------------------ cursor and blocks

    def move_cursor(self, delta: int, steps_only: bool = False) -> None:
        """Move the cursor by blocks and keep it visible.

        Args:
            delta: +1 for the next block, -1 for the previous one.
            steps_only: Skip every block that is not a step.
        """
        if not self._entries:
            return
        index = self._index.get(self.cursor if self.cursor is not None else -1, len(self._entries))
        while True:
            index += delta
            if not 0 <= index < len(self._entries):
                return
            block = self.model.block(self._entries[index].block_id)
            if not steps_only or isinstance(block, StepBlock):
                break
        self.set_cursor(self._entries[index].block_id)

    def set_cursor(self, block_id: int) -> None:
        """Put the cursor on a block and scroll it into view.

        Args:
            block_id: The id of a block of this tab; another id does nothing.
        """
        index = self._index.get(block_id)
        if index is None:
            return
        before = self.cursor
        self.cursor = block_id
        self.refresh_blocks({block_id} | ({before} if before is not None else set()))
        entry = self._entries[index]
        height = self.scrollable_content_region.height
        y = round(self.scroll_y)
        if entry.top < y:
            self.scroll_to(y=entry.top, animate=False, immediate=True)
        elif entry.top + len(entry.strips) > y + height:
            target = min(entry.top, entry.top + len(entry.strips) - height)
            self.scroll_to(y=max(0, target), animate=False, immediate=True)
        self.post_message(self.Moved())

    def toggle(self, block_id: int | None = None) -> None:
        """Expand or collapse a block: the details of a step, the text of a thinking block.

        Args:
            block_id: The block; None means the block under the cursor.
        """
        target = self.cursor if block_id is None else block_id
        if target is None or target not in self._index:
            return
        block = self.model.block(target)
        if isinstance(block, StepBlock):
            self.expanded ^= {target}
        elif isinstance(block, ThinkingBlock):
            self.toggled ^= {target}
        else:
            return
        self.refresh_blocks({target})

    def toggle_thinking(self) -> None:
        """Show every thinking block in full, or each as one line."""
        self.thinking_open = not self.thinking_open
        self.toggled.clear()
        self.rebuild()

    def block_at(self, y: int) -> int | None:
        """Find the block drawn on a line of the widget.

        Args:
            y: The line within the widget, counted from 0.

        Returns:
            The block id, or None for a line beyond the end.
        """
        line = round(self.scroll_y) + y
        index = bisect_right(self._tops, line) - 1
        if 0 <= index < len(self._entries) and line - self._entries[index].top < len(
            self._entries[index].strips
        ):
            return self._entries[index].block_id
        return None

    def next_waiting(self) -> int | None:
        """Find the next step of this tab that waits for a decision, after the cursor.

        Returns:
            The block id, wrapping around to the first one; None if no step waits.
        """
        waiting = [
            entry.block_id
            for entry in self._entries
            if isinstance(block := self.model.block(entry.block_id), StepBlock)
            and block.waiting == "open"
        ]
        if not waiting:
            return None
        later = [block_id for block_id in waiting if self.cursor is None or block_id > self.cursor]
        return (later or waiting)[0]

    def on_click(self, event: events.Click) -> None:
        """Put the cursor on the clicked block and expand or collapse it.

        Args:
            event: The click.
        """
        block_id = self.block_at(event.y)
        if block_id is None:
            return
        self.cursor = block_id
        self.toggle(block_id)
        self.post_message(self.Moved())

    def on_key(self, event: events.Key) -> None:
        """Handle the keys of the focused transcript.

        `j`/`k` and the arrows move the cursor by blocks, `J`/`K` by steps, enter and space
        expand, `e` jumps to the next waiting step, `g`/`G` to the start and the end. Other
        keys are left to the app, which sends printable ones to the composer.

        Args:
            event: The key.
        """
        handled = True
        match event.key:
            case "j" | "down":
                self.move_cursor(1)
            case "k" | "up":
                self.move_cursor(-1)
            case "J":
                self.move_cursor(1, steps_only=True)
            case "K":
                self.move_cursor(-1, steps_only=True)
            case "enter" | "space":
                if self.cursor is not None:
                    block = self.model.block(self.cursor)
                    if isinstance(block, StepBlock) and block.tool in render.AGENT_TOOLS:
                        self.post_message(self.Opened(self.cursor))
                    self.toggle()
            case "e":
                target = self.next_waiting()
                if target is not None:
                    self.set_cursor(target)
            case "g" | "home":
                self.scroll_top()
            case "G" | "end":
                self.follow()
            case _:
                handled = False
        if handled:
            event.stop()
            event.prevent_default()
