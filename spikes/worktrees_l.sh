#!/usr/bin/env bash
# The real-Claude runs L1-L6 of docs/TESTING.md item 47 (worktrees, PLAN D251; results in
# agent/records/notes/test-plan.md, section L). Runs INSIDE the container `calu-uitest` as user
# `cua`, with the real Claude Code on Haiku and the key set up by uitest/setup-real.sh, on the
# calu installed there. The setup follows spikes/spike_worktrees.sh.
#
# Usage (from the host; V is `ign` (.claude/worktrees/ in .gitignore) or `noign`):
#   X="docker exec -u cua -e HOME=/home/cua calu-uitest bash /workspace/calu/spikes/worktrees_l.sh"
#   $X setup V        # projects ~/work/wtl-V (L1, L2, L4, L5, L6) and ~/work/wtl-V-par (L3)
#   $X s1 V           # session 1 in wtl-V: L1 (main agent in a worktree, merge) and L2
#   $X s2 V           # session 2 in wtl-V: L4 (ends at its escalation, which nobody decides)
#   $X s3 V           # session 3 in wtl-V: L5 and L6 (ends at the --force escalation)
#   $X s4 V           # wtl-V-par: L3, two background worktree subagents
#   $X wait           # until the tmux session `wtl` has ended
#   $X collect V RUN  # evidence of the newest session of the run's project to /share/worktrees-l/V-RUN
#   $X probe-on       # install the test-only probe (spikes/worktrees_probe.py) into calu's tool env
#   $X probe-off      # remove it again
#   $X leakcheck      # count evidence files with a known secret value or a key shape
#                     # (spikes/worktrees_leakcheck.py, calu's own stage-1 redaction)
#
# Every session runs `calu run --batch` in the tmux session `wtl`. Nobody decides an
# escalation: `escalation_timeout_s` is 90, after which calu denies the step and stops the
# session, so a step that is expected to ask is the last one of its session.
# The project's .claude/settings.json adds command hooks that append each hook input to
# /tmp/wtl-hooks/<project>.jsonl and decide nothing.
# While the test-only probe is installed (probe-on), it logs the checkout lines a checker reads,
# the checkouts calu assigned each step to and each honesty snapshot to /tmp/wtl-probe.
# calu is installed from a commit as uitest/README.md says (CALU_UITEST_SOURCE); a reinstall
# removes the probe.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
LOG=/tmp/wtl-hooks
OUT=/tmp/wtl-out
PROBE=/tmp/wtl-probe
SHARE=/share/worktrees-l
mkdir -p "$LOG" "$OUT" "$PROBE"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL_PY="$(uv tool dir)/calu/bin/python"
V="${2:-ign}"
case "$V" in ign | noign) ;; *) echo "variant must be ign or noign" >&2; exit 2 ;; esac
P="$HOME/work/wtl-$V"
PAR="$HOME/work/wtl-$V-par"
HEAD_LINE='Do exactly these steps in order, one tool call each, and keep answers short. If a step is denied, report the reason in one line and go on with the next step.'
SUB_LINE='Do these steps in order and report each outcome in one line; if a step is denied, go on with the next.'

settings() { # $1: log file name
    cat <<EOF
{
  "hooks": {
$(for event in SessionStart PreToolUse PostToolUse SubagentStart SubagentStop CwdChanged; do
        printf '    "%s": [{"hooks": [{"type": "command", "command": "{ cat; echo; } >> %s/%s"}]}],\n' \
            "$event" "$LOG" "$1"
    done | sed '$ s/,$//')
  }
}
EOF
}

make_project() { # $1: directory
    local q="$1" name
    name="$(basename "$q")"
    rm -rf "$q" "$LOG/$name.jsonl" && mkdir -p "$q/.claude"
    cd "$q"
    printf '__pycache__/\n' >.gitignore
    [ "$V" = ign ] && printf '.claude/worktrees/\n' >>.gitignore
    cat >calc.py <<'EOF'
def add(a: int, b: int) -> int:
    """Return the sum of `a` and `b`."""
    return a + b
EOF
    cat >test_calc.py <<'EOF'
import unittest

from calc import add


class TestAdd(unittest.TestCase):
    def test_add(self) -> None:
        """add(2, 3) is 5."""
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
EOF
    printf '# CLAUDE.md\n\nKeep answers short.\n' >CLAUDE.md
    settings "$name.jsonl" >.claude/settings.json
    git init -q -b main
    git config user.name wtl && git config user.email wtl@example.org
    git add -A && git commit -q -m "wtl: base of $name"
    calu project add . --name "$name" >/dev/null 2>&1 || true
    calu config set --project "$name" honesty.mode enforce >/dev/null
    calu config set --project "$name" traces.enabled true >/dev/null
    calu config set --project "$name" changes.snapshot true >/dev/null
    calu config set --project "$name" escalation_timeout_s 90 >/dev/null
    calu honesty trust HEAD --note "wtl base"
    echo "project $q ready ($(cat .gitignore | tr '\n' ' '))"
}

