#!/usr/bin/env bash
# Start the SSH box `calu-sshbox` (idempotent): a headless server with sshd, calu, PostgreSQL
# and a scripted `calu serve`, for the PyCharm plugin's SSH mode. See ../README.md.
#
# Usage: calu/uitest/sshbox/run.sh [--rebuild] [--no-install] [--reset-project]
#
#   1. Makes a key pair FOR THE TESTS once: ~/.cache/calu-uitest/ssh/id_ed25519 (no
#      passphrase). The owner's keys, agent and ~/.ssh are never read or changed.
#   2. Builds the image `calu-sshbox:latest` when it is missing (or with --rebuild).
#   3. Creates the network `calu-uitest-net` and connects the desktop container `calu-uitest`
#      to it if it runs (without restarting it), so the desktop reaches this box as host
#      `sshbox`, port 22.
#   4. Reuses a running container, starts a stopped one, or creates a new one: 2 GB memory,
#      the calu checkout read-only at /src/calu, SSH published on 127.0.0.1:$CALU_SSHBOX_PORT
#      (default 56522) for tests that run on the host.
#   5. Installs the public key for `dev`, then runs `sshbox-install` (calu, setup, project,
#      scripted server) unless --no-install.
#   6. If the desktop container runs: says so if it has no `ssh` client, and copies the private key to its user cua (~/.ssh/
#      calu_sshbox, mode 600) with a `Host sshbox` entry in ~/.ssh/config and the box's host
#      key in ~/.ssh/known_hosts.
#
# From the host:     ssh -F ~/.cache/calu-uitest/ssh/config sshbox
# From the desktop:  ssh sshbox
set -euo pipefail

NAME=calu-sshbox
IMAGE=calu-sshbox:latest
NET=calu-uitest-net
DESKTOP=calu-uitest
PORT="${CALU_SSHBOX_PORT:-56522}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
KEYDIR="$HOME/.cache/calu-uitest/ssh"
KEY="$KEYDIR/id_ed25519"
rebuild=no install=yes install_args=""
for arg in "$@"; do
    case "$arg" in
        --rebuild) rebuild=yes ;;
        --no-install) install=no ;;
        --reset-project) install_args="--reset-project" ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$KEYDIR" && chmod 700 "$KEYDIR"
if [ ! -s "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -C calu-uitest -f "$KEY"
fi

if [ "$rebuild" = yes ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$HERE"
fi

docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
desktop_runs=no
if [ "$(docker container inspect -f '{{.State.Status}}' "$DESKTOP" 2>/dev/null || true)" = running ]; then
    desktop_runs=yes
    if ! docker container inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$DESKTOP" | grep -qw "$NET"; then
        docker network connect "$NET" "$DESKTOP"
        echo "$DESKTOP connected to $NET"
    fi
fi

state="$(docker container inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || true)"
case "${state:-missing}" in
    running) echo "$NAME is already running" ;;
    missing)
        echo "memory before the start:"; free -g | sed -n 1,2p
        docker run -d --name "$NAME" --hostname sshbox --memory=2g \
            --network "$NET" --network-alias sshbox \
            -p "127.0.0.1:$PORT:22" \
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

# The test key for dev (the image holds none).
docker exec -i "$NAME" bash -c 'install -m 0600 -o dev -g dev /dev/stdin /home/dev/.ssh/authorized_keys' <"$KEY.pub"

if [ "$install" = yes ]; then
    # shellcheck disable=SC2086
    docker exec "$NAME" /usr/local/sbin/sshbox-install $install_args
fi

# The host's entry for the tests: its own config and known_hosts files, not the owner's.
HOSTKEY="$(docker exec "$NAME" cat /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f1,2)"
printf '[127.0.0.1]:%s %s\n' "$PORT" "$HOSTKEY" >"$KEYDIR/known_hosts"
cat >"$KEYDIR/config" <<CFG
Host sshbox
    HostName 127.0.0.1
    Port $PORT
    User dev
    IdentityFile $KEY
    IdentitiesOnly yes
    IdentityAgent none
    UserKnownHostsFile $KEYDIR/known_hosts
    StrictHostKeyChecking yes
CFG
chmod 600 "$KEYDIR/config"

if [ "$desktop_runs" = yes ]; then
    # The plugin's SSH mode starts the system's `ssh`; the desktop image has none.
    if ! docker exec "$DESKTOP" sh -c 'command -v ssh >/dev/null'; then
        echo "desktop: no ssh client. Install it once:" >&2
        echo "  \$D shell 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-client'" >&2
    fi
    docker exec -u cua "$DESKTOP" mkdir -p -m 700 /home/cua/.ssh
    docker exec -i -u cua -e HOME=/home/cua "$DESKTOP" bash -c 'install -m 0600 /dev/stdin /home/cua/.ssh/calu_sshbox' <"$KEY"
    docker exec -i -u cua -e HOME=/home/cua "$DESKTOP" bash -c '
        touch ~/.ssh/config ~/.ssh/known_hosts && chmod 600 ~/.ssh/config
        if ! grep -q "^Host sshbox\$" ~/.ssh/config; then
            printf "Host sshbox\n    User dev\n    IdentityFile ~/.ssh/calu_sshbox\n    IdentitiesOnly yes\n" >>~/.ssh/config
        fi
        grep -v "^sshbox " ~/.ssh/known_hosts >~/.ssh/known_hosts.new || true
        cat - >>~/.ssh/known_hosts.new && mv ~/.ssh/known_hosts.new ~/.ssh/known_hosts' <<<"sshbox $HOSTKEY"
    echo "desktop: key, config entry and host key for \`ssh sshbox\` are in /home/cua/.ssh"
fi

echo "ready: ssh -F $KEYDIR/config sshbox   (host: 127.0.0.1:$PORT, user dev, key $KEY)"
