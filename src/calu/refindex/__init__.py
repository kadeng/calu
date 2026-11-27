"""The reference index: which places of a project reference a unit of its code.

The honesty state needs this when the contract of a unit changes: the units that reference it
relied on the old contract and have to be judged again
(`agent/records/notes/honesty-dependencies-design.md`). The index sits behind one interface,
`ReferenceIndex`, so that an implementation can be exchanged for another (the owner's
requirement 52). The first one:

    SearchIndex      a word search over the project's files. Slow and complete: it reports
                     every place the bare name occurs, and cannot tell which of them mean the
                     unit. Every hit outside a comment or a string is "unclear".

To come: a fast one that asks a language server, and one that combines the two.

An index never answers "no referrers" because it knows nothing: what it cannot decide is
unclear, and a checker reads those places.
"""

from calu.refindex.model import Place, ReferenceIndex, Referrers, Unit
from calu.refindex.search import SearchIndex

__all__ = ["Place", "ReferenceIndex", "Referrers", "SearchIndex", "Unit"]
