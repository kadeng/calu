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

import functools
import importlib.util
import inspect
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calu.checkers.model import CheckerEffort
from calu.checkers.policy import Route
from calu.paths import calu_private_paths

__all__ = ["calu_private_paths"]  # re-exported: `calu.paths` holds it, callers import it here

EMPTY_CHAT_SUMMARY = "# Chat summary\n\nNo turns have been folded into this summary yet.\n"
"""Chat summary of a session without folded turns."""

SETTINGS_KEY = "settings"
"""Key of the `config` rows that hold a partial `Config`."""


ANTHROPIC_HAIKU = "anthropic:claude-haiku-5-5"
"""The model of every role whose model is left blank: the current Haiku on the Anthropic API."""

BEDROCK_EU_HAIKU = "bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0"
"""The same default in Bedrock mode (`claude.use_bedrock`): Haiku through AWS Bedrock's EU
inference profile, which keeps the requests in EU regions."""


def default_model(use_bedrock: bool) -> str:
    """Return the model a role uses when its model is left blank (PLAN.md D194).

    Args:
        use_bedrock: Whether Bedrock mode is on (`claude.use_bedrock`).

    Returns:
        `BEDROCK_EU_HAIKU` in Bedrock mode, else `ANTHROPIC_HAIKU`.
    """
    return BEDROCK_EU_HAIKU if use_bedrock else ANTHROPIC_HAIKU


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

    model: str = ""
    """Pydantic AI model string of this checker; blank uses the default: Haiku on the Anthropic
    API, or Haiku through AWS Bedrock in an EU region when `claude.use_bedrock` is on. Other
    values are e.g. "mistral:mistral-small-2603",
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


CHANGESET_REQUEST_LIMIT = 40
"""Model requests of one changeset check, tool calls included (a step checker has 5)."""

CHANGESET_OUTPUT_TOKENS_LIMIT = 120_000
"""Token budget for all answers of one changeset check, thinking included."""

CHANGESET_MAX_TOKENS = 16_000
"""Token cap for one answer of the changeset checker."""


def changeset_limits() -> CheckerConfig:
    """Return the default limits of the changeset checker (PLAN.md D197).

    Returns:
        A `CheckerConfig` with a blank model and the three `CHANGESET_*` limits.
    """
    return CheckerConfig(
        request_limit=CHANGESET_REQUEST_LIMIT,
        output_tokens_limit=CHANGESET_OUTPUT_TOKENS_LIMIT,
        max_tokens=CHANGESET_MAX_TOKENS,
    )


class ContextConfig(ConfigModel):
    """Context summary settings (see `calu.context`).

    Every `fold_every` unfolded turns, the oldest `fold_count` turns are folded into the chat
    summary. No file of the workspace is part of a checker's context: the agent under review
    can write there, so project rules that must bind belong in the checker instructions
    (PLAN.md D193). A stored `context.files` or `context.skill_dirs` of an earlier version is
    ignored.
    """

    fold_every: int = Field(default=30, ge=2)
    """Fold the oldest turns into the chat summary once this many turns are unfolded."""
    fold_count: int = Field(default=20, ge=1)
    """How many of the oldest turns are folded into the chat summary each time."""
    summarizer_model: str = ""
    """Model that writes the chat summary when turns are folded; blank uses the default (see
    a checker's `model`)."""
    narrate: bool = True
    """Narrate Claude's thinking and text for the checkers (`calu.context.narrator`)."""
    narrator_model: str = ""
    """Model that narrates Claude's thinking and text in indirect speech, so that a checker
    does not read it as an instruction; blank uses the default (see a checker's `model`)."""
    max_recent_chars: int = Field(default=40_000, ge=1_000)
    """Upper bound in characters for the unfolded turns in a checker's context."""


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
defaults additionally deny the container sockets, `/run/dbus` and `/run/user`.

The two calu entries are listed for the reader of the setting; removing them changes nothing,
because `SandboxConfig.settings` always adds `calu_private_paths()`."""


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

    none    `allowedDomains` = []: nothing is reachable, not even through the proxy
    proxy   `allowedDomains` = the configured list: only those domains
    full    `allowedDomains` = ["*"]: everything, minus `deniedDomains`, which still wins
"""


class SandboxConfig(ConfigModel):
    """Isolation of Bash commands by Claude Code's own sandbox (PLAN.md D15).

    calu passes these settings with `--settings`, which has precedence over the project's and
    the user's settings files, so a settings file does not switch the sandbox off. `settings()`
    writes each key of `PINNED_SANDBOX_KEYS`, also when its value is empty or false, so that
    the document itself says what every one of them is. Measured with CLI 2.1.319
    (`spikes/RESULTS.md`, "Can a settings file widen the pinned sandbox?"): while `--settings`
    carries that document, `filesystem.allowRead`, `network.allowedDomains` and
    `excludedCommands` of the project's `.claude/settings.json` and
    `.claude/settings.local.json` have no effect. Not measured: the user's
    `~/.claude/settings.json`, the other keys, and other CLI versions. calu therefore does not
    rely on the precedence alone: with `fail_if_unavailable`, a session does not start, and a
    Bash step waits for a human, while a loaded settings file holds a sandbox entry that goes
    beyond calu's configuration (`calu.preflight.settings_file_sandbox_findings`); without it,
    such entries are named in a start-up warning. The sandbox needs `bwrap` and `socat` on the
    machine (`calu.preflight`).

    calu's own directories (`calu_private_paths()`) are always on the read deny list; the
    `deny_read` setting holds further paths and cannot remove them.

    `fail_if_unavailable` makes Claude Code refuse to start without a working sandbox, instead
    of running commands unsandboxed, and makes calu refuse to start a session whose settings
    files hold sandbox entries beyond calu's configuration. `allow_unsandboxed_commands` False
    stops a call from leaving the sandbox with `dangerouslyDisableSandbox`. `auto_allow_bash`
    stays False: Claude Code would otherwise approve sandboxed Bash calls by itself, which
    would skip the auto-mode classifier that PLAN.md D5 uses as the second opinion next to the
    checkers.

    `excluded_commands` run outside the sandbox and are therefore only covered by the checkers
    and the deny lists; the default is empty. `allowed_domains` and `denied_domains` configure
    the sandbox's network: with an empty `allowed_domains` the sandbox blocks outgoing network
    access (verified in `spikes/RESULTS.md`).
    """

    enabled: bool = True
    """Isolate Bash commands with Claude Code's own sandbox. Off removes that isolation and
    leaves only the checkers and the deny lists."""
    fail_if_unavailable: bool = True
    """Refuse to start when calu cannot be sure of the sandbox: when none can be built
    (Claude Code would otherwise run commands unsandboxed), and when one of Claude Code's
    settings files holds a sandbox entry that goes beyond calu's configuration (a Bash step
    also waits for a human while such an entry exists). False turns both into warnings."""
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
    entries with `calu config add|remove claude.sandbox.deny_read <path>`. calu's own
    directories (its credentials, its env file with the checker keys, its snapshots) are
    denied whatever this list holds, also when it is empty."""
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

    def denied_read_paths(self) -> list[str]:
        """Return the read deny list as Claude Code gets it.

        Returns:
            `calu_private_paths()` followed by the entries of `deny_read`, without duplicates.
            Never empty: calu's own directories are denied whatever `deny_read` holds.
        """
        paths = calu_private_paths()
        for path in self.deny_read:
            if path not in paths:
                paths.append(path)
        return paths

    def settings(self) -> dict[str, Any]:
        """Return the Claude Code settings document for `--settings`.

        The document holds exactly the keys of `PINNED_SANDBOX_KEYS`, each of them always,
        also with an empty or false value. Four of them have no setting in calu and a fixed
        value: `filesystem.allowRead` and `filesystem.allowWrite` are empty (calu re-opens
        nothing inside a denied path and adds no writable path), and
        `enableWeakerNetworkIsolation`, `network.allowAllUnixSockets` and
        `network.allowLocalBinding` are false. The complete document was run through CLI
        2.1.319, which accepts it (`spikes/spike_settings_widen.sh`).

        Returns:
            `{"sandbox": {...}}` with the keys Claude Code expects (camel case).
            `filesystem.denyRead` is `denied_read_paths()`.

        Example:
            `SandboxConfig().settings()["sandbox"]["failIfUnavailable"]` is True, and
            `SandboxConfig(deny_read=[]).settings()["sandbox"]["filesystem"]["denyRead"]`
            still starts with "~/.config/calu".
        """
        sandbox: dict[str, Any] = {
            "enabled": self.enabled,
            "failIfUnavailable": self.fail_if_unavailable,
            "allowUnsandboxedCommands": self.allow_unsandboxed_commands,
            "autoAllowBashIfSandboxed": self.auto_allow_bash,
            "enableWeakerNestedSandbox": self.weaker_nested_sandbox,
            "enableWeakerNetworkIsolation": False,
            "excludedCommands": list(self.excluded_commands),
            "filesystem": {
                "denyRead": self.denied_read_paths(),
                "allowRead": [],
                "allowWrite": [],
            },
            "network": {
                "allowedDomains": list(self.allowed_domains),
                "deniedDomains": list(self.denied_domains),
                "allowUnixSockets": list(self.allow_unix_sockets),
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
        }
        return {"sandbox": sandbox}


PINNED_SANDBOX_KEYS = (
    "enabled",
    "failIfUnavailable",
    "allowUnsandboxedCommands",
    "autoAllowBashIfSandboxed",
    "enableWeakerNestedSandbox",
    "enableWeakerNetworkIsolation",
    "excludedCommands",
    "filesystem.denyRead",
    "filesystem.allowRead",
    "filesystem.allowWrite",
    "network.allowedDomains",
    "network.deniedDomains",
    "network.allowUnixSockets",
    "network.allowAllUnixSockets",
    "network.allowLocalBinding",
)
"""The keys of the `sandbox` object that `SandboxConfig.settings` writes, as dotted paths. A
test compares this list with the document, so the docstrings that refer to it stay true."""


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

    `permission_mode`: None (default) leaves the permission mode to Claude Code, which takes
    it from its own settings (`permissions.defaultMode`). Measured with CLI 2.1.319: a headless
    start without that setting reports the mode "default", not "auto". The auto mode is the
    intended way to run calu, because then a step runs only if the checkers (or the allow
    lists) **and** auto mode's classifier approve it (PLAN.md D5); set "auto" here or in Claude
    Code's settings to get it. Whatever the mode ends up being, the checkers work: the hook
    reads the effective mode from its input, defers a PASS to the classifier where the mode is
    "auto", and otherwise answers "allow" itself, so that the checkers decide alone (D21). The
    runner reports the effective mode as a `log` event at the start and whenever it changes,
    as a warning while it is not "auto". Before that, the start-up warnings and `calu doctor`
    say which mode is expected and whether the classifier will be active
    (`calu.preflight.permission_mode_line`).

    `require_auto_mode`: if true, a step the checkers PASS while the effective mode is not
    "auto" waits for a human instead of running on the checkers' word alone.

    `stream_partial`: if true (default), text and thinking are also streamed token by token
    (`assistant.delta` events); otherwise they arrive per completed block only.

    `sandbox`: how Bash commands are isolated (`SandboxConfig`); calu passes it to Claude Code
    with `--settings`, which has precedence over the settings files in the project.
    """

    use_bedrock: bool = False
    """Set `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it runs on AWS Bedrock with
    the environment's AWS credentials. False leaves the variable as it is; calu does not
    otherwise touch how Claude Code authenticates."""
    permission_mode: (
        Literal["default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"] | None
    ) = None
    """Permission mode for Claude Code; None leaves it to Claude Code's own settings
    (`permissions.defaultMode`; without that setting a headless start runs in the mode
    "default", where the checkers decide alone). "auto" adds auto mode's classifier as the
    second approval next to the checkers."""
    require_auto_mode: bool = False
    """Whether a step needs auto mode's classifier as well as the checkers. False (default):
    outside the auto mode (another mode was chosen, the model has none, or a tool switched
    the mode during the session) a step the checkers pass runs, and the session logs a
    warning that the classifier is not active. True: such a step waits for a human instead."""
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
    calu's `--settings` document has precedence over them for the sandbox either way; a
    sandbox entry of these files that goes beyond calu's own configuration keeps a session
    from starting (`claude.sandbox.fail_if_unavailable`), or is named in a start-up warning
    when that setting is off."""
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
    """`off`: a checker's marks are ignored. `track`: marks are kept in the session's memory
    (never stored, D180) and shown in `calu attach`, the web live view and an ACP editor, and
    nothing is blocked -- which is how to judge the checker on a real project before trusting
    it. `enforce`: a step that would run dirty code is denied, with the mark's reason. Default
    `off`, because the judgement needs a capable checker model (Q17)."""
    rules: str | None = None
    """The honesty standard the checkers that judge changes read: what dishonesty is and what an
    API contract must say (PLAN.md D181). Empty uses the rules calu ships
    (`calu honesty show`). Set it, globally or per project, to fit a project's documentation
    standards (`calu honesty edit [--project NAME]`); it is not meant to be edited often."""


class ClockConfig(ConfigModel):
    """How closely calu watches its own clock (PLAN.md D32, `calu.clock`).

    A checker attributes a file change to a step by comparing the file's change time with the
    times of the steps, which only holds while the wall clock moves forward. calu therefore
    compares `CLOCK_REALTIME` with `CLOCK_BOOTTIME`, which cannot be set.
    """

    check_interval_s: float = Field(default=10.0, ge=0)
    """Seconds between samples; 0 switches the watch off. The interval only decides how soon a
    jump is noticed, not whether it is: each sample is compared with the previous one. The
    heartbeat that tells the database the session is still running has a fixed interval of its
    own and does not depend on this value."""
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
    changeset_checker: CheckerConfig = Field(default_factory=lambda: changeset_limits())
    """The checker that reviews a whole change before a commit (PLAN.md D26). Its limits are
    far above a step checker's (`changeset_limits`): judging a whole change means reading the
    files around the diff, following callers and comparing the result with what was asked,
    which takes many tool calls and a long answer."""
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
    Code must not see, in addition to the ones from the env files and `PROVIDER_KEYS`. The
    runner blanks them in the environment of the Claude Code process, so its commands do not
    inherit them either. A changed list applies to the next session start. Each entry is the
    name of one variable (e.g. `XAI_API_KEY`), without a value. Add or remove single names
    with `calu config add|remove hidden_env <NAME>`."""
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

    @field_validator("hidden_env")
    @classmethod
    def _variable_names(cls, names: list[str]) -> list[str]:
        """Accept only names an environment variable can have.

        An entry such as `A=B` would otherwise fail much later, when the Claude Code process
        is started with it, and with an error that says nothing about this setting.

        Args:
            names: The configured entries.

        Returns:
            The entries, unchanged.

        Raises:
            ValueError: If an entry is empty, or contains "=", a NUL character or white space.
        """
        for name in names:
            if not name or "=" in name or "\0" in name or any(c.isspace() for c in name):
                raise ValueError(
                    f"hidden_env holds names of environment variables, and {name!r} is not "
                    'one: a name is not empty and has no "=", white space or NUL character'
                )
        return names

    @model_validator(mode="after")
    def _fill_blank_models(self) -> Config:
        """Give every role whose model is blank the default model (PLAN.md D194).

        Runs on every validation, so a `Config` never holds a blank model: readers use
        `command_checker.model`, `context.narrator_model` and the others as they are. What is
        stored in the database stays blank, so switching Bedrock mode later changes the
        default with it.

        Returns:
            The configuration itself, with the blank models filled.
        """
        model = default_model(self.claude.use_bedrock)
        for checker in CHECKER_FIELDS.values():
            section = getattr(self, checker)
            if not section.model.strip():
                section.model = model
        if not self.context.narrator_model.strip():
            self.context.narrator_model = model
        if not self.context.summarizer_model.strip():
            self.context.summarizer_model = model
        return self

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
    "openai-chat": ("OPENAI_API_KEY",),
    "openai-responses": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}
