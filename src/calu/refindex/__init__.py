"""The reference index: which places of a project reference a unit of its code.

The honesty state needs this when the contract of a unit changes: the units that reference it
relied on the old contract and have to be judged again
(`agent/records/notes/honesty-dependencies-design.md`). The index sits behind one interface,
`ReferenceIndex`, so that an implementation can be exchanged for another (the owner's
requirement 52):

    SearchIndex      `search.py`: a word search over the project's files. Slow and complete:
                     it reports every place the bare name occurs and cannot tell which of
                     them mean the unit. Every hit outside a comment or a string is "unclear".
    LspIndex         `lsp.py`: a language server. Fast and exact for its language; what it
                     cannot resolve is missing from its answer.
    CombinedIndex    `combined.py`: the search's hits, sorted by a fast index and then by a
                     model (`judge.py`) that reads the places nothing cheaper could settle.

This package's own names need no model library; `judge` and `combined` import Pydantic AI
and are imported by their module names.

An index never answers "no referrers" because it knows nothing: what it cannot decide is
unclear, and a checker reads those places.
"""

from calu.refindex.model import Place, ReferenceIndex, Referrers, Unit
from calu.refindex.search import SearchIndex

__all__ = ["Place", "ReferenceIndex", "Referrers", "SearchIndex", "Unit"]
