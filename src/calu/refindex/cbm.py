"""A self-contained fast reference index: the code graph of codebase-memory-mcp.

codebase-memory-mcp is a program that reads a project's source files and builds a graph of
its functions, methods and classes, with an edge wherever one of them calls or uses another.
It works for many languages without any configuration. This module asks that graph which
code refers to a given function, method or class.

How it works:

1. Before the first question, and again after files changed, it tells the program to read
   the whole project again (`cli index_repository`). On calu that takes about 19 seconds.
2. For a question it asks the graph for every edge that points at the unit
   (`cli query_graph`).
3. It reports one place per edge: the line of the call, or else the first line of the
   function that refers to the unit.

Which edges it believes:

- Imports, inheritance, overrides, decorators, and a function passed as a value: all of them.
- Calls: yes, when the program says how it found the target and that way is not a match by
  name. Examples are an import, the type of the receiver, or a function in the same file.
- Calls where the program only matched the name (the ways listed in `UNPROVEN`), or names no
  way at all: none. For a name match the program picks one function of that name, and it
  picks the wrong one about as often as the right one.
- The program's edge kind USAGE: none, for the same reason.

What to expect from it:

- It never says "this place does not refer to the unit". It only names places that do.
- It misses referrers. A call whose target the program cannot work out has no edge at all.
  On calu's code it found about two thirds of the files that a language server found
  (`spikes/RESULTS.md`, "The reference index").
- It is therefore meant as one stage of `calu.refindex.combined`, where the word search
  finds the places this index misses.

What it needs and what it leaves behind:

- The program must already be on the machine; `command` says how to start it. calu does not
  install it and never runs its `install` command, which would change the settings of
  coding agents.
- The program stores its graph in the directory `CBM_CACHE_DIR`. It writes nothing into the
  project.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from calu.refindex.model import PLACE_TEXT_CHARS, Place, Referrers, Unit

CBM = ("codebase-memory-mcp",)
"""The tool's command, as found on the path."""

INDEX_TIMEOUT_S = 600.0
"""Longest one indexing run may take."""

QUERY_TIMEOUT_S = 60.0
"""Longest one query may take."""

EDGES = ("CALLS", "CALL_REFERENCE", "OVERRIDE", "DECORATES", "IMPORTS", "INHERITS")
"""The edge kinds that are references. USAGE is left out (see the module docstring)."""

UNPROVEN = frozenset({"unique_name", "suffix_match", "heuristic"})
"""Call strategies of the tool that match by name alone; such an edge confirms nothing."""


class CbmError(Exception):
    """The tool is missing, failed, or answered with something that is no JSON."""


