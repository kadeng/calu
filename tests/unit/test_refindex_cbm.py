"""The code graph index (`calu.refindex.cbm`) against a scripted command line.

`tests/unit/fake_cbm.py` plays codebase-memory-mcp: it answers from a JSON file and records
which tools were called.
"""

import json
import sys
from pathlib import Path
from typing import Any

from calu.refindex import Unit
from calu.refindex.cbm import CbmIndex

TOOL = Path(__file__).with_name("fake_cbm.py")


def index_on(tmp_path: Path, plan: dict[str, Any]) -> tuple[CbmIndex, Path]:
    """Write a small project and the tool's script, and build the index on them.

    Args:
        tmp_path: The test's temporary directory.
        plan: What the scripted tool answers; "{p}" in its keys stands for the project name.

    Returns:
        (the index, the file that lists the tools that were called).
    """
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "store.py").write_text(
        "class Store:\n    def save(self, x):\n        return x\n", "utf-8"
    )
    (root / "typed.py").write_text("def run(store: Store):\n    store.save(1)\n", "utf-8")
    script = tmp_path / "plan.json"
    index = CbmIndex(root, command=(sys.executable, str(TOOL), str(script)))
    script.write_text(json.dumps(plan).replace("{p}", index.project), "utf-8")
    return index, Path(f"{script}.calls")


def test_edges_the_tool_resolved_are_referrers_and_name_matches_are_not(tmp_path: Path) -> None:
    """A call the tool resolved through a type is a referrer at the line of the call, and an
    import edge at the first line of the importing unit. A call matched by the bare name, and
    the unit's own definition, are left out. The project is indexed once, before the first
    question, and again after files changed."""
    unit = Unit("store.py", "Store.save", line=2)
    plan = {
        "edges": {
            "{p}.store.Store.save": [
                ["CALLS", "typed.py", "1", "2", "lsp_method"],
                ["CALLS", "other.py", "1", "2", "unique_name"],
                ["IMPORTS", "typed.py", "1", "", ""],
                ["CALLS", "store.py", "1", "2", "same_module"],
            ]
        }
    }
    index, calls = index_on(tmp_path, plan)
    answer = index.find(unit)
    assert [(p.path, p.line, p.text) for p in answer.referrers] == [
        ("typed.py", 1, "def run(store: Store):"),
        ("typed.py", 2, "store.save(1)"),
    ]
    assert answer.complete and answer.unclear == ()
    index.find(unit)
    assert calls.read_text("utf-8").split() == ["index_repository", "query_graph", "query_graph"]
    index.changed(["typed.py"])
    index.find(unit)
    assert calls.read_text("utf-8").split().count("index_repository") == 2


def test_a_unit_the_graph_has_no_node_for_is_a_problem_not_no_referrers(tmp_path: Path) -> None:
    """No edges can mean no referrers or no node (the tool has none for a nested function).
    The index asks which, and without a node its answer is not complete."""
    index, _calls = index_on(tmp_path, {"nodes": []})
    answer = index.find(Unit("store.py", "Store.save", line=2))
    assert not answer.complete and "has no node" in answer.problem
    known, _calls = index_on(tmp_path / "second", {"nodes": ["{p}.store.Store.save"]})
    assert known.find(Unit("store.py", "Store.save", line=2)).complete


def test_a_missing_tool_is_a_problem_not_an_empty_answer(tmp_path: Path) -> None:
    """Without the tool's program there is no code graph; the answer says so."""
    answer = CbmIndex(tmp_path, command=("calu-no-such-code-graph",)).find(Unit("a.py", "f", 1))
    assert not answer.complete and "is not installed" in answer.problem
