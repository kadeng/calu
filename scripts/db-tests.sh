#!/usr/bin/env bash
# Run the database layer (`-m 'db and not live and not browser'`) from the repository root.
#
#     scripts/db-tests.sh [pytest arguments]       # e.g. scripts/db-tests.sh -q tests/db/test_x.py
#
# The script exists so that a sandboxed Calu session on Linux can run the database layer: Claude
# Code's Linux sandbox blocks TCP and every Unix socket, so a sandboxed command cannot reach the
# test server. One fixed command name can be listed in `claude.sandbox.excluded_commands`, and
# Claude Code then runs this command outside the sandbox ("Developing Calu inside Calu" in
# DOCUMENTATION.md). Calu's checkers still judge every call of it.
#
# The test server comes from CALU_TEST_DSN (default: the `caludb` service of docker-compose.yml).
set -euo pipefail

cd "$(dirname "$0")/.."
exec uv run pytest -m 'db and not live and not browser' "$@"
