#!/usr/bin/env bash
# Runs inside the container of docker/vscode-test.Dockerfile (see test-in-docker.sh), with the
# repository mounted read-only at /repo.
#
#   1. copy the extension's sources to /work/ext (without node_modules, dist, out, *.vsix)
#   2. npm ci, the unit tests
#   3. calu from /repo into /opt/calu-venv (the image holds its dependencies)
#   4. the integration tests under xvfb, calu ide and the scripted session from that venv
set -euo pipefail

src=/repo/extensions/vscode
cd /work/ext
# Keep the cached VSCode download (.vscode-test is a volume), start with fresh profiles.
find . -mindepth 1 -maxdepth 1 ! -name .vscode-test -exec rm -rf {} +
rm -rf .vscode-test/user-data .vscode-test/extensions
tar -C "$src" \
    --exclude=./node_modules --exclude=./dist --exclude=./out --exclude=./.vscode-test \
    --exclude='./*.vsix' -cf - . | tar -xf -

npm ci --no-audit --no-fund
npm run test:unit

# calu itself: a copy of the sources (the build must not write into the read-only mount).
mkdir -p /tmp/calu-src
tar -C /repo --exclude=./.git --exclude=./.venv --exclude=./extensions --exclude=./node_modules \
    -cf - pyproject.toml uv.lock README.md LICENSE LICENSE.md src | tar -C /tmp/calu-src -xf -
uv sync --project /tmp/calu-src --frozen --no-dev
export CALU_TEST_CALU='["/opt/calu-venv/bin/calu"]'
export CALU_TEST_PYTHON='["/opt/calu-venv/bin/python"]'

xvfb-run -a --server-args="-screen 0 1280x1024x24" npm run test:integration
