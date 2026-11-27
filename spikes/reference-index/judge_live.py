"""Measure the reference judge with a real model on the spike's 50 sample units.

Usage, from the repository root, with ANTHROPIC_API_KEY set and a language server on the path:

    uv run --with basedpyright==1.40.2 python spikes/reference-index/judge_live.py [MODEL] [N]

MODEL defaults to anthropic:claude-haiku-5-5. N limits the run to the first N sample units.

What it does, per unit:

1. The word search lists the candidate places (Python files only, with the method rule).
2. The language server labels each place: a reference of the unit, another definition, or
   unknown. That label is the truth the judge is compared with.
3. The judge alone answers yes, no or unsure for every candidate.

It prints the judge's answers against the truth, the wrong answers with their reasons, and
the time. It makes model calls and reads the project's files; it writes nothing.
"""

import ast
import json
import sys
import time
from collections import Counter
from pathlib import Path

import anyio

from calu.models import resolve_model
from calu.refindex import SearchIndex, Unit
from calu.refindex.judge import MatchJudge
from calu.refindex.lsp import lsp_index
from calu.refindex.model import Resolution

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def line_now(sample: dict) -> int:
    """Find the line of a sample unit's definition in the checkout as it is now.

    Args:
        sample: One entry of `sample.json`.

    Returns:
        The 1-based line; 0 if the unit is gone.
    """

    def walk(node: ast.AST, prefix: str) -> int:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if prefix + child.name == sample["qualname"]:
                    return child.lineno
            if isinstance(child, ast.ClassDef):
                found = walk(child, prefix + child.name + ".")
                if found:
                    return found
        return 0

    return walk(ast.parse((ROOT / sample["path"]).read_text()), "")


async def main() -> None:
    """Run the measurement and print its result."""
    model = sys.argv[1] if len(sys.argv) > 1 else "anthropic:claude-haiku-5-5"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    samples = json.loads((HERE / "sample.json").read_text())[:count]
    search = SearchIndex(ROOT)
    judge = MatchJudge(ROOT, resolve_model(model))
    table: Counter[tuple[str, str]] = Counter()
    wrong: list[str] = []
    judge_seconds = 0.0
    slowest = (0.0, "", 0)
    with lsp_index(ROOT) as server:
        for sample in samples:
            unit = Unit(sample["path"], sample["qualname"], line_now(sample))
            places = list(search.find(unit).unclear)
            known = {(p.path, p.line) for p in server.find(unit).referrers}
            truth = {}
            for place in places:
                if (place.path, place.line) in known:
                    truth[place] = "yes"
                else:
                    answer = server.resolve_blocking(place, unit)
                    truth[place] = {
                        Resolution.UNIT: "yes",
                        Resolution.OTHER: "no",
                        Resolution.UNKNOWN: "unknown",
                    }[answer]
            started = time.monotonic()
            answers = await judge.judge(unit, places)
            took = time.monotonic() - started
            judge_seconds += took
            if took > slowest[0]:
                slowest = (took, unit.qualname, len(places))
            for place, answer in answers.items():
                table[truth[place], answer] += 1
                if truth[place] in ("yes", "no") and answer in ("yes", "no"):
                    if truth[place] != answer:
                        wrong.append(
                            f"  truth {truth[place]:3} judge {answer:3} {unit.qualname}: "
                            f"{place.path}:{place.line}  {place.text[:90]}"
                        )
            print(f"{sample['id']} {unit.qualname}: {len(places)} places, {took:.1f} s", flush=True)
    print(f"\nmodel {model}, {len(samples)} units, {sum(table.values())} places")
    print("truth (language server) x judge:")
    for truth_value in ("yes", "no", "unknown"):
        row = {a: table[truth_value, a] for a in ("yes", "no", "unsure")}
        print(f"  {truth_value:8} {row}")
    print(
        f"judge time: {judge_seconds:.0f} s in all; slowest unit {slowest[1]} "
        f"({slowest[2]} places) {slowest[0]:.1f} s"
    )
    print(f"wrong answers ({len(wrong)}):")
    print("\n".join(wrong[:60]))


anyio.run(main)
