"""Session configuration, built from the database (PLAN.md D6).

Sources, later ones override earlier ones:

    Config() defaults
    config rows, scope "global"         key "settings", value: a partial `Config` as JSON
    config rows, scope "project"        same, scope_id = project name
    checker_types rows                  model and limits of the command, edit and
                                        subagent-result checkers and the narrator model

Nested objects merge key by key (see `merge_settings`); lists and scalars are replaced. The
checker instructions themselves are not part of `Config`; the session passes them to the
checkers directly.

Checker API keys are not configuration: calu reads them from its own environment and from
`~/.config/calu/.env` (`load_secrets`), and hides them from Claude Code.
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from calu.checkers.model import CheckerEffort
from calu.checkers.policy import Route

EMPTY_CHAT_SUMMARY = "# Chat summary\n\nNo turns have been folded into this summary yet.\n"
"""Chat summary of a session without folded turns."""

SETTINGS_KEY = "settings"
"""Key of the `config` rows that hold a partial `Config`."""


class ConfigModel(BaseModel):
    """Base of the configuration models: the docstring under a field is its description.

    Pydantic then carries the text into `FieldInfo.description`, which is what the generated
    reference (`calu.configdoc`, `docs/CONFIGURATION.md`), the JSON schema of the protocol and
    the web interface's settings form read. A field without a docstring is therefore a field
    nobody can look up, so every one of them has one.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)


class CheckerConfig(ConfigModel):
    """Model, thinking effort and limits of one checker. `model` is a Pydantic AI model string.

    `max_tokens` caps one answer, `output_tokens_limit` all answers of one check (thinking
    included on most providers). A check that hits either is retried once, then escalated.
    `effort` buys accuracy with latency and tokens, so a cheap checker on a simple route can run
    with "off" while the changeset checker thinks.
    """

    model: str = "mistral:mistral-small-2603"  # Mistral Small 4
    """Pydantic AI model string of this checker, e.g. "mistral:mistral-small-2603",
    "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or "bedrock:eu.anthropic....".
    `claude:<alias>` ("claude:haiku", "claude:sonnet") is special: the check then runs through
    the `claude` CLI on this machine's own Claude Code account, which is the only way a
    claude.ai subscription can answer a check and needs no API key (PLAN.md D117). That model
    type has **no tools**, so such a checker cannot read files, glob or grep."""
    effort: CheckerEffort | None = None
    """Thinking effort: "off" for none, "low" to "max", or None for the model's default.
    More effort buys accuracy with latency and tokens."""
    request_limit: int = 5
    """Maximum model requests for one check, including the tool calls a checker makes. Five
    covers the request itself, two tool calls and one retry after an answer that was too long;
    with less, a check that uses its tools and then retries fails instead of deciding
    (measured, PLAN.md D72)."""
    output_tokens_limit: int = 24_000
    """Token budget for all answers of one check (thinking included on most providers).
    A check that hits it is retried once, then escalated. It holds three times `max_tokens`,
    because a budget that one maximal answer exhausts leaves no room for that retry."""
    max_tokens: int = 8_000
    """Token cap for one answer of this checker. On a provider that counts thinking towards the
    answer, a reasoning model can spend several thousand tokens before writing its decision, so
    a lower cap makes it fail rather than decide (measured, PLAN.md D72)."""


class ContextConfig(ConfigModel):
    """Context summary settings (see `calu.context`).

    Every `fold_every` unfolded turns, the oldest `fold_count` turns are folded into
    CHAT_SUMMARY.md. `files` are workspace-relative files included verbatim in the cached context
    prefix when they exist; `skill_dirs` contribute the name and description of each skill.
    """

    fold_every: int = Field(default=30, ge=2)
    """Fold the oldest turns into the chat summary once this many turns are unfolded."""
    fold_count: int = Field(default=20, ge=1)
    """How many of the oldest turns are folded into the chat summary each time."""
    summarizer_model: str = "mistral:ministral-3b-latest"
    """Model that writes the chat summary when turns are folded."""
    narrate: bool = True
    """Narrate Claude's thinking and text for the checkers (`calu.context.narrator`)."""
    narrator_model: str = "mistral:mistral-small-2603"  # Mistral Small 4
    """Model that narrates Claude's thinking and text in indirect speech, so that a checker
    does not read it as an instruction."""
    max_recent_chars: int = Field(default=40_000, ge=1_000)
    """Upper bound in characters for the unfolded turns in a checker's context."""
    files: list[str] = Field(
        default_factory=lambda: ["AGENTS.md", "CLAUDE.md", "GLOSSARY.md", "MEMORY.md"]
    )
    """Workspace-relative files included verbatim in the cached context prefix where they
    exist, so the checkers read the project's own instructions."""
    skill_dirs: list[str] = Field(default_factory=lambda: [".claude/skills"])
    """Workspace-relative directories whose skills contribute their name and description to
    the context prefix."""


