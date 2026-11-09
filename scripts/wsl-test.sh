#!/usr/bin/env bash
# Run the checks in WSL with the virtual environment outside the Windows checkout:
#   wsl.exe bash scripts/wsl-test.sh [pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/calu-venv"
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest "$@"
