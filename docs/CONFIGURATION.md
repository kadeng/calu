# calu configuration

Every value below is a row in the database, not a file in the project: a session may read the
configuration and may not change it (`PLAN.md` §6.2). Read and change it with the `admin`
credential:

```bash
calu config get                                  # everything the global scope stores
calu config get claude.sandbox                   # one subtree
calu config set claude.model opus                # one value
calu config add claude.sandbox.deny_read ~/x     # one entry of a list, keeping the others
calu config remove claude.sandbox.deny_read ~/x
calu config unset claude.model                   # back to the default below
calu config set claude.model opus --project demo # the project's scope instead of the global one
```

A project's scope overrides the global one, and both override the defaults listed here. The
four `*_checker` groups have one more source below them: the checker definition
(`calu checker set <name> --model ...`) provides the model and the limits, and a value set here
overrides it -- so `calu config set command_checker.model` wins over the definition's own
model, per checker field. The web
interface has the same settings as a form (`calu web`, "Settings"). A running session applies a
change at its next step; what Claude Code reads when it starts -- its model, effort and
permission mode -- applies to the next session.

This file is generated from `calu.config` by `calu.configdoc`; a test keeps the two in step.

## Settings

| Key | Type | Default | What it does |
|---|---|---|---|
| `command_checker` | CheckerConfig | see below | The checker that reviews Bash commands and every tool call routed to it. |
| `command_checker.model` | text | `"mistral:mistral-small-2603"` | Pydantic AI model string of this checker, e.g. "mistral:mistral-small-2603", "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or "bedrock:eu.anthropic....". |
| `command_checker.effort` | one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (or empty) | (empty) | Thinking effort: "off" for none, "low" to "max", or None for the model's default. More effort buys accuracy with latency and tokens. |
| `command_checker.request_limit` | int | `3` | Maximum model requests for one check, including the tool calls a checker makes. |
| `command_checker.output_tokens_limit` | int | `8000` | Token budget for all answers of one check (thinking included on most providers). A check that hits it is retried once, then escalated. |
| `command_checker.max_tokens` | int | `4000` | Token cap for one answer of this checker. |
| `edit_checker` | CheckerConfig | see below | The checker that reviews a file edit before it is written. |
| `edit_checker.model` | text | `"mistral:mistral-small-2603"` | Pydantic AI model string of this checker, e.g. "mistral:mistral-small-2603", "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or "bedrock:eu.anthropic....". |
| `edit_checker.effort` | one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (or empty) | (empty) | Thinking effort: "off" for none, "low" to "max", or None for the model's default. More effort buys accuracy with latency and tokens. |
| `edit_checker.request_limit` | int | `3` | Maximum model requests for one check, including the tool calls a checker makes. |
| `edit_checker.output_tokens_limit` | int | `8000` | Token budget for all answers of one check (thinking included on most providers). A check that hits it is retried once, then escalated. |
| `edit_checker.max_tokens` | int | `4000` | Token cap for one answer of this checker. |
| `subagent_checker` | CheckerConfig | see below | The checker that reviews what a subagent hands back. |
| `subagent_checker.model` | text | `"mistral:mistral-small-2603"` | Pydantic AI model string of this checker, e.g. "mistral:mistral-small-2603", "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or "bedrock:eu.anthropic....". |
| `subagent_checker.effort` | one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (or empty) | (empty) | Thinking effort: "off" for none, "low" to "max", or None for the model's default. More effort buys accuracy with latency and tokens. |
| `subagent_checker.request_limit` | int | `3` | Maximum model requests for one check, including the tool calls a checker makes. |
| `subagent_checker.output_tokens_limit` | int | `8000` | Token budget for all answers of one check (thinking included on most providers). A check that hits it is retried once, then escalated. |
| `subagent_checker.max_tokens` | int | `4000` | Token cap for one answer of this checker. |
| `changeset_checker` | CheckerConfig | see below | The checker that reviews a whole change before a commit (PLAN.md D26). |
| `changeset_checker.model` | text | `"mistral:mistral-small-2603"` | Pydantic AI model string of this checker, e.g. "mistral:mistral-small-2603", "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or "bedrock:eu.anthropic....". |
| `changeset_checker.effort` | one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (or empty) | (empty) | Thinking effort: "off" for none, "low" to "max", or None for the model's default. More effort buys accuracy with latency and tokens. |
| `changeset_checker.request_limit` | int | `3` | Maximum model requests for one check, including the tool calls a checker makes. |
| `changeset_checker.output_tokens_limit` | int | `8000` | Token budget for all answers of one check (thinking included on most providers). A check that hits it is retried once, then escalated. |
| `changeset_checker.max_tokens` | int | `4000` | Token cap for one answer of this checker. |
| `check_subagent_results` | bool | `true` | Review the final result of every subagent with the subagent checker. |
| `auto_escalate` | AutoEscalate | see below | Categories that always wait for the human (see `AutoEscalate`); all off by default. |
| `auto_escalate.commands` | bool | `false` | Every command waits for a human, even one the command checker approved. |
| `auto_escalate.edits` | bool | `false` | Every file edit waits for a human, even one the edit checker approved. |
| `auto_escalate.subagents` | bool | `false` | Every subagent's final result waits for a human, even an accepted one. |
| `escalation_timeout_s` | number | `28800` | How long an escalation waits for a human before the session stops (PLAN.md D27). An unsupervised run can sit in front of an escalation for a long time; at some point the session should give the machine and the Claude session back instead of waiting for ever. On the timeout the step is denied and the session stops, with Claude's session id recorded, so that `calu run --resume <id>` takes the conversation up again. 0 waits without a limit. |
| `store_failures_before_stop` | int | `10` | How many database writes may fail in a row before the session stops (PLAN.md D59). A session that cannot write is a session nobody can watch or unblock: its steps, its turns and its escalations do not reach the database, and with the heartbeat of D56 the fleet shows it as gone. Stopping at the first failure would make a network hiccup end an overnight run, so failures are tolerated and counted, and the count resets on the next write that works. 0 never stops, which keeps a session running blind -- only sensible when somebody is attached to it. |
| `tool_routes` | map of text to one of `command`, `edit`, `changeset`, `allow`, `escalate`, `subagent`, `question`, `deny` | (empty) | Route per tool name, overriding the built-in table (`calu.checkers.policy`), e.g. `{"mcp__deploy__ship": "escalate"}`. |
| `default_route` | one of `command`, `edit`, `changeset`, `allow`, `escalate`, `subagent`, `question`, `deny` | `"command"` | Route for a tool the built-in table does not know, including MCP tools and tools a newer Claude Code adds. "command" means such a call is checked rather than allowed. |
| `context` | ContextConfig | see below | What the checkers read as context, and how the conversation is folded. |
| `context.fold_every` | int | `30` | Fold the oldest turns into the chat summary once this many turns are unfolded. |
| `context.fold_count` | int | `20` | How many of the oldest turns are folded into the chat summary each time. |
| `context.summarizer_model` | text | `"mistral:ministral-3b-latest"` | Model that writes the chat summary when turns are folded. |
| `context.narrate` | bool | `true` | Narrate Claude's thinking and text for the checkers (`calu.context.narrator`). |
| `context.narrator_model` | text | `"mistral:mistral-small-2603"` | Model that narrates Claude's thinking and text in indirect speech, so that a checker does not read it as an instruction. |
| `context.max_recent_chars` | int | `40000` | Upper bound in characters for the unfolded turns in a checker's context. |
| `context.files` | list of text | `["AGENTS.md", "CLAUDE.md", "GLOSSARY.md", "MEMORY.md"]` | Workspace-relative files included verbatim in the cached context prefix where they exist, so the checkers read the project's own instructions. |
| `context.skill_dirs` | list of text | `[".claude/skills"]` | Workspace-relative directories whose skills contribute their name and description to the context prefix. |
| `claude` | ClaudeConfig | see below | The Claude Code session itself: model, effort, permission mode, sandbox. |
| `claude.use_bedrock` | bool | `false` | Set `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it runs on AWS Bedrock with the environment's AWS credentials. False leaves the variable as it is; calu does not otherwise touch how Claude Code authenticates. |
| `claude.permission_mode` | one of `default`, `auto`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions` (or empty) | (empty) | Permission mode for Claude Code; None leaves it to Claude Code (its default is "auto"). |
| `claude.stream_partial` | bool | `true` | Stream text and thinking token by token (`assistant.delta`). False delivers completed blocks only. |
| `claude.thinking_display` | one of `summarized`, `omitted` | `"summarized"` | How Claude Code returns thinking: "summarized" (readable summaries, shown by front ends as `assistant.thinking`) or "omitted" (empty thinking blocks, nothing is shown). |
| `claude.model` | text (or empty) | `"opus"` | Claude Code model alias ("fable", "opus", "sonnet", "haiku": always the latest of that family) or a full model id; None uses Claude Code's default. |
| `claude.effort` | one of `low`, `medium`, `high`, `xhigh`, `max` (or empty) | (empty) | Reasoning effort of the Claude session ("low" to "max"); None uses Claude Code's default. `calu run --effort` overrides it for one session. |
| `claude.cli_path` | text (or empty) | (empty) | Path to the `claude` executable; None looks it up on PATH. |
| `claude.setting_sources` | list of text | `["project"]` | Which of Claude Code's own settings files it may read ("user", "project", "local"). calu's `--settings` document overrides them for the sandbox either way. |
| `claude.sandbox` | SandboxConfig | see below | Isolation of Bash commands (PLAN.md D15, D4). |
| `claude.sandbox.enabled` | bool | `true` | Isolate Bash commands with Claude Code's own sandbox. Off removes that isolation and leaves only the checkers and the deny lists. |
| `claude.sandbox.fail_if_unavailable` | bool | `true` | Refuse to start when no sandbox can be built, instead of running commands unsandboxed. |
| `claude.sandbox.allow_unsandboxed_commands` | bool | `false` | Let a call leave the sandbox with `dangerouslyDisableSandbox`. False keeps it in. |
| `claude.sandbox.auto_allow_bash` | bool | `false` | Let Claude Code approve sandboxed Bash calls by itself. Stays False: it would skip auto mode's classifier, which PLAN.md D5 uses as the second opinion next to the checkers. |
| `claude.sandbox.weaker_nested_sandbox` | bool | `false` | Weaker sandbox inside unprivileged Docker containers (Linux); reduces the isolation. |
| `claude.sandbox.excluded_commands` | list of text | (empty) | Commands that run outside the sandbox, covered only by the checkers and the deny lists. |
| `claude.sandbox.deny_read` | list of text | 15 entries (see `calu config get`) | Paths a sandboxed command must not read (see `DEFAULT_DENY_READ`). Edit single entries with `calu config add|remove claude.sandbox.deny_read <path>`. |
| `claude.sandbox.allowed_domains` | list of text | (empty) | Domains a sandboxed command may reach through the sandbox's HTTP proxy; `["*"]` is every domain and an empty list is none. A project's network policy sets this (PLAN.md D39, D45); raw TCP is blocked whatever it says. |
| `claude.sandbox.denied_domains` | list of text | (empty) | Domains that stay blocked even when `allowed_domains` holds the wildcard, which is how "everything except these" is written. |
| `claude.sandbox.allow_unix_sockets` | list of text | (empty) | Unix sockets a sandboxed command may connect to (e.g. a local database socket). |
| `clock` | ClockConfig | see below | Watching the system clock for tampering (PLAN.md D32). |
| `clock.check_interval_s` | number | `10.0` | Seconds between samples; 0 switches the watch off. The interval only decides how soon a jump is noticed, not whether it is: each sample is compared with the previous one. It is also how often the session tells the database that it is still running, and a herd calls a session stale after 180 s of silence, so an interval near that threshold makes a running session look dead in `calu sessions` (the start warns about it). |
| `clock.max_backward_s` | number | `1.0` | A backward move up to this size counts as a time-server correction. Above it, the session escalates its next tool call, because the attribution of file changes is no longer sound. |
| `clock.max_forward_s` | number | `300.0` | A forward move above this size is logged (a first time-server sync after a boot jumps forward legitimately, so it is not an escalation). |
| `changes` | ChangesConfig | see below | Measuring what a command wrote, and reviewing it afterwards (PLAN.md D31). |
| `changes.snapshot` | bool | `false` | Take a git snapshot of the project directory before and after every Bash step. |
| `changes.review` | bool | `false` | Let a checker judge what a command wrote, once it has run, and undo the change if it refuses (needs `snapshot`). The agent waits for that review, so a refusal is not a comment on something that already stands: the files go back to the state from before the command. |
| `changes.max_file_bytes` | int | `1000000` | Files above this size are reported by path and size, but their content is not shown. |
| `changes.max_patch_bytes` | int | `200000` | Upper bound on the patch a checker or a client ever sees. |
| `changes.always_capture` | list of text | `[".env"]` | Paths to snapshot even though a `.gitignore` covers them, so that a write to them is noticed; they are the ones that matter most and the ones git would otherwise hide. |
| `web` | WebConfig | see below | Settings of the web interface. |
| `web.git_url_template` | text | `""` | URL of one commit in whatever git browser the project uses, with `{commit}` and `{remote}` as placeholders, e.g. "http://localhost:8080/calu/commit/{commit}". Empty guesses the URL from the project's git remote, which works for GitHub, GitLab and Bitbucket; calu does not browse git itself. |
