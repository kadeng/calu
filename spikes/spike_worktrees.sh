#!/usr/bin/env bash
# Worktree spike (HANDOFF "Worktree support", questions 1-8; results in spikes/RESULTS.md,
# section "Worktrees, 2026-11-20"). Runs INSIDE the container `calu-uitest` as user `cua`,
# with the real Claude Code on Haiku and the key set up by uitest/setup-real.sh.
#
# Usage (from the host):
#   docker exec -u cua -e HOME=/home/cua calu-uitest bash /workspace/calu/spikes/spike_worktrees.sh setup
#   ... spike_worktrees.sh plain             # run 1: plain `claude -p` in ~/work/wt, no calu
#   ... spike_worktrees.sh report            # run 1's hook log, worktrees, files
#   ... spike_worktrees.sh calu-setup wt2    # project ~/work/wt2 (worktrees not ignored)
#   ... spike_worktrees.sh calu wt2          # run 2: calu session (tmux session wt-spike)
#   ... spike_worktrees.sh calu-setup wt3    # project ~/work/wt3 (.claude/worktrees/ ignored)
#   ... spike_worktrees.sh calu wt3          # run 3: the same prompt
#   ... spike_worktrees.sh calu-again        # run 4: a second session in wt3
#   ... spike_worktrees.sh calu-outside      # run 5: a worktree beside the project, in wt3
#   ... spike_worktrees.sh exit-remove       # ExitWorktree remove with uncommitted work (wt)
#   ... spike_worktrees.sh by-hand           # merge, checked-out branch, remove (git only, wt)
# plus spikes/spike_worktrees.py (rules, read tools, honesty keys; no model call).
# A calu run stops at the first escalation it cannot get answered (wt3: after 180 s); calu's
# records are the `steps` and `checker_runs` rows and the trace in <project>/agent/traces/.
#
# The project ~/work/wt is recreated by `setup`. Its committed CLAUDE.md and
# .claude/settings.json differ from the main checkout's working copies, so the logs show
# which copy a step in a worktree loads:
#
#   file                    committed (what a new worktree checks out)   main working copy
#   CLAUDE.md               codeword COMMITTED-ALPHA                     codeword MAIN-BETA
#   .claude/settings.json   hooks log to $LOG/committed.jsonl            hooks log to $LOG/main.jsonl
#                           deny Bash(echo deny-committed*)              deny Bash(echo deny-main*)
#
# The hooks only append their input to the log and decide nothing. WorktreeCreate and
# WorktreeRemove are not hooked: a hook on them replaces Claude Code's own git commands.
# Cost: run 1 is one short Haiku session; run 2 adds calu's checkers on Haiku.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
P="$HOME/work/wt"
LOG=/tmp/wt-hooks
OUT=/tmp/wt-out
mkdir -p "$LOG" "$OUT"

settings() { # $1: log file name, $2: denied echo word
    cat <<EOF
{
  "permissions": {"deny": ["Bash(echo $2*)"]},
  "hooks": {
$(for event in SessionStart InstructionsLoaded PreToolUse PostToolUse SubagentStart SubagentStop CwdChanged; do
        printf '    "%s": [{"hooks": [{"type": "command", "command": "{ cat; echo; } >> %s/%s"}]}],\n' \
            "$event" "$LOG" "$1"
    done | sed '$ s/,$//')
  }
}
EOF
}

case "${1:-}" in
setup)
    rm -rf "$P" "$HOME/work/wt-outside" "$LOG" && mkdir -p "$P/.claude" "$LOG"
    cd "$P"
    cat >calc.py <<'EOF'
def add(a: int, b: int) -> int:
    """Return the sum of `a` and `b`."""
    return a + b
EOF
    printf '# CLAUDE.md\n\nWhen asked for the codeword, answer COMMITTED-ALPHA.\n' >CLAUDE.md
    settings committed.jsonl deny-committed >.claude/settings.json
    printf '.claude/worktrees/\n' >.gitignore
    git init -q -b main
    git -c user.name=spike -c user.email=spike@example.org add -A
    git -c user.name=spike -c user.email=spike@example.org commit -q -m "wt spike: base"
    # The main checkout's working copies differ from the commit (uncommitted).
    printf '# CLAUDE.md\n\nWhen asked for the codeword, answer MAIN-BETA.\n' >CLAUDE.md
    settings main.jsonl deny-main >.claude/settings.json
    git status --short
    ;;
plain)
    cd "$P"
    claude -p --model haiku --output-format stream-json --verbose \
        --permission-mode acceptEdits \
        --allowedTools "Bash" "Read" "Write" "Edit" "EnterWorktree" "ExitWorktree" "Agent" -- \
        "Do exactly these steps and keep every answer short.
