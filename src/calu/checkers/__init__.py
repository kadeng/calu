"""Checker agents that review Claude's tool calls before they run.

The data shapes (`Step`, `Decision`, `Route`, ...) are imported with the package. The agents
(`Checker`, `ResultChecker` and what belongs to them) need Pydantic AI, whose import takes
over a second, so their modules are imported at the first access to one of their names
(`__getattr__`): a command of `calu` that runs no checker does not pay for them.
"""

from typing import TYPE_CHECKING, Any

from calu.checkers.model import (
    CheckDecision,
    CheckerEffort,
    CodeMark,
    Decision,
    Step,
    SubagentResult,
)
from calu.checkers.policy import Route, ToolPolicy
from calu.checkers.render import render_step
from calu.lazy import load

if TYPE_CHECKING:
    from calu.checkers.checker import CheckContext, Checker, CheckOutcome
    from calu.checkers.result_checker import (
        ResultChecker,
        ResultOutcome,
        ResultReview,
        ResultVerdict,
    )

__all__ = [
    "CheckContext",
    "CheckDecision",
    "CheckOutcome",
    "Checker",
    "CheckerEffort",
    "CodeMark",
    "Decision",
    "ResultChecker",
    "ResultOutcome",
    "ResultReview",
    "ResultVerdict",
    "Route",
    "Step",
    "SubagentResult",
    "ToolPolicy",
    "render_step",
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
            "CheckContext": "calu.checkers.checker",
            "Checker": "calu.checkers.checker",
            "CheckOutcome": "calu.checkers.checker",
            "ResultChecker": "calu.checkers.result_checker",
            "ResultOutcome": "calu.checkers.result_checker",
            "ResultReview": "calu.checkers.result_checker",
            "ResultVerdict": "calu.checkers.result_checker",
        },
    )
