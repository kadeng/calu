#!/usr/bin/env bash
# Run the PyCharm plugin in JetBrains remote development ("split mode": a backend and a thin
# client, both on the desktop of the container) with Gradle's `runIdeSplitMode`.
# Runs INSIDE the container `calu-uitest` as user `cua`.
#
# Before the first run, on the host (the container has no JDK for the build):
#   mkdir -p ~/.cache/calu-uitest/share/jdk && cp -a ~/.cache/calu-jdk/jdk-25.* ~/.cache/calu-uitest/share/jdk/
# Then, from the host, in the calu checkout:
#   D="uv run --no-project --with cua python uitest/desktop.py"
#   $D shell 'setsid -f bash /workspace/calu/uitest/pycharm-split.sh >/tmp/pycharm-split.log 2>&1'
#   $D shell 'bash /workspace/calu/uitest/pycharm-split.sh --stop'
#
# What it does:
#   1. Copies the JDK from /share/jdk to ~/jdk (once) and the plugin's sources from
#      /workspace/calu/extensions/jetbrains to ~/work/jb (every run; build output is kept).
#   2. Lowers the Gradle and Kotlin daemon heaps in the copy (the container has 8 GB; backend,
#      client and the daemons together need about 6 GB). Close the ordinary PyCharm first.
#   3. Runs `./gradlew runIdeSplitMode -PlocalIde=/opt/pycharm` in the tmux session `calu-split`
#      (log: /tmp/gradle-split.log). The IDE's sandbox is ~/work/jb/.intellijPlatform/sandbox;
#      the logs of the two halves are below it in log_runIdeBackend and log_runIdeFrontend.
#
# The thin client (JetBrains Client) shows the "JetBrains Team Tools User Agreement" at its
# first start. This script answers no such dialog: a person reads and decides in the viewer.
set -euo pipefail
TMUX_SESSION=calu-split
COPY="$HOME/work/jb"

if [ "${1:-}" = "--stop" ]; then
    tmux send-keys -t "$TMUX_SESSION" C-c 2>/dev/null || true
    sleep 5
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    if [ -d "$COPY" ]; then (cd "$COPY" && JAVA_HOME="$(ls -d "$HOME"/jdk/jdk-25* | tail -1)" ./gradlew --stop >/dev/null 2>&1 || true); fi
    echo "split mode stopped"
    exit 0
fi

mkdir -p "$HOME/jdk"
if ! ls -d "$HOME"/jdk/jdk-25* >/dev/null 2>&1; then
    cp -a /share/jdk/jdk-25* "$HOME/jdk/"
fi
JAVA_HOME="$(ls -d "$HOME"/jdk/jdk-25* | tail -1)"
export JAVA_HOME DISPLAY="${DISPLAY:-:1}"

rsync -a --delete --exclude build --exclude .gradle --exclude .intellijPlatform --exclude .kotlin \
    /workspace/calu/extensions/jetbrains/ "$COPY/"
sed -i 's/-Xmx4096m/-Xmx1536m/' "$COPY/gradle.properties"
echo 'kotlin.daemon.jvmargs=-Xmx1200m' >>"$COPY/gradle.properties"

tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
tmux new-session -d -s "$TMUX_SESSION" -c "$COPY" \
    "export JAVA_HOME='$JAVA_HOME' DISPLAY='$DISPLAY'; ./gradlew runIdeSplitMode -PlocalIde=/opt/pycharm 2>&1 | tee /tmp/gradle-split.log"
echo "started in the tmux session $TMUX_SESSION; log: /tmp/gradle-split.log"
