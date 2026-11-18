#!/usr/bin/env bash
# Start the root box `calu-rootbox` (idempotent) and install calu in it from this checkout.
#
# Usage: calu/uitest/rootbox/run.sh [--rebuild] [--no-install]
#
#   1. Builds the image `calu-rootbox:latest` when it is missing (or with --rebuild).
#   2. Reuses a running container, starts a stopped one, or creates a new one. The calu
#      checkout is mounted read-only at /src/calu; nothing else of the host is mounted,
#      no port is published, and the container is not privileged.
#   3. Waits for PostgreSQL inside, then runs `rootbox-install` (calu for root and for ada).
#
# Afterwards:  docker exec -it calu-rootbox bash            (root)
#              docker exec -it -u ada calu-rootbox bash -l  (ada; also bob, carol)
# Stop with `docker stop calu-rootbox`; `docker rm -f calu-rootbox` deletes it with
# everything set up inside.
set -euo pipefail

NAME=calu-rootbox
IMAGE=calu-rootbox:latest
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
rebuild=no install=yes
for arg in "$@"; do
    case "$arg" in
        --rebuild) rebuild=yes ;;
        --no-install) install=no ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$rebuild" = yes ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$HERE"
fi

state="$(docker container inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || true)"
case "${state:-missing}" in
    running) echo "$NAME is already running" ;;
    missing)
        docker run -d --name "$NAME" --hostname "$NAME" --memory=4g \
            -v "$REPO:/src/calu:ro" "$IMAGE" >/dev/null
        echo "$NAME created" ;;
    *)
        docker start "$NAME" >/dev/null
        echo "$NAME started (was $state)" ;;
esac

for _ in $(seq 1 120); do
    if docker exec "$NAME" pg_isready -q -h 127.0.0.1 2>/dev/null; then
        break
    fi
    sleep 0.5
done
docker exec "$NAME" pg_isready -h 127.0.0.1
if [ "$install" = yes ]; then
    docker exec "$NAME" /usr/local/sbin/rootbox-install
fi