DEFAULT_DENY_READ = (
    "~/.config/calu",
    "~/.local/state/calu",
    "~/.claude/.credentials.json",
    "~/.claude.json",
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.gnupg",
    "~/.config/gh",
    "~/.npmrc",
    "~/.pypirc",
)
"""Paths that sandboxed commands must not read (PLAN.md D15): calu's encrypted credentials,
Claude Code's own credentials, SSH, cloud and registry credentials. Claude Code's built-in
defaults additionally deny the container sockets, `/run/dbus` and `/run/user`."""


DEFAULT_ALLOWED_DOMAINS = (
    # Python
    "pypi.org",
    "files.pythonhosted.org",
    # JavaScript
    "registry.npmjs.org",
    # Rust
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    # Go
    "proxy.golang.org",
    "sum.golang.org",
    # git over HTTPS and the archives a checkout or an install pulls
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    # Debian and Ubuntu packages
    "deb.debian.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
)
"""What a coding task normally has to reach: package registries, git over HTTPS, OS packages.

`calu project add` writes this into a new project's settings as the starter allow list of the
"proxy" policy (PLAN.md D45), where `calu config add|remove claude.sandbox.allowed_domains ...`
edits it one entry at a time. It is deliberately **not** the default of
`SandboxConfig.allowed_domains`: a session whose project says nothing, or that runs without a
project at all, reaches nothing, and that is the side to err on.

Claude Code itself runs outside the sandbox (D4), so the model API is not in this list; only the
commands Claude runs are limited by it.
"""


DEFAULT_NETWORK_POLICY = "proxy"
"""The policy a new project gets (PLAN.md D45, proposed; Q5 asks the owner to confirm it).

"proxy" together with `DEFAULT_ALLOWED_DOMAINS` lets a session install packages and use git
over HTTPS and reach nothing else. `calu project add --network full` gives a project the old
behaviour, and `calu project network --name <p> --network <policy>` changes it later.
"""

NetworkPolicy = Literal["none", "proxy", "full"]
"""What a project's commands may reach (`projects.network_policy`, PLAN.md §7 and D39).

Measured with the real CLI (`spikes/RESULTS.md`, "The sandbox's network policy"): the sandbox
runs a filtering HTTP proxy and exports it as `HTTPS_PROXY`, and raw TCP is blocked whatever the
policy says. So the three policies are expressed through the proxy's allow list:

    none    no `network` key at all: nothing is reachable, not even through the proxy
    proxy   `allowedDomains` = the configured list: only those domains
    full    `allowedDomains` = ["*"]: everything, minus `deniedDomains`, which still wins
"""