@dataclass
class CbmIndex:
    """A reference index on the graph of codebase-memory-mcp (see the module docstring)."""

    root: Path
    """The project directory."""
    command: tuple[str, ...] = CBM
    """The tool's command line up to, not including, `cli`."""
    cache_dir: Path | None = None
    """Where the tool keeps its graph (`CBM_CACHE_DIR`); None leaves the tool's own choice."""
    _fresh: bool = field(default=False, init=False, repr=False)
    _one_at_a_time: anyio.Lock = field(default_factory=anyio.Lock, init=False, repr=False)

    @property
    def project(self) -> str:
        """The name the tool gives the project: its path with dashes for the separators.

        Returns:
            E.g. "home-user-projects-shop" for /home/user/projects/shop.
        """
        return str(self.root).strip("/").replace("/", "-")

    def changed(self, paths: list[str]) -> None:
        """Note that files were written; the next question indexes the project again.

        Args:
            paths: The files, unused: the tool reads the whole project.
        """
        if paths:
            self._fresh = False

    async def referrers(self, unit: Unit) -> Referrers:
        """Ask the graph for the places that reference `unit`, in a worker thread.

        Side effects: those of `find`. One question at a time.

        Args:
            unit: The unit.

        Returns:
            As `find`.
        """
        async with self._one_at_a_time:
            return await anyio.to_thread.run_sync(self.find, unit)

    def find(self, unit: Unit) -> Referrers:
        """Ask the graph for the places that reference `unit`; blocks while the tool runs.

        Side effects: runs the tool; indexes the project first if files changed since the
        last index; reads files of the project for the text of each place.

        Args:
            unit: The unit.

        Returns:
            One place for each edge this index believes, and no unclear places. It believes
            every edge of a kind in `EDGES`, except a call edge whose way of finding the
            target is in `UNPROVEN` or not given. If the program is not installed, fails, or
            has no node
            for the unit, the answer is marked as not complete and says why.
        """
        try:
            if not self._fresh:
                self._run("index_repository", {"repo_path": str(self.root)}, INDEX_TIMEOUT_S)
                self._fresh = True
            name = self._node(unit)
            edges = "|".join(EDGES)
            answer = self._run(
                "query_graph",
                {
                    "project": self.project,
                    "format": "json",
                    "max_rows": 5000,
                    "max_output_tokens": 400_000,
                    "query": (
                        f'MATCH (a)-[r:{edges}]->(f) WHERE f.qualified_name = "{name}" '
                        "RETURN type(r), a.file_path, a.start_line, r.line, r.strategy"
                    ),
                },
            )
            if not answer.get("rows") and not self._exists(name):
                return Referrers(complete=False, problem=f"The code graph has no node {name}.")
        except CbmError as exc:
            return Referrers(complete=False, problem=str(exc))
        spots: set[tuple[str, int]] = set()
        for kind, path, start, line, strategy in answer.get("rows", []):
            if kind == "CALLS" and (not strategy or strategy in UNPROVEN):
                continue
            number = _number(line) or _number(start)
            if path and number and (path, number) != (unit.path, unit.line):
                spots.add((str(path), number))
        return Referrers(referrers=tuple(self._place(*spot) for spot in sorted(spots)))

    def _node(self, unit: Unit) -> str:
        """Build the qualified name the tool gives the unit's node.

        Args:
            unit: The unit.

        Returns:
            Project, the file's path without its suffix, and the unit's dotted name, joined
            with dots: "home-user-shop.src.shop.parser.Parser.parse".

        Raises:
            CbmError: If the name holds a quotation mark or a backslash, which the query
                could not carry.
        """
        module = Path(unit.path).with_suffix("").as_posix().replace("/", ".")
        name = f"{self.project}.{module}.{unit.qualname}"
        if '"' in name or "\\" in name:
            raise CbmError(f"The code graph cannot be asked about {unit.qualname!r}.")
        return name

    def _exists(self, name: str) -> bool:
        """Ask whether the graph has a node of that qualified name.

        Args:
            name: The qualified name.

        Returns:
            True if it has.

        Raises:
            CbmError: If the tool fails.
        """
        query = f'MATCH (f) WHERE f.qualified_name = "{name}" RETURN f.qualified_name'
        answer = self._run(
            "query_graph", {"project": self.project, "format": "json", "query": query}
        )
        return bool(answer.get("rows"))

    def _run(self, tool: str, arguments: dict[str, Any], timeout: float = QUERY_TIMEOUT_S) -> Any:
        """Run one tool of the command line with JSON arguments on its input.

        Side effects: starts the tool's program; `index_repository` rewrites its graph.

        Args:
            tool: The tool's name, e.g. "query_graph".
            arguments: Its arguments.
            timeout: Longest wait in seconds.

        Returns:
            The parsed JSON answer.

        Raises:
            CbmError: If the program is not installed, does not end in time, or its output
                is no JSON object.
        """
        if shutil.which(self.command[0]) is None:
            raise CbmError(f"No code graph: {self.command[0]} is not installed.")
        environment = dict(os.environ)
        if self.cache_dir is not None:
            environment["CBM_CACHE_DIR"] = str(self.cache_dir)
        try:
            done = subprocess.run(
                [*self.command, "cli", "--quiet", tool],
                input=json.dumps(arguments).encode(),
                capture_output=True,
                timeout=timeout,
                env=environment,
                check=False,
            )
            answer = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise CbmError(f"The code graph tool failed at {tool} ({type(exc).__name__}).") from exc
        if not isinstance(answer, dict):
            raise CbmError(f"The code graph tool gave no answer to {tool}.")
        return answer

    def _place(self, path: str, line: int) -> Place:
        """Build the place of one edge, with the text of its line.

        Args:
            path: The file, relative to the project directory.
            line: The 1-based line.

        Returns:
            The place, with column 0: the tool names no column. Its text is "" if the file
            or the line cannot be read.
        """
        try:
            lines = (self.root / path).read_text("utf-8", errors="replace").splitlines()
            text = lines[line - 1].strip()[:PLACE_TEXT_CHARS]
        except OSError, IndexError:
            text = ""
        return Place(path, line, 0, text)


def _number(value: object) -> int:
    """Read a line number from a cell of the tool's answer.

    Args:
        value: A number, a string of digits, or nothing.

    Returns:
        The number; 0 if there is none.
    """
    try:
        return int(str(value))
    except ValueError:
        return 0
