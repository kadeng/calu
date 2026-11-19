#!/usr/bin/env bash
# Prepare the UI-testing desktop for a run of the VSCode extension against a
# scripted session. Runs INSIDE the container `calu-uitest` as user `cua`
# (idempotent: every step checks what is there already).
#
# Usage (from the host, in the calu checkout):
#   D="uv run --no-project --with cua python uitest/desktop.py"
#   $D shell 'bash /workspace/calu/uitest/setup-demo.sh'
#   $D shell 'bash /workspace/calu/uitest/setup-demo.sh --no-vscode'   # everything but VSCode
#   $D shell 'bash /workspace/calu/uitest/setup-demo.sh --stop-server' # stop the fake server
#   $D shell 'bash /workspace/calu/uitest/setup-demo.sh --no-server'   # steps 1 to 5 only
#
# What it does:
#   1. Installs PostgreSQL (apt) if missing and starts the cluster without systemd.
#   2. Creates the role `calu_owner` and the database `calu` (test password, see OWNER_PASSWORD).
#   3. Copies the checkout from /workspace/calu (the host's working tree, uncommitted
#      changes included; without .git, .venv, node_modules and .env) to ~/work/calu and
#      installs calu from the copy with `uv tool install` (command `calu` in ~/.local/bin).
#   4. Runs `calu setup --database server --no-password` once: the `admin` and `calu_admin`
#      logins are stored without a password, so `calu ide` asks for none. The session
#      password is blank. Switches the sandbox off (the container has no sandbox tools).
#   5. Creates the demo project ~/work/demo (a git repository with two Python files).
#   6. Starts uitest/fake_server.py in the tmux session `calu-fake` (restarted on every run).
#   7. Installs /share/calu.vsix into the `uitest-code` profile and opens ~/work/demo.
#
# Environment:
#   CALU_UITEST_VSIX     the extension package (default /share/calu.vsix)
#   CALU_UITEST_SOURCE   the checkout to install calu from (default /workspace/calu, the host's
#                        working tree). Point it at an exported commit, e.g. /share/src, when
#                        other work in progress in the working tree must stay out of the test.
#
# Nothing here is a secret worth protecting: the database lives in the test container, holds
# no real data and listens on the container's loopback only.
set -euo pipefail

OWNER_PASSWORD=calu-uitest
VSIX="${CALU_UITEST_VSIX:-/share/calu.vsix}"
SOURCE="${CALU_UITEST_SOURCE:-/workspace/calu}"
COPY="$HOME/work/calu"
DEMO="$HOME/work/demo"
TMUX_SESSION=calu-fake
export PATH="$HOME/.local/bin:$PATH"
export DISPLAY="${DISPLAY:-:1}"

say() { printf '\n== %s\n' "$*"; }

stop_server() {
    # C-c lets the server stop its sessions and mark them stopped in the database.
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux send-keys -t "$TMUX_SESSION" C-c
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            tmux has-session -t "$TMUX_SESSION" 2>/dev/null || return 0
            sleep 0.5
        done
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    fi
}

if [ "${1:-}" = "--stop-server" ]; then
    stop_server
    echo "fake server stopped"
    exit 0
fi

say "1. PostgreSQL"
if ! command -v pg_ctlcluster >/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql rsync
fi
PG_VERSION="$(ls /usr/lib/postgresql | sort -n | tail -1)"
if ! pg_lsclusters -h | grep -q online; then
    sudo pg_ctlcluster "$PG_VERSION" main start
fi
pg_lsclusters

say "2. Role and database"
as_postgres() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qAt "$@"; }
if [ "$(as_postgres -c "SELECT 1 FROM pg_roles WHERE rolname = 'calu_owner'")" != 1 ]; then
    as_postgres -c "CREATE ROLE calu_owner LOGIN CREATEROLE PASSWORD '$OWNER_PASSWORD'"
fi
if [ "$(as_postgres -c "SELECT 1 FROM pg_database WHERE datname = 'calu'")" != 1 ]; then
    as_postgres -c "CREATE DATABASE calu OWNER calu_owner"
fi

say "3. calu from the checkout"
mkdir -p "$COPY"
rsync -a --delete \
    --exclude .git --exclude .venv --exclude node_modules --exclude .env \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache --exclude '*.vsix' --exclude .intellijPlatform --exclude .gradle --exclude .kotlin \
    "$SOURCE/" "$COPY/"
uv tool install --quiet --python 3.14 --reinstall "$COPY"
calu --version 2>/dev/null || true
TOOL_PYTHON="$(uv tool dir)/calu/bin/python"

say "4. calu setup"
if ! calu credential list 2>/dev/null | grep -q '^admin\b'; then
    # The answers, in the order of the prompts: the owner's password, then the new session
    # password twice (blank).
    printf '%s\n\n\n' "$OWNER_PASSWORD" | calu setup --database server --host 127.0.0.1 \
        --port 5432 --dbname calu --user calu_owner --sslmode disable --no-password
else
    # `calu setup` refuses a second database; the migrations of a newer calu come from
    # `calu db migrate` (the admin login has no password here, so the prompt takes a blank).
    echo "credentials exist: setup skipped; migrations:"
    printf '\n' | calu db migrate
fi
calu config set claude.sandbox.enabled false

say "5. Demo project"
if [ ! -d "$DEMO/.git" ]; then
    mkdir -p "$DEMO"
    cat >"$DEMO/greeter.py" <<'EOF'
def greet(name: str) -> str:
    """Return the greeting for `name`."""
    return f"Hello, {name}!"


def greet_all(names: list[str]) -> list[str]:
    """Return one greeting per name, in the order given."""
    return [greet(name) for name in names]
EOF
    cat >"$DEMO/test_greeter.py" <<'EOF'
from greeter import greet, greet_all


def test_greet_names_the_person():
    assert "Ada" in greet("Ada")


def test_greet_all_keeps_the_order():
    assert [g.split(", ")[1] for g in greet_all(["Ada", "Bob"])] == ["Ada!", "Bob!"]
EOF
    printf '# demo\n\nA project for the calu UI tests.\n' >"$DEMO/README.md"
    git -C "$DEMO" init -q -b main
    git -C "$DEMO" -c user.name=uitest -c user.email=uitest@example.org add -A
    git -C "$DEMO" -c user.name=uitest -c user.email=uitest@example.org commit -q -m "Demo project"
fi
(cd "$DEMO" && calu project add . --name demo >/dev/null 2>&1 || true)
git -C "$DEMO" status --short

if [ "${1:-}" = "--no-server" ]; then
    # setup-real.sh starts `calu serve` itself.
    stop_server
    exit 0
fi

say "6. Fake server (tmux session $TMUX_SESSION)"
stop_server
# stdin is /dev/null: the blank session password is read from it, nothing is asked.
tmux new-session -d -s "$TMUX_SESSION" -c "$COPY" \
    "'$TOOL_PYTHON' uitest/fake_server.py </dev/null 2>&1 | tee /tmp/calu-fake-server.log"
sleep 3
tmux capture-pane -p -t "$TMUX_SESSION" | grep -v '^$' | tail -5 || {
    echo "the fake server did not start: see /tmp/calu-fake-server.log" >&2
    exit 1
}

if [ "${1:-}" = "--no-vscode" ]; then
    exit 0
fi

say "7. VSCode"
if [ -f "$VSIX" ]; then
    uitest-code --install-extension "$VSIX" --force
else
    echo "no $VSIX: build it on the host (npm run package) and copy it to the share" >&2
fi
setsid -f uitest-code "$DEMO" >/tmp/uitest-code.log 2>&1
echo "VSCode opens $DEMO"