class SandboxConfig(ConfigModel):
    """Isolation of Bash commands by Claude Code's own sandbox (PLAN.md D15).

    calu passes these settings with `--settings`, which overrides the project's and the user's
    settings files, so the agent cannot switch the sandbox off by writing
    `.claude/settings.json`. The sandbox needs `bwrap` and `socat` on the machine
    (`calu.preflight`).

    `fail_if_unavailable` makes Claude Code refuse to start without a working sandbox, instead
    of running commands unsandboxed. `allow_unsandboxed_commands` False stops a call from
    leaving the sandbox with `dangerouslyDisableSandbox`. `auto_allow_bash` stays False: Claude
    Code would otherwise approve sandboxed Bash calls by itself, which would skip the auto-mode
    classifier that PLAN.md D5 uses as the second opinion next to the checkers.

    `excluded_commands` run outside the sandbox and are therefore only covered by the checkers
    and the deny lists; the default is empty. `allowed_domains` and `denied_domains` configure
    the sandbox's network: with an empty `allowed_domains` the sandbox blocks outgoing network
    access (verified in `spikes/RESULTS.md`).
    """

    enabled: bool = True
    """Isolate Bash commands with Claude Code's own sandbox. Off removes that isolation and
    leaves only the checkers and the deny lists."""
    fail_if_unavailable: bool = True
    """Refuse to start when no sandbox can be built, instead of running commands unsandboxed."""
    allow_unsandboxed_commands: bool = False
    """Let a call leave the sandbox with `dangerouslyDisableSandbox`. False keeps it in."""
    auto_allow_bash: bool = False
    """Let Claude Code approve sandboxed Bash calls by itself. Stays False: it would skip
    auto mode's classifier, which PLAN.md D5 uses as the second opinion next to the
    checkers."""
    weaker_nested_sandbox: bool = False
    """Claude Code's `enableWeakerNestedSandbox`. Measured in calu's own container (PLAN.md
    D134): it does **not** make the sandbox work under Docker's default seccomp profile --
    bubblewrap still cannot create a user namespace there, so the container needs
    `seccomp:unconfined` either way -- and where the namespaces do work, calu's probe saw no
    difference in isolation. There is no known reason to switch it on."""
    excluded_commands: list[str] = Field(default_factory=list)
    """Commands that run outside the sandbox, covered only by the checkers and the deny
    lists."""
    deny_read: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_READ))
    """Paths a sandboxed command must not read (see `DEFAULT_DENY_READ`). Edit single
    entries with `calu config add|remove claude.sandbox.deny_read <path>`."""
    allowed_domains: list[str] = Field(default_factory=list)
    """Domains a sandboxed command may reach through the sandbox's HTTP proxy; `["*"]` is
    every domain and an empty list is none. A project's network policy sets this
    (PLAN.md D39, D45); raw TCP is blocked whatever it says."""
    denied_domains: list[str] = Field(default_factory=list)
    """Domains that stay blocked even when `allowed_domains` holds the wildcard, which is how
    "everything except these" is written."""
    allow_unix_sockets: list[str] = Field(default_factory=list)
    """Unix sockets a sandboxed command may connect to (e.g. a local database socket)."""

    def with_policy(self, policy: NetworkPolicy) -> SandboxConfig:
        """Return a copy whose network settings express `policy` (PLAN.md D39).

        The deny list is kept in every case: it still overrides the wildcard of "full", which is
        how "everything except these domains" is written.

        Args:
            policy: The project's network policy.

        Returns:
            A copy with `allowed_domains` set accordingly: empty for "none", the wildcard for
            "full", and unchanged for "proxy" — where an empty list means nothing is reachable,
            which `calu.preflight` warns about.
        """
        if policy == "none":
            return self.model_copy(update={"allowed_domains": []})
        if policy == "full":
            return self.model_copy(update={"allowed_domains": ["*"]})
        return self

    def settings(self) -> dict[str, Any]:
        """Return the Claude Code settings document for `--settings`.

        Returns:
            `{"sandbox": {...}}` with the keys Claude Code expects (camel case). The `network`
            and `filesystem` entries are left out when they would be empty.

        Example:
            `SandboxConfig().settings()["sandbox"]["failIfUnavailable"]` is True.
        """
        sandbox: dict[str, Any] = {
            "enabled": self.enabled,
            "failIfUnavailable": self.fail_if_unavailable,
            "allowUnsandboxedCommands": self.allow_unsandboxed_commands,
            "autoAllowBashIfSandboxed": self.auto_allow_bash,
        }
        if self.weaker_nested_sandbox:
            sandbox["enableWeakerNestedSandbox"] = True
        if self.excluded_commands:
            sandbox["excludedCommands"] = list(self.excluded_commands)
        if self.deny_read:
            sandbox["filesystem"] = {"denyRead": list(self.deny_read)}
        network: dict[str, Any] = {}
        if self.allowed_domains:
            network["allowedDomains"] = list(self.allowed_domains)
        if self.denied_domains:
            network["deniedDomains"] = list(self.denied_domains)
        if self.allow_unix_sockets:
            network["allowUnixSockets"] = list(self.allow_unix_sockets)
        if network:
            sandbox["network"] = network
        return {"sandbox": sandbox}


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Reasoning effort levels of Claude Code (`--effort`, `/effort`)."""


class ClaudeConfig(ConfigModel):
    """Settings for the Claude Code session.

    `model`: a Claude Code model alias ("fable", "opus", "sonnet", "haiku": always the latest
    version of that family) or a full model id; None uses Claude Code's default. Default: "opus".
    `effort`: reasoning effort ("low", "medium", "high", "xhigh", "max"); None uses Claude
    Code's default. Both can be changed per session (`session.start`) and during a session with
    the `/model` and `/effort` slash commands.

    Claude Code authenticates as it is configured on the machine (claude.ai login, AWS Bedrock,
    Google Vertex, Microsoft Foundry or an API key); calu does not change that. `use_bedrock`:
    if true, calu sets `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it uses AWS Bedrock
    with the AWS credentials of the environment (e.g. an EC2 instance role or `AWS_PROFILE`);
    false (default) leaves the variable as it is.

    `permission_mode`: None (default) leaves the permission mode to Claude Code, whose own
    default is the auto mode — the intended way to run calu, because then a step runs only if
    the checkers (or the allow lists) **and** auto mode's classifier approve it (PLAN.md D5).
    calu does not set the mode: changing it away from auto is a deliberate act of the user, in
    Claude Code's own settings or here. Whatever the mode ends up being, the checkers work: the
    hook reads the effective mode from its input, defers a PASS to the classifier where the mode
    is "auto", and otherwise answers "allow" itself, so that the checkers decide alone (D21).

    `stream_partial`: if true (default), text and thinking are also streamed token by token
    (`assistant.delta` events); otherwise they arrive per completed block only.

    `sandbox`: how Bash commands are isolated (`SandboxConfig`); calu passes it to Claude Code
    with `--settings`, which overrides the settings files in the project.
    """

    use_bedrock: bool = False
    """Set `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it runs on AWS Bedrock with
    the environment's AWS credentials. False leaves the variable as it is; calu does not
    otherwise touch how Claude Code authenticates."""
    permission_mode: (
        Literal["default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"] | None
    ) = None
    """Permission mode for Claude Code; None leaves it to Claude Code (its default is "auto")."""
    stream_partial: bool = True
    """Stream text and thinking token by token (`assistant.delta`). False delivers completed
    blocks only."""
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    """How Claude Code returns thinking: "summarized" (readable summaries, shown by front ends
    as `assistant.thinking`) or "omitted" (empty thinking blocks, nothing is shown)."""
    model: str | None = "opus"
    """Claude Code model alias ("fable", "opus", "sonnet", "haiku": always the latest of that
    family) or a full model id; None uses Claude Code's default."""
    effort: Effort | None = None
    """Reasoning effort of the Claude session ("low" to "max"); None uses Claude Code's
    default. `calu run --effort` overrides it for one session."""
    cli_path: str | None = None
    """Path to the `claude` executable; None looks it up on PATH."""
    setting_sources: list[str] = Field(default_factory=lambda: ["user", "project", "local"])
    """Which of Claude Code's own settings files it reads ("user", "project", "local"). All of
    them by default, as a plain `claude` does: calu wraps Claude Code and leaves how it loads
    its configuration, skills, MCP servers, plugins and auth alone (D178, the owner's answer
    to Q21). A narrower list is possible; the start-up warning then names what it leaves out.
    calu's `--settings` document overrides them for the sandbox either way."""
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    """Isolation of Bash commands (PLAN.md D15, D4)."""


