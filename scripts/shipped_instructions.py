"""Write `src/calu/defaults/checkers/shipped.json`: the hash of every shipped instruction text.

Run from the repository root after changing a shipped text in `src/calu/defaults/checkers/`:

    uv run python scripts/shipped_instructions.py

It reads every version each editable text ever had in the git history (all branches and
tags), the copies in `legacy/` and `legacy/v2/`, and the text in the working tree, and writes
per text name the list of `[sha256 of the text, sha256 of the text without its test
triggers]`, oldest first, without repeats (`calu.defaults.instructions.text_hash`).
`tests/unit/test_shipped_instructions.py` fails while the present text is not in the file, so
a changed default cannot be committed without it; the earlier versions stay in the file, which
is how calu tells a stored text that is an older shipped one, unchanged, from one a user
edited (agent/records/PLAN.md D248).
"""

import json
import subprocess
import sys
from pathlib import Path

from calu.defaults.instructions import SHIPPED_TEXTS, text_hash, without_test_triggers

ROOT = Path(__file__).resolve().parent.parent
BASE = "src/calu/defaults/checkers"
OUT = ROOT / BASE / "shipped.json"


def git(*args: str) -> str:
    """Run git in the repository root.

    Args:
        *args: The arguments after `git`.

    Returns:
        Its standard output.

    Raises:
        subprocess.CalledProcessError: If git fails.
    """
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def versions(path: str) -> list[str]:
    """Return every committed version of one file, oldest first.

    Args:
        path: The file's path from the repository root.

    Returns:
        The file's texts, one per commit that changed it (deletions left out).
    """
    commits = git("log", "--all", "--reverse", "--format=%H", "--", path).split()
    found = []
    for commit in commits:
        try:
            found.append(git("show", f"{commit}:{path}"))
        except subprocess.CalledProcessError:
            continue  # the commit deleted the file
    return found


def main() -> int:
    """Collect the versions and write the file.

    Returns:
        0.
    """
    table: dict[str, list[list[str]]] = {}
    for name in SHIPPED_TEXTS:
        texts: list[str] = []
        for path in (
            f"{BASE}/legacy/{name}.md",
            f"{BASE}/legacy/v2/{name}.md",
            f"{BASE}/{name}.md",
        ):
            texts += versions(path)
            if (ROOT / path).exists():
                texts.append((ROOT / path).read_text(encoding="utf-8"))
        rows: list[list[str]] = []
        for text in texts:
            row = [text_hash(text), text_hash(without_test_triggers(text))]
            if row not in rows:
                rows.append(row)
        table[name] = rows
    OUT.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: " + ", ".join(f"{k} {len(v)}" for k, v in table.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
