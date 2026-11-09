"""Render a `Comment` as the user message that is injected into Claude's session."""

from calu.comments.model import Comment, Excerpt
from calu.markdown import fence


def _lines_label(ex: Excerpt) -> str:
    """Return "line N", "lines N-M" or "no lines (insertion point before line N)".

    Args:
        ex: The excerpt whose line range is described.

    Returns:
        The label for use in the comment message.
    """
    if ex.is_empty:
        return f"no lines (insertion point before line {ex.start_line})"
    if ex.start_line == ex.end_line:
        return f"line {ex.start_line}"
    return f"lines {ex.start_line}-{ex.end_line}"


def format_comment(comment: Comment) -> str:
    """Return the Markdown message that tells Claude about `comment`.

    The message names the path and line ranges, quotes the excerpt(s) and ends with the user's
    comment text. Diff comments quote both the text before and after the change.

    Args:
        comment: A valid comment (a diff comment has `before`).

    Returns:
        The Markdown message.
    """
    if comment.kind == "diff":
        assert comment.before is not None
        ref = f" against {comment.ref}" if comment.ref else ""
        header = f"[User comment on a diff{ref} of `{comment.path}`]"
        body = (
            f"Before the change ({_lines_label(comment.before)}):\n"
            f"{fence(comment.before.text)}\n\n"
            f"After the change ({_lines_label(comment.after)}):\n"
            f"{fence(comment.after.text)}"
        )
    else:
        header = f"[User comment on `{comment.path}`, {_lines_label(comment.after)}]"
        body = fence(comment.after.text)
    return f"{header}\n{body}\n\nComment:\n{comment.text}"