class AutoEscalate(ConfigModel):
    """Categories whose steps always wait for the human, whatever the checker decided.

    `commands`: Bash and other tool calls reviewed by the command checker. `edits`: file edits.
    `subagents`: final results of subagents. The checker still runs; where it would have
    approved (ALLOW / ACCEPT / ANNOTATE), the step is escalated instead, with the checker's
    decision and reason in the escalation. A checker DENY stays a DENY. Read-only tools that the
    policy allows without a check are not affected. With training mode on as well, the training
    review (labelling the checker's decision) comes first, then the escalation (running the step).
    """

    commands: bool = False
    """Every command waits for a human, even one the command checker approved."""
    edits: bool = False
    """Every file edit waits for a human, even one the edit checker approved."""
    subagents: bool = False
    """Every subagent's final result waits for a human, even an accepted one."""


class WebConfig(ConfigModel):
    """Settings of the web interface (PLAN.md phase 6)."""

    git_url_template: str = ""
    """URL of one commit in whatever git browser the project uses, with `{commit}` and
    `{remote}` as placeholders, e.g. "http://localhost:8080/calu/commit/{commit}". Empty guesses
    the URL from the project's git remote, which works for GitHub, GitLab and Bitbucket; calu
    does not browse git itself."""


class ChangesConfig(ConfigModel):
    """Whether calu measures what a command wrote, and how much of it (PLAN.md D31).

    Off by default: it costs disk and hashing time in every session, while the attribution of
    `ClockConfig`/D32 needs nothing. A project turns it on when it wants the content of a
    command's writes and the ability to undo them.
    """

    snapshot: bool = False
    """Take a git snapshot of the project directory before and after every Bash step."""
    review: bool = False
    """Let a checker judge what a command wrote, once it has run, and undo the change if it
    refuses (needs `snapshot`). The agent waits for that review, so a refusal is not a comment
    on something that already stands: the files go back to the state from before the command."""
    max_file_bytes: int = Field(default=1_000_000, ge=0)
    """Files above this size are reported by path and size, but their content is not shown."""
    max_patch_bytes: int = Field(default=200_000, ge=0)
    """Upper bound on the patch a checker or a client ever sees."""
    always_capture: list[str] = Field(default_factory=lambda: [".env"])
    """Paths to snapshot even though a `.gitignore` covers them, so that a write to them is
    noticed; they are the ones that matter most and the ones git would otherwise hide."""


