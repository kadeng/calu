"""The honesty graph: the part of the honesty state that a client draws.

The chat pane can show the honesty state as a graph
(`agent/records/notes/honesty-graph-design.md`). This module turns the state into that graph.
It draws nothing; layout and drawing are the client's.

A node is a unit of code: a function, a method or a class, or a whole file where no unit can
be named. The units that are not clean are in the graph:

- `dishonest`: a checker or a review found it dishonest. It has an open mark.
- `changed`: its file changed and nobody verified the file. These are the definitions of
  an unreviewed file that differ from the file's earlier text.
- `recheck`: it uses a unit whose signature or doc comment changed
  (`HonestyState.recheck`).

Next to them the graph shows clean units, so that a reader sees where the units above sit
in the code. `add_neighbours` finds them for the view only; they block nothing:

- `caller`: it uses a dishonest unit and is itself unchanged.
- `related`: it uses a changed unit, or a changed or a dishonest unit uses it, and it is
  itself unchanged.
- `honest`: the unit whose changed contract caused a `recheck`. It is shown so that the
  edge has an end.

An edge goes from a unit to a unit it uses.

If a unit has several states, the first of this order wins: dishonest, recheck, changed,
caller, honest, related.

Units are named per definition only in Python files. Any other file is one node.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import anyio

from calu.callees import Callees
from calu.checkers.symbols import changed_definitions, definition_lines
from calu.dependents import ParsedFiles, enclosing_units
from calu.honesty import HonestySnapshot
from calu.refindex import ReferenceIndex, Unit

NodeState = Literal["dishonest", "recheck", "changed", "caller", "honest", "related"]
"""The states of a node, strongest first (`ORDER`)."""

ORDER: tuple[NodeState, ...] = (
    "dishonest",
    "recheck",
    "changed",
    "caller",
    "honest",
    "related",
)
"""Which state wins when a unit has several: the earlier one."""

MAX_NODES = 150
"""The most nodes of a graph by unit. `build_graph` reduces a larger graph to one node per
file (`by_file`); `add_neighbours` stops adding clean units at this number."""

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
    hidden_callers: int = 0
    """How many clean callers of the unit the graph leaves out (`MAX_CALLERS`)."""


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
    partial: bool = False
    """True if `add_neighbours` left callers or used units out: its time ran out, or the
    graph reached `MAX_NODES`."""


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

    def id_of(self, path: str, symbol: str) -> str:
        """Return the id of the node that stands for a unit.

        Args:
            path: The file, relative to the project directory.
            symbol: The dotted name in the file; "" for the whole file.

        Returns:
            `path:symbol` if the file defines the symbol, else the path: the whole file.
        """
        known = self.lines(path)
        return f"{path}:{symbol}" if symbol and known is not None and symbol in known else path

    def beside(self, path: str, symbol: str, state: NodeState, detail: str) -> str | None:
        """Add a clean unit that stands next to one that is not clean, if there is room.

        A unit that is in the graph already keeps its place and its stronger state.

        Args:
            path: The file, relative to the project directory.
            symbol: The dotted name in the file; "" for the whole file.
            state: `caller` or `related`.
            detail: The text for the hover.

        Returns:
            The node's id; None if the unit is not in the graph and the graph has
            `MAX_NODES` nodes already.
        """
        if self.id_of(path, symbol) not in self.nodes and len(self.nodes) >= MAX_NODES:
            return None
        return self.add(path, symbol, state, detail)

    def link(self, source: str, target: str, certain: bool) -> None:
        """Add a "uses" edge between two nodes, once.

        An edge from a node to itself is left out. A certain edge replaces an uncertain one
        between the same nodes, and an uncertain one does not replace a certain one.

        Args:
            source: The id of the node that uses.
            target: The id of the node that is used.
            certain: False if a reference index only suspects the link.
        """
        if source == target:
            return
        edge = GraphEdge(source, target, certain)
        if certain:
            self.edges.discard(replace(edge, certain=False))
            self.edges.add(edge)
        elif replace(edge, certain=True) not in self.edges:
            self.edges.add(edge)

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

    Side effects: reads the files of the graph's units: the unreviewed files, the marked files,
    the files with units to judge again, and the files of the units whose change caused that.

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


MAX_KEPT_FILES = 20_000
"""The most files a `NeighbourMemo` keeps; at this number it forgets all of them."""

type FileState = tuple[int, int] | None
"""A file's modification time in nanoseconds and its size; None for a file that is missing
or cannot be asked. Equal means "not changed"."""


def file_state(root: Path, path: str) -> FileState:
    """Ask the file system for a file's state.

    Args:
        root: The project directory.
        path: The file, relative to it.

    Returns:
        (modification time in nanoseconds, size); None for a missing file or one that cannot
        be asked.
    """
    try:
        stat = (root / path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@dataclass
class NeighbourMemo:
    """What `add_neighbours` keeps between requests, so that a repeated request is cheap.

    It keeps, per file, what was parsed of it: the unit around each line (`parsed`) and the
    definitions and imports `calu.callees` read (`callees`). Each file is kept with its state
    as it was before it was read. `begin` forgets every file whose state changed since. A
    file changed without a new modification time or size is not noticed; the memo serves a
    view only.
    """

    root: Path
    """The project directory."""
    states: dict[str, FileState] = field(default_factory=dict[str, FileState])
    """Per kept file its state before it was read."""
    parsed: ParsedFiles = field(default_factory=dict[str, tuple[list[str], list[tuple[int, int]]]])
    """The kept results of `enclosing_units`."""
    callees: Callees = field(init=False)
    """The kept reads of `calu.callees`."""

    def __post_init__(self) -> None:
        """Create the callee lookup, which reads through `read`."""
        self.callees = Callees(self.read)

    def begin(self) -> None:
        """Start a request: forget the files that changed since they were read.

        Side effects: asks the file system for the state of every kept file. With
        `MAX_KEPT_FILES` files kept, forgets all of them.
        """
        if len(self.states) >= MAX_KEPT_FILES:
            self.states.clear()
            self.parsed.clear()
            self.callees = Callees(self.read)
            return
        changed = [
            path for path, state in self.states.items() if file_state(self.root, path) != state
        ]
        for path in changed:
            del self.states[path]
            self.parsed.pop(path, None)
        self.callees.forget(changed)

    def note(self, paths: Iterable[str]) -> None:
        """Record the state of files that are about to be read, if they are not kept yet.

        Args:
            paths: The files, relative to the project directory.
        """
        for path in paths:
            if path not in self.states:
                self.states[path] = file_state(self.root, path)

    def read(self, path: str) -> str | None:
        """Read a file for `calu.callees`, after recording its state.

        Args:
            path: The file, relative to the project directory.

        Returns:
            What `read_text` returns.
        """
        self.note((path,))
        return read_text(self.root, path)


NEIGHBOURS_BUDGET_S = 20.0
"""How long `add_neighbours` asks the reference index for callers before it stops."""

MAX_CALLERS = 12
"""The most clean callers `add_neighbours` shows for one unit."""

CLEAN_STATES: tuple[NodeState, ...] = ("caller", "honest", "related")
"""The states of units that are in the graph only for the picture."""


def _units_around(
    root: Path,
    places: Sequence[tuple[str, int, bool]],
    parsed: ParsedFiles,
    memo: NeighbourMemo | None = None,
) -> list[tuple[str, str, bool]]:
    """Name the unit around each place a reference index reported.

    Side effects: reads and parses the files of the places that `parsed` does not hold, and
    adds them to it. With a `memo`, records the state of those files there first.

    Args:
        root: The project directory.
        places: (path, 1-based line, whether the index is certain) per place.
        parsed: The files parsed so far; the memo's `parsed` when a memo is given.
        memo: What is kept between requests, or None.

    Returns:
        (path, dotted name of the unit, certain) per place; "" as the name for the top level
        of a file. A place inside an `import` statement is left out.
    """
    lines = [(path, line) for path, line, _certain in places]
    if memo is not None:
        memo.note(path for path, _line in lines if path not in parsed)
    units = enclosing_units(root, lines, parsed)
    return [
        (path, unit, certain)
        for (path, _line, certain), unit in zip(places, units, strict=True)
        if unit is not None
    ]


async def add_neighbours(
    graph: HonestyGraph,
    root: Path,
    index: ReferenceIndex,
    budget_s: float = NEIGHBOURS_BUDGET_S,
    memo: NeighbourMemo | None = None,
) -> HonestyGraph:
    """Add the clean units around the dishonest and the changed units, with "uses" edges.

    This is for the view only: what it adds blocks nothing. For every dishonest and every
    changed unit (not for a whole file) it adds:

    - the units it uses, read from its own syntax (`calu.callees`), as `related`;
    - the units that use it, from the reference index: as `caller` for a dishonest unit,
      as `related` for a changed one.

    A unit that is in the graph already keeps its state. It only gets the edge and, in its
    hover text, a line that names the unit it is used by or uses. So two
    changed units of which one uses the other are connected, too.

    The index should be a cheap one (the word search, a code navigator), because the view
    must cost no model call.

    At most `MAX_CALLERS` clean callers are shown per unit (the owner, 2026-11-27). A caller
    that is itself dishonest, changed or to judge again is always connected. The unit's
    `hidden_callers` says how many clean callers were left out.

    The function has two more limits. Each one sets `partial` on the result:

    - The index is asked for at most `budget_s` seconds in total, dishonest units first. The
      units after that get no callers.
    - No clean unit is added once the graph has `MAX_NODES` nodes.

    Side effects: those of the index; reads the files of the units, of what they import and
    of the places the index reports, in worker threads. With a `memo`, a file the memo
    holds is not read again, and what is read is added to it. The caller calls
    `memo.begin` first, so that changed files are read again.

    Args:
        graph: The graph of `build_graph`. A graph by file is returned unchanged.
        root: The project directory.
        index: The reference index to ask about each unit.
        budget_s: The seconds the index may take in total.
        memo: What is kept between requests; None reads every file once in this call.

    Returns:
        The graph with the added units and edges. A place the index only suspects gives an
        edge that is not `certain`; an edge to a used unit is certain.
    """
    if graph.by_file:
        return graph
    builder = _Builder(root, nodes={node.id: node for node in graph.nodes}, edges=set(graph.edges))
    subjects = sorted(
        (
            node
            for node in graph.nodes
            if node.kind == "unit" and node.state in ("dishonest", "changed")
        ),
        key=lambda node: (ORDER.index(node.state), node.id),
    )

    def add_used() -> bool:
        """Add what each subject uses.

        Returns:
            True if a used unit was left out because the graph is full.
        """
        callees = memo.callees if memo is not None else Callees(lambda p: read_text(root, p))
        full = False
        for node in subjects:
            for used in callees.of(node.path, node.id.partition(":")[2]):
                target = builder.beside(used.path, used.symbol, "related", f"Used by {node.name}.")
                if target is None:
                    full = True
                else:
                    builder.link(node.id, target, True)
        return full

    partial = await anyio.to_thread.run_sync(add_used)
    parsed: ParsedFiles = memo.parsed if memo is not None else {}
    deadline = anyio.current_time() + budget_s
    for node in subjects:
        left = deadline - anyio.current_time()
        if left <= 0:
            partial = True
            break
        found = None
        with anyio.move_on_after(left):
            found = await index.referrers(Unit(node.path, node.id.partition(":")[2], node.line))
        if found is None:
            partial = True
            break
        places = [
            (place.path, place.line, certain)
            for group, certain in ((found.referrers, True), (found.unclear, False))
            for place in group
        ]
        state: NodeState = "caller" if node.state == "dishonest" else "related"
        shown: set[str] = set()
        hidden: set[str] = set()
        around = await anyio.to_thread.run_sync(_units_around, root, places, parsed, memo)
        for path, unit, certain in around:
            caller = builder.id_of(path, unit)
            known = builder.nodes.get(caller)
            if known is None or known.state in CLEAN_STATES:
                # A clean caller is shown for the picture only, so their number is limited.
                if caller not in shown and len(shown) >= MAX_CALLERS:
                    if caller != node.id:
                        hidden.add(caller)
                    continue
                if caller != node.id:
                    shown.add(caller)
            source = builder.beside(path, unit, state, f"Uses {node.name}.")
            if source is None:
                partial = True
            else:
                builder.link(source, node.id, certain)
        if hidden:
            builder.nodes[node.id] = replace(builder.nodes[node.id], hidden_callers=len(hidden))
    return replace(
        graph,
        nodes=tuple(
            sorted(builder.nodes.values(), key=lambda node: (node.path, node.line, node.id))
        ),
        edges=tuple(sorted(builder.edges, key=lambda edge: (edge.source, edge.target))),
        partial=partial,
    )


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
