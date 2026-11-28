"""Measure what each fast stage leaves for the judge, on the spike's 50 sample functions.

Usage, from the repository root:

    uv run --with basedpyright==1.40.2 python spikes/reference-index/measure_fast.py lsp
    uv run python spikes/reference-index/measure_fast.py cbm

`cbm` expects a running container `calu-spike-cbm-try` that holds codebase-memory-mcp and
has the repository mounted read-only at the same path as on the host.
Prints, summed over the samples:

- `candidates`: the places the plain search reports as unclear
- `confirmed`: the references the combined index confirms
- `left`: the places still unclear after the fast stage, with `left_py` and `left_other` for
  Python and other files
- `problems`: the samples for which the combined index reports a problem

The number of places the fast stage explained away is not printed. The same counts
(`candidates`, `confirmed`, `left`) are printed per group of samples.
"""

import ast
import json
import sys
import time
from pathlib import Path

import anyio

from calu.refindex import SearchIndex, Unit
from calu.refindex.combined import CombinedIndex

root = Path.cwd()
sample = json.load(open(root / "spikes/reference-index/sample.json"))


def line_now(s):
    """Find the line where a sample function is defined now.

    The file may have changed since the sample was taken, so the stored line can be stale.

    Args:
        s: A sample entry with the keys `path` (relative to the repository root) and
            `qualname` (dotted names of the classes and the function).

    Returns:
        The line number of the definition, or 0 when the file has no such function.
    """

    def walk(node, prefix):
        for n in getattr(node, "body", []):
            if (
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and prefix + n.name == s["qualname"]
            ):
                return n.lineno
            if isinstance(n, ast.ClassDef):
                found = walk(n, prefix + n.name + ".")
                if found:
                    return found

    return walk(ast.parse((root / s["path"]).read_text()), "") or 0


async def main(kind):
    """Measure the plain search and the combined index on every sample and print the sums.

    Starts the fast index, which for `cbm` runs `docker exec` into `calu-spike-cbm-try`, and
    closes it at the end. Prints the sums over all samples and then the sums per group.

    Args:
        kind: `lsp` for the language server index, anything else for the `cbm` container.
    """
    if kind == "lsp":
        from calu.refindex.lsp import lsp_index

        fast = lsp_index(root)
    else:
        from calu.refindex.cbm import CbmIndex

        fast = CbmIndex(
            root,
            command=("docker", "exec", "-i", "calu-spike-cbm-try", "/opt/cbm/codebase-memory-mcp"),
        )
    combined = CombinedIndex(SearchIndex(root), fast)
    plain = SearchIndex(root)
    t0 = time.time()
    tot = dict(candidates=0, confirmed=0, left=0, left_py=0, left_other=0, problems=0)
    per_group = {}
    for s in sample:
        unit = Unit(s["path"], s["qualname"], line_now(s))
        before = await plain.referrers(unit)
        after = await combined.referrers(unit)
        g = per_group.setdefault(s["group"], dict(candidates=0, confirmed=0, left=0))
        for d in (tot, g):
            d["candidates"] += len(before.unclear)
            d["confirmed"] += len(after.referrers)
            d["left"] += len(after.unclear)
        tot["left_py"] += sum(p.path.endswith(".py") for p in after.unclear)
        tot["left_other"] += sum(not p.path.endswith(".py") for p in after.unclear)
        tot["problems"] += bool(after.problem)
    if hasattr(fast, "close"):
        fast.close()
    print(kind, tot, "seconds", round(time.time() - t0))
    print(per_group)


anyio.run(main, sys.argv[1])
