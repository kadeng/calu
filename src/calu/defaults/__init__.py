"""Built-in defaults: checker instructions and rule lists.

`calu setup` loads them into the database; `default_instructions` is the fallback for checker
types that the database does not hold.
"""

from calu.defaults.instructions import default_instructions

__all__ = ["default_instructions"]
