#!/usr/bin/env bash
# Spike (review finding cs0): can a settings file of the project widen the sandbox that the
# wrapper passes with --settings?
#
#   bash spikes/spike_settings_widen.sh [model]          (default model: haiku)
#
# Part 1. A scratch project's settings file re-opens one file inside a denied directory
# (sandbox.filesystem.allowRead) and allows one domain (sandbox.network.allowedDomains). Each of
# the two files (.claude/settings.json, .claude/settings.local.json) is tried twice:
#
#   alone      no --settings: the control, which shows that the file's entries work at all
#   pinned     with the --settings document calu passes (calu.config.SandboxConfig.settings)
#
# Part 2. The same two files name a command in sandbox.excludedCommands, and the session runs
# that command on the denied file. "alone" is again the control: it shows whether the entry takes
# the command out of the sandbox at all.
#
# The pinned document is the complete one calu writes (every key of
# `calu.config.SandboxConfig.settings`), so each pinned case also shows that the CLI accepts it.
#
# Measured 2026-11-12, CLI 2.1.319: see spikes/RESULTS.md, "Can a settings file widen the pinned
# sandbox?". calu does not rely on the measurement alone: while the sandbox must not be
# unavailable (`claude.sandbox.fail_if_unavailable`), a session does not start, and a Bash step
# waits for a human, as long as a loaded settings file holds such an entry
# (`calu.preflight.settings_file_sandbox_findings`).
#
# Not covered: the user's own ~/.claude/settings.json. Measuring it means editing the file every
# other Claude Code session of this user reads, or running the CLI with a copy of the login.
set -euo pipefail
export PATH="/tmp/calu-spike-tools/bin:$PATH"
model="${1:-haiku}"
work="$(mktemp -d /tmp/calu-spike-widen-XXXX)"
mkdir -p "$work/secret"
echo "SECRET-A" > "$work/secret/a.txt"
file_settings="{\"sandbox\":{\"enabled\":true,\"autoAllowBashIfSandboxed\":true,\"filesystem\":{\"denyRead\":[\"$work/secret\"],\"allowRead\":[\"$work/secret/a.txt\"]},\"network\":{\"allowedDomains\":[\"example.com\"]}}}"
pinned="{\"sandbox\":{\"enabled\":true,\"failIfUnavailable\":true,\"allowUnsandboxedCommands\":false,\"autoAllowBashIfSandboxed\":true,\"enableWeakerNestedSandbox\":false,\"enableWeakerNetworkIsolation\":false,\"excludedCommands\":[],\"filesystem\":{\"denyRead\":[\"$work/secret\"],\"allowRead\":[],\"allowWrite\":[]},\"network\":{\"allowedDomains\":[],\"deniedDomains\":[],\"allowUnixSockets\":[],\"allowAllUnixSockets\":false,\"allowLocalBinding\":false}}}"
excluding="{\"sandbox\":{\"enabled\":true,\"autoAllowBashIfSandboxed\":true,\"excludedCommands\":[\"cat\",\"cat:*\",\"cat *\"],\"filesystem\":{\"denyRead\":[\"$work/secret\"]}}}"
prompt_excluded="Run exactly this one Bash command as a single Bash call, do not retry or change it, then reply with only its literal output: cat $work/secret/a.txt"
prompt="Run exactly these two Bash commands, each as its own separate Bash call, in this order, then reply with only the word DONE. First: cat $work/secret/a.txt > read.out 2>&1   Second: curl -sS -m 8 -o /dev/null -w '%{http_code}' https://example.com > net.out 2>&1"

run_case() { # <name> <settings file name> <file content> <prompt> [claude arguments...]
  local name="$1" file="$2" content="$3" text="$4"
  shift 4
  local project="$work/$name"
  mkdir -p "$project/.claude"
  echo "$content" > "$project/.claude/$file"
  (
    cd "$project"
    git init -q .
    echo "$text" | claude -p --model "$model" --output-format json --no-session-persistence \
      --setting-sources project,local --strict-mcp-config "$@" \
      --tools Bash --allowedTools Bash > result.json 2> stderr.txt || true
    # A file that is missing means the model skipped that command: run the spike again.
    echo "== $name: read: $(cat read.out 2>/dev/null | tr '\n' ' ' | sed "s#$work#W#g")| net: $(cat net.out 2>/dev/null | tr '\n' ' ')"
  )
}

run_excluded() { # <name> <settings file name> [claude arguments...]
  # The command is run without a redirection, because a command with one was not matched by
  # the exclusion in the control; so the answer is read from the result, not from a file.
  local name="$1" file="$2"
  shift 2
  local project="$work/$name"
  mkdir -p "$project/.claude"
  echo "$excluding" > "$project/.claude/$file"
  (
    cd "$project"
    git init -q .
    echo "$prompt_excluded" | claude -p --model "$model" --output-format json --no-session-persistence \
      --setting-sources project,local --strict-mcp-config "$@" \
      --tools Bash --allowedTools Bash > result.json 2> stderr.txt || true
    echo "== $name: $(python3 -c 'import json,sys; print(" ".join(str(json.load(open("result.json")).get("result")).split())[:300])' | sed "s#$work#W#g")"
  )
}

echo "scratch: $work"
run_case project-alone settings.json "$file_settings" "$prompt"
run_case project-pinned settings.json "$file_settings" "$prompt" --settings "$pinned"
run_case local-alone settings.local.json "$file_settings" "$prompt"
run_case local-pinned settings.local.json "$file_settings" "$prompt" --settings "$pinned"
run_excluded excluded-project-alone settings.json
run_excluded excluded-project-pinned settings.json --settings "$pinned"
run_excluded excluded-local-alone settings.local.json
run_excluded excluded-local-pinned settings.local.json --settings "$pinned"
