#!/usr/bin/env bash
# Start the UI-testing desktop container `calu-uitest` (idempotent).
#
# Usage: calu/uitest/run.sh [--rebuild]
#
# What it does:
#   1. Builds the image `calu-uitest:latest` from this directory when it is
#      missing (or always with --rebuild).
#   2. Creates ~/.cache/calu-uitest/share (exchange directory) and
#      ~/.cache/calu-uitest/token (random access token for cua-spacesd, 0600).
#   3. Reuses a running container, starts a stopped one, or creates a new one.
#   4. Waits until cua-spacesd answers on http://127.0.0.1:$PORT/health.
#
# The container gets exactly two host mounts: the calu repository read-only at
# /workspace/calu and the exchange directory at /share. It runs without
# --privileged and without the Docker socket. The only published port is
# 127.0.0.1:$PORT -> 3211 (cua-spacesd: gRPC, HTTP viewer, MCP).
#
# `--security-opt seccomp=unconfined` lets bubblewrap create user namespaces, so that
# Claude Code's sandbox runs inside the desktop (Docker's default seccomp profile blocks
# them; agent/records/PLAN.md D134). It applies to a newly created container only; README.md
# ("Claude Code's sandbox in the desktop") says how to move an existing one over.
#
# Environment:
#   CALU_UITEST_PORT    host port for cua-spacesd (default 56211)
#   CALU_UITEST_MEMORY  memory limit of the container (default 8g)
#   CALU_UITEST_IMAGE   image of a newly created container (default calu-uitest:latest;
#                       e.g. an image made with `docker commit` from the old container)
set -euo pipefail

NAME=calu-uitest
IMAGE="${CALU_UITEST_IMAGE:-calu-uitest:latest}"
PORT="${CALU_UITEST_PORT:-56211}"
MEMORY="${CALU_UITEST_MEMORY:-8g}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STATE="$HOME/.cache/calu-uitest"
SHARE="$STATE/share"
TOKEN_FILE="$STATE/token"

mkdir -p "$SHARE"
if [ ! -s "$TOKEN_FILE" ]; then
    (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$TOKEN_FILE")
fi

if [ "${1:-}" = "--rebuild" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t calu-uitest:latest "$HERE"
fi

# `docker container inspect` prints an empty line for a missing container.
state="$(docker container inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || true)"
state="${state:-missing}"
case "$state" in
    running)
        echo "$NAME is already running" ;;
    missing)
        docker run -d --name "$NAME" --hostname "$NAME" \
            --shm-size=1g --memory="$MEMORY" \
            --security-opt seccomp=unconfined \
            -e CUA_ENV_TOKEN="$(cat "$TOKEN_FILE")" \
            -e DO_NOT_TRACK=1 -e CUA_TELEMETRY=0 \
            -p "127.0.0.1:$PORT:3211" \
            -v "$REPO:/workspace/calu:ro" \
            -v "$SHARE:/share" \
            "$IMAGE" >/dev/null
        echo "$NAME created" ;;
    *)
        docker start "$NAME" >/dev/null
        echo "$NAME started (was $state)" ;;
esac

for _ in $(seq 1 120); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; then
        echo "cua-spacesd is ready: http://127.0.0.1:$PORT (token: $TOKEN_FILE)"
        echo "viewer link: uv run --no-project --with cua python $HERE/desktop.py viewer-url"
        exit 0
    fi
    sleep 0.5
done
echo "cua-spacesd did not answer on 127.0.0.1:$PORT within 60 s; see: docker logs $NAME" >&2
exit 1