HonestyMode = Literal["off", "track", "enforce"]
"""What calu does with the honesty register (PLAN.md D83)."""


class HonestyConfig(ConfigModel):
    """Whether dishonest code is recorded, and whether it may be run (PLAN.md §13).

    A checker whose instructions ask for it records which files or symbols say something other
    than what they do, and clears the record when they agree again. The register is enforced
    where temporary dishonesty becomes a risk: a step that would **run** dirty code is denied.
    Editing it is never blocked, because fixing the dishonesty is an edit.
    """

    mode: HonestyMode = "off"
    """`off`: the register is neither written nor read, and a checker's marks are ignored.
    `track`: marks are recorded and reported (`calu honesty list`, the web interface) and
    nothing is blocked -- which is how to judge the checker on a real project before trusting
    it. `enforce`: a step that would run dirty code is denied, with the mark's reason. Default
    `off`, because the judgement needs a capable checker model (Q17)."""
    scope: Literal["project", "branch"] = "project"
    """Where a mark applies. `project`: on every branch of the project, so a mark made on
    `feature/x` also blocks the same path on `main`. `branch`: only on the branch it was made on
    -- a mark from another branch neither blocks nor reaches the checkers, and
    `calu honesty list` still shows it as stale. A mark without a recorded branch applies
    everywhere. Default `project` until the owner decides Q19 (PLAN.md D142)."""


class ClockConfig(ConfigModel):
    """How closely calu watches its own clock (PLAN.md D32, `calu.clock`).

    A checker attributes a file change to a step by comparing the file's change time with the
    times of the steps, which only holds while the wall clock moves forward. calu therefore
    compares `CLOCK_REALTIME` with `CLOCK_BOOTTIME`, which cannot be set.
    """

    check_interval_s: float = Field(default=10.0, ge=0)
    """Seconds between samples; 0 switches the watch off. The interval only decides how soon a
    jump is noticed, not whether it is: each sample is compared with the previous one. It is
    also how often the session tells the database that it is still running, and a herd calls a
    session stale after 180 s of silence, so an interval near that threshold makes a running
    session look dead in `calu sessions` (the start warns about it)."""
    max_backward_s: float = Field(default=1.0, ge=0)
    """A backward move up to this size counts as a time-server correction. Above it, the session
    escalates its next tool call, because the attribution of file changes is no longer sound."""
    max_forward_s: float = Field(default=300.0, ge=0)
    """A forward move above this size is logged (a first time-server sync after a boot jumps
    forward legitimately, so it is not an escalation)."""


