"""The rows of a diff view, computed from the content of a file before and after an edit.

`diff_rows` turns two texts into rows that both layouts of the diff screen draw: the unified
one (one line per row) and the split one (the old line left, the new line right). A row knows
its line numbers on both sides, so a selection of rows can be turned into the line range of a
comment (`selection_range`). This module imports nothing from Textual.

    before: a b c d        rows:  1 1   a          (context)
    after:  a B c d e             2 -  -b          (removed)   one changed pair:
                                  - 2  +B          (added)     split view shows b | B
                                  3 3   c
                                  4 4   d
                                  - 5  +e          (added)
"""

import difflib
from dataclasses import dataclass

CONTEXT_LINES = 3
"""Unchanged lines shown before and after each change."""


@dataclass(frozen=True)
class Row:
    """One row of a diff.

    `kind` is "context" (the line is on both sides), "removed" (only before), "added" (only
    after), "changed" (a removed line paired with an added one; only the split layout pairs
    them) or "gap" (unchanged lines left out; `text` says how many).
    """

    kind: str
    before_no: int | None = None
    """Line number in the old content, from 1; None for an added line or a gap."""
    after_no: int | None = None
    """Line number in the new content, from 1; None for a removed line or a gap."""
    before: str = ""
    after: str = ""
    hunk: int = 0
    """Which block of changes the row belongs to, from 0."""

    @property
    def text(self) -> str:
        """The text of the row in the unified layout.

        Returns:
            The new line for an added row, else the old one.
        """
        return self.after if self.kind == "added" else self.before


def diff_rows(
    before: str, after: str, pair: bool = False, context: int = CONTEXT_LINES
) -> list[Row]:
    """Compute the rows of a diff.

    Args:
        before: The old content.
        after: The new content.
        pair: True for the split layout: a replaced stretch gives "changed" rows with the old
            line and the new line side by side (and "removed" or "added" rows for what one
            side has more). False gives all removed lines of a stretch, then all added ones.
        context: Unchanged lines kept around each change; the rest becomes one "gap" row.

    Returns:
        The rows in reading order; an empty list if the two texts are equal.
    """
    old, new = before.splitlines(), after.splitlines()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    rows: list[Row] = []
    previous_end = 0
    hunk = 0

    def gap(count: int) -> Row:
        """Build the row that stands for unchanged lines left out.

        Args:
            count: How many lines.

        Returns:
            The row.
        """
        return Row("gap", before=f"{count} unchanged line{'s' if count != 1 else ''}", hunk=hunk)

    for hunk, group in enumerate(matcher.get_grouped_opcodes(context)):
        skipped = group[0][1] - previous_end
        if skipped > 0:
            rows.append(gap(skipped))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [
                    Row("context", i1 + k + 1, j1 + k + 1, old[i1 + k], new[j1 + k], hunk)
                    for k in range(i2 - i1)
                ]
                continue
            paired = min(i2 - i1, j2 - j1) if pair else 0
            rows += [
                Row("changed", i1 + k + 1, j1 + k + 1, old[i1 + k], new[j1 + k], hunk)
                for k in range(paired)
            ]
            rows += [
                Row("removed", i1 + k + 1, None, old[i1 + k], "", hunk)
                for k in range(paired, i2 - i1)
            ]
            rows += [
                Row("added", None, j1 + k + 1, "", new[j1 + k], hunk)
                for k in range(paired, j2 - j1)
            ]
        previous_end = group[-1][2]
    if rows and len(old) > previous_end:
        rows.append(gap(len(old) - previous_end))
    return rows


def hunk_starts(rows: list[Row]) -> list[int]:
    """Find where each block of changes begins.

    Args:
        rows: The rows of `diff_rows`.

    Returns:
        The index of the first changed row (not context, not a gap) of every hunk, in order.
    """
    starts: list[int] = []
    seen: set[int] = set()
    for index, row in enumerate(rows):
        if row.kind in {"context", "gap"} or row.hunk in seen:
            continue
        seen.add(row.hunk)
        starts.append(index)
    return starts


def selection_range(rows: list[Row], first: int, last: int) -> tuple[str, int, int] | None:
    """Turn a selection of rows into the side and the line range of a comment.

    Args:
        rows: The rows of `diff_rows`.
        first: Index of the first selected row.
        last: Index of the last selected row, inclusive.

    Returns:
        `(side, start_line, end_line)` with 1-based inclusive lines: on the side "after" when
        a selected row has a line there, else (only removed lines selected) on the side
        "before". None if the selection holds no line at all (only a gap row).
    """
    low, high = sorted((first, last))
    chosen = rows[max(0, low) : high + 1]
    after = [row.after_no for row in chosen if row.after_no is not None]
    if after:
        return "after", min(after), max(after)
    before = [row.before_no for row in chosen if row.before_no is not None]
    if before:
        return "before", min(before), max(before)
    return None


def unified_text(path: str, before: str, after: str) -> str:
    """Return the diff as text in the unified format, for the clipboard.

    Args:
        path: The path of the file.
        before: The old content.
        after: The new content.

    Returns:
        The unified diff with `a/<path>` and `b/<path>` headers; "" if nothing changed.
    """
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
