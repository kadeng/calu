#!/bin/sh
# Runs spikes/spike_other_uid.py as root in a container (D206): the checkout and its virtual
# environment are mounted read-only at their own paths, so the environment's interpreter works.
#
# Usage: spikes/spike_other_uid.sh MODE WORKSPACE
#   MODE       sdk-user, sdk-user-str, wrapper or launcher
#   WORKSPACE  an empty directory owned by uid 1000; the container mounts it as /work
# Needs docker, the image postgres:18 (any glibc image with setpriv), ANTHROPIC_API_KEY and
# optionally ANTHROPIC_BASE_URL in the environment.
set -eu
mode=$1
workspace=$(cd "$2" && pwd)
repo=$(cd "$(dirname "$0")/.." && pwd)
python=$(dirname "$(dirname "$(dirname "$(readlink -f "$repo/.venv/bin/python")")")")
exec docker run --rm --network host \
    -e ANTHROPIC_API_KEY -e ANTHROPIC_BASE_URL \
    -v "$repo:$repo:ro" -v "$python:$python:ro" -v "$workspace:/work" \
    postgres:18 "$repo/.venv/bin/python" "$repo/spikes/spike_other_uid.py" "$mode" /work
