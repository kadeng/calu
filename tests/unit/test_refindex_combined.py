"""The reference judge and the combined index (`calu.refindex.judge`, `.combined`).

The judge's model is a function that answers by markers in the request; the fast index is a
small class with fixed answers. No language server and no model is started.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.refindex import Place, Referrers, SearchIndex, Unit
from calu.refindex.combined import CombinedIndex
from calu.refindex.judge import CONTEXT_LINES, MatchJudge
from calu.refindex.model import Resolution

pytestmark = pytest.mark.anyio

UNIT = Unit("store.py", "Store.save", line=2)


def project(tmp_path: Path) -> Path:
    """Write a project with one unit and four places that hold its name.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The project directory (no git repository: every file is searched).
    """
    (tmp_path / "store.py").write_text(
        "class Store:\n    def save(self, x):\n        return x\n", "utf-8"
    )
    (tmp_path / "typed.py").write_text("def run(store: Store):\n    store.save(1)\n", "utf-8")
    (tmp_path / "other.py").write_text("def run(disk: Disk):\n    disk.save(2)\n", "utf-8")
    filler = "".join(f"# line {n}\n" for n in range(CONTEXT_LINES + 3))
    (tmp_path / "far.py").write_text(f"thing = Store()\n{filler}thing.save(3)\n", "utf-8")
    (tmp_path / "glue.rs").write_text('#[pyo3(name = "save")]\nfn store_save() {}\n', "utf-8")
    return tmp_path


def judging(requests: list[str]) -> FunctionModel:
    """Build a judge model that answers by what the request shows.

    A place whose marked line holds `#[pyo3` is answered yes. A marked `thing.save` is yes if
    the request also shows `thing = Store()`, and otherwise unsure with `needs_file`. Any
    other place is unsure.

    Args:
        requests: Gets the text of every request.

    Returns:
        The model.
    """

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = next(
            str(part.content) for part in messages[-1].parts if isinstance(part, UserPromptPart)
        )
        requests.append(text)
        verdicts = []
        for number, body in re.findall(
            r"## Place (\d+): [^\n]*\n(.*?)(?=\n## Place|\Z)", text, re.S
        ):
            marked = "\n".join(line for line in text.splitlines() if line.startswith(">>"))
            if "#[pyo3" in body:
                verdict = {"refers": "yes", "reason": "a binding under that name"}
            elif "thing.save" in body or (
                "(in the file shown above)" in body and "thing.save" in marked
            ):
                if "thing = Store()" in text:
                    verdict = {"refers": "yes", "reason": "thing is a Store"}
                else:
                    verdict = {"refers": "unsure", "needs_file": True}
            else:
                verdict = {"refers": "unsure"}
            verdicts.append({"number": int(number), **verdict})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"places": verdicts})])

    return FunctionModel(answer)


@dataclass
class FastIndex:
    """A fast index with fixed answers: it confirms `typed.py` and resolves `other.py` away."""

    asked: list[str] = field(default_factory=list)

    async def referrers(self, unit: Unit) -> Referrers:
        """Report the one place it knows as a reference.

        Args:
            unit: The unit, unused.

        Returns:
            The call in `typed.py`.
        """
        return Referrers(referrers=(Place("typed.py", 2, 10, "store.save(1)"),))

    async def resolve(self, place: Place, unit: Unit) -> Resolution:
        """Resolve a place in `other.py` to another definition, and nothing else.

        Args:
            place: The place.
            unit: The unit, unused.

        Returns:
            `OTHER` for `other.py`, else `UNKNOWN`.
        """
        self.asked.append(place.path)
        return Resolution.OTHER if place.path == "other.py" else Resolution.UNKNOWN


async def test_each_stage_sees_only_what_the_one_before_could_not_settle(tmp_path: Path) -> None:
    """The owner's design: the search finds four places. The fast index confirms one and
    explains one away, and is not asked about the one it confirmed. The judge reads the two
    that are left: the binding in another language is a referrer from its excerpt, and the
    call whose receiver is defined out of the excerpt's reach is settled from the whole
    file, which the judge asked for."""
    root = project(tmp_path)
    requests: list[str] = []
    fast = FastIndex()
    index = CombinedIndex(SearchIndex(root), fast, MatchJudge(root, judging(requests)))
    answer = await index.referrers(UNIT)
    assert [(p.path, p.line) for p in answer.referrers] == [
        ("far.py", CONTEXT_LINES + 5),
        ("glue.rs", 1),
        ("typed.py", 2),
    ]
    assert answer.unclear == () and answer.complete
    assert answer.dismissed == 2  # the definition itself and the call in other.py
    assert sorted(fast.asked) == ["far.py", "glue.rs", "other.py"]
    assert len(requests) == 2
    assert "other.py" not in requests[0] and "typed.py" not in requests[0]
    assert "`Store.save`, defined in `store.py`, line 2" in requests[0]
    assert "thing = Store()" not in requests[0] and "thing = Store()" in requests[1]
    assert f">> {CONTEXT_LINES + 5}: thing.save(3)" in requests[1]


async def test_without_a_fast_index_and_a_judge_every_place_stays_unclear(tmp_path: Path) -> None:
    """An index that knows nothing hides no referrer: the combination without its two
    optional stages is the word search."""
    root = project(tmp_path)
    answer = await CombinedIndex(SearchIndex(root)).referrers(UNIT)
    assert answer.referrers == () and len(answer.unclear) == 4


async def test_a_judge_that_fails_leaves_its_places_unclear(tmp_path: Path) -> None:
    """A model error dismisses nothing: the places the judge was asked about stay unclear."""

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("no model")

    root = project(tmp_path)
    index = CombinedIndex(SearchIndex(root), FastIndex(), MatchJudge(root, FunctionModel(broken)))
    answer = await index.referrers(UNIT)
    assert [p.path for p in answer.referrers] == ["typed.py"]
    assert sorted(p.path for p in answer.unclear) == ["far.py", "glue.rs"]


async def test_a_fast_index_that_is_not_ready_adds_its_problem_and_loses_nothing(
    tmp_path: Path,
) -> None:
    """A fast index without an answer names its problem; its places go on to the judge or
    stay unclear, so the answer is still complete."""

    @dataclass
    class NotReady:
        async def referrers(self, unit: Unit) -> Referrers:
            """Answer as an index whose server is not ready.

            Args:
                unit: The unit, unused.

            Returns:
                No referrers and the problem.
            """
            return Referrers(complete=False, problem="The language server is not ready.")

    root = project(tmp_path)
    answer = await CombinedIndex(SearchIndex(root), NotReady()).referrers(UNIT)
    assert len(answer.unclear) == 4 and answer.complete
    assert "The fast index: The language server is not ready." in answer.problem
