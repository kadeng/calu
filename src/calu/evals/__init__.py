"""Evaluation of the checkers: cases, runs and metrics (`calu evals`).

The cases are imported with the package. The runner needs the checkers and with them
Pydantic AI, so its module is imported at the first access to one of its names
(`__getattr__`), as in `calu.checkers`.
"""

from typing import TYPE_CHECKING, Any

from calu.evals.cases import EvalCase, builtin_cases, cases_from_escalations, load_cases
from calu.lazy import load

if TYPE_CHECKING:
    from calu.evals.runner import CaseResult, Report, run_cases

__all__ = [
    "CaseResult",
    "EvalCase",
    "Report",
    "builtin_cases",
    "cases_from_escalations",
    "load_cases",
    "run_cases",
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
            "CaseResult": "calu.evals.runner",
            "Report": "calu.evals.runner",
            "run_cases": "calu.evals.runner",
        },
    )
