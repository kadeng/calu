"""Data model of user comments on code selections.

A comment refers to a range of lines in a project file. There are two kinds:

- `diff`: made in a diff view. It carries the selected excerpt after the change (`after`) and the
  corresponding excerpt before the change (`before`).
- `file`: made in a regular editor. It carries only `after` (the current text); `before` is `None`.

Line numbers are 1-based and inclusive. An empty excerpt (for example the "before" side of purely
inserted lines) has `end_line == start_line - 1` and `text == ""`; `start_line` is then the line
before which the other side's lines were inserted.
"""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class Excerpt(BaseModel):
    r"""A contiguous range of lines of one version of a file.

    Invariants: `start_line >= 1`; `end_line >= start_line - 1`; `text` holds exactly the lines
    `start_line..end_line`, joined with "\n" and without a trailing newline (empty if the range
    is empty).
    """

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        """Validate `end_line >= start_line - 1` (pydantic validator).

        Returns:
            The unchanged model.

        Raises:
            ValueError: If `end_line < start_line - 1`.
        """
        if self.end_line < self.start_line - 1:
            raise ValueError(f"end_line {self.end_line} < start_line - 1 ({self.start_line - 1})")
        return self

    @property
    def is_empty(self) -> bool:
        """Whether the excerpt covers no lines.

        Returns:
            True if `end_line == start_line - 1`.
        """
        return self.end_line == self.start_line - 1


class Comment(BaseModel):
    """A user comment on a selection, as relayed to Claude and stored for later evaluation.

    Invariants: `kind == "diff"` if and only if `before` is not `None`. `path` is relative to the
    workspace root and uses "/" separators.
    """

    kind: Literal["diff", "file"]
    path: str
    after: Excerpt
    before: Excerpt | None = None
    text: str = Field(min_length=1)
    ref: str | None = None
    """For diff comments: the version of the left side, e.g. "HEAD", "index", or an edit id."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        """Validate the kind/`before` invariant and the path format (pydantic validator).

        Returns:
            The unchanged model.

        Raises:
            ValueError: If `before` does not match `kind`, or `path` starts with "/" or
                contains a backslash.
        """
        if (self.kind == "diff") != (self.before is not None):
            raise ValueError("a diff comment needs `before`; a file comment must not have it")
        if self.path.startswith("/") or "\\" in self.path:
            raise ValueError(f"path must be relative with '/' separators: {self.path!r}")
        return self
