#!/usr/bin/env bash
# What `enableWeakerNestedSandbox` changes inside a container (PLAN.md Q1, D134).
#
#     spikes/spike_nested_sandbox.sh WEAKER SECCOMP
#
# WEAKER is true or false; SECCOMP is "default" (Docker's own profile) or "unconfined". One real
# sandboxed tool call: Claude Code in the calu-dev image runs spikes/probe.sh through its Bash
# tool with the sandbox on, `failIfUnavailable` and no unsandboxed fallback, and prints what the
# probe saw. Needs `docker compose build dev` and ANTHROPIC_API_KEY in the environment (Claude
# Code authenticates with it inside the container; a run costs a fraction of a cent on haiku).
set -euo pipefail
weaker=${1:?true or false}
seccomp=${2:?default or unconfined}
root=$(cd "$(dirname "$0")/.." && pwd)
settings=$(printf '{"sandbox":{"enabled":true,"failIfUnavailable":true,"allowUnsandboxedCommands":false,"autoAllowBashIfSandboxed":true,"enableWeakerNestedSandbox":%s}}' "$weaker")
opt=()
if [ "$seccomp" = unconfined ]; then opt=(--security-opt seccomp=unconfined); fi
docker run --rm "${opt[@]}" -e ANTHROPIC_API_KEY \
  -v "$root/spikes/probe.sh:/probe/probe.sh:ro" -w /tmp calu-dev-dev:latest bash -c "
    mkdir -p /tmp/proj && cd /tmp/proj && git init -q &&
    timeout 240 claude -p 'Run exactly this command with the Bash tool and then print its complete output verbatim, nothing else: bash /probe/probe.sh' \
      --model haiku --settings '$settings' --allowedTools Bash --no-session-persistence \
      --output-format text < /dev/null 2>&1"
