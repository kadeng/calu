"""The honesty graph: the part of the honesty state that a client draws.

The chat pane can show the honesty state as a graph
(`agent/records/notes/honesty-graph-design.md`). This module turns the state into that graph.
It draws nothing; layout and drawing are the client's.

A node is a unit of code: a function, a method or a class, or a whole file where no unit can
be named. Only units that are not clean are in the graph:

- `dishonest`: a checker or a review found it dishonest. It has an open mark.
- `changed`: its file changed and nobody verified the file. These are the definitions of
  an unreviewed file that differ from the file's earlier text.
- `recheck`: it uses a unit whose signature or doc comment changed
  (`HonestyState.recheck`).
- `caller`: it uses a dishonest unit and is itself unchanged. A reference index finds
  these for the view only (`add_callers`); they block nothing.
- `honest`: the unit whose changed contract caused a `recheck`. It is shown so that the
  edge has an end.

An edge goes from a unit to a unit it uses.

If a unit has several states, the first of this order wins: dishonest, recheck, changed,
caller, honest.

Units are named per definition only in Python files. Any other file is one node.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from calu.checkers.symbols import changed_definitions, definition_lines
from calu.dependents import enclosing_unit
from calu.honesty import HonestySnapshot
from calu.refindex import ReferenceIndex, Unit

NodeState = Literal["dishonest", "recheck", "changed", "caller", "honest"]
"""The states of a node, strongest first (`ORDER`)."""

ORDER: tuple[NodeState, ...] = ("dishonest", "recheck", "changed", "caller", "honest")
"""Which state wins when a unit has several: the earlier one."""

MAX_NODES = 150
"""With more nodes than this the graph is reduced to one node per file (`by_file`)."""

MAX_FILE_BYTES = 2_000_000
"""A larger file is one node; it is not read."""

DETAIL_CHARS = 400
"""The longest detail text of a node."""


@dataclass(frozen=True)
class GraphNode:
    """One unit in the honesty graph."""

    id: str
    """`path:dotted.name`, or the path alone for a whole file."""
    path: str
    """The file, relative to the project directory."""
    line: int
    """The line of the unit's definition (1-based); 1 for a whole file or when not known."""
    name: str
    """The short name to show: `parse`, `Parser.parse`, or the file's name."""
    kind: Literal["unit", "file"]
    state: NodeState
    detail: str = ""
    """What a client shows on hover: the reason of a mark, what changed, or what it uses."""
    reviewing: bool = False
    """True while a running honesty review reads the unit's file."""
    mark_ids: tuple[int, ...] = ()
    """The ids of the open marks on the unit, for clearing one by hand."""
    recheck_ids: tuple[int, ...] = ()
    """The ids of the recheck entries of the unit, for dismissing one by hand."""
    count: int = 1
    """How many units the node stands for; more than 1 only in a graph by file."""


@dataclass(frozen=True)
class GraphEdge:
    """One "uses" relation: `source` uses `target`."""

    source: str
    target: str
    certain: bool = True
    """False for a link that an index found by the name alone and nothing confirmed."""


@dataclass(frozen=True)
class HonestyGraph:
    """The nodes and edges a client draws."""

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    by_file: bool = False
    """True if the graph was reduced to one node per file because it had too many nodes."""
    everything: bool = False
    """True if every file counts as unreviewed (no baseline); the graph then holds only the
    marks and the recheck entries."""