class Config(ConfigModel):
    """The complete configuration with defaults for every field."""

    command_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews Bash commands and every tool call routed to it."""
    edit_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews a file edit before it is written."""
    subagent_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews what a subagent hands back."""
    honesty: HonestyConfig = Field(default_factory=HonestyConfig)
    """Whether dishonest code is recorded and whether it may be run (§13)."""
    changeset_checker: CheckerConfig = Field(default_factory=lambda: CheckerConfig(request_limit=8))
    """The checker that reviews a whole change before a commit (PLAN.md D26). It gets more
    requests than a step checker, because judging a whole change means reading some of the
    files around the diff: with five, measured, one check in four ran out before deciding
    (D75)."""
    check_subagent_results: bool = True
    """Review the final result of every subagent with the subagent checker."""
    auto_escalate: AutoEscalate = Field(default_factory=AutoEscalate)
    """Categories that always wait for the human (see `AutoEscalate`); all off by default."""
    escalation_timeout_s: float = Field(default=8 * 3600, ge=0)
    """How long an escalation waits for a human before the session stops (PLAN.md D27).

    An unsupervised run can sit in front of an escalation for a long time; at some point the
    session should give the machine and the Claude session back instead of waiting for ever. On
    the timeout the step is denied and the session stops, with Claude's session id recorded, so
    that `calu run --resume <id>` takes the conversation up again. 0 waits without a limit.
    """
    store_failures_before_stop: int = Field(default=10, ge=0)
    """How many database writes may fail in a row before the session stops (PLAN.md D59).

    A session that cannot write is a session nobody can watch or unblock: its steps, its turns
    and its escalations do not reach the database, and with the heartbeat of D56 the fleet shows
    it as gone. Stopping at the first failure would make a network hiccup end an overnight run,
    so failures are tolerated and counted, and the count resets on the next write that works.
    0 never stops, which keeps a session running blind -- only sensible when somebody is
    attached to it.
    """
    hidden_env: list[str] = Field(default_factory=list)
    """Further environment variables (e.g. API keys of other checker providers) that Claude
    Code must not see, in addition to the ones from the env files and `PROVIDER_KEYS`."""
    tool_routes: dict[str, Route] = Field(default_factory=dict)
    """Route per tool name, overriding the built-in table (`calu.checkers.policy`), e.g.
    `{"mcp__deploy__ship": "escalate"}`."""
    default_route: Route = Route.COMMAND
    """Route for a tool the built-in table does not know, including MCP tools and tools a
    newer Claude Code adds. "command" means such a call is checked rather than allowed."""
    context: ContextConfig = Field(default_factory=ContextConfig)
    """What the checkers read as context, and how the conversation is folded."""
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    """The Claude Code session itself: model, effort, permission mode, sandbox."""
    clock: ClockConfig = Field(default_factory=ClockConfig)
    """Watching the system clock for tampering (PLAN.md D32)."""
    changes: ChangesConfig = Field(default_factory=ChangesConfig)
    """Measuring what a command wrote, and reviewing it afterwards (PLAN.md D31)."""
    web: WebConfig = Field(default_factory=WebConfig)
    """Settings of the web interface."""

    def with_model(self, model: str) -> Config:
        """Return a copy in which every model role uses `model`.

        The roles are the command, edit and subagent result checkers, the narrator and the
        summarizer (e.g. for a test run with one model).

        Args:
            model: A Pydantic AI model string, e.g. "mistral:mistral-small-2603",
                "fireworks:accounts/fireworks/models/..." or "bedrock:eu.anthropic....".

        Returns:
            A new `Config`; `self` is unchanged.
        """
        return self.model_copy(
            update={
                "command_checker": self.command_checker.model_copy(update={"model": model}),
                "edit_checker": self.edit_checker.model_copy(update={"model": model}),
                "subagent_checker": self.subagent_checker.model_copy(update={"model": model}),
                "changeset_checker": self.changeset_checker.model_copy(update={"model": model}),
                "context": self.context.model_copy(
                    update={"narrator_model": model, "summarizer_model": model}
                ),
            }
        )


def merge_settings(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a partial configuration into another, key by key.

    Args:
        base: The configuration so far (not modified).
        override: Keys to set; a dict value merges into a dict value of `base`, any other
            value replaces it.

    Returns:
        A new dict.

    Example:
        merge_settings({"claude": {"model": "opus", "effort": "low"}},
                       {"claude": {"model": "sonnet"}})
        == {"claude": {"model": "sonnet", "effort": "low"}}
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


CHECKER_FIELDS = {
    "command": "command_checker",
    "edit": "edit_checker",
    "subagent-result": "subagent_checker",
    "changeset": "changeset_checker",
}
"""Checker type names of the database mapped to the `Config` fields they configure."""


def build_config(
    layers: Iterable[Mapping[str, Any]],
    checker_rows: Iterable[tuple[str, str, int, int, int]] = (),
) -> Config:
    """Build the configuration from database values.

    A checker definition carries a model and limits of its own (`calu checker set --model`),
    and so does the configuration (`calu config set command_checker.model`). **The
    configuration wins**, because that is the layer a project overrides: the definition's
    values come first as the default, and the global and then the project settings are merged on
    top. Before 2026-11-10 it was the other way round, which made `calu config set
    command_checker.model` -- documented in CALU_GUIDE.md and offered by the web interface's
    settings form -- silently do nothing.

    Args:
        layers: Partial configurations in increasing precedence (global, then project).
        checker_rows: (name, model, request_limit, output_tokens_limit, max_tokens) of the
            enabled checker types. "command", "edit" and "subagent-result" fill the checker
            fields of `Config`; "narrator" fills `context.narrator_model`; other names are
            ignored here.

    Returns:
        The validated configuration.

    Raises:
        pydantic.ValidationError: If the merged values do not match `Config`.
    """
    merged: dict[str, Any] = {}
    for name, model, request_limit, output_tokens_limit, max_tokens in checker_rows:
        if name in CHECKER_FIELDS:
            merged = merge_settings(
                merged,
                {
                    CHECKER_FIELDS[name]: {
                        "model": model,
                        "request_limit": request_limit,
                        "output_tokens_limit": output_tokens_limit,
                        "max_tokens": max_tokens,
                    }
                },
            )
        elif name == "narrator":
            merged = merge_settings(merged, {"context": {"narrator_model": model}})
    for layer in layers:
        merged = merge_settings(merged, layer)
    return Config.model_validate(merged)


def user_env_file() -> Path:
    """Return the path of calu's env file for checker API keys.

    Returns:
        `$XDG_CONFIG_HOME/calu/.env` or `~/.config/calu/.env`.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "calu" / ".env"


