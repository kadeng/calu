#!/usr/bin/env bash
# Wrapper for CLAUDE_CODE_SHELL_PREFIX (spike Q1b). Appends its argv to $SPIKE_LOG, then runs the
# command: unchanged (SPIKE_MODE=log) or inside bubblewrap (SPIKE_MODE=bwrap) with its own PID
# namespace and a fresh /proc, the file system read-only except the project directory
# ($SPIKE_PROJECT) and /tmp. Claude Code passes the whole command as ONE argument (a shell
# script), so the wrapper runs it with `bash -c "$1"`.
{
  echo "---- $(date +%T.%N) pid=$$ ppid=$PPID argc=$#"
  i=0
  for a in "$@"; do
    i=$((i + 1))
    printf 'argv[%d] (%d chars): %s\n' "$i" "${#a}" "$(printf '%s' "$a" | head -c 2000 | tr '\n' ' ')"
  done
} >> "${SPIKE_LOG:-/tmp/calu-spike-prefix.log}"
if [ "${SPIKE_MODE:-log}" = bwrap ]; then
  # Order matters: --ro-bind / / first, otherwise it hides the fresh /dev and /proc mounts.
  exec bwrap --die-with-parent --unshare-pid --ro-bind / / --dev /dev --proc /proc \
    --bind /tmp /tmp --bind "$SPIKE_PROJECT" "$SPIKE_PROJECT" \
    --chdir "$PWD" /bin/bash -c "$1"
fi
exec /bin/bash -c "$1"