@dataclass
class _Builder:
    """Collects nodes by id and keeps the strongest state of each."""

    root: Path
    nodes: dict[str, GraphNode] = field(default_factory=dict[str, GraphNode])
    edges: set[GraphEdge] = field(default_factory=set[GraphEdge])
    _lines: dict[str, dict[str, int] | None] = field(
        default_factory=dict[str, dict[str, int] | None]
    )

    def lines(self, path: str) -> dict[str, int] | None:
        """Return the definitions of a file with their lines, read once.

        Args:
            path: The file, relative to the project directory.

        Returns:
            Dotted name -> line; None for a file that is not Python, too large, unreadable or
            does not parse.
        """
        if path not in self._lines:
            text = read_text(self.root, path)
            self._lines[path] = None if text is None else definition_lines(text, path)
        return self._lines[path]

    def add(
        self,
        path: str,
        symbol: str,
        state: NodeState,
        detail: str = "",
        mark_id: int | None = None,
        recheck_id: int | None = None,
    ) -> str:
        """Add a unit, or strengthen the node it already has.

        A symbol that the file does not define (any more) is shown as the whole file.

        Args:
            path: The file, relative to the project directory.
            symbol: The dotted name in the file; "" for the whole file.
            state: The state to give it.
            detail: The text for the hover; it replaces the node's text when `state` is the
                stronger one, and is appended when the states are equal.
            mark_id: The id of the mark that causes the node, if any.
            recheck_id: The id of the recheck entry that causes the node, if any.

        Returns:
            The node's id.
        """
        known = self.lines(path)
        if symbol and known is not None and symbol in known:
            node_id, line, kind = f"{path}:{symbol}", known[symbol], "unit"
            name = ".".join(symbol.split(".")[-2:])
        else:
            node_id, line, kind, name = path, 1, "file", path.rsplit("/", 1)[-1]
            if symbol and not detail.startswith(symbol):
                detail = f"{symbol}: {detail}" if detail else symbol
        marks = () if mark_id is None else (mark_id,)
        rechecks = () if recheck_id is None else (recheck_id,)
        old = self.nodes.get(node_id)
        if old is None:
            self.nodes[node_id] = GraphNode(
                node_id,
                path,
                line,
                name,
                kind,
                state,
                detail[:DETAIL_CHARS],
                False,
                marks,
                rechecks,
            )
            return node_id
        stronger = ORDER.index(state) < ORDER.index(old.state)
        if stronger:
            text = detail
        elif state == old.state and detail and detail not in old.detail:
            text = f"{old.detail}\n{detail}" if old.detail else detail
        else:
            text = old.detail
        self.nodes[node_id] = replace(
            old,
            state=state if stronger else old.state,
            detail=text[:DETAIL_CHARS],
            mark_ids=old.mark_ids + marks,
            recheck_ids=old.recheck_ids + rechecks,
        )
        return node_id


def build_graph(
    root: Path,
    snapshot: HonestySnapshot,
    before: Callable[[str], str | None],
    reviewing: Iterable[str] = (),
) -> HonestyGraph:
    """Build the honesty graph from the honesty state, without the callers of dishonest units.

    Side effects: reads the unreviewed and the marked files of the project.

    Args:
        root: The project directory.
        snapshot: The honesty state.
        before: For an unreviewed file, its text when it was last clean (the baseline's, or
            the content last verified); None for a new file or when that text is not known.
        reviewing: The files a running honesty review reads.

    Returns:
        The graph, with one node per file if it has more than `MAX_NODES` nodes. Without a
        baseline (`everything`) the unreviewed files are not listed; the marks and the
        recheck entries are.
    """
    builder = _Builder(root)
    for mark in snapshot.marks:
        by = f" (found by {mark.opened_by})" if mark.opened_by else ""
        builder.add(mark.path, mark.symbol, "dishonest", f"{mark.reason}{by}", mark_id=mark.id)
    for unit in snapshot.recheck:
        cause_path, _, cause_symbol = unit.cause.partition(":")
        source = builder.add(
            unit.path,
            unit.symbol,
            "recheck",
            f"Uses {cause_symbol or cause_path}, whose signature or doc comment changed.",
            recheck_id=unit.id,
        )
        target = builder.add(
            cause_path, cause_symbol, "honest", "Its signature or doc comment changed."
        )
        if source != target:
            builder.edges.add(GraphEdge(source, target))
    if not snapshot.unreviewed.everything:
        for path in snapshot.unreviewed.paths:
            for symbol, detail in changed_units(root, path, before(path)):
                builder.add(path, symbol, "changed", detail)
    read_now = set(reviewing)
    nodes = [replace(node, reviewing=node.path in read_now) for node in builder.nodes.values()]
    graph = HonestyGraph(
        nodes=tuple(sorted(nodes, key=lambda node: (node.path, node.line, node.id))),
        edges=tuple(sorted(builder.edges, key=lambda edge: (edge.source, edge.target))),
        everything=snapshot.unreviewed.everything,
    )
    return per_file(graph) if len(graph.nodes) > MAX_NODES else graph