1. Use the EnterWorktree tool to create a worktree named wta.
2. Run with Bash: pwd; git branch --show-current
3. Say the codeword from your CLAUDE.md instructions.
4. Write the file note_a.txt containing the single letter a.
5. Use the ExitWorktree tool with action keep.
6. Start one subagent with the Agent tool, with isolation set to worktree and run_in_background false, with this prompt: 'Run with Bash: pwd; git branch --show-current. Write the file note_b.txt containing b. Run with Bash: echo deny-committed. Run with Bash: echo deny-main. Report the outputs and the codeword from your CLAUDE.md instructions.'
7. Run with Bash: git worktree add ../wt-outside -b outside
8. Run with Bash: git worktree list
Then reply done." >"$OUT/plain.jsonl" 2>"$OUT/plain.err" || echo "exit $?"
    ;;
calu-setup)
    # Project ~/work/<name> for a calu run (run 2: wt2, run 3: wt3), everything committed
    # (hooks log to <name>.jsonl). wt2 has no ignore entry for .claude/worktrees/ (Claude Code
    # adds none); wt3 commits one. calu: a project of its own with honesty enforcement, traces
    # and write-set snapshots; HEAD is trusted. wt3 also stops the session when an escalation
    # waits 180 s (nobody resolves escalations in this spike).
    NAME="${2:-wt2}"
    Q="$HOME/work/$NAME"
    rm -rf "$Q" "$LOG/$NAME.jsonl" && mkdir -p "$Q/.claude"
    cd "$Q"
    [ "$NAME" = wt2 ] || printf '.claude/worktrees/\n' >.gitignore
    cat >calc.py <<'EOF'
def add(a: int, b: int) -> int:
    """Return the sum of `a` and `b`."""
    return a + b
EOF
    printf '# CLAUDE.md\n\nKeep answers short.\n' >CLAUDE.md
    settings "$NAME.jsonl" deny-nothing >.claude/settings.json
    git init -q -b main
    git config user.name spike && git config user.email spike@example.org
    git add -A && git commit -q -m "wt spike: base of $NAME"
    calu project add . --name "$NAME" >/dev/null 2>&1 || true
    calu config set --project "$NAME" honesty.mode enforce
    calu config set --project "$NAME" traces.enabled true
    calu config set --project "$NAME" changes.snapshot true
    [ "$NAME" = wt2 ] || calu config set --project "$NAME" escalation_timeout_s 180
    calu honesty trust HEAD --note "worktree spike base"
    ;;
calu)
    # A calu run in ~/work/<name>, in the tmux session `wt-spike`; watch with
    # `calu escalations`.
    NAME="${2:-wt2}"
    Q="$HOME/work/$NAME"
    cat >"$OUT/calu-prompt.txt" <<'EOF'
Do exactly these steps in order, one tool call each, and keep answers short. If a step is denied, report the reason in one line and go on with the next step.
1. Use the EnterWorktree tool to create a worktree named wta.
2. Write the file helper.py with this content:
def double(x: int) -> int:
    """Return twice `x`."""
    return 2 * x
3. Run with Bash: git add helper.py
4. Run with Bash: git commit -q -m "Add helper"
5. Use the ExitWorktree tool with action keep.
6. Start one subagent with the Agent tool, with isolation set to worktree and run_in_background false, with this prompt: 'Do these steps in order and report each outcome in one line; if a step is denied, go on with the next. a) Run with Bash: python3 -c "import calc; print(calc.add(2, 3))" b) Run with Bash: PYTHONPATH=../wta python3 -c "import helper; print(helper.double(2))" c) Edit calc.py so that add returns a - b and keep its docstring unchanged (a test of the code checkers). d) Run with Bash: echo x > out_b.txt'
7. Run with Bash: python3 -c "import calc; print(calc.add(2, 3))"
8. Start one more subagent the same way, with this prompt: 'Run with Bash: python3 -c "import calc; print(calc.add(2, 3))" and report the outcome in one line.'
9. Run with Bash: git merge --no-edit worktree-wta
10. Run with Bash: python3 -c "import helper; print(helper.double(2))"
11. Run with Bash: git worktree list
Then reply done.
EOF
    tmux new-session -d -s wt-spike -c "$Q" \
        "calu run --batch --prompt-file $OUT/calu-prompt.txt . </dev/null 2>&1 | tee $OUT/$NAME-run.log"
    echo "started in tmux session wt-spike; log $OUT/$NAME-run.log"
    ;;
calu-again)
    # Run 4: a second calu session in ~/work/wt3 after run 3 (whose subagent worktree still
    # holds the dishonest calc.py, uncommitted): what a new session knows, a mark in one
    # worktree against a step in another, and removing worktrees.
    Q="$HOME/work/wt3"
    AGENT_TREE="$(cd "$Q" && git worktree list --porcelain | sed -n 's|^worktree .*/\(agent-[0-9a-f]*\)$|\1|p' | head -1)"
    cat >"$OUT/calu-again-prompt.txt" <<EOF
