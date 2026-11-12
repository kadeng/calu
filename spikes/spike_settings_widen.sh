#!/usr/bin/env bash
# Spike (review finding cs0): can a settings file of the project widen the sandbox that the
# wrapper passes with --settings?
#
#   bash spikes/spike_settings_widen.sh [model]          (default model: haiku)
#
# A scratch project's settings file re-opens one file inside a denied directory
# (sandbox.filesystem.allowRead) and allows one domain (sandbox.network.allowedDomains). Each of
# the two files (.claude/settings.json, .claude/settings.local.json) is tried twice:
#
#   alone      no --settings: the control, which shows that the file's entries work at all
#   pinned     with the --settings document calu passes (calu.config.SandboxConfig.settings)
#
# Expected (measured 2026-11-12, CLI 2.1.319): "alone" reads the file and reaches the domain;
# "pinned" does neither. If "pinned" ever reads the file or reaches the domain, the settings
# files can widen calu's sandbox, and calu.preflight.settings_file_sandbox_warnings is no longer
# a precaution but the only notice of it.
set -euo pipefail
export PATH="/tmp/calu-spike-tools/bin:$PATH"
model="${1:-haiku}"
work="$(mktemp -d /tmp/calu-spike-widen-XXXX)"
mkdir -p "$work/secret"
echo "SECRET-A" > "$work/secret/a.txt"
file_settings="{\"sandbox\":{\"enabled\":true,\"autoAllowBashIfSandboxed\":true,\"filesystem\":{\"denyRead\":[\"$work/secret\"],\"allowRead\":[\"$work/secret/a.txt\"]},\"network\":{\"allowedDomains\":[\"example.com\"]}}}"
pinned="{\"sandbox\":{\"enabled\":true,\"failIfUnavailable\":true,\"allowUnsandboxedCommands\":false,\"autoAllowBashIfSandboxed\":true,\"enableWeakerNestedSandbox\":false,\"excludedCommands\":[],\"filesystem\":{\"denyRead\":[\"$work/secret\"],\"allowRead\":[]},\"network\":{\"allowedDomains\":[],\"allowAllUnixSockets\":false,\"allowLocalBinding\":false}}}"
prompt="Run exactly these two Bash commands, each as its own separate Bash call, in this order, then reply with only the word DONE. First: cat $work/secret/a.txt > read.out 2>&1   Second: curl -sS -m 8 -o /dev/null -w '%{http_code}' https://example.com > net.out 2>&1"

run_case() { # <name> <settings file name> [claude arguments...]
  local name="$1" file="$2"
  shift 2
  local project="$work/$name"
  mkdir -p "$project/.claude"
  echo "$file_settings" > "$project/.claude/$file"
  (
    cd "$project"
    git init -q .
    echo "$prompt" | claude -p --model "$model" --output-format json --no-session-persistence \
      --setting-sources project,local --strict-mcp-config "$@" \
      --tools Bash --allowedTools Bash > result.json 2> stderr.txt || true
    # A file that is missing means the model skipped that command: run the spike again.
    echo "== $name: read: $(cat read.out 2>/dev/null | tr '\n' ' ' | sed "s#$work#W#g")| net: $(cat net.out 2>/dev/null | tr '\n' ' ')"
  )
}

echo "scratch: $work"
run_case project-alone settings.json
run_case project-pinned settings.json --settings "$pinned"
run_case local-alone settings.local.json
run_case local-pinned settings.local.json --settings "$pinned"
