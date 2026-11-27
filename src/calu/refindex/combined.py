"""The combined reference index: search first, then explain away (the owner's idea).

    search    every place the unit's name occurs               (complete, far too many)
    fast      the places a fast index reports as references    -> referrers
              each other place: what does the name resolve to?
                  the unit                                     -> referrer
                  another definition                           -> dismissed
                  not known                                    -> left over
    judge     the places left over, read by a model            yes -> referrer, no ->
                                                               dismissed, unsure -> unclear

Each stage is cheaper than the next and sees only what the one before could not settle. The
fast index and the judge are optional: without them their places stay unclear, so an index
that knows nothing hides no referrer. The one way to lose a referrer is a wrong "another
definition" of the resolver or a wrong "no" of the judge.
"""

from dataclasses import dataclass

from calu.refindex.judge import MatchJudge
from calu.refindex.model import Place, ReferenceIndex, Referrers, Resolution, Resolver, Unit


@dataclass
class CombinedIndex:
    """A word search whose hits a fast index and a judge sort (see the module docstring)."""

    search: ReferenceIndex
    """The complete index, usually a `SearchIndex`; its unclear places are the candidates."""
    fast: ReferenceIndex | None = None
    """An index whose confirmed referrers are taken as they are, e.g. an `LspIndex`. If it
    is a `Resolver` too, it is asked about every other candidate."""
    judge: MatchJudge | None = None
    """The model that reads what is left over."""

    async def referrers(self, unit: Unit) -> Referrers:
        """Find the places that reference `unit`.

        Side effects: those of the three stages: reading the project's files, asking a
        language server, model calls.

        Args:
            unit: The unit.

        Returns:
            The referrers the fast index or the judge confirmed, the places nobody could
            settle as unclear, and the number of dismissed occurrences. Not `complete` if the
            search was not; a fast index that is not ready or not installed adds its problem
            to `problem` and leaves the answer complete, because its places are still judged
            or unclear.
        """
        found = await self.search.referrers(unit)
        confirmed: dict[tuple[str, int], Place] = {(p.path, p.line): p for p in found.referrers}
        dismissed = found.dismissed
        problem = found.problem
        left: list[Place] = list(found.unclear)
        if self.fast is not None:
            known = await self.fast.referrers(unit)
            if known.problem:
                problem = f"{problem} The fast index: {known.problem}".strip()
            for place in known.referrers:
                confirmed.setdefault((place.path, place.line), place)
            left = [place for place in left if (place.path, place.line) not in confirmed]
            if isinstance(self.fast, Resolver):
                undecided: list[Place] = []
                for place in left:
                    answer = await self.fast.resolve(place, unit)
                    if answer is Resolution.UNIT:
                        confirmed[place.path, place.line] = place
                    elif answer is Resolution.OTHER:
                        dismissed += 1
                    else:
                        undecided.append(place)
                left = undecided
        unclear = left
        if self.judge is not None and left:
            unclear = []
            for place, answer in (await self.judge.judge(unit, left)).items():
                if answer == "yes":
                    confirmed.setdefault((place.path, place.line), place)
                elif answer == "no":
                    dismissed += 1
                else:
                    unclear.append(place)
        return Referrers(
            referrers=tuple(confirmed[key] for key in sorted(confirmed)),
            unclear=tuple(unclear),
            dismissed=dismissed,
            complete=found.complete,
            problem=problem,
        )
