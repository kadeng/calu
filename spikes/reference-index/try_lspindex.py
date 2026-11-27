"""Compare `calu.refindex.lsp.lsp_index` with reference answers for the spike's sample.

Usage, from the repository root, with a language server on the path:

    uv run --with basedpyright==1.40.2 python spikes/reference-index/try_lspindex.py REFS.json

REFS.json maps each sample id to a list of {"file": ...} entries (the spike's
`refs-with-bases.json`). Prints how many reference files the index found.
"""

import ast
import json
import sys
import time
from pathlib import Path

from calu.refindex import Unit
from calu.refindex.lsp import lsp_index

here = Path(__file__).resolve().parent
root = here.parent.parent
sample = json.load(open(here / "sample.json"))
ref = json.load(open(sys.argv[1]))


def line_now(s):
    # the checkout moved since the sample was taken: find the definition again by qualified name
    tree = ast.parse((root / s["path"]).read_text())

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

    return walk(tree, "")


t0 = time.time()
tot = found = extra = 0
bad = []
incomplete = 0
with lsp_index(root) as index:
    for s in sample:
        a = index.find(Unit(s["path"], s["qualname"], line_now(s) or 0))
        if s["id"] == "S01":
            print("first answer after", round(time.time() - t0, 1), "s")
        if not a.complete:
            incomplete += 1
            print(s["id"], "INCOMPLETE", a.problem)
        R = {e["file"] for e in ref[s["id"]] or []}
        T = {p.path for p in a.referrers}
        tot += len(R)
        found += len(R & T)
        extra += len(T - R)
        if R - T or T - R:
            bad.append((s["id"], s["qualname"], sorted(R - T), sorted(T - R)))
print(
    "samples",
    len(sample),
    "reference files",
    tot,
    "found",
    found,
    "extra",
    extra,
    "incomplete",
    incomplete,
    "seconds",
    round(time.time() - t0, 1),
)
for b in bad:
    print(" ", b)
