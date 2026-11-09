"""Markdown helpers for text that quotes untrusted content (commands, diffs, file excerpts)."""


def fence(text: str, info: str = "") -> str:
    """Return `text` in a Markdown code fence that the text itself cannot close.

    The fence is one backtick longer than the longest run of backticks in `text` (at least
    three), so content such as a Bash command containing a line of three backticks stays inside
    the code block and cannot inject Markdown that hides the rest of the content.

    Args:
        text: The content to quote verbatim.
        info: Optional info string after the opening fence, e.g. "bash" or "diff".

    Returns:
        Opening fence line, `text` (without a trailing newline), closing fence line.
    """
    longest, run = 0, 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    marker = "`" * max(3, longest + 1)
    return f"{marker}{info}\n{text.removesuffix(chr(10))}\n{marker}"
