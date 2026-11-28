"""Count evidence files that calu's stage-1 redaction would change (known secret values from
calu's env file, and key shapes). Prints file names and counts only, never a value.

Usage: python worktrees_leakcheck.py DIRECTORY. Every file below the directory is read and
counted, except the files in a directory named `probe`: they are neither read nor counted, so
the total leaves them out."""

import pathlib
import sys

from calu.config import load_secrets
from calu.traces.redact import Secrets, redact_shapes

names = load_secrets()
secrets = Secrets.from_environment(names)
root = pathlib.Path(sys.argv[1])
total = known = shapes = 0
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.parent.name == "probe":
        continue
    total += 1
    text = path.read_text(encoding="utf-8", errors="replace")
    if secrets.redact(text) != text:
        known += 1
        print("KNOWN SECRET VALUE IN", path.relative_to(root))
    elif redact_shapes(text) != text:
        shapes += 1
        print("key shape in", path.relative_to(root))
print(f"{total} files, {known} with a known secret value, {shapes} with a key shape")
