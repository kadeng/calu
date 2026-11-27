"""The fast reference index (`calu.refindex.lsp`) against a scripted language server.

`tests/unit/fake_lsp_server.py` plays the server: a child process of the test that answers
from a JSON file and reads no project file.
"""

import json
import sys
from pathlib import Path
from typing import Any

from calu.refindex import Unit
from calu.refindex.lsp import LspIndex

SERVER = Path(__file__).with_name("fake_lsp_server.py")


def project(tmp_path: Path, script: dict[str, Any]) -> LspIndex:
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
    return LspIndex(root, command=(sys.executable, str(SERVER), str(plan)))


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
        answer = index.referrers(Unit("a.py", "Store.save", line=2))
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
        answer = index.referrers(Unit("a.py", "Store.save", line=2))
    assert answer.referrers == () and not answer.complete
    assert "did not report that it has read the project" in answer.problem


def test_a_missing_server_is_a_problem_not_an_empty_answer(tmp_path: Path) -> None:
    """Without the server's program there is no fast index; the answer says so."""
    index = LspIndex(tmp_path, command=("calu-no-such-language-server",))
    answer = index.referrers(Unit("a.py", "Store.save", line=2))
    assert not answer.complete and "is not installed" in answer.problem


def test_a_unit_whose_name_is_not_on_its_line_is_a_problem(tmp_path: Path) -> None:
    """The server is asked at the position of the name; a wrong line cannot be asked about."""
    with project(tmp_path, {"ready": True}) as index:
        answer = index.referrers(Unit("a.py", "Store.save", line=1))
    assert not answer.complete and "is not on line 1 of a.py" in answer.problem
