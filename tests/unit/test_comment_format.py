"""The message a comment becomes in Claude's session (`calu.comments.format`).

A comment is injected as a user message, so this text is what Claude acts on: the path, which
lines, the code as it was and as it is, and the person's words. The line labels are the part
worth pinning -- "line 7" and "lines 7-9" read differently, and a selection that maps to
nothing on the other side has no lines at all, which has to say so rather than claim line 0.
"""

from calu.comments.format import format_comment
from calu.comments.model import Comment, Excerpt


def excerpt(start: int, end: int, text: str) -> Excerpt:
    """Build an excerpt.

    Args:
        start: First line, 1-based.
        end: Last line, inclusive; `start - 1` for an empty range.
        text: The excerpt's text.

    Returns:
        The excerpt.
    """
    return Excerpt(start_line=start, end_line=end, text=text)


def test_a_file_comment_names_the_path_the_lines_and_quotes_the_code() -> None:
    """The simple case: a selection in a project file."""
    message = format_comment(
        Comment(
            kind="file",
            path="src/a.py",
            after=excerpt(7, 9, "def f():\n    return 1"),
            text="name it after what it returns",
        )
    )
    assert message.startswith("[User comment on `src/a.py`, lines 7-9]")
    assert "def f():" in message
    assert message.rstrip().endswith("name it after what it returns")
    assert "Comment:" in message


def test_one_line_and_no_line_read_differently() -> None:
    """A label is the only thing that tells Claude where a comment points."""
    one = format_comment(
        Comment(kind="file", path="a.py", after=excerpt(4, 4, "x = 1"), text="why?")
    )
    assert "`a.py`, line 4]" in one

    # An empty range is an insertion point: end_line is start_line - 1.
    nothing = format_comment(
        Comment(kind="file", path="a.py", after=excerpt(4, 3, ""), text="add it here")
    )
    assert "no lines (insertion point before line 4)" in nothing


def test_a_diff_comment_quotes_both_sides() -> None:
    """The point of a diff comment: Claude sees what the line was and what it became."""
    message = format_comment(
        Comment(
            kind="diff",
            path="src/a.py",
            ref="HEAD~1",
            before=excerpt(7, 7, "return 1"),
            after=excerpt(7, 8, "return 2\nprint(1)"),
            text="the print stays in",
        )
    )
    assert "[User comment on a diff against HEAD~1 of `src/a.py`]" in message
    assert "Before the change (line 7):" in message and "return 1" in message
    assert "After the change (lines 7-8):" in message and "print(1)" in message
    assert message.index("Before the change") < message.index("After the change")

    # Without a ref the header simply says "a diff".
    plain = format_comment(
        Comment(
            kind="diff",
            path="a.py",
            before=excerpt(1, 1, "old"),
            after=excerpt(1, 1, "new"),
            text="why",
        )
    )
    assert "[User comment on a diff of `a.py`]" in plain


def test_code_that_contains_a_fence_is_still_quoted_safely() -> None:
    """A Markdown fence inside the excerpt must not end the quote early."""
    message = format_comment(
        Comment(
            kind="file",
            path="README.md",
            after=excerpt(1, 2, "```python\nx = 1"),
            text="this fence is unbalanced",
        )
    )
    # The quote has to be longer than the fence it contains, or Claude reads the comment as code.
    assert "````" in message or "~~~" in message
    assert "x = 1" in message
