#!/usr/bin/env bash
# Spike Q1b: does CLAUDE_CODE_SHELL_PREFIX route every Bash command through a wrapper, and does a
# bubblewrap wrapper (own PID namespace) hide the Claude Code process from tool commands?
#
#   bash spikes/spike_q1b_shell_prefix.sh [model]        (default model: haiku)
#
# Claude Code's own sandbox is off here. Run 1 logs the wrapper's argv only (SPIKE_MODE=log) for
# a foreground and a background command; run 2 executes the commands in bubblewrap
# (SPIKE_MODE=bwrap) and runs probe.sh. Start it from a plain Linux environment (see
# spike_q1a_cc_sandbox.sh).
set -euo pipefail
model="${1:-haiku}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d /tmp/calu-spike-q1b-XXXX)"
tr -d '\r' < "$here/probe.sh" > "$work/probe.sh"
tr -d '\r' < "$here/shell-prefix.sh" > "$work/shell-prefix.sh"
chmod +x "$work/shell-prefix.sh"
cd "$work"
git init -q .
echo "scratch: $work"
export CLAUDE_CODE_SHELL_PREFIX="$work/shell-prefix.sh"
export SPIKE_PROJECT="$work"
run() {
  echo "$1" |
    claude -p --model "$model" --output-format json --no-session-persistence \
      --setting-sources "" --strict-mcp-config \
      --tools Bash --allowedTools Bash |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ("subtype","is_error","num_turns","result")})'
}
SPIKE_MODE=log SPIKE_LOG="$work/prefix-log.txt" \
  run "Run these two Bash commands, each exactly once, then reply with only DONE. First: echo foreground-marker > fg.out. Second, with run_in_background set to true: echo background-marker > bg.out"
SPIKE_MODE=log SPIKE_LOG="$work/prefix-control.txt" \
  run "Run exactly this Bash command once and then reply with only the word DONE: bash probe.sh > probe-control.out 2>&1"
SPIKE_MODE=bwrap SPIKE_LOG="$work/prefix-bwrap.txt" \
  run "Run exactly this Bash command once and then reply with only the word DONE: bash probe.sh > probe-bwrap.out 2>&1"
for f in prefix-log.txt fg.out bg.out probe-control.out prefix-bwrap.txt probe-bwrap.out; do
  echo "==== $f"
  cat "$f" 2>/dev/null || echo "(missing)"
done
echo "==== outside: detached 'sleep 177' processes still alive after claude exited"
pgrep -af "sleep 177" || echo "none"
