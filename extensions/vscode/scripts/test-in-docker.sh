#!/usr/bin/env bash
# Build the test image and run the extension's unit and integration tests in Docker: a real
# VSCode under xvfb, `calu ide` from the checkout, a scripted session without database, Claude
# Code or keys (test/integration/scripted_session.py).
#
# Usage (Linux/WSL, from anywhere):  extensions/vscode/scripts/test-in-docker.sh
# Environment: VSCODE_TEST_VERSION (default "stable") selects the VSCode version.
# The Docker volumes `calu-vscode-test-cache` (the VSCode download) and `calu-npm-cache` make
# later runs faster. Nothing of the host but the checkout (read-only) is mounted.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image=calu-vscode-test

context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
cp "$repo/pyproject.toml" "$repo/uv.lock" "$context/"
docker build -q -t "$image" -f "$repo/docker/vscode-test.Dockerfile" "$context" >/dev/null

tty_flag=()
[ -t 1 ] && tty_flag=(-t)
docker run --rm "${tty_flag[@]}" \
    -e VSCODE_TEST_VERSION="${VSCODE_TEST_VERSION:-stable}" \
    -v "$repo:/repo:ro" \
    -v calu-vscode-test-cache:/work/ext/.vscode-test \
    -v calu-npm-cache:/home/node/.npm \
    "$image" bash /repo/extensions/vscode/scripts/docker-run-tests.sh
