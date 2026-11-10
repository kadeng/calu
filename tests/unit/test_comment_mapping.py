"""Mapping a line selection on one side of a diff to the other side (`calu.comments.mapping`).

A diff comment carries the selected text **and** its counterpart on the other side, which is
what Claude is shown next to the comment. A wrong mapping therefore quotes the wrong code at
it, so the cases that are not a plain one-to-one mapping are the ones worth pinning: an
inserted line, a deleted line, a replaced block, and where an empty excerpt is placed.
"""

import pytest

from calu.comments.mapping import excerpt, map_selection, split_lines

BEFORE = "a\nb\nc\nd\ne\n"
AFTER = "a\nb\nc\nX\nd\ne\n"
"""One line inserted after "c"."""


def test_lines_are_counted_the_way_an_editor_counts_them() -> None:
    """A trailing newline does not add a line; CR, LF and CRLF all break one."""
    assert split_lines("") == []
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\nb") == ["a", "b"]
    assert split_lines("a\r\nb\rc\n") == ["a", "b", "c"]
    # A blank line in the middle is a line; only the final empty piece is dropped.
    assert split_lines("a\n\nb\n") == ["a", "", "b"]
    assert split_lines("\n") == [""]


def test_an_excerpt_refuses_a_range_outside_the_document() -> None:
    """The range comes from a front end, so it is checked rather than clamped."""
    lines = ["a", "b", "c"]
    assert excerpt(lines, 2, 3).text == "b\nc"
    # An empty range (end == start - 1) is allowed: it positions an empty excerpt.
    assert excerpt(lines, 2, 1).text == ""
    for start, end in ((0, 1), (1, 4), (4, 4), (2, 1_000)):
        with pytest.raises(ValueError, match="outside"):
            excerpt(lines, start, end)


def test_unchanged_lines_map_one_to_one() -> None:
    """The ordinary case: the same lines on both sides, at their own numbers."""
    before, after = map_selection(BEFORE, AFTER, "after", 1, 3)
    assert after.text == "a\nb\nc"
    assert (before.start_line, before.end_line) == (1, 3)
    assert before.text == "a\nb\nc"


def test_a_selection_of_only_an_inserted_line_has_no_counterpart() -> None:
    """The line exists on "after" alone, so "before" gets an empty excerpt in its place."""
    before, after = map_selection(BEFORE, AFTER, "after", 4, 4)
    assert after.text == "X"
    assert before.text == ""
    # Positioned where the line would be: after "c", which is before's line 3.
    assert (before.start_line, before.end_line) == (4, 3)


def test_a_selection_of_only_a_deleted_line_has_no_counterpart() -> None:
    """The mirror case: selecting on "before" a line that "after" does not have."""
    before, after = map_selection(AFTER, BEFORE, "before", 4, 4)
    assert before.text == "X"
    assert after.text == ""
    assert (after.start_line, after.end_line) == (4, 3)


def test_a_line_that_exists_only_on_the_other_side_is_included_when_it_is_inside() -> None:
    """Selecting across the insertion point brings the inserted line along."""
    # On "before", lines 3..4 are "c" and "d" -- the insertion sits strictly between them.
    before, after = map_selection(BEFORE, AFTER, "before", 3, 4)
    assert before.text == "c\nd"
    assert after.text == "c\nX\nd"
    assert (after.start_line, after.end_line) == (3, 5)


def test_a_replaced_block_maps_to_its_whole_counterpart() -> None:
    """Half of a rewritten block is still the whole block on the other side."""
    old = "a\nold one\nold two\nz\n"
    new = "a\nnew one\nnew two\nnew three\nz\n"
    before, after = map_selection(old, new, "after", 2, 2)
    assert after.text == "new one"
    # The counterpart is the complete replaced block, not one line of it.
    assert before.text == "old one\nold two"
    assert (before.start_line, before.end_line) == (2, 3)


def test_a_selection_past_the_end_lands_at_the_end_of_the_other_side() -> None:
    """A front end may select the last, empty line; that must not raise."""
    before, after = map_selection("a\nb\n", "a\nb\nc\nd\n", "after", 3, 4)
    assert after.text == "c\nd"
    assert before.text == ""
    assert (before.start_line, before.end_line) == (3, 2)


def test_empty_documents_map_to_empty_excerpts() -> None:
    """A new file has no "before", and a deleted one has no "after"."""
    before, after = map_selection("", "only\n", "after", 1, 1)
    assert after.text == "only"
    assert (before.start_line, before.end_line, before.text) == (1, 0, "")
    before, after = map_selection("gone\n", "", "before", 1, 1)
    assert before.text == "gone"
    assert (after.start_line, after.end_line, after.text) == (1, 0, "")
    # Both empty: an empty selection is the only valid one.
    before, after = map_selection("", "", "after", 1, 0)
    assert before.text == "" and after.text == ""
