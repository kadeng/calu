"""Map a line selection on one side of a diff to the corresponding lines on the other side.

Front ends send the selected line range, the side it was made on, and both document texts. This
module computes the excerpt pair. It uses a line diff (`difflib.SequenceMatcher`) of the two texts:

    before:  a b c   d e        selection on "after" lines 3..4 (c, X)
    after:   a b c X d e   ──►  before lines 3..3 (c); X was inserted

Rules for a selection on side S with the other side O:
- Unchanged lines map one to one.
- A changed block (replace) that overlaps the selection maps to its complete counterpart.
- Lines that exist only on S (insert/delete) map to nothing on O.
- Lines that exist only on O are included if they lie strictly inside the selection.
- If nothing maps, O gets an empty excerpt positioned where S's lines would be.
"""

import difflib
import re
from collections.abc import Sequence
from typing import Literal

from calu.comments.model import Excerpt

Side = Literal["before", "after"]

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(text: str) -> list[str]:
    r"""Split `text` into lines the way editors count them.

    Only "\n", "\r\n" and "\r" end a line (unlike `str.splitlines`, which also splits on form
    feeds and Unicode line separators that editors show inside a line). A trailing line
    terminator does not produce an extra empty line: "a\nb\n" -> ["a", "b"].

    Args:
        text: The document text.

    Returns:
        The lines without terminators; [] for "".
    """
    if not text:
        return []
    lines = _LINE_BREAK.split(text)
    if lines[-1] == "":
        lines.pop()
    return lines


def excerpt(lines: list[str], start_line: int, end_line: int) -> Excerpt:
    """Return the excerpt of `lines` for the 1-based inclusive range `start_line..end_line`.

    `end_line == start_line - 1` gives an empty excerpt positioned before line `start_line`.

    Args:
        lines: The document lines without terminators.
        start_line: First line, 1-based.
        end_line: Last line, 1-based and inclusive.

    Returns:
        The excerpt; its text is the lines joined with newlines.

    Raises:
        ValueError: Unless `1 <= start_line` and `start_line - 1 <= end_line <= len(lines)`.
    """
    if not (1 <= start_line and start_line - 1 <= end_line <= len(lines)):
        raise ValueError(f"range {start_line}..{end_line} outside 1..{len(lines)}")
    return Excerpt(
        start_line=start_line,
        end_line=end_line,
        text="\n".join(lines[start_line - 1 : end_line]),
    )


def map_selection(
    before_text: str,
    after_text: str,
    side: Side,
    start_line: int,
    end_line: int,
) -> tuple[Excerpt, Excerpt]:
    r"""Return `(before, after)` excerpts for a selection of lines on `side`.

    The excerpt of `side` is exactly the selection; the excerpt of the other side follows the
    rules in the module docstring.

    Example: before "a\nb\nc", after "a\nB\nc", selection after 2..2 -> before 2..2 "b",
    after 2..2 "B".

    Args:
        before_text: Document text before the change.
        after_text: Document text after the change.
        side: The side the selection was made on.
        start_line: First selected line, 1-based, in the document of `side`.
        end_line: Last selected line, 1-based and inclusive; `start_line - 1` for an empty
            selection.

    Returns:
        The excerpts `(before, after)`.

    Raises:
        ValueError: If the range lies outside the document of `side`.
    """
    before_lines, after_lines = split_lines(before_text), split_lines(after_text)
    own_lines = after_lines if side == "after" else before_lines
    selected = excerpt(own_lines, start_line, end_line)

    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    lo, hi = start_line - 1, end_line  # 0-based, half-open selection on `side`
    covered: list[tuple[int, int]] = []  # 0-based, half-open ranges on the other side
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        own1, own2, oth1, oth2 = (j1, j2, i1, i2) if side == "after" else (i1, i2, j1, j2)
        if own1 == own2:
            # Lines that exist only on the other side: include if strictly inside the selection.
            if lo < own1 < hi:
                covered.append((oth1, oth2))
            continue
        overlap_lo, overlap_hi = max(lo, own1), min(hi, own2)
        if overlap_lo >= overlap_hi:
            continue
        if tag == "equal":
            offset = oth1 - own1
            covered.append((overlap_lo + offset, overlap_hi + offset))
        elif tag == "replace":
            covered.append((oth1, oth2))
        # "insert"/"delete" with own lines only: nothing on the other side.

    other_lines = before_lines if side == "after" else after_lines
    if covered:
        other = excerpt(other_lines, min(c[0] for c in covered) + 1, max(c[1] for c in covered))
    else:
        position = _other_position(matcher.get_opcodes(), side, lo)
        other = excerpt(other_lines, position + 1, position)

    return (other, selected) if side == "after" else (selected, other)


def _other_position(
    opcodes: Sequence[tuple[str, int, int, int, int]], side: Side, own_index: int
) -> int:
    """Return the 0-based index on the other side that corresponds to `own_index` on `side`.

    Used to position an empty excerpt: inside an insert/delete block the result is the start of
    the other side's (empty) range of that block. An index at or after the end of `side` maps
    to the end of the other side.

    Args:
        opcodes: `SequenceMatcher.get_opcodes()` of before (a) and after (b).
        side: The side `own_index` refers to.
        own_index: 0-based line index on `side`.

    Returns:
        The 0-based line index on the other side.
    """
    for _tag, i1, i2, j1, j2 in opcodes:
        own1, own2, oth1, oth2 = (j1, j2, i1, i2) if side == "after" else (i1, i2, j1, j2)
        if own1 <= own_index < own2:
            return min(oth1 + (own_index - own1), oth2)
    if not opcodes:
        return 0
    _tag, _i1, i2, _j1, j2 = opcodes[-1]
    return i2 if side == "after" else j2
