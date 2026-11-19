"""Condensed conversation context for the checkers: turn records and CHAT_SUMMARY.md folding.

The turn records are imported with the package. `ContextBuilder` and the summary helpers
need Pydantic AI, so their module is imported at the first access to one of them
(`__getattr__`), as in `calu.checkers`.
"""

from typing import TYPE_CHECKING, Any

from calu.context.turns import StepRecord, TurnRecord, label_for_unchecked
from calu.lazy import load

if TYPE_CHECKING:
    from calu.context.builder import ContextBuilder, join_summary, split_summary

__all__ = [
    "ContextBuilder",
    "StepRecord",
    "TurnRecord",
    "join_summary",
    "label_for_unchecked",
    "split_summary",
]


def __getattr__(name: str) -> Any:
    """Import the module of a lazily exported name and return the name's value (PEP 562).

    Side effect: imports the module (and with it Pydantic AI) and stores the value in this
    package's namespace (`calu.lazy.load`).

    Args:
        name: The attribute that was not found in the package.

    Returns:
        The object of that name.

    Raises:
        AttributeError: If the package exports no such name.
    """
    return load(
        __name__,
        name,
        {
            "ContextBuilder": "calu.context.builder",
            "join_summary": "calu.context.builder",
            "split_summary": "calu.context.builder",
        },
    )