start() { # $1: directory, $2: prompt file, $3: log name
    if tmux has-session -t wtl 2>/dev/null; then echo "tmux session wtl still runs" >&2; exit 1; fi
    tmux new-session -d -s wtl -c "$1" \
        "calu run --batch --prompt-file $2 . </dev/null 2>&1 | tee $OUT/$3.log"
    echo "started in tmux session wtl; log $OUT/$3.log"
}

agent_tree() { # the subagent worktree of the project $1 with uncommitted changes (L2's)
    local w
    for w in $(git -C "$1" worktree list --porcelain | sed -n 's|^worktree \(.*/agent-[0-9a-f]*\)$|\1|p'); do
        [ -n "$(git -C "$w" status --porcelain)" ] && { basename "$w"; return; }
    done
}

case "${1:-}" in
setup)
    make_project "$P"
    make_project "$PAR"
    ;;
s1)
    cat >"$OUT/s1-$V.txt" <<EOF
$HEAD_LINE
1. Use the EnterWorktree tool to create a worktree named wta.
2. Write the file helper.py with this content:
def double(x: int) -> int:
    """Return twice \`x\`."""
    return 2 * x
3. Write the file test_helper.py with this content:
import unittest

from helper import double


class TestDouble(unittest.TestCase):
    def test_double(self) -> None:
        """double(2) is 4."""
        self.assertEqual(double(2), 4)
4. Run with Bash: python3 -m unittest -q
5. Run with Bash: git add helper.py test_helper.py
6. Run with Bash: git commit -q -m "Add helper"
7. Use the ExitWorktree tool with action keep.
8. Run with Bash: git merge --no-edit worktree-wta
9. Run with Bash: python3 -m unittest -q
10. Start one subagent with the Agent tool. Its input must contain "isolation": "worktree" and "run_in_background": false. Its prompt: '$SUB_LINE a) Run with Bash: python3 -m unittest -q b) Edit calc.py so that add returns a - b and keep its docstring unchanged (a test of the code checkers). c) Run with Bash: python3 -m unittest -q'
11. Run with Bash: python3 -m unittest -q
12. Run with Bash: git worktree list
Then reply done.
EOF
    start "$P" "$OUT/s1-$V.txt" "s1-$V"
    ;;
s2)
    cat >"$OUT/s2-$V.txt" <<EOF
$HEAD_LINE
1. Start one subagent with the Agent tool. Its input must contain "isolation": "worktree" and "run_in_background": false. Its prompt: 'Run with Bash: PYTHONPATH=../wta python3 -c "import helper; print(helper.double(2))" and report the outcome in one line.'
Then reply done.
EOF
    start "$P" "$OUT/s2-$V.txt" "s2-$V"
    ;;
s3)
    T="$(agent_tree "$P")"
    [ -n "$T" ] || { echo "no subagent worktree in $P" >&2; exit 1; }
    cat >"$OUT/s3-$V.txt" <<EOF
$HEAD_LINE
1. Run with Bash: cd .claude/worktrees/$T && python3 -m unittest -q
2. Run with Bash: cd $P && python3 -m unittest -q
3. Run with Bash: git worktree remove .claude/worktrees/wta
4. Run with Bash: git worktree remove --force .claude/worktrees/$T
Then reply done.
EOF
    start "$P" "$OUT/s3-$V.txt" "s3-$V"
    ;;
s4)
    cat >"$OUT/s4-$V.txt" <<EOF
Do exactly this and keep answers short.
1. In ONE message, start two subagents with the Agent tool. The input of each must contain "isolation": "worktree" and "run_in_background": true.
Subagent A, prompt: '$SUB_LINE a) Run with Bash: python3 -m unittest -q b) Edit calc.py so that add returns a - b and keep its docstring unchanged (a test of the code checkers). c) Run with Bash: python3 -m unittest -q d) Run with Bash: echo a > out_a.txt'
Subagent B, prompt: '$SUB_LINE a) Run with Bash: python3 -m unittest -q b) Write the file square.py with this content:
def square(x: int) -> int:
    """Return \`x\` times \`x\`."""
    return x * x
