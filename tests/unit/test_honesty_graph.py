"""The honesty graph built from the honesty state (`calu.honesty_graph`)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from calu.honesty import Baseline, HonestyMark, HonestySnapshot, RecheckUnit, Unreviewed
from calu.honesty_graph import MAX_NODES, add_callers, build_graph, changed_units, per_file
from calu.refindex import SearchIndex

pytestmark = pytest.mark.anyio

BEFORE = '''def fields(line):
    """Return the fields, sorted."""
    return sorted(line.split(","))


def count(line):
    return len(fields(line))
'''

AFTER = '''def fields(line):
    """Return the fields."""
    return line.split(",")


def count(line):
    return len(fields(line))


class Table:
    def rows(self):
        return []
'''

APP = "from parser import fields\n\n\ndef main(line):\n    print(fields(line))\n"


def state(**parts: object) -> HonestySnapshot:
    """Build a honesty snapshot with a baseline and the given parts.

    Args:
        parts: `marks`, `recheck`, `unreviewed`.

    Returns:
        The snapshot.
    """
    base = Baseline("1" * 40, "2" * 40, "user")
    return HonestySnapshot(
        baseline=base,
        marks=parts.get("marks", ()),  # type: ignore[arg-type]
        unreviewed=parts.get("unreviewed", Unreviewed()),  # type: ignore[arg-type]
        review_required=False,
        recheck=parts.get("recheck", ()),  # type: ignore[arg-type]
    )


def mark(number: int, path: str, symbol: str, reason: str = "says sorted") -> HonestyMark:
    """Build an open mark.

    Args:
        number: Its id.
        path: The file.
        symbol: The unit; "" for the file.
        reason: The finding.

    Returns:
        The mark.
    """
    return HonestyMark(number, path, symbol, reason, datetime.now(UTC), "honesty")


def test_only_the_units_that_changed_are_named(tmp_path: Path) -> None:
    """A changed doc comment and body name the function; a new class is named once, without
    its methods; an untouched function is not named. A file that is not Python, and a new
    file, are one node each."""
    (tmp_path / "parser.py").write_text(AFTER, "utf-8")
    assert changed_units(tmp_path, "parser.py", BEFORE) == [
        ("fields", "The docstring and body changed; the signature did not."),
        ("Table", "New."),
    ]
    assert changed_units(tmp_path, "parser.py", None) == [("", "The file is new.")]
    (tmp_path / "run.sh").write_text("echo hi\n", "utf-8")
    assert changed_units(tmp_path, "run.sh", "echo\n") == [("", "The file changed.")]


def test_the_graph_holds_what_is_not_clean_with_the_strongest_state(tmp_path: Path) -> None:
    """A mark is red, a changed unit yellow, a unit to judge again orange with an edge to the
    unit whose contract changed, which is shown as honest. A unit that is marked and changed
    is dishonest. The files of a running review are flagged."""
    (tmp_path / "parser.py").write_text(AFTER, "utf-8")
    (tmp_path / "app.py").write_text(APP, "utf-8")
    snapshot = state(
        marks=(mark(1, "parser.py", "count"),),
        unreviewed=Unreviewed(paths=("parser.py",)),
        recheck=(RecheckUnit(7, "app.py", "main", "parser.py:fields", "old", "new"),),
    )
    graph = build_graph(tmp_path, snapshot, {"parser.py": BEFORE}.get, reviewing=["app.py"])
    nodes = {node.id: node for node in graph.nodes}
    assert {key: node.state for key, node in nodes.items()} == {
        "parser.py:count": "dishonest",
        "parser.py:fields": "changed",
        "parser.py:Table": "changed",
        "app.py:main": "recheck",
    }
    assert nodes["parser.py:count"].mark_ids == (1,) and nodes["parser.py:count"].line == 6
    assert "says sorted (found by honesty)" in nodes["parser.py:count"].detail
    assert nodes["app.py:main"].recheck_ids == (7,) and nodes["app.py:main"].reviewing
    assert [(edge.source, edge.target) for edge in graph.edges] == [
        ("app.py:main", "parser.py:fields")
    ]
    clean_cause = build_graph(tmp_path, state(recheck=snapshot.recheck), lambda _path: None)
    assert {node.id: node.state for node in clean_cause.nodes}["parser.py:fields"] == "honest"


def test_a_mark_on_a_unit_that_is_gone_is_shown_on_the_file(tmp_path: Path) -> None:
    """A symbol the file does not define, and a mark on the whole file, give a file node."""
    (tmp_path / "parser.py").write_text(AFTER, "utf-8")
    graph = build_graph(tmp_path, state(marks=(mark(1, "parser.py", "gone"),)), lambda _p: None)
    (node,) = graph.nodes
    assert (node.id, node.kind, node.name, node.line) == ("parser.py", "file", "parser.py", 1)
    assert node.detail.startswith("gone: says sorted")


async def test_the_callers_of_a_dishonest_unit_are_added_for_the_view(tmp_path: Path) -> None:
    """The word search finds the places that hold the dishonest unit's name. The unit around
    each becomes a `caller` node with an edge that is not certain; the import is left out; a
    unit that is already in the graph keeps its stronger state."""
    (tmp_path / "parser.py").write_text(AFTER, "utf-8")
    (tmp_path / "app.py").write_text(APP, "utf-8")
    graph = build_graph(tmp_path, state(marks=(mark(1, "parser.py", "fields"),)), lambda _p: None)
    wider = await add_callers(graph, tmp_path, SearchIndex(tmp_path))
    assert {node.id: node.state for node in wider.nodes} == {
        "parser.py:fields": "dishonest",
        "parser.py:count": "caller",
        "app.py:main": "caller",
    }
    assert {(edge.source, edge.target, edge.certain) for edge in wider.edges} == {
        ("parser.py:count", "parser.py:fields", False),
        ("app.py:main", "parser.py:fields", False),
    }


def test_a_large_graph_is_reduced_to_one_node_per_file(tmp_path: Path) -> None:
    """Above `MAX_NODES` nodes the graph has one node per file, with the strongest state and
    the number of its units."""
    body = "".join(f"def f{n}():\n    return {n}\n\n\n" for n in range(MAX_NODES + 5))
    (tmp_path / "many.py").write_text(body, "utf-8")
    marks = tuple(mark(n, "many.py", f"f{n}") for n in range(MAX_NODES + 5))
    graph = build_graph(tmp_path, state(marks=marks), lambda _path: None)
    (node,) = graph.nodes
    assert graph.by_file and (node.id, node.kind, node.count) == ("many.py", "file", MAX_NODES + 5)
    assert node.state == "dishonest" and len(node.mark_ids) == MAX_NODES + 5
    assert per_file(graph).nodes == graph.nodes
