#!/usr/bin/env bash
# Spike Q1a, part 2: can project or local settings (files the agent could write in the project)
# switch off a sandbox that the wrapper enables with --settings?
#
#   bash spikes/spike_q1a_precedence.sh [model]          (default model: haiku)
#
# The scratch project's .claude/settings.json and settings.local.json disable the sandbox and
# allow unsandboxed commands; --settings enables it; project and local sources are loaded.
set -euo pipefail
export PATH="/tmp/calu-spike-tools/bin:$PATH"
model="${1:-haiku}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d /tmp/calu-spike-q1p-XXXX)"
tr -d '\r' < "$here/probe.sh" > "$work/probe.sh"
mkdir -p "$work/.claude"
off='{"sandbox":{"enabled":false,"allowUnsandboxedCommands":true,"failIfUnavailable":false}}'
echo "$off" > "$work/.claude/settings.json"
echo "$off" > "$work/.claude/settings.local.json"
cd "$work"
git init -q .
settings='{"sandbox":{"enabled":true,"failIfUnavailable":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false}}'
echo "scratch: $work"
echo "Run exactly this Bash command once and then reply with only the word DONE: bash probe.sh > probe.out 2>&1" |
  claude -p --model "$model" --output-format json --no-session-persistence \
    --setting-sources project,local --strict-mcp-config --settings "$settings" \
    --tools Bash --allowedTools Bash |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ("subtype","is_error","result")})'
grep -E "pid_ns|claude processes|candidate|environ|fd/0|network|reachable|blocked|\.git" probe.out || cat probe.out
