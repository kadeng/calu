"""Names a package exports but imports only when they are first used.

`calu.checkers`, `calu.context` and `calu.evals` export classes whose modules import Pydantic
AI, which takes over a second. Every `calu` command imports these packages for their data
shapes, so the costly modules are imported at the first access to one of their names: the
package defines `__getattr__` (PEP 562) and calls `load` from it.
"""

import sys
from collections.abc import Mapping
from importlib import import_module
from typing import Any


def load(package: str, name: str, modules: Mapping[str, str]) -> Any:
    """Import the module that defines a lazily exported name and return the name's value.

    Side effect: imports the module and stores the value in the package's namespace, so the
    next access to the name does not call the package's `__getattr__` again.

    Args:
        package: The package's `__name__`; it is imported already (its `__getattr__` runs).
        name: The attribute that was not found in the package.
        modules: Lazily exported name -> the module that defines it.

    Returns:
        The object of that name.

    Raises:
        AttributeError: If `modules` has no such name, as for any missing attribute.

    Example:
        In `calu/context/__init__.py`:
        `def __getattr__(name): return load(__name__, name, {"ContextBuilder":
        "calu.context.builder"})` makes `from calu.context import ContextBuilder` import
        `calu.context.builder` at that moment.
    """
    module = modules.get(name)
    if module is None:
        raise AttributeError(f"module {package!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    setattr(sys.modules[package], name, value)
    return value
