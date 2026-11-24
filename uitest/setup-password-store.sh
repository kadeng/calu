#!/usr/bin/env bash
# A second calu store WITH passwords, for the one-password flow of an editor plugin.
# Runs INSIDE the container `calu-uitest` as user `cua`, after setup-demo.sh.
#
# Usage (from the host, in the calu checkout):
#   D="uv run --no-project --with cua python uitest/desktop.py"
#   $D shell 'bash /workspace/calu/uitest/setup-password-store.sh'          # create, start its server
#   $D shell 'bash /workspace/calu/uitest/setup-password-store.sh --stop'   # stop its server
#
# What it does:
#   1. Creates a second PostgreSQL cluster `pw` on port 5433 with the role `calu_owner` and the
#      database `calu_pw` (calu's roles are server-wide: the first cluster keeps its own).
#   2. Runs `calu setup` with its own XDG directories below ~/pwstore: the calu user password
#      is `user-pw`; it protects the logins `user` and `db-admin`. A store without `db-admin`
#      (an earlier setup failed, or a calu from before the one user login stored it) starts
#      again from an empty database.
#   3. Starts uitest/fake_server.py for that store in the tmux session `calu-fake-pw`.
#   4. Writes ~/pwstore/calu: a wrapper that runs calu with those XDG directories. An editor
#      plugin reaches the store with the setting "calu command" = /home/cua/pwstore/calu.
#
# The passwords are test values of a throwaway database inside the test container.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
STORE="$HOME/pwstore"
TMUX_SESSION=calu-fake-pw
COPY="$HOME/work/calu"

if [ "${1:-}" = "--stop" ]; then
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    echo "password store's server stopped"
    exit 0
fi

mkdir -p "$STORE/config" "$STORE/state" "$STORE/data"
cat >"$STORE/calu" <<EOF
#!/usr/bin/env bash
# calu with the password-protected store of setup-password-store.sh.
export XDG_CONFIG_HOME="$STORE/config" XDG_STATE_HOME="$STORE/state" XDG_DATA_HOME="$STORE/data"
exec "$HOME/.local/bin/calu" "\$@"
EOF
chmod +x "$STORE/calu"

# calu's database roles are server-wide, so the second store needs a PostgreSQL cluster of its
# own (port 5433); the cluster of setup-demo.sh keeps its roles.
PG_VERSION="$(ls /usr/lib/postgresql | sort -n | tail -1)"
if ! pg_lsclusters -h | grep -q "^$PG_VERSION *pw "; then
    sudo pg_createcluster "$PG_VERSION" pw --port 5433 >/dev/null
fi
if ! pg_lsclusters -h | grep "^$PG_VERSION *pw " | grep -q online; then
    sudo pg_ctlcluster "$PG_VERSION" pw start
fi
PSQL=(sudo -u postgres psql -p 5433 -At)
if ! "${PSQL[@]}" -c "SELECT 1 FROM pg_roles WHERE rolname='calu_owner'" | grep -q 1; then
    "${PSQL[@]}" -c "CREATE ROLE calu_owner LOGIN CREATEROLE PASSWORD 'calu-uitest'"
fi
if ! "${PSQL[@]}" -c "SELECT 1 FROM pg_database WHERE datname='calu_pw'" | grep -q 1; then
    sudo -u postgres createdb -p 5433 -O calu_owner calu_pw
fi
if [ ! -s "$STORE/config/calu/credentials.json" ] || ! grep -q '"db-admin"' "$STORE/config/calu/credentials.json"; then
    rm -f "$STORE/config/calu/credentials.json"
    # A store that is incomplete (an earlier setup failed, or calu stores other logins now)
    # starts from an empty database: calu refuses to take over the roles of a setup whose
    # logins this machine no longer holds. The cluster is this script's own throwaway.
    "${PSQL[@]}" -c "DROP DATABASE IF EXISTS calu_pw" >/dev/null
    for role in calu_session calu_operator calu_admin; do
        "${PSQL[@]}" -c "DROP ROLE IF EXISTS $role" >/dev/null
    done
    sudo -u postgres createdb -p 5433 -O calu_owner calu_pw
    # The answers, in the order of the prompts: the owner's password, then the new calu user
    # password twice.
    printf 'calu-uitest\nuser-pw\nuser-pw\n' | "$STORE/calu" setup --database server \
        --host 127.0.0.1 --port 5433 --dbname calu_pw --user calu_owner --sslmode disable
fi
(cd "$HOME/work/demo" && printf 'user-pw\n' | "$STORE/calu" project add . --name demo >/dev/null 2>&1 || true)

tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
TOOL_PYTHON="$(uv tool dir)/calu/bin/python"
tmux new-session -d -s "$TMUX_SESSION" -c "$COPY" \
    "export XDG_CONFIG_HOME='$STORE/config' XDG_STATE_HOME='$STORE/state' XDG_DATA_HOME='$STORE/data'; printf 'user-pw\n' | '$TOOL_PYTHON' uitest/fake_server.py 2>&1 | tee /tmp/calu-fake-pw-server.log"
sleep 4
tail -3 /tmp/calu-fake-pw-server.log || true
echo "calu command for the plugin: $STORE/calu"