PROVIDER_KEYS = frozenset(
    {
        "MISTRAL_API_KEY",
        "FIREWORKS_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "LOGFIRE_TOKEN",
    }
)
"""Keys of model providers used by checkers; always hidden from Claude Code."""

PROVIDER_KEYS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "mistral": ("MISTRAL_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}
"""Pydantic AI model-string prefix -> environment variables, any one of which holds the key.
Prefixes that are not listed (e.g. "bedrock", "ollama", "test") need no key or take their
credentials from elsewhere, so they are not checked."""


def load_secrets(env_file: Path | None = None) -> set[str]:
    """Load checker API keys from calu's env file into `os.environ`.

    Variables that are already set in the environment are not overwritten. A project's own
    `.env` is deliberately not read: it may hold unrelated secrets, which Claude must not see.

    Args:
        env_file: The env file; None uses `user_env_file()`. A missing file is skipped.

    Returns:
        The names of all variables defined in the file, plus `PROVIDER_KEYS`; the runner hides
        them from Claude Code.
    """
    names = set(PROVIDER_KEYS)
    path = env_file or user_env_file()
    if path.is_file():
        values = dotenv_values(path)
        names |= set(values)
        for key, value in values.items():
            if value is not None and key not in os.environ:
                os.environ[key] = value
    return names
