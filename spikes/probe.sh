#!/usr/bin/env bash
# Probe for the isolation spikes (Q1a, Q1b). Run as a tool command; writes its findings to
# stdout. It writes nothing outside the current directory except one marker file attempt
# (~/.claude/calu-spike-marker, removed again if it was created).
echo "== identity"
echo "uid=$(id -u) pid=$$ ppid=$PPID"
echo "pid_ns=$(readlink /proc/self/ns/pid) mnt_ns=$(readlink /proc/self/ns/mnt)"
echo "processes visible in /proc: $(ls -d /proc/[0-9]* 2>/dev/null | wc -l)"
echo "== process list"
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  printf '%s %s %s\n' "$p" "$(cat "$d/comm" 2>/dev/null)" "$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null | cut -c1-100)"
done
echo "== ancestors"
p=$$
while [ -n "$p" ] && [ "$p" != 0 ]; do
  echo "$p $(cat /proc/$p/comm 2>/dev/null)"
  p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null)
done
echo "== claude processes"
for d in /proc/[0-9]*; do
  if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q -E '(^|/)claude( |$)|claude-code|cli.js'; then
    p=${d#/proc/}
    echo "candidate pid $p: $(tr '\0' ' ' < "$d/cmdline" | cut -c1-120)"
    if head -c 200 "$d/environ" >/dev/null 2>&1; then
      echo "  environ READABLE ($(tr '\0' '\n' < "$d/environ" | wc -l) vars)"
    else
      echo "  environ not readable"
    fi
    echo "  fd0 -> $(readlink "$d/fd/0" 2>&1)"
    # Open stdin of the process for writing without writing anything (": >>" opens in append mode).
    if ( : >> "$d/fd/0" ) 2>/dev/null; then echo "  fd/0 OPENABLE FOR WRITING"; else echo "  fd/0 not openable for writing"; fi
  fi
done
echo "== file access"
for f in "$HOME/.claude/settings.json" "$HOME/.claude.json" ".claude/settings.json" ".claude/settings.local.json"; do
  if [ -e "$f" ]; then
    if [ -w "$f" ] && ( : >> "$f" ) 2>/dev/null; then echo "$f: WRITABLE"; else echo "$f: not writable"; fi
  else
    echo "$f: missing"
  fi
done
# Readability only; the content is never printed.
for f in "$HOME/.claude/.credentials.json" "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa" "/tmp/calu-spike-secret"; do
  if [ -e "$f" ]; then
    if head -c 1 "$f" >/dev/null 2>&1; then echo "$f: READABLE"; else echo "$f: not readable"; fi
  else
    echo "$f: missing or hidden"
  fi
done
if ( : > "$HOME/.claude/calu-spike-marker" ) 2>/dev/null; then
  echo "~/.claude: CAN CREATE FILES"; rm -f "$HOME/.claude/calu-spike-marker"
else
  echo "~/.claude: cannot create files"
fi
if ( : > "./probe-write-test" ) 2>/dev/null; then echo "cwd: writable"; rm -f ./probe-write-test; else echo "cwd: not writable"; fi
if [ -d .git/hooks ]; then
  if ( : > .git/hooks/probe-test ) 2>/dev/null; then echo ".git/hooks: WRITABLE"; rm -f .git/hooks/probe-test; else echo ".git/hooks: not writable"; fi
  if ( : >> .git/config ) 2>/dev/null; then echo ".git/config: WRITABLE"; else echo ".git/config: not writable"; fi
fi
echo "== process that outlives the command"
# A detached sleep with a recognisable argument; the spike script looks for it after claude exits.
setsid nohup sleep 177 >/dev/null 2>&1 &
echo "started detached 'sleep 177' (pid $! in this namespace)"
echo "== network"
if timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then echo "tcp 1.1.1.1:443 reachable"; else echo "tcp 1.1.1.1:443 blocked"; fi
