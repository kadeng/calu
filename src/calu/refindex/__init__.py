"""The reference index: which places in a project refer to a given function, method or class.

Why calu needs it: when the documented behaviour of a unit changes, the code that uses the
unit relied on the old description and has to be checked again for honesty
(`agent/records/notes/honesty-dependencies-design.md`).

All implementations answer the same question through one interface, `ReferenceIndex`, so
that one can be exchanged for another (the owner's requirement 52):

- `SearchIndex` (`search.py`): a word search over the project's files. Slow, and it misses
  nothing. It cannot tell which occurrences really mean the unit, so it reports them all as
  "unclear".
- `LspIndex` (`lsp.py`): asks a language server. Fast and exact for one language. What the
  server cannot work out is silently missing.
- `CbmIndex` (`cbm.py`): asks the code graph of codebase-memory-mcp. No setup, many
  languages, and it finds fewer referrers than a language server.
- `CombinedIndex` (`combined.py`): the word search first, then a fast index, then a model
  (`judge.py`) for the places nothing cheaper could decide.

The rule all of them follow: an index never answers "nothing refers to this" merely because
it knows nothing. What it cannot decide is reported as unclear, and a checker reads those
places.

Importing this package does not load a model library. `judge` and `combined` use Pydantic
AI; import them by their module names.
"""

from calu.refindex.model import Place, ReferenceIndex, Referrers, Unit
from calu.refindex.search import SearchIndex

__all__ = ["Place", "ReferenceIndex", "Referrers", "SearchIndex", "Unit"]
