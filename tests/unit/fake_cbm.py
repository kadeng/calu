"""A scripted stand-in for the command line of codebase-memory-mcp, for `calu.refindex.cbm`.

Called as `fake_cbm.py SCRIPT.json cli --quiet TOOL` with JSON arguments on stdin. It appends
the tool's name to `SCRIPT.json.calls` and answers from the script:

    {"nodes": ["proj.store.Store.save"],
     "edges": {"proj.store.Store.save": [["CALLS", "typed.py", "1", "2", "lsp_method"], ...]}}

It reads no project file.
"""

import json
import re
import sys
from pathlib import Path


def main() -> None:
    """Answer one tool call from the script and record it."""
    script = Path(sys.argv[1])
    plan = json.loads(script.read_text("utf-8"))
    tool = sys.argv[-1]
    arguments = json.loads(sys.stdin.read())
    with Path(f"{script}.calls").open("a", encoding="utf-8") as calls:
        calls.write(tool + "\n")
    if tool == "index_repository":
        print(json.dumps({"status": "indexed"}))
        return
    name = re.search(r'qualified_name = "([^"]+)"', arguments["query"])
    wanted = name.group(1) if name else ""
    if "RETURN type(r)" in arguments["query"]:
        rows = plan.get("edges", {}).get(wanted, [])
    else:
        rows = [[wanted]] if wanted in plan.get("nodes", []) else []
    print(json.dumps({"rows": rows}))


if __name__ == "__main__":
    main()