async def add_callers(graph: HonestyGraph, root: Path, index: ReferenceIndex) -> HonestyGraph:
    """Add the units that use a dishonest unit, as `caller` nodes.

    This is for the view only: a caller of a dishonest unit blocks nothing. The index should
    be a cheap one (the word search, a code navigator), because the view must cost no model
    call.

    Side effects: those of the index; reads the files of the places it reports.

    Args:
        graph: The graph of `build_graph`. A graph by file is returned unchanged.
        root: The project directory.
        index: The reference index to ask about each dishonest unit.

    Returns:
        The graph with the callers and an edge from each to the unit it uses. A place the
        index only suspects gives an edge that is not `certain`.
    """
    if graph.by_file:
        return graph
    builder = _Builder(root, nodes={node.id: node for node in graph.nodes}, edges=set(graph.edges))
    for node in graph.nodes:
        if node.state != "dishonest" or node.kind != "unit":
            continue
        symbol = node.id.partition(":")[2]
        found = await index.referrers(Unit(node.path, symbol, node.line))
        for places, certain in ((found.referrers, True), (found.unclear, False)):
            for place in places:
                unit = enclosing_unit(root, place.path, place.line)
                if unit is None:
                    continue
                source = builder.add(place.path, unit, "caller", f"Uses {node.name}.")
                if source == node.id:
                    continue
                edge = GraphEdge(source, node.id, certain)
                if certain:
                    builder.edges.discard(replace(edge, certain=False))
                    builder.edges.add(edge)
                elif replace(edge, certain=True) not in builder.edges:
                    builder.edges.add(edge)
    result = replace(
        graph,
        nodes=tuple(
            sorted(builder.nodes.values(), key=lambda node: (node.path, node.line, node.id))
        ),
        edges=tuple(sorted(builder.edges, key=lambda edge: (edge.source, edge.target))),
    )
    return per_file(result) if len(result.nodes) > MAX_NODES else result


def changed_units(root: Path, path: str, before: str | None) -> list[tuple[str, str]]:
    """Name the units of a file that differ from an earlier text of it.

    Side effects: reads the file.

    Args:
        root: The project directory.
        path: The file, relative to it.
        before: The earlier text; None if there is none (a new file) or it is not known.

    Returns:
        (dotted name, what changed) for each definition that changed or is new. A new class
        is named once, without its methods. [("", ...)] stands for the whole file: a file
        that is not Python, too large, unreadable or does not parse, a file without an
        earlier text, and a file whose change is outside every definition.
    """
    text = read_text(root, path)
    if text is None:
        return [("", "The file changed.")]
    now = definition_lines(text, path)
    if now is None or before is None:
        return [("", "The file is new." if before is None else "The file changed.")]
    earlier = definition_lines(before, path)
    if earlier is None:
        return [("", "The file changed.")]
    found = [
        (item.name, item.what_changed.capitalize() + ".")
        for item in changed_definitions(before, text, path)
    ]
    new = sorted(name for name in now if name not in earlier)
    found += [
        (name, "New.") for name in new if not any(name.startswith(other + ".") for other in new)
    ]
    return found or [("", "The file changed outside its functions and classes.")]


def per_file(graph: HonestyGraph) -> HonestyGraph:
    """Reduce a graph to one node per file.

    Args:
        graph: The graph by unit.

    Returns:
        One node per file with the strongest state of its units, the number of units, and the
        marks and recheck entries of all of them; one edge per pair of files.
    """
    files: dict[str, GraphNode] = {}
    for node in graph.nodes:
        old = files.get(node.path)
        if old is None:
            files[node.path] = replace(
                node,
                id=node.path,
                line=1,
                name=node.path.rsplit("/", 1)[-1],
                kind="file",
                detail="",
            )
            continue
        stronger = ORDER.index(node.state) < ORDER.index(old.state)
        files[node.path] = replace(
            old,
            state=node.state if stronger else old.state,
            reviewing=old.reviewing or node.reviewing,
            mark_ids=old.mark_ids + node.mark_ids,
            recheck_ids=old.recheck_ids + node.recheck_ids,
            count=old.count + node.count,
        )
    path_of: Mapping[str, str] = {node.id: node.path for node in graph.nodes}
    pairs: dict[tuple[str, str], bool] = {}
    for edge in graph.edges:
        key = (path_of[edge.source], path_of[edge.target])
        if key[0] != key[1]:
            pairs[key] = pairs.get(key, False) or edge.certain
    return replace(
        graph,
        nodes=tuple(files[path] for path in sorted(files)),
        edges=tuple(
            GraphEdge(source, target, certain)
            for (source, target), certain in sorted(pairs.items())
        ),
        by_file=True,
    )


def read_text(root: Path, path: str) -> str | None:
    """Read one file of the project as text.

    Args:
        root: The project directory.
        path: The file, relative to it.

    Returns:
        The text; None for a file that is missing, larger than `MAX_FILE_BYTES`, not a
        regular file or not readable.
    """
    target = root / path
    try:
        if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            return None
        return target.read_text("utf-8", errors="replace")
    except OSError:
        return None
