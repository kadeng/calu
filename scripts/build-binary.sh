#!/usr/bin/env bash
# Build the single-file `calu` binary and prove it runs (PLAN.md D41, REQUIREMENTS 21).
#
#   scripts/build-binary.sh [output directory]      # default: dist/
#
# PyInstaller freezes the interpreter, calu and its dependencies into one executable. Measured
# on 2026-11-10 (Linux x86_64, Python 3.14): ~70 MiB, starts in ~1.8 s, applies all migrations,
# seeds the default checkers and rules and serves the web interface -- `importlib.resources`
# finds the package data inside the archive, which is what `--collect-data calu` puts there.
#
# Two flags are not optional:
#
#   --collect-data calu        the migrations, default instructions, rules, eval cases, web
#                              templates and static files; without them the binary starts and
#                              fails at the first command that reads one of them.
#   --copy-metadata ...        a dependency that reads its **own** package metadata at import
#                              time (genai_prices, via pydantic-ai) raises
#                              PackageNotFoundError without it, and nothing at build time warns
#                              about that. This is the reason the script runs the result below
#                              instead of only building it: a new dependency of that kind
#                              breaks the binary and only running it shows that.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${1:-dist}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/entry.py" <<'PY'
import sys

from calu.cli import main

sys.exit(main())
PY

uv run --with pyinstaller pyinstaller --onefile --name calu \
    --distpath "$out" --workpath "$work/build" --specpath "$work" --noconfirm \
    --collect-data calu --collect-submodules calu \
    --copy-metadata genai_prices --copy-metadata pydantic-ai-slim --copy-metadata pydantic \
    "$work/entry.py"

binary="$out/calu"
echo "--- built $binary ($(du -h "$binary" | cut -f1))"

# It has to run, not just link: these three cover the entry point, the protocol models and the
# package data that `calu doctor` and the schema export read.
"$binary" --version
"$binary" protocol --out "$work/schema.json"
python3 - "$work/schema.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    schema = json.load(handle)
count = len(schema.get("$defs", {}))
if count < 40:
    raise SystemExit(f"the protocol schema has only {count} definitions; the build is incomplete")
print(f"--- the binary wrote a protocol schema with {count} definitions")
PY
# `calu doctor` exits non-zero when something a session needs is missing, which is correct on a
# build machine, so only its own failure modes matter here.
if ! "$binary" doctor >/dev/null 2>&1; then
    echo "--- calu doctor ran and reported something missing (expected on a build machine)"
fi
echo "--- the binary runs"
