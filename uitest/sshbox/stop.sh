#!/usr/bin/env bash
# Stop the SSH box `calu-sshbox`. The container and everything set up inside are kept;
# `run.sh` starts it again (its scripted servers start with it).
#
# Usage: calu/uitest/sshbox/stop.sh [--remove]
#   --remove   also delete the container, and the network `calu-uitest-net` if nothing else
#              is connected to it (the desktop container is disconnected first). The test key
#              in ~/.cache/calu-uitest/ssh is kept.
set -euo pipefail
NAME=calu-sshbox
NET=calu-uitest-net
DESKTOP=calu-uitest
docker stop "$NAME" >/dev/null 2>&1 && echo "$NAME stopped" || echo "$NAME was not running"
if [ "${1:-}" = "--remove" ]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network disconnect "$NET" "$DESKTOP" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    echo "$NAME and $NET removed"
fi
