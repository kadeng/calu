#!/usr/bin/env bash
# Prepare the UI-testing desktop for a run of the VSCode extension against a REAL Claude Code
# and real checker models. Runs INSIDE the container `calu-uitest` as user `cua` (idempotent).
#
# Usage (from the host, in the calu checkout):
#   D="uv run --no-project --with cua python uitest/desktop.py"
#   $D shell 'bash /workspace/calu/uitest/setup-real.sh'              # everything
#   $D shell 'bash /workspace/calu/uitest/setup-real.sh --no-deploy'  # keep the installed calu
#   $D shell 'bash /workspace/calu/uitest/setup-real.sh --stop-server'
#   $D shell 'bash /workspace/calu/uitest/setup-real.sh --remove-key' # delete the key files
#
# The API key: the owner puts a file with the lines `ANTHROPIC_API_KEY=...` and, optionally,
# `ANTHROPIC_BASE_URL=...` at ~/.cache/calu-uitest/share/anthropic.env on the host (mode 600;
# /share/anthropic.env here). This script never prints a value of that file and never puts
# one on a command line. It moves the file to the two places that read it and deletes the
# copy in /share:
#
#   ~/.config/calu/.env        (mode 600)  calu's env file: the checkers, the narrator, the
#                                          summarizer and the trace redactor read it
#   ~/.claude/settings.json    (mode 600)  its `env` block: Claude Code reads it itself
#
# Both are needed. calu blanks every variable of its env file in the environment it hands to
# Claude Code, so a key that is only there never reaches Claude Code; Claude Code's own
# settings are the machine's set-up, which calu neither checks nor changes.
#
# What it does:
#   1. `setup-demo.sh --no-server` (database, calu from the host's working tree, the demo
#      project) and stops the fake server if it runs.
#   2. Installs bubblewrap and socat (apt) and Claude Code (the native installer), no login.
#   3. Puts the key in place (see above).
#   4. Sets every model role to Claude Haiku (`calu config models`), Claude Code's model to
#      `haiku`, and switches Claude Code's sandbox off if this container cannot create user
#      namespaces (Docker without privileges cannot).
#   5. Runs `calu doctor --keys`.
#   6. Starts `calu serve --insecure` in the tmux session `calu-real` (log:
#      /tmp/calu-real-server.log).
#
# Cost: step 5 only lists models. Every session afterwards bills the key.
set -euo pipefail

SHARED_KEY=/share/anthropic.env
CALU_ENV="$HOME/.config/calu/.env"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HAIKU=anthropic:claude-haiku-5-5
TMUX_SESSION=calu-real
LOG=/tmp/calu-real-server.log
export PATH="$HOME/.local/bin:$PATH"

say() { printf '\n== %s\n' "$*"; }

stop_server() {
    # C-c lets the server stop its sessions and mark them stopped in the database.
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux send-keys -t "$TMUX_SESSION" C-c
        for _ in $(seq 1 40); do
            tmux has-session -t "$TMUX_SESSION" 2>/dev/null || return 0
            sleep 0.5
        done
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    fi
}

case "${1:-}" in
--stop-server)
    stop_server
    echo "real server stopped"
    exit 0
    ;;
--remove-key)
    stop_server
    rm -f "$SHARED_KEY" "$CALU_ENV" "$CLAUDE_SETTINGS"
    echo "key files removed: $SHARED_KEY, $CALU_ENV, $CLAUDE_SETTINGS"
    exit 0
    ;;
esac

if [ "${1:-}" != "--no-deploy" ]; then
    say "1. Database, calu and the demo project (setup-demo.sh)"
    stop_server
    bash "$(dirname "$0")/setup-demo.sh" --no-server
fi
bash "$(dirname "$0")/setup-demo.sh" --stop-server

say "2. Sandbox tools and Claude Code"
if ! command -v bwrap >/dev/null || ! command -v socat >/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap socat
fi
if ! command -v claude >/dev/null; then
    curl -fsSL https://claude.ai/install.sh -o /tmp/claude-install.sh
    bash /tmp/claude-install.sh >/tmp/claude-install.log 2>&1
fi
claude --version

say "3. The API key"
if [ -f "$SHARED_KEY" ]; then
    mkdir -p "$(dirname "$CALU_ENV")" "$(dirname "$CLAUDE_SETTINGS")"
    install -m 600 "$SHARED_KEY" "$CALU_ENV"
    # Claude Code's settings: the same variables as an `env` block. Python reads the file;
    # no value appears in an argument or in the output.
    (umask 077 && python3 - "$SHARED_KEY" "$CLAUDE_SETTINGS" <<'EOF'
import json
import pathlib
import sys

source, target = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
env = {}
for line in source.read_text(encoding="utf-8").splitlines():
    name, sep, value = line.strip().partition("=")
    if sep and name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL") and value:
        env[name] = value.strip().strip("'\"")
settings = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
settings["env"] = {**settings.get("env", {}), **env}
target.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
print("variables written:", ", ".join(sorted(env)))
EOF
    )
    chmod 600 "$CLAUDE_SETTINGS"
    rm -f "$SHARED_KEY"
    echo "moved to $CALU_ENV and $CLAUDE_SETTINGS; $SHARED_KEY deleted"
elif [ -f "$CALU_ENV" ] && [ -f "$CLAUDE_SETTINGS" ]; then
    echo "in place already ($CALU_ENV, $CLAUDE_SETTINGS)"
else
    echo "no $SHARED_KEY and no key in place: see the header of this script" >&2
    exit 1
fi
ls -l "$CALU_ENV" "$CLAUDE_SETTINGS" | awk '{print $1, $NF}'

say "4. Models: Haiku for every role; sandbox"
calu config models "$HAIKU"
calu config set claude.model haiku
if bwrap --unshare-user --unshare-net --ro-bind / / true 2>/dev/null; then
    calu config set claude.sandbox.enabled true
else
    echo "this container cannot create user namespaces: Claude Code's sandbox stays off"
    calu config set claude.sandbox.enabled false
fi
# The list of every model setting, to see that no role names another model.
calu config list 2>/dev/null | grep -E 'model|effort' || true

say "5. calu doctor --keys"
calu doctor --keys || true

say "6. calu serve (tmux session $TMUX_SESSION)"
stop_server
# The user login has no password (setup-demo.sh), so nothing is asked; stdin is /dev/null
# all the same.
# The log of an earlier run would pass the check below at once.
rm -f "$LOG"
tmux new-session -d -s "$TMUX_SESSION" -c "$HOME" \
    "calu serve --insecure </dev/null 2>&1 | tee $LOG"
# The server needs several seconds to import calu and connect; wait for its ready line.
for _ in $(seq 60); do
    grep -q "answering session requests" "$LOG" 2>/dev/null && break
    sleep 0.5
done
grep -q "answering session requests" "$LOG" 2>/dev/null || {
    echo "calu serve did not start within 30 s: see $LOG" >&2
    exit 1
}
tail -3 "$LOG"