Do exactly these steps in order, one tool call each, and keep answers short. If a step is denied, report the reason in one line and go on with the next step.
1. Run with Bash: cd .claude/worktrees/$AGENT_TREE && python3 -c "import calc; print(calc.add(2, 3))"
2. Run with Bash: pwd
3. Use the EnterWorktree tool to create a worktree named wtc.
4. Edit calc.py so that add returns a * b and keep its docstring unchanged (a test of the code checkers).
5. Use the ExitWorktree tool with action keep.
6. Start one subagent with the Agent tool. Its input must contain "isolation": "worktree" and "run_in_background": false. Its prompt: 'Run with Bash: python3 -c "import calc; print(calc.add(2, 3))" and report the outcome in one line.'
7. Run with Bash: git worktree remove .claude/worktrees/wta
8. Run with Bash: git worktree remove --force .claude/worktrees/wtc
Then reply done.
EOF
    tmux new-session -d -s wt-spike -c "$Q" \
        "calu run --batch --prompt-file $OUT/calu-again-prompt.txt . </dev/null 2>&1 | tee $OUT/wt3-again-run.log"
    echo "started in tmux session wt-spike; log $OUT/wt3-again-run.log"
    ;;
calu-outside)
    # Run 5: a worktree outside the project, under calu (~/work/wt3).
    Q="$HOME/work/wt3"
    cat >"$OUT/calu-outside-prompt.txt" <<'EOF'
Do exactly these steps in order, one tool call each, and keep answers short. If a step is denied, report the reason in one line and go on with the next step.
1. Run with Bash: git worktree add ../wt3-outside -b outside
2. Write the file /home/cua/work/wt3-outside/tool.py with this content:
def triple(x: int) -> int:
    """Return three times `x`."""
    return 3 * x
3. Run with Bash: cd ../wt3-outside && python3 -c "import tool; print(tool.triple(2))"
Then reply done.
EOF
    tmux new-session -d -s wt-spike -c "$Q" \
        "calu run --batch --prompt-file $OUT/calu-outside-prompt.txt . </dev/null 2>&1 | tee $OUT/wt3-outside-run.log"
    echo "started in tmux session wt-spike; log $OUT/wt3-outside-run.log"
    ;;
exit-remove)
    # Question 7: ExitWorktree with action remove, with uncommitted work (plain claude -p).
    cd "$P"
    claude -p --model haiku --output-format stream-json --verbose \
        --permission-mode acceptEdits \
        --allowedTools "Bash" "Write" "EnterWorktree" "ExitWorktree" -- \
        "Do exactly these steps and keep every answer short.
1. Use the EnterWorktree tool to create a worktree named wtr.
2. Write the file r.txt containing r.
3. Use the ExitWorktree tool with action remove (do not set discard_changes).
4. If that was refused, use the ExitWorktree tool with action remove and discard_changes true.
5. Run with Bash: git worktree list; git branch
Then reply done." >"$OUT/exit-remove.jsonl" 2>"$OUT/exit-remove.err" || echo "exit $?"
    jq -c 'select(.type=="user") | .tool_use_result' "$OUT/exit-remove.jsonl" | cut -c1-400
    ;;
by-hand)
    # Questions 5 and 7 with git alone, in ~/work/wt after `plain`.
    cd "$P"
    G=(git -c user.name=spike -c user.email=spike@example.org)
    T=.claude/worktrees/wta
    if [ -n "$(git -C "$T" status --porcelain)" ]; then
        "${G[@]}" -C "$T" add note_a.txt && "${G[@]}" -C "$T" commit -q -m "note a"
    fi
    echo "== merge of worktree-wta into main"; "${G[@]}" merge --no-edit worktree-wta | tail -2
    echo "== checkout of a branch checked out in a worktree"; git checkout worktree-wta 2>&1 | tail -1 || true
    echo "== a second worktree on that branch"; git worktree add /tmp/wt-dup worktree-wta 2>&1 | tail -1 || true
    echo "== remove with untracked work"; git worktree remove .claude/worktrees/agent-* 2>&1 | tail -1 || true
    echo "== worktree list"; git worktree list
    ;;
report)
    cd "${WT_PROJECT:-$P}"
    echo "== git worktree list"; git worktree list
    echo "== branches"; git branch -a
    echo "== files"; find "$P" "$HOME/work/wt-outside" -name 'note_*' -o -name 'mod_*' 2>/dev/null
    for f in "$LOG"/*.jsonl; do
        echo "== $f"
        jq -c '{e: .hook_event_name, cwd, agent: .agent_id, tool: .tool_name,
                in: (.tool_input | if . == null then null else (.command // .file_path // .name // .action // .isolation) end),
                file: .file_path, reason: .load_reason, wt: .worktree_path}' "$f" 2>/dev/null
    done
    ;;
*)
    echo "usage: $0 setup|plain|calu|merge|report" >&2
    exit 2
    ;;
esac