"""Pydantic AI model-string prefix -> environment variables, any one of which holds the key.
These are the providers whose missing key `calu.preflight.key_warnings` reports. Every variable
here except `ANTHROPIC_API_KEY` (which Claude Code itself may use) is in `PROVIDER_KEYS`."""

KEYLESS_PREFIXES = frozenset({"bedrock", "claude", "function", "google-vertex", "ollama", "test"})
"""Model-string prefixes that take no API key from an environment variable of their own: AWS and
Google application credentials, the local `claude` CLI, a local server, and the test models."""

_GETENV = re.compile(r"""os\.getenv\(\s*['"]([A-Z][A-Z0-9_]*)['"]""")
"""A variable a Pydantic AI provider module reads, as its source spells it."""
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)")
"""Which of those variables hold a credential (and not a base URL, a region or a version)."""


@functools.cache
def provider_key_variables(prefix: str) -> tuple[str, ...] | None:
    """Return the environment variables that hold the API key of a model provider.

    The names in `PROVIDER_KEYS_BY_PREFIX` are used where the prefix is listed. For every other
    provider the names are taken from the installed Pydantic AI itself: the provider's module is
    the code that reads the key, so the variables it reads with `os.getenv` and that are named
    like a credential are the ones a key can be in. Nothing is guessed from the prefix: the
    variable of `xai:` is `XAI_API_KEY` and that of `huggingface:` is `HF_TOKEN`.

    Reads the module's source file; the provider's own SDK is not needed, and nothing of the
    provider is called. The result is cached per prefix.

    Args:
        prefix: The part of a model string before the colon, e.g. "xai" or "azure".

    Returns:
        The variable names in the order the module reads them; an empty tuple for a prefix of
        `KEYLESS_PREFIXES`; None when Pydantic AI has no provider of that name or its module
        reads no credential variable, which means calu does not know the variable.

    Example:
        `provider_key_variables("xai")` is `("XAI_API_KEY",)`;
        `provider_key_variables("no-such-provider")` is None.
    """
    if prefix in PROVIDER_KEYS_BY_PREFIX:
        return PROVIDER_KEYS_BY_PREFIX[prefix]
    if prefix in KEYLESS_PREFIXES:
        return ()
    source = _provider_source(prefix)
    names = [name for name in _GETENV.findall(source) if _SECRET_NAME.search(name)]
    return tuple(dict.fromkeys(names)) or None


def _provider_source(prefix: str) -> str:
    """Return the source text of the Pydantic AI module that implements a provider.

    Args:
        prefix: The provider name of a model string.

    Returns:
        The module's source, or "" when Pydantic AI knows no such provider or the source
        cannot be read. A provider whose own SDK is not installed is still found, by the
        module name Pydantic AI uses for it (the prefix with "-" as "_").
    """
    path: str | None = None
    try:
        from pydantic_ai.providers import infer_provider_class

        path = inspect.getsourcefile(infer_provider_class(prefix))
    except Exception:  # unknown name (ValueError), missing SDK (ImportError), no source
        try:
            spec = importlib.util.find_spec(f"pydantic_ai.providers.{prefix.replace('-', '_')}")
        except ImportError, ValueError:
            spec = None
        path = spec.origin if spec is not None else None
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


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