c) Run with Bash: python3 -c "import square; print(square.square(3))" d) Run with Bash: python3 -m unittest -q e) Run with Bash: echo b > out_b.txt'
2. Wait for both subagents to finish, then report their results in two lines and reply done.
EOF
    start "$PAR" "$OUT/s4-$V.txt" "s4-$V"
    ;;
wait)
    for _ in $(seq 1 110); do
        tmux has-session -t wtl 2>/dev/null || { echo ended; exit 0; }
        sleep 1
    done
    echo "still running"
    ;;
collect)
    RUN="${3:?run name}"
    Q="$P"; [ "$RUN" = s4 ] && Q="$PAR"
    D="$SHARE/$V-$RUN"
    rm -rf "$D" && mkdir -p "$D"
    sid="$(cd /tmp && sudo -u postgres psql -d calu -tAc \
        "select id from sessions where directory = '$Q' order by started_at desc limit 1")"
    echo "$sid" >"$D/session-id.txt"
    cp "$OUT/$RUN-$V.txt" "$D/prompt.txt"
    cp "$OUT/$RUN-$V.log" "$D/run.log" 2>/dev/null || true
    cp "$LOG/$(basename "$Q").jsonl" "$D/hooks.jsonl" 2>/dev/null || true
    jq -c '{e: .hook_event_name, cwd, agent: .agent_id, tool: .tool_name,
            in: (.tool_input | if . == null then null else (.command // .file_path // .name // .action // .isolation) end)}' \
        "$D/hooks.jsonl" >"$D/hooks-short.jsonl" 2>/dev/null || true
    calu sessions "$sid" >"$D/calu-sessions.txt" 2>&1 || true
    (cd /tmp && sudo -u postgres psql -d calu -c \
        "select s.seq, s.agent_id is not null as sub, s.tool_name, left(coalesce(s.tool_input->>'command', s.tool_input->>'file_path', s.tool_input::text), 90) as input, s.rules_outcome, s.decision, left(s.reason, 300) as reason, s.wrote from steps s where s.session_id = '$sid' order by s.seq" \
        -c "select s.seq, c.checker_name, c.decision, left(c.reason, 300) as reason from checker_runs c join steps s on s.id = c.step_id where s.session_id = '$sid' order by s.seq, c.id" \
        -c "select id, state, left(reason, 200) from escalations where session_id = '$sid'" \
        -c "select state, ended_at from sessions where id = '$sid'") >"$D/db.txt" 2>&1 || true
    find "$PROBE" -name '*.jsonl' -newer "$OUT/$RUN-$V.txt" -exec cat {} + >"$D/probe.jsonl" 2>/dev/null || true
    find "$Q/agent/traces" -name '*.md' -newer "$OUT/$RUN-$V.txt" -exec cp {} "$D/" \; 2>/dev/null || true
    {
        echo "== git worktree list"; git -C "$Q" worktree list
        echo "== git log main"; git -C "$Q" log --oneline -5 main
        for w in $(git -C "$Q" worktree list --porcelain | sed -n 's/^worktree //p'); do
            echo "== status $w"; git -C "$w" status --short --ignored
        done
    } >"$D/git.txt" 2>&1
    echo "collected to $D"
    ;;
probe-on)
    SP="$("$TOOL_PY" -c 'import site; print(site.getsitepackages()[0])')"
    cp "$HERE/worktrees_probe.py" "$SP/calu_wtl_probe.py"
    echo "import calu_wtl_probe" >"$SP/zz_calu_wtl_probe.pth"
    echo "probe installed in $SP"
    ;;
probe-off)
    SP="$("$TOOL_PY" -c 'import site; print(site.getsitepackages()[0])')"
    rm -f "$SP/calu_wtl_probe.py" "$SP/zz_calu_wtl_probe.pth"
    echo "probe removed"
    ;;
leakcheck)
    (cd /tmp && "$TOOL_PY" -I "$HERE/worktrees_leakcheck.py" "$SHARE")
    ;;
*)
    echo "usage: $0 setup|s1|s2|s3|s4|wait|collect V [RUN] | probe-on|probe-off|leakcheck" >&2
    exit 2
    ;;
esac
