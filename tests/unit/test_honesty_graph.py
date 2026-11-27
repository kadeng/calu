"""The honesty graph built from the honesty state (`calu.honesty_graph`)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from calu.honesty import Baseline, HonestyMark, HonestySnapshot, RecheckUnit, Unreviewed
from calu.honesty_graph import (
    MAX_CALLERS,
    MAX_NODES,
    add_neighbours,
    build_graph,
    changed_units,
    per_file,
)
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
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path))
    assert {node.id: node.state for node in wider.nodes} == {
        "parser.py:fields": "dishonest",
        "parser.py:count": "caller",
        "app.py:main": "caller",
    }
    assert {(edge.source, edge.target, edge.certain) for edge in wider.edges} == {
        ("parser.py:count", "parser.py:fields", False),
        ("app.py:main", "parser.py:fields", False),
    }
    assert not wider.partial


STORE = "def save(row):\n    return row\n"

REPORT_BEFORE = '''from store import save


def report(line):
    """Return the cleaned line."""
    return clean(line)


def total(line):
    """Return the length of the report."""
    return len(report(line))


def clean(line):
    return line.strip()
'''

REPORT_AFTER = REPORT_BEFORE.replace("    return clean(line)\n", "    return save(clean(line))\n")

MAIN = "from report import report\n\n\ndef main(line):\n    print(report(line))\n"


def write_report_project(root: Path) -> HonestySnapshot:
    """Write a project whose unreviewed file `report.py` has one changed unit, `report`.

    `report` uses `clean` of the same file and `save` of `store.py`; `total` of the same
    file and `main` of `main.py` use `report`.

    Args:
        root: The project directory.

    Returns:
        The honesty state: `report.py` is unreviewed.
    """
    (root / "store.py").write_text(STORE, "utf-8")
    (root / "report.py").write_text(REPORT_AFTER, "utf-8")
    (root / "main.py").write_text(MAIN, "utf-8")
    return state(unreviewed=Unreviewed(paths=("report.py",)))


async def test_a_changed_unit_is_shown_with_what_it_uses_and_what_uses_it(tmp_path: Path) -> None:
    """The owner: "Show callers and callees of unreviewed nodes ( as far as known ) as well."

    The units a changed unit uses come from its syntax, with a certain edge. The units that
    use it come from the word search, with an edge that is not certain. All of them are
    `related`: unchanged, shown for the picture, blocking nothing.
    """
    snapshot = write_report_project(tmp_path)
    graph = build_graph(tmp_path, snapshot, {"report.py": REPORT_BEFORE}.get)
    assert {node.id: node.state for node in graph.nodes} == {"report.py:report": "changed"}
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path))
    nodes = {node.id: node for node in wider.nodes}
    assert {key: node.state for key, node in nodes.items()} == {
        "report.py:report": "changed",
        "report.py:clean": "related",
        "store.py:save": "related",
        "report.py:total": "related",
        "main.py:main": "related",
    }
    assert {(edge.source, edge.target, edge.certain) for edge in wider.edges} == {
        ("report.py:report", "report.py:clean", True),
        ("report.py:report", "store.py:save", True),
        ("report.py:total", "report.py:report", False),
        ("main.py:main", "report.py:report", False),
    }
    assert nodes["store.py:save"].detail == "Used by report."
    assert nodes["main.py:main"].detail == "Uses report."
    assert not wider.partial


async def test_two_changed_units_are_connected_and_keep_their_state(tmp_path: Path) -> None:
    """A changed unit that uses another changed unit gets the edge; neither turns `related`."""
    write_report_project(tmp_path)
    snapshot = state(unreviewed=Unreviewed(paths=("report.py",)))
    before = REPORT_BEFORE.replace("return line.strip()", "return line")
    graph = build_graph(tmp_path, snapshot, {"report.py": before}.get)
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path))
    states = {node.id: node.state for node in wider.nodes}
    assert states["report.py:report"] == "changed" and states["report.py:clean"] == "changed"
    assert ("report.py:report", "report.py:clean", True) in {
        (edge.source, edge.target, edge.certain) for edge in wider.edges
    }


async def test_the_search_for_callers_stops_when_its_time_is_used_up(tmp_path: Path) -> None:
    """With no time for the index, the used units are still shown and the graph says that
    something is missing."""
    snapshot = write_report_project(tmp_path)
    graph = build_graph(tmp_path, snapshot, {"report.py": REPORT_BEFORE}.get)
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path), budget_s=0)
    assert wider.partial
    assert {node.id for node in wider.nodes} == {
        "report.py:report",
        "report.py:clean",
        "store.py:save",
    }


async def test_at_most_twelve_clean_callers_of_a_unit_are_shown(tmp_path: Path) -> None:
    """The owner: "by default we don't show more than 12 callers of a node unless it's
    dishonest or unreviewed itself".

    `target` has 20 unchanged callers and one changed caller. The changed one is connected
    in any case; 12 of the others are shown, and the node says that 8 are left out.
    """
    callers = "".join(f"def c{n:02}():\n    return target()\n\n\n" for n in range(20))
    before = f"def target():\n    return 0\n\n\n{callers}def busy():\n    return 1\n"
    after = before.replace("return 0", "return 2").replace("return 1", "return target()")
    (tmp_path / "many.py").write_text(after, "utf-8")
    snapshot = state(unreviewed=Unreviewed(paths=("many.py",)))
    graph = build_graph(tmp_path, snapshot, {"many.py": before}.get)
    assert {node.id for node in graph.nodes} == {"many.py:target", "many.py:busy"}
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path))
    nodes = {node.id: node for node in wider.nodes}
    assert sum(node.state == "related" for node in wider.nodes) == MAX_CALLERS
    assert nodes["many.py:target"].hidden_callers == 20 - MAX_CALLERS
    assert nodes["many.py:busy"].state == "changed" and nodes["many.py:busy"].hidden_callers == 0
    assert ("many.py:busy", "many.py:target") in {(e.source, e.target) for e in wider.edges}
    assert not wider.partial


async def test_no_clean_unit_is_added_to_a_full_graph(tmp_path: Path) -> None:
    """At `MAX_NODES` nodes the graph takes edges between its nodes, but no further node."""
    body = "def helper():\n    return 0\n\n\n" + "".join(
        f"def f{n}():\n    return helper()\n\n\n" for n in range(MAX_NODES)
    )
    (tmp_path / "many.py").write_text(body, "utf-8")
    marks = tuple(mark(n, "many.py", f"f{n}") for n in range(MAX_NODES))
    graph = build_graph(tmp_path, state(marks=marks), lambda _path: None)
    assert not graph.by_file and len(graph.nodes) == MAX_NODES
    wider = await add_neighbours(graph, tmp_path, SearchIndex(tmp_path), budget_s=0)
    assert wider.partial and len(wider.nodes) == MAX_NODES
    assert "many.py:helper" not in {node.id for node in wider.nodes}


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
