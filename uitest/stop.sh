#!/usr/bin/env bash
# Stop the UI-testing desktop container `calu-uitest`.
#
# Usage: calu/uitest/stop.sh [--remove]
#
# Without arguments the container is stopped and kept: run.sh starts it again
# with the same home directory (VSCode profile, PyCharm settings, installed
# extensions). With --remove the container is deleted, which discards
# everything inside it except /share (~/.cache/calu-uitest/share on the host).
# This script touches no container other than `calu-uitest`.
set -euo pipefail
NAME=calu-uitest
if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "$NAME does not exist"
    exit 0
fi
docker stop "$NAME" >/dev/null
echo "$NAME stopped"
if [ "${1:-}" = "--remove" ]; then
    docker rm "$NAME" >/dev/null
    echo "$NAME removed"
fi
