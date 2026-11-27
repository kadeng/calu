"""The fast reference index (`calu.refindex.lsp`) against a scripted language server.

`tests/unit/fake_lsp_server.py` plays the server: a child process of the test that answers
from a JSON file and reads no project file.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from calu.refindex import Place, Unit
from calu.refindex.lsp import lsp_index
from calu.refindex.model import Resolution
from calu.refindex.navigator import NavigatorIndex, Position

SERVER = Path(__file__).with_name("fake_lsp_server.py")


def project(tmp_path: Path, script: dict[str, Any]) -> NavigatorIndex:
    """Write a small project and the server's script, and build the index on them.

    Args:
        tmp_path: The test's temporary directory.
        script: What the scripted server answers.

    Returns:
        The index; the caller closes it.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("class Store:\n    def save(self, x):\n        return x\n", "utf-8")
    (root / "base.py").write_text("class Base:\n    def save(self, x): ...\n", "utf-8")
    (root / "b.py").write_text("store.save(1)\nbase.save(2)\n", "utf-8")
    plan = tmp_path / "script.json"
    plan.write_text(json.dumps(script), "utf-8")
    return lsp_index(root, command=(sys.executable, str(SERVER), str(plan)))


def test_the_index_reports_the_servers_references_and_those_of_a_base_method(
    tmp_path: Path,
) -> None:
    """The server's references of the unit are confirmed places. A call through a base class
    counts too: the server lists it under the base method, and the index finds that method
    through the symbols of the same name and their implementations. The definition itself
    and a place outside the project are left out."""
    script = {
        "ready": True,
        "references": {"a.py:2": [["b.py", 1, 6], ["a.py", 2, 8]], "base.py:2": [["b.py", 2, 5]]},
        "symbols": [["Store.save", "a.py", 2, 8], ["Base.save", "base.py", 2, 8]],
        "implementations": {"base.py:2": [["a.py", 2, 8]]},
    }
    with project(tmp_path, script) as index:
        answer = index.find(Unit("a.py", "Store.save", line=2))
    assert [(p.path, p.line, p.column) for p in answer.referrers] == [
        ("b.py", 1, 6),
        ("b.py", 2, 5),
    ]
    assert answer.referrers[0].text == "store.save(1)"
    assert answer.unclear == () and answer.complete and answer.problem == ""


def test_a_server_that_never_reports_ready_gives_no_complete_answer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Measured with basedpyright: asked before it has read the project, the server answers
    with an empty list. Without the ready message the index says that its answer is not
    complete, so an empty list is not read as "no referrers"."""
    monkeypatch.setattr("calu.refindex.lsp.START_TIMEOUT_S", 0.5)
    with project(tmp_path, {"ready": False}) as index:
        answer = index.find(Unit("a.py", "Store.save", line=2))
    assert answer.referrers == () and not answer.complete
    assert "did not report that it has read the project" in answer.problem


def test_a_missing_server_is_a_problem_not_an_empty_answer(tmp_path: Path) -> None:
    """Without the server's program there is no fast index; the answer says so."""
    index = lsp_index(tmp_path, command=("calu-no-such-language-server",))
    answer = index.find(Unit("a.py", "Store.save", line=2))
    assert not answer.complete and "is not installed" in answer.problem


def test_a_unit_whose_name_is_not_on_its_line_is_a_problem(tmp_path: Path) -> None:
    """The server is asked at the position of the name; a wrong line cannot be asked about."""
    with project(tmp_path, {"ready": True}) as index:
        answer = index.find(Unit("a.py", "Store.save", line=1))
    assert not answer.complete and "is not on line 1 of a.py" in answer.problem


def test_resolving_a_place_asks_where_its_name_is_defined(tmp_path: Path) -> None:
    """`resolve` asks the server for the definition of the name at a place: the unit itself
    means the place refers to it, another definition means it does not, no answer means the
    server does not know. A file the server does not cover is never asked about."""
    script = {
        "ready": True,
        "definitions": {"b.py:1": [["a.py", 2, 8]], "b.py:2": [["base.py", 2, 8]]},
    }
    unit = Unit("a.py", "Store.save", line=2)
    with project(tmp_path, script) as index:
        assert index.resolve_blocking(Place("b.py", 1, 6), unit) is Resolution.UNIT
        assert index.resolve_blocking(Place("b.py", 2, 5), unit) is Resolution.OTHER
        assert index.resolve_blocking(Place("a.py", 3, 15), unit) is Resolution.UNKNOWN
        assert index.resolve_blocking(Place("glue.rs", 1, 0), unit) is Resolution.UNKNOWN


@dataclass
class DictNavigator:
    """A code navigator without a process: answers from dictionaries. It shows that the index
    needs nothing of a language server beyond the `CodeNavigator` interface."""

    uses: dict[Position, list[Position]] = field(default_factory=dict)
    ready: bool = True

    def covers(self, path: str) -> bool:
        """Cover Python files.

        Args:
            path: The file.

        Returns:
            True for a name ending in ".py".
        """
        return path.endswith(".py")

    def start(self) -> None:
        """Nothing to start."""

    def references(self, at: Position) -> list[Position]:
        """Answer from `uses`.

        Args:
            at: The position.

        Returns:
            The listed uses.
        """
        return self.uses.get(at, [])

    def definitions(self, at: Position) -> list[Position]:
        """Know no definitions.

        Args:
            at: The position, unused.

        Returns:
            Nothing.
        """
        return []

    def implementations(self, at: Position) -> list[Position]:
        """Know no implementations.

        Args:
            at: The position, unused.

        Returns:
            Nothing.
        """
        return []

    def symbols(self, name: str) -> list[Position]:
        """Know no other symbols.

        Args:
            name: The name, unused.

        Returns:
            Nothing.
        """
        return []

    def changed(self, paths: list[str]) -> None:
        """Ignore changes.

        Args:
            paths: The files, unused.
        """

    def close(self) -> None:
        """Nothing to stop."""


def test_any_navigator_that_answers_the_interface_serves_the_index(tmp_path: Path) -> None:
    """The index works on any `CodeNavigator`; a navigator that is not ready gives answers that
    are marked as not complete."""
    (tmp_path / "a.py").write_text("class Store:\n    def save(self, x): ...\n", "utf-8")
    (tmp_path / "b.py").write_text("store.save(1)\n", "utf-8")
    navigator = DictNavigator({Position("a.py", 2, 8): [Position("b.py", 1, 6)]})
    with NavigatorIndex(tmp_path, navigator) as index:
        answer = index.find(Unit("a.py", "Store.save", line=2))
        assert [(p.path, p.line, p.text) for p in answer.referrers] == [
            ("b.py", 1, "store.save(1)")
        ]
        assert answer.complete
        navigator.ready = False
        assert not index.find(Unit("a.py", "Store.save", line=2)).complete
