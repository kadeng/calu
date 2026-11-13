#!/bin/sh
# Manual check of "calu as root, Claude Code as another user" (D206) with the real Claude Code:
# runs spikes/smoke_session.py --user 1000 as root in the development image.
#
# Usage: spikes/smoke_privileged.sh WORKSPACE
#   WORKSPACE  an empty directory owned by uid 1000; the container mounts it as /work
# Needs docker, the image calu-dev-dev (docker compose build dev), the test database
# (docker compose up -d caludb --wait), ANTHROPIC_API_KEY and optionally ANTHROPIC_BASE_URL.
#
# What it sets up in the project, as uid 1000: a git repository whose clean filter records
# which user git runs it as (.git/spy), and a symbolic link `leak` to a file only root may
# read. It prints: what a command of Claude Code ran as, what Claude could read through the
# link, which user git's filter ran as, and who owns what calu and Claude Code wrote.
set -eu
workspace=$(cd "$1" && pwd)
repo=$(cd "$(dirname "$0")/.." && pwd)
python=$(dirname "$(dirname "$(dirname "$(readlink -f "$repo/.venv/bin/python")")")")
exec docker run --rm --network host \
    -e ANTHROPIC_API_KEY -e ANTHROPIC_BASE_URL \
    -v "$repo:$repo:ro" -v "$python:$python:ro" -v "$workspace:/work" \
    calu-dev-dev:latest sh -c '
set -eu
echo "only root reads this" > /etc/root-only; chmod 600 /etc/root-only
as_user() { setpriv --reuid 1000 --regid 1000 --clear-groups env HOME=/tmp/setup-home "$@"; }
mkdir -p /tmp/setup-home; chown 1000:1000 /tmp/setup-home
cd /work
as_user git init -q .
as_user git config user.email smoke@example.invalid; as_user git config user.name smoke
as_user git config filter.spy.clean "sh -c \"id >> /work/.git/spy; cat\""
as_user sh -c "echo \"* filter=spy\" > .gitattributes; echo hello > a.txt; ln -sf /etc/root-only leak"
as_user git add .gitattributes a.txt; as_user git commit -q -m first
rm -f /work/.git/spy
"'"$repo"'/.venv/bin/python" "'"$repo"'/spikes/smoke_session.py" /work --user 1000 --changes \
    --honesty track --timeout 200 -p "Do three things. 1: run the command: id > who.txt 2: append the word more to a.txt with the Edit tool. 3: read the file named leak with the Read tool. Then answer with what reading leak gave you." 2>&1 | tail -45
echo "=== who.txt: $(cat who.txt 2>&1)"
echo "=== owners in the project:"; ls -ln /work | tail -n +2
echo "=== git filter ran as: $(sort -u /work/.git/spy 2>&1)"
echo "=== below /var/lib/calu:"; find /var/lib/calu -maxdepth 3 -printf "%u:%g %m %p\n"
'
